"""A reviewed startup continuation consumes one ordinary lifecycle predecessor.

The fixture uses real decisions, audit SQL and admission checks. Its runtime is
an explicit SQLite-backed simulation; it launches no host processes or models.
"""
import copy
import json

import pytest

from agentic_tools.aos import startup_failure_recovery as recovery
from agentic_tools.aos import startup_resource_continuation as continuation
from agentic_tools.aos import lifecycle as lifecycle_module
from agentic_tools.aos.store import Rejected, canonical
from agentic_tools.aos.worker import FakeRuntime, Worker
from test_aos_lifecycle import assessment, implementation
from test_aos_startup_failure_recovery import failed_startup, sql, tables
from test_startup_resource_continuation import (
    acknowledge, approved_continuation, continuation_proposal,
)


@pytest.fixture
def ready_continuation(approved_continuation):
    c = approved_continuation
    outcome = acknowledge(c, apply=True)
    assert outcome['status'] == 'success' and outcome['applied'] is True
    assert outcome['grants_work'] is outcome['grants_time'] is False
    # Satisfy this separate synthetic fixture obligation explicitly. The real
    # continuation is required to preserve unrelated blockers.
    with c.store._transaction() as db:
        assert db.execute("DELETE FROM technical_blocker WHERE work_id='Q1' AND code='unrelated_obligation'").rowcount == 1
        c.lifecycle._touch(db, 'Q1', 'fixture_obligation_satisfied', {'simulated': True})
    c.predecessor = 'startup-continuation:' + c.continuation_operation
    c.continuation_outcome = outcome
    c.lifecycle.reconcile('Q1')
    return c


def assignment_rows(c):
    return [row for row in c.lifecycle.assignments() if row['work_id'] == 'Q1']


def immutable_history(c):
    with c.store._connection() as db:
        original = dict(db.execute('SELECT * FROM assignment WHERE attempt_id=?', (c.aid,)).fetchone())
        return {
            'assignment': original,
            'outbox': dict(db.execute('SELECT * FROM outbox WHERE assignment_id=?', (original['assignment_id'],)).fetchone()),
            'observation': dict(db.execute('SELECT * FROM runtime_observation WHERE attempt_id=?', (c.aid,)).fetchone()),
            'recovery': dict(db.execute(f'SELECT * FROM {recovery.TABLE} WHERE operation_id=?', (c.request['operation_id'],)).fetchone()),
            'continuation': dict(db.execute(f'SELECT * FROM {continuation.TABLE} WHERE operation_id=?', (c.continuation_operation,)).fetchone()),
            'decisions': [dict(row) for row in db.execute("SELECT * FROM decision WHERE work_id='Q1' ORDER BY decision_id")],
        }


def admission_tables(c):
    # Reconcile legitimately refreshes decision readback and the projection
    # outside the admission transaction. Compare the authoritative write set.
    selected = ('assignment', 'outbox', 'resource', 'lifecycle', 'work', 'operation',
                'lifecycle_event', 'technical_blocker', 'completion', recovery.TABLE, continuation.TABLE)
    all_tables = tables(c.store.path)
    return {name: all_tables[name] for name in selected}


def simulate(c, assessed, monkeypatch):
    # These untyped fixture results test lifecycle transitions, not native
    # adapter qualification. Keep the real package lookup and all assessment
    # validation, replacing only that separate qualification service.
    expected_recipe = copy.deepcopy(c.continuation_contract['runtime_recipe'])
    binding_calls = []
    def fixture_bindings(store, work_id, package, recipe):
        assert store is c.store and work_id == 'Q1'
        assert recipe == expected_recipe == package['execution_envelope']['runtime_recipe']
        assert not any('result_kind' in check or 'adapter_id' in check for check in recipe.get('checks', []))
        binding_calls.append(recipe)
        return {}
    fake = FakeRuntime(c.root / 'continuation-simulation.sqlite3', [
        {'kind': 'implementation', 'result': implementation()},
        {'kind': 'assessment', 'result': assessed},
    ])
    with monkeypatch.context() as qualification_only:
        qualification_only.setattr(lifecycle_module, 'bindings', fixture_bindings)
        with Worker(c.lifecycle, fake) as worker:
            for _ in range(6):
                worker.tick()
    # Every call above must bind the exact package. Failed assessment stops
    # before the additional acceptance check, so call count is not an invariant.
    assert binding_calls
    return fake


def test_successful_audit_admits_one_writer_without_rewriting_failed_history(ready_continuation):
    c = ready_continuation
    history, runtime_before = immutable_history(c), tables(c.runtime)
    result = c.lifecycle.admit('admit-reviewed-continuation', 'Q1')
    assert result['execution'] == 'eligible'
    rows = assignment_rows(c)
    assert len(rows) == 2
    new = next(row for row in rows if row['attempt_id'] != c.aid)
    assert new['predecessor'] == c.predecessor and new['kind'] == 'implementation'
    assert new['state'] == 'proposed'
    assert new['concept_revision'] == c.continuation_concept['concept_revision']
    assert new['concept_digest'] == c.continuation_concept['digest']
    assert new['contract_revision'] == c.continuation_contract['revision']
    assert immutable_history(c) == history and tables(c.runtime) == runtime_before
    with c.store._connection() as db:
        assert db.execute('SELECT state FROM outbox WHERE assignment_id=?', (new['assignment_id'],)).fetchone()[0] == 'pending'
        run = dict(db.execute("SELECT * FROM lifecycle WHERE work_id='Q1'").fetchone())
        assert run['admitted'] == 1
        contract = json.loads(run['contract'])
        assert contract['max_assignments'] == 3 and contract['max_repairs'] == 0


def test_repeat_admission_consumes_no_second_predecessor_and_preserves_active_state(ready_continuation):
    c = ready_continuation
    first = c.lifecycle.admit('admit-once', 'Q1')
    assert c.lifecycle.admit('admit-once', 'Q1') == first
    fake = FakeRuntime(c.root / 'waiting-writer.sqlite3', [
        {'kind': 'implementation', 'result': implementation(), 'polls': 20}])
    with Worker(c.lifecycle, fake) as worker:
        worker.tick()
        before = c.lifecycle.snapshot('Q1')
        rows_before = assignment_rows(c)
        result = c.lifecycle.admit('admit-again-new-operation', 'Q1')
        assert result['continuation_already_admitted'] is True
        assert result['execution'] == before['execution']
        assert assignment_rows(c) == rows_before
        assert c.lifecycle.snapshot('Q1')['execution'] == before['execution']
    assert len(assignment_rows(c)) == 2
    assert sql(fake.path, 'SELECT COUNT(*) FROM attempt')[0][0] == 1


def test_new_writer_and_assessor_deliver_with_three_cumulative_assignments(ready_continuation, monkeypatch):
    c = ready_continuation
    history, runtime_before = immutable_history(c), tables(c.runtime)
    c.lifecycle.admit('admit-delivery', 'Q1')
    fake = simulate(c, assessment(), monkeypatch)
    snapshot = c.lifecycle.snapshot('Q1')
    rows = assignment_rows(c)
    assert snapshot['execution'] == 'delivered' and snapshot['delivery']['accepted'] is True
    assert snapshot['delivery']['committed'] is snapshot['delivery']['installed'] is snapshot['delivery']['released'] is False
    assert len(rows) == 3
    assert [row['kind'] for row in rows].count('implementation') == 2
    assert [row['kind'] for row in rows].count('assessment') == 1
    assert next(row for row in rows if row['attempt_id'] == c.aid)['state'] == 'failed'
    assert all(row['state'] == 'completed' for row in rows if row['attempt_id'] != c.aid)
    assert sql(fake.path, 'SELECT COUNT(*) FROM attempt')[0][0] == 2
    assert immutable_history(c) == history
    assert tables(c.runtime) == runtime_before, 'Simulation must not rewrite the original real-runtime reservations'
    assert snapshot['resources'] == []
    with pytest.raises(Rejected, match='terminal_execution'):
        c.lifecycle.admit('no-fourth-after-delivery', 'Q1')
    assert len(assignment_rows(c)) == 3


def test_confirmed_assessor_failure_cannot_allocate_a_repair(ready_continuation, monkeypatch):
    c = ready_continuation
    history = immutable_history(c)
    c.lifecycle.admit('admit-no-repairs', 'Q1')
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'SIMULATED remaining defect'})
    bad['checks'][0]['result'] = 'fail'
    simulate(c, bad, monkeypatch)
    snapshot = c.lifecycle.snapshot('Q1')
    assert snapshot['execution'] == 'held'
    assert len(assignment_rows(c)) == 3
    assert all(row['kind'] != 'repair' for row in assignment_rows(c))
    assert any(blocker['code'] == 'repair_limit' for blocker in snapshot['technical_blockers'])
    assert immutable_history(c) == history


@pytest.mark.parametrize('guard', ['approval', 'unrelated-blocker'])
def test_ordinary_approval_and_blocker_guards_run_before_continuation_consumption(ready_continuation, monkeypatch, guard):
    c = ready_continuation
    with c.store._transaction() as db:
        if guard == 'approval':
            db.execute('DELETE FROM decision WHERE decision_id=?', (c.continuation_decision['decision_id'],))
        else:
            c.lifecycle._block(db, 'Q1', 'fixture_open_obligation', 'SIMULATED unsatisfied obligation')
        c.lifecycle._touch(db, 'Q1', 'fixture_guard_change', {'guard': guard})
    def must_not_consume(*args):
        pytest.fail('Continuation lookup must follow ordinary admission guards')
    monkeypatch.setattr(continuation, 'admission_predecessor', must_not_consume)
    before = assignment_rows(c)
    result = c.lifecycle.admit('admit-without-ordinary-authority', 'Q1')
    assert result['execution'] == 'held' and result['blockers']
    assert assignment_rows(c) == before
    assert not sql(c.store.path, "SELECT 1 FROM resource WHERE work_id='Q1'")


@pytest.mark.parametrize('field', ['work_id', 'attempt_id'])
def test_foreign_recovery_audit_cannot_back_current_continuation(ready_continuation, field):
    c = ready_continuation
    raw = sql(c.store.path, f'SELECT record FROM {recovery.TABLE} WHERE operation_id=?', (c.request['operation_id'],))[0][0]
    record = json.loads(raw)
    record[field] = 'foreign-work-or-attempt'
    sql(c.store.path, f'UPDATE {recovery.TABLE} SET record=? WHERE operation_id=?',
        (canonical(record), c.request['operation_id']))
    c.lifecycle.reconcile('Q1')
    before, runtime_before = admission_tables(c), tables(c.runtime)
    with pytest.raises(Rejected, match='startup_resource_continuation'):
        c.lifecycle.admit('admit-foreign-audit', 'Q1')
    assert admission_tables(c) == before and tables(c.runtime) == runtime_before


def insert_terminal_assignment(c, *, key, work_id='Q1', predecessor=None):
    with c.store._transaction() as db:
        row = dict(db.execute('SELECT * FROM assignment WHERE attempt_id=?', (c.aid,)).fetchone())
        row.update(assignment_id=key, attempt_id=key, work_id=work_id,
                   predecessor=predecessor or 'fixture-terminal:' + key)
        columns = list(row)
        db.execute('INSERT INTO assignment (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                   [row[column] for column in columns])
        c.lifecycle._touch(db, 'Q1', 'fixture_retained_assignment', {'id': key})


def test_exhausted_cumulative_assignment_limit_rolls_back_admission(ready_continuation):
    c = ready_continuation
    insert_terminal_assignment(c, key='fixture-failed-two')
    insert_terminal_assignment(c, key='fixture-failed-three')
    c.lifecycle.reconcile('Q1')
    before, runtime_before = admission_tables(c), tables(c.runtime)
    with pytest.raises(Rejected, match='startup_continuation'):
        c.lifecycle.admit('admit-exhausted-continuation', 'Q1')
    assert len(assignment_rows(c)) == 3
    assert admission_tables(c) == before and tables(c.runtime) == runtime_before
    assert not sql(c.store.path, 'SELECT 1 FROM assignment WHERE predecessor=?', (c.predecessor,))
    assert not sql(c.store.path, "SELECT 1 FROM resource WHERE work_id='Q1'")


def test_foreign_assignment_cannot_consume_the_global_unique_predecessor(ready_continuation):
    c = ready_continuation
    insert_terminal_assignment(c, key='fixture-foreign-owner', work_id='OTHER', predecessor=c.predecessor)
    c.lifecycle.reconcile('Q1')
    before = admission_tables(c)
    with pytest.raises(Rejected, match='startup_continuation'):
        c.lifecycle.admit('admit-foreign-predecessor', 'Q1')
    assert admission_tables(c) == before
    assert len(assignment_rows(c)) == 1


def test_failed_enqueue_does_not_claim_resources_or_consume_the_continuation(ready_continuation):
    c = ready_continuation
    before, runtime_before = admission_tables(c), tables(c.runtime)
    def fail(stage):
        if stage == 'before_outbox_enqueue':
            raise RuntimeError('SIMULATED enqueue interruption')
    c.lifecycle._fault = fail
    with pytest.raises(RuntimeError, match='SIMULATED enqueue'):
        c.lifecycle.admit('admit-interrupted-enqueue', 'Q1')
    assert admission_tables(c) == before and tables(c.runtime) == runtime_before
    assert not sql(c.store.path, 'SELECT 1 FROM assignment WHERE predecessor=?', (c.predecessor,))
    c.lifecycle._fault = lambda stage: None
    result = c.lifecycle.admit('admit-interrupted-enqueue', 'Q1')
    assert result['execution'] == 'eligible'
    assert len(assignment_rows(c)) == 2
