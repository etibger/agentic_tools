"""Startup cancellation races use scripted RPC and bounded event handshakes."""
import copy
import threading

import pytest

from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import claim, prepared
from test_aos_startup_recovery import rejected


CLOCK_FIELDS = (
    'created_at', 'startup_monotonic', 'startup_deadline',
    'startup_deadline_monotonic', 'startup_cleanup_reserve', 'budget_id', 'budget_seconds',
)


def clocks(record):
    return {key: record[key] for key in CLOCK_FIELDS}


def assert_reserved(p):
    receipt = p.runtime.budget_receipt()
    assert receipt['attempts_reserved'] == 1
    assert receipt['seconds_reserved'] == 60


def before_thread_failure(*_args, **_kwargs):
    raise Rejected('runtime_sandbox', 'scripted permission probe failure')


@pytest.fixture
def held_startup(rejected, monkeypatch):
    """Retain a real scripted startup failure before its terminal cleanup."""
    p = rejected
    p.assignment = claim(p)
    with monkeypatch.context() as patch:
        patch.setattr(p.runtime, '_probe_sandbox', before_thread_failure)
        patch.setattr(p.runtime, '_startup_failure',
                      lambda record: p.runtime._observation(record, 'unknown'))
        assert p.runtime.start(p.assignment)['state'] == 'unknown'
    record = p.runtime._get(p.assignment['attempt_id'])
    assert record['phase'] == 'intent'
    assert record['startup_outcome']['kind'] == 'before_thread_request'
    assert record['startup_failure_settled'] is True
    assert len(record['server_history']) == 1
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
    return p


def acknowledged(p, record):
    """Advance a retained fixture to an acknowledged, unsent thread frontier."""
    p.runtime._startup_patch(record, {'phase': 'thread_request'})
    p.runtime._startup_patch(record, {
        'thread_id': 'fixture-thread',
        'startup_outcome': {
            'kind': 'acknowledged_no_turn_failure', 'attempt_id': record['attempt_id'],
            'method': 'thread/start', 'thread_id': 'fixture-thread', 'turn_started': False,
        },
    })


def test_cancel_captured_before_failure_preserves_proof_and_reuses_clean_stop(rejected, monkeypatch):
    p = rejected
    assignment = claim(p)
    aid = assignment['attempt_id']
    probe_entered, release_probe = threading.Event(), threading.Event()
    cancel_captured, failure_saved, cancel_done = (threading.Event() for _ in range(3))
    observations, errors, snapshots = {}, [], {}
    original_stop, original_failure = p.runtime._startup_stop, p.runtime._startup_failure

    def probe(record, _recipe, **_kwargs):
        snapshots['probe'] = copy.deepcopy(record)
        probe_entered.set()
        assert release_probe.wait(5), 'test did not release the scripted probe'
        before_thread_failure()

    def stop(record, recipe):
        if threading.current_thread().name == 'startup-canceller':
            snapshots['cancel'] = copy.deepcopy(record)
            cancel_captured.set()
            assert failure_saved.wait(5), 'startup failure was not saved'
        return original_stop(record, recipe)

    def failure(record):
        snapshots['failure'] = copy.deepcopy(record)
        failure_saved.set()
        assert cancel_done.wait(5), 'cancellation did not finish'
        return original_failure(record)

    def run_start():
        try:
            observations['start'] = p.runtime.start(assignment)
        except BaseException as error:
            errors.append(error)

    def run_cancel():
        try:
            observations['cancel'] = p.runtime.cancel(aid)
        except BaseException as error:
            errors.append(error)
        finally:
            cancel_done.set()

    monkeypatch.setattr(p.runtime, '_probe_sandbox', probe)
    monkeypatch.setattr(p.runtime, '_startup_stop', stop)
    monkeypatch.setattr(p.runtime, '_startup_failure', failure)
    starter = threading.Thread(target=run_start, name='startup-starter', daemon=True)
    canceller = threading.Thread(target=run_cancel, name='startup-canceller', daemon=True)
    starter.start()
    try:
        assert probe_entered.wait(5), 'startup did not reach the scripted probe'
        canceller.start()
        assert cancel_captured.wait(5), 'cancel did not capture the old intent'
        assert snapshots['cancel']['phase'] == 'intent'
        assert 'startup_outcome' not in snapshots['cancel']
        release_probe.set()
        starter.join(10)
        canceller.join(10)
    finally:
        release_probe.set()
        failure_saved.set()
        cancel_done.set()
        starter.join(5)
        if canceller.ident is not None:
            canceller.join(5)
    assert not starter.is_alive() and not canceller.is_alive()
    assert not errors
    assert observations['start']['state'] == 'startup_failed'
    assert observations['cancel']['effects_reconciled'] is True
    saved = p.runtime._get(aid)
    assert saved['phase'] == 'startup_terminal'
    assert saved['startup_outcome'] == snapshots['failure']['startup_outcome']
    assert saved['startup_outcome']['kind'] == 'before_thread_request'
    assert saved['error'] == snapshots['failure']['error']
    assert saved['error_history'] == snapshots['failure']['error_history']
    assert saved['startup_failure_settled'] and saved['native_cancel_requested']
    assert saved['startup_owner_exit']['started'] == 'saved-birth'
    assert saved['startup_stop_receipt'] == observations['cancel']
    assert observations['start']['result']['observed_at'] == observations['cancel']['observed_at']
    assert clocks(saved) == clocks(snapshots['probe'])
    assert saved['model_calls_attempted'] == 0
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
    assert_reserved(p)
    assert p.runtime.lookup(aid) == observations['start']
    replacement = {**saved['startup_stop_receipt'], 'observed_at': 'replacement'}
    with pytest.raises(Rejected, match='Positive startup cleanup'):
        p.runtime._startup_patch(saved, {'startup_stop_receipt': replacement})
    assert p.runtime._get(aid)['startup_stop_receipt'] == observations['cancel']


@pytest.mark.parametrize('phase', ['thread_request', 'turn_request', 'running'])
def test_stale_cleanup_cannot_roll_back_an_advanced_frontier(held_startup, phase):
    p = held_startup
    current = p.runtime._get(p.assignment['attempt_id'])
    stale = copy.deepcopy(current)
    acknowledged(p, current)
    if phase != 'thread_request':
        p.runtime._startup_patch(current, {'phase': 'turn_request', 'model_calls_attempted': 1})
    if phase == 'running':
        p.runtime._startup_patch(current, {'phase': 'running', 'turn_id': 'fixture-turn'})
    saved = p.runtime._get(current['attempt_id'])
    with pytest.raises(Rejected, match='advanced during reconciliation'):
        p.runtime._startup_patch(stale, {'startup_stop_receipt': {
            'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
        }})
    assert p.runtime._get(current['attempt_id']) == saved
    assert saved['phase'] == phase and saved['thread_id'] == 'fixture-thread'
    assert clocks(saved) == clocks(stale)
    assert_reserved(p)


def test_turn_intent_removes_proof_and_stale_cancel_cannot_restore_it(held_startup):
    p = held_startup
    current = p.runtime._get(p.assignment['attempt_id'])
    acknowledged(p, current)
    stale = copy.deepcopy(current)
    proof = copy.deepcopy(current['startup_outcome'])
    original_clocks = clocks(current)
    p.runtime._startup_patch(current, {'phase': 'turn_request', 'model_calls_attempted': 1})
    committed = p.runtime._get(current['attempt_id'])
    assert committed['phase'] == 'turn_request' and committed['model_calls_attempted'] == 1
    assert 'startup_outcome' not in committed
    p.runtime._startup_patch(stale, {'native_cancel_requested': True}, match_frontier=False)
    assert stale == p.runtime._get(current['attempt_id'])
    assert 'startup_outcome' not in stale and stale['native_cancel_requested'] is True
    with pytest.raises(Rejected, match='No-turn proof contradicts'):
        p.runtime._startup_patch(stale, {'startup_outcome': proof})
    saved = p.runtime._get(current['attempt_id'])
    assert saved['phase'] == 'turn_request' and saved['model_calls_attempted'] == 1
    assert 'startup_outcome' not in saved
    assert clocks(saved) == original_clocks
    assert_reserved(p)


@pytest.mark.parametrize('next_request', ['thread', 'turn'])
def test_cancellation_wins_before_next_request_is_committed(held_startup, next_request):
    p = held_startup
    main = p.runtime._get(p.assignment['attempt_id'])
    if next_request == 'turn':
        acknowledged(p, main)
    cancel = copy.deepcopy(main)
    p.runtime._startup_patch(cancel, {'native_cancel_requested': True}, match_frontier=False)
    saved = p.runtime._get(main['attempt_id'])
    updates = {'phase': 'thread_request'} if next_request == 'thread' else {
        'phase': 'turn_request', 'model_calls_attempted': 1,
    }
    with pytest.raises(Rejected, match='stopped before another request'):
        p.runtime._startup_patch(main, updates)
    assert p.runtime._get(main['attempt_id']) == saved
    assert saved['model_calls_attempted'] == 0
    assert p.backend.count('turn/start') == 0
    assert_reserved(p)


def test_owner_exit_observations_and_errors_survive_frontier_advancement(held_startup):
    p = held_startup
    main = p.runtime._get(p.assignment['attempt_id'])
    cleanup = copy.deepcopy(main)
    owner = main['server_history'][0]
    original_clocks = clocks(main)
    acknowledged(p, main)
    p.backend.clients[0].close()
    exit_fact = {'pid': owner['pid'], 'started': owner['started'], 'observed_at': 'fixture-exit'}
    p.runtime._startup_patch(cleanup, {
        'startup_owner_exit': exit_fact,
        'startup_observed_processes': {str(owner['pid']): owner['started']},
    }, match_frontier=False)
    event = {'operation': 'cancel', 'detail': 'retained cleanup observation', 'at': 'fixture-time'}
    p.runtime._startup_patch(main, {'error_history': [event]}, match_frontier=False)
    saved = p.runtime._get(main['attempt_id'])
    assert saved['phase'] == 'thread_request' and saved['thread_id'] == 'fixture-thread'
    assert saved['startup_owner_exit'] == exit_fact
    assert saved['startup_observed_processes'] == {str(owner['pid']): owner['started']}
    assert event in saved['error_history']
    assert any(item['operation'] == 'startup' for item in saved['error_history'])
    assert saved['error'] == cleanup['error']
    assert clocks(saved) == original_clocks
    assert_reserved(p)


def test_unsettled_acknowledgment_is_not_clean_until_main_failure_settles(held_startup):
    p = held_startup
    record = p.runtime._get(p.assignment['attempt_id'])
    # Fixture construction only: represent the interval after ACK and before
    # the main startup call has settled its post-ACK permission readback.
    record.pop('startup_failure_settled')
    p.runtime._save(record)
    acknowledged(p, record)
    stop = p.runtime._startup_stop(record, p.recipe)
    assert stop['stopped'] is True and stop['owned_processes'] == []
    assert stop['effects_reconciled'] is False
    saved = p.runtime._get(record['attempt_id'])
    assert 'terminal_observation' not in saved
    assert saved['startup_owner_exit']['started'] == 'saved-birth'
    p.runtime._startup_patch(record, {'startup_failure_settled': True})
    result = p.runtime.lookup(record['attempt_id'])
    assert result['state'] == 'startup_failed'
    assert result['result']['effects_reconciled'] is True
    assert result['model_calls_attempted'] == 0 and result['turn_id'] is None
    assert p.backend.count('turn/start') == 0
    assert_reserved(p)


@pytest.mark.parametrize('field', ['startup_deadline', 'startup_deadline_monotonic'])
def test_stale_original_clock_cannot_be_replaced_by_cleanup(held_startup, field):
    p = held_startup
    saved = p.runtime._get(p.assignment['attempt_id'])
    stale = copy.deepcopy(saved)
    stale[field] = 'changed' if field == 'startup_deadline' else stale[field] + 60
    with pytest.raises(Rejected, match='original clocks changed'):
        p.runtime._startup_patch(stale, {'native_cancel_requested': True}, match_frontier=False)
    assert p.runtime._get(saved['attempt_id']) == saved
    assert_reserved(p)


def test_changed_same_frontier_proof_prevents_stale_cleanup_publication(held_startup):
    p = held_startup
    current = p.runtime._get(p.assignment['attempt_id'])
    stale = copy.deepcopy(current)
    original = copy.deepcopy(current['startup_outcome'])
    newer = {**original, 'observed_at': 'newer-factual-observation'}
    p.runtime._startup_patch(current, {'startup_outcome': newer})
    saved = p.runtime._get(current['attempt_id'])
    with pytest.raises(Rejected, match='evidence changed before publication'):
        p.runtime._startup_patch(stale, {'startup_stop_receipt': {
            'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
        }}, expected_evidence={'startup_outcome': original})
    assert p.runtime._get(current['attempt_id']) == saved
    assert saved['startup_outcome'] == newer
    assert_reserved(p)
