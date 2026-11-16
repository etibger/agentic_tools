"""Synthetic submissions over the production HTTPS consumer; no human proof."""
from contextlib import contextmanager
from html import unescape
from http.client import HTTPSConnection
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlencode

import pytest

from agentic_tools.aos import human_intent
from agentic_tools.aos.intent_operator import IntentService
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store
from test_aos_lifecycle import setup


@pytest.fixture
def fixture(tmp_path, request):
    store, lifecycle, _ = setup(tmp_path, admitted=False, approved=getattr(request, 'param', True))
    setup(tmp_path, store=store, work_id='OTHER', admitted=False)
    key, cert = store.directory / 'tls.key', store.directory / 'tls.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-keyout', str(key), '-out', str(cert)],
                   capture_output=True, check=True, timeout=15)
    key.chmod(0o600)
    operator = store.directory / 'operator.key'
    operator.write_text('SIMULATED-operator-secret-with-at-least-32-characters')
    operator.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return store, lifecycle, {'certificate': str(cert), 'key': str(key), 'operator_key': operator,
                              'port': port, 'work_id': 'Q1', 'qualification_reference': 'SIMULATED:operator-TLS-test'}


class Browser:
    def __init__(self, service):
        self.service = service
        self.cookie = ''

    def request(self, method='GET', route='/', fields=None, **headers):
        conn = HTTPSConnection('127.0.0.1', self.service.port, context=ssl._create_unverified_context(), timeout=4)
        actual = {'Host': self.service.origin.removeprefix('https://'), 'Origin': self.service.origin,
                  'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/x-www-form-urlencoded',
                  'Cookie': self.cookie, **headers}
        conn.request(method, route, urlencode(fields) if fields is not None else None, actual)
        response = conn.getresponse()
        body = response.read().decode()
        if response.getheader('Set-Cookie'):
            self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            assert 'Secure' in response.getheader('Set-Cookie')
            assert 'HttpOnly' in response.getheader('Set-Cookie')
            assert 'SameSite=Strict' in response.getheader('Set-Cookie')
        status = response.status
        conn.close()
        return status, body

    def login(self, key, **headers):
        assert self.request('POST', '/unlock', {'operator_key': key.read_text().strip()}, **headers)[0] == 200

    def form(self, **changes):
        """Synthetic maintenance input, including actions absent from normal UI."""
        status, body = self.request()
        assert status == 200
        fields = {name: unescape(value) for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', body)}
        if not fields:
            controller = self.service.controller
            csrf = controller.csrf(self.cookie.split('=', 1)[1])
            assert csrf  # Trusted synthetic seam; never a deployed human action.
            current = controller.status()
            with controller.store._connection() as db:
                owner = json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (controller.work_id,)).fetchone()[0])['receiving_owner']
            fields = {'csrf': csrf, 'input_id': controller.issue_form(csrf, current), 'owner': current.get('owner', owner),
                      'reference': current.get('controlling_instruction') or '',
                      'supersedes': ' '.join(current.get('current_heads', []) + [r['conflict_id'] for r in current.get('conflicts', [])])}
        return {**fields, 'action': 'resume', **changes}

    def submit(self, fields, expected=200, **headers):
        status, body = self.request('POST', '/instruction', fields, **headers)
        assert status == expected, body
        return body


@contextmanager
def running(fixture):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        browser.login(options['operator_key'])
        yield browser, lifecycle
    assert service.cleanup == {'listener_stopped': True, 'requests_stopped': True}


def confirmation_fields(page, action='resume'):
    """Use only the actual public form, with no synthetic maintenance seam."""
    inputs = re.findall(r'<input\b([^>]*)>', page)
    assert len(inputs) == 6 and all('type="hidden"' in item for item in inputs)
    fields = {name: unescape(value) for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)}
    assert set(fields) == {'csrf', 'input_id', 'action', 'owner', 'reference', 'supersedes'}
    assert fields['action'] == action and '<textarea' not in page
    assert '<button>Yes</button>' in page and page.index('</form>') < page.index('href="/?' + action + '=no"')
    return fields


@pytest.mark.parametrize('action', ['stop', 'pause'])
@pytest.mark.parametrize('title,question', [('Disposable AOS qualification work', 'Resume the disposable AOS qualification work?'),
                                          ('A different project', 'Resume this work?')])
def test_contextual_yes_no_carries_exact_metadata_and_no_is_read_only(fixture, action, title, question):
    store, _, _ = fixture
    with store._transaction() as db:
        db.execute("UPDATE work SET title=? WHERE work_id='Q1'", (title,))
    other = store.read('OTHER')
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action=action))
        before = store.read('Q1')
        status, page = browser.request()
        assert status == 200 and '<h1>' + title + '</h1>' in page and question in page
        assert 'Status: <strong>' + action.capitalize() + ' instruction in effect</strong>' in page
        fields = confirmation_fields(page)
        current = before['lifecycle']['human_intent']
        assert fields['owner'] == current['owner'] and fields['reference'] == current['controlling_instruction']
        assert fields['supersedes'].split() == current['current_heads']
        visible = re.sub(r'<input[^>]*>', '', page.split('<details>', 1)[0])
        assert current['controlling_instruction'] not in visible and current['owner'] not in visible
        assert 'Enable controls' not in visible and 'Replay original' not in visible and 'Check same-owner' not in visible
        with store._connection() as db:
            count = db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0]
        status, declined = browser.request('GET', '/?resume=no')
        assert status == 200 and 'No resume instruction was submitted.' in declined and '<form' not in declined
        assert store.read('Q1')['revision'] == before['revision']
        assert lifecycle.snapshot('Q1')['human_intent'] == current
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == count
        result = browser.submit(fields, Origin='null')
        assert 'Your resume instruction is saved.' in result and '<details><summary>Technical evidence</summary>' in result
        after = store.read('Q1')
        assert after['lifecycle']['human_intent']['controlling_instruction'] == fields['input_id']
        assert after['lifecycle']['human_intent']['owner'] == current['owner']
        assert after['decisions'] == before['decisions']
        browser.submit(fields)  # Lost response retry remains the original instruction.
        assert store.read('Q1')['revision'] == after['revision']
    assert store.read('OTHER')['revision'] == other['revision']
    assert store.read('OTHER')['decisions'] == other['decisions']


def test_contextual_yes_cannot_bypass_newer_stop_and_retains_rejected_attempt(fixture):
    store, _, _ = fixture
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='stop'))
        fields = confirmation_fields(browser.request()[1])
        newer_stop = browser.form(action='stop')
        browser.submit(newer_stop)
        before = lifecycle.snapshot('Q1')['human_intent']
        browser.submit(fields, expected=409)
        assert lifecycle.snapshot('Q1')['human_intent'] == before
        assert before['controlling_instruction'] == newer_stop['input_id'] and before['disposition'] == 'stop'
        with store._connection() as db:
            assert db.execute('SELECT 1 FROM human_instruction WHERE instruction_id=?', (fields['input_id'],)).fetchone() is None
            assert db.execute('SELECT 1 FROM human_operator_input WHERE input_id=?', (fields['input_id'],)).fetchone()
        browser.submit(fields)  # Read back the uncertain attempt; never repeat its effect.
        assert lifecycle.snapshot('Q1')['human_intent'] == before


def test_running_contextual_stop_remains_accessible_and_stale_stop_cannot_override_resume(fixture):
    store, _, _ = fixture
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='resume'))
        before = store.read('Q1')
        page = browser.request()[1]
        assert '<h2>Stop this work?</h2>' in page
        stale_stop = confirmation_fields(page, 'stop')
        assert stale_stop['owner'] == before['lifecycle']['human_intent']['owner']
        assert stale_stop['supersedes'].split() == before['lifecycle']['human_intent']['current_heads']
        with store._connection() as db:
            count = db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0]
        assert 'No stop instruction was submitted.' in browser.request('GET', '/?stop=no')[1]
        assert store.read('Q1')['revision'] == before['revision']
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == count
        stopped = confirmation_fields(browser.request()[1], 'stop')
        assert 'Your stop instruction is saved.' in browser.submit(stopped)
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == stopped['input_id']
        resumed = confirmation_fields(browser.request()[1])
        browser.submit(resumed)
        browser.submit(stale_stop, expected=409)
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed['input_id']
        assert lifecycle.snapshot('Q1')['human_intent']['owner'] == stale_stop['owner']
        browser.submit(confirmation_fields(browser.request()[1], 'stop'))
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'stop'
        assert store.read('Q1')['decisions'] == before['decisions']


@pytest.mark.parametrize('changes', [{'owner': 'different-responsibility'}, {'work_id': 'OTHER'}, {'csrf': 'wrong'}])
def test_contextual_resume_preserves_owner_scope_and_csrf_checks(fixture, changes):
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='stop'))
        before = lifecycle.snapshot('Q1')['human_intent']
        fields = confirmation_fields(browser.request()[1])
        browser.submit({**fields, **changes}, expected=409)
        assert lifecycle.snapshot('Q1')['human_intent'] == before


def test_stop_instruction_label_does_not_claim_runtime_cleanup(fixture):
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='stop'))
        # Synthetic presentation fixture: saved Stop before runtime reconciliation.
        with lifecycle.store._transaction() as db:
            db.execute("UPDATE lifecycle SET execution='active' WHERE work_id='Q1'")
        page = browser.request()[1]
        assert 'Status: <strong>Stop instruction in effect</strong>' in page
        assert '<strong>Stopped</strong>' not in page and 'execution' in page and 'active' in page


@pytest.mark.parametrize('state', ['unenrolled', 'ambiguous', 'conflicting', 'cancelled', 'delivered', 'retired'])
def test_contextual_page_is_status_only_without_safe_resume_choice(fixture, state):
    with running(fixture) as (browser, lifecycle):
        if state != 'unenrolled':
            browser.submit(browser.form(action='enroll'))
        if state in {'conflicting', 'cancelled', 'delivered', 'retired'}:
            browser.submit(browser.form(action='stop'))
        if state == 'conflicting':
            browser.submit(browser.form(action='resume', supersedes=''))
        if state in {'cancelled', 'delivered', 'retired'}:
            # Disposable presentation fixtures only; not a terminal execution proof.
            with lifecycle.store._transaction() as db:
                db.execute("UPDATE lifecycle SET execution=? WHERE work_id='Q1'", (state,))
        before = lifecycle.snapshot('Q1')['human_intent']
        for route in ('/', '/?resume=no'):
            status, page = browser.request('GET', route)
            assert status == 200 and '<form' not in page and '<button' not in page and '<textarea' not in page
            assert lifecycle.snapshot('Q1')['human_intent'] == before


@pytest.mark.parametrize('fixture', [False], indirect=True)
def test_contextual_yes_saves_resume_but_keeps_missing_approval_hold(fixture):
    store, _, _ = fixture
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='stop'))
        page = browser.submit(confirmation_fields(browser.request()[1]))
        assert 'Separate approval and other checks still apply.' in page
        record = store.read('Q1')
        assert record['lifecycle']['human_intent']['disposition'] == 'running'
        assert record['execution'] == 'paused' and record['decisions'] == []
        with store._connection() as db:
            assert db.execute("SELECT count(*) FROM assignment WHERE work_id='Q1'").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM resource WHERE work_id='Q1'").fetchone()[0] == 0


def test_selected_https_stop_resume_replay_later_stop_and_unrelated_preservation(fixture):
    store, _, _ = fixture
    other = store.read('OTHER')
    with running(fixture) as (browser, lifecycle):
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'unenrolled'
        browser.submit(browser.form(action='enroll'))
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'ambiguous'
        initial = browser.form(action='stop')
        browser.submit(initial)
        assert lifecycle.snapshot('Q1')['execution'] == 'paused'
        stale = browser.form(action='stop')
        resumed = browser.form(action='resume')
        browser.submit(resumed)
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'running'
        browser.submit(stale, expected=409)
        replay = browser.form(action='replay', reference=initial['input_id'])
        browser.submit(replay)
        browser.submit(initial)  # exact POST retry returns saved receipt, no new stop
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed['input_id']
        assert lifecycle.snapshot('Q1')['execution'] != 'paused'
        browser.submit(browser.form(action='handoff'))
        browser.submit(browser.form(action='handoff', owner='different-role'), expected=409)
        stopped = browser.form(action='stop')
        browser.submit(stopped)
        assert lifecycle.snapshot('Q1')['execution'] == 'paused'
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == stopped['input_id']
        with store._connection() as db:
            raw = json.loads(db.execute('SELECT payload FROM human_operator_input WHERE input_id=?', (stopped['input_id'],)).fetchone()[0])
            assert raw['action'] == 'stop' and 'csrf' not in raw
    reopened = Lifecycle(Store(store.directory, store.worker_writable_roots))
    assert reopened.snapshot('Q1')['human_intent']['disposition'] == 'stop'
    assert store.read('OTHER')['revision'] == other['revision']
    assert store.read('OTHER')['decisions'] == other['decisions']


def test_missing_provenance_conflicting_heads_and_heartbeat_do_not_bypass_or_invalidate_stop(fixture):
    store, _, _ = fixture
    with running(fixture) as (browser, lifecycle):
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='resume'))
        browser.submit(browser.form(action='unresolved'))
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'ambiguous'
        browser.submit(browser.form(action='handoff'), expected=409)
        browser.submit(browser.form(action='resume'))
        # Deliberately omit supersession: two original inputs have no established ordering.
        browser.submit(browser.form(action='stop', supersedes=''))
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'ambiguous'
        browser.submit(browser.form(action='resume'))
        form = browser.form(action='stop')
        with store._transaction() as db:
            lifecycle._touch(db, 'Q1', 'synthetic-progress', {})
        browser.submit(form)
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'stop'


@pytest.mark.parametrize('headers', [ {'Origin': 'https://foreign.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                                     {'Host': 'foreign.invalid'}, {'Content-Type': 'application/json'} ])
def test_foreign_origin_cross_site_host_and_content_type_reject(fixture, headers):
    with running(fixture) as (browser, lifecycle):
        form = browser.form(action='enroll')
        assert browser.request('POST', '/instruction', form, **headers)[0] == 409
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'unenrolled'


def test_same_origin_null_origin_unlock_controls_and_stale_replay(fixture):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        browser.login(options['operator_key'], Origin='null')
        browser.submit(browser.form(action='enroll'), Origin='null')
        stopped = browser.form(action='stop')
        browser.submit(stopped, Origin='null')
        assert lifecycle.snapshot('Q1')['execution'] == 'paused'
        stale = browser.form(action='stop')
        resumed = browser.form(action='resume')
        browser.submit(resumed, Origin='null')
        browser.submit(stale, expected=409, Origin='null')
        browser.submit(browser.form(action='replay', reference=stopped['input_id']), Origin='null')
        browser.submit(stopped, Origin='null')
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed['input_id']
        browser.submit(browser.form(action='stop'), Origin='null')
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'stop'
        browser.submit(browser.form(action='resume'), Origin='null')
        browser.submit(browser.form(action='handoff'), Origin='null')
        assert len(store.read('Q1')['decisions']) == 1  # Existing synthetic fixture decision only.


def raw_post(browser, route, body, headers):
    """Preserve missing/duplicate headers; the high-level client normalizes them."""
    conn = HTTPSConnection('127.0.0.1', browser.service.port, context=ssl._create_unverified_context(), timeout=4)
    try:
        conn.putrequest('POST', route, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read().decode(), response.getheader('Set-Cookie')
    finally:
        conn.close()


REQUIRED_HEADERS = ('Host', 'Origin', 'Sec-Fetch-Site', 'Content-Type', 'Content-Length')


@pytest.mark.parametrize('change,name,value', [
    *[('missing', name, '') for name in REQUIRED_HEADERS],
    *[('duplicate', name, '') for name in REQUIRED_HEADERS],
    ('replace', 'Host', 'foreign.invalid'),
    ('replace', 'Origin', 'https://foreign.invalid'),
    ('replace', 'Origin', ''),
    ('replace', 'Origin', 'NULL'),
    ('replace', 'Origin', 'null, null'),
    ('replace', 'Sec-Fetch-Site', 'cross-site'),
    ('replace', 'Sec-Fetch-Site', 'same-site'),
    ('replace', 'Sec-Fetch-Site', 'none'),
    ('replace', 'Content-Type', 'application/json'),
    ('replace', 'Content-Length', 'malformed'),
    ('extra', 'Transfer-Encoding', 'chunked'),
])
def test_null_origin_preserves_required_header_checks_before_unlock_or_control(fixture, change, name, value):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        for route in ('/unlock', '/instruction'):
            if route == '/unlock':
                fields = {'operator_key': options['operator_key'].read_text().strip()}
            else:
                browser.login(options['operator_key'], Origin='null')
                browser.submit(browser.form(action='enroll'), Origin='null')
                browser.submit(browser.form(action='stop'), Origin='null')
                fields = browser.form(action='resume')
            before = lifecycle.snapshot('Q1')['human_intent']
            with store._connection() as db:
                counts = [db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                          for table in ('human_operator_input', 'human_instruction')]
            body = urlencode(fields).encode()
            headers = [('Host', service.origin.removeprefix('https://')), ('Origin', 'null'),
                       ('Sec-Fetch-Site', 'same-origin'), ('Content-Type', 'application/x-www-form-urlencoded'),
                       ('Content-Length', str(len(body))), ('Cookie', browser.cookie)]
            if change in {'missing', 'replace'}:
                headers = [(key, item) for key, item in headers if key != name]
            if change == 'duplicate':
                value = next(item for key, item in headers if key == name)
            if change != 'missing':
                headers.append((name, value))
            status, page, cookie = raw_post(browser, route, body, headers)
            assert status == 409 and cookie is None
            assert 'Owner changes' not in page and 'SIMULATED-operator-secret' not in page
            if route == '/unlock':
                assert 'Unlock not confirmed' in page and 'No instruction was submitted' in page
                category = ('Unlock form is incomplete or malformed.' if name == 'Content-Length' and change == 'replace'
                            else 'Request checks rejected unlock.')
                assert category in page and 'Operator key did not match.' not in page
                assert not service.controller.sessions
            else:
                assert fields['csrf'] not in page
            assert lifecycle.snapshot('Q1')['human_intent'] == before
            with store._connection() as db:
                assert [db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                        for table in ('human_operator_input', 'human_instruction')] == counts


@pytest.mark.parametrize('body', [b'', b'malformed', b'operator_key=one&operator_key=two', b'\xff', b'x=' + b'a' * 16384])
def test_null_origin_does_not_accept_malformed_forms(fixture, body):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        for route in ('/unlock', '/instruction'):
            if route == '/instruction':
                browser.login(options['operator_key'], Origin='null')
                browser.submit(browser.form(action='enroll'), Origin='null')
                browser.submit(browser.form(action='stop'), Origin='null')
            before = lifecycle.snapshot('Q1')['human_intent']
            with store._connection() as db:
                count = db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0]
            headers = [('Host', service.origin.removeprefix('https://')), ('Origin', 'null'),
                       ('Sec-Fetch-Site', 'same-origin'), ('Content-Type', 'application/x-www-form-urlencoded'),
                       ('Content-Length', str(len(body))), ('Cookie', browser.cookie)]
            status, page, cookie = raw_post(browser, route, body, headers)
            assert status == 409 and cookie is None
            if route == '/unlock':
                assert 'Unlock form is incomplete or malformed.' in page
                assert 'Operator key did not match.' not in page
            assert lifecycle.snapshot('Q1')['human_intent'] == before
            with store._connection() as db:
                assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == count


def test_null_origin_still_requires_operator_key_session_and_form_csrf(fixture):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        status, page = browser.request('POST', '/unlock', {'operator_key': 'wrong'}, Origin='null')
        assert status == 409 and 'Unlock not confirmed' in page and not browser.cookie
        browser.login(options['operator_key'], Origin='null')
        browser.submit(browser.form(action='enroll'), Origin='null')
        browser.submit(browser.form(action='stop'), Origin='null')
        fields = browser.form(action='resume')
        before = lifecycle.snapshot('Q1')['human_intent']
        cookie, browser.cookie = browser.cookie, ''
        browser.submit(fields, expected=409, Origin='null')
        browser.cookie = cookie
        browser.submit({**fields, 'csrf': 'wrong'}, expected=409, Origin='null')
        browser.submit({**fields, 'input_id': 'human-unissued'}, expected=409, Origin='null')
        assert lifecycle.snapshot('Q1')['human_intent'] == before
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == 2


@pytest.mark.parametrize('origin', ['exact', 'null'])
@pytest.mark.parametrize('supplied', ['wrong', '', 'trailing-space'])
def test_unlock_key_mismatch_has_fixed_nonsecret_message_and_no_side_effects(fixture, capsys, origin, supplied):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        before = lifecycle.snapshot('Q1')['human_intent']
        value = options['operator_key'].read_text().strip() + ' ' if supplied == 'trailing-space' else supplied
        status, page = browser.request('POST', '/unlock', {'operator_key': value},
                                       Origin=service.origin if origin == 'exact' else 'null')
        assert status == 409 and not browser.cookie and not service.controller.sessions
        assert 'Operator key did not match.' in page and 'No instruction was submitted.' in page
        assert 'Request checks rejected unlock.' not in page and 'Operator sessions are full.' not in page
        assert 'SIMULATED-operator-secret' not in page
        assert lifecycle.snapshot('Q1')['human_intent'] == before
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_unlock_session_limit_is_reported_only_after_matching_key_and_preserves_stop(fixture, capsys):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        browser.login(options['operator_key'], Origin='null')
        browser.submit(browser.form(action='enroll'))
        browser.submit(browser.form(action='stop'))
        before = lifecycle.snapshot('Q1')['human_intent']
        for _ in range(31):
            browser.login(options['operator_key'], Origin='null')
        assert len(service.controller.sessions) == 32
        cookie = browser.cookie
        assert 'Operator key did not match.' in browser.request('POST', '/unlock', {'operator_key': 'wrong'}, Origin='null')[1]
        status, page = browser.request('POST', '/unlock', {'operator_key': options['operator_key'].read_text().strip()}, Origin='null')
        assert status == 409 and 'Operator sessions are full.' in page
        assert 'No instruction was submitted.' in page and 'Operator key did not match.' not in page
        assert browser.cookie == cookie and len(service.controller.sessions) == 32
        assert 'SIMULATED-operator-secret' not in page
        assert all(token not in page and csrf not in page for token, csrf in service.controller.sessions.items())
        assert browser.form(action='resume')['csrf']  # Existing authenticated sessions still work.
        assert lifecycle.snapshot('Q1')['human_intent'] == before
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == 2
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_unlock_incomplete_body_reports_form_failure_without_logging(fixture, capsys):
    store, _, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        body = b'operator_key=SIMULATED-incomplete-body'
        headers = [('Host', service.origin.removeprefix('https://')), ('Origin', 'null'),
                   ('Sec-Fetch-Site', 'same-origin'), ('Content-Type', 'application/x-www-form-urlencoded'),
                   ('Content-Length', str(len(body) + 1))]
        status, page, cookie = raw_post(browser, '/unlock', body, headers)
        assert status == 409 and cookie is None and not service.controller.sessions
        assert 'Unlock form is incomplete or malformed.' in page and 'SIMULATED-incomplete-body' not in page
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize('failure', [OSError, RuntimeError])
def test_unexpected_unlock_failure_uses_safe_fallback_without_echoing_exception(fixture, monkeypatch, capsys, failure):
    store, _, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        def unavailable(_supplied):
            raise failure('SIMULATED-sensitive-exception-content')
        monkeypatch.setattr(service.controller, 'login', unavailable)
        status, page = browser.request('POST', '/unlock', {'operator_key': 'SIMULATED-supplied-value'}, Origin='null')
        assert status == 409 and not browser.cookie and not service.controller.sessions
        assert 'Unlock could not complete.' in page and 'No instruction was submitted.' in page
        assert 'Operator key did not match.' not in page and 'SIMULATED' not in page
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize('broken_fallback', [False, True])
def test_unlock_response_failure_never_logs_exception_or_creates_instruction(fixture, monkeypatch, capsys, broken_fallback):
    from http.client import RemoteDisconnected
    store, _, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        handler = service.server.RequestHandlerClass
        response = handler.response
        def broken(self, status, *args, **kwargs):
            if status == 200 or broken_fallback:
                raise RuntimeError('SIMULATED-sensitive-response-error')
            return response(self, status, *args, **kwargs)
        monkeypatch.setattr(handler, 'response', broken)
        if broken_fallback:
            with pytest.raises(RemoteDisconnected):
                browser.login(options['operator_key'])
        else:
            status, page = browser.request('POST', '/unlock', {'operator_key': options['operator_key'].read_text().strip()})
            assert status == 409 and 'Unlock could not complete.' in page and 'SIMULATED' not in page
        assert not browser.cookie and len(service.controller.sessions) == 1  # Access delivery remains unconfirmed.
        with store._connection() as db:
            assert db.execute('SELECT count(*) FROM human_operator_input').fetchone()[0] == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_authentication_csrf_scope_and_duplicate_fields(fixture):
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        assert 'Operator access required' in browser.request()[1]
        assert browser.request('POST', '/unlock', {'operator_key': 'wrong'})[0] == 409
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'unenrolled'
        browser.login(options['operator_key'])
        form = browser.form(action='enroll')
        assert browser.request('POST', '/instruction', {**form, 'csrf': 'wrong'})[0] == 409
        assert browser.request('POST', '/instruction', {**form, 'work_id': 'OTHER'})[0] == 409
        assert browser.request('POST', '/instruction', list(form.items()) + [('action', 'stop')])[0] == 409
        browser.submit(form)
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'ambiguous'
        assert 'SIMULATED-operator-secret' not in browser.request()[1]
    with IntentService(store, **options) as restarted:
        browser.service = restarted
        assert 'Operator access required' in browser.request()[1]
        assert browser.request('POST', '/instruction', form)[0] == 409


@pytest.mark.parametrize('damage', ['public', 'symlink', 'worker_root'])
def test_operator_credentials_must_be_private_and_outside_worker_roots(fixture, tmp_path, damage):
    store, _, options = fixture
    operator = options['operator_key']
    if damage == 'public':
        operator.chmod(0o644)
    elif damage == 'symlink':
        alias = store.directory / 'alias'
        alias.symlink_to(operator)
        options['operator_key'] = alias
    else:
        root = store.worker_writable_roots[0]
        root.mkdir(parents=True)
        operator = root / 'key'
        operator.write_text('SIMULATED-secret-with-at-least-32-characters')
        operator.chmod(0o600)
        options['operator_key'] = operator
    with pytest.raises(Rejected, match='operator_file'):
        IntentService(store, **options)


def test_cli_selects_maintained_consumer_without_exposing_mcp_ingestion(fixture, monkeypatch):
    from agentic_tools.aos import cli, intent_operator
    from agentic_tools.aos.coordinator_mcp import create_coordinator_server
    store, _, options = fixture
    observed = []
    monkeypatch.setattr(intent_operator, 'serve', lambda store, **kw: observed.append(kw))
    args = ['intent-serve', '--state-dir', str(store.directory), '--worker-writable-root', str(store.worker_writable_roots[0])]
    for name, value in options.items():
        args += ['--' + name.replace('_', '-'), str(value)]
    assert cli.main(args) == 0
    assert observed[0]['work_id'] == 'Q1'
    server = create_coordinator_server(store, decision_origin='https://127.0.0.1:8443')
    import asyncio
    tools = asyncio.run(server.list_tools())
    assert not any(t.name.startswith('intent') for t in tools)


def test_interrupted_saved_input_recovers_truthful_receipt_without_repeating_stop(fixture, monkeypatch):
    from http.client import RemoteDisconnected
    store, lifecycle, options = fixture
    with IntentService(store, **options) as service:
        browser = Browser(service)
        browser.login(options['operator_key'])
        browser.submit(browser.form(action='enroll'))
        original_control = service.controller.lifecycle.control
        def crash(*args, **kwargs):
            raise RuntimeError('SIMULATED power loss after instruction persistence')
        monkeypatch.setattr(service.controller.lifecycle, 'control', crash)
        interrupted = browser.form(action='stop')
        with pytest.raises(RemoteDisconnected):
            browser.request('POST', '/instruction', interrupted)
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == interrupted['input_id']
        monkeypatch.setattr(service.controller.lifecycle, 'control', original_control)
    with IntentService(store, **options) as restarted:
        browser = Browser(restarted)
        browser.login(options['operator_key'])
        recovered = browser.submit(browser.form(action='replay', reference=interrupted['input_id']))
        assert 'interrupted' in recovered and 'no new lifecycle action' in recovered
        browser.submit(browser.form(action='resume'))
        resumed = lifecycle.snapshot('Q1')['human_intent']['controlling_instruction']
        browser.submit(browser.form(action='replay', reference=interrupted['input_id']))
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed


def test_actual_cli_process_serves_operator_controls_and_closes_listener(fixture):
    import select
    import signal
    from types import SimpleNamespace
    store, lifecycle, options = fixture
    capability = store.host_capability('SIMULATED CLI setup', 'SIMULATED original stopped fixture')
    human_intent.enable(lifecycle, 'Q1', 'fixture-owner', capability=capability)
    human_intent.record(lifecycle, {'instruction_id': 'synthetic-cli-stop', 'human_reference': 'SIMULATED:cli-stop',
        'source_kind': 'direct_human', 'scope': ['Q1'], 'action': 'stop', 'supersedes': [],
        'provenance_available': True, 'context': 'original'}, capability=capability)
    lifecycle.control('synthetic-cli-pause', 'Q1', 'pause', instruction_id='synthetic-cli-stop')
    argv = [sys.executable, '-m', 'agentic_tools.aos.cli', 'intent-serve', '--state-dir', str(store.directory),
            '--worker-writable-root', str(store.worker_writable_roots[0])]
    for name, value in options.items():
        argv += ['--' + name.replace('_', '-'), str(value)]
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, 'Owned operator host did not become ready'
        url = child.stdout.readline().strip()
        assert url == f"https://127.0.0.1:{options['port']}/"
        browser = Browser(SimpleNamespace(port=options['port'], origin=url.rstrip('/')))
        browser.login(options['operator_key'])
        assert browser.request('GET', '/?resume=no')[0] == 200
        assert lifecycle.snapshot('Q1')['human_intent']['disposition'] == 'stop'
        resumed = browser.form(action='resume')
        browser.submit(resumed)
        browser.submit(resumed)  # Same original input identity; no duplicate instruction.
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed['input_id']
    finally:
        child.send_signal(signal.SIGINT)
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            pytest.fail('Owned CLI listener did not stop on interrupt')
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1', options['port'])) != 0
    assert len(store.read('Q1')['decisions']) == 1  # pre-existing synthetic fixture approval only
