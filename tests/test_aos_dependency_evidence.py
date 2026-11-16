"""Scoped prerequisite proof reaches the real turn; no live/model execution."""
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from agentic_tools.aos import plan_runtime
from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_lifecycle import implementation
from test_aos_reassessment_prompt import case, ready, retained
from test_aos_startup_retry import claim


def final_review(p, phase='native-repair'):
    """Simulate qualified earlier work while retaining real evidence bytes."""
    checkpoint = ready(p, phase)
    p.checkpoint = checkpoint
    value = retained(p, checkpoint, 'validated')
    p.accepted = copy.deepcopy(value)
    assert p.lifecycle.complete('checkpoint', checkpoint['assignment_id'], value)['disposition'] == 'milestone_passed'
    for index in range(2, len(p.plan['tasks']), 2):
        writer = claim(p.lifecycle)
        # A legitimate packaging change leaves accepted utility bytes intact.
        (p.source / 'README.txt').write_text(f'SIMULATED packaged instructions {index}\n')
        assert p.lifecycle.complete('writer-' + str(index), writer['assignment_id'],
            implementation(candidate(p.source, recipe=p.recipe)))['disposition'] == 'assessment_required'
        assessment = claim(p.lifecycle)
        if index == len(p.plan['tasks']) - 2:
            return assessment
        assert p.lifecycle.complete('assessment-' + str(index), assessment['assignment_id'],
            retained(p, assessment, 'validated'))['disposition'] == 'milestone_passed'


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}, {'count': 6, 'packaging': True}], indirect=True)
@pytest.mark.parametrize('phase', ['initial', 'static-repair', 'native-repair'])
def test_actual_turn_supplies_only_qualified_ancestors_and_preserves_review_mode(case, phase):
    p = case
    assignment = final_review(p, phase)
    before = copy.deepcopy(p.recipe)
    assert p.runtime.start(assignment)['state'] == 'active'
    record = p.runtime._get(assignment['attempt_id'])
    text = next(params['input'][0]['text'] for method, params in p.backend.calls if method == 'turn/start')
    assert 'Assessment review phase: initial-assessment' in text
    assert 'reassessment_evidence' not in record
    bound = record['dependency_evidence']
    raw = Path(bound['path']).read_bytes()
    evidence = json.loads(raw)
    assert evidence == record['dependency_feedback']
    assert bound['sha256'] == hashlib.sha256(raw).hexdigest()
    assert bound['path'] in text and bound['sha256'] in text
    assert Path(bound['path']).parent == Path(record['run_outputs'])
    assert 'Historical passes do not replace your independent review' in text
    assert evidence['assignment_id'] == assignment['assignment_id']
    assert evidence['task_id'] == 'task-' + str(len(p.plan['tasks']) - 1)
    assert evidence['plan_sha256'] == digest(p.plan)
    ancestors = evidence['ancestors']
    assert [a['task_id'] for a in ancestors] == [t['id'] for t in p.plan['tasks'][:-1]]
    assert all(a['assignment_id'] != assignment['assignment_id'] for a in ancestors)
    assert all(a['proof_sha256'] == digest(a['proof']) for a in ancestors)
    accepted = ancestors[1]
    assert accepted['candidate'] == p.accepted['candidate'] != evidence['candidate']
    assert accepted['candidate']['inventory']['fixture.py'] == evidence['candidate']['inventory']['fixture.py']
    assert accepted['candidate']['inventory']['README.txt'] != evidence['candidate']['inventory']['README.txt']
    assert ancestors[2]['input_candidate'] == accepted['candidate']
    observed = accepted['origin_milestone_observation']
    assert observed['detail_sha256'] == accepted['proof_sha256']
    assert observed['event_id'] > 0
    accepted_at = datetime.fromisoformat(observed['created_at'])
    packaging = ancestors[2]['assignment_observation']
    assert datetime.fromisoformat(accepted['assignment_observation']['terminal_at']) <= accepted_at
    assert accepted_at <= datetime.fromisoformat(packaging['created_at']) <= datetime.fromisoformat(packaging['sent_at'])
    assert datetime.fromisoformat(accepted['completion_observation']['received_at']) <= datetime.fromisoformat(packaging['sent_at'])
    with p.store._connection() as db:
        assert dict(db.execute('SELECT event_id,created_at FROM lifecycle_event WHERE event_id=?',
            (observed['event_id'],)).fetchone()) == {k: observed[k] for k in ('event_id', 'created_at')}
    assert evidence['assignment_observation']['sent_at'] == assignment['sent_at']
    assert accepted['review']['report']['verdict'] == 'pass'
    assert accepted['checks'][0]['passed'] == 1 and accepted['checks'][0]['skipped'] == 0
    assert accepted['review_context']['mode'] == ('initial-assessment' if phase == 'initial' else 'post-repair-reassessment')
    if phase != 'initial':
        assert ancestors[0]['candidate'] != p.failed['candidate']
        assert accepted['review_context']['rejected_candidate'] == p.failed['candidate']['identity']
    if phase == 'native-repair':
        history = accepted['review_context']['native_failure']
        assert history['origin']['receipt_id'] == 'failure'
        assert 'SIMULATED native assertion' in history['checks'][0]['output_excerpt']
    assert p.recipe == before
    assert p.runtime._scoped_recipe(p.recipe, record['run_outputs'])['outputs'] == record['run_outputs']
    assert p.runtime._native_for_attempt(assignment['attempt_id']) == []


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
@pytest.mark.parametrize('fault', ['review', 'native', 'failure-native', 'receipt', 'proof', 'task', 'contract', 'package', 'input', 'ordering'])
def test_missing_or_changed_prerequisite_rejects_before_reservation_and_model(case, monkeypatch, fault):
    p = case
    assignment = final_review(p)
    if fault == 'review':
        p.reports[-1].write_text('{}')
    elif fault in ('native', 'failure-native'):
        p.raws[-1 if fault == 'native' else 0].write_text('tampered retained output')
    else:
        with p.store._transaction() as db:
            if fault == 'receipt':
                db.execute("UPDATE completion SET fingerprint='forged' WHERE receipt_id='checkpoint'")
            elif fault == 'proof':
                db.execute("DELETE FROM lifecycle_event WHERE kind='di_milestone_passed' AND json_extract(detail,'$.milestone_id')='gate-1'")
            elif fault == 'package':
                db.execute("UPDATE concept SET package='{}' WHERE work_id='PROMPT'")
            elif fault == 'ordering':
                db.execute("UPDATE assignment SET sent_at='2000-01-01T00:00:00+00:00' WHERE assignment_id=?",
                    (p.lifecycle.assignments()[-2]['assignment_id'],))
            else:
                prior = p.checkpoint if fault in ('task', 'contract') else p.lifecycle.assignments()[-2]
                if fault == 'task':
                    scope = json.loads(prior['scope']); scope['di_task_id'] = 'task-3'
                    db.execute('UPDATE assignment SET scope=? WHERE assignment_id=?', (canonical(scope), prior['assignment_id']))
                elif fault == 'contract':
                    db.execute('UPDATE assignment SET contract_revision=99 WHERE assignment_id=?', (prior['assignment_id'],))
                else:
                    db.execute("UPDATE assignment SET candidate='{}' WHERE assignment_id=?", (prior['assignment_id'],))
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve',
        lambda *a, **k: pytest.fail('Invalid prerequisite must not consume capacity'))
    with pytest.raises(Rejected):
        p.runtime.start(assignment)
    assert p.runtime._get(assignment['attempt_id']) is None
    assert p.backend.calls == []


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
@pytest.mark.parametrize('fault', ['copy', 'receipt', 'review', 'native'])
def test_prerequisite_changed_during_startup_prevents_turn_send(case, monkeypatch, fault):
    p = case
    assignment = final_review(p)
    def tamper(stage):
        if stage != 'before_turn_request':
            return
        if fault == 'copy':
            Path(p.runtime._get(assignment['attempt_id'])['dependency_evidence']['path']).write_text('{}')
        elif fault == 'receipt':
            with p.store._transaction() as db:
                db.execute("UPDATE completion SET fingerprint='forged' WHERE receipt_id='checkpoint'")
        elif fault == 'review':
            p.reports[-1].write_text('{}')
        else:
            p.raws[-1].write_text('changed during startup')
    monkeypatch.setattr(p.runtime, '_fault', tamper)
    assert p.runtime.start(assignment)['state'] != 'active'
    assert p.backend.count('thread/start') == 1
    assert p.backend.count('turn/start') == 0


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
@pytest.mark.parametrize('fault', ['copy', 'native'])
def test_reconnect_native_admission_and_cached_success_recheck_prerequisites(case, monkeypatch, fault):
    p = case
    assignment = final_review(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    record = p.runtime._get(assignment['attempt_id'])
    record.update(phase='terminal', model_stop_receipt={'stopped': True, 'effects_reconciled': True,
        'owned_processes': []}, terminal_observation={'state': 'completed',
        'result': {'status': 'completed', 'native_checks': [], 'checks': []}})
    p.runtime._save(record)
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'completed'
    if fault == 'copy':
        Path(record['dependency_evidence']['path']).write_text('{}')
    else:
        p.raws[-1].write_text('changed after turn send')
    with pytest.raises(Rejected):
        p.runtime._verify_dependency_evidence(record)
    monkeypatch.setattr('agentic_tools.aos.reviewed_host.check', lambda *a, **k: pytest.fail('No native admission'))
    with pytest.raises(Rejected):
        p.runtime._check({**p.recipe, '_attempt_id': assignment['attempt_id']},
            p.recipe['checks'][0], Path(record['run_outputs']) / 'check', 'assessor', json.loads(assignment['candidate']))
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
def test_historical_export_does_not_relax_live_phase_or_current_authority(case):
    p = case
    assignment = final_review(p)
    with pytest.raises(Rejected) as error:
        p.lifecycle.assessment_review_context(p.checkpoint['assignment_id'])
    assert error.value.code == 'assessment_context'
    with p.store._transaction() as db:
        db.execute("UPDATE lifecycle SET control='pause' WHERE work_id='PROMPT'")
    with pytest.raises(Rejected, match='current authorized task'):
        p.lifecycle.dependency_review_context(assignment['assignment_id'])


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
def test_later_writer_gets_accepted_baseline_evidence_without_future_scope(case):
    p = case
    checkpoint = ready(p)
    assert p.lifecycle.complete('checkpoint', checkpoint['assignment_id'],
        retained(p, checkpoint, 'validated'))['disposition'] == 'milestone_passed'
    writer = claim(p.lifecycle)
    assert p.runtime.start(writer)['state'] == 'active'
    evidence = p.runtime._get(writer['attempt_id'])['dependency_feedback']
    assert evidence['task_id'] == 'task-2'
    assert [a['task_id'] for a in evidence['ancestors']] == ['task-0', 'task-1']
    assert evidence['candidate'] == evidence['ancestors'][-1]['candidate']


@pytest.mark.parametrize('case', [{'count': 6, 'packaging': True, 'repairs': 2}], indirect=True)
def test_each_accepted_correction_uses_its_own_historical_cycle(case):
    p = case
    checkpoint = ready(p)
    assert p.lifecycle.complete('checkpoint', checkpoint['assignment_id'],
        retained(p, checkpoint, 'validated'))['disposition'] == 'milestone_passed'
    writer = claim(p.lifecycle)
    (p.source / 'README.txt').write_text('SIMULATED second-stage defect\n')
    p.lifecycle.complete('writer-2', writer['assignment_id'], implementation(candidate(p.source, recipe=p.recipe)))
    failed = claim(p.lifecycle)
    assert p.lifecycle.complete('failure-2', failed['assignment_id'],
        retained(p, failed, 'native_failed'))['disposition'] == 'triage_required'
    triage = claim(p.lifecycle)
    assert p.lifecycle.complete('triage-2', triage['assignment_id'],
        retained(p, triage, 'triage'))['disposition'] == 'repair'
    repair = claim(p.lifecycle)
    (p.source / 'fixture.py').write_text('answer = 4  # SIMULATED second correction\n')
    p.lifecycle.complete('repair-2', repair['assignment_id'], implementation(candidate(p.source, recipe=p.recipe)))
    accepted = claim(p.lifecycle)
    assert p.lifecycle.complete('accepted-2', accepted['assignment_id'],
        retained(p, accepted, 'validated'))['disposition'] == 'milestone_passed'
    final_writer = claim(p.lifecycle)
    (p.source / 'README.txt').write_text('SIMULATED final packaging\n')
    p.lifecycle.complete('writer-4', final_writer['assignment_id'], implementation(candidate(p.source, recipe=p.recipe)))
    final = claim(p.lifecycle)
    assert p.runtime.start(final)['state'] == 'active'
    evidence = p.runtime._get(final['attempt_id'])['dependency_feedback']
    histories = [a['review_context']['native_failure']['origin'] for a in evidence['ancestors'] if 'review_context' in a]
    assert [(h['receipt_id'], h['cycle']) for h in histories] == [('failure', 0), ('failure-2', 1)]


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
def test_last_native_result_cannot_accept_changed_prerequisite(case, monkeypatch):
    p = case
    assignment = final_review(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    clean = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
             'partial_mutations': {'status': 'none', 'changes': []}}
    def reconcile(record, *a, **k):
        record['model_stop_receipt'] = copy.deepcopy(clean)
        return copy.deepcopy(clean)
    def last_result(record, *a):
        Path(record['dependency_evidence']['path']).write_text('{}')
        return {'status': 'completed', **clean}
    monkeypatch.setattr(p.runtime, '_events', lambda *_: None)
    monkeypatch.setattr(p.runtime, '_read_turn', lambda *_, **__: {'status': 'completed'})
    monkeypatch.setattr(p.runtime, '_reconcile', reconcile)
    monkeypatch.setattr(p.runtime, '_result', last_result)
    monkeypatch.setattr(p.runtime, '_finish_stop', lambda *a: copy.deepcopy(clean))
    observation = p.runtime.poll(assignment['attempt_id'])
    assert observation['state'] == 'failed' and observation['result']['status'] == 'failed'
    assert 'prerequisite evidence copy changed' in observation['result']['error']


@pytest.mark.parametrize('case', [{'count': 4, 'packaging': True}], indirect=True)
def test_oversized_required_evidence_holds_before_reservation(case, monkeypatch):
    p = case
    assignment = final_review(p)
    read = p.runtime._read_source_review
    def oversized(*a):
        review, ref = read(*a)
        if review['verdict'] == 'pass':
            review['limits'] = 'SIMULATED large review ' * 14000
        return review, ref
    monkeypatch.setattr(p.runtime, '_read_source_review', oversized)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve',
        lambda *a, **k: pytest.fail('Oversized proof must not consume capacity'))
    with pytest.raises(Rejected, match='256 KiB'):
        p.runtime.start(assignment)
    assert p.backend.calls == []


def test_export_preserves_verified_inherited_proofs_and_origin_candidate_edges(tmp_path):
    from test_aos_internal_milestones import serial_setup, scripted_four, milestone_events
    from test_aos_lifecycle import CANDIDATE, run_to_end, runtime
    store, lifecycle, current = serial_setup(tmp_path)
    frozen = {**CANDIDATE, 'identity': 'sha256:SIMULATED-second-stage', 'source_identity': 'sha256:SIMULATED-source'}
    run_to_end(lifecycle, runtime(tmp_path, scripted_four(second_candidate=frozen, final_polls=1000)))
    original_proofs = milestone_events(store)
    package = store.read('Q1')['concepts'][-1]['package']
    successor = store.present('continued', 'Q1', store.read('Q1')['revision'], 'Q1-DI', 2,
        'SIMULATED technical continuation export seam', package['acceptance'], package['team_envelope'],
        package['authority'], execution_envelope=package['execution_envelope'], di_plan=package['di_plan'])
    # This private export seam grants no current launch/approval authority.
    with store._transaction() as db:
        plan_runtime.inherit_frozen_candidate_in(lifecycle, db, 'Q1', current['digest'], frozen)
        scope = plan_runtime.bind_scope_in(db, 'Q1', 'assessment', {})
        assignment = dict(lifecycle.assignments()[-1], assignment_id='SIMULATED-successor',
            concept_digest=successor['digest'], scope=canonical(scope))
        lifecycle._touch(db, 'Q1', 'di_assignment_planned',
            {'assignment_id': assignment['assignment_id'], 'scope_digest': digest(scope)})
        evidence = plan_runtime.dependency_evidence_in(db, assignment)
    assert len(evidence) == 3
    assert evidence[-1]['proof']['concept_digest'] == successor['digest']
    assert evidence[-1]['proof']['inherited_proof_digest'] == digest(original_proofs[-1])
    assert all(e['assignment']['concept_digest'] == current['digest'] for e in evidence)
    assert evidence[1]['result']['candidate'] == CANDIDATE != evidence[2]['result']['candidate']
    assert evidence[2]['assignment']['candidate'] == canonical(CANDIDATE)
