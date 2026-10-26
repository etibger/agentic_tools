"""Exact binding and received/recorded distinction required by amendment r2."""
import json
import sqlite3
import subprocess
import sys

import pytest

from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store
from test_aos_lifecycle import contract
from test_aos_store import TEAM


def pending(tmp_path):
    store = Store(tmp_path / 'state', (tmp_path / 'fixture',))
    lifecycle = Lifecycle(store)
    store.introduce('intro', 'RECOVERY', 'Exact decision recovery', ['fixture/'], TEAM)
    bounds = contract({'digest': 'not-self-referential'})
    bounds.pop('concept_digest')
    bounds.pop('concept_revision')
    team = [{**TEAM[0], 'name': person} for person in ('writer', 'assessor')]
    current = store.present('concept', 'RECOVERY', 1, 'RECOVERY-DI', 1, 'Exact immutable snapshot', ['Fixture acceptance'],
                            team, bounds['authority'], execution_envelope=bounds)
    lifecycle.register('contract', 'RECOVERY', contract(current))
    lifecycle.reconcile('RECOVERY')
    cap = store.host_capability('test-host', 'SIMULATED:observed')
    request = store.begin_approval('approval', 'RECOVERY', store.read('RECOVERY')['revision'], current['digest'], capability=cap)
    return store, lifecycle, current, cap, request


def finish(store, cap, request):
    return store.finish_approval(request['challenge_id'], 'accept', True, capability=cap,
                                 exact_answer='Yes, approve this exact revision', source_channel='simulated-tab',
                                 interaction_reference='synthetic-user-interaction')


def test_failed_persistence_reports_received_not_recorded(tmp_path):
    store, lifecycle, _, cap, request = pending(tmp_path)
    def fail(stage):
        if stage == 'before_decision_commit':
            raise sqlite3.OperationalError('injected disk write failure')
    store._fault = fail
    with pytest.raises(Rejected, match='Approval received, recording failed') as error:
        finish(store, cap, request)
    assert error.value.code == 'approval_recording_failed'
    record = store.read('RECOVERY')
    assert record['decisions'] == [] and record['execution'] == 'held'
    # Same interaction/challenge is reconciled, not a duplicate request.
    store._fault = lambda _: None
    result = finish(store, cap, request)
    assert result['approval_recorded'] is True
    assert len(store.read('RECOVERY')['decisions']) == 1


def test_failed_readback_holds_original_approval_without_reopening_decision(tmp_path):
    store, lifecycle, _, cap, request = pending(tmp_path)
    def fail(stage):
        if stage == 'decision_readback':
            raise sqlite3.OperationalError('injected readback failure')
    store._fault = fail
    with pytest.raises(Rejected, match='do not request approval again') as error:
        finish(store, cap, request)
    assert error.value.code == 'decision_readback_failed'
    record = store.read('RECOVERY')
    assert record['concept_decision'] == 'approved' and record['execution'] == 'held'
    assert len(record['decisions']) == 1 and not record['pending_decisions']
    assert lifecycle.admit('held-admission', 'RECOVERY')['execution'] == 'held'
    store._fault = lambda _: None
    assert finish(store, cap, request)['approval_recorded'] is True
    lifecycle.reconcile('RECOVERY')
    assert lifecycle.admit('recovered-admission', 'RECOVERY')['execution'] == 'eligible'
    assert len(store.read('RECOVERY')['decisions']) == 1


def test_interrupt_after_commit_before_projection_recovers_exact_approval_and_hold(tmp_path):
    store, lifecycle, current, cap, request = pending(tmp_path)
    def interrupt(stage):
        if stage == 'after_decision_commit':
            raise RuntimeError('interrupted after durable commit before projection')
    store._fault = interrupt
    with pytest.raises(RuntimeError):
        finish(store, cap, request)
    # A separate interpreter verifies/reconciles the original database, not a mirror.
    script = '''import json,sys
from pathlib import Path
from agentic_tools.aos.store import Store
from agentic_tools.aos.lifecycle import Lifecycle
s=Store(Path(sys.argv[1]),(Path(sys.argv[2]),))
l=Lifecycle(s)
l.reconcile('RECOVERY')
print(json.dumps({'source':l.snapshot('RECOVERY'),'record':s.read('RECOVERY')}))
'''
    fresh = subprocess.run([sys.executable, '-c', script, str(store.directory), str(tmp_path / 'fixture')],
                           check=True, capture_output=True, text=True, timeout=10)
    result = json.loads(fresh.stdout)
    source, record = result['source'], result['record']
    assert source['concept_decision'] == 'approved' and source['execution'] == 'held'
    assert source['projection']['health'] == 'current'
    decision = record['decisions'][0]
    assert decision['digest'] == current['digest']
    assert decision['exact_answer'] == 'Yes, approve this exact revision'
    assert decision['interaction_reference'] == 'synthetic-user-interaction'
    assert source['pending_human_decisions'] == []
    assert source['assignments'] == []
    assert len(record['decisions']) == 1


@pytest.mark.parametrize('field,value', [('exact_answer', 'altered answer'), ('interaction_reference', 'altered reference'),
                                        ('source_channel', 'altered source'), ('created_at', '2000-01-01'),
                                        ('digest', 'sha256:wrong'), ('package_snapshot', '{}')])
def test_any_bound_receipt_mismatch_holds_as_technical_fault(tmp_path, field, value):
    store, lifecycle, _, cap, request = pending(tmp_path)
    finish(store, cap, request)
    with store._transaction() as db:
        db.execute(f'UPDATE decision SET {field}=?', (value,))
    source = lifecycle.reconcile('RECOVERY')
    assert source['concept_decision'] == 'approved' and source['execution'] == 'held'
    assert source['pending_human_decisions'] == []
    assert any(b['code'] == 'decision_readback' for b in source['technical_blockers'])


def test_old_pending_challenge_cannot_overwrite_answer_during_recovery(tmp_path):
    store, lifecycle, _, cap, request = pending(tmp_path)
    finish(store, cap, request)
    with store._transaction() as db:
        db.execute("UPDATE challenge SET status='pending'")  # obsolete cached challenge view
    source = lifecycle.reconcile('RECOVERY')
    assert source['concept_decision'] == 'approved' and source['pending_human_decisions'] == []
    assert not store.pending_approvals()
    assert finish(store, cap, request)['outcome'] == 'approved'
    assert len(store.read('RECOVERY')['decisions']) == 1


def test_existing_database_is_snapshotted_before_additive_migration(tmp_path):
    state = tmp_path / 'old-state'
    state.mkdir(mode=0o700)
    db_path = state / 'aos.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('''CREATE TABLE decision (decision_id TEXT PRIMARY KEY, challenge_id TEXT UNIQUE NOT NULL,
            work_id TEXT NOT NULL, concept_revision INTEGER NOT NULL, digest TEXT NOT NULL, outcome TEXT NOT NULL,
            host TEXT NOT NULL, qualification TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db_path.chmod(0o600)
    store = Store(state, (tmp_path / 'fixture',))
    backup = state / 'aos.pre-q1.sqlite3'
    assert backup.exists()
    with sqlite3.connect(backup) as old:
        assert 'package_snapshot' not in {r[1] for r in old.execute('PRAGMA table_info(decision)')}
    with store._connection() as new:
        assert 'package_snapshot' in {r[1] for r in new.execute('PRAGMA table_info(decision)')}


def test_same_challenge_retry_cannot_change_original_provenance(tmp_path):
    store, _, _, cap, request = pending(tmp_path)
    finish(store, cap, request)
    with pytest.raises(Rejected, match='different answer provenance'):
        store.finish_approval(request['challenge_id'], 'accept', True, capability=cap,
                              exact_answer='A rewritten answer', source_channel='simulated-tab',
                              interaction_reference='synthetic-user-interaction')


def test_completed_request_retry_performs_fresh_readback(tmp_path):
    store, _, current, cap, request = pending(tmp_path)
    finish(store, cap, request)
    with store._transaction() as db:
        db.execute("UPDATE decision SET interaction_reference='corrupted-reference'")
    with pytest.raises(Rejected, match='durable readback failed'):
        store.begin_approval('approval', 'RECOVERY', request['package']['revision'] + 2, current['digest'], capability=cap)
