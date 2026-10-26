"""Corrective regressions for approved bounds, exact owned stop and mutation evidence."""
import json
import sqlite3
from importlib.metadata import version

import pytest

from agentic_tools.aos import version_receipt
from agentic_tools.aos.cli import main
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Store, canonical
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import approve, assessment, contract, count_attempts, implementation, run_to_end, runtime, setup
from test_aos_store import TEAM


@pytest.mark.parametrize('override', [
    {'model': 'UNAPPROVED-MODEL'}, {'effort': 'low'}, {'writer': 'different-writer'}, {'assessor': 'different-assessor'},
    {'authority': 'Install and publish outside fixture'}, {'receiving_owner': 'different-owner'},
    {'required_checks': ['replacement-check']}, {'compatibility_checks': ['replacement-consumer']},
    {'resources': ['different-source']}, {'baseline': 'different-baseline'}, {'behavior_delta': 'different-feature'},
    {'qualified_route': 'unqualified-route'}, {'prerequisites': {'weaker': True}},
    {'max_assignments': 9}, {'max_seconds': 61},
])
def test_outside_approved_execution_bounds_cannot_create_attempt(tmp_path, override):
    store, lifecycle, _ = setup(tmp_path, **override)
    fake = runtime(tmp_path)
    run_to_end(lifecycle, fake)
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'held' and source['concept_decision'] == 'approved'
    assert source['pending_human_decisions'] == []
    assert source['assignments'] == [] and count_attempts(fake) == 0
    assert any('approved' in b['detail'] for b in source['technical_blockers'])
    assert len(store.read('Q1')['decisions']) == 1


def test_approved_package_contains_exact_execution_bounds_and_named_team(tmp_path):
    store, lifecycle, _ = setup(tmp_path, max_seconds=30, max_assignments=4, max_repairs=1)
    package = store.read('Q1')['concepts'][0]['package']
    bounds = package['execution_envelope']
    assert bounds['model'] == 'gpt-6.1-sol' and bounds['effort'] == 'high'
    assert bounds['max_seconds'] == 60 and bounds['max_assignments'] == 8
    assert {m['name'] for m in package['team_envelope']} == {'writer', 'assessor'}
    assert lifecycle.snapshot('Q1')['execution'] == 'eligible'  # Narrower limits are permitted.


def test_legacy_approved_concept_without_execution_envelope_stays_approved_and_held(tmp_path):
    store = Store(tmp_path / 'state', (tmp_path / 'fixture',))
    store.introduce('intro', 'Q1', 'Synthetic legacy concept', ['fixture/'], TEAM)
    current = store.present('present', 'Q1', 1, 'Q1-DI', 1, 'Legacy package', ['No dispatch'], TEAM, 'Synthetic only')
    approve(store, current)
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'Q1', contract(current))
    assert lifecycle.admit('admit', 'Q1')['execution'] == 'held'
    assert store.read('Q1')['concept_decision'] == 'approved'
    assert len(store.read('Q1')['decisions']) == 1 and not store.read('Q1')['pending_decisions']


@pytest.mark.parametrize('field,value', [('model', 'UNAPPROVED-MODEL'), ('authority', 'Install elsewhere'), ('max_seconds', 61)])
def test_saved_launch_rechecks_approved_envelope_before_runtime_start(tmp_path, field, value):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        def drift(stage):
            if stage == 'after_intent_before_launch':
                with store._transaction() as db:
                    row = db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()
                    changed = json.loads(row[0])
                    changed[field] = value
                    db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(changed),))
        worker._fault = drift
        worker.tick()
    assert count_attempts(fake) == 0
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert count_attempts(fake) == 0
    assert lifecycle.snapshot('Q1')['owned_stops'][0]['state'] == 'stopped'
    assert lifecycle.snapshot('Q1')['resources']


def test_recovery_of_active_attempt_rechecks_authority_and_stops_exact_attempt(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    attempt = lifecycle.assignments()[0]['attempt_id']
    with store._transaction() as db:
        changed = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()[0])
        changed['authority'] = 'Expanded unapproved authority'
        db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(changed),))
    calls = []
    cancel = fake.cancel
    def observed_cancel(attempt_id):
        calls.append(attempt_id)
        return cancel(attempt_id)
    fake.cancel = observed_cancel
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert calls == [attempt]
    assert fake.lookup(attempt)['state'] == 'cancelled'
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert lifecycle.snapshot('Q1')['resources']


@pytest.mark.parametrize('unknown_stop', [False, True])
def test_deadline_checks_before_poll_and_retains_stop_evidence_and_ownership(tmp_path, unknown_stop):
    store, lifecycle, _ = setup(tmp_path, max_seconds=1)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000, 'unknown_effects': unknown_stop}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        first = lifecycle.assignments()[0]
        with store._transaction() as db:
            db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00'")
        fake.poll = lambda _: pytest.fail('Expired attempt must stop before another poll/terminal consumption')
        worker.tick()
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'held' and source['resources']
    stop = source['owned_stops'][0]
    assert stop['attempt_id'] == first['attempt_id'] and stop['reason'] == 'assignment_deadline'
    assert stop['state'] == ('blocked_unknown_effects' if unknown_stop else 'stopped')
    assert stop['observation']['partial_mutations']
    assert len(source['assignments']) == 1


def test_terminal_returned_by_poll_after_deadline_cannot_enqueue_successor(tmp_path):
    store, lifecycle, _ = setup(tmp_path, max_seconds=1)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        first = lifecycle.assignments()[0]
        def late_poll(attempt_id):
            with store._transaction() as db:
                db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00'")
            return {'state': 'completed', 'attempt_id': attempt_id, 'model': 'gpt-6.1-sol', 'effort': 'high',
                    'receipt_id': 'late-terminal', 'result': implementation()}
        fake.poll = late_poll
        worker.tick()
        worker.tick()
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'held' and len(source['assignments']) == 1
    assert source['owned_stops'][0]['attempt_id'] == first['attempt_id']
    assert fake.lookup(first['attempt_id'])['state'] == 'cancelled'
    with store._connection() as db:
        assert db.execute("SELECT disposition FROM completion WHERE receipt_id='late-terminal'").fetchone()[0] == 'deadline_held'


@pytest.mark.parametrize('partial', [None, '', 'unknown', 'unreconciled', False, {}, [], {'status': 'unknown'}, {'status': 'none', 'changes': ['file.py']}, {'status': 'recorded', 'detail': 'unknown'}, {'status': 'recorded', 'changes': ['unknown']}])
def test_missing_or_unknown_cancellation_mutations_hold_resources(tmp_path, partial):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    first = lifecycle.assignments()[0]
    lifecycle.control('cancel', 'Q1', 'cancel')
    observation = {'attempt_id': first['attempt_id'], 'stopped': True, 'owned_processes': [],
                   'effects_reconciled': True, 'partial_mutations': partial}
    lifecycle.cancellation_result('Q1', [observation])
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'held' and source['resources']
    assert source['owned_stops'][0]['observation'] == observation
    assert source['owned_stops'][0]['state'] == 'blocked_unknown_effects'


@pytest.mark.parametrize('partial', ['none', {'status': 'none'}, {'status': 'recorded', 'changes': ['fixture.py']}])
def test_explicit_known_cancellation_mutations_are_durable_before_resource_release(tmp_path, partial):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    first = lifecycle.assignments()[0]
    lifecycle.control('cancel', 'Q1', 'cancel')
    observation = {'attempt_id': first['attempt_id'], 'stopped': True, 'owned_processes': [],
                   'effects_reconciled': True, 'partial_mutations': partial}
    lifecycle.cancellation_result('Q1', [observation])
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    source = reopened.snapshot('Q1')
    assert source['execution'] == 'cancelled' and source['resources'] == []
    assert source['owned_stops'][0]['observation'] == observation
    with store._connection() as db:
        saved = json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='cancel_reconciled'").fetchone()[0])
        assert saved == [observation]


def test_version_receipt_explicitly_distinguishes_component_and_distribution(capsys):
    receipt = version_receipt()
    assert receipt['aos_component_version'] == '0.2.3'
    assert receipt['distribution_name'] == 'agentic-tools'
    assert receipt['distribution_version'] == version('agentic-tools')
    with pytest.raises(SystemExit) as exit:
        main(['--version'])
    assert exit.value.code == 0
    output = capsys.readouterr().out
    assert 'AOS component 0.2.3' in output and f"agentic-tools distribution {version('agentic-tools')}" in output


def test_pause_survives_deadline_stop_and_restart(tmp_path):
    store, lifecycle, _ = setup(tmp_path, max_seconds=1)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        lifecycle.control('pause-before-expiry', 'Q1', 'pause')
        with store._transaction() as db:
            db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00'")
        worker.tick()
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    with Worker(reopened, fake) as worker:
        worker.tick()
    assert reopened.snapshot('Q1')['execution'] == 'paused'
    assert reopened.snapshot('Q1')['owned_stops'][0]['state'] == 'stopped'
    assert count_attempts(fake) == 1
