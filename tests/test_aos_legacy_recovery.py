"""Prospective legacy retirement never manufactures historical cleanup."""
import fcntl
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from agentic_tools.aos import legacy_recovery as recovery
from agentic_tools.aos.lifecycle import Lifecycle, snapshot_in
from agentic_tools.aos.projection import collect, dashboard
from agentic_tools.aos.store import Rejected, canonical, digest
from agentic_tools.aos.worker import FakeRuntime, Worker
from test_aos_lifecycle import setup, implementation


HOST = {'host_id': 'a' * 64, 'boot_seconds': 1577923200, 'boot_microseconds': 0,
        'boot_session': 'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE'}
EARLIER = '2020-01-01T00:00:00+00:00'


def retain(path, value):
    raw = (json.dumps(value, sort_keys=True) if not isinstance(value, str) else value).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return {'path': str(path), 'sha256': recovery.sha256(raw)}


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'observe_host', lambda: dict(HOST))
    monkeypatch.setattr(recovery, 'observe_quiescence', lambda resources, attempt, assignment:
                        {'uid': os.getuid(), 'methods': ['simulated-quiescence'], 'matching_processes': []})
    store, lifecycle, concept = setup(tmp_path)
    source = tmp_path / 'fixture'
    source.mkdir()
    (source / 'old.py').write_text('original\n')
    baseline = recovery.inventory(source)
    (source / 'old.py').write_text('later authorized delivery\n')
    (source / 'new.py').write_text('later authorized new file\n')
    outputs = tmp_path / 'outputs'
    with store._transaction() as db:
        a = dict(db.execute("SELECT * FROM assignment WHERE work_id='Q1'").fetchone())
        db.execute("UPDATE assignment SET state='blocked_unknown_effects',created_at=?,sent_at=? WHERE assignment_id=?", (EARLIER, EARLIER, a['assignment_id']))
        db.execute("UPDATE lifecycle SET execution='held',control='blocked_unknown_effects' WHERE work_id='Q1'")
        run = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()[0])
        run.update(runtime_recipe={'source': str(source), 'outputs': str(outputs), 'max_seconds': 14400}, resources=[str(source), str(outputs), 'native-executor'])
        db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(run),))
        db.execute("DELETE FROM resource WHERE work_id='Q1'")
        for resource in run['resources']:
            db.execute('INSERT INTO resource VALUES(?,?,?,?,?)', (resource, 'Q1', a['assignment_id'], 'owned', EARLIER))
        lifecycle._block(db, 'Q1', 'unknown_effects', 'Retained uncertain legacy startup')
        lifecycle._block(db, 'Q1', 'other_obligation', 'Unrelated evidence obligation remains')
        db.execute("INSERT INTO worker_status VALUES(1,1,'old-worker',?,'stopped')", (EARLIER,))
    attempt_outputs = outputs / a['attempt_id']
    attempt_outputs.mkdir(parents=True)
    (attempt_outputs / 'retained-error.txt').write_text('original failure\n')
    record = {'attempt_id': a['attempt_id'], 'assignment': a, 'phase': 'thread_request',
              'model_calls_attempted': 0, 'created_at': EARLIER, 'baseline': baseline,
              'recipe_digest': digest(run['runtime_recipe']),
              'run_outputs': str(attempt_outputs), 'budget_id': 'original-budget', 'budget_seconds': 14400,
              'error': 'Retained legacy thread/start failure'}
    runtime = store.directory / 'codex-runtime.sqlite3'
    with sqlite3.connect(runtime) as db:
        db.execute('CREATE TABLE attempt(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('INSERT INTO attempt VALUES(?,?)', (a['attempt_id'], canonical(record)))
        db.execute('CREATE TABLE runtime_budget(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('INSERT INTO runtime_budget VALUES(?,?)', ('original-budget', canonical({
            'budget_id': 'original-budget', 'kind': 'product', 'source': str(source), 'product_id': 'Q1',
            'max_assignments': 4, 'max_seconds': 57600, 'max_job_seconds': 14400})))
        db.execute('CREATE TABLE product_budget_scope(scope TEXT PRIMARY KEY,budget_id TEXT NOT NULL)')
        db.execute('INSERT INTO product_budget_scope VALUES(?,?)', ('source:' + str(source), 'original-budget'))
        db.execute('INSERT INTO product_budget_scope VALUES(?,?)', ('product:Q1', 'original-budget'))
    runtime.chmod(0o600)
    request = {'operation_id': 'retire-old-1', 'work_id': 'Q1', 'expected_work_revision': store.read('Q1')['revision'],
               'digest': concept['digest'], 'attempt_id': a['attempt_id'],
               'runtime_record_sha256': recovery.sha256(canonical(record).encode())}
    observed = recovery.inspect_legacy(lifecycle, request)
    evidence_root = tmp_path / 'private-evidence'
    evidence_root.mkdir(mode=0o700)
    evidence = {'format': 'aos-legacy-recovery-v1', 'work_id': 'Q1', 'attempt_id': a['attempt_id'],
                'host': observed['host'], 'snapshot': observed['snapshot'],
                'host_provenance': retain(evidence_root / 'host.txt', 'SIMULATED independent local host provenance'),
                'authority': retain(evidence_root / 'authority.txt', 'SIMULATED explicit user request to recover current resources'),
                'adoption': {'summary': 'Retain current known delivery; historical startup remains unknown',
                             'delivery_inventory': retain(evidence_root / 'delivery.json', observed['snapshot']['source_inventory'])},
                'review': {'author': 'operator', 'reviewer': 'independent-assessor',
                           'report': retain(evidence_root / 'review.json', {
                               'format': 'aos-legacy-current-safety-review-v1', 'work_id': 'Q1', 'attempt_id': a['attempt_id'],
                               'host': observed['host'], 'snapshot_digest': digest(observed['snapshot']),
                               'author': 'operator', 'reviewer': 'independent-assessor', 'verdict': 'pass',
                               'historical_startup_outcome': 'unknown', 'current_content_adoption': True,
                               'local_execution_provenance_verified': True,
                               'reason': 'SIMULATED independent assessment of current resource safety and adoption'})}}
    bundle = retain(evidence_root / 'bundle.json', evidence)
    request.update(evidence_path=bundle['path'], evidence_sha256=bundle['sha256'])
    return store, lifecycle, request, evidence, record


def refresh_bundle(request, evidence):
    from pathlib import Path
    request['evidence_sha256'] = retain(Path(request['evidence_path']), evidence)['sha256']


def test_preview_and_apply_preserve_history_budget_and_other_work(legacy, tmp_path):
    store, lifecycle, req, evidence, record = legacy
    setup(tmp_path, store=store, work_id='OTHER', admitted=False)
    with store._connection() as db:
        other = snapshot_in(db, 'OTHER')
    before = store.read('Q1')
    runtime = store.directory / 'codex-runtime.sqlite3'
    original_runtime = runtime.read_bytes()
    ledger = store.path.read_bytes()
    with store._connection() as db:
        old_events = [dict(r) for r in db.execute('SELECT * FROM lifecycle_event ORDER BY event_id')]
    assert recovery.inspect_legacy(lifecycle, req)['snapshot'] == evidence['snapshot']
    assert store.path.read_bytes() == ledger
    preview = recovery.recover(lifecycle, req)
    assert preview['ready'] and not preview['applied']
    assert store.path.read_bytes() == ledger
    result = recovery.recover(lifecycle, req, apply=True)
    assert result['execution'] == 'retired' and result['historical_startup_outcome'] == 'unknown'
    assert result['runtime_and_budget_modified'] is result['new_work_admitted'] is False
    assert runtime.read_bytes() == original_runtime
    after = store.read('Q1')
    assert after['decisions'] == before['decisions'] and after['concepts'] == before['concepts']
    with store._connection() as db:
        assert [dict(r) for r in db.execute('SELECT * FROM lifecycle_event ORDER BY event_id')][:-1] == old_events
        assert snapshot_in(db, 'OTHER') == other
    assert after['lifecycle']['resources'] == [] and after['lifecycle']['control'] == 'retired'
    assert after['lifecycle']['owned_stops'] == before['lifecycle']['owned_stops'] == []
    assert any(b['code'] == 'other_obligation' for b in after['lifecycle']['technical_blockers'])
    with store._connection() as db:
        event = json.loads(db.execute("SELECT detail FROM lifecycle_event WHERE kind='legacy_retired'").fetchone()[0])
        assert event['evidence'] == evidence and event['historical_startup_outcome'] == 'unknown'
        assert db.execute("SELECT admitted FROM lifecycle WHERE work_id='Q1'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == 'cancelled'
    rendered = dashboard(collect(store, 'Q1'))
    assert 'retired' in rendered['stage_status']
    assert 'Historical startup remains unknown' in rendered['attention_note']
    assert rendered['tasks'][0]['status'] == 'retired' and rendered['completed_at'] is None
    assert recovery.recover(lifecycle, req, apply=True) == result
    assert recovery.recover(lifecycle, req) == result
    with (store.directory / 'worker.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert recovery.recover(lifecycle, req, apply=True) == result
    conflict = {**req, 'evidence_sha256': 'b' * 64}
    with pytest.raises(Rejected, match='idempotency_conflict'):
        recovery.recover(lifecycle, conflict, apply=True)


@pytest.mark.parametrize('mutation,error', [
    ('revision', 'stale_revision'), ('concept', 'stale_concept'), ('runtime', 'legacy_runtime_changed'),
    ('source', 'legacy_inventory_changed'), ('outputs', 'legacy_inventory_changed'),
    ('resource', 'legacy_resource_scope'), ('resource-state', 'legacy_inventory_changed'),
    ('foreign-resource', 'legacy_resource_conflict'),
    ('extra-assignment', 'legacy_scope'), ('stop-conflict', 'legacy_stop_conflict'),
    ('stop-receipt', 'legacy_stop_conflict'), ('budget', 'legacy_inventory_changed'),
    ('budget-scope', 'legacy_budget_missing'), ('failed-review', 'legacy_review_required'),
    ('stale-review', 'legacy_review_required'),
    ('worker', 'legacy_worker_active'), ('host', 'legacy_host_changed'),
    ('earlier-boot', 'legacy_reboot_required'), ('evidence', 'legacy_evidence_changed'),
    ('delivery', 'legacy_inventory_changed'), ('same-reviewer', 'legacy_review_required'),
])
def test_reject_changed_or_unqualified_recovery(legacy, mutation, error, monkeypatch):
    store, life, req, evidence, record = legacy
    from pathlib import Path
    if mutation == 'revision':
        req['expected_work_revision'] += 1
    elif mutation == 'concept':
        req['digest'] = 'sha256:wrong'
    elif mutation == 'runtime':
        with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
            db.execute("UPDATE attempt SET record='{}'")
    elif mutation == 'source':
        (Path(evidence['snapshot']['source']) / 'new.py').write_text('changed')
    elif mutation == 'outputs':
        (Path(evidence['snapshot']['outputs']) / 'another.txt').write_text('new')
    elif mutation == 'host':
        monkeypatch.setattr(recovery, 'observe_host', lambda: {**HOST, 'host_id': 'f' * 64})
    elif mutation == 'earlier-boot':
        changed = {**HOST, 'boot_seconds': 1577750400}
        evidence['host'] = changed
        ref = evidence['review']['report']
        report = json.loads(Path(ref['path']).read_bytes())
        report['host'] = changed
        evidence['review']['report'] = retain(Path(ref['path']), report)
        refresh_bundle(req, evidence)
        monkeypatch.setattr(recovery, 'observe_host', lambda: changed)
    elif mutation == 'evidence':
        Path(req['evidence_path']).write_text('{}')
    elif mutation == 'delivery':
        ref = evidence['adoption']['delivery_inventory']
        evidence['adoption']['delivery_inventory'] = retain(Path(ref['path']), {})
        refresh_bundle(req, evidence)
    elif mutation in ('failed-review', 'stale-review'):
        ref = evidence['review']['report']
        report = json.loads(Path(ref['path']).read_bytes())
        report['verdict' if mutation == 'failed-review' else 'snapshot_digest'] = 'failed-or-stale'
        evidence['review']['report'] = retain(Path(ref['path']), report)
        refresh_bundle(req, evidence)
    elif mutation in ('budget', 'budget-scope'):
        with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
            if mutation == 'budget':
                raw = json.loads(db.execute('SELECT record FROM runtime_budget').fetchone()[0])
                raw['max_seconds'] += 1
                db.execute('UPDATE runtime_budget SET record=?', (canonical(raw),))
            else:
                db.execute("DELETE FROM product_budget_scope WHERE scope='product:Q1'")
    elif mutation == 'same-reviewer':
        evidence['review']['reviewer'] = evidence['review']['author']
        refresh_bundle(req, evidence)
    else:
        with store._transaction() as db:
            if mutation == 'resource':
                db.execute("DELETE FROM resource WHERE resource_id='native-executor'")
            elif mutation == 'resource-state':
                db.execute("UPDATE resource SET observed_at='changed'")
            elif mutation == 'foreign-resource':
                db.execute('INSERT INTO resource VALUES(?,?,?,?,?)',
                           (str(Path(evidence['snapshot']['source']) / 'overlap'), 'OTHER', None, 'owned', EARLIER))
            elif mutation == 'extra-assignment':
                a = dict(db.execute('SELECT * FROM assignment').fetchone())
                a.update(assignment_id='extra', attempt_id='extra', predecessor='extra')
                db.execute('INSERT INTO assignment VALUES(' + ','.join('?' for _ in a) + ')', tuple(a.values()))
            elif mutation == 'stop-conflict':
                life._block(db, 'Q1', 'stop_receipt_conflict', 'conflicting retained receipt')
            elif mutation == 'stop-receipt':
                db.execute('INSERT INTO owned_stop VALUES(?,?,?,?,?,?,?)',
                           (req['attempt_id'], 'Q1', 'old', 'stopped', EARLIER, '{}', EARLIER))
            elif mutation == 'worker':
                db.execute("UPDATE worker_status SET state='running'")
    before = store.path.read_bytes()
    with pytest.raises(Rejected, match=error):
        recovery.recover(life, req, apply=True)
    assert store.path.read_bytes() == before
    assert life.snapshot('Q1')['resources'] or mutation == 'resource'


@pytest.mark.parametrize('field,value', [('phase', 'turn_request'), ('model_calls_attempted', 1),
    ('model_calls_attempted', None), ('model_calls_attempted', False), ('thread_id', 'unexpected'),
    ('turn_id', 'unexpected'), ('startup_version', 1), ('startup_version', None), ('terminal_observation', {})])
def test_reject_nonlegacy_or_possible_model_intent(legacy, field, value):
    store, life, req, evidence, record = legacy
    record[field] = value
    raw = canonical(record)
    with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
        db.execute('UPDATE attempt SET record=?', (raw,))
    req['runtime_record_sha256'] = recovery.sha256(raw.encode())
    with pytest.raises(Rejected, match='legacy_record_class'):
        recovery.recover(life, req, apply=True)


def test_active_worker_lock_and_unknown_boot_cannot_release(legacy, monkeypatch):
    store, life, req, _, _ = legacy
    with (store.directory / 'worker.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Rejected, match='worker_owned'):
            recovery.recover(life, req, apply=True)
    monkeypatch.setattr(recovery, 'observe_host', lambda: (_ for _ in ()).throw(Rejected('legacy_host_unavailable', 'unknown')))
    with pytest.raises(Rejected, match='legacy_host_unavailable'):
        recovery.recover(life, req, apply=True)
    assert life.snapshot('Q1')['resources']


@pytest.mark.parametrize('stage', ['legacy_before_recheck', 'legacy_before_commit'])
def test_interruptions_are_atomic_and_retry_is_safe(legacy, stage):
    store, life, req, _, _ = legacy
    before = store.path.read_bytes()
    def interrupt(current):
        if current == stage:
            raise RuntimeError('SIMULATED interruption')
    life._fault = interrupt
    with pytest.raises(RuntimeError, match='interruption'):
        recovery.recover(life, req, apply=True)
    assert store.path.read_bytes() == before
    life._fault = lambda _: None
    assert recovery.recover(life, req, apply=True)['execution'] == 'retired'


def test_content_and_evidence_are_rechecked_at_commit(legacy):
    store, life, req, evidence, _ = legacy
    from pathlib import Path
    def change(stage):
        if stage == 'legacy_before_recheck':
            Path(evidence['review']['report']['path']).write_text('different assessment')
    life._fault = change
    with pytest.raises(Rejected, match='legacy_evidence_changed'):
        recovery.recover(life, req, apply=True)
    assert life.snapshot('Q1')['resources']


def test_retired_work_cannot_reopen_or_dispatch_on_late_events(legacy, tmp_path):
    store, life, req, _, _ = legacy
    aid = req['attempt_id']
    recovery.recover(life, req, apply=True)
    result = life.complete('late-result', aid, implementation())
    assert result['disposition'] == 'historical'
    life.unknown(aid, 'late unknown')
    life.request_owned_stop(aid, 'late', 'late stop')
    life.cancellation_result('Q1', [])
    for command in ('resume', 'pause', 'cancel'):
        with pytest.raises(Rejected, match='terminal_execution'):
            life.control('late-' + command, 'Q1', command)
    with pytest.raises(Rejected, match='terminal_execution'):
        life.admit('late-admit', 'Q1')
    with pytest.raises(Rejected, match='terminal_execution'):
        life.reassess('late-reassess', 'Q1')
    fake = FakeRuntime(tmp_path / 'unused-runtime.sqlite3', [])
    with Worker(life, fake) as worker:
        worker.tick()
    with fake._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
    assert life.snapshot('Q1')['execution'] == 'retired'
    assert not life.snapshot('Q1')['resources']


def test_private_evidence_and_strict_cli_shape(legacy):
    store, life, req, evidence, _ = legacy
    from pathlib import Path
    path = Path(req['evidence_path'])
    path.chmod(0o644)
    with pytest.raises(Rejected, match='legacy_evidence_permissions'):
        recovery.recover(life, req, apply=True)
    path.chmod(0o600)
    request_file = path.with_name('request.json')
    request_file.write_text(json.dumps({**req, 'approve': True}))
    output = subprocess.run([sys.executable, '-m', 'agentic_tools.aos.cli', 'recover-legacy-cancellation',
                             '--state-dir', str(store.directory), '--worker-writable-root', str(store.worker_writable_roots[0]),
                             '--request', str(request_file)], capture_output=True, text=True)
    assert output.returncode == 2 and 'invalid_request' in output.stderr


@pytest.mark.parametrize('condition', ['clear', 'open-resource', 'executable-resource', 'lsof-error', 'ps-error'])
def test_native_quiescence_is_narrow_and_fails_closed(monkeypatch, condition):
    uid = os.getuid()
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        if args[0] == '/usr/sbin/lsof':
            output = b'p99999\0n/unrelated/work\0\n'
            if condition == 'open-resource':
                output = b'p99999\0n/held/source/a.py\0\n'
            return subprocess.CompletedProcess(args, 1 if condition == 'lsof-error' else 0,
                                               output, b'incomplete' if condition == 'lsof-error' else b'')
        output = f'99999 {uid} /unrelated/editor\n'.encode()
        if condition == 'executable-resource':
            output = f'99999 {uid} /held/source/executable\n'.encode()
        return subprocess.CompletedProcess(args, 2 if condition == 'ps-error' else 0, output, b'')
    monkeypatch.setattr(recovery.subprocess, 'run', command)
    if condition == 'clear':
        assert recovery.observe_quiescence([{'resource_id': '/held/source'}], 'attempt', 'assignment')['open_resources'] == []
    else:
        with pytest.raises(Rejected, match='legacy_resource_busy' if condition.endswith('resource') else 'legacy_quiescence_unavailable'):
            recovery.observe_quiescence([{'resource_id': '/held/source'}], 'attempt', 'assignment')
    assert all('command=' not in arg for command in commands for arg in command)


def test_current_resource_access_is_rechecked_before_commit(legacy, monkeypatch):
    store, life, req, _, _ = legacy
    def change(stage):
        if stage == 'legacy_before_recheck':
            monkeypatch.setattr(recovery, 'observe_quiescence', lambda *args: (_ for _ in ()).throw(
                Rejected('legacy_resource_busy', 'new resource access')))
    life._fault = change
    before = store.path.read_bytes()
    with pytest.raises(Rejected, match='legacy_resource_busy'):
        recovery.recover(life, req, apply=True)
    assert store.path.read_bytes() == before
