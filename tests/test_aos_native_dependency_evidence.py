"""Real reviewed-host pytest logs reach transitive consumers; model/approval scripted.

No vendor, model, readiness or product-case execution. Native command ownership,
typed adapters, journals, dependency preparation and lifecycle are production code.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from agentic_tools.aos import checkout_boundary, reviewed_host
from test_aos_codex_runtime import Backend
from test_aos_finish_capacity import allowance
from test_aos_internal_milestones import serial_plan
from test_aos_lifecycle import approve, contract
from test_aos_startup_retry import claim
from test_aos_store import TEAM

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS reviewed-host ownership route')


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir(); outputs.mkdir()
    (source / 'fixture.py').write_text('answer = 0\n')
    (source / 'README.txt').write_text('original usage\n')
    for name, count in [('summary', 9), ('cli', 6)]:
        (source / ('test_' + name + '.py')).write_text(
            'import pytest\nfrom fixture import answer\n'
            f'@pytest.mark.parametrize("case", range({count}))\n'
            'def test_answer(case): assert answer == 2\n')
    store = Store(tmp_path / 'state', (source, outputs))
    budget = allowance(store.directory, max_assignments=7, max_seconds=420)
    budget.pop('kind')
    recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': 60,
        'execution_policy': 'isolated-checkout-v1', 'instruction_manifest': {},
        'native_process_ownership': 'host-owned-v1', 'requires_detached_processes': False,
        'probe_interpreter': str(Path(sys.executable).resolve()),
        'read_roots': list(dict.fromkeys([str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve())])),
        'implementation_prompt': 'SCRIPTED fixture writer only.', 'assessment_prompt': 'SCRIPTED source review only.',
        'checkout_policy': {'native_validation': 'reviewed-host-v1', 'change_roots': ['fixture.py', 'README.txt'],
            'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 1000000, 'max_output_bytes': 1000000}},
        'checks': [{'name': name + '-tests', 'argv': [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                       'test_' + name + '.py'], 'timeout_seconds': 10,
                    'scope': 'Disposable ' + name + ' behavior', 'result_kind': 'pytest', 'adapter_id': 'pytest-v1'}
                   for name in ('summary', 'cli')]}
    bounds = contract({'digest': 'unused'}, max_assignments=7, max_repairs=1, max_startup_retries=0,
        baseline=candidate(source, recipe=recipe)['identity'], execution_policy='isolated-checkout-v1',
        runtime_recipe=recipe, runtime_recipe_digest=digest(recipe), product_budget=budget,
        required_checks=['summary-tests', 'cli-tests'],
        check_adapters={'pytest-v1': check_adapters.identities()['pytest-v1']})
    bounds.pop('concept_revision'); bounds.pop('concept_digest')
    team = [{**TEAM[0], 'name': name} for name in ('writer', 'assessor')]
    store.introduce('intro', 'NATIVE-DI', 'SCRIPTED approval; actual disposable native evidence', ['fixture.py'], team)
    concept = store.present('present', 'NATIVE-DI', 1, 'NATIVE-DI-plan', 1, 'Disposable evidence route',
        ['Actual host log producer and transitive consumer'], team, bounds['authority'],
        execution_envelope=bounds, di_plan=serial_plan(bounds))
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'NATIVE-DI', {**bounds, 'concept_revision': 1, 'concept_digest': concept['digest']})
    approve(store, concept, 'NATIVE-DI')
    lifecycle.admit('admit', 'NATIVE-DI')
    backend = Backend()
    runtime = CodexRuntime(lifecycle, {'NATIVE-DI': recipe})
    # These are synthetic model/config gates, never qualification.
    # Keep actual native/check adapters, host ownership and dependency verification.
    monkeypatch.setattr(runtime, '_recipe', lambda *_: recipe)
    for name in ('_watch', '_resource_watch', '_verify_config'):
        monkeypatch.setattr(runtime, name, lambda *a, **k: None)
    monkeypatch.setattr(runtime, '_profile', lambda *a, **k: ([], {}))
    monkeypatch.setattr(runtime, '_probe_sandbox', lambda *a, **k: {'SCRIPTED': True})
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes', lambda **kwargs: {})
    for name in ('validate', 'verify_settings'):
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.instructions.' + name, lambda *a, **k: None)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.instructions.readback', lambda *a, **k: {})
    def client(record, recipe, **kwargs):
        aid = record['attempt_id']
        if aid not in runtime.clients or runtime.clients[aid].closed:
            runtime.clients[aid] = backend.factory(None, [], source)
        return runtime.clients[aid]
    monkeypatch.setattr(runtime, '_client', client)
    p = SimpleNamespace(source=source, outputs=outputs, store=store, lifecycle=lifecycle,
                        recipe=recipe, runtime=runtime, backend=backend)
    yield p
    runtime.close()


def finish(p, assignment, *, edit=None, finding=False, consume=True):
    observation = p.runtime.start(assignment)
    assert observation['state'] == 'active', observation
    if edit:
        edit()
    if assignment['kind'] == 'assessment':
        report = p.outputs / assignment['attempt_id'] / ('review-' + assignment['attempt_id'] + '.txt')
        report.write_text(canonical({'candidate_identity': json.loads(assignment['candidate'])['identity'],
            'verdict': 'fail' if finding else 'pass', 'findings': [{'path': 'fixture.py',
                'summary': 'answer is one instead of two', 'evidence': 'Actual native assertions expected two',
                'confirmed': True, 'in_scope': True}] if finding else [],
            'limits': 'Scripted source review; actual native tests and ownership are independent of this fixture verdict.'}))
    p.backend.finish(observation['thread_id'])
    observed = p.runtime.poll(assignment['attempt_id'])
    assert observed['state'] in ('completed', 'failed'), observed
    if not consume:
        return observed['result'], observed
    response = p.lifecycle.complete(observed['receipt_id'], assignment['assignment_id'], observed['result'])
    return observed['result'], response


def checkpoint(p, *, corrective=True):
    finish(p, claim(p.lifecycle), edit=lambda: (p.source / 'fixture.py').write_text(
        'answer = 1\n' if corrective else 'answer = 2\n'))
    if corrective:
        _, failed = finish(p, claim(p.lifecycle))
        assert failed['disposition'] == 'triage_required'
        _, triaged = finish(p, claim(p.lifecycle), finding=True)
        assert triaged['disposition'] == 'repair'
        finish(p, claim(p.lifecycle), edit=lambda: (p.source / 'fixture.py').write_text('answer = 2\n'))
    p.accepted_assignment = claim(p.lifecycle)
    p.accepted, completed = finish(p, p.accepted_assignment)
    assert completed['disposition'] == 'milestone_passed'
    assert [(c['selected'], c['passed']) for c in p.accepted['checks']] == [(9, 9), (6, 6)]
    p.packaging = claim(p.lifecycle)
    return p.packaging


def test_actual_corrective_host_logs_reach_packaging_and_final_consumer(native_case):
    p = native_case
    packaging = checkpoint(p)
    accepted_output = p.outputs / p.accepted_assignment['attempt_id']
    for check in p.accepted['checks']:
        assert Path(check['output']).is_relative_to(accepted_output)
        assert {str(Path(raw).name) for raw in check['raw_outputs'] if not Path(raw).is_relative_to(accepted_output)} == {
            'stdout.log', 'stderr.log'}
        assert all(Path(raw).parent == p.store.directory / 'native-status' / (check['native_check_id'] + '-host')
                   for raw in check['raw_outputs'] if not Path(raw).is_relative_to(accepted_output))
    assert not p.runtime._get(packaging['attempt_id'])
    _, packed = finish(p, packaging, edit=lambda: (p.source / 'README.txt').write_text('packaged usage\n'))
    assert packed['disposition'] == 'assessment_required'
    final = claim(p.lifecycle)
    assert p.runtime.start(final)['state'] == 'active'
    record = p.runtime._get(final['attempt_id'])
    artifact = Path(record['dependency_evidence']['path'])
    assert artifact.parent == p.outputs / final['attempt_id']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == record['dependency_evidence']['sha256']
    evidence = json.loads(artifact.read_bytes())
    assert [a['task_id'] for a in evidence['ancestors']] == ['task-0', 'task-1', 'task-2']
    accepted = evidence['ancestors'][1]
    assert accepted['candidate'] == p.accepted['candidate'] != evidence['candidate']
    assert accepted['checks'][0]['raw_outputs'] == p.accepted['checks'][0]['raw_outputs']
    scoped = p.runtime._scoped_recipe(p.recipe, record['run_outputs'])
    filesystem = checkout_boundary.profile(scoped, p.store.directory, assessment=True)
    assert filesystem[str(p.store.directory)] == 'deny'
    assert not any(access in ('read', 'write') and Path(path).is_relative_to(p.store.directory)
                   for path, access in filesystem.items() if path.startswith('/'))
    _, delivered = finish(p, final)
    assert delivered['disposition'] == 'delivered'
    assert len(p.lifecycle.assignments()) == p.backend.count('turn/start') == 7
    assert len(p.runtime._native_for_attempt(final['attempt_id'])) == 2


@pytest.mark.parametrize('boundary', ['active', 'unconsumed'])
def test_changed_host_prerequisite_blocks_native_and_unconsumed_success(native_case, monkeypatch, boundary):
    p = native_case
    finish(p, checkpoint(p, corrective=False), edit=lambda: (p.source / 'README.txt').write_text('packaged\n'))
    final = claim(p.lifecycle)
    assert p.runtime.start(final)['state'] == 'active'
    if boundary == 'unconsumed':
        _, observation = finish(p, final, consume=False)
        assert observation['state'] == p.runtime.lookup(final['attempt_id'])['state'] == 'completed'
    record = p.runtime._get(final['attempt_id'])
    before = p.backend.count('turn/start')
    with p.runtime._db() as db:
        checks_before = db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0]
    raw = next(path for path in p.accepted['checks'][0]['raw_outputs'] if path.endswith('/stdout.log'))
    Path(raw).write_text(Path(raw).read_text() + 'changed before consumption\n')
    with pytest.raises(Rejected):
        p.runtime._verify_dependency_evidence(record)
    monkeypatch.setattr(reviewed_host, 'check', lambda *a, **k: pytest.fail('No native admission'))
    with pytest.raises(Rejected):
        p.runtime._check({**p.recipe, '_attempt_id': final['attempt_id']}, p.recipe['checks'][0],
                         Path(record['run_outputs']) / 'summary-tests', 'assessor', json.loads(final['candidate']))
    if boundary == 'unconsumed':
        assert p.runtime.lookup(final['attempt_id'])['state'] == 'unknown'
    assert p.backend.count('turn/start') == before
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == checks_before


def test_relocated_captures_cannot_downgrade_current_host_contract(native_case, monkeypatch):
    p = native_case
    packaging = checkpoint(p, corrective=False)
    context = p.lifecycle.dependency_review_context(packaging['assignment_id'])
    check = next(a for a in context['ancestors'] if a['assignment']['kind'] == 'assessment')['result']['checks'][0]
    for filename in list(check['raw_outputs']):
        path = Path(filename)
        if path.name in ('stdout.log', 'stderr.log'):
            relocated = Path(check['output']).parent / path.name
            relocated.write_bytes(path.read_bytes())
            check['raw_outputs'][str(relocated)] = check['raw_outputs'].pop(filename)
    # Exercise the complete consumer after lifecycle proof qualification. Even
    # same-byte files inside its output folder cannot select a weaker verifier.
    monkeypatch.setattr(p.lifecycle, 'dependency_review_context', lambda *_: context)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve',
                        lambda *a, **k: pytest.fail('Relocation must fail before reservation'))
    with pytest.raises(Rejected, match='exact host capture paths'):
        p.runtime.start(packaging)
    assert not p.runtime._get(packaging['attempt_id'])
    assert not (p.outputs / packaging['attempt_id']).exists()


@pytest.mark.parametrize('fault', ['raw-hash', 'transcript-hash', 'raw-symlink', 'ancestor-symlink',
    'raw-hardlink', 'row-missing', 'db-attempt', 'record-attempt', 'cross-work', 'host-root',
    'check-output', 'candidate', 'command', 'adapter', 'running', 'cleanup', 'host-receipt'])
def test_actual_host_evidence_tampering_rejects_before_packaging_reservation(native_case, monkeypatch, fault):
    p = native_case
    packaging = checkpoint(p, corrective=False)
    check = p.accepted['checks'][0]
    raw = Path(next(path for path in check['raw_outputs'] if path.endswith('/stdout.log')))
    row = p.runtime._native_get(check['native_check_id'])
    if fault in ('raw-hash', 'transcript-hash'):
        path = raw if fault == 'raw-hash' else Path(check['output'])
        path.write_bytes(path.read_bytes() + b'tampered')
    elif fault == 'raw-symlink':
        other = raw.with_name('substitute.log'); raw.rename(other); raw.symlink_to(other)
    elif fault == 'ancestor-symlink':
        original = raw.parent; other = original.with_name(original.name + '-substitute')
        original.rename(other); original.symlink_to(other, target_is_directory=True)
    elif fault == 'raw-hardlink':
        raw.with_name('substitute.log').hardlink_to(raw)
    elif fault == 'cross-work':
        record = p.runtime._get(p.accepted_assignment['attempt_id'])
        record['assignment']['work_id'] = 'OTHER-WORK'
        with p.runtime._db() as db:
            db.execute('UPDATE attempt SET record=? WHERE id=?', (canonical(record), record['attempt_id']))
    elif fault == 'host-receipt':
        path = Path(row['host_control']) / 'receipt.json'
        value = json.loads(path.read_text()); value['cleanup_verified'] = False
        path.write_text(canonical(value))
    else:
        if fault == 'record-attempt': row['attempt_id'] = 'another-attempt'
        elif fault == 'host-root': row['host_control'] = str(raw.parent.parent / 'other-check-host')
        elif fault == 'check-output': row['output'] = str(p.outputs / 'another-attempt' / check['name'])
        elif fault == 'candidate': row['candidate'] = {'identity': 'wrong'}
        elif fault == 'command': row['command'] = ['/usr/bin/false']
        elif fault == 'adapter': row['adapter'] = {}
        elif fault == 'running': row['phase'] = 'running'
        elif fault == 'cleanup': row['cleanup']['effects_reconciled'] = False
        with p.runtime._db() as db:
            if fault == 'row-missing': db.execute('DELETE FROM native_check WHERE id=?', (row['check_id'],))
            elif fault == 'db-attempt':
                db.execute("UPDATE native_check SET attempt_id='another-attempt' WHERE id=?", (row['check_id'],))
            else: db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(row), row['check_id']))
    before = p.backend.count('turn/start')
    with p.runtime._db() as db:
        attempts = db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0]
    def forbidden(*args, **kwargs):
        pytest.fail('Changed historical evidence must not reserve a packaging attempt')
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.budgets.reserve', forbidden)
    with pytest.raises((Rejected, RuntimeError)):
        p.runtime.start(packaging)
    assert p.runtime._get(packaging['attempt_id']) is None
    assert not (p.outputs / packaging['attempt_id']).exists()
    assert p.backend.count('turn/start') == before
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == attempts


@pytest.mark.parametrize('fault', ['wrong-check', 'wrong-attempt', 'wrong-root', 'escape', 'cross-work',
    'missing-stream', 'foreign-artifact', 'transcript-root', 'unknown-check', 'unselected-tier',
    'missing-id', 'malformed-raw', 'missing-candidate'])
def test_forged_paths_cannot_authorize_reads_from_a_broader_state_root(native_case, fault):
    p = native_case
    checkpoint(p, corrective=False)
    prior, recipe, accepted = copy.deepcopy((p.accepted_assignment, p.recipe, p.accepted))
    check = accepted['checks'][0]
    raw = next(path for path in check['raw_outputs'] if path.endswith('/stdout.log'))
    actual = Path(raw)
    work = prior['work_id']
    if fault in ('wrong-check', 'wrong-attempt', 'wrong-root', 'escape'):
        replacement = {'wrong-check': next(path for path in accepted['checks'][1]['raw_outputs'] if path.endswith('/stdout.log')),
            'wrong-attempt': str(p.store.directory / 'native-status' / 'other-attempt-host/stdout.log'),
            'wrong-root': str(p.store.directory / 'other-native-status' / actual.parent.name / actual.name),
            'escape': str(actual.parent / '..' / actual.parent.name / actual.name)}[fault]
        check['raw_outputs'][replacement] = check['raw_outputs'].pop(raw)
    elif fault == 'cross-work': work = 'OTHER-WORK'
    elif fault == 'missing-stream': check['raw_outputs'].pop(raw)
    elif fault == 'foreign-artifact': check['raw_outputs'][str(p.store.directory / 'aos.sqlite3')] = 'a' * 64
    elif fault == 'transcript-root': check['output'] = str(p.outputs / 'other-attempt/output.txt')
    elif fault == 'unknown-check': check['name'] = 'unknown'
    elif fault == 'unselected-tier': recipe['checkout_policy'].pop('native_validation')
    elif fault == 'missing-id': check.pop('native_check_id')
    elif fault == 'malformed-raw': check['raw_outputs'] = None
    elif fault == 'missing-candidate': accepted.pop('candidate')
    with pytest.raises(Rejected):
        reviewed_host.verify_dependency_check(p.runtime, work, prior, recipe, accepted, check,
                                               deadline=time.monotonic() + 10)
