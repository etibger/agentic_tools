"""Pre-model rejection is distinct from ambiguous creation and clean effects."""
import copy
import threading
from datetime import datetime, timedelta, timezone

import pytest

from agentic_tools.aos.codex_runtime import CodexRuntime, RPCRejection, StdioRPC
from agentic_tools.aos.store import Rejected
from agentic_tools.aos.worker import Worker
from test_aos_codex_runtime import prepared, claim, empty_metadata_error


@pytest.fixture
def rejected(prepared, monkeypatch):
    p = prepared
    def groups(**kwargs):
        return {c.proc.pid: {'parent': 1, 'group': c.proc.pid, 'started': 'saved-birth'}
                for c in p.backend.clients if not c.proc.exited}
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', groups)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes',
                        lambda **kwargs: {pid: (row['parent'], row['started']) for pid, row in groups().items()})
    factory = p.backend.factory
    def rejecting_factory(*args):
        client = factory(*args)
        request = client.request
        def reject(method, params=None):
            if method == 'thread/start':
                p.backend.calls.append((method, copy.deepcopy(params)))
                raise RPCRejection(method, 'correlated-request', -32600, 'Required instructions denied')
            return request(method, params)
        client.request = reject
        return client
    p.runtime.factory = rejecting_factory
    return p


def test_definite_rejection_is_failed_not_approved_and_reservation_survives_restart(rejected):
    p = rejected
    with Worker(p.lifecycle, p.runtime) as worker:
        worker.tick()
    assignment = p.lifecycle.assignments()[0]
    assert assignment['state'] == 'failed'
    assert assignment['runtime_model'] is None and assignment['runtime_effort'] is None
    assert p.lifecycle.snapshot('Q2')['execution'] == 'held'
    assert p.backend.count('thread/start') == 1 and p.backend.count('turn/start') == 0
    receipt = p.runtime.budget_receipt()
    assert receipt['attempts_reserved'] == 1 and receipt['seconds_reserved'] == 60
    p.runtime.close()
    reopened = CodexRuntime(p.lifecycle, {'Q2': p.recipe}, transport_factory=p.runtime.factory)
    try:
        for _ in range(2):
            observed = reopened.lookup(assignment['attempt_id'])
            assert observed['state'] == 'startup_failed'
            assert observed['result']['effects_reconciled'] is True
        p.lifecycle.control('cancel-rejected', 'Q2', 'cancel')
        with Worker(p.lifecycle, reopened) as worker:
            worker.tick()
        assert p.lifecycle.snapshot('Q2')['execution'] == 'cancelled'
        assert reopened.budget_receipt()['attempts_reserved'] == 1
        assert p.backend.count('thread/start') == 1
        with p.store._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM resource').fetchone()[0] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize('damage', ['source', 'output', 'owner_identity', 'missing_exit', 'child', 'observation', 'legacy'])
def test_rejection_does_not_waive_cleanup_or_ownership(rejected, monkeypatch, damage):
    p = rejected
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'startup_failed'
    record = p.runtime._get(assignment['attempt_id'])
    if damage == 'source':
        (p.source / 'example.py').write_text('unexpected change')
    elif damage == 'output':
        from pathlib import Path
        (Path(record['run_outputs']) / 'unexpected').write_text('unexpected effect')
    elif damage == 'owner_identity':
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups',
                            lambda **kwargs: {99999999: {'parent': 1, 'group': 99999999, 'started': 'reused-birth'}})
    elif damage == 'missing_exit':
        record.pop('startup_owner_exit')
    elif damage == 'child':
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups',
                            lambda **kwargs: {99999998: {'parent': 1, 'group': 99999999, 'started': 'child-birth'}})
    elif damage == 'observation':
        def fail(**kwargs):
            raise OSError('unavailable')
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', fail)
    else:
        record.pop('startup_version')
    p.runtime._save(record)
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled'] is False
    assert p.backend.count('turn/start') == 0


@pytest.mark.parametrize('failure', ['lost_ack', 'timeout', 'malformed'])
def test_non_authoritative_failures_never_become_rejected_or_relaunch(prepared, failure):
    p = prepared
    p.backend.lost_thread_ack = failure == 'lost_ack'
    if failure != 'lost_ack':
        factory = p.runtime.factory
        def broken_factory(*args):
            client = factory(*args)
            request = client.request
            def broken(method, params=None):
                if method == 'thread/start':
                    p.backend.calls.append((method, params))
                    raise Rejected('runtime_timeout' if failure == 'timeout' else 'runtime_rpc', failure)
                return request(method, params)
            client.request = broken
            return client
        p.runtime.factory = broken_factory
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled'] is False
    assert p.backend.count('thread/start') == 1 and p.backend.count('turn/start') == 0


def test_startup_deadline_is_original_and_does_not_dispatch_after_setup(rejected, monkeypatch):
    p = rejected
    assignment = claim(p)
    original = p.runtime._client
    def expire(record, recipe):
        client = original(record, recipe)
        record['startup_deadline'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        p.runtime._save(record)
        return client
    monkeypatch.setattr(p.runtime, '_client', expire)
    result = p.runtime.start(assignment)
    assert result['state'] == 'unknown'
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
    assert p.runtime.budget_receipt()['attempts_reserved'] == 1


def test_missing_or_invented_startup_proof_cannot_bypass_worker_settings_checks(prepared):
    p = prepared
    assignment = claim(p)
    with Worker(p.lifecycle, p.runtime) as worker:
        worker._consume(assignment, {'state': 'startup_failed', 'attempt_id': assignment['attempt_id'],
                                    'model_calls_attempted': 0, 'result': {'status': 'failed'}})
    assert p.lifecycle.assignments()[0]['state'] == 'blocked_unknown_effects'


@pytest.mark.parametrize('shape', ['rejection', 'contradiction', 'malformed', 'wrong_id', 'eof'])
def test_only_correlated_error_only_transport_reply_is_authoritative(shape):
    client = object.__new__(StdioRPC)
    client.lock, client.waiters, client.timeout = threading.Lock(), {}, .01
    def respond(request):
        response = {'id': request['id'], 'error': {'code': -32600, 'message': 'denied'}}
        if shape == 'contradiction':
            response['result'] = {'thread': {'id': 'possibly-created'}}
        elif shape == 'malformed':
            response['error'] = 'bad error'
        elif shape == 'wrong_id':
            response['id'] = 'unrelated'
        elif shape == 'eof':
            response = {'transport_closed': True}
        client.waiters[request['id']].put(response)
    client._send = respond
    with pytest.raises(Rejected) as result:
        client.request('thread/start')
    assert isinstance(result.value, RPCRejection) == (shape == 'rejection')


def test_structured_metadata_gap_preserves_acknowledged_waiting(prepared):
    p = prepared
    message = str(empty_metadata_error()).removeprefix('runtime_rpc: thread/read failed: ')
    p.backend.read_results = [RPCRejection('thread/read', 'matched-read', -32600, message)]
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    observed = p.runtime.poll(assignment['attempt_id'])
    assert observed['state'] == 'waiting'
    assert p.backend.count('turn/start') == 1


def test_cleanup_observations_are_clipped_to_original_deadline(rejected, monkeypatch):
    p = rejected
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'startup_failed'
    record = p.runtime._get(assignment['attempt_id'])
    record.pop('terminal_observation')  # Reconciliation has not yet been accepted.
    record['startup_deadline'] = (datetime.now(timezone.utc) + timedelta(seconds=.2)).isoformat()
    p.runtime._save(record)
    seen = []
    def slow_query(*, timeout):
        seen.append(timeout)
        import time
        time.sleep(.21)
        return {}
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', slow_query)
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert len(seen) == 1 and 0 < seen[0] <= .2


def test_config_rejection_retains_owner_until_positive_cleanup(rejected):
    p = rejected
    p.backend.bad_config = True
    result = p.runtime.start(claim(p))
    assert result['state'] == 'startup_failed'
    assert result['result']['startup_outcome']['kind'] == 'before_thread_request'
    assert result['result']['effects_reconciled']
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0


def test_expired_terminal_failure_remains_readable_without_restarting_cleanup(rejected):
    p = rejected
    assignment = claim(p)
    first = p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    record['startup_deadline'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    p.runtime._save(record)
    assert p.runtime.lookup(assignment['attempt_id']) == first
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled']
    (p.source / 'example.py').write_text('later contradictory effect')
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'


def test_post_spawn_observation_failure_retains_exact_handle_for_close(rejected, monkeypatch):
    p = rejected
    def observation(**kwargs):
        if p.backend.clients:
            raise OSError('identity observation unavailable')
        return {}
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes', observation)
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    client = p.backend.clients[0]
    assert p.runtime.clients[assignment['attempt_id']] is client
    assert not client.proc.exited
    p.runtime.close()
    assert client.proc.exited
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled'] is False
