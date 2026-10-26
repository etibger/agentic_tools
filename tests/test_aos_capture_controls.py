"""Delay, renewal and exact proposal selection use the ordinary HTTP surface."""
from http.client import HTTPConnection
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from urllib.parse import urlencode

import pytest

from agentic_tools.aos.approval_web import DecisionController
from agentic_tools.aos.store import Rejected
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
