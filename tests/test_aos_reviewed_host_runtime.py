"""Reviewed-host dispatch gates; all model and native execution is simulated."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_checkout_integration import isolated, record, refresh


@pytest.fixture
def assessment(isolated, monkeypatch, request):
    p = isolated
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    p.recipe['native_process_ownership'] = 'host-owned-v1'
    refresh(p)
    p.frozen = candidate(p.source, recipe=p.recipe)
    p.assignment.update(kind='assessment', executor='assessor', candidate=canonical(p.frozen))
    p.context = {'purpose': 'assessment'}
    p.runtime.lifecycle.assessment_context = lambda *_: copy.deepcopy(p.context)
    p.item = record(p)
    p.stop = {'attempt_id': p.item['attempt_id'], 'stopped': True, 'effects_reconciled': True,
              'owned_processes': [], 'partial_mutations': {'status': 'none', 'changes': []}}
    p.item.update(thread_id='SIMULATED-review-thread', turn_id='SIMULATED-review-turn',
                  model_calls_attempted=1, model_stop_receipt=copy.deepcopy(p.stop))
    Path(p.item['run_outputs']).mkdir()
    p.report = Path(p.item['run_outputs']) / ('review-' + p.item['attempt_id'] + '.txt')
    p.review = {'candidate_identity': p.frozen['identity'], 'verdict': 'pass', 'findings': [],
                'limits': 'SIMULATED source review; no real model or native check.'}
    p.report.write_text(json.dumps(p.review))
    if getattr(request, 'param', None) == 'triage':
        retained_native_failure(p)
        p.item['triage_feedback'] = p.runtime._triage_feedback(p.assignment)
        evidence = Path(p.item['run_outputs']) / 'native-failure-evidence.json'
        evidence.write_text(canonical(p.item['triage_feedback']) + '\n')
        p.item['triage_evidence'] = {'path': str(evidence),
            'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}
    p.item['assessment_context'] = {key: copy.deepcopy(p.context[key])
                                    for key in ('purpose', 'origin') if key in p.context}
    p.runtime._save(p.item)
    p.calls, p.rows = [], []
    p.adapters = {'fixture': {'id': 'SIMULATED-adapter', 'sha256': 'SIMULATED-exact-code-binding'}}
    monkeypatch.setattr(p.runtime, '_bindings', lambda *_: copy.deepcopy(p.adapters))
    p.check_result = 'pass'
    monkeypatch.setattr(p.runtime, '_resource_sample', lambda *args, **kwargs: None)
    monkeypatch.setattr(p.runtime, '_native_for_attempt', lambda *_args, **_kwargs: copy.deepcopy(p.rows))
    # An unexpected path must fail this deterministic suite instead of starting
    # a real app-server, permission probe or native process.
    monkeypatch.setattr(p.runtime, '_client', lambda *args, **kwargs: pytest.fail('No model process is permitted'))
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *args, **kwargs: pytest.fail('No host probe is permitted'))
    monkeypatch.setattr(p.runtime, '_check', lambda *args, **kwargs: pytest.fail('Install an explicit simulated check'))
    return p


def write_review(p, **changes):
    p.review.update(changes)
    p.report.write_text(json.dumps(p.review))


def run_result(p, *, status='completed'):
    return p.runtime._result(p.item, p.recipe, {'status': status}, copy.deepcopy(p.stop))


def install_check(p, monkeypatch, *, reviewed=True, mutate=None):
    def check(recipe, descriptor, output, executor, frozen):
        saved = p.runtime._get(p.item['attempt_id'])
        assert frozen == p.frozen
        assert executor == 'assessor'
        if reviewed:
            gate = saved.get('reviewed_host_gate')
            assert isinstance(gate, dict), 'Review approval must be durable before native dispatch'
            assert gate['candidate'] == frozen
            assert gate['review'] == {'path': str(p.report),
                'sha256': hashlib.sha256(p.report.read_bytes()).hexdigest()}
            assert gate['recipe_digest'] == digest(p.recipe)
            assert gate['checks'] == p.recipe['checks']
            assert gate['adapters'] == p.adapters
            assert saved['model_stop_receipt']['stopped'] is True
        else:
            assert not saved.get('reviewed_host_gate')
        p.calls.append(descriptor['name'])
        check_id = 'nc-SIMULATED-' + descriptor['name']
        p.rows.append({'check_id': check_id})
        if mutate:
            mutate()
        return {'name': descriptor['name'], 'command': descriptor['argv'], 'environment': 'SIMULATED',
                'executor': executor, 'candidate': frozen, 'result': p.check_result,
                'selected': 1, 'scope': descriptor['scope'], 'limits': 'No host command executed',
                'output': str(output / 'transcript.txt'), 'native_check_id': check_id,
                'cleanup': {'stopped': True, 'owned_processes': [], 'effects_reconciled': True}}
    monkeypatch.setattr(p.runtime, '_check', check)


def assert_review_phase(result, phase):
    metadata = result['reviewed_host_validation']
    assert metadata['mode'] == 'reviewed-host-v1'
    assert metadata['phase'] == phase
    assert metadata['review_verdict'] in ('pass', 'fail', 'blocked')
    assert metadata['review'] == result['initial_reports']['review']


def test_exact_passing_review_is_persisted_before_first_host_check(assessment, monkeypatch):
    p = assessment
    install_check(p, monkeypatch)
    result = run_result(p)
    assert p.calls == ['fixture']
    assert result['status'] == 'completed'
    assert result['review_findings'] == []
    assert_review_phase(result, 'validated')
    assert result['initial_reports']['execution'] == [result['checks'][0]['output']]


@pytest.mark.parametrize('defect', ['missing', 'empty', 'malformed', 'wrong-candidate', 'wrong-verdict',
                                   'wrong-findings', 'missing-limits', 'symlink'])
def test_invalid_review_never_launches_a_host_check(assessment, monkeypatch, defect):
    p = assessment
    install_check(p, monkeypatch)
    if defect == 'missing':
        p.report.unlink()
    elif defect == 'empty':
        p.report.write_text('')
    elif defect == 'malformed':
        p.report.write_text('{')
    elif defect == 'wrong-candidate':
        write_review(p, candidate_identity='SIMULATED-other-candidate')
    elif defect == 'wrong-verdict':
        write_review(p, verdict='approved')
    elif defect == 'wrong-findings':
        write_review(p, findings={})
    elif defect == 'missing-limits':
        write_review(p, limits='')
    else:
        target = p.report.with_name('unowned-review.txt')
        target.write_text(p.report.read_text())
        p.report.unlink()
        p.report.symlink_to(target)
    with pytest.raises(Rejected):
        run_result(p)
    assert p.calls == []
    assert not p.runtime._get(p.item['attempt_id']).get('reviewed_host_gate')


@pytest.mark.parametrize('verdict,findings', [
    ('fail', []), ('blocked', []),
    ('pass', [{'confirmed': True, 'in_scope': True, 'path': 'code.py',
               'summary': 'Still wrong', 'evidence': 'SIMULATED source counterexample'}]),
])
def test_nonpassing_or_contradictory_review_returns_no_execution_evidence(assessment, monkeypatch, verdict, findings):
    p = assessment
    install_check(p, monkeypatch)
    write_review(p, verdict=verdict, findings=findings)
    result = run_result(p)
    assert result['status'] == 'failed'
    assert result['checks'] == result['initial_reports']['execution'] == p.calls == []
    assert result['review_findings'] == findings
    assert_review_phase(result, 'review_rejected')
    assert not p.runtime._get(p.item['attempt_id']).get('reviewed_host_gate')


def test_confirmed_static_finding_can_request_repair_without_running_tests(assessment, monkeypatch):
    p = assessment
    install_check(p, monkeypatch)
    finding = {'confirmed': True, 'in_scope': True, 'path': 'code.py',
               'summary': 'Counterexample is not handled', 'evidence': 'SIMULATED exact source counterexample'}
    write_review(p, verdict='fail', findings=[finding])
    result = run_result(p)
    assert result['status'] == 'failed'
    assert result['finding']['confirmed'] is True and result['finding']['in_scope'] is True
    assert result['review_findings'] == [finding]
    assert result['checks'] == result['initial_reports']['execution'] == p.calls == []
    assert_review_phase(result, 'review_rejected')


@pytest.mark.parametrize('status', ['interrupted', 'failed', 'inProgress'])
def test_incomplete_model_turn_cannot_authorize_host_checks(assessment, monkeypatch, status):
    p = assessment
    install_check(p, monkeypatch)
    p.report.unlink()  # Interrupted turns need no report to prohibit execution.
    result = run_result(p, status=status)
    assert result['status'] == 'failed'
    assert result['checks'] == result['initial_reports']['execution'] == p.calls == []
    assert_review_phase(result, 'review_rejected')
    assert result['reviewed_host_validation']['review_verdict'] == 'blocked'


@pytest.mark.parametrize('defect', ['missing', 'not-stopped', 'unreconciled', 'live-owner'])
def test_positive_model_cleanup_is_required_before_host_checks(assessment, monkeypatch, defect):
    p = assessment
    install_check(p, monkeypatch)
    if defect == 'missing':
        p.item.pop('model_stop_receipt')
    else:
        key, value = {'not-stopped': ('stopped', False), 'unreconciled': ('effects_reconciled', False),
                      'live-owner': ('owned_processes', [991234])}[defect]
        p.item['model_stop_receipt'][key] = value
    p.runtime._save(p.item)
    with pytest.raises(Rejected):
        run_result(p)
    assert p.calls == []


def test_changed_candidate_cannot_reuse_a_passed_review(assessment, monkeypatch):
    p = assessment
    install_check(p, monkeypatch)
    (p.source / 'code.py').write_text('answer = 999\n')
    with pytest.raises(Rejected):
        run_result(p)
    assert p.calls == []


def test_native_failure_has_explicit_phase_after_valid_review(assessment, monkeypatch):
    p = assessment
    p.check_result = 'fail'
    install_check(p, monkeypatch)
    result = run_result(p)
    assert p.calls == ['fixture'] and result['status'] == 'failed'
    assert_review_phase(result, 'native_failed')
    assert not result.get('finding'), 'A failing command is not a confirmed source diagnosis'


@pytest.mark.parametrize('assessment', ['triage'], indirect=True)
@pytest.mark.parametrize('verdict,findings', [
    ('pass', []), ('blocked', []),
    ('fail', [{'confirmed': True, 'in_scope': True, 'path': 'code.py',
               'summary': 'Confirmed failure cause', 'evidence': 'SIMULATED retained failing check evidence'}]),
])
def test_triage_never_reexecutes_checks_or_claims_validation(assessment, monkeypatch, verdict, findings):
    p = assessment
    install_check(p, monkeypatch)
    write_review(p, verdict=verdict, findings=findings)
    result = run_result(p)
    assert result['status'] == 'failed'
    assert result['checks'] == result['initial_reports']['execution'] == p.calls == []
    assert_review_phase(result, 'triage')
    assert result['reviewed_host_validation']['triage_origin'] == p.context['origin']


def test_default_checkout_mode_keeps_existing_native_before_review_order(assessment, monkeypatch):
    p = assessment
    p.recipe['checkout_policy'].pop('native_validation')
    p.recipe['native_process_ownership'] = 'sandbox-census-v1'
    p.item['recipe_digest'] = digest(p.recipe)
    p.runtime._save(p.item)
    p.report.unlink()
    install_check(p, monkeypatch, reviewed=False)
    with pytest.raises(Rejected, match='review'):
        run_result(p)
    assert p.calls == ['fixture']
    assert not p.runtime._get(p.item['attempt_id']).get('reviewed_host_gate')


def retained_native_failure(p):
    """Construct actual private evidence files, never a native command or owner."""
    original = p.outputs / 'a-prior-native'
    check_output = original / 'fixture'
    check_output.mkdir(parents=True)
    report = original / 'review-a-prior-native.txt'
    report.write_text(json.dumps(p.review))
    descriptor = {'path': str(report), 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()}
    transcript = check_output / 'transcript.txt'
    transcript.write_text('HEAD: assertion failed\n' + ('bounded test data\n' * 4000) + 'TAIL: one failure\n')
    raw = check_output / 'stdout.txt'
    raw.write_text('SIMULATED: one test failed\n')
    check = {'name': 'fixture', 'candidate': copy.deepcopy(p.frozen), 'result': 'fail',
             'kind': 'pytest', 'selected': 1, 'passed': 0, 'failed': 1, 'errors': 0, 'exit_code': 1,
             'output': str(transcript), 'output_sha256': hashlib.sha256(transcript.read_bytes()).hexdigest(),
             'raw_outputs': {str(raw): hashlib.sha256(raw.read_bytes()).hexdigest()},
             'cleanup': {'stopped': True, 'effects_reconciled': True, 'owned_processes': []}}
    failed = {'status': 'failed', 'candidate': copy.deepcopy(p.frozen), 'checks': [check],
              'initial_reports': {'review': descriptor},
              'reviewed_host_validation': {'mode': 'reviewed-host-v1', 'phase': 'native_failed',
                                           'review': descriptor, 'review_verdict': 'pass'}}
    p.context = {'purpose': 'native_failure_triage',
                 'origin': {'attempt_id': original.name, 'result_sha256': digest(failed)},
                 'failure_result': failed}
    return failed, report, transcript, raw


def test_triage_copies_only_bounded_verified_evidence_without_sibling_permissions(assessment):
    p = assessment
    retained_native_failure(p)
    original_recipe = copy.deepcopy(p.recipe)
    feedback = p.runtime._triage_feedback(p.assignment)
    assert p.recipe == original_recipe
    assert feedback['candidate'] == p.frozen['identity']
    assert feedback['origin'] == p.context['origin']
    assert len(canonical(feedback)) <= 32768
    excerpt = feedback['checks'][0]['output_excerpt']
    assert 'HEAD: assertion failed' in excerpt and 'TAIL: one failure' in excerpt
    assert len(excerpt.encode()) < 17000
    assert 'raw_outputs' not in feedback['checks'][0]
    assert p.calls == []


@pytest.mark.parametrize('defect', ['raw-changed', 'review-changed', 'source-changed', 'candidate-changed',
                                   'missing-check', 'uncertain-cleanup', 'sibling-output', 'result-changed'])
def test_triage_rejects_changed_or_unowned_failure_evidence(assessment, defect):
    p = assessment
    failed, report, transcript, raw = retained_native_failure(p)
    if defect == 'raw-changed':
        raw.write_text('tampered after completion\n')
    elif defect == 'review-changed':
        report.write_text(json.dumps({**p.review, 'limits': 'changed after completion'}))
    elif defect == 'source-changed':
        (p.source / 'code.py').write_text('answer = 2\n')
    elif defect == 'candidate-changed':
        failed['candidate'] = {**p.frozen, 'identity': 'SIMULATED-different-candidate'}
    elif defect == 'missing-check':
        failed['checks'] = []
    elif defect == 'uncertain-cleanup':
        failed['checks'][0]['cleanup']['effects_reconciled'] = False
    elif defect == 'sibling-output':
        sibling = p.outputs / 'different-attempt-output.txt'
        sibling.write_text(transcript.read_text())
        failed['checks'][0]['output'] = str(sibling)
    else:
        failed['status'] = 'different-result'
    if defect != 'result-changed':
        # Keep the saved-result identity internally consistent so each case
        # exercises its specific boundary, rather than only a digest mismatch.
        p.context['origin']['result_sha256'] = digest(failed)
    with pytest.raises(Rejected):
        p.runtime._triage_feedback(p.assignment)
    assert p.calls == []


def test_stale_triage_evidence_is_rejected_before_any_new_reservation(assessment, monkeypatch):
    p = assessment
    _, _, _, raw = retained_native_failure(p)
    raw.write_text('tampered before next assignment\n')
    fresh = {**p.assignment, 'attempt_id': 'a-new-triage', 'assignment_id': 'as-new-triage'}
    monkeypatch.setattr(p.runtime, '_recipe', lambda *_: p.recipe)
    monkeypatch.setattr(p.runtime, '_checkout_before_start',
                        lambda *args, **kwargs: pytest.fail('Verify triage evidence before preparing a new attempt'))
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve',
                        lambda *args, **kwargs: pytest.fail('Changed failure evidence must not consume a reservation'))
    with pytest.raises(Rejected):
        p.runtime.start(fresh)
    assert p.runtime._get(fresh['attempt_id']) is None
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0
    assert p.calls == []


_RETAINED_REVIEW_FIELDS = {
    'assessment_context': {'purpose': 'native_failure_triage', 'origin': {'attempt_id': 'a-prior'}},
    'triage_feedback': {'candidate': 'SIMULATED-candidate', 'checks': [{'name': 'fixture', 'result': 'fail'}]},
    'triage_evidence': {'path': 'SIMULATED-owned-evidence', 'sha256': 'SIMULATED-exact-bytes'},
    'reviewed_host_gate': {'candidate': {'identity': 'SIMULATED-candidate'},
                           'review': {'path': 'SIMULATED-review', 'sha256': 'SIMULATED-exact-review'}},
}


@pytest.mark.parametrize('field', list(_RETAINED_REVIEW_FIELDS))
def test_stale_save_preserves_review_evidence_absent_from_old_snapshot(isolated, field):
    p = isolated
    original = record(p)
    writer, stale = copy.deepcopy(original), copy.deepcopy(original)
    writer[field] = copy.deepcopy(_RETAINED_REVIEW_FIELDS[field])
    p.runtime._save(writer)
    assert field not in stale
    stale['usage'] = {'SIMULATED-token-count': 7}
    p.runtime._save(stale)
    saved = p.runtime._get(original['attempt_id'])
    assert saved[field] == writer[field]
    assert saved['usage'] == stale['usage']
    # The saved fact is also returned to the stale caller, so its next save
    # cannot accidentally recreate an evidence-free in-memory record.
    assert stale[field] == writer[field]
    stale[field]['local-only'] = True
    assert 'local-only' not in p.runtime._get(original['attempt_id'])[field]


@pytest.mark.parametrize('field', list(_RETAINED_REVIEW_FIELDS))
@pytest.mark.parametrize('replacement', ['different', 'null'])
def test_stale_save_cannot_replace_review_evidence_atomically(isolated, field, replacement):
    p = isolated
    original = record(p)
    writer, stale = copy.deepcopy(original), copy.deepcopy(original)
    writer[field] = copy.deepcopy(_RETAINED_REVIEW_FIELDS[field])
    p.runtime._save(writer)
    expected = p.runtime._get(original['attempt_id'])
    stale[field] = None if replacement == 'null' else {**writer[field], 'different': True}
    stale['phase'] = 'SIMULATED-would-be-saved-with-conflict'
    with pytest.raises(Rejected) as error:
        p.runtime._save(stale)
    assert error.value.code == 'runtime_review'
    assert p.runtime._get(original['attempt_id']) == expected


@pytest.mark.parametrize('field', list(_RETAINED_REVIEW_FIELDS))
def test_repeating_identical_review_evidence_can_save_other_progress(isolated, field):
    p = isolated
    original = record(p)
    original[field] = copy.deepcopy(_RETAINED_REVIEW_FIELDS[field])
    p.runtime._save(original)
    retry = copy.deepcopy(original)
    retry['usage'] = {'SIMULATED-token-count': 8}
    p.runtime._save(retry)
    saved = p.runtime._get(original['attempt_id'])
    assert saved[field] == original[field]
    assert saved['usage'] == retry['usage']


def test_legacy_save_without_review_fields_keeps_normal_update_behavior(isolated):
    p = isolated
    p.recipe.pop('execution_policy')
    p.recipe.pop('checkout_policy')
    original = record(p)
    first, later = copy.deepcopy(original), copy.deepcopy(original)
    first.update(phase='running', usage={'SIMULATED-token-count': 1})
    p.runtime._save(first)
    later.update(phase='completed', usage={'SIMULATED-token-count': 2})
    p.runtime._save(later)
    saved = p.runtime._get(original['attempt_id'])
    assert saved['phase'] == 'completed' and saved['usage'] == later['usage']
    assert not set(_RETAINED_REVIEW_FIELDS).intersection(saved)


@pytest.mark.parametrize('assessment', ['triage'], indirect=True)
@pytest.mark.parametrize('tamper', ['changed', 'missing', 'symlink'])
def test_triage_requires_unchanged_owned_evidence_copy(assessment, monkeypatch, tamper):
    p = assessment
    install_check(p, monkeypatch)
    evidence = Path(p.item['triage_evidence']['path'])
    if tamper == 'changed':
        evidence.write_text(canonical({'different': 'evidence'}) + '\n')
    elif tamper == 'missing':
        evidence.unlink()
    else:
        sibling = p.outputs / 'different-evidence-copy.json'
        sibling.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(sibling)
    with pytest.raises(Rejected) as error:
        run_result(p)
    assert error.value.code == 'runtime_review'
    assert p.calls == []


def streamed_log_receipt(tmp_path):
    path = tmp_path / 'native-transcript.log'
    block = b'bounded native evidence\n' * 4096
    expected = hashlib.sha256()
    with path.open('wb') as stream:
        for _ in range(32):
            stream.write(block)
            expected.update(block)
    return path, {'output': str(path), 'output_sha256': expected.hexdigest(), 'raw_outputs': {}}


def observe_bounded_log_reads(monkeypatch, path, *, after_read=None):
    from contextlib import contextmanager
    original_open = Path.open
    observed = {'sizes': [], 'bytes': 0, 'closed': False}
    @contextmanager
    def guarded_open(current, *args, **kwargs):
        with original_open(current, *args, **kwargs) as stream:
            if current != path:
                yield stream
                return
            class Reader:
                def read(self, size=-1):
                    assert 0 < size <= 1024 * 1024, 'Evidence verification must bound each allocation'
                    observed['sizes'].append(size)
                    data = stream.read(size)
                    observed['bytes'] += len(data)
                    if after_read is not None:
                        after_read()
                    return data
            try:
                yield Reader()
            finally:
                observed['closed'] = True
    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'read_bytes',
                        lambda *_: pytest.fail('Evidence verification must not allocate the entire log'))
    return observed


@pytest.mark.parametrize('matching', [True, False])
def test_large_check_receipt_verifies_exact_hash_without_whole_file_allocation(tmp_path, monkeypatch, matching):
    from agentic_tools.aos.codex_runtime import CodexRuntime
    path, receipt = streamed_log_receipt(tmp_path)
    size = path.stat().st_size
    if not matching:
        receipt['output_sha256'] = '0' * 64
    observed = observe_bounded_log_reads(monkeypatch, path)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.time.monotonic', lambda: 0.0)
    if matching:
        CodexRuntime._verify_check_receipt(receipt, deadline=1.0)
    else:
        with pytest.raises(Rejected) as error:
            CodexRuntime._verify_check_receipt(receipt, deadline=1.0)
        assert error.value.code == 'check_evidence'
    assert observed['bytes'] == size
    assert len(observed['sizes']) > 1 and observed['closed'] is True


def test_check_receipt_expiry_during_streaming_stops_and_closes_log(tmp_path, monkeypatch):
    from agentic_tools.aos.codex_runtime import CodexRuntime
    path, receipt = streamed_log_receipt(tmp_path)
    clock = {'now': 0.0}
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.time.monotonic', lambda: clock['now'])
    observed = observe_bounded_log_reads(monkeypatch, path,
        after_read=lambda: clock.update(now=2.0))
    with pytest.raises(Rejected) as error:
        CodexRuntime._verify_check_receipt(receipt, deadline=1.0)
    assert error.value.code == 'checkout_deadline'
    assert len(observed['sizes']) == 1
    assert 0 < observed['bytes'] < path.stat().st_size
    assert observed['closed'] is True
