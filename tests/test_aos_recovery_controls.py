"""Disposable prerequisite evidence, held recovery copies and connected history."""
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from agentic_tools.aos.archive import ArchiveJournal, LocalFakeDestination
from agentic_tools.aos.dashboard_service import DashboardService
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.projection import collect, dashboard
from agentic_tools.aos.store import Rejected, Store
from agentic_tools.aos.worker import FakeRuntime, Worker
from test_aos_coordinator_cli import Coordinator, answer
from test_aos_dashboard_service import command, request, running
from test_aos_lifecycle import setup


def observation(satisfied=True):
    return {'satisfied': satisfied, 'reference': 'SIMULATED:retained-qualification-123',
            'observed_at': '2026-10-26T00:00:00+00:00'}


def prerequisites(cli, current, *, operation='qualified', evidence=None):
    return {'operation_id': operation, 'work_id': 'CLI', 'expected_work_revision': cli.status()['revision'],
            'digest': current['digest'], 'evidence': evidence or {'q0': observation()}}


def test_supported_prerequisite_evidence_preserves_exact_approval_scope_and_retries(tmp_path):
    cli = Coordinator(tmp_path)
    current, _ = cli.prepare(prerequisites={'q0': False, 'isolated_fixture': True})
    answer(cli, current)
    before = cli.status()
    held, _ = cli.control('admit', 'technical-hold', current, code=3)
    assert held['execution'] == 'held'
    payload = prerequisites(cli, current)
    first = cli.request('reconcile-prerequisites', payload)
    assert first['approval_preserved'] and first['technical_prerequisites']['q0'] is True
    assert first == cli.request('reconcile-prerequisites', payload)
    after = cli.status()
    assert after['concepts'] == before['concepts'] and after['decisions'] == before['decisions']
    assert not after['pending_decisions']
    assert after['lifecycle']['prerequisite_evidence'][0]['evidence'] == payload['evidence']
    assert cli.control('admit', 'fresh-admission', current)[0]['execution'] == 'eligible'
    assert cli.request('reconcile-prerequisites', payload) == first  # Replay cannot mutate an admitted contract.
    payload['operation_id'] = 'unsafe-active-update'
    payload['expected_work_revision'] = cli.status()['revision']
    assert cli.request('reconcile-prerequisites', payload, code=2)['error'] == 'prerequisite_ownership_held'


@pytest.mark.parametrize('mode', ['foreign', 'stale', 'wrong-digest', 'conflict', 'malformed'])
def test_prerequisite_updates_reject_foreign_stale_or_conflicting_evidence(tmp_path, mode):
    cli = Coordinator(tmp_path)
    current, _ = cli.prepare(prerequisites={'q0': False, 'isolated_fixture': True})
    answer(cli, current)
    payload = prerequisites(cli, current)
    expected = {'foreign': 'foreign_prerequisite', 'stale': 'stale_revision', 'wrong-digest': 'stale_concept',
                'conflict': 'idempotency_conflict', 'malformed': 'invalid_prerequisite_evidence'}[mode]
    if mode == 'foreign':
        payload['evidence'] = {'new-authority': observation()}
    elif mode == 'stale':
        payload['expected_work_revision'] = 1
    elif mode == 'wrong-digest':
        payload['digest'] = 'sha256:foreign'
    elif mode == 'conflict':
        cli.request('reconcile-prerequisites', payload)
        payload['evidence']['q0']['reference'] = 'different-observation'
    else:
        payload['evidence']['q0']['observed_at'] = 'not-a-qualified-time'
    assert cli.request('reconcile-prerequisites', payload, code=2)['error'] == expected
    assert len(cli.status()['decisions']) == 1 and cli.status()['concept_decision'] == 'approved'


def test_prerequisites_never_replace_approval_or_clear_unrelated_blockers(tmp_path):
    store, lifecycle, current = setup(tmp_path, approved=False, admitted=False)
    with pytest.raises(Rejected, match='approval_required'):
        lifecycle.reconcile_prerequisites('preapproval', 'Q1', store.read('Q1')['revision'], current['digest'], {'q0': observation()})
    other = tmp_path / 'approved'
    approved, lifecycle, current = setup(other, admitted=False, prerequisites={'q0': False, 'isolated_fixture': True})
    with approved._transaction() as db:
        lifecycle._block(db, 'Q1', 'independent_review', 'Missing separate mandatory assessment')
        lifecycle._touch(db, 'Q1', 'test_blocker', {})
    lifecycle.reconcile_prerequisites('technical-fact', 'Q1', approved.read('Q1')['revision'], current['digest'], {'q0': observation()})
    assert any(item['code'] == 'independent_review' for item in lifecycle.snapshot('Q1')['technical_blockers'])
    assert lifecycle.admit('still-held', 'Q1')['execution'] == 'held'


@pytest.mark.parametrize('admitted', [False, True])
def test_pre_t3_canonical_projection_survives_new_read_only_metadata_without_false_corruption(tmp_path, admitted):
    store, lifecycle, _ = setup(tmp_path, admitted=admitted)
    lifecycle.reconcile('Q1')
    with store._connection() as db:
        retained = dict(db.execute("SELECT * FROM projection WHERE work_id='Q1'").fetchone())
    legacy_shape = json.loads(retained['snapshot'])
    assert 'technical_prerequisites' not in legacy_shape and 'prerequisite_evidence' not in legacy_shape
    before = store.read('Q1')
    assert before['lifecycle']['technical_prerequisites']
    lifecycle.reconcile('Q1')
    after = store.read('Q1')
    assert after['revision'] == before['revision'] and after['decisions'] == before['decisions']
    assert after['lifecycle']['assignments'] == before['lifecycle']['assignments']
    assert after['lifecycle']['resources'] == before['lifecycle']['resources']
    assert after['lifecycle']['projection']['health'] == 'current'
    assert not any(item['code'] == 'projection_mismatch' for item in after['lifecycle']['technical_blockers'])
    # Existing corruption detection must still reject a conflicting saved digest.
    with store._transaction() as db:
        db.execute("UPDATE projection SET source_digest='sha256:actual-corruption' WHERE work_id='Q1'")
    lifecycle.reconcile('Q1')
    assert any(item['code'] == 'projection_mismatch' for item in lifecycle.snapshot('Q1')['technical_blockers'])


def restore(backup, target, roots, sha256, *, code=0):
    result = subprocess.run([sys.executable, '-m', 'agentic_tools.aos.cli', 'restore-ledger',
                             '--state-dir', str(target), '--worker-writable-root', str(roots),
                             '--backup', str(backup), '--sha256', sha256], capture_output=True, text=True, timeout=15)
    assert result.returncode == code, result.stdout + result.stderr
    return json.loads(result.stderr if code == 2 else result.stdout)


def test_supported_backup_restore_preserves_decisions_operations_and_holds_dispatch(tmp_path):
    store, _, _ = setup(tmp_path, admitted=False)
    original = store.path.read_bytes()
    backup = tmp_path / 'backup' / 'ledger.sqlite3'
    retained = command(store, 'backup-ledger', '--backup', backup)
    target = tmp_path / 'recovery-copy'
    restored = restore(backup, target, tmp_path / 'fixture', retained['sha256'])
    assert restored['tables'] == retained['tables'] and restored['readback'] == 'verified'
    assert restored['runtime_dispatch_restored'] is restored['runtime_sidecars_restored'] is restored['shared_budget_sidecars_restored'] is False
    reopened = Store(target, (tmp_path / 'fixture',), read_only=True)
    before, after = store.read('Q1'), reopened.read('Q1')
    assert before['decisions'] == after['decisions'] and before['concepts'] == after['concepts']
    assert after['concept_decision'] == 'approved' and after['recovery_hold']['adoption_authorized'] is False
    with store._connection() as first, reopened._connection() as second:
        assert [tuple(row) for row in first.execute('SELECT * FROM operation ORDER BY operation_id')] == [tuple(row) for row in second.execute('SELECT * FROM operation ORDER BY operation_id')]
    lifecycle = Lifecycle(Store(target, (tmp_path / 'fixture',)))
    assert lifecycle.admit('copy-cannot-admit', 'Q1')['execution'] == 'held'
    with pytest.raises(Rejected, match='recovery_copy_held'):
        with Worker(lifecycle, FakeRuntime(target / 'fake.sqlite3', [])):
            pass
    assert any(b['title'] == 'Recovery copy is held' for b in dashboard(collect(reopened, 'Q1'))['blockers'])
    plan = tmp_path / 'empty-plan.json'
    plan.write_text('[]')
    assert command(reopened, 'worker', '--fake-plan', plan, '--max-cycles', '1', code=2)['error'] == 'recovery_copy_held'
    assert not (target / 'fake-runtime.sqlite3').exists()
    assert restore(backup, target, tmp_path / 'fixture', retained['sha256'], code=2)['error'] == 'restore_destination_exists'
    assert store.path.read_bytes() == original
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == retained['sha256']


def test_restore_persists_hold_before_copy_and_retains_interrupted_destination(tmp_path, monkeypatch):
    store, _, _ = setup(tmp_path, admitted=False)
    backup = tmp_path / 'backup' / 'ledger.sqlite3'
    retained = store.consistent_backup(backup)
    target = tmp_path / 'interrupted-recovery'
    real_fsync = os.fsync
    synced = []

    def interrupted_sync(fd):
        assert not (target / 'aos.sqlite3').exists()
        synced.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        real_fsync(fd)
        if len(synced) == 2:
            raise OSError('SIMULATED directory persistence interruption')

    monkeypatch.setattr(os, 'fsync', interrupted_sync)
    with pytest.raises(OSError, match='SIMULATED'):
        Store.restore_backup(backup, target, (tmp_path / 'fixture',), retained['sha256'])
    assert synced == ['file', 'directory']
    assert json.loads((target / 'restore-hold.json').read_text())['adoption_authorized'] is False
    assert stat.S_IMODE((target / 'restore-hold.json').stat().st_mode) == 0o600
    assert not (target / 'aos.sqlite3').exists()
    with pytest.raises(Rejected, match='restore_destination_exists'):
        Store.restore_backup(backup, target, (tmp_path / 'fixture',), retained['sha256'])
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == retained['sha256']


@pytest.mark.parametrize('mode', ['wrong-hash', 'active-ownership', 'worker-root', 'permissions'])
def test_restore_rejects_unsafe_identity_ownership_or_destination_before_creation(tmp_path, mode):
    store, _, _ = setup(tmp_path, admitted=mode == 'active-ownership')
    backup = tmp_path / 'backup' / 'ledger.sqlite3'
    retained = command(store, 'backup-ledger', '--backup', backup)
    target = tmp_path / 'recovery-copy'
    sha256 = retained['sha256']
    expected = {'wrong-hash': 'backup_identity_mismatch', 'active-ownership': 'restore_ownership_held',
                'worker-root': 'worker_writable_state', 'permissions': 'unsafe_backup_permissions'}[mode]
    if mode == 'wrong-hash':
        sha256 = '0' * 64
    elif mode == 'worker-root':
        target = tmp_path / 'fixture' / 'state'
    elif mode == 'permissions':
        backup.chmod(0o644)
    assert restore(backup, target, tmp_path / 'fixture', sha256, code=2)['error'] == expected
    assert not target.exists()


def test_connected_local_history_retains_failure_retry_conflict_and_read_only_identity(tmp_path):
    store, _, _ = setup(tmp_path, admitted=False)
    journal_path = tmp_path / 'history' / 'journal.sqlite3'
    destination = LocalFakeDestination(tmp_path / 'history' / 'destination.sqlite3')
    journal = ArchiveJournal(journal_path)
    journal.prepare('milestone', collect(store, 'Q1'), 'synthetic-milestone', destination)
    destination._fault = lambda _: (_ for _ in ()).throw(OSError('private path must not appear in public history'))
    with pytest.raises(OSError):
        journal.publish_local('milestone', destination)
    destination._fault = lambda _: None
    journal.publish_local('milestone', destination)
    with sqlite3.connect(destination.path) as db:
        db.execute("UPDATE page SET version=2,payload='conflicting retained edit'")
    with pytest.raises(Rejected, match='archive_conflict'):
        journal.publish_local('milestone', destination)
    setup(tmp_path, store=store, work_id='OTHER', admitted=False)
    journal.prepare('other-work-milestone', collect(store, 'OTHER'), 'foreign-private-milestone', destination)
    ledger_before, journal_before = store.path.read_bytes(), journal_path.read_bytes()
    with running(store, archive_journal=journal_path) as (_, receipt):
        history = json.loads(request(receipt, '/work/fixture/history.json')[2])
        assert history['health'] == 'current'
        assert len(history['records']) == 1 and 'foreign-private-milestone' not in json.dumps(history)
        assert [item['state'] for item in history['records'][0]['attempts']] == ['failed', 'verified-local', 'failed']
        assert history['records'][0]['status'] == 'failed'
        assert str(tmp_path) not in json.dumps(history) and 'private path' not in json.dumps(history)
        http = json.loads(request(receipt, '/work/fixture/workflow-state.json')[2])
        cli = command(store, 'status', 'Q1', '--surface', 'dashboard', '--archive-journal', journal_path)
        assert http['source_revision'] == cli['source_revision'] and http['source_digest'] == cli['source_digest']
        assert http['local_history']['records_digest'] == cli['local_history']['records_digest']
        assert http['decisions'] == cli['decisions'] and http['blockers'] == cli['blockers'] and http['usage'] == cli['usage']
        assert not [item for item in http['decisions'] if item['status'] == 'pending']
        assert 'no external publication' in ' '.join(http['findings'])
        history = json.loads(request(receipt, '/work/fixture/history.json')[2])
        retained = journal_path.with_suffix('.retained')
        journal_path.rename(retained)
        try:
            failed = json.loads(request(receipt, '/work/fixture/history.json')[2])
            assert failed['health'] == 'failed' and failed['observed_at'] == history['observed_at']
            assert failed['records'] == history['records']
            view = json.loads(request(receipt, '/work/fixture/workflow-state.json')[2])
            assert any(item['title'] == 'Local history collection failed' for item in view['blockers'])
            assert view['stage_status'] == 'Concept approved · Execution held'
        finally:
            retained.rename(journal_path)
        assert json.loads(request(receipt, '/work/fixture/history.json')[2])['health'] == 'current'
    assert store.path.read_bytes() == ledger_before and journal_path.read_bytes() == journal_before


@pytest.mark.parametrize('surface', ['status', 'dashboard', 'preparation'])
def test_supported_history_paths_reject_assigned_worker_writable_journals(tmp_path, surface):
    store, _, _ = setup(tmp_path, admitted=False)
    journal = tmp_path / 'fixture' / 'history' / 'journal.sqlite3'
    before = store.path.read_bytes()
    if surface == 'status':
        assert command(store, 'status', 'Q1', '--archive-journal', journal, code=2)['error'] == 'worker_writable_history'
    elif surface == 'dashboard':
        readonly = Store(store.directory, (tmp_path / 'fixture',), read_only=True)
        with pytest.raises(Rejected, match='worker_writable_history'):
            DashboardService(readonly, {'fixture': 'Q1'}, 0, archive_journal=journal)
    else:
        assert command(store, 'archive-prepare-local', 'Q1', '--milestone', 'held', '--journal', journal,
                       '--intent-id', 'unsafe-intent', '--fake-destination', tmp_path / 'pages.sqlite3',
                       code=2)['error'] == 'worker_writable_history'
        assert not (tmp_path / 'pages.sqlite3').exists()
    assert not journal.parent.exists() and store.path.read_bytes() == before
