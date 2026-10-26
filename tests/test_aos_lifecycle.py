"""Q1 simulations qualify state semantics, not a human host or live agents."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime

import pytest

from agentic_tools.aos import __version__
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical
from agentic_tools.aos.worker import FakeRuntime, Worker
from test_aos_store import TEAM


CANDIDATE = {'identity': 'sha256:frozen-scoped-patch', 'source_identity': 'sha256:base-plus-patch',
             'inventory': ['fixture.py', 'test_fixture.py']}


def contract(current, workflow='bug-fix', **overrides):
    return {'revision': 1, 'workflow': workflow, 'concept_revision': 1, 'concept_digest': current['digest'],
            'baseline': 'synthetic-existing-product-v1', 'behavior_delta': 'fix count / add counter label',
            'required_checks': ['fixture'], 'compatibility_checks': ['consumer'] if workflow == 'feature-change' else [],
            'writer': 'writer', 'assessor': 'assessor', 'receiving_owner': 'fixture-owner',
            'authority': 'Synthetic fixture only; no commits, install or publication', 'resources': ['fixture-source'],
            'prerequisites': {'q0': True, 'isolated_fixture': True}, 'qualified_route': 'SIMULATED:qualified-fixture',
            'max_assignments': 8, 'max_repairs': 2, 'max_seconds': 60, 'model': 'gpt-6.1-sol', 'effort': 'high', **overrides}


def approve(store, current, work_id='Q1', *, operation='approve'):
    cap = store.host_capability('test-host', 'SIMULATED:Q1')
    challenge = store.begin_approval(operation, work_id, store.read(work_id)['revision'], current['digest'], capability=cap)
    if challenge['completed']:
        return challenge['result']
    return store.finish_approval(challenge['challenge_id'], 'accept', True, capability=cap,
                                 exact_answer='Approve this exact synthetic package', source_channel='simulated-host',
                                 interaction_reference='simulated-interaction-123')


def setup(tmp_path, workflow='bug-fix', *, approved=True, admitted=True, work_id='Q1', store=None, **overrides):
    store = store or Store(tmp_path / 'state', (tmp_path / 'fixture',))
    store.introduce('intro-' + work_id, work_id, 'Synthetic common lifecycle', ['fixture/'], TEAM)
    approved_bounds = contract({'digest': 'not-self-referential'}, workflow)
    approved_bounds.pop('concept_digest')
    approved_bounds.pop('concept_revision')
    execution_team = [{**TEAM[0], 'name': person, 'role': role, 'scope': 'Synthetic fixture; max 8 assignments, 60 seconds each, two repairs'}
                      for person, role in [('writer', 'Implementer'), ('assessor', 'Independent assessor')]]
    current = store.present('present-' + work_id, work_id, 1, 'Q1-DI', 1, 'Synthetic fixture', ['Synthetic acceptance'],
                            execution_team, approved_bounds['authority'], execution_envelope=approved_bounds)
    lifecycle = Lifecycle(store)
    lifecycle.register('register-' + work_id, work_id, contract(current, workflow, **overrides))
    if approved:
        approve(store, current, work_id, operation='approve-' + work_id)
    if admitted:
        lifecycle.admit('admit-' + work_id, work_id)
    return store, lifecycle, current


def implementation(candidate=CANDIDATE):
    return {'status': 'completed', 'executor': 'writer', 'candidate': candidate, 'output': 'SIMULATED:author-check', 'owned_processes': [], 'effects_reconciled': True, 'partial_mutations': 'frozen synthetic patch'}


def assessment(workflow='bug-fix', candidate=CANDIDATE, **overrides):
    names = ['fixture'] + (['consumer'] if workflow == 'feature-change' else [])
    return {'status': 'completed', 'executor': 'assessor', 'candidate': candidate,
            'observed_source_identity': candidate['source_identity'],
            'owned_processes': [], 'effects_reconciled': True, 'partial_mutations': 'none; read-only assessment',
            'initial_reports': {'execution': 'SIMULATED:execution-initial', 'review': 'SIMULATED:source-review-initial', 'independent': True},
            'checks': [{'name': name, 'command': 'python -m pytest test_fixture.py', 'environment': 'synthetic isolated fixture',
                        'executor': 'assessor', 'selected': 1, 'result': 'pass', 'output': 'SIMULATED:' + name,
                        'candidate': candidate, 'scope': 'fixture and direct consumer', 'limits': 'simulated only'} for name in names], **overrides}


def runtime(tmp_path, plan=None):
    return FakeRuntime(tmp_path / 'fake.sqlite3', plan or [
        {'kind': 'implementation', 'result': implementation()}, {'kind': 'assessment', 'result': assessment()}])


def count_attempts(fake):
    with sqlite3.connect(fake.path) as db:
        return db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0]


def run_to_end(lifecycle, fake):
    with Worker(lifecycle, fake) as worker:
        for _ in range(5):
            worker.tick()


@pytest.mark.parametrize('workflow', ['bug-fix', 'feature-change'])
def test_both_workflows_share_durable_assessment_delivery(tmp_path, workflow):
    store, lifecycle, _ = setup(tmp_path, workflow)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()},
                              {'kind': 'assessment', 'result': assessment(workflow)}])
    run_to_end(lifecycle, fake)
    source = lifecycle.snapshot('Q1')
    assert source['concept_decision'] == 'approved'
    assert source['execution'] == 'delivered'
    assert source['delivery']['accepted'] is True
    assert source['delivery']['committed'] is source['delivery']['installed'] is source['delivery']['released'] is False
    assert source['delivery']['receiving_owner'] == 'fixture-owner'
    assert source['resources'] == []
    assert [a['kind'] for a in source['assignments']] == ['implementation', 'assessment']
    assert count_attempts(fake) == 2
    assert source['projection']['health'] == 'current'
    assert source['usage']['cost'] is None and source['usage']['model_calls'] == 0
    assert source['live_dispatch_available'] is False
    first, second = source['assignments']
    assert first['sent_at'] and first['acknowledged_at'] and first['observed_active_at'] and first['terminal_at']
    assert (datetime.fromisoformat(second['sent_at']) - datetime.fromisoformat(second['eligible_at'])).total_seconds() < 5
    readback = Store(store.directory, (tmp_path / 'fixture',), read_only=True).read('Q1')
    assert readback['lifecycle']['source_revision'] == source['source_revision']
    assert readback['execution'] == 'delivered'
    assert __version__ == '0.2.5'


def test_approval_and_execution_hold_are_separate_and_fresh_process_readback(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    before = lifecycle.reconcile('Q1')
    assert before['concept_decision'] == 'approved' and before['execution'] == 'held'
    script = '''import json,sys
from pathlib import Path
from agentic_tools.aos.store import Store
s=Store(Path(sys.argv[1]),(Path(sys.argv[2]),),read_only=True)
print(json.dumps(s.read('Q1')))
'''
    child = subprocess.run([sys.executable, '-c', script, str(store.directory), str(tmp_path / 'fixture')],
                           check=True, capture_output=True, text=True, timeout=10)
    reopened = json.loads(child.stdout)
    decision = reopened['decisions'][0]
    assert decision['exact_answer'] == 'Approve this exact synthetic package'
    assert decision['interaction_reference'] == 'simulated-interaction-123'
    assert decision['source_channel'] == 'simulated-host'
    assert decision['requested_at'] and decision['created_at']
    assert json.loads(decision['package_snapshot'])['revision'] == 1
    assert reopened['concept_decision'] == 'approved' and reopened['execution'] == 'held'
    assert reopened['pending_decisions'] == []


def test_unapproved_contract_cannot_dispatch_then_same_approval_suffices(tmp_path):
    store, lifecycle, current = setup(tmp_path, approved=False)
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert lifecycle.assignments() == []
    approve(store, current, operation='real-fixture-answer')
    assert lifecycle.admit('retry-admission', 'Q1')['execution'] == 'eligible'
    assert len(store.read('Q1')['decisions']) == 1


@pytest.mark.parametrize('missing', ['compatibility_checks', 'baseline', 'assessor'])
def test_incomplete_feature_contract_is_rejected(tmp_path, missing):
    store, lifecycle, current = setup(tmp_path, admitted=False)
    broken = contract(current, 'feature-change')
    broken[missing] = [] if missing == 'compatibility_checks' else ''
    with pytest.raises(Rejected):
        lifecycle.register('bad-feature', 'Q1', broken)


def test_distinct_assessor_and_bounded_repairs_required(tmp_path):
    with pytest.raises(Rejected, match='distinct assessor'):
        setup(tmp_path, assessor='writer')
    with pytest.raises(Rejected, match='at most two repairs'):
        setup(tmp_path / 'other', max_repairs=3)


def test_revised_concept_preserves_old_approval_and_holds_execution(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    store.present('revised', 'Q1', store.read('Q1')['revision'], 'Q1-DI', 2, 'Different scope', ['New acceptance'], TEAM, 'Different authority')
    result = lifecycle.admit('new-revision-admit', 'Q1')
    assert result['execution'] == 'held'
    record = store.read('Q1')
    assert record['concept_decision'] == 'pending'
    assert record['decisions'][0]['outcome'] == 'approved'
    assert lifecycle.assignments() == []


def test_only_one_project_and_worker_own_execution(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    _, _, _ = setup(tmp_path, work_id='SECOND', store=store)
    assert lifecycle.snapshot('SECOND')['execution'] == 'held'
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as first:
        with pytest.raises(Rejected, match='already owns'):
            with Worker(lifecycle, fake):
                pass
        first_epoch = first.epoch
    with Worker(lifecycle, fake) as second:
        assert second.epoch == first_epoch + 1
        with pytest.raises(Rejected, match='current worker epoch'):
            lifecycle.claim(lifecycle.assignments()[0]['assignment_id'], first_epoch)


@pytest.mark.parametrize('boundary', ['after_intent_before_launch', 'after_launch_before_ack'])
def test_interrupt_launch_boundaries_reconcile_one_external_attempt(tmp_path, boundary):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    def fail(stage):
        if stage == boundary:
            raise RuntimeError('simulated worker interruption')
    with Worker(lifecycle, fake) as worker:
        worker._fault = fail
        with pytest.raises(RuntimeError):
            worker.tick()
    assert lifecycle.assignments()[0]['state'] == 'sent'
    run_to_end(lifecycle, fake)
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assert count_attempts(fake) == 2  # one writer, one successor assessor


def test_unknown_lost_ack_never_relaunches_or_releases_resources(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        worker._fault = lambda stage: (_ for _ in ()).throw(RuntimeError('lost ack')) if stage == 'after_launch_before_ack' else None
        with pytest.raises(RuntimeError):
            worker.tick()
    fake.lookup = lambda attempt: {'state': 'unknown', 'attempt_id': attempt}
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        worker.tick()
    snapshot = lifecycle.snapshot('Q1')
    assert snapshot['execution'] == 'held'
    assert snapshot['resources']
    assert count_attempts(fake) == 1
    assert any(b['code'] == 'unknown_effects' for b in snapshot['technical_blockers'])


def test_completion_and_outbox_commit_atomically_then_deduplicate_successor(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 100}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    first = lifecycle.assignments()[0]
    def before_enqueue(stage):
        if stage == 'before_outbox_enqueue':
            raise RuntimeError('interrupted before successor enqueue')
    lifecycle._fault = before_enqueue
    with pytest.raises(RuntimeError):
        lifecycle.complete('terminal-writer', first['assignment_id'], implementation())
    assert lifecycle.assignments()[0]['state'] == 'observed_active'
    assert len(lifecycle.assignments()) == 1
    with lifecycle.store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM completion').fetchone()[0] == 0
    lifecycle._fault = lambda _: None
    assert lifecycle.complete('terminal-writer', first['assignment_id'], implementation())['disposition'] == 'assessment_required'
    assert lifecycle.complete('terminal-writer', first['assignment_id'], implementation())['duplicate'] is True
    assert lifecycle.complete('another-receipt-same-turn', first['assignment_id'], implementation())['disposition'] == 'historical'
    assert len(lifecycle.assignments()) == 2
    with pytest.raises(Rejected, match='different factual output'):
        lifecycle.complete('terminal-writer', first['assignment_id'], {'different': True})


def test_crash_after_completion_commit_preserves_one_successor(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 100}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    first = lifecycle.assignments()[0]
    lifecycle._fault = lambda stage: (_ for _ in ()).throw(RuntimeError('after commit')) if stage == 'after_completion_commit' else None
    with pytest.raises(RuntimeError):
        lifecycle.complete('terminal', first['assignment_id'], implementation())
    reopened = Lifecycle(Store(lifecycle.store.directory, (tmp_path / 'fixture',)))
    assert reopened.complete('terminal', first['assignment_id'], implementation())['duplicate'] is True
    assert len(reopened.assignments()) == 2


@pytest.mark.parametrize('fault', ['wrong_candidate', 'wrong_source', 'author_assessor', 'missing_reports', 'failed', 'blocked', 'unrun', 'zero_tests', 'missing_output', 'missing_check'])
def test_bad_assessment_holds_then_independent_reassessment_delivers(tmp_path, fault):
    store, lifecycle, _ = setup(tmp_path)
    bad = assessment()
    if fault == 'wrong_candidate':
        bad['candidate'] = {**CANDIDATE, 'identity': 'wrong'}
    elif fault == 'wrong_source':
        bad['observed_source_identity'] = 'drifted-source'
    elif fault == 'author_assessor':
        bad['executor'] = 'writer'
    elif fault == 'missing_reports':
        bad['initial_reports'] = {}
    elif fault in ('failed', 'blocked', 'unrun'):
        bad['checks'][0]['result'] = fault
    elif fault == 'zero_tests':
        bad['checks'][0]['selected'] = 0
    elif fault == 'missing_output':
        bad['checks'][0]['output'] = ''
    else:
        bad['checks'] = []
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()},
                              {'kind': 'assessment', 'result': bad}, {'kind': 'assessment', 'result': assessment()}])
    run_to_end(lifecycle, fake)
    held = lifecycle.snapshot('Q1')
    assert held['execution'] == 'held' and held['concept_decision'] == 'approved'
    assert len(held['assignments']) == 2 and held['resources'][0]['state'] == 'frozen'
    lifecycle.reassess('corrected-evidence', 'Q1')
    run_to_end(lifecycle, fake)
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assert len(store.read('Q1')['decisions']) == 1


def test_failed_check_confirmed_finding_repairs_then_reassesses(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'fixture still fails'})
    bad['checks'][0]['result'] = 'fail'
    fixed_candidate = {**CANDIDATE, 'identity': 'sha256:repaired-patch', 'source_identity': 'sha256:repaired-source'}
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()}, {'kind': 'assessment', 'result': bad},
                              {'kind': 'repair', 'result': implementation(fixed_candidate)},
                              {'kind': 'assessment', 'result': assessment(candidate=fixed_candidate)}])
    run_to_end(lifecycle, fake)
    snap = lifecycle.snapshot('Q1')
    assert snap['execution'] == 'delivered'
    assert [a['kind'] for a in snap['assignments']] == ['implementation', 'assessment', 'repair', 'assessment']
    assert snap['delivery']['candidate'] == fixed_candidate


def test_two_repair_cycles_are_the_limit(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'confirmed defect'})
    bad['checks'][0]['result'] = 'fail'
    plan = [{'kind': 'implementation', 'result': implementation()}, {'kind': 'assessment', 'result': bad}]
    for _ in range(2):
        plan.extend([{'kind': 'repair', 'result': implementation()}, {'kind': 'assessment', 'result': bad}])
    fake = runtime(tmp_path, plan)
    run_to_end(lifecycle, fake)
    assert count_attempts(fake) == 6
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert any(b['code'] == 'repair_limit' for b in lifecycle.snapshot('Q1')['technical_blockers'])


def test_pause_survives_restart_and_completion_does_not_dispatch_until_resume(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 1},
                              {'kind': 'assessment', 'result': assessment()}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        lifecycle.control('pause', 'Q1', 'pause')
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    with Worker(reopened, fake) as worker:
        worker.tick()
        worker.tick()
    assert reopened.snapshot('Q1')['execution'] == 'paused'
    assert count_attempts(fake) == 1
    reopened.control('resume', 'Q1', 'resume')
    run_to_end(reopened, fake)
    assert reopened.snapshot('Q1')['execution'] == 'delivered'


@pytest.mark.parametrize('unknown', [False, True])
def test_cancel_fences_racing_completion_and_requires_owned_effect_reconciliation(tmp_path, unknown):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 100, 'unknown_effects': unknown}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        assignment = lifecycle.assignments()[0]
        lifecycle.control('cancel', 'Q1', 'cancel')
        late = lifecycle.complete('racing-terminal', assignment['assignment_id'], implementation())
        assert late['disposition'] == 'historical'
        worker.tick()
    snap = lifecycle.snapshot('Q1')
    assert snap['concept_decision'] == 'approved'
    assert len(snap['assignments']) == 1
    if unknown:
        assert snap['control'] == 'blocked_unknown_effects' and snap['resources']
        assert snap['execution'] == 'held'
    else:
        assert snap['execution'] == 'cancelled' and snap['resources'] == []


def test_projection_mismatch_is_technical_and_never_a_new_approval(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    lifecycle.reconcile('Q1')
    with store._transaction() as db:
        db.execute("UPDATE projection SET snapshot='{}' WHERE work_id='Q1'")
    held = lifecycle.reconcile('Q1')
    assert held['execution'] == 'held' and held['concept_decision'] == 'approved'
    assert held['pending_human_decisions'] == []
    assert any(b['code'] == 'projection_mismatch' for b in held['technical_blockers'])
    assert lifecycle.admit('mismatch-admit', 'Q1')['execution'] == 'held'
    lifecycle.repair_projection('Q1')
    assert lifecycle.admit('repaired-admit', 'Q1')['execution'] == 'eligible'
    assert len(store.read('Q1')['decisions']) == 1


def test_assignment_limit_is_not_a_spend_claim(tmp_path):
    _, lifecycle, _ = setup(tmp_path, max_assignments=1)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()}])
    run_to_end(lifecycle, fake)
    snap = lifecycle.snapshot('Q1')
    assert len(snap['assignments']) == 1 and snap['execution'] == 'held'
    assert any(b['code'] == 'assignment_limit' for b in snap['technical_blockers'])
    assert snap['usage']['cost'] is None


def test_foreground_cli_worker_runs_in_separate_process_without_chat_dispatch(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    plan = tmp_path / 'fake-plan.json'
    plan.write_text(json.dumps([{'kind': 'implementation', 'result': implementation()},
                                {'kind': 'assessment', 'result': assessment()}]))
    child = subprocess.Popen([sys.executable, '-m', 'agentic_tools.aos.cli', 'worker',
                              '--state-dir', str(store.directory), '--worker-writable-root', str(tmp_path / 'fixture'),
                              '--fake-plan', str(plan), '--max-cycles', '3', '--interval', '0.05'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output, errors = child.communicate(timeout=10)
    assert child.returncode == 0, output + errors
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'delivered'
    assert source['worker']['state'] == 'stopped'
    assert [a['executor'] for a in source['assignments']] == ['writer', 'assessor']
    assert all(a['runtime_model'] == 'gpt-6.1-sol' for a in source['assignments'])
    assert json.loads(source['assignments'][0]['scope'])['source_access'] == 'write'
    assert json.loads(source['assignments'][1]['scope'])['source_access'] == 'read-only'


def test_pause_after_intent_before_launch_does_not_start_during_recovery(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        def pause(stage):
            if stage == 'after_intent_before_launch':
                lifecycle.control('pause-at-intent', 'Q1', 'pause')
        worker._fault = pause
        worker.tick()
    assert count_attempts(fake) == 0
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert count_attempts(fake) == 0
    assert lifecycle.snapshot('Q1')['execution'] == 'paused'
    lifecycle.control('resume-intent', 'Q1', 'resume')
    run_to_end(lifecycle, fake)
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'


def test_cancel_after_intent_before_launch_never_starts_runtime(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        worker._fault = lambda stage: lifecycle.control('cancel-intent', 'Q1', 'cancel') if stage == 'after_intent_before_launch' else None
        worker.tick()
        worker.tick()
    assert count_attempts(fake) == 0
    assert lifecycle.snapshot('Q1')['execution'] == 'cancelled'


def test_runtime_configuration_mismatch_holds_with_visible_actual_config(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'model': 'unapproved-model', 'result': implementation()}])
    run_to_end(lifecycle, fake)
    source = lifecycle.snapshot('Q1')
    assert source['execution'] == 'held'
    assert len(source['assignments']) == 1
    assert source['assignments'][0]['runtime_model'] == 'unapproved-model'
    assert any(b['code'] == 'configuration_mismatch' for b in source['technical_blockers'])


def test_unknown_terminal_effects_cannot_release_ownership(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    result = implementation()
    result['effects_reconciled'] = False
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': result, 'unknown_effects': True}])
    run_to_end(lifecycle, fake)
    assert lifecycle.assignments()[0]['state'] == 'blocked_unknown_effects'
    lifecycle.control('cancel-unknown-terminal', 'Q1', 'cancel')
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    assert lifecycle.snapshot('Q1')['control'] == 'blocked_unknown_effects'
    assert lifecycle.snapshot('Q1')['resources']


def test_stale_completion_records_history_without_successor(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation(), 'polls': 100}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
    first = lifecycle.assignments()[0]
    store.present('new-concept', 'Q1', store.read('Q1')['revision'], 'Q1-DI', 2, 'Revised requirement', ['New acceptance'], TEAM, 'Fixture only')
    assert lifecycle.complete('late-previous-concept', first['assignment_id'], implementation())['disposition'] == 'historical'
    assert len(lifecycle.assignments()) == 1
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert any(b['code'] == 'stale_assignment' for b in lifecycle.snapshot('Q1')['technical_blockers'])


def test_stale_worker_heartbeat_visible_in_status(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake):
        with store._transaction() as db:
            db.execute("UPDATE worker_status SET heartbeat='2000-01-01T00:00:00+00:00'")
        assert store.read('Q1')['lifecycle']['worker']['health'] == 'stale'


def test_detached_process_requirements_are_rejected(tmp_path):
    with pytest.raises(Rejected, match='detached processes remain unqualified'):
        setup(tmp_path, requires_detached_processes=True)


def test_process_death_releases_worker_lock_and_restart_reconciles_saved_attempt(tmp_path):
    import time
    store, lifecycle, _ = setup(tmp_path)
    plan = tmp_path / 'restart-plan.json'
    plan.write_text(json.dumps([{'kind': 'implementation', 'result': implementation(), 'polls': 100},
                                {'kind': 'assessment', 'result': assessment()}]))
    args = [sys.executable, '-m', 'agentic_tools.aos.cli', 'worker', '--state-dir', str(store.directory),
            '--worker-writable-root', str(tmp_path / 'fixture'), '--fake-plan', str(plan)]
    child = subprocess.Popen([*args, '--max-cycles', '1000', '--interval', '0.05'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if lifecycle.assignments()[0]['state'] == 'observed_active':
                break
            if child.poll() is not None:
                raise AssertionError(child.communicate())
            time.sleep(0.02)
        assert lifecycle.assignments()[0]['state'] == 'observed_active'
        old_epoch = lifecycle.snapshot('Q1')['worker']['epoch']
        child.terminate()  # Only this test-owned foreground worker process.
        child.communicate(timeout=5)
        assert lifecycle.snapshot('Q1')['resources']
        restarted = subprocess.run([*args, '--max-cycles', '110', '--interval', '0.005'],
                                   capture_output=True, text=True, timeout=10)
        assert restarted.returncode == 0, restarted.stderr
        source = lifecycle.snapshot('Q1')
        assert source['execution'] == 'delivered' and source['worker']['epoch'] == old_epoch + 1
        assert source['concept_decision'] == 'approved' and len(store.read('Q1')['decisions']) == 1
        with sqlite3.connect(store.directory / 'fake-runtime.sqlite3') as db:
            assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 2
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=5)
