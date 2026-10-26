"""Reusable evidence contract and exact technical qualification; no model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos import budgets
from agentic_tools.aos.checks import bindings, check_kind, passed, source_digest, validate_normalized
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate, inventory
from agentic_tools.aos.coordinator import capabilities
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest, now
from test_aos_codex_runtime import Backend, claim
from test_aos_lifecycle import contract
from test_aos_store import TEAM


def prepared_checks(tmp_path, monkeypatch, *, delegated=False, product=None, recipe_update=None):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir()
    outputs.mkdir()
    (source / 'example.py').write_text('def add(a,b):\n return a-b\n')
    (source / 'test_example.py').write_text('from example import add\ndef test_add():\n assert add(2,3)==5\n')
    (source / 'pytest.ini').write_text('[pytest]\n')
    recipe = {'source': str(source), 'outputs': str(outputs),
              'read_roots': sorted({str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve())}),
              'probe_interpreter': sys.executable, 'max_seconds': 60, 'requires_detached_processes': False,
              'mutable': ['example.py'], 'immutable': {k: v for k, v in inventory(source).items() if k != 'example.py'},
              'implementation_prompt': 'Fix add in example.py only.', 'assessment_prompt': 'Read source independently.',
              'checks': [{'name': 'compile', 'result_kind': 'build', 'argv': [sys.executable, '-c', 'print("build complete")'],
                          'scope': 'synthetic build', 'timeout_seconds': 10},
                         {'name': 'fixture', 'result_kind': 'pytest',
                          'argv': [sys.executable, '-m', 'pytest', '-q', '-c', 'pytest.ini', '--rootdir=.', 'test_example.py'],
                          'scope': 'synthetic tests', 'timeout_seconds': 10}]}
    if recipe_update:
        recipe_update(recipe)
    store = Store(tmp_path / 'state', (source, outputs))
    team = [{**TEAM[0], 'name': name, 'role': role} for name, role in
            [('writer', 'Implementer'), ('assessor', 'Independent assessor'),
             ('source-author', 'Tool implementer'), ('source-assessor', 'Independent tool assessor')]]
    store.introduce('intro', 'ADAPTERS', 'Disposable generic contract', ['source/'], team)
    bounds = contract({'digest': 'unused'}, baseline=candidate(source)['identity'],
                      required_checks=sorted({c['name'] for c in recipe['checks']}), runtime_recipe=recipe, runtime_recipe_digest=digest(recipe))
    bounds.pop('concept_digest')
    bounds.pop('concept_revision')
    if product:
        bounds['product_budget'] = {**product, 'ledger_directory': str(store.directory.resolve())}
    else:
        bounds['shared_qualification_budget'] = {'budget_id': budgets.QUALIFICATION_ID,
            'ledger_directory': str(store.directory.resolve()), 'max_assignments': 10, 'max_seconds': 3000}
    selected = {check_adapters.select(c).ADAPTER_ID for c in recipe['checks']}
    identities = {k: v for k, v in check_adapters.identities().items() if k in selected}
    if delegated:
        bounds['adapter_architecture'] = {'minimum_adapters': sorted(selected)}
        bounds['prerequisites']['independent_candidate'] = False
    else:
        bounds['check_adapters'] = identities
    concept = store.present('present', 'ADAPTERS', 1, 'ADAPTER-DI', 1, 'Disposable checks', ['build and tests'],
                            team, bounds['authority'], execution_envelope=bounds)
    cap = store.host_capability('test-host', 'SIMULATED:adapter-contract')
    challenge = store.begin_approval('approval', 'ADAPTERS', store.read('ADAPTERS')['revision'], concept['digest'], capability=cap)
    store.finish_approval(challenge['challenge_id'], 'accept', True, capability=cap, exact_answer='Synthetic approval',
                          source_channel='simulated-host', interaction_reference='simulated-contract')
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'ADAPTERS', {**bounds, 'concept_revision': 1, 'concept_digest': concept['digest']})
    backend = Backend()
    runtime = CodexRuntime(lifecycle, {'ADAPTERS': recipe}, transport_factory=backend.factory)
    monkeypatch.setattr(runtime, '_watch', lambda *_: None)
    monkeypatch.setattr(runtime, '_probe_sandbox', lambda *_: {'SIMULATED': True})
    package = store.read('ADAPTERS')['concepts'][0]['package']
    p = SimpleNamespace(source=source, outputs=outputs, store=store, lifecycle=lifecycle, runtime=runtime,
                        recipe=recipe, backend=backend, package=package, concept=concept, identities=identities, bounds=bounds)
    if not delegated:
        lifecycle.admit('admit', 'ADAPTERS')
    return p


@pytest.mark.parametrize('text,exit_code,recognized,selected,success', [
    ('running 3 tests\ntest result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n', 0, True, 3, True),
    ('running 3 tests\ntest result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n', 101, True, 3, False),
    ('running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 3 filtered out; finished in 0.01s\n', 0, True, 0, False),
    ('Finished build successfully', 0, False, 0, False),
    ('', 0, False, 0, False),
    ('running 3 tests\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n', 0, False, 2, False),
    ('running 1 test\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n', 101, True, 1, False),
    ('running 1 test\ntest result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out\n', 0, True, 1, False),
    ('running 1 test\ntest result: ok. 0 passed; 0 failed; 0 ignored; 1 measured; 0 filtered out\n', 0, False, 0, False),
])
def test_cargo_evidence_requires_recognized_positive_test_results(tmp_path, text, exit_code, recognized, selected, success):
    adapter = check_adapters.select({'result_kind': 'cargo-test'})
    evidence = adapter.interpret(tmp_path, text, 'build stderr', exit_code)
    assert evidence['recognized'] is recognized and evidence['selected'] == selected
    assert passed(evidence, exit_code) is success
    validate_normalized(evidence)


def test_multiple_cargo_suites_aggregate_raw_counts(tmp_path):
    text = ('running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n'
            'running 3 tests\ntest result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n')
    evidence = check_adapters.select({'result_kind': 'cargo-test'}).interpret(tmp_path, text, '', 0)
    assert evidence['selected'] == evidence['passed'] == 3 and passed(evidence, 0)


@pytest.mark.parametrize('xml,selected,success', [
    ('<testsuites><testsuite><testcase/></testsuite></testsuites>', 1, True),
    ('<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>', 1, False),
    ('<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>', 1, False),
    ('<testsuites><testsuite><testcase><system-out/></testcase></testsuite></testsuites>', 1, False),
    ('<testsuites><testsuite/></testsuites>', 0, False), ('malformed', 0, False),
])
def test_extracted_pytest_preserves_historical_acceptance(tmp_path, xml, selected, success):
    (tmp_path / 'junit.xml').write_text(xml)
    adapter = check_adapters.select({})
    evidence = adapter.interpret(tmp_path, 'raw output', '', 0)
    assert evidence['selected'] == selected and passed(evidence, 0) is success
    assert adapter.command(['python', '-m', 'pytest'], tmp_path)[-2:] == ['no:cacheprovider', '--junitxml=' + str(tmp_path / 'junit.xml')]


@pytest.mark.parametrize('exit_code,output,success', [(0, 'built', True), (1, 'failed', False), (0, '', False), (None, 'built', False)])
def test_build_evidence_never_claims_selected_tests(tmp_path, exit_code, output, success):
    evidence = check_adapters.select({'result_kind': 'build'}).interpret(tmp_path, '', output, exit_code)
    assert evidence['kind'] == 'build' and evidence['selected'] == evidence['passed'] == 0
    assert passed(evidence, exit_code) is success


@pytest.mark.parametrize('descriptor', [{'result_kind': 'unknown'}, {'adapter_id': 'not-installed'},
    {'result_kind': 'build', 'adapter_id': 'cargo-test-v1'}])
def test_unrecognized_or_mismatched_adapter_is_rejected(descriptor):
    with pytest.raises(ValueError):
        check_adapters.select(descriptor)


def test_tool_identity_covers_reusable_adapters():
    report = capabilities()
    assert report['source_digest'] == source_digest()
    assert set(report['check_adapters']) == {'pytest-v1', 'native-build-v1', 'cargo-test-v1'}
    assert all(v['version'] == 1 and v['source_digest'].startswith('sha256:') for v in report['check_adapters'].values())
    root = Path(__file__).parents[1] / 'src/agentic_tools'
    for package in ('aos', 'check_adapters'):
        for path in (root / package).glob('*.py'):
            assert 'Herdr' not in path.read_text()


def qualification(p, *, mutate=None, path=None):
    receipt = {'format': 'aos-check-adapters-v1', 'accepted': True, 'independent': True,
               'work_id': 'ADAPTERS', 'concept_digest': p.concept['digest'], 'runtime_recipe_digest': digest(p.recipe),
               'source_digest': source_digest(), 'check_adapters': p.identities,
               'source_author': 'source-author', 'assessor': 'source-assessor'}
    if mutate:
        receipt.update(mutate)
    path = path or p.store.directory / 'qualification.json'
    path.write_text(json.dumps(receipt))
    reference = 'aos-check-adapters-v1:' + hashlib.sha256(path.read_bytes()).hexdigest() + ':' + str(path)
    p.lifecycle.reconcile_prerequisites('qualified', 'ADAPTERS', p.store.read('ADAPTERS')['revision'], p.concept['digest'],
        {'independent_candidate': {'satisfied': True, 'reference': reference, 'observed_at': now()}})
    return path, reference


def test_missing_qualification_is_held_without_manufacturing_approval(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch, delegated=True)
    try:
        with pytest.raises(Rejected, match='qualified adapter'):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
        assert not p.backend.calls and p.runtime.budget_receipt()['attempts_reserved'] == 0
        assert len(p.store.read('ADAPTERS')['decisions']) == 1
    finally:
        p.runtime.close()


def test_exact_independent_qualification_binds_without_altering_approval(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch, delegated=True)
    try:
        before = p.store.read('ADAPTERS')['decisions']
        _, reference = qualification(p)
        selected = bindings(p.store, 'ADAPTERS', p.package, p.recipe)
        assert all(v['binding'] == reference for v in selected.values())
        assert p.store.read('ADAPTERS')['decisions'] == before
        p.lifecycle.admit('admit', 'ADAPTERS')
        p.runtime.start(claim(p))
        saved = p.runtime._get(p.lifecycle.assignments()[0]['attempt_id'])
        assert saved['check_adapters'] == selected
        assert p.runtime.budget_receipt()['attempts_reserved'] == 1
    finally:
        p.runtime.close()


@pytest.mark.parametrize('mutation', [{'accepted': False}, {'independent': False}, {'work_id': 'OTHER'},
    {'concept_digest': 'sha256:other'}, {'runtime_recipe_digest': 'sha256:other'}, {'source_digest': 'sha256:old'},
    {'assessor': 'source-author'}, {'assessor': 'worker-invented'}, {'check_adapters': {}}])
def test_wrong_or_nonindependent_qualification_cannot_enable_execution(tmp_path, monkeypatch, mutation):
    p = prepared_checks(tmp_path, monkeypatch, delegated=True)
    try:
        qualification(p, mutate=mutation)
        with pytest.raises(Rejected, match='qualification receipt'):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('defect', ['changed', 'worker-writable', 'symlink', 'unsatisfied'])
def test_qualification_reference_is_pinned_protected_and_current(tmp_path, monkeypatch, defect):
    p = prepared_checks(tmp_path, monkeypatch, delegated=True)
    try:
        path, _ = qualification(p, path=p.outputs / 'worker.json' if defect == 'worker-writable' else None)
        if defect == 'changed':
            path.write_text(path.read_text() + ' ')
        elif defect == 'symlink':
            moved = path.with_suffix('.actual')
            path.rename(moved)
            path.symlink_to(moved)
        elif defect == 'unsatisfied':
            p.lifecycle.reconcile_prerequisites('unqualified', 'ADAPTERS', p.store.read('ADAPTERS')['revision'], p.concept['digest'],
                {'independent_candidate': {'satisfied': False, 'reference': 'withdrawn', 'observed_at': now()}})
        with pytest.raises(Rejected):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
    finally:
        p.runtime.close()


def test_adapter_code_in_worker_scope_is_rejected(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        p.store.worker_writable_roots += (check_adapters.code_paths()[0].parent,)
        with pytest.raises(Rejected, match='worker-writable'):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('root', ['source', 'outputs'])
def test_actual_recipe_permissions_cannot_make_adapter_code_worker_writable(tmp_path, monkeypatch, root):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        recipe = {**p.recipe, root: str(check_adapters.code_paths()[0].parent)}
        with pytest.raises(Rejected, match='worker-writable'):
            bindings(p.store, 'ADAPTERS', p.package, recipe)
    finally:
        p.runtime.close()


def test_unqualified_version_or_digest_cannot_be_silently_selected(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        actual = check_adapters.identities()
        actual['pytest-v1']['version'] = 2
        monkeypatch.setattr(check_adapters, 'identities', lambda: actual)
        with pytest.raises(Rejected, match='identity/version'):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
    finally:
        p.runtime.close()


def test_native_build_and_pytest_run_as_distinct_exact_checks(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
        frozen = candidate(p.source)
        scoped = {**p.recipe, '_adapter_bindings': bindings(p.store, 'ADAPTERS', p.package, p.recipe)}
        results = [p.runtime._check(scoped, c, p.outputs / c['name'], 'assessor', frozen) for c in p.recipe['checks']]
        assert [r['result'] for r in results] == ['pass', 'pass']
        assert [r['kind'] for r in results] == ['build', 'tests']
        assert [r['selected'] for r in results] == [0, 1]
        assert results[0]['command'] == p.recipe['checks'][0]['argv']
        assert all(r['candidate'] == frozen and r['cleanup']['effects_reconciled'] for r in results)
        assert all(r['adapter']['version'] == 1 and r['raw_outputs'] for r in results)
        p.runtime._verify_check_receipt(results[0])
        p.runtime._verify_check_receipt(results[1])
        junit = p.outputs / 'fixture/junit.xml'
        assert str(junit) in results[1]['raw_outputs']
        junit.write_text('<testsuites/>')
        with pytest.raises(Rejected, match='raw check output'):
            p.runtime._verify_check_receipt(results[1])
        Path(results[0]['output']).write_text('altered')
        with pytest.raises(Rejected, match='raw check output'):
            p.runtime._verify_check_receipt(results[0])
    finally:
        p.runtime.close()


def test_scoped_output_substitution_has_no_shell_interpretation(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        check = {**p.recipe['checks'][0], 'argv': [sys.executable, '-c', 'import sys;print(sys.argv[1:])',
                                                '{output}/target', '$(touch forbidden)', '; exit 9']}
        scoped = {**p.recipe, '_adapter_bindings': bindings(p.store, 'ADAPTERS', p.package, p.recipe)}
        result = p.runtime._check(scoped, check, p.outputs / 'literal', 'assessor', candidate(p.source))
        assert result['result'] == 'pass' and result['command'][-3:] == [str(p.outputs / 'target'), '$(touch forbidden)', '; exit 9']
        assert not (p.source / 'forbidden').exists()
    finally:
        p.runtime.close()


def ledger():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE attempt (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
    budgets.initialize(db)
    return db


def reserve_attempt(db, budget, source='/project/one', *, seconds=60, aid='job'):
    with db:
        db.execute('BEGIN IMMEDIATE')
        budgets.reserve(db, budget, {'source': source, 'max_seconds': seconds})
        db.execute('INSERT INTO attempt VALUES(?,?)', (aid, canonical({'budget_id': budget['budget_id'], 'budget_seconds': seconds})))


def product_budget(name='one', **overrides):
    return {'budget_id': 'product-' + name, 'product_id': name, 'kind': 'product',
            'max_assignments': 2, 'max_job_seconds': 60, 'max_seconds': 120, **overrides}


def test_legacy_seven_reservations_migrate_without_reset_and_use_exact_extension():
    db = ledger()
    for index in range(7):
        db.execute('INSERT INTO attempt VALUES(?,?)', ('old-' + str(index), canonical({'phase': 'unknown'})))
    db.commit()
    before = budgets.receipt(db, Path('/state'))
    assert before['attempts_reserved'] == 7 and before['seconds_reserved'] == 2100
    qualified = {'budget_id': budgets.QUALIFICATION_ID, 'kind': 'qualification',
                 'max_assignments': 10, 'max_seconds': 3000, 'max_job_seconds': 300}
    reserve_attempt(db, qualified, seconds=300, aid='new-1')
    reserve_attempt(db, qualified, seconds=300, aid='new-2')
    after = budgets.receipt(db, Path('/state'))
    assert after['attempts_reserved'] == 9 and after['seconds_reserved'] == 2700
    assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 9
    assert after['limits']['max_assignments'] == 10


def test_product_budgets_are_separate_and_revisions_or_renames_cannot_reset():
    db = ledger()
    one, two = product_budget(), product_budget('two')
    reserve_attempt(db, one)
    reserve_attempt(db, two, '/project/two', aid='job-two')
    assert budgets.receipt(db, Path('/state'), one['budget_id'])['attempts_reserved'] == 1
    assert budgets.receipt(db, Path('/state'))['attempts_reserved'] == 0
    for changed, source in [(product_budget('renamed', product_id='one'), '/project/one'),
                            (product_budget('renamed'), '/project/one'),
                            (one, '/project/renamed'),
                            (product_budget(max_assignments=3), '/project/one')]:
        with pytest.raises(Rejected):
            reserve_attempt(db, changed, source, aid='forbidden')
    reserve_attempt(db, one, aid='job-again')
    with pytest.raises(Rejected, match='reservation exhausted'):
        reserve_attempt(db, one, aid='exhausted')


@pytest.mark.parametrize('field,value', [('max_job_seconds', 0), ('max_job_seconds', True),
    ('product_id', ''), ('budget_id', budgets.QUALIFICATION_ID), ('max_seconds', 0)])
def test_product_allowance_requires_all_explicit_bounds(field, value):
    budget = {**product_budget(), 'ledger_directory': '/state', field: value}
    with pytest.raises(Rejected):
        budgets.approved_budget({'product_budget': budget}, Path('/state'))


def test_product_aggregate_and_job_limit_fail_before_reservation():
    db = ledger()
    one = product_budget(max_assignments=8, max_seconds=90)
    reserve_attempt(db, one, seconds=60)
    with pytest.raises(Rejected, match='reservation exhausted'):
        reserve_attempt(db, one, seconds=31, aid='too-much-total')
    with pytest.raises(Rejected, match='per-job'):
        reserve_attempt(db, one, seconds=61, aid='too-much-job')
    assert budgets.receipt(db, Path('/state'), one['budget_id'])['attempts_reserved'] == 1


@pytest.mark.parametrize('bounds', [{}, {'product_budget': {}, 'shared_qualification_budget': {}},
    {'product_budget': {'budget_id': 'one'}, 'shared_qualification_budget': {'budget_id': budgets.QUALIFICATION_ID}}])
def test_no_default_or_ambiguous_product_allowance(bounds):
    with pytest.raises(Rejected, match='Exactly one explicit'):
        budgets.approved_budget(bounds, Path('/state'))


def test_probe_interpreter_is_independent_of_the_check_command(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    captured = []
    def run(argv, **kwargs):
        captured.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'denied': True}))
    # Use the real method without starting a model or changing its interpreter.
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.subprocess.run', run)
    try:
        CodexRuntime._probe_sandbox(p.runtime, {'assignment': {'kind': 'assessment'}, 'attempt_id': 'probe'}, p.recipe)
        assert captured[0][captured[0].index('--include-managed-config') + 3] == sys.executable
    finally:
        p.runtime.close()


def test_typed_recipe_without_explicit_probe_interpreter_cannot_launch(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=lambda recipe: recipe.pop('probe_interpreter'))
    try:
        with pytest.raises(Rejected, match='probe interpreter'):
            p.runtime.start(claim(p))
        assert not p.backend.calls and p.runtime.budget_receipt()['attempts_reserved'] == 0
    finally:
        p.runtime.close()


def test_duplicate_recipe_checks_fail_before_transport(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=lambda recipe: recipe['checks'].append(copy.deepcopy(recipe['checks'][0])))
    try:
        with pytest.raises(Rejected, match='Exact native checks'):
            p.runtime.start(claim(p))
        assert not p.backend.calls
    finally:
        p.runtime.close()


def test_product_failed_start_is_reserved_and_survives_runtime_restart(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch, product=product_budget())
    try:
        p.backend.lost_thread_ack = True
        assignment = claim(p)
        assert p.runtime.start(assignment)['state'] == 'unknown'
        original = p.runtime.budget_receipt('product-one')
        assert original['attempts_reserved'] == 1 and original['seconds_reserved'] == 60
        assert p.runtime.budget_receipt()['attempts_reserved'] == 0
        reopened = CodexRuntime(p.lifecycle, {'ADAPTERS': p.recipe}, transport_factory=p.backend.factory)
        try:
            assert reopened.start(assignment)['state'] == 'unknown'
            assert reopened.budget_receipt('product-one') == original
            assert p.backend.count('thread/start') == 1
        finally:
            reopened.close()
    finally:
        p.runtime.close()


@pytest.mark.parametrize('mutation', ['none', 'build-as-test', 'test-as-build', 'zero-tests', 'bad-counts',
                                      'wrong-adapter', 'other-qualified-test-adapter', 'wrong-candidate', 'missing-raw-output',
                                      'missing-kind', 'missing-adapter'])
def test_lifecycle_accepts_only_matching_typed_obligations(tmp_path, monkeypatch, mutation):
    def additional_test_adapter(recipe):
        recipe['checks'].append({'name': 'other-cargo', 'result_kind': 'cargo-test',
                                 'argv': [sys.executable, '-c', 'print("synthetic Cargo output")'],
                                 'timeout_seconds': 10, 'scope': 'unrelated additional test adapter'})
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=additional_test_adapter)
    try:
        frozen = candidate(p.source)
        qualified = bindings(p.store, 'ADAPTERS', p.package, p.recipe)
        checks = []
        for descriptor in p.recipe['checks']:
            adapter = check_adapters.select(descriptor)
            count = 0 if adapter.KIND == 'build' else 1
            checks.append({'name': descriptor['name'], 'kind': adapter.KIND, 'recognized': True,
                           'selected': count, 'passed': count, 'failed': 0, 'skipped': 0, 'errors': 0,
                           'result': 'pass', 'exit_code': 0, 'adapter': qualified[adapter.ADAPTER_ID],
                           'command': descriptor['argv'], 'environment': 'SIMULATED', 'executor': 'assessor',
                           'output': 'SIMULATED:output', 'output_sha256': 'SIMULATED:hash', 'raw_outputs': {'SIMULATED': 'hash'},
                           'candidate': frozen, 'scope': descriptor['scope'], 'limits': 'Synthetic contract only'})
        if mutation == 'build-as-test':
            checks[1] = {**checks[0], 'name': 'fixture'}
        elif mutation == 'test-as-build':
            checks[0] = {**checks[1], 'name': 'compile'}
        elif mutation == 'zero-tests':
            checks[1].update(selected=0, passed=0)
        elif mutation == 'bad-counts':
            checks[1]['passed'] = 2
        elif mutation == 'wrong-adapter':
            checks[1]['adapter'] = {**checks[1]['adapter'], 'version': 999}
        elif mutation == 'other-qualified-test-adapter':
            checks[1]['adapter'] = qualified['cargo-test-v1']
        elif mutation == 'wrong-candidate':
            checks[1]['candidate'] = {'identity': 'other'}
        elif mutation == 'missing-raw-output':
            checks[1].pop('raw_outputs')
        elif mutation == 'missing-kind':
            checks[1].pop('kind')
        elif mutation == 'missing-adapter':
            checks[1].pop('adapter')
        result = {'candidate': frozen, 'observed_source_identity': frozen['source_identity'], 'executor': 'assessor',
                  'initial_reports': {'execution': ['SIMULATED'], 'review': 'SIMULATED', 'independent': True}, 'checks': checks}
        assignment = {'candidate': canonical(frozen), 'work_id': 'ADAPTERS', 'concept_revision': 1, 'concept_digest': p.concept['digest']}
        error = p.lifecycle._assessment_error(p.bounds, assignment, result)
        assert (error is None) is (mutation == 'none')
    finally:
        p.runtime.close()


@pytest.mark.parametrize('mutation', [{'selected': True}, {'selected': -1}, {'passed': 2}, {'recognized': 1}, {'kind': 'unknown'}])
def test_normalized_contract_rejects_malformed_counts(mutation):
    evidence = {'kind': 'tests', 'recognized': True, 'selected': 1, 'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0, **mutation}
    with pytest.raises(Rejected):
        validate_normalized(evidence)


def test_boolean_exit_status_is_not_terminal_success():
    evidence = {'kind': 'tests', 'recognized': True, 'selected': 1, 'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0}
    assert not passed(evidence, False)


def test_raw_stdout_drift_blocks_receipt_replay(tmp_path, monkeypatch):
    p = prepared_checks(tmp_path, monkeypatch)
    try:
        scoped = {**p.recipe, '_adapter_bindings': bindings(p.store, 'ADAPTERS', p.package, p.recipe)}
        descriptor = p.recipe['checks'][0]
        result = p.runtime._check(scoped, descriptor, p.outputs / 'build', 'assessor', candidate(p.source))
        Path(next(iter(result['raw_outputs']))).write_text('substituted raw output')
        with pytest.raises(Rejected, match='raw check output'):
            p.runtime._check(scoped, descriptor, p.outputs / 'build', 'assessor', candidate(p.source))
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 1
    finally:
        p.runtime.close()
