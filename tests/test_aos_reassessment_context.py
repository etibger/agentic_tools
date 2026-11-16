"""Qualified reassessment history is read-only input, never dispatch authority."""
from copy import deepcopy
import json

import pytest

from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_internal_milestones import serial_plan, serial_setup
from test_aos_lifecycle import CANDIDATE, assessment, implementation
import test_aos_reviewed_host_triage as host
from test_aos_startup_retry import claim, complete as complete_startup, failure


def setup(tmp_path, monkeypatch, *, retries=0, serial=True):
    if serial:
        monkeypatch.setattr(host, 'synthetic_plan', lambda bounds: serial_plan(bounds, 2))
    return host.setup(tmp_path, retries=retries)


def retry(lifecycle, assignment, count):
    for _ in range(count):
        observation = failure(lifecycle, assignment)
        assert complete_startup(lifecycle, assignment, observation)['disposition'] == 'startup_retry'
        assignment = claim(lifecycle)
    return assignment


def postrepair(tmp_path, monkeypatch, *, native=False, repair_retries=0, assessor_retries=0,
               serial=True):
    store, lifecycle = setup(tmp_path, monkeypatch, retries=repair_retries + assessor_retries,
                             serial=serial)
    initial = claim(lifecycle)
    initial_result = host.result(lifecycle, initial, 'native_failed' if native else 'review_rejected')
    disposition = host.complete(lifecycle, initial, initial_result, 'initial-rejection')['disposition']
    rejected = initial
    rejection_result = initial_result
    if native:
        assert disposition == 'triage_required'
        rejected = claim(lifecycle)
        rejection_result = host.result(lifecycle, rejected, 'triage')
        assert host.complete(lifecycle, rejected, rejection_result, 'triage-rejection')['disposition'] == 'repair'
    else:
        assert disposition == 'repair'
    repair = retry(lifecycle, claim(lifecycle), repair_retries)
    repaired = {**deepcopy(CANDIDATE), 'identity': 'sha256:repaired-candidate',
                'source_identity': 'sha256:repaired-source'}
    repair_result = implementation(repaired)
    assert lifecycle.complete('repair-result', repair['assignment_id'], repair_result)['disposition'] == 'assessment_required'
    current = retry(lifecycle, claim(lifecycle), assessor_retries)
    return {'store': store, 'lifecycle': lifecycle, 'initial': initial, 'initial_result': initial_result,
            'rejected': rejected, 'rejection_result': rejection_result, 'repair': repair,
            'repair_result': repair_result, 'current': current, 'candidate': repaired}


def retained(store):
    with store._connection() as db:
        return list(db.iterdump())


def rewrite_result(db, assignment, receipt, change):
    value = json.loads(db.execute('SELECT result FROM completion WHERE receipt_id=?', (receipt,)).fetchone()[0])
    change(value)
    db.execute('UPDATE assignment SET result=? WHERE assignment_id=?', (canonical(value), assignment['assignment_id']))
    db.execute('UPDATE completion SET result=?,fingerprint=? WHERE receipt_id=?',
               (canonical(value), digest([assignment['assignment_id'], value]), receipt))


@pytest.mark.parametrize('serial', [False, True])
def test_initial_review_context_is_distinct_from_reassessment_and_read_only(tmp_path, monkeypatch, serial):
    store, lifecycle = setup(tmp_path, monkeypatch, serial=serial)
    current = claim(lifecycle)
    before = retained(store)
    assert Lifecycle(store).assessment_review_context(current['assignment_id']) == {'mode': 'initial-assessment'}
    assert lifecycle.assessment_context(current['assignment_id']) == {'purpose': 'assessment'}
    assert retained(store) == before
    assert len(lifecycle.assignments()) == 2


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('repair_retries,assessor_retries', [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2)])
def test_postrepair_review_contains_only_exact_retained_same_task_history(
        tmp_path, monkeypatch, native, repair_retries, assessor_retries):
    p = postrepair(tmp_path, monkeypatch, native=native, repair_retries=repair_retries,
                   assessor_retries=assessor_retries)
    store, lifecycle, current = p['store'], p['lifecycle'], p['current']
    before = retained(store)
    context = Lifecycle(store).assessment_review_context(current['assignment_id'])
    assert context['mode'] == 'post-repair-reassessment'
    assert context['assignment_id'] == current['assignment_id']
    assert context['candidate'] == p['candidate'] != CANDIDATE
    assert context['task_id'] == 'task-1'
    assert context['repair_assignment']['assignment_id'] == p['repair']['assignment_id']
    assert context['repair_receipt_id'] == 'repair-result'
    assert context['repair_result'] == p['repair_result']
    assert context['rejected_assignment']['assignment_id'] == p['rejected']['assignment_id']
    assert context['rejection_receipt_id'] == ('triage-rejection' if native else 'initial-rejection')
    assert context['rejection_result'] == p['rejection_result']
    current_scope = json.loads(current['scope'])
    original_scope = json.loads(p['initial']['scope'])
    assert {key: value for key, value in original_scope.items() if key != 'di_dependencies'} == {
        key: value for key, value in current_scope.items() if key != 'di_dependencies'}
    assert original_scope['di_dependencies'] != current_scope['di_dependencies']
    assert json.loads(p['repair']['scope'])['di_plan_digest'] == current_scope['di_plan_digest']
    assert json.loads(p['repair']['scope'])['di_task_id'] == 'task-0'
    assert lifecycle.assessment_context(current['assignment_id']) == {'purpose': 'assessment'}
    if native:
        assert context['native_failure']['failure_result'] == p['initial_result']
        origin = context['native_failure']['origin']
        assert origin['receipt_id'] == 'initial-rejection'
        assert origin['assignment_id'] == p['initial']['assignment_id']
        assert origin['result_sha256'] == digest(p['initial_result'])
        assert origin['candidate_identity'] == CANDIDATE['identity']
    else:
        assert 'native_failure' not in context
    assert retained(store) == before
    assert lifecycle.snapshot(host.WORK)['delivery'] is None
    with pytest.raises(Rejected):
        lifecycle.triage_context_for_repair(p['repair']['assignment_id'], p['rejected']['assignment_id'])


def test_native_failure_triage_context_preserves_exact_purpose_without_queuing_repair(tmp_path, monkeypatch):
    store, lifecycle = setup(tmp_path, monkeypatch, retries=2)
    initial = claim(lifecycle)
    failed = host.result(lifecycle, initial, 'native_failed')
    assert host.complete(lifecycle, initial, failed, 'native-failure')['disposition'] == 'triage_required'
    current = retry(lifecycle, claim(lifecycle), 2)
    before = retained(store)
    context = lifecycle.assessment_review_context(current['assignment_id'])
    assert context == {'mode': 'native-failure-triage', 'assignment_id': current['assignment_id'],
                       'candidate': CANDIDATE, 'task_id': 'task-1',
                       'origin': lifecycle.assessment_context(current['assignment_id'])['origin'],
                       'failure_result': failed}
    assert context['origin']['assignment_id'] == initial['assignment_id']
    assert context['origin']['receipt_id'] == 'native-failure'
    assert retained(store) == before
    assert [a['kind'] for a in lifecycle.assignments()] == ['implementation', 'assessment', 'assessment', 'assessment', 'assessment']


@pytest.mark.parametrize('native', [False, True])
def test_legacy_postrepair_history_uses_same_evidence_without_inventing_task_id(tmp_path, monkeypatch, native):
    p = postrepair(tmp_path, monkeypatch, native=native, serial=False)
    context = p['lifecycle'].assessment_review_context(p['current']['assignment_id'])
    assert context['mode'] == 'post-repair-reassessment'
    assert context['task_id'] is None
    assert context['repair_result'] == p['repair_result']
    assert context['rejection_result'] == p['rejection_result']
    assert ('native_failure' in context) is native


def test_manual_reassessment_is_neither_initial_review_nor_invented_repair(tmp_path, monkeypatch):
    store, lifecycle = setup(tmp_path, monkeypatch)
    initial = claim(lifecycle)
    inconclusive = host.result(lifecycle, initial, 'review_rejected', confirmed=False)
    assert host.complete(lifecycle, initial, inconclusive, 'inconclusive-review')['disposition'] == 'held'
    lifecycle.reassess('manual-reassessment', host.WORK)
    current = claim(lifecycle)
    before = retained(store)
    assert lifecycle.assessment_review_context(current['assignment_id']) == {'mode': 'assessment'}
    assert retained(store) == before
    assert [a['kind'] for a in lifecycle.assignments()] == ['implementation', 'assessment', 'assessment']


@pytest.mark.parametrize('fault', [
    'missing_repair_receipt', 'repair_fingerprint', 'rejection_fingerprint', 'repair_candidate',
    'rejection_candidate', 'repair_executor', 'rejection_executor', 'current_candidate',
    'current_owner', 'current_task', 'repair_task', 'rejected_task', 'plan_digest',
    'contract_revision', 'stale_current', 'unconfirmed_finding', 'unqualified_review',
])
def test_changed_or_stale_corrective_history_is_rejected_without_new_assignments(tmp_path, monkeypatch, fault):
    p = postrepair(tmp_path, monkeypatch)
    store, lifecycle = p['store'], p['lifecycle']
    with store._transaction() as db:
        if fault == 'missing_repair_receipt':
            db.execute("DELETE FROM completion WHERE receipt_id='repair-result'")
        elif fault in ('repair_fingerprint', 'rejection_fingerprint'):
            receipt = 'repair-result' if fault == 'repair_fingerprint' else 'initial-rejection'
            db.execute('UPDATE completion SET fingerprint=? WHERE receipt_id=?', ('changed', receipt))
        elif fault in ('repair_candidate', 'rejection_candidate'):
            repair = fault == 'repair_candidate'
            rewrite_result(db, p['repair'] if repair else p['rejected'],
                           'repair-result' if repair else 'initial-rejection',
                           lambda value: value['candidate'].update(identity='sha256:foreign-candidate'))
        elif fault in ('repair_executor', 'rejection_executor'):
            repair = fault == 'repair_executor'
            rewrite_result(db, p['repair'] if repair else p['rejected'],
                           'repair-result' if repair else 'initial-rejection',
                           lambda value: value.update(executor='foreign-owner'))
        elif fault == 'current_candidate':
            db.execute('UPDATE assignment SET candidate=? WHERE assignment_id=?',
                       (canonical(CANDIDATE), p['current']['assignment_id']))
        elif fault == 'current_owner':
            db.execute("UPDATE assignment SET executor='writer' WHERE assignment_id=?", (p['current']['assignment_id'],))
        elif fault in ('current_task', 'repair_task', 'rejected_task', 'plan_digest'):
            key = 'repair' if fault == 'repair_task' else 'rejected' if fault == 'rejected_task' else 'current'
            scope = json.loads(p[key]['scope'])
            scope['di_plan_digest' if fault == 'plan_digest' else 'di_task_id'] = 'foreign'
            db.execute('UPDATE assignment SET scope=? WHERE assignment_id=?', (canonical(scope), p[key]['assignment_id']))
        elif fault == 'contract_revision':
            db.execute('UPDATE assignment SET contract_revision=9 WHERE assignment_id=?', (p['repair']['assignment_id'],))
        elif fault == 'stale_current':
            db.execute("UPDATE assignment SET state='completed' WHERE assignment_id=?", (p['current']['assignment_id'],))
        elif fault == 'unconfirmed_finding':
            rewrite_result(db, p['rejected'], 'initial-rejection', lambda value: value['finding'].update(confirmed=False))
        elif fault == 'unqualified_review':
            rewrite_result(db, p['rejected'], 'initial-rejection', lambda value: value['reviewed_host_validation'].update(review_verdict='pass'))
    before = retained(store)
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context(p['current']['assignment_id'])
    assert retained(store) == before
    assert len(lifecycle.assignments()) == 4


@pytest.mark.parametrize('fault', ['native_result', 'accepted_origin', 'failure_fingerprint', 'cycle'])
def test_native_reassessment_rejects_changed_original_failure_or_triage_origin(tmp_path, monkeypatch, fault):
    p = postrepair(tmp_path, monkeypatch, native=True)
    store, lifecycle = p['store'], p['lifecycle']
    with store._transaction() as db:
        if fault == 'native_result':
            rewrite_result(db, p['initial'], 'initial-rejection', lambda value: value['checks'][0].update(output_sha256='f' * 64))
        elif fault == 'accepted_origin':
            rewrite_result(db, p['rejected'], 'triage-rejection',
                           lambda value: value['reviewed_host_validation']['triage_origin'].update(result_sha256='changed'))
        elif fault == 'failure_fingerprint':
            db.execute("UPDATE completion SET fingerprint='changed' WHERE receipt_id='initial-rejection'")
        elif fault == 'cycle':
            db.execute('UPDATE lifecycle SET cycle=0 WHERE work_id=?', (host.WORK,))
    before = retained(store)
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context(p['current']['assignment_id'])
    assert retained(store) == before
    assert len(lifecycle.assignments()) == 5


@pytest.mark.parametrize('fault', ['missing_retry_event', 'current_retry_fingerprint', 'repair_retry_fingerprint',
                                 'current_retry_observation', 'repair_retry_scope'])
def test_corrupt_startup_retry_history_cannot_fall_back_to_initial_review(tmp_path, monkeypatch, fault):
    p = postrepair(tmp_path, monkeypatch, repair_retries=1, assessor_retries=1)
    store, lifecycle = p['store'], p['lifecycle']
    with store._transaction() as db:
        current_receipt = p['current']['predecessor'].removeprefix('startup-retry:')
        repair_receipt = p['repair']['predecessor'].removeprefix('startup-retry:')
        if fault == 'missing_retry_event':
            db.execute("DELETE FROM lifecycle_event WHERE kind='startup_retry'")
        elif fault.endswith('_fingerprint'):
            db.execute("UPDATE completion SET fingerprint='changed' WHERE receipt_id=?",
                       (current_receipt if fault.startswith('current') else repair_receipt,))
        elif fault == 'current_retry_observation':
            db.execute("DELETE FROM lifecycle_event WHERE kind='startup_observation_received'")
        elif fault == 'repair_retry_scope':
            prior = db.execute('SELECT assignment_id FROM completion WHERE receipt_id=?', (repair_receipt,)).fetchone()[0]
            db.execute("UPDATE assignment SET scope='{}' WHERE assignment_id=?", (prior,))
    before = retained(store)
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context(p['current']['assignment_id'])
    assert retained(store) == before


@pytest.mark.parametrize('changed', ['current', 'repair', 'rejected'])
def test_identical_candidate_and_owner_cannot_substitute_another_approved_stage(tmp_path, changed):
    store, lifecycle, _ = serial_setup(tmp_path, count=4)
    writer = claim(lifecycle)
    assert lifecycle.complete('writer-result', writer['assignment_id'], implementation())['disposition'] == 'assessment_required'
    rejected = claim(lifecycle)
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'Synthetic current-task defect'})
    bad['checks'][0]['result'] = 'fail'
    assert lifecycle.complete('rejected-result', rejected['assignment_id'], bad)['disposition'] == 'repair'
    repair = claim(lifecycle)
    assert lifecycle.complete('repair-result', repair['assignment_id'], implementation())['disposition'] == 'assessment_required'
    current = claim(lifecycle)
    assert lifecycle.assessment_review_context(current['assignment_id'])['mode'] == 'post-repair-reassessment'
    target = {'current': current, 'repair': repair, 'rejected': rejected}[changed]
    scope = json.loads(target['scope'])
    stage = 2 if changed == 'repair' else 3
    scope.update(di_task_id=f'task-{stage}', di_milestone_id=f'gate-{stage}')
    with store._transaction() as db:
        db.execute('UPDATE assignment SET scope=? WHERE assignment_id=?', (canonical(scope), target['assignment_id']))
    before = retained(store)
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context(current['assignment_id'])
    assert retained(store) == before
    assert len(lifecycle.assignments()) == 4


def test_review_context_rejects_unknown_and_writer_assignments(tmp_path, monkeypatch):
    _, lifecycle = setup(tmp_path, monkeypatch)
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context('missing-assignment')
    with pytest.raises(Rejected):
        lifecycle.assessment_review_context(lifecycle.assignments()[0]['assignment_id'])
