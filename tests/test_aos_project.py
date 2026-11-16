"""Project preparation binds facts without spending or granting product authority."""
from __future__ import annotations
from di_plan_fixture import synthetic_plan

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import budgets, project
from agentic_tools.aos.store import canonical, digest
from test_aos_lifecycle import contract
from test_aos_store import TEAM


def test_project_checks_bind_exact_split_bun_commands():
    check = {'name': 'native', 'argv': ['just', 'check'], 'timeout_seconds': 30,
             'scope': 'Declared test suites', 'result_kind': 'native-tests',
             'adapter_id': 'native-tests-v1', 'suite_counts': {'bun': 2},
             'bun_commands': ['bun test first.test.ts', 'bun test ./scripts/docs']}
    selected = project._checks([check], 30)['native']
    check['bun_commands'].reverse()
    assert selected.bun_commands == ('bun test first.test.ts', 'bun test ./scripts/docs')
    assert selected.command(check['argv'], Path('.')) == ['just', 'check']


@pytest.mark.parametrize('kind', ['pytest', 'build', 'cargo-test'])
def test_project_checks_reject_bun_commands_on_other_adapters(kind):
    check = {'name': 'native', 'argv': ['check'], 'timeout_seconds': 30,
             'scope': 'Declared test suites', 'result_kind': kind,
             'bun_commands': ['bun test first.test.ts']}
    with pytest.raises(project.Rejected, match='adapter'):
        project._checks([check], 30)


@pytest.mark.parametrize('counts,commands', [({'bun': 2}, ['bun test first.test.ts']),
    ({'unittest': 1}, ['bun test first.test.ts']), ({'bun': 1}, None)])
def test_project_checks_reject_inconsistent_split_bun_descriptor(counts, commands):
    check = {'name': 'native', 'argv': ['just', 'check'], 'timeout_seconds': 30,
             'scope': 'Declared test suites', 'result_kind': 'native-tests',
             'suite_counts': counts, 'bun_commands': commands}
    with pytest.raises(project.Rejected, match='adapter'):
        project._checks([check], 30)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source, outputs, state, control = [root / name for name in ('source', 'outputs', 'state', 'control')]
    for path in (source, outputs, state, control):
        path.mkdir(mode=0o700)
    (source / 'app.py').write_text('answer = 1\n')
    temporary = root / 'native-tmp'
    temporary.mkdir()
    monkeypatch.setattr(project.checkout_boundary, '_temporary_roots', lambda: (temporary, Path('/private/tmp')))
    monkeypatch.setattr(project.ssl, 'SSLContext', lambda *args: SimpleNamespace(load_cert_chain=lambda *args, **kwargs: None))
    certificate, key = control / 'certificate.pem', control / 'key.pem'
    certificate.write_text('fixture public certificate\n')
    key.write_text('fixture private key; never emitted\n')
    key.chmod(0o600)
    check = {'name': 'unit', 'argv': ['/usr/bin/true'], 'timeout_seconds': 60,
             'scope': 'Fixture unit check', 'result_kind': 'build'}
    recipe = {'execution_policy': 'isolated-checkout-v1', 'source': str(source), 'outputs': str(outputs),
              'read_roots': [], 'checks': [check], 'max_seconds': 60, 'requires_detached_processes': False,
              'implementation_prompt': 'Implement the exact fixture change', 'assessment_prompt': 'Independently assess it',
              'probe_interpreter': sys.executable, 'native_process_ownership': 'sandbox-census-v1',
              'instruction_manifest': {'version': 1, 'configs': [], 'chain': [], 'support': []},
              'checkout_policy': {'change_roots': ['app.py'], 'protected_roots': [], 'endpoint_limits': {
                  'minimum_free_bytes': 1, 'max_owned_growth_bytes': 10000, 'max_output_bytes': 10000}}}
    config = {'format': 'aos-project-v1', 'state_dir': str(state), 'readiness_dir': str(root / 'readiness'),
              'worker_writable_roots': [str(source), str(outputs)], 'recipe': recipe,
              'readiness': {'max_seconds': 60, 'max_attempts': 2, 'checks': [copy.deepcopy(check)]},
              'decision_service': {'certificate': str(certificate), 'key': str(key), 'port': 8443,
                                   'qualification_reference': 'existing-host-proof'}}
    team = [{**TEAM[0], 'name': name} for name in ('writer', 'assessor')]
    bounds = contract({'digest': 'unused'}, baseline='auto', required_checks=['unit'], max_assignments=4,
                      max_repairs=1, prerequisites={'host_readiness': False, 'design': True})
    bounds.pop('concept_digest')
    bounds.pop('concept_revision')
    bounds['product_budget'] = {'budget_id': 'project-fixture-v1', 'product_id': 'project-fixture',
                                'ledger_directory': str(state), 'max_assignments': 4,
                                'max_seconds': 240, 'max_job_seconds': 60}
    request = {'introduction': {'operation_id': 'intro-fixture', 'work_id': 'FIXTURE', 'title': 'Fixture',
                               'structure': ['app.py'], 'team': copy.deepcopy(team)},
               'concept': {'operation_id': 'concept-fixture', 'work_id': 'FIXTURE', 'expected_work_revision': 1,
                           'package_id': 'FIXTURE-DI', 'revision': 1, 'body': 'Exact user outcome',
                           'acceptance': ['Exact acceptance'], 'team_envelope': copy.deepcopy(team),
                           'authority': bounds['authority'], 'execution_envelope': bounds, 'di_plan': synthetic_plan(bounds)}}
    p = SimpleNamespace(config=config, request=request, state=state, source=source, outputs=outputs,
                        control=control, project_file=control / 'project.json', request_file=control / 'work.json',
                        temporary=temporary, calls=[], result_change=lambda value: None)
    monkeypatch.setattr(project.instructions, 'validate', lambda *args, **kwargs: [])
    monkeypatch.setattr(project.instructions, 'readback', lambda manifest, *args, **kwargs:
                        {'manifest': copy.deepcopy(manifest), 'configuration_transition': None})
    # Platform ownership is qualified separately; this suite launches no process.
    monkeypatch.setattr(project.native_processes, 'validate', lambda recipe: None)
    def runner(recipe, **kwargs):
        p.calls.append((copy.deepcopy(recipe), kwargs))
        inventory = {'app.py': hashlib.sha256((source / 'app.py').read_bytes()).hexdigest()}
        baseline = {'identity': digest(inventory), 'source_identity': digest(inventory), 'inventory': inventory}
        result = {'ready': True, 'status': 'passed', 'cleanup_verified': True, 'simulated': False,
                  'product_attempts': 0, 'product_reservations': 0, 'model_turns': 0,
                  'prepared_recipe': copy.deepcopy(recipe), 'recipe_digest': digest(recipe),
                  'source_before': copy.deepcopy(baseline), 'source_after': baseline,
                  'runtime_identity': {'runtime_source_digest': project.source_digest()}}
        p.result_change(result)
        return result
    def save():
        p.project_file.write_text(json.dumps(p.config))
        p.request_file.write_text(json.dumps(p.request))
    p.save = save
    p.runner = runner
    p.run = lambda: project.prepare(p.project_file, p.request_file, readiness_runner=runner)
    p.save()
    return p


def test_prepares_exact_existing_request_without_product_side_effects(setup, monkeypatch):
    p = setup
    from agentic_tools.aos.store import Store
    from agentic_tools.aos.codex_runtime import CodexRuntime
    monkeypatch.setattr(Store, '__init__', lambda *args, **kwargs: pytest.fail('No Store during preparation'))
    monkeypatch.setattr(CodexRuntime, '__init__', lambda *args, **kwargs: pytest.fail('No product runtime'))
    original = copy.deepcopy(p.request)
    result = p.run()
    assert result['ready'], result['blocker']
    assert not result['approval_recorded'] and not result['reservation_performed']
    assert list(p.state.iterdir()) == [] and len(p.calls) == 1
    assert result['introduction'] == original['introduction']
    concept, original_concept = result['concept'], original['concept']
    assert {k: v for k, v in concept.items() if k != 'execution_envelope'} == {
        k: v for k, v in original_concept.items() if k != 'execution_envelope'}
    final = concept['execution_envelope']
    derived = {'baseline', 'prerequisites', 'runtime_recipe', 'runtime_recipe_digest', 'execution_policy',
               'change_roots', 'check_adapters'}
    assert {k: v for k, v in final.items() if k not in derived} == {
        k: v for k, v in original_concept['execution_envelope'].items() if k not in derived}
    assert final['prerequisites'] == {'host_readiness': True, 'design': True}
    assert final['runtime_recipe_digest'] == digest(final['runtime_recipe'])
    assert final['change_roots'] == ['app.py']
    for name, path in [('project', p.project_file), ('request', p.request_file)]:
        assert result['input_bindings'][name]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result['finish_capacity']['required_assignments'] == 4
    assert result['finish_capacity']['required_seconds'] == 240
    assert 'fixture private key' not in json.dumps(result)
    assert p.request == original


@pytest.mark.parametrize('edit', [
    lambda p: p.config.update(unknown=True),
    lambda p: p.config['readiness'].update(extra=True),
    lambda p: p.config['readiness'].update(max_seconds=True),
    lambda p: p.config['decision_service'].update(port=0),
    lambda p: p.config['recipe'].update(unrecognized=True),
    lambda p: p.config['recipe'].update(checkout_policy=[]),
    lambda p: p.config['recipe']['checkout_policy'].update(native_environment=[]),
    lambda p: p.config['recipe'].pop('probe_interpreter'),
    lambda p: p.config['recipe']['checks'][0].update(timeout_seconds=61),
    lambda p: p.config['recipe']['checks'][0].update(unknown=True),
    lambda p: p.request.update(extra=True),
    lambda p: p.request['concept'].update(work_id='OTHER'),
    lambda p: p.request['concept'].update(authority='Changed'),
    lambda p: p.request['concept']['execution_envelope'].update(unknown=True),
    lambda p: p.request['concept']['execution_envelope'].update(model='other-model'),
    lambda p: p.request['concept']['execution_envelope'].update(change_roots=['.']),
    lambda p: p.request['concept']['execution_envelope'].update(runtime_recipe={'broad': True}),
    lambda p: p.request['concept']['execution_envelope']['prerequisites'].update(design=False),
    lambda p: p.request['concept']['execution_envelope']['prerequisites'].pop('host_readiness'),
    lambda p: p.request['concept']['team_envelope'][0].update(scope='Changed'),
])
def test_invalid_configuration_never_calls_readiness(setup, edit):
    p = setup
    edit(p)
    p.save()
    result = p.run()
    assert not result['ready'] and result['blocker']
    assert p.calls == [] and list(p.state.iterdir()) == []


@pytest.mark.parametrize('raw', ['{"format":"aos-project-v1","format":"duplicate"}', '{"n":NaN}', '{not JSON'])
def test_duplicate_nonfinite_and_malformed_json_stop_before_readiness(setup, raw):
    p = setup
    p.project_file.write_text(raw)
    result = p.run()
    assert not result['ready'] and p.calls == []


@pytest.mark.parametrize('field,value', [('max_assignments', 3), ('max_seconds', 239)])
def test_finish_capacity_before_readiness(setup, field, value):
    p = setup
    p.request['concept']['execution_envelope']['product_budget'][field] = value
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'project_finish_capacity'
    assert result['finish_capacity']['reservation_performed'] is False
    assert p.calls == [] and list(p.state.iterdir()) == []


def test_retained_failed_attempts_still_consume_shared_allowance(setup):
    p = setup
    budget = budgets.approved_budget(p.request['concept']['execution_envelope'], p.state)
    path = p.state / 'codex-runtime.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE attempt(id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        budgets.initialize(db)
        budgets.reserve(db, budget, {'source': str(p.source), 'max_seconds': 60})
        db.execute('INSERT INTO attempt VALUES(?,?)', ('failed', canonical({
            'budget_id': budget['budget_id'], 'budget_seconds': 60, 'phase': 'failed'})))
    before = path.read_bytes()
    result = p.run()
    assert result['blocker']['code'] == 'project_finish_capacity'
    assert result['finish_capacity']['attempts_reserved'] == 1
    assert result['finish_capacity']['seconds_reserved'] == 60
    assert path.read_bytes() == before and p.calls == []


def test_existing_lifecycle_assignments_cannot_be_reset_to_zero(setup):
    p = setup
    path = p.state / 'aos.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE work(work_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE assignment(work_id TEXT NOT NULL)')
        db.execute("INSERT INTO work VALUES('FIXTURE')")
        db.execute("INSERT INTO assignment VALUES('FIXTURE')")
    path.chmod(0o600)
    before = path.read_bytes()
    result = p.run()
    assert result['blocker']['code'] == 'project_finish_capacity'
    assert result['finish_capacity']['lifecycle_remaining_assignments'] == 3
    assert path.read_bytes() == before and p.calls == []


@pytest.mark.parametrize('name', ['state_dir', 'readiness_dir'])
def test_controls_cannot_be_inside_worker_root(setup, name):
    p = setup
    p.config[name] = str(p.source / 'controls')
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_broad_project_read_grant_cannot_expose_control_folder(setup):
    p = setup
    p.config['recipe']['read_roots'] = [str(p.control.parent)]
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_private_key_is_protected_even_under_implicit_native_temporary_root(setup):
    p = setup
    key = p.temporary / 'key.pem'
    key.write_text('private fixture value')
    key.chmod(0o600)
    p.config['decision_service']['key'] = str(key)
    p.save()
    result = p.run()
    assert result['ready'], result['blocker']
    recipe = result['concept']['execution_envelope']['runtime_recipe']
    profile = project.checkout_boundary.profile(recipe, p.state, native=True)
    assert profile[p.config['decision_service']['key']] == 'deny'
    assert profile[str(p.state)] == 'deny'
    assert profile[str(Path(p.config['readiness_dir']) / 'control')] == 'deny'
    assert profile[str(p.project_file)] == profile[str(p.request_file)] == 'deny'


def test_alias_control_path_is_rejected_before_readiness(setup):
    p = setup
    alias = p.control / 'alias'
    alias.symlink_to(p.state, target_is_directory=True)
    p.config['state_dir'] = str(alias)
    p.save()
    assert not p.run()['ready'] and p.calls == []


def test_missing_owned_directories_are_reported_without_creating_them(setup):
    p = setup
    p.outputs.rmdir()
    result = p.run()
    assert result['blocker']['code'] == 'project_directories'
    assert result['required_directories'] == [str(p.outputs)]
    assert not p.outputs.exists() and p.calls == []


@pytest.mark.parametrize('change', [
    lambda r: r.update(ready=False),
    lambda r: r.update(simulated=True),
    lambda r: r.update(cleanup_verified=False),
    lambda r: r.update(product_attempts=1),
    lambda r: r.update(product_reservations=1),
    lambda r: r.update(model_turns=1),
    lambda r: r.update(source_after={**r['source_after'], 'identity': 'different'}),
    lambda r: r['runtime_identity'].update(runtime_source_digest='old'),
    lambda r: r.update(runtime_identity=[]),
])
def test_incomplete_or_simulated_readiness_cannot_prepare_product_request(setup, change):
    p = setup
    p.result_change = change
    result = p.run()
    assert not result['ready'] and result['blocker']['code'] == 'project_readiness'
    assert list(p.state.iterdir()) == []


def test_readiness_cannot_broaden_scope_even_with_consistent_recipe_digest(setup):
    p = setup
    def change(receipt):
        receipt['prepared_recipe']['checkout_policy']['change_roots'] = ['.']
        receipt['recipe_digest'] = digest(receipt['prepared_recipe'])
    p.result_change = change
    result = p.run()
    assert result['blocker']['code'] == 'project_scope'
    assert result['concept'] == p.request['concept']


def test_inputs_changed_during_readiness_do_not_become_ready(setup):
    p = setup
    p.result_change = lambda _: p.request_file.write_text(p.request_file.read_text() + '\n')
    result = p.run()
    assert result['blocker']['code'] == 'project_input_drift'


def test_fixed_requested_baseline_must_match_observed_source(setup):
    p = setup
    p.request['concept']['execution_envelope']['baseline'] = 'sha256:other'
    p.save()
    assert p.run()['blocker']['code'] == 'project_baseline'


def test_repeated_preparation_keeps_operations_and_never_creates_product_journal(setup):
    p = setup
    first, second = p.run(), p.run()
    assert first == second and first['ready'], first['blocker']
    assert first['introduction']['operation_id'] == 'intro-fixture'
    assert first['concept']['operation_id'] == 'concept-fixture'
    assert list(p.state.iterdir()) == []


def test_capacity_uses_actual_job_duration_with_larger_envelope_ceiling(setup):
    p = setup
    p.request['concept']['execution_envelope']['max_seconds'] = 1800
    p.save()
    result = p.run()
    assert result['ready'], result['blocker']
    assert result['finish_capacity']['required_seconds'] == 240


def test_missing_certificate_stops_before_readiness(setup):
    p = setup
    Path(p.config['decision_service']['certificate']).unlink()
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_mismatched_tls_pair_stops_before_readiness(setup, monkeypatch):
    p = setup
    def unusable(*args, **kwargs):
        raise OSError('fixture certificate mismatch')
    monkeypatch.setattr(project.ssl, 'SSLContext', lambda *args: SimpleNamespace(load_cert_chain=unusable))
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_retained_readiness_cannot_live_under_implicit_temporary_grant(setup):
    p = setup
    p.config['readiness_dir'] = str(p.temporary / 'readiness')
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_readiness_can_consume_declared_registration_without_configuration_change(setup, monkeypatch):
    p = setup
    manifest = p.config['recipe']['instruction_manifest']
    manifest['startup_project_registration'] = {'explicit': 'fixture'}
    p.save()
    actual = {key: value for key, value in manifest.items() if key != 'startup_project_registration'}
    monkeypatch.setattr(project.instructions, 'readback', lambda *args, **kwargs:
                        {'manifest': copy.deepcopy(actual), 'configuration_transition': None})
    def normalized(receipt):
        receipt['prepared_recipe']['instruction_manifest'] = copy.deepcopy(actual)
        receipt['recipe_digest'] = digest(receipt['prepared_recipe'])
    p.result_change = normalized
    result = p.run()
    assert result['ready'], result['blocker']
    assert result['concept']['execution_envelope']['runtime_recipe']['instruction_manifest'] == actual


def test_adapter_change_during_readiness_cannot_create_stale_approval_packet(setup, monkeypatch):
    p = setup
    original = project.check_adapters.identities()
    def changed(receipt):
        updated = copy.deepcopy(original)
        updated['native-build-v1']['source_digest'] = 'different'
        monkeypatch.setattr(project.check_adapters, 'identities', lambda: updated)
    p.result_change = changed
    assert p.run()['blocker']['code'] == 'project_adapter_drift'


def test_temporary_exclusion_uses_physical_alias_aware_overlap(setup, monkeypatch):
    p = setup
    original = project.native_paths.overlaps
    readiness = Path(p.config['readiness_dir'])
    def alias(left, right, **kwargs):
        return (left == readiness and right == p.temporary) or original(left, right, **kwargs)
    monkeypatch.setattr(project.native_paths, 'overlaps', alias)
    result = p.run()
    assert result['blocker']['code'] == 'project_controls' and p.calls == []


def test_readiness_failure_keeps_one_actionable_explanation(setup):
    p = setup
    p.result_change = lambda r: r.update(ready=False, blockers=[
        {'code': 'native_check', 'detail': 'Configured compiler is unavailable; select the installed tool root'},
        {'code': 'later', 'detail': 'Additional retained detail'}])
    result = p.run()
    assert 'Configured compiler is unavailable' in result['blocker']['detail']
    assert len(result['readiness']['blockers']) == 2
    assert not result['ready']


def test_missing_instruction_manifest_is_discovered_without_raising_byte_limit(setup, monkeypatch):
    p = setup
    manifest = p.config['recipe'].pop('instruction_manifest')
    manifest.update(codex_home=str(p.control), project_root=str(p.source))
    p.save()
    calls = []
    def discover(source, **kwargs):
        calls.append((source, kwargs))
        return copy.deepcopy(manifest)
    monkeypatch.setattr(project.instructions, 'discover', discover)
    result = p.run()
    assert result['ready'], result['blocker']
    assert calls == [(p.source, {})]
    assert result['concept']['execution_envelope']['runtime_recipe']['instruction_manifest'] == manifest
    assert 'instruction_manifest' not in p.config['recipe']


def test_instruction_discovery_failure_is_a_blocker_before_readiness(setup, monkeypatch):
    p = setup
    p.config['recipe'].pop('instruction_manifest')
    p.save()
    def unavailable(*args, **kwargs):
        raise project.Rejected('instruction_limit', 'Current instruction chain exceeds the configured byte limit')
    monkeypatch.setattr(project.instructions, 'discover', unavailable)
    result = p.run()
    assert result['blocker']['code'] == 'instruction_limit' and p.calls == []
    assert 'configured byte limit' in result['blocker']['detail']


def test_explicit_instruction_manifest_is_not_silently_rediscovered(setup, monkeypatch):
    p = setup
    monkeypatch.setattr(project.instructions, 'discover', lambda *args, **kwargs:
                        pytest.fail('An explicitly supplied manifest must be retained and verified'))
    assert p.run()['ready']


@pytest.mark.parametrize('registered', [True, False])
def test_automatic_instruction_registration_is_only_for_an_absent_project_entry(setup, monkeypatch, registered):
    p = setup
    manifest = p.config['recipe'].pop('instruction_manifest')
    manifest.update(codex_home=str(p.control), project_root=str(p.source),
                    configs=[{'path': str(p.control / 'config.toml')}])
    p.save()
    calls = []
    def discover(source, **kwargs):
        calls.append(kwargs)
        result = copy.deepcopy(manifest)
        if kwargs.get('allow_project_registration'):
            result['startup_project_registration'] = {'explicit': 'fixture'}
        return result
    monkeypatch.setattr(project.instructions, 'discover', discover)
    monkeypatch.setattr(project.instructions, 'read_configuration', lambda *args:
                        {'projects': {str(p.source): {'trust_level': 'trusted'}}} if registered else {})
    result = p.run()
    assert result['ready'], result['blocker']
    assert calls == ([{}] if registered else [{}, {'allow_project_registration': True}])


def test_initializer_revalidates_one_exact_plan_without_running_readiness(setup):
    p = setup
    p.state.rmdir()
    p.outputs.rmdir()
    generated = p.source / 'target'
    xdg = p.outputs / 'config'
    p.config['recipe']['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    p.config['recipe']['checkout_policy']['native_environment'] = {'XDG_CONFIG_HOME': str(xdg)}
    p.save()
    plan = p.run()
    assert plan['blocker']['code'] == 'project_directories'
    assert set(plan['required_directories']) == {str(p.state), str(p.outputs), str(generated), str(xdg)}
    assert plan['finish_capacity']['ready'] and p.calls == []
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert result['prepared'], result['blocker']
    assert set(result['created_directories']) == set(plan['required_directories'])
    assert all(Path(name).stat().st_mode & 0o777 == 0o700 for name in result['created_directories'])
    assert p.calls == [] and list(p.state.iterdir()) == []
    final = p.run()
    assert final['ready'], final['blocker']
    assert len(p.calls) == 1 and list(p.state.iterdir()) == []


@pytest.mark.parametrize('edit', [
    lambda p: p.request['concept'].update(authority='Changed'),
    lambda p: p.config['recipe']['checks'][0].update(unknown=True),
    lambda p: p.request['concept']['execution_envelope']['product_budget'].update(max_assignments=3),
])
def test_invalid_request_with_missing_directories_has_no_usable_creation_plan(setup, edit):
    p = setup
    p.outputs.rmdir()
    edit(p)
    p.save()
    plan = p.run()
    assert plan['blocker']['code'] != 'project_directories'
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert not result['prepared'] and result['created_directories'] == []
    assert not p.outputs.exists() and p.calls == []


def test_directory_initializer_preserves_existing_modes_and_contents(setup):
    p = setup
    p.outputs.chmod(0o750)
    keep = p.outputs / 'keep.txt'
    keep.write_text('preserved')
    generated = p.source / 'target'
    p.config['recipe']['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    p.save()
    plan = p.run()
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert result['prepared'], result['blocker']
    assert result['created_directories'] == [str(generated)]
    assert p.outputs.stat().st_mode & 0o777 == 0o750 and keep.read_text() == 'preserved'


def test_directory_initializer_rejects_changed_input_bytes_before_mkdir(setup):
    p = setup
    p.outputs.rmdir()
    plan = p.run()
    p.request_file.write_text(p.request_file.read_text() + '\n')
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert result['blocker']['code'] == 'project_input_drift'
    assert result['created_directories'] == [] and not p.outputs.exists()


@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_unusable_directory_entry_is_never_replaced(setup, kind):
    p = setup
    p.outputs.rmdir()
    if kind == 'file':
        p.outputs.write_text('preserved')
    else:
        p.outputs.symlink_to(p.control, target_is_directory=True)
    plan = p.run()
    assert not plan['ready'] and p.calls == []
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert not result['prepared'] and result['created_directories'] == []
    assert p.outputs.is_symlink() if kind == 'symlink' else p.outputs.read_text() == 'preserved'


def test_initializer_does_not_create_unlisted_ancestors(setup):
    p = setup
    p.config['recipe']['native_write_roots'] = [{'path': str(p.source / 'target' / 'nested'), 'kind': 'generated'}]
    p.save()
    plan = p.run()
    assert 'explicitly configured' in plan['blocker']['detail']
    assert not (p.source / 'target').exists() and p.calls == []


@pytest.mark.parametrize('relative', ['.git', '.codex', 'AGENTS.md', 'AGENTS.override.md', 'src/AGENTS.custom'])
def test_initializer_never_creates_repository_or_instruction_control_directories(setup, relative):
    p = setup
    target = p.source / relative
    p.config['recipe']['native_write_roots'] = [{'path': str(target), 'kind': 'generated'}]
    p.save()
    plan = p.run()
    assert plan['blocker']['code'] == 'project_scope' and p.calls == []
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan)
    assert not result['prepared'] and result['created_directories'] == []
    assert not target.exists()


@pytest.mark.parametrize('registered', [True, False])
def test_explicit_project_instruction_limit_is_honored_without_changing_authority(setup, monkeypatch, registered):
    p = setup
    manifest = p.config['recipe'].pop('instruction_manifest')
    limit = 131072
    p.config['project_doc_max_bytes'] = limit
    manifest.update(codex_home=str(p.control), project_root=str(p.source),
                    configs=[{'path': str(p.control / 'config.toml')}],
                    settings={'project_doc_max_bytes': limit}, runtime_overrides={'project_doc_max_bytes': limit})
    p.save()
    original = copy.deepcopy(p.request['concept']['execution_envelope'])
    calls = []
    def discover(source, **kwargs):
        calls.append(kwargs)
        result = copy.deepcopy(manifest)
        if kwargs.get('allow_project_registration'):
            result['startup_project_registration'] = {'explicit': 'fixture'}
        return result
    monkeypatch.setattr(project.instructions, 'discover', discover)
    monkeypatch.setattr(project.instructions, 'read_configuration', lambda *args:
                        {'projects': {str(p.source): {'trust_level': 'trusted'}}} if registered else {})
    result = p.run()
    assert result['ready'], result['blocker']
    expected = [{'project_doc_max_bytes': limit}]
    if not registered:
        expected.append({'allow_project_registration': True, 'project_doc_max_bytes': limit})
    assert calls == expected
    final = result['concept']['execution_envelope']
    assert final['runtime_recipe']['instruction_manifest']['runtime_overrides'] == {'project_doc_max_bytes': limit}
    for key in ('authority', 'max_seconds', 'max_assignments', 'max_repairs', 'product_budget', 'model', 'effort'):
        assert final[key] == original[key]


def test_explicit_manifest_and_project_instruction_limit_must_agree(setup):
    p = setup
    p.config['project_doc_max_bytes'] = 131072
    manifest = p.config['recipe']['instruction_manifest']
    manifest.update(settings={'project_doc_max_bytes': 65536}, runtime_overrides={'project_doc_max_bytes': 65536})
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'project_instruction_limit' and p.calls == []


def test_matching_explicit_manifest_instruction_limit_is_retained(setup):
    p = setup
    p.config['project_doc_max_bytes'] = 131072
    manifest = p.config['recipe']['instruction_manifest']
    manifest.update(settings={'project_doc_max_bytes': 131072}, runtime_overrides={'project_doc_max_bytes': 131072})
    p.save()
    result = p.run()
    assert result['ready'], result['blocker']
    assert result['concept']['execution_envelope']['runtime_recipe']['instruction_manifest'] == manifest


@pytest.mark.parametrize('limit', [True, 0, -1, 1048577, '131072'])
def test_project_instruction_limit_is_strict_and_bounded(setup, limit):
    p = setup
    p.config['project_doc_max_bytes'] = limit
    p.save()
    result = p.run()
    assert not result['ready'] and p.calls == []
    assert list(p.state.iterdir()) == []


@pytest.mark.parametrize('port', [8443, 49152, 50000, 65535])
def test_native_local_tests_must_explicitly_deny_the_actual_decision_port(setup, monkeypatch, port):
    p = setup
    p.state.rmdir()
    p.outputs.rmdir()
    p.config['decision_service']['port'] = port
    p.config['recipe']['checkout_policy']['native_local_tests'] = {
        'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': []}
    p.save()
    monkeypatch.setattr(project.native_network, '_run', lambda *args:
                        pytest.fail('Configuration validation must not inspect or open host ports'))
    result = p.run()
    assert result['blocker']['code'] == 'project_decision_port'
    assert str(port) in result['blocker']['detail'] and 'denied_tcp_ports' in result['blocker']['detail']
    assert p.calls == [] and not p.state.exists() and not p.outputs.exists()
    assert result['required_directories'] == []
    assert p.config['recipe']['checkout_policy']['native_local_tests']['denied_tcp_ports'] == []


@pytest.mark.parametrize('port', [8443, 8444, 50000])
def test_explicit_decision_port_exclusion_is_retained_without_permission_changes(setup, monkeypatch, port):
    p = setup
    p.config['decision_service']['port'] = port
    declared = {'ephemeral_tcp': {'first': 49152, 'last': 65535},
                'denied_tcp_ports': sorted({8443, 8444, port})}
    p.config['recipe']['checkout_policy']['native_local_tests'] = copy.deepcopy(declared)
    p.save()
    monkeypatch.setattr(project.native_network, '_run', lambda *args:
                        pytest.fail('This simulated preparation must not perform a host network observation'))
    result = p.run()
    assert result['ready'], result['blocker']
    prepared = result['concept']['execution_envelope']['runtime_recipe']['checkout_policy']['native_local_tests']
    assert prepared == declared
    assert p.config['recipe']['checkout_policy']['native_local_tests'] == declared
    assert len(p.calls) == 1 and list(p.state.iterdir()) == []


@pytest.mark.parametrize('value', [None, {},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': ['8443']},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8443, 8443]},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8443], 'extra': True},
    {'ephemeral_tcp': {'first': 1024, 'last': 65535}, 'denied_tcp_ports': [8443]},
])
def test_project_decision_port_guard_uses_the_existing_strict_network_schema(setup, value):
    p = setup
    p.config['recipe']['checkout_policy']['native_local_tests'] = value
    p.save()
    result = p.run()
    assert result['blocker']['code'] == 'native_network' and p.calls == []
    assert list(p.state.iterdir()) == []


def test_default_project_network_policy_needs_no_new_port_declarations(setup, monkeypatch):
    p = setup
    p.config['decision_service']['port'] = 50000
    p.save()
    monkeypatch.setattr(project.native_network, '_run', lambda *args:
                        pytest.fail('The default profile has no native TCP observer'))
    result = p.run()
    assert result['ready'], result['blocker']
    assert 'native_local_tests' not in result['concept']['execution_envelope']['runtime_recipe']['checkout_policy']


def test_static_plan_is_not_readiness_or_approval_and_opens_no_product_store(setup, monkeypatch):
    from agentic_tools.aos.store import Store
    from agentic_tools.aos.codex_runtime import CodexRuntime
    p = setup
    monkeypatch.setattr(Store, '__init__', lambda *args, **kwargs: pytest.fail('Static planning must not create Store'))
    monkeypatch.setattr(CodexRuntime, '__init__', lambda *args, **kwargs: pytest.fail('Static planning must not create runtime'))
    result = project.static_plan(p.project_file, p.request_file)
    assert result['ready'] is False and result['blocker'] is None
    assert result['readiness'] is None and result['finish_capacity']['ready'] is True
    assert result['approval_recorded'] is result['reservation_performed'] is False
    assert result['introduction'] == p.request['introduction'] and result['concept'] == p.request['concept']
    assert result['required_directories'] == [] and p.calls == [] and list(p.state.iterdir()) == []


def test_state_only_bootstrap_creates_only_exact_state_then_requires_a_fresh_plan(setup):
    p = setup
    p.state.rmdir()
    p.outputs.rmdir()
    generated = p.source / 'target'
    p.config['recipe']['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    p.save()
    initial = project.static_plan(p.project_file, p.request_file)
    assert initial['ready'] is False and initial['blocker']['code'] == 'project_directories'
    assert set(initial['required_directories']) == {str(p.state), str(p.outputs), str(generated)}
    result = project.prepare_directories(p.project_file, p.request_file, plan=initial, state_only=True)
    assert result['prepared'] and result['scope'] == 'state-only'
    assert result['created_directories'] == [str(p.state)]
    assert p.state.stat().st_mode & 0o777 == 0o700
    assert not p.outputs.exists() and not generated.exists()
    assert not Path(p.config['readiness_dir']).exists() and p.calls == []
    stale = project.prepare_directories(p.project_file, p.request_file, plan=initial)
    assert not stale['prepared'] and stale['created_directories'] == []
    assert stale['blocker']['code'] == 'project_input_drift'
    fresh = project.static_plan(p.project_file, p.request_file)
    assert set(fresh['required_directories']) == {str(p.outputs), str(generated)}
    complete = project.prepare_directories(p.project_file, p.request_file, plan=fresh)
    assert complete['prepared'] and set(complete['created_directories']) == {str(p.outputs), str(generated)}
    final = project.static_plan(p.project_file, p.request_file)
    assert final['ready'] is False and final['blocker'] is None and final['readiness'] is None
    assert list(p.state.iterdir()) == [] and p.calls == []


def test_state_only_validates_other_declared_paths_before_creating_state(setup):
    p = setup
    p.state.rmdir()
    p.outputs.rmdir()
    forbidden = p.source / '.git'
    p.config['recipe']['native_write_roots'] = [{'path': str(forbidden), 'kind': 'generated'}]
    p.save()
    plan = project.static_plan(p.project_file, p.request_file)
    assert plan['blocker']['code'] == 'project_scope'
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan, state_only=True)
    assert not result['prepared'] and result['created_directories'] == []
    assert not p.state.exists() and not p.outputs.exists() and not forbidden.exists() and p.calls == []


def test_state_only_does_not_create_outputs_when_state_already_exists(setup):
    p = setup
    p.outputs.rmdir()
    plan = project.static_plan(p.project_file, p.request_file)
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan, state_only=True)
    assert not result['prepared'] and result['created_directories'] == []
    assert 'state directory to be missing' in result['blocker']['detail']
    assert not p.outputs.exists() and list(p.state.iterdir()) == []


def test_state_only_rechecks_original_inputs_before_creating_state(setup):
    p = setup
    p.state.rmdir()
    p.outputs.rmdir()
    plan = project.static_plan(p.project_file, p.request_file)
    p.request_file.write_text(p.request_file.read_text() + '\n')
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan, state_only=True)
    assert result['blocker']['code'] == 'project_input_drift'
    assert result['created_directories'] == [] and not p.state.exists() and not p.outputs.exists()


@pytest.mark.parametrize('scope', [1, 'state', None])
def test_directory_preparation_scope_is_not_coerced(setup, scope):
    p = setup
    p.state.rmdir()
    plan = project.static_plan(p.project_file, p.request_file)
    result = project.prepare_directories(p.project_file, p.request_file, plan=plan, state_only=scope)
    assert not result['prepared'] and result['created_directories'] == [] and not p.state.exists()



def _reviewed_host_project(p, *, jobs=5):
    p.config['recipe']['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    p.config['recipe']['native_process_ownership'] = 'host-owned-v1'
    p.config['readiness']['checks'] = []
    bounds = p.request['concept']['execution_envelope']
    bounds['max_assignments'] = jobs
    bounds['product_budget'].update(max_assignments=jobs, max_seconds=jobs * 60)
    p.save()


def test_reviewed_host_project_binds_exact_mode_and_metadata_only_readiness(setup):
    p = setup
    _reviewed_host_project(p)
    original = copy.deepcopy(p.request)
    result = p.run()
    assert result['ready'], result['blocker']
    assert len(p.calls) == 1 and p.calls[0][1]['native_checks'] == []
    final = result['concept']['execution_envelope']
    recipe = final['runtime_recipe']
    assert recipe['checkout_policy']['native_validation'] == 'reviewed-host-v1'
    assert recipe['native_process_ownership'] == 'host-owned-v1'
    assert final['runtime_recipe_digest'] == digest(recipe)
    assert result['finish_capacity']['required_assignments'] == 5
    assert result['finish_capacity']['required_seconds'] == 300
    for key in ('body', 'acceptance', 'team_envelope', 'authority'):
        assert result['concept'][key] == original['concept'][key]
    assert not result['approval_recorded'] and not result['reservation_performed']
    assert list(p.state.iterdir()) == [] and p.request == original


def test_reviewed_host_project_rejects_native_readiness_before_any_preparation(setup):
    p = setup
    _reviewed_host_project(p)
    p.config['readiness']['checks'] = [copy.deepcopy(p.config['recipe']['checks'][0])]
    p.state.rmdir()
    p.outputs.rmdir()
    p.save()
    plan = project.static_plan(p.project_file, p.request_file)
    assert plan['blocker']['code'] == 'project_readiness'
    assert p.run()['blocker']['code'] == 'project_readiness'
    assert p.calls == [] and not p.state.exists() and not p.outputs.exists()
    assert not Path(p.config['readiness_dir']).exists()


def test_reviewed_host_project_counts_triage_before_readiness(setup):
    p = setup
    _reviewed_host_project(p, jobs=4)
    result = p.run()
    assert not result['ready'] and result['blocker']['code'] == 'project_finish_capacity'
    assert result['finish_capacity']['required_assignments'] == 5
    assert result['finish_capacity']['required_seconds'] == 300
    assert p.calls == [] and list(p.state.iterdir()) == []


@pytest.mark.parametrize('backend', [None, 'sandbox-census-v1', 'other'])
def test_reviewed_host_project_rejects_a_mislabelled_native_owner(setup, backend):
    p = setup
    _reviewed_host_project(p)
    p.config['recipe']['native_process_ownership'] = backend
    p.save()
    result = p.run()
    assert not result['ready'] and result['blocker']['code'] == 'project_recipe'
    assert p.calls == [] and list(p.state.iterdir()) == []


def test_reviewed_host_project_cannot_mix_local_test_permissions(setup):
    p = setup
    _reviewed_host_project(p)
    p.config['recipe']['checkout_policy']['native_local_tests'] = {
        'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8443]}
    p.save()
    result = p.run()
    assert not result['ready'] and result['blocker']['code'] == 'checkout_scope'
    assert 'cannot mix native_local_tests' in result['blocker']['detail']
    assert p.calls == [] and list(p.state.iterdir()) == []
