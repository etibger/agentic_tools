"""Connected disposable-store checks; none qualifies an actual desktop restart."""
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from agentic_tools.aos.cli import main
from agentic_tools.aos.projection import SnapshotCollector, collect, dashboard
from agentic_tools.aos.store import Rejected, Store, digest
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import setup, runtime, implementation, assessment, count_attempts, run_to_end
from test_aos_codex_runtime import prepared, claim


def test_cli_and_dashboard_share_exact_source_without_writes(tmp_path, capsys):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    before = store.path.read_bytes()
    readonly = Store(store.directory, (tmp_path / 'fixture',), read_only=True)
    snapshot = collect(readonly, 'Q1')
    rendered = dashboard(snapshot)
    assert rendered['source_revision'] == snapshot['source_revision']
    assert rendered['source_digest'] == snapshot['source_digest'] == digest(snapshot['source'])
    assert rendered['stage_status'] == 'Concept approved · Execution held'
    assert not [d for d in rendered['decisions'] if d['status'] == 'pending']
    assert rendered['decisions'][0]['answer'] == 'Approve this exact synthetic package'
    assert rendered['usage']['total_tokens'] is rendered['usage']['actual_billed'] is None
    assert main(['status', '--state-dir', str(store.directory), '--worker-writable-root', str(tmp_path / 'fixture'),
                 '--surface', 'service', 'Q1']) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli['source_digest'] == rendered['source_digest']
    assert store.path.read_bytes() == before
    script = '''import json,sys
from pathlib import Path
from agentic_tools.aos.store import Store
from agentic_tools.aos.projection import collect,dashboard
print(json.dumps(dashboard(collect(Store(Path(sys.argv[1]),(Path(sys.argv[2]),),read_only=True),'Q1'))))
'''
    result = subprocess.run([sys.executable, '-c', script, str(store.directory), str(tmp_path / 'fixture')],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout)['source_digest'] == snapshot['source_digest']


def test_collector_failure_retains_observation_and_approval_with_technical_blocker(tmp_path, monkeypatch):
    store, _, _ = setup(tmp_path, admitted=False)
    collector = SnapshotCollector(Store(store.directory, (tmp_path / 'fixture',), read_only=True), 'Q1')
    good = collector.collect()
    monkeypatch.setattr(collector.store, 'read', lambda _: (_ for _ in ()).throw(sqlite3.OperationalError('private path redacted')))
    failed = collector.collect()
    assert failed['observed_at'] == good['observed_at']
    assert failed['source_digest'] == good['source_digest']
    assert failed['collector']['health'] == 'failed'
    assert 'private path' not in failed['collector']['failure']
    view = dashboard(failed)
    assert not [d for d in view['decisions'] if d['status'] == 'pending']
    assert any(b['title'] == 'Source collection failed' for b in view['blockers'])


def test_view_mismatch_and_stale_source_do_not_manufacture_a_decision(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    lifecycle.reconcile('Q1')
    with store._transaction() as db:
        db.execute("UPDATE work SET updated_at='2000-01-01T00:00:00+00:00'")
        db.execute("UPDATE projection SET source_digest='sha256:changed'")
    snapshot = collect(store, 'Q1')
    assert snapshot['freshness']['source_stale'] is True
    view = dashboard(snapshot)
    assert view['stage_status'] == 'Concept approved · Execution held'
    assert any('projection differs' in b['title'] for b in view['blockers'])
    changed = deepcopy(snapshot)
    changed['source']['concept_decision'] = 'pending'
    with pytest.raises(Rejected, match='snapshot_mismatch'):
        dashboard(changed)


def test_regressed_source_is_a_collector_failure_not_a_new_current_view(tmp_path, monkeypatch):
    store, lifecycle, _ = setup(tmp_path)
    old = store.read('Q1')
    lifecycle.control('pause', 'Q1', 'pause')
    collector = SnapshotCollector(store, 'Q1')
    newer = collector.collect()
    monkeypatch.setattr(store, 'read', lambda _: old)
    retained = collector.collect()
    assert retained['source_digest'] == newer['source_digest']
    assert retained['collector']['health'] == 'failed'
    assert dashboard(retained)['stage_status'].endswith('Execution paused')


def test_current_assignments_distinguish_future_review_from_history(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    before = dashboard(collect(store, 'Q1'))
    assert len(before['agents']) == 1 and 'writer' in before['agents'][0]['role']
    assert 'assessor' not in str(before['phase_flow']['nodes'])
    run_to_end(lifecycle, fake)
    after = dashboard(collect(store, 'Q1'))
    assert after['agents'] == []
    assert len(after['phase_graph']['nodes']) == 2
    assert [n['title'] for n in after['phase_flow']['nodes']] == ['Concept decision', 'Current disposition']
    assert after['stage_status'] == 'Concept approved · Execution delivered'


@pytest.mark.parametrize('boundary', ['after_intent_before_launch', 'after_launch_before_ack'])
def test_lost_launch_event_restart_projects_one_attempt_and_no_double_handoff(tmp_path, boundary):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        worker._fault = lambda stage: (_ for _ in ()).throw(RuntimeError(boundary)) if stage == boundary else None
        with pytest.raises(RuntimeError):
            worker.tick()
    held_view = dashboard(collect(store, 'Q1'))
    assert held_view['agents'][0]['status'] == 'sent'
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    run_to_end(reopened, fake)
    first = reopened.snapshot('Q1')['assignments'][0]
    with store._connection() as db:
        receipt = db.execute('SELECT receipt_id FROM completion WHERE assignment_id=?', (first['assignment_id'],)).fetchone()[0]
    # Identical receipt replay returns its original durable disposition.
    assert reopened.complete(receipt, first['assignment_id'], implementation())['disposition'] == 'assessment_required'
    assert count_attempts(fake) == 2
    assert dashboard(collect(store, 'Q1'))['stage_status'].endswith('Execution delivered')


def test_pause_resume_cancel_and_ownership_connected_to_snapshot(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        with pytest.raises(Rejected, match='already owns'):
            with Worker(lifecycle, fake):
                pass
        lifecycle.control('pause', 'Q1', 'pause')
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    with Worker(reopened, fake) as worker:
        worker.tick()
        assert dashboard(collect(store, 'Q1'))['stage_status'].endswith('Execution paused')
        reopened.control('resume', 'Q1', 'resume')
        reopened.control('cancel', 'Q1', 'cancel')
        worker.tick()
    view = dashboard(collect(store, 'Q1'))
    assert view['stage_status'] == 'Concept approved · Execution cancelled'
    assert view['agents'] == [] and collect(store, 'Q1')['source']['lifecycle']['resources'] == []
    assert count_attempts(fake) == 1


def test_latest_per_attempt_usage_includes_failed_work_but_never_unknown_billing(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    assignment = lifecycle.assignments()[0]
    for tokens in (1, 1, 2):
        # These are already-normalized per-attempt totals, as supplied by the
        # unchanged adapter. A new snapshot must not sum previous snapshots.
        lifecycle.observe_runtime(assignment, {'simulated': False, 'attempt_id': assignment['attempt_id'],
             'state': 'failed', 'model_calls_attempted': 1,
             'usage': {'tokens': {'totalTokens': tokens, 'inputTokens': 0}, 'partial': True}})
    view = dashboard(collect(store, 'Q1'))
    assert view['usage']['total_tokens'] == 2
    assert view['usage']['input_tokens'] == 0
    assert view['usage']['output_tokens'] is None
    assert view['usage']['partial'] is True
    assert view['usage']['actual_billed'] is None


def test_adapter_duplicate_reset_and_projection_keep_one_high_water_total(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    def event(total):
        return {'method': 'thread/tokenUsage/updated', 'params': {'threadId': record['thread_id'],
            'turnId': record['turn_id'], 'tokenUsage': {'total': {'inputTokens': total, 'outputTokens': 2, 'totalTokens': total + 2}}}}
    p.backend.clients[0].events = [event(100), event(100), event(4)]
    p.runtime._events(record, p.backend.clients[0])
    p.lifecycle.observe_runtime(assignment, p.runtime._observation(record, 'active'))
    view = dashboard(collect(p.store, 'Q2'))
    assert view['usage']['total_tokens'] == 102
    assert view['usage']['partial'] is True
    assert view['usage']['observed_at'] == record['usage']['at']
    assert view['usage']['actual_billed'] is None


def test_missing_review_remains_held_in_both_views(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()},
                             {'kind': 'assessment', 'result': assessment(initial_reports={})}])
    run_to_end(lifecycle, fake)
    snapshot = collect(store, 'Q1')
    assert snapshot['source']['execution'] == 'held'
    assert dashboard(snapshot)['stage_status'] == 'Concept approved · Execution held'
    assert snapshot['source']['lifecycle']['delivery'] is None


def test_service_snapshot_runs_through_existing_dashboard_renderer(tmp_path):
    store, _, _ = setup(tmp_path, admitted=False)
    fixture = tmp_path / 'dashboard.json'
    fixture.write_text(json.dumps(dashboard(collect(store, 'Q1'))))
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'check_aos_dashboard.mjs'
    completed = subprocess.run(['node', str(script), str(fixture)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'Service/dashboard integration passed' in completed.stdout
