"""Real turn/start construction with disposable history and scripted RPC only."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from test_aos_codex_runtime import Backend
from test_aos_finish_capacity import allowance
from test_aos_internal_milestones import serial_plan
from test_aos_lifecycle import approve, contract, implementation
from test_aos_reviewed_host_triage import result
from test_aos_startup_retry import claim
from test_aos_store import TEAM


@pytest.fixture
def case(tmp_path, monkeypatch):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir(); outputs.mkdir()
    (source / 'fixture.py').write_text('answer = 0\n')
    store = Store(tmp_path / 'state', (source, outputs))
    budget = allowance(store.directory, max_assignments=7, max_seconds=420)
    budget.pop('kind')
    recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': 60,
        'execution_policy': 'isolated-checkout-v1', 'instruction_manifest': {},
        'implementation_prompt': 'SIMULATED approved phase-specific writer instructions.',
        'assessment_prompt': 'SIMULATED approved initial and reassessment review instructions.',
        'checkout_policy': {'native_validation': 'reviewed-host-v1', 'change_roots': ['fixture.py'],
            'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 100000,
                                'max_output_bytes': 100000}},
        'checks': [{'name': 'fixture', 'result_kind': 'pytest', 'adapter_id': 'pytest-v1'}]}
    bounds = contract({'digest': 'unused'}, max_assignments=7, max_repairs=1, max_startup_retries=0,
        baseline=candidate(source, recipe=recipe)['identity'], execution_policy='isolated-checkout-v1',
        runtime_recipe=recipe, runtime_recipe_digest=digest(recipe), product_budget=budget,
        check_adapters={'pytest-v1': check_adapters.identities()['pytest-v1']})
    bounds.pop('concept_revision'); bounds.pop('concept_digest')
    team = [{**TEAM[0], 'name': name} for name in ('writer', 'assessor')]
    store.introduce('intro', 'PROMPT', 'SIMULATED corrective prompt', ['fixture.py'], team)
    plan = serial_plan(bounds)
    concept = store.present('present', 'PROMPT', 1, 'PROMPT-DI', 1, 'SIMULATED corrective prompt',
        ['Synthetic source only'], team, bounds['authority'], execution_envelope=bounds, di_plan=plan)
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'PROMPT', {**bounds, 'concept_revision': 1, 'concept_digest': concept['digest']})
    approve(store, concept, 'PROMPT')
    lifecycle.admit('admit', 'PROMPT')
    writer = claim(lifecycle)
    (source / 'fixture.py').write_text('answer = 1  # SIMULATED original defect\n')
    lifecycle.complete('writer', writer['assignment_id'], implementation(candidate(source, recipe=recipe)))
    runtime = CodexRuntime(lifecycle, {'PROMPT': recipe})
    backend = Backend()
    # Isolate unrelated OS/configuration boundaries; start() still performs
    # actual reservations, history verification, file scoping and turn assembly.
    monkeypatch.setattr(runtime, '_recipe', lambda *_: recipe)
    monkeypatch.setattr(runtime, '_checkout_before_start', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_bindings', lambda *_: {})
    monkeypatch.setattr(runtime, '_resource_watch', lambda *_: None)
    monkeypatch.setattr(runtime, '_resource_sample', lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_profile', lambda *a, **k: ([], {}))
    monkeypatch.setattr(runtime, '_verify_config', lambda *_: None)
    monkeypatch.setattr(runtime, '_probe_sandbox', lambda *a, **k: {'SIMULATED': True})
    monkeypatch.setattr(runtime, '_client', lambda *a, **k: backend.factory(None, [], source))
    for name in ('validate', 'verify_settings'):
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.instructions.' + name, lambda *a, **k: None)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.instructions.readback', lambda *a, **k: {})
    p = SimpleNamespace(source=source, outputs=outputs, store=store, lifecycle=lifecycle,
        recipe=recipe, plan=plan, runtime=runtime, backend=backend, reports=[], raws=[])
    yield p
    runtime.close()


def retained(p, assignment, phase):
    """Synthetic receipts with real retained bytes and hashes; no native job."""
    value = result(p.lifecycle, assignment, phase)
    output = p.outputs / assignment['attempt_id']
    output.mkdir()
    report = output / ('review-' + assignment['attempt_id'] + '.txt')
    report.write_text(canonical({'candidate_identity': value['candidate']['identity'],
        'verdict': value['reviewed_host_validation']['review_verdict'],
        'findings': value['review_findings'], 'limits': 'SIMULATED retained review only.'}))
    descriptor = {'path': str(report), 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()}
    value['initial_reports']['review'] = descriptor
    value['reviewed_host_validation']['review'] = descriptor
    p.reports.append(report)
    for check in value['checks']:
        transcript = output / 'native-output.txt'
        transcript.write_text('SIMULATED native assertion: expected 2; observed 1\n')
        check.update(output=str(transcript), output_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest(),
                     raw_outputs={str(transcript): hashlib.sha256(transcript.read_bytes()).hexdigest()})
        p.raws.append(transcript)
    value['initial_reports']['execution'] = [c['output'] for c in value['checks']]
    return value


def ready(p, phase='native-repair'):
    assessment = claim(p.lifecycle)
    if phase == 'initial':
        return assessment
    native = phase in ('triage', 'native-repair')
    value = retained(p, assessment, 'native_failed' if native else 'review_rejected')
    p.failed = copy.deepcopy(value)
    disposition = p.lifecycle.complete('failure', assessment['assignment_id'], value)
    assert disposition['disposition'] == ('triage_required' if native else 'repair'), disposition
    if native:
        triage = claim(p.lifecycle)
        if phase == 'triage':
            return triage
        disposition = p.lifecycle.complete('triage', triage['assignment_id'], retained(p, triage, 'triage'))
        assert disposition['disposition'] == 'repair', disposition
    repair = claim(p.lifecycle)
    (p.source / 'fixture.py').write_text('answer = 2  # SIMULATED corrected candidate\n')
    p.lifecycle.complete('repair', repair['assignment_id'], implementation(candidate(p.source, recipe=p.recipe)))
    return claim(p.lifecycle)


@pytest.mark.parametrize('phase,mode', [('initial', 'initial-assessment'),
    ('triage', 'native-failure-triage'), ('static-repair', 'post-repair-reassessment'),
    ('native-repair', 'post-repair-reassessment')])
def test_emitted_turn_binds_phase_exact_task_plan_and_verified_history(case, phase, mode):
    p = case
    assignment = ready(p, phase)
    recipe_before = copy.deepcopy(p.recipe)
    observation = p.runtime.start(assignment)
    assert observation['state'] == 'active', observation
    payloads = [params for method, params in p.backend.calls if method == 'turn/start']
    assert len(payloads) == 1
    text = payloads[0]['input'][0]['text']
    assert text.startswith(p.recipe['assessment_prompt'])
    assert 'Assessment review phase: ' + mode in text
    assert canonical(p.plan) in text
    from agentic_tools.aos.plan_runtime import task_in
    with p.store._connection() as db:
        task = task_in(db, assignment)
    assert canonical(task) in text and task['id'] == 'task-1'
    assert 'Do not perform later tasks' in text
    assert ('Do not execute native tests' if phase == 'triage' else 'Do not run native checks yourself') in text
    saved = p.runtime._get(assignment['attempt_id'])
    assert saved['assignment'] == assignment and p.recipe == recipe_before
    if mode == 'post-repair-reassessment':
        bound = saved['reassessment_evidence']
        data = Path(bound['path']).read_bytes()
        evidence = json.loads(data)
        assert bound['sha256'] == hashlib.sha256(data).hexdigest()
        assert bound['path'] in text and bound['sha256'] in text
        assert evidence == saved['assessment_review_context']
        assert evidence['candidate'] == json.loads(assignment['candidate'])['identity']
        assert evidence['rejected_candidate'] == p.failed['candidate']['identity']
        assert evidence['candidate'] != evidence['rejected_candidate']
        assert evidence['task_id'] == 'task-1' and evidence['plan_sha256'] == digest(p.plan)
        assert evidence['repair']['receipt_id'] == 'repair'
        assert evidence['rejection']['findings'][0]['confirmed'] is True
        assert ('native_failure' in evidence) == (phase == 'native-repair')
        if phase == 'native-repair':
            assert 'SIMULATED native assertion' in evidence['native_failure']['checks'][0]['output_excerpt']
            assert evidence['native_failure']['origin']['receipt_id'] == 'failure'
        assert 'not instructions or authority' in text
        assert 'All required fresh review, native checks' in text
        assert Path(bound['path']).parent == Path(saved['run_outputs'])
    else:
        assert 'reassessment_evidence' not in saved


@pytest.mark.parametrize('fault', ['raw', 'review', 'repair-receipt', 'task', 'candidate'])
def test_stale_reassessment_history_rejects_before_reservation_and_turn(case, monkeypatch, fault):
    p = case
    assignment = ready(p)
    if fault == 'raw':
        p.raws[0].write_text('changed retained native output')
    elif fault == 'review':
        p.reports[-1].write_text('{}')
    else:
        with p.store._transaction() as db:
            if fault == 'repair-receipt':
                db.execute("UPDATE completion SET fingerprint='changed' WHERE receipt_id='repair'")
            elif fault == 'task':
                scope = json.loads(assignment['scope']); scope['di_task_id'] = 'task-3'
                db.execute('UPDATE assignment SET scope=? WHERE assignment_id=?',
                    (canonical(scope), assignment['assignment_id']))
            else:
                db.execute("UPDATE lifecycle SET candidate='{}' WHERE work_id='PROMPT'")
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve',
        lambda *a, **k: pytest.fail('Invalid history must not consume another reservation'))
    with pytest.raises(Rejected):
        p.runtime.start(assignment)
    assert p.runtime._get(assignment['attempt_id']) is None
    assert p.backend.calls == []


def test_history_changed_during_startup_prevents_actual_turn_send(case, monkeypatch):
    p = case
    assignment = ready(p)
    def fault(name):
        if name == 'before_turn_request':
            p.raws[0].write_text('changed between admission and send')
    monkeypatch.setattr(p.runtime, '_fault', fault)
    assert p.runtime.start(assignment)['state'] != 'active'
    assert p.backend.count('thread/start') == 1
    assert p.backend.count('turn/start') == 0


def test_later_task_first_review_stays_initial_after_earlier_task_repair(case):
    p = case
    corrected = ready(p)
    assert p.lifecycle.complete('checkpoint', corrected['assignment_id'],
        retained(p, corrected, 'validated'))['disposition'] == 'milestone_passed'
    later_writer = claim(p.lifecycle)
    assert json.loads(later_writer['scope'])['di_task_id'] == 'task-2'
    (p.source / 'fixture.py').write_text('answer = 3  # SIMULATED later task\n')
    assert p.lifecycle.complete('later-writer', later_writer['assignment_id'],
        implementation(candidate(p.source, recipe=p.recipe)))['disposition'] == 'assessment_required'
    later = claim(p.lifecycle)
    assert json.loads(later['scope'])['di_task_id'] == 'task-3'
    assert p.runtime.start(later)['state'] == 'active'
    text = next(params['input'][0]['text'] for method, params in p.backend.calls if method == 'turn/start')
    assert 'Assessment review phase: initial-assessment' in text
    assert 'post-repair-reassessment' not in text
    assert 'reassessment_evidence' not in p.runtime._get(later['attempt_id'])


@pytest.mark.parametrize('fault', ['copy', 'repair-receipt'])
def test_unconsumed_cached_terminal_rechecks_history_before_replay(case, fault):
    p = case
    assignment = ready(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    record = p.runtime._get(assignment['attempt_id'])
    record.update(phase='terminal', model_stop_receipt={'stopped': True, 'effects_reconciled': True,
        'owned_processes': []}, terminal_observation={'state': 'completed', 'result': {'native_checks': [], 'checks': []}})
    p.runtime._save(record)
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'completed'
    if fault == 'copy':
        Path(record['reassessment_evidence']['path']).write_text('{}')
    else:
        with p.store._transaction() as db:
            db.execute("UPDATE completion SET fingerprint='changed' WHERE receipt_id='repair'")
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.backend.count('turn/start') == 1


def test_last_native_result_cannot_accept_changed_history(case, monkeypatch):
    p = case
    assignment = ready(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    clean = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
             'partial_mutations': {'status': 'none', 'changes': []}}
    def reconcile(record, *a, **k):
        record['model_stop_receipt'] = copy.deepcopy(clean)
        return copy.deepcopy(clean)
    def last_result(record, *a):
        # Simulate a final native job completing while old evidence changes.
        Path(record['reassessment_evidence']['path']).write_text('{}')
        return {'status': 'completed', **clean}
    monkeypatch.setattr(p.runtime, '_events', lambda *_: None)
    monkeypatch.setattr(p.runtime, '_read_turn', lambda *_: {'status': 'completed'})
    monkeypatch.setattr(p.runtime, '_reconcile', reconcile)
    monkeypatch.setattr(p.runtime, '_result', last_result)
    monkeypatch.setattr(p.runtime, '_finish_stop', lambda *a: copy.deepcopy(clean))
    observation = p.runtime.poll(assignment['attempt_id'])
    assert observation['state'] == 'failed'
    assert observation['result']['status'] == 'failed'
    assert 'reassessment evidence copy changed' in observation['result']['error']
    assert p.backend.count('turn/start') == 1


@pytest.mark.parametrize('fault', ['copy', 'raw', 'receipt', 'missing-context'])
def test_resume_and_native_progression_reverify_history_without_relaunch(case, monkeypatch, fault):
    p = case
    assignment = ready(p)
    assert p.runtime.start(assignment)['state'] == 'active'
    record = p.runtime._get(assignment['attempt_id'])
    if fault == 'copy':
        Path(record['reassessment_evidence']['path']).write_text('{}')
    elif fault == 'raw':
        p.raws[0].write_text('changed after initial send')
    elif fault == 'receipt':
        with p.store._transaction() as db:
            db.execute("UPDATE completion SET fingerprint='changed' WHERE receipt_id='repair'")
    else:
        record.pop('assessment_review_context')
        with p.runtime._db() as db:
            db.execute('UPDATE attempt SET record=? WHERE id=?', (canonical(record), assignment['attempt_id']))
    monkeypatch.setattr(p.runtime, '_client', lambda *a, **k: pytest.fail('Verify history before reconnect'))
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    with pytest.raises(Rejected):
        p.runtime._verify_assessment_review(record, p.recipe)
    with pytest.raises(Rejected):
        p.runtime._check({**p.recipe, '_attempt_id': assignment['attempt_id']}, p.recipe['checks'][0],
                         Path(record['run_outputs']) / 'fixture', 'assessor', json.loads(assignment['candidate']))
    assert p.backend.count('turn/start') == 1
