"""Delay, renewal and exact proposal selection use the ordinary HTTP surface."""
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from urllib.parse import urlencode

import pytest

from agentic_tools.aos.approval_web import DecisionController
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from test_aos_approval_web import fixture, running_page, get_form, post
from test_aos_store import TEAM


def response(conn, method, path, data=None, headers=None):
    conn.request(method, path, data, headers or {})
    r = conn.getresponse()
    return r.status, r.read().decode(), r.getheader('Set-Cookie')


def test_first_click_after_two_hours_records_the_exact_unchanged_proposal(tmp_path):
    store, proposal = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        with store._transaction() as db:
            db.execute("UPDATE challenge SET created_at=?",
                       ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
        assert post(conn, cookie, challenge, csrf, 'approve', origin) == 200
        assert post(conn, cookie, challenge, csrf, 'approve', origin) == 200
        assert post(conn, cookie, challenge, csrf, 'decline', origin) == 409
        status, body, _ = response(conn, 'GET', '/?' + urlencode({'challenge': challenge}))
        assert status == 200 and 'approved' in body and 'held' in body and 'already answered' in body
        conn.close()
    assert store.read('WEB-TEST')['decisions'][0]['digest'] == proposal['digest']
    assert len(store.read('WEB-TEST')['decisions']) == 1


def test_reload_renews_credentials_for_same_proposal_without_creating_another_request(tmp_path):
    store, proposal = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, old_cookie, challenge, old_csrf = get_form(port)
        status, body, renewed_cookie = response(conn, 'GET', '/?' + urlencode({'challenge': challenge}))
        csrf = re.search(r'name="csrf" value="([^\"]+)"', body).group(1)
        assert status == 200 and proposal['digest'] in body
        assert renewed_cookie.split(';')[0] != old_cookie
        assert re.search(r'name="challenge" value="([^\"]+)"', body).group(1) == challenge
        assert not store.read('WEB-TEST')['decisions']
        assert post(conn, renewed_cookie.split(';')[0], challenge, csrf, 'approve', origin) == 200
        assert post(conn, old_cookie, challenge, old_csrf, 'decline', origin) == 409
        conn.close()
    with store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM challenge').fetchone()[0] == 1
    assert len(store.read('WEB-TEST')['decisions']) == 1


def test_multiple_unchanged_pending_proposals_require_exact_selection(tmp_path):
    store, first = fixture(tmp_path)
    store.introduce('intro2', 'OTHER', 'Other', ['fixture'], TEAM)
    second = store.present('present2', 'OTHER', 1, 'OTHER-DI', 1, 'Another proposal', ['No work'], TEAM, 'test only')
    cap = store.host_capability('test', 'SIMULATED:multiple')
    request = store.begin_approval('request2', 'OTHER', 2, second['digest'], capability=cap)
    with running_page(store) as (port, origin):
        conn = HTTPConnection('127.0.0.1', port)
        status, body, cookie = response(conn, 'GET', '/')
        assert status == 200 and 'Pending decisions' in body and cookie is None
        assert 'Exact test package' in body and 'Another proposal' in body
        status, body, cookie = response(conn, 'GET', '/?' + urlencode({'challenge': request['challenge_id']}))
        assert status == 200 and second['digest'] in body and first['digest'] not in body
        csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
        assert post(conn, cookie.split(';')[0], request['challenge_id'], csrf, 'approve', origin) == 200
        conn.close()
    assert not store.read('WEB-TEST')['decisions']
    assert store.read('OTHER')['concept_decision'] == 'approved'


@pytest.mark.parametrize('mutation', ['revision', 'digest', 'withdrawn', 'work_identity'])
def test_delayed_old_form_cannot_approve_changed_or_withdrawn_request(tmp_path, mutation):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        with store._transaction() as db:
            if mutation == 'revision':
                db.execute("UPDATE work SET revision=revision+1")
            elif mutation == 'digest':
                db.execute("UPDATE challenge SET digest='sha256:wrong'")
            elif mutation == 'withdrawn':
                db.execute("UPDATE challenge SET status='withdrawn'")
            else:
                # A work identity can't be substituted using the original credential.
                challenge = 'different-challenge'
        assert post(conn, cookie, challenge, csrf, 'approve', origin) in {200, 403, 409}
        conn.close()
    assert store.read('WEB-TEST')['concept_decision'] != 'approved'


def test_delay_does_not_allow_competing_or_reanswered_challenges(tmp_path):
    store, current = fixture(tmp_path)
    cap = store.host_capability('test', 'SIMULATED:terminal')
    with store._transaction() as db:
        db.execute("UPDATE challenge SET created_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(Rejected, match='already pending'):
        store.begin_approval('competing', 'WEB-TEST', 2, current['digest'], capability=cap)
    challenge = store.pending_approvals()[0]
    store.finish_approval(challenge['challenge_id'], 'decline', None, capability=cap)
    with pytest.raises(Rejected, match='terminal decision'):
        store.begin_approval('reanswer', 'WEB-TEST', 3, current['digest'], capability=cap)


def test_restart_discards_old_credentials_and_preserves_pending_exact_proposal(tmp_path):
    store, _ = fixture(tmp_path)
    first = DecisionController(store, origin='https://127.0.0.1:8443', qualification_reference='SIMULATED:first')
    challenge = first.current()['challenge_id']
    token = first.issue_session(challenge)
    second = DecisionController(store, origin=first.origin, qualification_reference='SIMULATED:restart')
    assert second.current()['challenge_id'] == challenge
    assert second.use_session(token, challenge, token, 'approve') == 'invalid'
    fresh = second.issue_session(challenge)
    assert second.use_session(fresh, challenge, fresh, 'approve') == 'new'
    assert not store.read('WEB-TEST')['decisions']


@pytest.mark.parametrize('stage', ['before_decision_commit', 'after_decision_commit', 'decision_readback'])
def test_same_http_answer_reconciles_recording_failure_without_second_approval(tmp_path, stage):
    store, proposal = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        def fail(actual):
            if actual == stage:
                raise sqlite3.OperationalError('SIMULATED recording or reply failure')
        store._fault = fail
        assert post(conn, cookie, challenge, csrf, 'approve', origin) == 503
        assert len(store.read('WEB-TEST')['decisions']) == (0 if stage == 'before_decision_commit' else 1)
        assert post(conn, cookie, challenge, csrf, 'decline', origin) == 409
        store._fault = lambda _: None
        assert post(conn, cookie, challenge, csrf, 'approve', origin) == 200
        conn.close()
    reader = Store(store.directory, store.worker_writable_roots, read_only=True)
    saved = reader.read('WEB-TEST')
    assert len(saved['decisions']) == 1 and saved['concept_decision'] == 'approved'
    assert saved['execution'] == 'held' and not saved['pending_decisions']
    assert reader.verify_decision(saved['decisions'][0]['decision_id'])['digest'] == proposal['digest']


def test_concurrent_opposite_answers_with_preissued_credentials_record_one_terminal_decision(tmp_path):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, origin):
        forms = [get_form(port), get_form(port)]
        def answer(index):
            conn, _, cookie, challenge, csrf = forms[index]
            try:
                return post(conn, cookie, challenge, csrf, ('approve', 'decline')[index], origin)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(answer, range(2))) == [200, 409]
    assert len(store.read('WEB-TEST')['decisions']) == 1


@pytest.mark.parametrize('mutation', ['package_bytes', 'concept_revision', 'work_identity'])
def test_issued_form_cannot_answer_a_different_saved_package(tmp_path, mutation):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        with store._transaction() as db:
            if mutation == 'concept_revision':
                db.execute('UPDATE work SET current_concept=2')
            else:
                package = json.loads(db.execute('SELECT package FROM concept').fetchone()[0])
                package['body' if mutation == 'package_bytes' else 'work_id'] = 'SIMULATED:different-approved-identity'
                db.execute('UPDATE concept SET package=?,digest=?', (canonical(package), digest(package)))
        assert post(conn, cookie, challenge, csrf, 'approve', origin) in {403, 409}
        conn.close()
    assert not store.read('WEB-TEST')['decisions']


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://foreign.example', 'Sec-Fetch-Site': 'same-origin'},
    {'Origin': 'null', 'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'same-origin'},
])
def test_valid_credential_never_replaces_required_origin_and_fetch_metadata(tmp_path, headers):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, _):
        conn, _, cookie, challenge, csrf = get_form(port)
        body = urlencode({'challenge': challenge, 'csrf': csrf, 'choice': 'approve'})
        conn.request('POST', '/decide', body, {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie, **headers})
        reply = conn.getresponse()
        assert reply.status == 403
        reply.read()
        conn.close()
    assert not store.read('WEB-TEST')['decisions']
