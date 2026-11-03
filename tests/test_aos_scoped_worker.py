"""Scoped ownership tests use synthetic runtime records, never product jobs."""
import pytest

from agentic_tools.aos.store import Rejected
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import setup, runtime, count_attempts, implementation


def test_scope_leaves_unrelated_history_unchanged(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    setup(tmp_path, store=store, work_id='OTHER', approved=False, admitted=False)
    with store._connection() as db:
        before = {name: list(map(tuple, db.execute('SELECT * FROM ' + name + ' WHERE work_id=?', ('OTHER',))))
                  for name in ('work', 'lifecycle', 'projection', 'technical_blocker', 'lifecycle_event')}
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake, work_id='Q1') as worker:
        worker.tick()
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    with store._connection() as db:
        after = {name: list(map(tuple, db.execute('SELECT * FROM ' + name + ' WHERE work_id=?', ('OTHER',))))
                 for name in before}
    assert after == before
    assert count_attempts(fake) == 2


def test_foreign_unresolved_work_is_rejected_before_owner_status_or_reconcile(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    setup(tmp_path, store=store, work_id='OTHER', approved=False, admitted=False)
    fake = runtime(tmp_path)
    with store._connection() as db:
        before = list(map(tuple, db.execute('SELECT * FROM worker_status')))
    with pytest.raises(Rejected, match='worker_scope'):
        with Worker(lifecycle, fake, work_id='OTHER'):
            pytest.fail('Foreign unresolved work must not be adopted')
    with store._connection() as db:
        assert list(map(tuple, db.execute('SELECT * FROM worker_status'))) == before
    assert count_attempts(fake) == 0
    # Failed entry released the lock; the actual owner can still enter.
    with Worker(lifecycle, fake, work_id='Q1'):
        pass


def test_runtime_attachment_requires_the_one_live_owner(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    worker = Worker(lifecycle, work_id='Q1')
    with pytest.raises(Rejected, match='worker_not_owned'):
        worker.attach_runtime(fake)
    with worker:
        with pytest.raises(Rejected, match='runtime_missing'):
            worker.tick()
        worker.attach_runtime(fake)
        with pytest.raises(Rejected, match='runtime_attached'):
            worker.attach_runtime(fake)
        with pytest.raises(Rejected, match='worker_owned'):
            with Worker(lifecycle, work_id='Q1'):
                pass
        worker.tick()
    assert count_attempts(fake) == 2


def test_reconcile_exception_does_not_leak_worker_lock(tmp_path, monkeypatch):
    _, lifecycle, _ = setup(tmp_path)
    worker = Worker(lifecycle, runtime(tmp_path), work_id='Q1')
    monkeypatch.setattr(worker, 'reconcile_sources', lambda: (_ for _ in ()).throw(RuntimeError('fixture failure')))
    with pytest.raises(RuntimeError, match='fixture failure'):
        with worker:
            pass
    with Worker(lifecycle, work_id='Q1', reconcile_on_enter=False):
        pass


@pytest.mark.parametrize('unknown', [False, True])
def test_scoped_stop_preserves_positive_or_unknown_cleanup(tmp_path, unknown):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'polls': 100,
                              'unknown_effects': unknown, 'result': implementation()}])
    with Worker(lifecycle, fake, work_id='Q1') as worker:
        worker.tick()
        result = worker.stop_owned()
    assert result['known_stopped'] == (not unknown)
    view = lifecycle.snapshot('Q1')
    assert view['resources']
    assert view['owned_stops'][0]['state'] == ('blocked_unknown_effects' if unknown else 'stopped')
    assert count_attempts(fake) == 1


def test_deferred_reconcile_does_not_modify_unbound_target(tmp_path):
    store, lifecycle, _ = setup(tmp_path, approved=False, admitted=False)
    with store._connection() as db:
        before = list(map(tuple, db.execute('SELECT * FROM projection')))
    with Worker(lifecycle, work_id='Q1', reconcile_on_enter=False) as worker:
        worker.heartbeat()
        assert worker.runtime is None
    with store._connection() as db:
        assert list(map(tuple, db.execute('SELECT * FROM projection'))) == before


def test_terminal_assignment_does_not_hide_a_later_stop_conflict(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'polls': 100, 'result': implementation()}])
    with Worker(lifecycle, fake, work_id='Q1') as worker:
        worker.tick()
        assignment = worker.assignments()[0]
        assert worker.stop_owned()['known_stopped']
        conflict = {'attempt_id': assignment['attempt_id'], 'stopped': False,
                    'owned_processes': ['SIMULATED unknown child'], 'effects_reconciled': False,
                    'partial_mutations': 'unknown'}
        assert lifecycle.owned_stop_result(assignment['assignment_id'], conflict)['disposition'] == 'conflict'
        result = worker.stop_owned()
        assert result['known_stopped'] is False
        assert result['stop_conflicts'] == [assignment['attempt_id']]
        view = lifecycle.snapshot('Q1')
        assert view['owned_stops'][0]['state'] == 'stopped'  # Original evidence is preserved.
        assert view['stop_conflicts'] and view['resources']



def test_absent_intent_cleanup_rechecks_current_pause_instead_of_reusing_prior_absence(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake, work_id='Q1') as owner:
        assignment = owner.assignments()[0]
        lifecycle.claim(assignment['assignment_id'], owner.epoch)
        lifecycle.control('pause-sent', 'Q1', 'pause')
        sent = owner.assignments()[0]
        assert owner._unlaunched_intent(sent)
        lifecycle.control('resume-before-cleanup', 'Q1', 'resume')
        result = owner.stop_owned(preserve_absent_intents=True)
    assert result['unlaunched_intents'] == []
    assert lifecycle.snapshot('Q1')['owned_stops'][0]['reason'] == 'supervisor_stopped'
    assert count_attempts(fake) == 0
