"""Owned service tests with simulated TLS and human answers; no product runtime.

The existing handler guards run over real disposable loopback sockets. The TLS
context is substituted only in these unit fixtures; host qualification must also
exercise the actual encrypted endpoint.
"""
from contextlib import closing
from http.client import HTTPConnection
import os
import socket
import ssl
import sqlite3
from threading import Event
import time
from urllib.parse import urlencode

import pytest

from agentic_tools.aos import approval_web
from agentic_tools.aos.approval_web import DecisionController, DecisionService
from agentic_tools.aos.store import Rejected, Store
from test_aos_approval_web import fixture, get_form, post, running_page
from test_aos_store import TEAM


class SimulatedTLS:
    def __init__(self, protocol):
        assert protocol == ssl.PROTOCOL_TLS_SERVER
        self.minimum_version = None
        self.loaded = None
        self.sockets = []

    def load_cert_chain(self, certificate, key, password=None):
        assert callable(password) and password() == ''
        self.loaded = (certificate, key)

    def wrap_socket(self, connection, *, server_side, do_handshake_on_connect):
        assert self.minimum_version == ssl.TLSVersion.TLSv1_2
        assert server_side is True and do_handshake_on_connect is False
        assert connection.gettimeout() == 2
        self.sockets.append(connection)
        return connection


def unused_port():
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def service_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_web.ssl, 'SSLContext', SimulatedTLS)
    key, certificate = tmp_path / 'fixture.key', tmp_path / 'fixture.crt'
    key.write_text('SIMULATED private key; not a credential')
    key.chmod(0o600)
    certificate.write_text('SIMULATED public certificate')
    return {'certificate': str(certificate), 'key': str(key), 'port': unused_port(),
            'qualification_reference': 'SIMULATED:owned-service-test'}


def read_page(service, route='/'):
    with closing(HTTPConnection('127.0.0.1', service.port, timeout=3)) as conn:
        conn.request('GET', route)
        response = conn.getresponse()
        return response.status, response.read().decode(), response.getheader('Set-Cookie')


def concept_without_challenge(tmp_path):
    store = Store(tmp_path / 'state', (tmp_path / 'worker',))
    store.introduce('intro', 'WEB-TEST', 'Synthetic web decision', ['fixture/'], TEAM)
    concept = store.present('concept', 'WEB-TEST', 1, 'WEB-DI', 1, 'Exact service proposal',
                            ['No product work'], TEAM, 'SIMULATED test only')
    return store, concept


def test_listener_precedes_challenge_and_saved_answer_survives_reopen(tmp_path, service_inputs):
    store, concept = concept_without_challenge(tmp_path)
    target = {'work_id': 'WEB-TEST', 'digest': concept['digest']}
    with DecisionService(store, **service_inputs, **target,
                         status=lambda: {'stage': 'Checking', 'detail': '<remaining checks>'}) as service:
        service.check()
        assert service.origin == f"https://127.0.0.1:{service_inputs['port']}"
        assert read_page(service)[0] == 200
        assert not store.pending_approvals() and not store.read('WEB-TEST')['decisions']
        capability = store.host_capability('preparer', 'SIMULATED:fixture-answer')
        store.begin_approval('challenge', 'WEB-TEST', 2, concept['digest'], capability=capability)
        conn, _, cookie, challenge, csrf = get_form(service.port)
        try:
            assert post(conn, cookie, challenge, csrf, 'approve', service.origin) == 200
        finally:
            conn.close()
        before = store.read('WEB-TEST')
        route = '/?' + urlencode({'challenge': challenge})
        code, page, cookie = read_page(service, route)
        assert code == 200 and 'Decision recorded' in page and '<strong>approved</strong>' in page
        assert 'Checking' in page and '&lt;remaining checks&gt;' in page
        assert '<form' not in page and cookie is None
        after = store.read('WEB-TEST')
        assert after['decisions'] == before['decisions']
        assert after['decision_readbacks'] == before['decision_readbacks']
    assert service.closed and service.cleanup == {'listener_stopped': True, 'requests_stopped': True}
    assert not service.thread.is_alive()
    with DecisionService(store, **service_inputs, **target) as reopened:
        code, page, cookie = read_page(reopened, route)
        assert code == 200 and 'saved and verified' in page and '<form' not in page and cookie is None
        assert store.read('WEB-TEST')['decisions'] == before['decisions']
        assert store.read('WEB-TEST')['decision_readbacks'] == before['decision_readbacks']


def test_scoped_page_never_offers_another_pending_or_answered_work(tmp_path):
    store, concept = fixture(tmp_path)
    own = store.pending_approvals()[0]['challenge_id']
    store.introduce('other-intro', 'WEB-OTHER', 'Other secret title', ['fixture/'], TEAM)
    other = store.present('other-concept', 'WEB-OTHER', 1, 'OTHER-DI', 1,
                          'Other secret body', ['Other acceptance'], TEAM, 'SIMULATED test only')
    cap = store.host_capability('preparer', 'SIMULATED:other')
    request = store.begin_approval('other-challenge', 'WEB-OTHER', 2, other['digest'], capability=cap)
    foreign = request['challenge_id']
    with running_page(store, work_id='WEB-TEST', digest=concept['digest']) as (port, _):
        conn, page, _, challenge, _ = get_form(port)
        assert challenge == own and 'Other secret' not in page and 'Pending decisions' not in page
        for answer in (False, True):
            if answer:
                store.finish_approval(foreign, 'accept', True, capability=cap)
            conn.request('GET', '/?' + urlencode({'challenge': foreign}))
            response = conn.getresponse()
            body = response.read().decode()
            assert response.status == 200 and '<form' not in body
            assert 'Other secret' not in body and 'WEB-OTHER' not in body and other['digest'] not in body
        conn.close()
    assert store.read('WEB-TEST')['decisions'] == []


@pytest.mark.parametrize('target', [
    {'work_id': 'WEB-TEST'}, {'digest': 'sha256:' + '1' * 64},
    {'work_id': 'WEB-TEST', 'digest': 'unknown'},
    {'work_id': 'WEB-TEST', 'digest': 'sha256:' + 'z' * 64},
])
def test_incomplete_scope_is_rejected_before_capability_or_listener(tmp_path, target):
    store, _ = fixture(tmp_path)
    count = len(store._capabilities)
    with pytest.raises(Rejected, match='decision_scope'):
        DecisionController(store, origin='https://127.0.0.1:8443', qualification_reference='SIMULATED', **target)
    assert len(store._capabilities) == count


def test_pending_service_restart_reuses_challenge_with_fresh_credentials(tmp_path, service_inputs):
    store, concept = fixture(tmp_path)
    target = {'work_id': 'WEB-TEST', 'digest': concept['digest']}
    with DecisionService(store, **service_inputs, **target) as first:
        conn, _, old_cookie, challenge, old_csrf = get_form(first.port)
        conn.close()
    with DecisionService(store, **service_inputs, **target) as second:
        with closing(HTTPConnection('127.0.0.1', second.port, timeout=3)) as conn:
            assert post(conn, old_cookie, challenge, old_csrf, 'approve', second.origin) == 403
        conn, _, cookie, reopened, csrf = get_form(second.port)
        conn.close()
        assert reopened == challenge and cookie != old_cookie and csrf != old_csrf
        assert len(store.pending_approvals()) == 1 and not store.read('WEB-TEST')['decisions']


def test_duplicate_listener_leaves_existing_listener_healthy(tmp_path, service_inputs):
    store, concept = fixture(tmp_path)
    target = {'work_id': 'WEB-TEST', 'digest': concept['digest']}
    with DecisionService(store, **service_inputs, **target) as first:
        duplicate = DecisionService(store, **service_inputs, **target)
        with pytest.raises(Rejected, match='decision_port_in_use'):
            duplicate.__enter__()
        assert duplicate.thread is None and duplicate.server is None
        first.check()
        assert 'Needs your decision' in read_page(first)[1]
        assert not store.read('WEB-TEST')['decisions']


def test_owned_incomplete_request_is_closed_and_unrelated_socket_survives(tmp_path, service_inputs):
    store, concept = fixture(tmp_path)
    sentinel = socket.socket()
    sentinel.bind(('127.0.0.1', 0))
    sentinel.listen()
    client = None
    service = DecisionService(store, **service_inputs, work_id='WEB-TEST', digest=concept['digest'])
    try:
        service.__enter__()
        client = socket.create_connection(('127.0.0.1', service.port), timeout=3)
        client.sendall(b'GET / HTTP/1.1\r\nHost: 127.0.0.1')
        until = time.monotonic() + 2
        while time.monotonic() < until:
            with service.server.owned_lock:
                if any(thread.is_alive() for thread in service.server.handlers):
                    break
            time.sleep(.01)
        with service.server.owned_lock:
            assert service.server.connections and any(t.is_alive() for t in service.server.handlers)
        started = time.monotonic()
        assert service.close() == {'listener_stopped': True, 'requests_stopped': True}
        assert time.monotonic() - started < 3
        assert not service.server.connections and not any(t.is_alive() for t in service.server.handlers)
        assert not service.thread.is_alive() and service.closed
        assert service.close() == service.cleanup
        assert sentinel.getsockname()[1] != 0 and os.fstat(sentinel.fileno())
    finally:
        if client:
            client.close()
        service.close()
        sentinel.close()


def test_cleanup_cannot_report_success_when_request_join_is_unknown(tmp_path, service_inputs, monkeypatch):
    store, concept = fixture(tmp_path)
    service = DecisionService(store, **service_inputs, work_id='WEB-TEST', digest=concept['digest'])
    service.__enter__()
    original = service.server.close_requests
    monkeypatch.setattr(service.server, 'close_requests', lambda _deadline: False)
    try:
        with pytest.raises(Rejected, match='decision_service_cleanup'):
            service.close()
        assert not service.closed
        assert service.cleanup == {'listener_stopped': True, 'requests_stopped': False}
        assert not service.thread.is_alive()
    finally:
        monkeypatch.setattr(service.server, 'close_requests', original)
        service.close()
    assert service.closed


def test_failed_listener_health_is_detected_without_new_decision(tmp_path, service_inputs, monkeypatch):
    store, concept = fixture(tmp_path)
    failed = Event()
    def fail(_server, **_kwargs):
        failed.set()
        raise OSError('SIMULATED listener failure')
    monkeypatch.setattr(approval_web._DecisionHTTPServer, 'handle_request', fail)
    service = DecisionService(store, **service_inputs, work_id='WEB-TEST', digest=concept['digest'])
    try:
        try:
            service.__enter__()
        except Rejected as error:
            assert error.code == 'decision_service_unavailable'
        assert failed.wait(1)
        service.thread.join(1)
        with pytest.raises(Rejected, match='decision_service_unavailable'):
            service.check()
        assert not store.read('WEB-TEST')['decisions']
    finally:
        service.close()


def test_unverified_saved_answer_is_not_displayed_as_approved(tmp_path, service_inputs):
    store, concept = fixture(tmp_path)
    cap = store.host_capability('fixture', 'SIMULATED:tamper-test')
    challenge = store.pending_approvals()[0]['challenge_id']
    store.finish_approval(challenge, 'accept', True, capability=cap)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE decision SET exact_answer='SIMULATED changed answer'")
    with DecisionService(store, **service_inputs, work_id='WEB-TEST', digest=concept['digest']) as service:
        code, page, cookie = read_page(service, '/?' + urlencode({'challenge': challenge}))
        assert code == 503 and '<strong>approved</strong>' not in page and '<form' not in page and cookie is None


def test_tls_configuration_failure_never_opens_listener(tmp_path, service_inputs, monkeypatch):
    store, _ = fixture(tmp_path)
    def fail(*_args, **_kwargs):
        raise ssl.SSLError('SIMULATED invalid certificate')
    monkeypatch.setattr(SimulatedTLS, 'load_cert_chain', fail)
    service = DecisionService(store, **service_inputs)
    with pytest.raises(Rejected, match='tls_unavailable'):
        service.__enter__()
    assert service.server is None and service.thread is None
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', service.port))


@pytest.mark.parametrize('outcome', ['unavailable', 'stale'])
def test_ineffective_recorded_interaction_does_not_claim_answer_is_settled(tmp_path, service_inputs, outcome):
    store, concept = fixture(tmp_path)
    challenge = store.pending_approvals()[0]['challenge_id']
    cap = store.host_capability('preparer', 'SIMULATED:ineffective-decision')
    if outcome == 'stale':
        with store._transaction() as db:
            db.execute("UPDATE work SET revision=revision+1 WHERE work_id='WEB-TEST'")
    result = store.finish_approval(challenge, 'unavailable' if outcome == 'unavailable' else 'accept',
                                  None if outcome == 'unavailable' else True, capability=cap)
    assert result['outcome'] == outcome
    with DecisionService(store, **service_inputs, work_id='WEB-TEST', digest=concept['digest']) as service:
        code, page, cookie = read_page(service, '/?' + urlencode({'challenge': challenge}))
        assert code == 200 and 'Decision not applied' in page
        assert 'did not supply an effective decision' in page and 'held for coordinator attention' in page
        assert 'No further answer' not in page and '<form' not in page and cookie is None
        assert store.read('WEB-TEST')['concept_decision'] == 'pending'
