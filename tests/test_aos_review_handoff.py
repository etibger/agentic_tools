"""Independent review handoff simulations; no model, native or service launch."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agentic_tools.aos import codex_runtime as module
from agentic_tools.aos.codex_runtime import candidate, inventory
from agentic_tools.aos.store import Rejected, canonical
from test_aos_checkout_integration import isolated, resources
from test_aos_lifecycle import setup as lifecycle_setup, implementation
from agentic_tools.aos.worker import Worker


CLEAN = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
         'partial_mutations': {'status': 'none', 'changes': []}}
COLUMNS = ('assignment_id', 'attempt_id', 'work_id', 'kind', 'predecessor', 'executor',
           'concept_revision', 'concept_digest', 'contract_revision', 'candidate',
           'scope', 'requested_model', 'requested_effort', 'state', 'sent_at')


def finding(**changes):
    return {'path': 'code.py', 'summary': 'The configured answer is incorrect',
            'evidence': 'The retained source sets answer to 1; the accepted result requires 2',
            'confirmed': True, 'in_scope': True, **changes}


def assess(p, *, findings=None, verdict='fail', native='pass', turn='completed', assignment=None):
    frozen = candidate(p.source, recipe=p.recipe)
    assignment = copy.deepcopy(assignment or p.assessment)
    assignment['candidate'] = canonical(frozen)
    output = p.outputs / assignment['attempt_id']
    output.mkdir(exist_ok=True)
    report = output / ('review-' + assignment['attempt_id'] + '.txt')
    review = {'candidate_identity': frozen['identity'], 'verdict': verdict,
              'findings': [finding()] if findings is None else findings,
              'limits': 'Simulated independent source review; no real model execution'}
    report.write_text(json.dumps(review))
    record = {'assignment': assignment, 'attempt_id': assignment['attempt_id'],
              'baseline': inventory(p.source, recipe=p.recipe), 'run_outputs': str(output),
              'thread_id': 'simulated-review-thread', 'turn_id': 'simulated-review-turn', 'usage': None}
    p.native_result = native
    result = p.runtime._result(record, p.recipe, {'status': turn}, copy.deepcopy(CLEAN))
    return result, report


@pytest.fixture
def handoff(isolated, monkeypatch):
    p = isolated
    p.assignment.update(kind='repair', predecessor='completion:review-receipt', candidate=None, state='sent')
    p.assessment = {**p.assignment, 'assignment_id': 'as-review', 'attempt_id': 'a-review',
                    'kind': 'assessment', 'executor': 'assessor', 'predecessor': 'completion:writer-receipt',
                    'candidate': canonical(candidate(p.source, recipe=p.recipe)), 'state': 'failed'}
    p.contract.update(max_repairs=1, writer='writer', assessor='assessor')
    p.budget.update(max_assignments=4, max_seconds=240)
    p.native_result, p.check_calls = 'pass', []
    monkeypatch.setattr(p.runtime, '_resource_sample', lambda *args, **kwargs: None)
    def native_check(recipe, descriptor, output, executor, frozen):
        p.check_calls.append((recipe['_attempt_id'], descriptor['name']))
        return {'name': descriptor['name'], 'result': p.native_result, 'selected': 1,
                'output': 'SIMULATED:native-result', 'executor': executor, 'candidate': frozen,
                'command': descriptor['argv'], 'scope': descriptor['scope'],
                'environment': 'SIMULATED', 'limits': 'No native command executed',
                'native_check_id': 'nc-' + recipe['_attempt_id'], 'cleanup': copy.deepcopy(CLEAN)}
    monkeypatch.setattr(p.runtime, '_check', native_check)
    monkeypatch.setattr(p.runtime, '_native_for_attempt', lambda aid, **kwargs: [{'check_id': 'nc-' + aid}])
    p.result, p.report = assess(p)
    p.ledger = p.state / 'review-ledger.sqlite3'
    @contextmanager
    def connection():
        db = sqlite3.connect(p.ledger)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    p.connection = connection
    # Prompt delivery now reads the digest-bound DI plan. Retain it in this
    # otherwise minimal simulation rather than bypassing plan validation.
    from di_plan_fixture import synthetic_plan
    from agentic_tools.aos.store import digest
    envelope = {**p.package['execution_envelope'], **p.contract, 'compatibility_checks': []}
    p.package.update(execution_envelope=envelope,
                     team_envelope=[{'name': 'writer'}, {'name': 'assessor'}],
                     di_plan=synthetic_plan(envelope))
    with connection() as db:
        db.execute('CREATE TABLE work(work_id TEXT PRIMARY KEY,current_concept INTEGER)')
        db.execute('CREATE TABLE concept(work_id TEXT,revision INTEGER,package TEXT,digest TEXT)')
        db.execute('INSERT INTO work VALUES(?,?)', (p.assignment['work_id'], 1))
        db.execute('INSERT INTO concept VALUES(?,?,?,?)',
                   (p.assignment['work_id'], 1, canonical(p.package), digest(p.package)))
    with connection() as db:
        db.execute('CREATE TABLE assignment (' + ','.join(column + ' ' + (
            'INTEGER' if column in {'concept_revision', 'contract_revision'} else 'TEXT') for column in COLUMNS)
            + ', PRIMARY KEY(assignment_id))')
        db.execute('CREATE TABLE completion(receipt_id TEXT PRIMARY KEY, assignment_id TEXT, result TEXT, disposition TEXT)')
    def assignment(row):
        with connection() as db:
            db.execute('INSERT INTO assignment VALUES(' + ','.join('?' for _ in COLUMNS) + ')',
                       tuple(row.get(column) for column in COLUMNS))
    def completion(receipt_id, row, result, disposition):
        with connection() as db:
            db.execute('INSERT INTO completion VALUES(?,?,?,?)',
                       (receipt_id, row['assignment_id'], canonical(result), disposition))
    p.insert_assignment, p.insert_completion = assignment, completion
    assignment(p.assessment)
    completion('review-receipt', p.assessment, p.result, 'repair')
    assignment(p.assignment)
    p.runtime.lifecycle.store._connection = connection
    p.runtime.lifecycle._run = lambda db, work: ({'cycle': 1}, p.contract)
    return p


def update(p, table, values, key, identity):
    with p.connection() as db:
        db.execute('UPDATE ' + table + ' SET ' + ','.join(name + '=?' for name in values) + ' WHERE ' + key + '=?',
                   (*values.values(), identity))


def retry_chain(p, count):
    predecessor = 'completion:review-receipt'
    rows = []
    for index in range(count):
        row = {**p.assignment, 'assignment_id': 'retry-assignment-' + str(index),
               'attempt_id': 'retry-attempt-' + str(index), 'predecessor': predecessor, 'state': 'failed'}
        receipt = 'retry-receipt-' + str(index)
        p.insert_assignment(row)
        p.insert_completion(receipt, row, {'status': 'failed', **CLEAN}, 'startup_retry')
        predecessor = 'startup-retry:' + receipt
        rows.append(row)
    update(p, 'assignment', {'predecessor': predecessor}, 'assignment_id', p.assignment['assignment_id'])
    return rows


def test_actual_assessment_result_contains_validated_structured_finding(handoff):
    p = handoff
    assert p.result['status'] == 'failed'
    assert p.result['finding'] == {'confirmed': True, 'in_scope': True,
                                   'summary': 'code.py: The configured answer is incorrect'}
    assert p.result['review_findings'] == [finding()]
    assert p.result['initial_reports']['review'] == {
        'path': str(p.report), 'sha256': hashlib.sha256(p.report.read_bytes()).hexdigest()}
    assert p.result['initial_reports']['independent'] is True
    assert p.check_calls == [('a-review', 'fixture')]


@pytest.mark.parametrize('verdict,findings,native,turn', [
    ('fail', [], 'fail', 'completed'),
    ('pass', [], 'fail', 'completed'),
    ('blocked', [finding()], 'pass', 'completed'),
    ('pass', [finding()], 'pass', 'completed'),
    ('fail', ['a generic failure'], 'pass', 'completed'),
    ('fail', [finding(confirmed=False)], 'pass', 'completed'),
    ('fail', [finding(confirmed=1)], 'pass', 'completed'),
    ('fail', [finding(in_scope=False)], 'pass', 'completed'),
    ('fail', [finding(path='test_example.py')], 'pass', 'completed'),
    ('fail', [finding(path='../code.py')], 'pass', 'completed'),
    ('fail', [finding(), finding(path='test_example.py')], 'pass', 'completed'),
    ('fail', [finding(summary=' ')], 'pass', 'completed'),
    ('fail', [finding(evidence='')], 'pass', 'completed'),
    ('fail', [finding()], 'pass', 'failed'),
    ('fail', [finding()], 'pass', 'interrupted'),
])
def test_unsupported_review_and_generic_failed_commands_never_authorize_correction(handoff, verdict, findings, native, turn):
    result, _ = assess(handoff, verdict=verdict, findings=findings, native=native, turn=turn)
    assert result['status'] == 'failed'
    assert 'finding' not in result and 'review_findings' not in result


@pytest.mark.parametrize('findings', [[finding()] * 17, [finding(evidence='x' * 4097)],
                                     [finding(summary='s' * 3000)] * 3, [finding(evidence='e' * 4096)] * 9])
def test_result_does_not_authorize_feedback_that_exceeds_the_later_prompt_bound(handoff, findings):
    result, _ = assess(handoff, findings=findings)
    assert result['status'] == 'failed'
    assert 'finding' not in result and 'review_findings' not in result


def test_failed_native_check_can_accompany_a_separately_confirmed_source_finding(handoff):
    result, _ = assess(handoff, native='fail')
    assert result['status'] == 'failed' and result['finding']['confirmed'] is True
    assert result['checks'][0]['result'] == 'fail' and result['review_findings'] == [finding()]


def test_deleted_scoped_file_remains_a_valid_independent_repair_finding(handoff):
    p = handoff
    (p.source / 'code.py').unlink()
    result, _ = assess(p, findings=[finding(summary='The required file was removed',
                                          evidence='The approved code.py file is absent from the candidate')])
    assert 'code.py' not in result['candidate']['inventory']
    assert result['finding']['confirmed'] is True and result['review_findings'][0]['path'] == 'code.py'


def test_a_directory_is_not_a_source_file_finding(handoff):
    p = handoff
    (p.source / 'code.py').unlink()
    (p.source / 'code.py').mkdir()
    result, _ = assess(p)
    assert result['status'] == 'failed' and 'finding' not in result


@pytest.mark.parametrize('count', [0, 1, 2])
def test_exact_handoff_survives_at_most_two_saved_startup_retries(handoff, count):
    p = handoff
    retry_chain(p, count)
    result = p.runtime._repair_feedback(p.assignment)
    assert result == {'assessor': 'assessor', 'candidate': p.result['candidate']['identity'],
                      'review': p.result['initial_reports']['review'], 'finding': p.result['finding'],
                      'findings': p.result['review_findings']}
    assert len(canonical(result)) <= 32768


def test_third_retry_and_cyclic_chain_cannot_reach_an_assessment(handoff):
    p = handoff
    retry_chain(p, 3)
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)
    update(p, 'assignment', {'predecessor': 'startup-retry:retry-receipt-2'}, 'assignment_id', 'retry-assignment-2')
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)


@pytest.mark.parametrize('field,value', [('work_id', 'OTHER'), ('concept_revision', 2),
                                       ('concept_digest', 'sha256:other'), ('contract_revision', 2),
                                       ('kind', 'implementation')])
@pytest.mark.parametrize('link', ['assessment', 'retry'])
def test_predecessor_chain_cannot_cross_work_or_approved_contract(handoff, field, value, link):
    p = handoff
    rows = retry_chain(p, 1)
    target = p.assessment if link == 'assessment' else rows[0]
    update(p, 'assignment', {field: value}, 'assignment_id', target['assignment_id'])
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)


@pytest.mark.parametrize('disposition,receipt', [('held', 'review-receipt'), ('repair', 'retry-receipt-0')])
def test_only_recorded_repair_and_startup_retry_dispositions_form_the_chain(handoff, disposition, receipt):
    p = handoff
    retry_chain(p, 1)
    update(p, 'completion', {'disposition': disposition}, 'receipt_id', receipt)
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)


def test_result_candidate_must_equal_the_failed_assessment_candidate(handoff):
    p = handoff
    wrong = {**p.result['candidate'], 'identity': 'sha256:other'}
    update(p, 'assignment', {'candidate': canonical(wrong)}, 'assignment_id', p.assessment['assignment_id'])
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)


@pytest.mark.parametrize('change', ['contents', 'reference', 'hash', 'missing', 'symlink', 'oversize'])
def test_retained_report_must_keep_exact_path_content_hash_and_regular_file(handoff, change):
    p = handoff
    result = copy.deepcopy(p.result)
    if change == 'contents':
        p.report.write_text('a different review')
    elif change == 'reference':
        result['initial_reports']['review']['path'] = str(p.report.with_name('other.txt'))
    elif change == 'hash':
        result['initial_reports']['review']['sha256'] = '0' * 64
    elif change == 'missing':
        p.report.unlink()
    elif change == 'symlink':
        target = p.report.with_name('retained.txt')
        p.report.rename(target)
        p.report.symlink_to(target)
    else:
        p.report.write_text('x' * (1024 * 1024 + 1))
        result['initial_reports']['review']['sha256'] = hashlib.sha256(p.report.read_bytes()).hexdigest()
    update(p, 'completion', {'result': canonical(result)}, 'receipt_id', 'review-receipt')
    with pytest.raises(Rejected, match='handoff'):
        p.runtime._repair_feedback(p.assignment)


class NoTimer:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass
    def cancel(self): pass


def scripted_start(p, monkeypatch, *, on_thread=lambda: None):
    calls = []
    def request(method, params):
        calls.append((method, copy.deepcopy(params)))
        if method == 'thread/start':
            on_thread()
            return {'thread': {'id': 'SIMULATED-repair-thread'}, 'model': p.assignment['requested_model'],
                    'reasoningEffort': p.assignment['requested_effort'], 'approvalPolicy': 'never',
                    'cwd': p.recipe['source']}
        if method == 'config/read':
            return {'config': {}}
        if method == 'turn/start':
            return {'turn': {'id': 'SIMULATED-repair-turn'}}
        raise AssertionError(method)
    client = SimpleNamespace(request=request, timeout=15)
    monkeypatch.setattr(p.runtime, '_client', lambda *args, **kwargs: client)
    monkeypatch.setattr(p.runtime, '_checkout_before_start', lambda *args, **kwargs: resources())
    monkeypatch.setattr(p.runtime, '_resource_watch', lambda *args: None)
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(p.runtime, '_verify_config', lambda *args: None)
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *args, **kwargs: {'SIMULATED': True})
    monkeypatch.setattr(module.instructions, 'readback', lambda *args, **kwargs:
                        {'manifest': {}, 'configuration_transition': None})
    monkeypatch.setattr(module.instructions, 'verify_settings', lambda *args: None)
    monkeypatch.setattr(module.threading, 'Timer', NoTimer)
    return calls


def test_real_start_prompt_receives_saved_phase_and_exact_feedback_without_rereading_siblings(handoff, monkeypatch):
    p = handoff
    expected = p.runtime._repair_feedback(p.assignment)
    calls = scripted_start(p, monkeypatch, on_thread=lambda: p.report.write_text('changed after feedback was frozen'))
    observation = p.runtime.start(p.assignment)
    assert observation['state'] == 'active', observation
    turns = [params for method, params in calls if method == 'turn/start']
    assert len(turns) == 1 and p.runtime.model_calls == 1
    prompt = turns[0]['input'][0]['text']
    assert '\nAssignment phase: repair\n' in prompt
    assert canonical(expected) in prompt
    assert 'changed after feedback was frozen' not in prompt
    assert 'do not repeat first-implementation rehearsal instructions' in prompt
    saved = p.runtime._get(p.assignment['attempt_id'])
    assert saved['repair_feedback'] == expected and saved['model_calls_attempted'] == 1
    assert saved['budget_seconds'] == 60


@pytest.mark.parametrize('change', ['report', 'source'])
def test_invalid_handoff_blocks_real_start_before_any_charge_or_model_send(handoff, monkeypatch, change):
    p = handoff
    calls = scripted_start(p, monkeypatch)
    (p.report if change == 'report' else p.source / 'code.py').write_text('changed before repair startup')
    with pytest.raises(Rejected):
        p.runtime.start(p.assignment)
    assert calls == [] and p.runtime.model_calls == 0
    with p.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM runtime_budget').fetchone()[0] == 0


def test_actual_validated_assessment_drives_one_bounded_lifecycle_correction(handoff, tmp_path):
    p = handoff
    # The result-producing runtime and receiving lifecycle must name the same
    # work; dependency verification resolves the recipe by that identity.
    work_id = p.assignment['work_id']
    store, lifecycle, _ = lifecycle_setup(tmp_path / 'lifecycle', max_repairs=1, work_id=work_id)
    frozen = candidate(p.source, recipe=p.recipe)
    with Worker(lifecycle, reconcile_on_enter=False) as worker:
        writer = lifecycle.claim(lifecycle.assignments()[0]['assignment_id'], worker.epoch)
        lifecycle.complete('writer-result', writer['assignment_id'], implementation(frozen))
        assessment = lifecycle.claim(lifecycle.assignments()[-1]['assignment_id'], worker.epoch)
        result, _ = assess(p, assignment=assessment)
        accepted = lifecycle.complete('assessment-result', assessment['assignment_id'], result)
        assert accepted['disposition'] == 'repair'
        duplicate = lifecycle.complete('assessment-result', assessment['assignment_id'], result)
        assert duplicate == {'disposition': 'repair', 'duplicate': True}
        repair_rows = [row for row in lifecycle.assignments() if row['kind'] == 'repair']
        assert len(repair_rows) == 1
        repair = lifecycle.claim(repair_rows[0]['assignment_id'], worker.epoch)
        lifecycle.complete('repair-result', repair['assignment_id'], implementation(frozen))
        reassessment = lifecycle.claim(lifecycle.assignments()[-1]['assignment_id'], worker.epoch)
        second, _ = assess(p, assignment=reassessment)
        exhausted = lifecycle.complete('second-assessment-result', reassessment['assignment_id'], second)
        assert exhausted['disposition'] == 'held'
        assert len([row for row in lifecycle.assignments() if row['kind'] == 'repair']) == 1
        snapshot = lifecycle.snapshot(work_id)
        assert snapshot['execution'] == 'held'
        assert any(item['code'] == 'repair_limit' for item in snapshot['technical_blockers'])


@pytest.mark.parametrize('unknown', [{'effects_reconciled': False}, {'owned_processes': ['SIMULATED:unreaped']}])
def test_confirmed_finding_does_not_bypass_owned_cleanup_before_repair(handoff, tmp_path, unknown):
    p = handoff
    work_id = p.assignment['work_id']
    _, lifecycle, _ = lifecycle_setup(tmp_path / 'cleanup-lifecycle', max_repairs=1, work_id=work_id)
    frozen = candidate(p.source, recipe=p.recipe)
    with Worker(lifecycle, reconcile_on_enter=False) as worker:
        writer = lifecycle.claim(lifecycle.assignments()[0]['assignment_id'], worker.epoch)
        lifecycle.complete('writer-result', writer['assignment_id'], implementation(frozen))
        assessment = lifecycle.claim(lifecycle.assignments()[-1]['assignment_id'], worker.epoch)
        result, _ = assess(p, assignment=assessment)
        result.update(unknown)
        response = lifecycle.complete('assessment-result', assessment['assignment_id'], result)
        assert response['disposition'] == 'held'
        assert not any(row['kind'] == 'repair' for row in lifecycle.assignments())
        assert lifecycle.snapshot(work_id)['execution'] == 'held'


@pytest.mark.parametrize('phase', ['implementation', 'assessment'])
def test_real_start_prompt_identifies_each_nonrepair_phase(handoff, monkeypatch, phase):
    p = handoff
    values = {'kind': phase, 'executor': 'assessor' if phase == 'assessment' else 'writer',
              'candidate': canonical(candidate(p.source, recipe=p.recipe)) if phase == 'assessment' else None}
    p.assignment.update(values)
    update(p, 'assignment', values, 'assignment_id', p.assignment['assignment_id'])
    calls = scripted_start(p, monkeypatch)
    observation = p.runtime.start(p.assignment)
    assert observation['state'] == 'active', observation
    prompt = next(params['input'][0]['text'] for method, params in calls if method == 'turn/start')
    assert '\nAssignment phase: ' + phase in prompt
    assert 'Exact retained independent assessment feedback' not in prompt
    if phase == 'assessment':
        assert 'confirmed:true and in_scope:true' in prompt
        assert 'a failed command alone is not a confirmed finding' in prompt


def test_source_change_between_handoff_validation_and_baseline_capture_spends_nothing(handoff, monkeypatch):
    p = handoff
    calls = scripted_start(p, monkeypatch)
    def changed_during_preparation(*args, **kwargs):
        (p.source / 'code.py').write_text('answer = 99\n')
        return resources()
    monkeypatch.setattr(p.runtime, '_checkout_before_start', changed_during_preparation)
    with pytest.raises(Rejected, match='source|candidate'):
        p.runtime.start(p.assignment)
    assert calls == [] and p.runtime.model_calls == 0
    with p.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM runtime_budget').fetchone()[0] == 0
