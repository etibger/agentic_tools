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
def fixture(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
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
        status = response.status
        conn.close()
        return status, body

    def login(self, key, **headers):
        assert self.request('POST', '/unlock', {'operator_key': key.read_text().strip()}, **headers)[0] == 200

    def form(self, **changes):
        status, body = self.request()
        assert status == 200
        fields = {name: unescape(value) for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', body)}
        fields['supersedes'] = unescape(re.search(r'<textarea name="supersedes">(.*?)</textarea>', body, re.S).group(1))
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
            assert raw_post(browser, route, body, headers)[0] == 409
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
        browser.submit(browser.form(action='enroll'))
        stopped = browser.form(action='stop')
        browser.submit(stopped)
        resumed = browser.form(action='resume')
        browser.submit(resumed)
        browser.submit(browser.form(action='replay', reference=stopped['input_id']))
        assert lifecycle.snapshot('Q1')['human_intent']['controlling_instruction'] == resumed['input_id']
        browser.submit(browser.form(action='handoff'))
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
