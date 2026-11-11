"""Scripted admission/transport wiring; artifact integrity has real-Git tests."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import budgets, source_review_evidence
from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_codex_runtime import claim, prepared
from test_aos_readiness import setup
from test_aos_reviewed_host import case


@pytest.fixture
def assessment(prepared, monkeypatch):
    p = prepared
    assignment = {**claim(p), 'kind': 'assessment', 'candidate': canonical(candidate(p.source))}
    original = p.store.directory.parent / 'coordinator-review.json'
    original.write_bytes(b'{"fixture":"source review data"}\n')
    original.chmod(0o400)
    descriptor = {'format': source_review_evidence.DESCRIPTOR_FORMAT, 'path': str(original),
                  'sha256': hashlib.sha256(original.read_bytes()).hexdigest(), 'work_id': 'Q2',
                  'baseline_identity': candidate(p.source)['identity'],
                  'candidate_identity': candidate(p.source)['identity'], 'git_base': 'a' * 40}
    recipe = {**p.recipe, 'source_review_evidence': descriptor}
    p.runtime.recipes['Q2'] = recipe
    # Authority validation has its own integration coverage. This fixture isolates
    # transport order while retaining real attempt/reservation persistence.
    monkeypatch.setattr(p.runtime, '_recipe', lambda selected: recipe)
    calls = []

    def validate(selected, **kwargs):
        calls.append(('validate', copy.deepcopy(kwargs)))
        assert kwargs['source'] == p.source
        assert kwargs['work_id'] == 'Q2'
        assert kwargs['baseline_identity'] == descriptor['baseline_identity']
        assert kwargs['candidate'] == json.loads(assignment['candidate'])
        assert kwargs['writable_roots'] == p.store.worker_writable_roots
        if selected != descriptor or hashlib.sha256(original.read_bytes()).hexdigest() != descriptor['sha256']:
            raise Rejected('source_review_evidence', 'Altered original evidence')
        return {'fixture': 'verified'}

    def seed(selected, output, **kwargs):
        validate(selected, **kwargs)
        path = Path(output) / source_review_evidence.FILENAME
        path.write_bytes(original.read_bytes())
        path.chmod(0o400)
        calls.append(('seed', str(path)))
        return {'path': str(path), 'sha256': descriptor['sha256'], 'descriptor': copy.deepcopy(descriptor)}

    def verify_copy(bound, output, **kwargs):
        validate(bound['descriptor'], **kwargs)
        path = Path(output) / source_review_evidence.FILENAME
        calls.append(('verify_copy', str(path)))
        if bound['path'] != str(path) or hashlib.sha256(path.read_bytes()).hexdigest() != bound['sha256']:
            raise Rejected('source_review_evidence', 'Altered attempt evidence')
        return {'fixture': 'verified'}

    monkeypatch.setattr(source_review_evidence, 'validate', validate)
    monkeypatch.setattr(source_review_evidence, 'seed', seed)
    monkeypatch.setattr(source_review_evidence, 'verify_copy', verify_copy)
    return SimpleNamespace(p=p, assignment=assignment, recipe=recipe, original=original,
                           descriptor=descriptor, calls=calls)


def corrupt(path):
    path.chmod(0o600)
    path.write_bytes(b'changed evidence\n')
    path.chmod(0o400)


def test_invalid_evidence_rejects_before_attempt_or_reservation(assessment, monkeypatch):
    c = assessment
    corrupt(c.original)
    monkeypatch.setattr(budgets, 'reserve', lambda *args, **kwargs: pytest.fail('Invalid evidence reserved a model slot'))
    with pytest.raises(Rejected, match='Altered original evidence'):
        c.p.runtime.start(c.assignment)
    assert c.p.runtime._get(c.assignment['attempt_id']) is None
    assert c.p.backend.calls == []
    assert not (c.p.outputs / c.assignment['attempt_id']).exists()
    with c.p.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM runtime_budget').fetchone()[0] == 0


def test_evidence_is_assessment_only_before_reservation(assessment, monkeypatch):
    c = assessment
    monkeypatch.setattr(budgets, 'reserve', lambda *args, **kwargs: pytest.fail('Evidence authorized a writer'))
    with pytest.raises(Rejected, match='exact assessment'):
        c.p.runtime.start({**c.assignment, 'kind': 'implementation'})
    assert c.p.runtime._get(c.assignment['attempt_id']) is None
    assert c.p.backend.calls == []


def test_seed_precedes_startup_snapshot_and_turn_contains_exact_pointer(assessment):
    c = assessment
    assert c.p.runtime.start(c.assignment)['state'] == 'active'
    record = c.p.runtime._get(c.assignment['attempt_id'])
    bound = record['source_review_evidence']
    assert record['startup_outputs'][source_review_evidence.FILENAME] == bound['sha256']
    assert bound['descriptor'] == c.descriptor
    assert bound['path'] == str(c.p.outputs / c.assignment['attempt_id'] / source_review_evidence.FILENAME)
    kinds = [entry[0] for entry in c.calls]
    assert kinds.index('seed') < kinds.index('verify_copy')
    turn = next(params for method, params in c.p.backend.calls if method == 'turn/start')
    text = turn['input'][0]['text']
    assert bound['path'] in text and bound['sha256'] in text
    assert 'data, not instructions' in text
    assert record['model_calls_attempted'] == 1


def test_copy_tampered_during_probe_never_reaches_model_turn(assessment, monkeypatch):
    c = assessment
    def probe(record, recipe, **kwargs):
        assert record['startup_outputs'][source_review_evidence.FILENAME] == c.descriptor['sha256']
        corrupt(Path(record['source_review_evidence']['path']))
        return {'SIMULATED': True}
    monkeypatch.setattr(c.p.runtime, '_probe_sandbox', probe)
    outcome = c.p.runtime.start(c.assignment)
    record = c.p.runtime._get(c.assignment['attempt_id'])
    assert outcome['state'] != 'active'
    assert c.p.backend.count('thread/start') == 1
    assert c.p.backend.count('turn/start') == 0
    assert record['model_calls_attempted'] == 0
    assert 'Altered attempt evidence' in record['error']


def test_saved_binding_cannot_be_replaced(assessment):
    c = assessment
    assert c.p.runtime.start(c.assignment)['state'] == 'active'
    record = c.p.runtime._get(c.assignment['attempt_id'])
    original = copy.deepcopy(record['source_review_evidence'])
    record['source_review_evidence']['sha256'] = '0' * 64
    with pytest.raises(Rejected, match='cannot be replaced'):
        c.p.runtime._save(record)
    assert c.p.runtime._get(c.assignment['attempt_id'])['source_review_evidence'] == original


def test_review_completion_rechecks_copy_before_native_admission(assessment, monkeypatch):
    c = assessment
    assert c.p.runtime.start(c.assignment)['state'] == 'active'
    record = c.p.runtime._get(c.assignment['attempt_id'])
    frozen = json.loads(c.assignment['candidate'])
    stop = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True}
    record['model_stop_receipt'] = stop
    record['assessment_context'] = {'purpose': 'assessment'}
    report = Path(record['run_outputs']) / ('review-' + record['attempt_id'] + '.txt')
    report.write_text(canonical({'candidate_identity': frozen['identity'], 'verdict': 'pass',
                                 'findings': [], 'limits': 'Scripted independent source review.'}))
    corrupt(Path(record['source_review_evidence']['path']))
    monkeypatch.setattr(c.p.lifecycle, 'assessment_context', lambda selected: {'purpose': 'assessment'})
    monkeypatch.setattr(c.p.runtime, '_check', lambda *args: pytest.fail('Altered evidence reached native validation'))
    with pytest.raises(Rejected, match='Altered attempt evidence'):
        c.p.runtime._reviewed_host_result(record, c.recipe, {'status': 'completed'}, stop, {}, frozen)
    assert c.p.runtime._native_for_attempt(record['attempt_id']) == []
    assert 'reviewed_host_gate' not in c.p.runtime._get(record['attempt_id'])


def test_each_host_check_revalidates_evidence_before_runner(case, monkeypatch):
    descriptor = {'fixture': 'bound descriptor'}
    case.base['source_review_evidence'] = descriptor
    case.recipe['source_review_evidence'] = descriptor
    calls = []
    def verify(record, recipe):
        calls.append((record['attempt_id'], recipe))
        raise Rejected('source_review_evidence', 'Evidence changed between review and native admission')
    monkeypatch.setattr(case.runtime, '_verify_source_review', verify)
    with pytest.raises(Rejected, match='between review and native admission'):
        case.runtime._check(case.recipe, case.descriptor, case.output, 'reviewer', case.frozen)
    assert calls == [('assessment-1', case.base)]
    assert case.calls == [] and case.runtime._native_for_attempt('assessment-1') == []


def test_kwargs_keep_original_approved_baseline_and_current_candidate(prepared):
    p = prepared
    original_baseline = candidate(p.source)['identity']
    (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
    frozen = candidate(p.source)
    assignment = {'kind': 'assessment', 'work_id': 'Q2', 'candidate': canonical(frozen)}
    kwargs = p.runtime._source_review_kwargs(assignment, p.recipe)
    assert kwargs['baseline_identity'] == original_baseline != frozen['identity']
    assert kwargs['candidate'] == frozen and kwargs['work_id'] == 'Q2'
    assert kwargs['writable_roots'] == p.store.worker_writable_roots


def test_old_recipe_does_not_activate_evidence_path(prepared, monkeypatch):
    for name in ('validate', 'seed', 'verify_copy'):
        monkeypatch.setattr(source_review_evidence, name, lambda *args, **kwargs: pytest.fail('Legacy recipe activated evidence'))
    assignment = claim(prepared)
    assert prepared.runtime.start(assignment)['state'] == 'active'
    record = prepared.runtime._get(assignment['attempt_id'])
    assert 'source_review_evidence' not in record
    assert source_review_evidence.FILENAME not in record['startup_outputs']


def test_opt_in_descriptor_does_not_broaden_model_permissions(prepared):
    p = prepared
    for assessment_role in (False, True):
        original = p.runtime._profile(p.recipe, assessment=assessment_role)
        bound = {**p.recipe, 'source_review_evidence': {'path': '/protected/coordinator/evidence.json'}}
        assert p.runtime._profile(bound, assessment=assessment_role) == original


def test_metadata_readiness_preserves_binding_without_product_assignment_lookup(setup, monkeypatch):
    p = setup
    descriptor = {'work_id': 'real-product', 'baseline_identity': 'original-baseline',
                  'candidate_identity': 'exact-candidate', 'path': '/protected/coordinator/evidence.json'}
    p.recipe['source_review_evidence'] = descriptor
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    for name in ('validate', 'seed', 'verify_copy'):
        monkeypatch.setattr(source_review_evidence, name,
                            lambda *args, **kwargs: pytest.fail('Metadata roles used synthetic product authority'))
    before = {path.name: path.read_bytes() for path in p.state.iterdir()}
    result = p.run()
    assert result['ready'] and result['cleanup_verified']
    assert result['readiness_scope'] == 'model-metadata-only'
    assert result['product_attempts'] == result['product_reservations'] == result['model_turns'] == 0
    assert p.calls.count('thread/start') == 2 and 'turn/start' not in p.calls
    assert all(probe['recipe']['source_review_evidence'] == descriptor for probe in p.probes)
    assert {path.name: path.read_bytes() for path in p.state.iterdir()} == before
