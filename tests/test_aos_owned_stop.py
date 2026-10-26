"""Q1 stop/cancel precedence, receipt replay and recovery regressions."""
import json

import pytest

from agentic_tools.aos.lifecycle import Lifecycle, snapshot_in
from agentic_tools.aos.store import Rejected, Store
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import count_attempts, implementation, run_to_end, runtime, setup


def source(lifecycle):
    with lifecycle.store._connection() as db:
        return snapshot_in(db, 'Q1')


def active(tmp_path, **overrides):
    store, lifecycle, _ = setup(tmp_path, **overrides)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    return store, lifecycle, fake, lifecycle.assignments()[0]


def receipt(assignment, known=True):
    return {'attempt_id': assignment['attempt_id'], 'stopped': known,
            'owned_processes': [] if known else ['SIMULATED:owned-child'],
            'effects_reconciled': known,
            'partial_mutations': {'status': 'recorded', 'changes': ['fixture.py']} if known else 'unknown'}


def reopen(tmp_path, store):
    return Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))


@pytest.mark.parametrize('control', ['cancel_requested', 'reconciling', 'blocked_unknown_effects'])
@pytest.mark.parametrize('known', [False, True])
def test_request_and_result_preserve_each_cancellation_control_across_restart(tmp_path, control, known):
    store, lifecycle, fake, assignment = active(tmp_path)
    lifecycle.control('cancel', 'Q1', 'cancel')
    # Exercise each durable control boundary, including a recovered in-flight
    # cancellation and an earlier unresolved cancellation result.
    if control == 'blocked_unknown_effects':
        lifecycle.cancellation_result('Q1', [receipt(assignment, False)])
    elif control == 'reconciling':
        with store._transaction() as db:
            db.execute("UPDATE lifecycle SET control='reconciling' WHERE work_id='Q1'")
    lifecycle = reopen(tmp_path, store)
    assert lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    assert source(lifecycle)['control'] == control
    before_retry = source(lifecycle)
    assert lifecycle.request_owned_stop(assignment['assignment_id'], 'approved_envelope', 'Repeated exact stop')
    assert source(lifecycle) == before_retry
    observation = receipt(assignment, known)
    lifecycle.owned_stop_result(assignment['assignment_id'], observation)
    after = source(lifecycle)
    assert after['control'] == control and after['execution'] == 'held' and after['resources']
    assert after['owned_stops'][0]['observation'] == observation
    assert lifecycle.owned_stop_result(assignment['assignment_id'], observation) == {'disposition': 'duplicate'}
    assert source(lifecycle) == after
    lifecycle = reopen(tmp_path, store)
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert source(lifecycle)['execution'] == 'cancelled' and source(lifecycle)['resources'] == []
    assert count_attempts(fake) == 1


@pytest.mark.parametrize('known', [False, True])
def test_cancel_arriving_inside_stop_callback_survives_restart(tmp_path, known):
    store, lifecycle, fake, assignment = active(tmp_path, max_seconds=1)
    with store._transaction() as db:
        db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00'")
    cancel = fake.cancel
    def racing_cancel(attempt_id):
        assert attempt_id == assignment['attempt_id']
        lifecycle.control('racing-user-cancel', 'Q1', 'cancel')
        return receipt(assignment, known)
    fake.cancel = racing_cancel
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    pending = source(lifecycle)
    assert pending['control'] == 'cancel_requested' and pending['resources']
    assert pending['owned_stops'][0]['observation'] == receipt(assignment, known)
    lifecycle = reopen(tmp_path, store)
    fake.cancel = cancel
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    after = source(lifecycle)
    assert after['execution'] == 'cancelled' and after['control'] == 'cancelled' and after['resources'] == []
    with store._connection() as db:
        evidence = [json.loads(r[0]) for r in db.execute("SELECT detail FROM lifecycle_event WHERE kind='owned_stop_reconciled'")]
    assert receipt(assignment, known) in evidence


def test_replayed_unknown_receipt_cannot_replace_later_proved_stop(tmp_path):
    store, lifecycle, _, assignment = active(tmp_path)
    lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    unknown, proved = receipt(assignment, False), receipt(assignment)
    lifecycle.owned_stop_result(assignment['assignment_id'], unknown)
    lifecycle.owned_stop_result(assignment['assignment_id'], proved)
    before = source(lifecycle)
    lifecycle = reopen(tmp_path, store)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], unknown) == {'disposition': 'duplicate'}
    assert source(lifecycle) == before
    assert before['owned_stops'][0]['observation'] == proved


def test_conflicting_stop_receipt_retains_both_records_and_replays_without_writes(tmp_path):
    store, lifecycle, _, assignment = active(tmp_path)
    lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    proved, conflict = receipt(assignment), receipt(assignment, False)
    lifecycle.owned_stop_result(assignment['assignment_id'], proved)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], conflict) == {'disposition': 'conflict'}
    before = source(lifecycle)
    assert before['owned_stops'][0]['observation'] == proved and before['resources']
    assert any(b['code'] == 'stop_receipt_conflict' for b in before['technical_blockers'])
    with store._connection() as db:
        assert json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='owned_stop_conflict'").fetchone()[0]) == {
            'retained': proved, 'received': conflict}
    lifecycle = reopen(tmp_path, store)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], conflict) == {'disposition': 'duplicate'}
    assert source(lifecycle) == before


@pytest.mark.parametrize('terminal', ['cancelled', 'delivered'])
def test_terminal_execution_rejects_stop_request_and_keeps_late_receipt_historical(tmp_path, terminal):
    if terminal == 'cancelled':
        store, lifecycle, fake, assignment = active(tmp_path)
        lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
        proved = receipt(assignment)
        lifecycle.owned_stop_result(assignment['assignment_id'], proved)
        lifecycle.control('cancel', 'Q1', 'cancel')
        with Worker(lifecycle, fake) as worker:
            worker.tick()
    else:
        store, lifecycle, _ = setup(tmp_path)
        fake = runtime(tmp_path)
        run_to_end(lifecycle, fake)
        assignment = lifecycle.assignments()[0]
        # A late callback with a retained preterminal stop intent must obey the
        # same terminal fence as ordinary callbacks. Delivery itself is native.
        with store._transaction() as db:
            db.execute("INSERT INTO owned_stop VALUES(?,?,'assignment_deadline','requested','2000-01-01T00:00:00+00:00',NULL,NULL)",
                       (assignment['attempt_id'], 'Q1'))
        proved = receipt(assignment)
    lifecycle = reopen(tmp_path, store)
    before = source(lifecycle)
    assert not lifecycle.request_owned_stop(assignment['assignment_id'], 'approved_envelope', 'Stale request')
    assert source(lifecycle) == before
    late = {**proved, 'partial_mutations': 'late supplemental mutation evidence'}
    assert lifecycle.owned_stop_result(assignment['assignment_id'], late) == {'disposition': 'historical'}
    after = source(lifecycle)
    assert after['execution'] == terminal and after['control'] == before['control'] and after['resources'] == []
    assert after['owned_stops'] == before['owned_stops'] and after['assignments'] == before['assignments']
    assert lifecycle.owned_stop_result(assignment['assignment_id'], late) == {'disposition': 'duplicate'}
    assert source(lifecycle) == after
    lifecycle.unknown(assignment['assignment_id'], 'Late error callback')
    assert source(lifecycle) == after
    calls = []
    fake.cancel = lambda attempt: calls.append(attempt)
    with Worker(lifecycle, fake) as worker:
        worker._stop_owned(assignment, 'assignment_deadline', 'Stale saved worker record')
        worker.tick()
    assert calls == []
    assert source(lifecycle)['execution'] == terminal


@pytest.mark.parametrize('paused', [False, True])
def test_stop_replay_and_late_terminal_cannot_create_successor_or_resume_hold(tmp_path, paused):
    store, lifecycle, fake, assignment = active(tmp_path)
    if paused:
        lifecycle.control('pause', 'Q1', 'pause')
    lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    observation = fake.cancel(assignment['attempt_id'])
    lifecycle.owned_stop_result(assignment['assignment_id'], observation)
    lifecycle = reopen(tmp_path, store)
    before = source(lifecycle)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], observation) == {'disposition': 'duplicate'}
    assert not lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Repeated stop')
    assert source(lifecycle) == before
    result = lifecycle.complete('late-terminal', assignment['assignment_id'], implementation())
    assert result == {'disposition': 'historical', 'duplicate': False}
    after = source(lifecycle)
    assert lifecycle.complete('late-terminal', assignment['assignment_id'], implementation()) == {'disposition': 'historical', 'duplicate': True}
    assert source(lifecycle) == after
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    after = source(lifecycle)
    assert after['execution'] == ('paused' if paused else 'held') and after['resources']
    assert len(after['assignments']) == count_attempts(fake) == 1
    assert after['owned_stops'] == before['owned_stops']


def conflicted(tmp_path):
    store, lifecycle, fake, assignment = active(tmp_path)
    lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    proved, contradiction = receipt(assignment), receipt(assignment, False)
    lifecycle.owned_stop_result(assignment['assignment_id'], proved)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], contradiction) == {'disposition': 'conflict'}
    return store, lifecycle, fake, assignment, proved, contradiction


def test_known_stop_without_conflict_cancels_after_restart_without_another_stop(tmp_path):
    store, lifecycle, fake, assignment = active(tmp_path)
    lifecycle.request_owned_stop(assignment['assignment_id'], 'assignment_deadline', 'Expired exact attempt')
    proved = fake.cancel(assignment['attempt_id'])
    lifecycle.owned_stop_result(assignment['assignment_id'], proved)
    lifecycle = reopen(tmp_path, store)
    lifecycle.control('cancel-known-stop', 'Q1', 'cancel')
    fake.cancel = lambda _: pytest.fail('Known no-conflict stop must remain idempotent')
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        worker.tick()
    after = source(lifecycle)
    assert after['execution'] == after['control'] == 'cancelled' and after['resources'] == []
    assert after['concept_decision'] == 'approved' and after['pending_human_decisions'] == []
    assert after['stop_conflicts'] == [] and after['owned_stops'][0]['observation'] == proved
    assert len(after['assignments']) == count_attempts(fake) == 1


def test_cancel_conflict_survives_empty_and_known_receipts_restart_and_replay(tmp_path):
    store, lifecycle, fake, assignment, proved, contradiction = conflicted(tmp_path)
    lifecycle = reopen(tmp_path, store)
    lifecycle.control('cancel-conflict', 'Q1', 'cancel')
    lifecycle.cancellation_result('Q1', [])
    before = source(lifecycle)
    assert before['execution'] == 'held' and before['resources'] and before['stop_conflicts']
    assert lifecycle.cancellation_assignments('Q1')[0]['attempt_id'] == assignment['attempt_id']
    calls, cancel = [], fake.cancel
    def exact_cancel(attempt):
        calls.append(attempt)
        return cancel(attempt)
    fake.cancel = exact_cancel
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    after = source(lifecycle)
    assert calls == [assignment['attempt_id']]
    assert after['execution'] == 'held' and after['control'] == 'blocked_unknown_effects' and after['resources']
    assert after['stop_conflicts'] == before['stop_conflicts']
    assert after['owned_stops'][0]['observation'] == proved
    with store._connection() as db:
        saved = json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='cancel_reconciled' ORDER BY event_id DESC").fetchone()[0])
    assert saved[0]['attempt_id'] == assignment['attempt_id'] and saved[0]['stopped'] is True
    lifecycle = reopen(tmp_path, store)
    before_replay = source(lifecycle)
    assert lifecycle.owned_stop_result(assignment['assignment_id'], contradiction) == {'disposition': 'duplicate'}
    assert source(lifecycle) == before_replay
    assert after['concept_decision'] == 'approved' and after['pending_human_decisions'] == []
    assert len(after['assignments']) == count_attempts(fake) == 1


@pytest.mark.parametrize('invalid', ['wrong_attempt', 'running_child', 'unknown_effects', 'missing_mutations', 'unknown_mutations', 'no_evidence', 'stale_conflict', 'duplicate_conflict'])
def test_conflict_resolution_requires_current_exact_owned_process_and_mutation_evidence(tmp_path, invalid):
    _, lifecycle, _, assignment, _, _ = conflicted(tmp_path)
    before = source(lifecycle)
    conflict_ids = [c['conflict_id'] for c in before['stop_conflicts']]
    observation, evidence = receipt(assignment), 'Q1 exact attempt process and mutation inspection'
    if invalid == 'wrong_attempt':
        observation['attempt_id'] = 'stale-attempt'
    elif invalid == 'running_child':
        observation['owned_processes'] = ['SIMULATED:still-running-child']
    elif invalid == 'unknown_effects':
        observation['effects_reconciled'] = False
    elif invalid == 'missing_mutations':
        observation.pop('partial_mutations')
    elif invalid == 'unknown_mutations':
        observation['partial_mutations'] = 'unknown'
    elif invalid == 'no_evidence':
        evidence = ' '
    elif invalid == 'stale_conflict':
        conflict_ids = ['sha256:stale-conflict']
    else:
        conflict_ids *= 2
    with pytest.raises(Rejected):
        lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], conflict_ids, observation, evidence)
    assert source(lifecycle) == before


@pytest.mark.parametrize('partial', [{'status': 'none'}, {'status': 'recorded', 'changes': ['fixture.py']}])
def test_explicit_conflict_reconciliation_is_durable_and_cannot_duplicate_successor(tmp_path, partial):
    store, lifecycle, fake, assignment, proved, contradiction = conflicted(tmp_path)
    lifecycle.control('cancel-with-conflict', 'Q1', 'cancel')
    lifecycle.cancellation_result('Q1', [])
    lifecycle = reopen(tmp_path, store)
    conflict_ids = [c['conflict_id'] for c in source(lifecycle)['stop_conflicts']]
    resolved = {**fake.cancel(assignment['attempt_id']), 'partial_mutations': partial}
    evidence = 'Q1 exact saved attempt checked: no owned processes; partial mutations inspected'
    assert lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], conflict_ids, resolved, evidence) == {'disposition': 'resolved'}
    reconciled = source(lifecycle)
    assert reconciled['resources'] and reconciled['execution'] == 'held' and reconciled['stop_conflicts'] == []
    assert reconciled['owned_stops'][0]['observation'] == resolved
    assert not any(b['code'] == 'stop_receipt_conflict' for b in reconciled['technical_blockers'])
    with store._connection() as db:
        old = json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='owned_stop_conflict'").fetchone()[0])
        resolution = json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='owned_stop_conflict_resolved'").fetchone()[0])
    assert old == {'retained': proved, 'received': contradiction}
    assert resolution['observation'] == resolved and resolution['conflict_ids'] == conflict_ids and resolution['evidence'] == evidence
    lifecycle = reopen(tmp_path, store)
    assert lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], conflict_ids, resolved, evidence) == {'disposition': 'duplicate'}
    assert lifecycle.owned_stop_result(assignment['assignment_id'], contradiction) == {'disposition': 'duplicate'}
    assert lifecycle.owned_stop_result(assignment['assignment_id'], proved) == {'disposition': 'duplicate'}
    assert source(lifecycle) == reconciled
    fake.cancel = lambda _: pytest.fail('Reconciled known stop must not be repeated')
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        worker.tick()
    terminal = source(lifecycle)
    assert terminal['execution'] == terminal['control'] == 'cancelled' and terminal['resources'] == []
    assert terminal['concept_decision'] == 'approved' and terminal['pending_human_decisions'] == []
    assert lifecycle.complete('late-conflict-completion', assignment['assignment_id'], implementation())['disposition'] == 'historical'
    assert lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], conflict_ids, resolved, evidence) == {'disposition': 'duplicate'}
    assert lifecycle.owned_stop_result(assignment['assignment_id'], contradiction) == {'disposition': 'duplicate'}
    assert len(source(lifecycle)['assignments']) == count_attempts(fake) == 1


def test_new_conflict_during_coordinator_recovery_requires_all_current_conflicts(tmp_path):
    _, lifecycle, _, assignment, _, contradiction = conflicted(tmp_path)
    old_ids = [c['conflict_id'] for c in source(lifecycle)['stop_conflicts']]
    later = {**contradiction, 'owned_processes': ['SIMULATED:later-owned-child']}
    assert lifecycle.owned_stop_result(assignment['assignment_id'], later) == {'disposition': 'conflict'}
    before = source(lifecycle)
    with pytest.raises(Rejected) as rejected:
        lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], old_ids, receipt(assignment), 'Earlier inspection')
    assert rejected.value.code == 'stop_conflict_changed' and source(lifecycle) == before
    current_ids = [c['conflict_id'] for c in before['stop_conflicts']]
    assert len(current_ids) == 2
    lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], current_ids, receipt(assignment), 'Both owned children reconciled')
    assert source(lifecycle)['stop_conflicts'] == [] and source(lifecycle)['resources']


def test_conflict_journal_remains_authoritative_without_derived_blocker(tmp_path):
    store, lifecycle, fake, assignment, proved, _ = conflicted(tmp_path)
    with store._transaction() as db:
        db.execute("DELETE FROM technical_blocker WHERE code='stop_receipt_conflict'")
    lifecycle = reopen(tmp_path, store)
    lifecycle.control('cancel-from-journal', 'Q1', 'cancel')
    lifecycle.cancellation_result('Q1', [])
    held = source(lifecycle)
    assert held['execution'] == 'held' and held['resources'] and held['stop_conflicts']
    lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'],
        [c['conflict_id'] for c in held['stop_conflicts']], proved, 'SIMULATED:exact saved attempt inspection')
    fake.cancel = lambda _: pytest.fail('A resolved known stop must not run again')
    with Worker(reopen(tmp_path, store), fake) as worker:
        worker.tick()
        worker.tick()
    assert source(lifecycle)['execution'] == 'cancelled' and source(lifecycle)['resources'] == []
    assert count_attempts(fake) == 1


def test_lost_stop_reply_reconciles_the_same_attempt_after_restart(tmp_path):
    store, lifecycle, fake, assignment = active(tmp_path, max_seconds=1)
    cancel = fake.cancel
    calls = []
    def lost_reply(attempt):
        calls.append(attempt)
        cancel(attempt)
        raise OSError('SIMULATED lost reply after exact runtime stop')
    fake.cancel = lost_reply
    with store._transaction() as db:
        db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00'")
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert source(lifecycle)['owned_stops'][0]['state'] == 'blocked_unknown_effects'
    lifecycle = reopen(tmp_path, store)
    fake.cancel = lambda attempt: (calls.append(attempt), cancel(attempt))[1]
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        worker.tick()
    after = source(lifecycle)
    assert calls == [assignment['attempt_id'], assignment['attempt_id']]
    assert after['owned_stops'][0]['state'] == 'stopped'
    assert after['execution'] == 'held' and after['resources'] and count_attempts(fake) == 1


def test_interrupted_conflict_resolution_rolls_back_receipt_event_and_blocker(tmp_path):
    store, lifecycle, _, assignment, proved, _ = conflicted(tmp_path)
    before = source(lifecycle)
    ids = [c['conflict_id'] for c in before['stop_conflicts']]
    touch = lifecycle._touch
    def interrupt(db, work_id, kind, detail):
        touch(db, work_id, kind, detail)
        if kind == 'owned_stop_conflict_resolved':
            raise OSError('SIMULATED interruption before resolution commit')
    lifecycle._touch = interrupt
    changed = {**proved, 'partial_mutations': {'status': 'recorded', 'changes': ['fixture.py']}}
    with pytest.raises(OSError, match='SIMULATED'):
        lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], ids, changed, 'SIMULATED:reconciled files')
    lifecycle = reopen(tmp_path, store)
    assert source(lifecycle) == before
    assert lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], ids, changed,
                                                'SIMULATED:reconciled files') == {'disposition': 'resolved'}
    assert source(lifecycle)['owned_stops'][0]['observation'] == changed


def test_old_resolution_replay_cannot_clear_a_later_contradiction(tmp_path):
    store, lifecycle, fake, assignment, proved, contradiction = conflicted(tmp_path)
    old_ids = [c['conflict_id'] for c in source(lifecycle)['stop_conflicts']]
    evidence = 'SIMULATED:first exact attempt inspection'
    lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], old_ids, proved, evidence)
    lifecycle.owned_stop_result(assignment['assignment_id'], {**contradiction, 'owned_processes': ['SIMULATED:later-child']})
    lifecycle = reopen(tmp_path, store)
    before = source(lifecycle)
    assert lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'], old_ids, proved, evidence) == {'disposition': 'duplicate'}
    assert source(lifecycle) == before
    lifecycle.control('cancel-after-new-conflict', 'Q1', 'cancel')
    lifecycle.cancellation_result('Q1', [proved])
    assert source(lifecycle)['resources'] and source(lifecycle)['stop_conflicts'] and count_attempts(fake) == 1


def test_conflict_arriving_during_cancel_is_rechecked_before_resource_release(tmp_path):
    store, lifecycle, fake, assignment, proved, contradiction = conflicted(tmp_path)
    lifecycle.control('cancel-with-race', 'Q1', 'cancel')
    def cancellation(attempt):
        assert attempt == assignment['attempt_id']
        lifecycle.resolve_owned_stop_conflict(assignment['assignment_id'],
            [c['conflict_id'] for c in source(lifecycle)['stop_conflicts']], proved, 'SIMULATED:callback inspection')
        lifecycle.owned_stop_result(assignment['assignment_id'], {**contradiction, 'owned_processes': ['SIMULATED:racing-child']})
        return proved
    fake.cancel = cancellation
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    after = source(reopen(tmp_path, store))
    assert after['execution'] == 'held' and after['resources'] and after['stop_conflicts']
    assert after['owned_stops'][0]['observation'] == proved and count_attempts(fake) == 1
