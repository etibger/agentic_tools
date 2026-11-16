"""Synthetic production-runtime observations; no vendor/model qualification."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

import pytest

from agentic_tools.aos import codex_runtime as module
from agentic_tools.aos.codex_runtime import CodexRuntime, RPCRejection
from agentic_tools.aos.store import Rejected
from agentic_tools.aos.worker import Worker
from test_aos_codex_runtime import prepared, claim, empty_metadata_error


def rejection(**overrides):
    return RPCRejection(**({'method': 'thread/read', 'request_id': 'read-1', 'code': -32603,
                            'message': 'list_turns is not supported yet'} | overrides))


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(delta=0, wall_offset=0, mono_offset=0, sleeps=[], hook=lambda: None)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=value.delta + value.wall_offset)
    def sleep(seconds):
        value.sleeps.append(seconds)
        value.delta += seconds
        value.hook()
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: time.monotonic() + value.delta + value.mono_offset,
                                                       time=lambda: time.time() + value.delta + value.wall_offset, sleep=sleep))
    return value


def started(p):
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    return assignment['attempt_id']


@pytest.mark.parametrize('failures', [1, 2])
def test_worker_consumes_same_turn_retry_without_successor_or_new_charge(prepared, clock, failures):
    p = prepared
    p.backend.read_results = [rejection(request_id=str(i)) for i in range(failures)]
    with Worker(p.lifecycle, p.runtime) as worker:
        assert worker.tick()['dispatched'] == 1
        assignment = p.lifecycle.assignments()[0]
        saved = p.runtime._get(assignment['attempt_id'])
        state = saved['first_status_read_retry']
        assert state['state'] == 'readable' and state['reads'] == failures + 1
        assert len(state['rejections']) == failures
        assert len([e for e in saved['error_history'] if e['operation'] == 'thread/read']) == failures
        assert len(clock.sleeps) == failures
        assert all(0 < actual <= limit for actual, limit in zip(clock.sleeps, [0.101, 0.401]))
        assert not p.lifecycle.snapshot('Q2')['technical_blockers']
        assert len(p.lifecycle.assignments()) == 1
        assert saved['model_calls_attempted'] == 1
        assert p.lifecycle.startup_retries_used('Q2') == 0
        assert p.backend.count('thread/start') == p.backend.count('turn/start') == 1
        assert p.backend.count('thread/resume') == p.backend.count('turn/interrupt') == 0
        assert saved['startup_deadline'] == p.runtime._get(assignment['attempt_id'])['startup_deadline']


def test_three_rejections_are_durable_and_cannot_be_refilled(prepared, clock):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [rejection(request_id=str(i)) for i in range(4)]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    saved = p.runtime._get(attempt)
    assert saved['first_status_read_retry']['reads'] == 3
    assert len(saved['first_status_read_retry']['rejections']) == 3
    assert p.backend.count('thread/read') == 3
    for _ in range(2):
        assert p.runtime.lookup(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 3
    assert p.runtime._get(attempt)['first_status_read_retry'] == saved['first_status_read_retry']


@pytest.mark.parametrize('error', [
    Rejected('runtime_rpc', 'thread/read failed: list_turns is not supported yet'),
    Rejected('runtime_timeout', 'no reply'), rejection(method='thread/resume'),
    rejection(message='list_turns is not supported yet!'), rejection(request_id=''),
    rejection(message='Permission denied'), rejection(code=True),
])
def test_only_exact_correlated_rejection_admits_retries(prepared, clock, error):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [error]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert not clock.sleeps and p.backend.count('thread/read') == 1
    assert not p.runtime._get(attempt).get('first_status_read_retry')


@pytest.mark.parametrize('error', [Rejected('runtime_timeout', 'no reply'), empty_metadata_error(), rejection(message='denied')])
def test_other_error_inside_retry_cannot_become_wait_or_new_read(prepared, clock, error):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [rejection(), error]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert not p.runtime._get(attempt).get('status_wait')
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 2


def test_success_then_error_never_retries(prepared, clock):
    p = prepared
    attempt = started(p)
    assert p.runtime.poll(attempt)['state'] == 'active'
    p.backend.read_results = [rejection()]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert not clock.sleeps


@pytest.mark.parametrize('wrong', ['thread', 'cwd', 'turn', 'extra', 'missing', 'status', 'malformed'])
def test_invalid_reply_is_held_without_publishing_or_another_retry(prepared, clock, wrong):
    p = prepared
    attempt = started(p)
    thread = copy.deepcopy(p.backend.threads['thread-0'])
    if wrong in ('thread', 'cwd'):
        thread['id' if wrong == 'thread' else 'cwd'] = 'wrong'
    elif wrong == 'turn':
        thread['turns'][0]['id'] = 'wrong'
    elif wrong == 'extra':
        thread['turns'].append(copy.deepcopy(thread['turns'][0]))
    elif wrong == 'missing':
        thread['turns'] = []
    elif wrong == 'status':
        thread['turns'][0]['status'] = 'new-status'
    p.backend.read_results = [rejection(), {} if wrong == 'malformed' else {'thread': thread}]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert not p.runtime._get(attempt).get('status_readable_at')
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 2


@pytest.mark.parametrize('when', ['backoff', 'reply'])
@pytest.mark.parametrize('fence', ['cancel', 'authority', 'source', 'resource', 'dependency', 'frontier', 'deadline', 'recipe'])
def test_guards_rechecked_before_retry_and_publication(prepared, clock, monkeypatch, when, fence):
    p = prepared
    attempt = started(p)
    def stop():
        if fence == 'cancel':
            current = p.runtime._get(attempt)
            p.runtime._startup_patch(current, {'native_cancel_requested': True}, match_frontier=False)
        elif fence == 'authority':
            with p.store._connection() as db:
                db.execute("UPDATE lifecycle SET control='cancelled' WHERE work_id='Q2'")
        elif fence == 'source':
            (p.source / 'test_example.py').write_text('changed protected test')
        elif fence == 'frontier':
            with p.runtime._db() as db:
                db.execute("UPDATE attempt SET record=json_set(record,'$.phase','terminal') WHERE id=?", (attempt,))
        elif fence == 'deadline':
            clock.mono_offset = 6
        elif fence == 'recipe':
            p.recipe['implementation_prompt'] += ' changed'
        else:
            def reject(*args, **kwargs):
                raise Rejected('resource_limit_exceeded' if fence == 'resource' else 'dependency_evidence', 'injected sticky guard failure')
            monkeypatch.setattr(p.runtime, '_resource_sample' if fence == 'resource' else '_verify_assessment_review', reject)
    p.backend.read_results = [rejection()]
    if when == 'backoff':
        clock.hook = stop
    else:
        client = p.backend.clients[0]
        request = client.request
        def read(method, params=None):
            result = request(method, params)
            if method == 'thread/read':
                stop()
            return result
        monkeypatch.setattr(client, 'request', read)
    result = p.runtime.poll(attempt)
    assert result['state'] != 'active'
    assert p.runtime._get(attempt)['first_status_read_retry']['state'] != 'readable'
    # Resource failure may invoke existing cleanup; those reads are not retries.
    methods = [method for method, _ in p.backend.calls]
    before_cleanup = methods[:methods.index('thread/resume')] if 'thread/resume' in methods else methods
    assert before_cleanup.count('thread/read') == (1 if when == 'backoff' else 2)
    if fence == 'frontier':
        assert p.runtime._get(attempt)['phase'] == 'terminal'
    assert p.backend.count('turn/start') == 1


@pytest.mark.parametrize('clock_kind', ['wall', 'monotonic', 'original_wall', 'original_monotonic'])
def test_fixed_grace_and_original_cleanup_deadline_bound_retries(prepared, clock, clock_kind):
    p = prepared
    attempt = started(p)
    if clock_kind.startswith('original'):
        record = p.runtime._get(attempt)
        updates = ({'startup_deadline': (datetime.now(timezone.utc) + timedelta(seconds=record['startup_cleanup_reserve'] + .05)).isoformat()}
                   if clock_kind.endswith('wall') else {'startup_deadline_monotonic': time.monotonic() + record['startup_cleanup_reserve'] + .05})
        p.runtime._startup_patch(record, updates)
    else:
        clock.hook = lambda: setattr(clock, 'wall_offset' if clock_kind == 'wall' else 'mono_offset', 6)
    p.backend.read_results = [rejection()]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 1
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 1


def test_reconnect_preserves_pending_count_clocks_and_stale_saves(prepared, clock, monkeypatch):
    p = prepared
    attempt = started(p)
    stale = p.runtime._get(attempt)
    class Crash(BaseException):
        pass
    def crash():
        raise Crash()
    p.backend.read_results = [rejection()]
    clock.hook = crash
    with pytest.raises(Crash):
        p.runtime.poll(attempt)
    pending = p.runtime._get(attempt)['first_status_read_retry']
    assert pending['reads'] == 1 and pending['state'] == 'pending'
    p.runtime._save(stale)
    assert p.runtime._get(attempt)['first_status_read_retry'] == pending
    for client in p.backend.clients:
        client.close()
    runtime = CodexRuntime(p.lifecycle, {'Q2': p.recipe}, transport_factory=p.backend.factory)
    monkeypatch.setattr(runtime, '_probe_sandbox', lambda *args, **kwargs: {'SIMULATED': True})
    monkeypatch.setattr(runtime, '_watch', lambda *args: None)
    clock.hook = lambda: None
    try:
        assert runtime.poll(attempt)['state'] == 'active'
        saved = runtime._get(attempt)['first_status_read_retry']
        assert saved['reads'] == 2 and saved['deadline'] == pending['deadline']
        assert saved['deadline_monotonic'] == pending['deadline_monotonic']
        assert p.backend.count('thread/start') == p.backend.count('turn/start') == 1
    finally:
        runtime.close()


def test_cleanup_does_not_retry_and_retry_timeout_is_clamped(prepared, clock, monkeypatch):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [rejection()]
    client = p.backend.clients[0]
    with pytest.raises(RPCRejection):
        p.runtime._read_turn(p.runtime._get(attempt), p.recipe, client)
    assert not clock.sleeps
    # A new first-read history is not invented to hide this failed cleanup.
    assert not p.runtime._get(attempt).get('first_status_read_retry')


def test_extra_read_timeout_uses_remaining_grace_and_restores_client(prepared, clock, monkeypatch):
    p = prepared
    attempt = started(p)
    client = p.backend.clients[0]
    client.timeout = 30
    request = client.request
    timeouts = []
    def read(method, params=None):
        if method == 'thread/read':
            timeouts.append(client.timeout)
        return request(method, params)
    monkeypatch.setattr(client, 'request', read)
    p.backend.read_results = [rejection(), rejection()]
    assert p.runtime.poll(attempt)['state'] == 'active'
    assert timeouts[0] == 30 and 0 < timeouts[2] < timeouts[1] <= 5
    assert client.timeout == 30


def test_unknown_inflight_read_is_consumed_and_cannot_be_repeated(prepared, clock, monkeypatch):
    p = prepared
    attempt = started(p)
    client = p.backend.clients[0]
    request = client.request
    class Crash(BaseException):
        pass
    def read(method, params=None):
        if method == 'thread/read' and p.runtime._get(attempt).get('first_status_read_retry'):
            assert p.runtime._get(attempt)['first_status_read_retry']['reads'] == 2
            raise Crash()
        return request(method, params)
    monkeypatch.setattr(client, 'request', read)
    p.backend.read_results = [rejection()]
    with pytest.raises(Crash):
        p.runtime.poll(attempt)
    assert p.runtime._get(attempt)['first_status_read_retry']['state'] == 'reading'
    monkeypatch.setattr(client, 'request', request)
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 1


def test_actual_lost_turn_ack_cannot_enter_retry(prepared, clock):
    p = prepared
    p.backend.lost_turn_ack = True
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert not p.runtime._get(assignment['attempt_id']).get('turn_id')
    p.backend.read_results = [rejection()]
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'
    assert not clock.sleeps
    assert not p.runtime._get(assignment['attempt_id']).get('first_status_read_retry')
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 1


def test_real_worker_advances_once_only_after_verified_completion(prepared, clock):
    p = prepared
    p.backend.read_results = [rejection(), rejection()]
    with Worker(p.lifecycle, p.runtime) as worker:
        assert worker.tick()['dispatched'] == 1
        first = p.lifecycle.assignments()[0]
        before = p.runtime._get(first['attempt_id'])
        assert len(p.lifecycle.assignments()) == 1
        (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
        p.backend.finish('thread-0')
        assert worker.tick()['dispatched'] == 1
        after = p.runtime._get(first['attempt_id'])
        assert after['terminal_observation']['state'] == 'completed'
        assert after['first_status_read_retry'] == before['first_status_read_retry']
        assert after['startup_deadline'] == before['startup_deadline']
        assert len(p.lifecycle.assignments()) == 2
        assert worker.tick()['dispatched'] == 0
        assert p.backend.count('turn/start') == 2


def test_other_prior_failure_remains_held(prepared, clock):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [Rejected('runtime_timeout', 'uncertain first read')]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 1


def test_monotonic_reset_remains_held(prepared, clock):
    p = prepared
    attempt = started(p)
    p.backend.read_results = [rejection()]
    clock.hook = lambda: setattr(clock, 'mono_offset', -time.monotonic())
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert p.backend.count('thread/read') == 1


@pytest.mark.parametrize('stage', ['review', 'source_review', 'resource'])
@pytest.mark.parametrize('change', ['expiry', 'stop'])
def test_slow_gate_preparation_cannot_read_or_publish_after_expiry_or_stop(prepared, clock, monkeypatch, stage, change):
    p = prepared
    attempt = started(p)
    names = {'review': '_verify_assessment_review', 'source_review': '_verify_source_review', 'resource': '_resource_sample'}
    method = getattr(p.runtime, names[stage])
    deadlines = []
    def preparation(record, recipe, **kwargs):
        end = kwargs.get('deadline', kwargs.get('deadline_monotonic'))
        if end is not None:
            deadlines.append(end)
            assert end <= record['first_status_read_retry']['deadline_monotonic'] + .001
            assert end <= record['startup_deadline_monotonic'] - record['startup_cleanup_reserve'] + .001
            if change == 'expiry':
                clock.mono_offset = 6
            else:
                with p.store._connection() as db:
                    db.execute("UPDATE lifecycle SET control='cancelled' WHERE work_id='Q2'")
        return method(record, recipe, **kwargs)
    monkeypatch.setattr(p.runtime, names[stage], preparation)
    p.backend.read_results = [rejection()]
    assert p.runtime.poll(attempt)['state'] == 'unknown'
    assert deadlines and p.backend.count('thread/read') == 1
    assert p.runtime._get(attempt)['first_status_read_retry']['state'] != 'readable'
