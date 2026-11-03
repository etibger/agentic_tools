"""Simulated checkout integration; no host commands, models or human decisions."""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import checkout_boundary, checkout_runtime, native_paths, native_processes
from agentic_tools.aos import codex_runtime as module
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate, inventory
from agentic_tools.aos.store import Rejected, canonical, digest, now


class DispatchReached(BaseException):
    """Stop a simulation exactly before any external execution."""


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for name in ('source', 'outputs', 'state', 'home', 'native-tmp'):
        (root / name).mkdir()
    home, source, outputs, state = (root / name for name in ('home', 'source', 'outputs', 'state'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setenv('TMPDIR', str(root / 'native-tmp'))
    (source / 'code.py').write_text('answer = 1\n')
    (source / 'test_example.py').write_text('def test_example(): assert True\n')
    recipe = {'execution_policy': checkout_boundary.POLICY,
        'source': str(source), 'outputs': str(outputs),
        'read_roots': [str(Path(sys.base_prefix).resolve())],
        'max_seconds': 60, 'requires_detached_processes': False,
        'native_process_ownership': native_processes.BACKEND,
        'probe_interpreter': str(Path(sys.executable).resolve()), 'instruction_manifest': {},
        'implementation_prompt': 'SIMULATED: edit code.py within scope.',
        'assessment_prompt': 'SIMULATED: independently review the candidate.',
        'checkout_policy': {'change_roots': ['code.py'], 'protected_roots': [],
            'native_environment': {}, 'endpoint_limits': {'minimum_free_bytes': 100,
                'max_owned_growth_bytes': 100, 'max_output_bytes': 100}},
        'checks': [{'name': 'fixture', 'argv': [sys.executable, '-m', 'pytest', '-q', 'test_example.py'],
                    'timeout_seconds': 10, 'scope': 'SIMULATED behavior check'}]}
    assignment = {'attempt_id': 'a-checkout', 'assignment_id': 'as-checkout', 'work_id': 'CHECKOUT-FIXTURE',
        'kind': 'implementation', 'concept_revision': 1, 'concept_digest': 'SIMULATED:approved',
        'contract_revision': 1, 'scope': 'code.py', 'executor': 'writer',
        'requested_model': 'gpt-6.1-sol', 'requested_effort': 'high', 'sent_at': now()}
    contract = {'execution_policy': checkout_boundary.POLICY, 'baseline': digest(inventory(source)),
                'max_seconds': 60, 'required_checks': ['fixture'], 'max_repairs': 0}
    package = {'execution_envelope': {'execution_policy': checkout_boundary.POLICY,
                                      'baseline': contract['baseline'], 'runtime_recipe_digest': digest(recipe)}}
    budget = {'budget_id': 'checkout-fixture-budget', 'product_id': 'checkout-fixture-product',
        'kind': 'product', 'ledger_directory': str(state), 'max_assignments': 2,
        'max_seconds': 120, 'max_job_seconds': 60}
    @contextmanager
    def connection():
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(
            fetchone=lambda: {**assignment, 'state': 'sent'}))
    lifecycle = SimpleNamespace(store=SimpleNamespace(directory=state, _connection=connection),
        assignment_authority_errors=lambda row: [], _run=lambda db, work: ({'cycle': 0}, contract))
    runtime = CodexRuntime(lifecycle, {assignment['work_id']: recipe})
    monkeypatch.setattr(runtime, '_package', lambda row: package)
    monkeypatch.setattr(runtime, '_budget', lambda row: budget)
    monkeypatch.setattr(runtime, '_bindings', lambda *args: {})
    monkeypatch.setattr(module.instructions, 'validate', lambda *args, **kwargs: [])
    monkeypatch.setattr(module.instructions, 'runtime_overrides', lambda *args: [])
    # The actual kernel boundary is separately qualified; these dispatch tests
    # must run on any host and must never query the process census.
    monkeypatch.setattr(module.native_processes, 'validate', lambda recipe: None)
    p = SimpleNamespace(runtime=runtime, recipe=recipe, assignment=assignment, contract=contract,
        package=package, budget=budget, source=source, outputs=outputs, state=state, home=home)
    yield p
    runtime.close()


def refresh(p):
    p.package['execution_envelope']['runtime_recipe_digest'] = digest(p.recipe)


def resources(*, growth=0, output=0, breach=None):
    return {'mode': 'endpoints', 'baseline': {'owned_bytes': 100, 'output_bytes': 0, 'free_bytes': 1000},
        'latest': {}, 'observed_growth_high_water': growth, 'output_high_water': output,
        'breach': breach, 'limitation': 'Endpoint observations only.'}


def record(p, *, resource_state=None, attempt_id=None):
    value = {'attempt_id': attempt_id or p.assignment['attempt_id'], 'assignment': copy.deepcopy(p.assignment),
        'recipe_digest': digest(p.recipe), 'phase': 'intent', 'created_at': now(),
        'run_outputs': str(p.outputs / (attempt_id or p.assignment['attempt_id'])),
        'baseline': inventory(p.source), 'model_calls_attempted': 0, 'usage': None,
        'budget_id': p.budget['budget_id'], 'budget_seconds': 60,
        'startup_monotonic': module.time.monotonic(),
        'startup_deadline_monotonic': module.time.monotonic() + 60,
        'startup_deadline': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        'startup_cleanup_reserve': 5}
    if resource_state is not None: value['checkout_resources'] = copy.deepcopy(resource_state)
    with p.runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (value['attempt_id'], canonical(value)))
    return value


def fail_external(*args, **kwargs):
    raise DispatchReached()


def test_exact_checkout_recipe_requires_both_saved_policy_markers(isolated):
    p = isolated
    assert p.runtime._recipe(p.assignment) is p.recipe
    p.contract.pop('execution_policy')
    with pytest.raises(Rejected, match='explicitly approved'):
        p.runtime._recipe(p.assignment)
    p.contract['execution_policy'] = checkout_boundary.POLICY
    p.package['execution_envelope'].pop('execution_policy')
    with pytest.raises(Rejected, match='explicitly approved'):
        p.runtime._recipe(p.assignment)


def test_legacy_recipe_is_separate_and_cannot_inherit_checkout_approval(isolated):
    p = isolated
    p.recipe.pop('execution_policy')
    p.recipe.pop('checkout_policy')
    p.recipe.update(mutable=['code.py'], immutable={'test_example.py': inventory(p.source)['test_example.py']})
    refresh(p)
    with pytest.raises(Rejected, match='execution policy differs'):
        p.runtime._recipe(p.assignment)
    p.contract.pop('execution_policy')
    p.package['execution_envelope'].pop('execution_policy')
    assert p.runtime._recipe(p.assignment) is p.recipe


def test_product_preflight_rejection_precedes_recipe_reads_and_charges(isolated, monkeypatch):
    p = isolated
    monkeypatch.setattr(p.runtime, '_recipe', lambda *args: pytest.fail('Product preflight must reject first'))
    monkeypatch.setattr(p.runtime, '_client', lambda *args, **kwargs: pytest.fail('No host/model launch'))
    with pytest.raises(Rejected, match='host-only'):
        p.runtime.start(p.assignment, preflight_only=True)
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0


@pytest.mark.parametrize('kind,admitted', [('implementation', False), ('assessment', True)])
def test_reservation_leaves_an_assessment_slot_but_does_not_require_a_third(isolated, monkeypatch, kind, admitted):
    p = isolated
    p.assignment['kind'] = kind
    p.assignment['candidate'] = canonical(candidate(p.source))
    p.budget.update(max_assignments=1, max_seconds=60)
    monkeypatch.setattr(p.runtime, '_client', fail_external)
    if admitted:
        with pytest.raises(DispatchReached): p.runtime.start(p.assignment)
    else:
        with pytest.raises(Rejected, match='following jobs'): p.runtime.start(p.assignment)
    with p.runtime._db() as db:
        saved = [json.loads(row[0]) for row in db.execute('SELECT record FROM attempt')]
    assert len(saved) == int(admitted)
    assert sum(row['budget_seconds'] for row in saved) == (60 if admitted else 0)
    assert p.runtime.model_calls == 0


@pytest.mark.parametrize('path,allowed', [('code.py', True), ('README.md', False), ('AGENTS.md', False)])
def test_completed_writer_diff_is_checked_against_approved_scope(isolated, monkeypatch, path, allowed):
    p = isolated
    item = record(p)
    item.update(thread_id='SIMULATED-thread', turn_id='SIMULATED-turn')
    (p.source / path).write_text('changed\n')
    monkeypatch.setattr(p.runtime, '_resource_sample', lambda *args, **kwargs: None)
    if allowed:
        result = p.runtime._result(item, p.recipe, {'status': 'completed'}, {'effects_reconciled': True})
        assert result['status'] == 'completed'
    else:
        with pytest.raises(Rejected, match='approved change scope'):
            p.runtime._result(item, p.recipe, {'status': 'completed'}, {'effects_reconciled': True})


def test_stale_save_and_startup_patch_keep_first_breach_and_high_water(isolated):
    p = isolated
    item = record(p, resource_state=resources())
    stale = copy.deepcopy(item)
    breach = {'code': 'checkout_resource_limit', 'detail': 'first breach'}
    p.runtime._startup_patch(item, {'checkout_resources': resources(growth=80, output=70, breach=breach)},
                             match_frontier=False)
    stale['checkout_resources'] = resources(growth=1, output=2)
    p.runtime._save(stale)
    p.runtime._startup_patch(stale, {'checkout_resources': resources(growth=3, output=4,
        breach={'code': 'later', 'detail': 'must not replace first evidence'})}, match_frontier=False)
    saved = p.runtime._get(item['attempt_id'])['checkout_resources']
    assert saved['baseline'] == resources()['baseline']
    assert saved['breach'] == breach
    assert (saved['observed_growth_high_water'], saved['output_high_water']) == (80, 70)


@pytest.mark.parametrize('field,value', [('mode', 'continuous'), ('baseline', {'owned_bytes': 0})])
@pytest.mark.parametrize('method', ['save', 'startup'])
def test_saved_endpoint_identity_cannot_change(isolated, field, value, method):
    p = isolated
    item = record(p, resource_state=resources())
    changed = resources()
    changed[field] = value
    with pytest.raises(Rejected, match='scope/baseline'):
        if method == 'save':
            item['checkout_resources'] = changed
            p.runtime._save(item)
        else:
            p.runtime._startup_patch(item, {'checkout_resources': changed}, match_frontier=False)
    assert p.runtime._get(item['attempt_id'])['checkout_resources'] == resources()


def test_next_assignment_keeps_original_budget_baseline_and_prior_high_water(isolated, monkeypatch):
    p = isolated
    record(p, resource_state=resources(growth=90, output=60), attempt_id='a-original')
    monkeypatch.setattr(checkout_runtime, 'observe_resources', lambda *args, **kwargs:
                        {'owned_bytes': 150, 'output_bytes': 20, 'free_bytes': 1000})
    result = p.runtime._checkout_before_start(p.recipe, p.budget)
    assert result['baseline']['owned_bytes'] == 100
    assert result['latest']['observed_growth_bytes'] == 50
    assert (result['observed_growth_high_water'], result['output_high_water']) == (90, 60)


def test_existing_budget_breach_blocks_next_assignment_without_new_charge(isolated, monkeypatch):
    p = isolated
    record(p, resource_state=resources(breach={'code': 'prior-breach'}), attempt_id='a-original')
    monkeypatch.setattr(checkout_runtime, 'observe_resources', lambda *args, **kwargs:
                        {'owned_bytes': 100, 'output_bytes': 0, 'free_bytes': 1000})
    with pytest.raises(Rejected, match='retained endpoint breach'):
        p.runtime._checkout_before_start(p.recipe, p.budget)
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1


def test_finish_keeps_failed_endpoint_high_water_after_files_shrink(isolated, monkeypatch):
    p = isolated
    p.recipe['checkout_policy']['endpoint_limits']['max_owned_growth_bytes'] = 20
    item = record(p, resource_state=resources())
    observations = iter([{'owned_bytes': 150, 'output_bytes': 40, 'free_bytes': 1000},
                         {'owned_bytes': 110, 'output_bytes': 10, 'free_bytes': 1000}])
    monkeypatch.setattr(checkout_runtime, 'observe_resources', lambda *args, **kwargs: next(observations))
    failed = p.runtime._checkout_endpoint(item, p.recipe, finish=True)
    recovered = p.runtime._checkout_endpoint(item, p.recipe, finish=True)
    assert recovered['breach'] == failed['breach'] and recovered['breach'] is not None
    assert (recovered['observed_growth_high_water'], recovered['output_high_water']) == (50, 40)


def test_endpoint_cannot_report_success_over_concurrent_saved_breach(isolated, monkeypatch):
    p = isolated
    item = record(p, resource_state=resources())
    peer = copy.deepcopy(item)
    def observe(*args, **kwargs):
        p.runtime._startup_patch(peer, {'checkout_resources': resources(growth=80, output=70,
            breach={'code': 'peer-breach'})}, match_frontier=False)
        return {'owned_bytes': 110, 'output_bytes': 10, 'free_bytes': 1000}
    monkeypatch.setattr(checkout_runtime, 'observe_resources', observe)
    with pytest.raises(Rejected, match='prior endpoint'):
        p.runtime._checkout_endpoint(item, p.recipe)
    assert p.runtime._get(item['attempt_id'])['checkout_resources']['breach']['code'] == 'peer-breach'


def test_active_poll_does_not_start_continuous_or_repeated_resource_scans(isolated, monkeypatch):
    p = isolated
    item = record(p, resource_state=resources())
    item.update(phase='running', thread_id='SIMULATED-thread', turn_id='SIMULATED-turn')
    p.runtime._save(item)
    monkeypatch.setattr(checkout_runtime, 'observe_resources', lambda *args, **kwargs: pytest.fail('Polling rescanned resources'))
    monkeypatch.setattr(p.runtime, '_resource_sample', lambda *args, **kwargs: pytest.fail('Polling sampled resources'))
    monkeypatch.setattr(p.runtime, '_client', lambda *args, **kwargs: object())
    monkeypatch.setattr(p.runtime, '_events', lambda *args: None)
    monkeypatch.setattr(p.runtime, '_read_turn', lambda *args: {'status': 'inProgress'})
    for _ in range(3): assert p.runtime.poll(item['attempt_id'])['state'] == 'active'
    assert p.runtime._resource_monitors == {}


def test_scoped_model_startup_keeps_prepared_runtime_directories_valid(isolated, monkeypatch):
    p = isolated
    env = p.recipe['checkout_policy']['native_environment']
    for kind in ('CONFIG', 'STATE', 'DATA', 'RUNTIME'):
        path = p.outputs / kind.lower()
        path.mkdir()
        env['XDG_RUNTIME_DIR' if kind == 'RUNTIME' else 'XDG_' + kind + '_HOME'] = str(path)
    refresh(p)
    item = record(p)
    Path(item['run_outputs']).mkdir()
    monkeypatch.setattr(module, 'processes', lambda **kwargs: {})
    monkeypatch.setattr(module, 'version', lambda name: module.PIN)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='codex-cli ' + module.PIN))
    p.runtime.factory = fail_external
    with pytest.raises(DispatchReached): p.runtime._client(item, p.recipe)


def test_runtime_instruction_overlay_cannot_reopen_protected_support(isolated, monkeypatch):
    p = isolated
    protected = p.home / 'private-control'
    protected.mkdir()
    support = protected / 'support.txt'
    support.write_text('private')
    p.recipe['checkout_policy']['protected_roots'] = [str(protected)]
    p.recipe['instruction_manifest'] = {'support': [{'path': str(support), 'resolved': str(support)}]}
    monkeypatch.setattr(module.instructions, 'validate', lambda *args, **kwargs: [str(support)])
    try:
        _, profile = p.runtime._profile(p.recipe)
    except Rejected as error:
        assert error.code == 'checkout_scope'
    else:
        assert profile['filesystem'].get(str(support)) != 'read'
        assert profile['filesystem'][str(protected)] == 'deny'


@pytest.mark.parametrize('backend', [None, 'wrong-backend'])
def test_checkout_native_dispatch_requires_census_before_launch(isolated, monkeypatch, backend):
    p = isolated
    p.recipe['native_process_ownership'] = backend
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs: ([], {'filesystem': {}}))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('No host launch'))
    with pytest.raises(Rejected, match='census'):
        p.runtime._check(p.recipe, p.recipe['checks'][0], p.outputs / 'native', 'assessor', candidate(p.source))


def test_checkout_native_dispatch_admits_into_existing_journal_before_execution(isolated, monkeypatch):
    p = isolated
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs: ([], {'filesystem': {}}))
    monkeypatch.setattr(module.native_preparation, 'Preparation', lambda *args:
                        SimpleNamespace(before_temporary={}, stage='profile', failure=lambda *args, **kwargs: None))
    def reached(recipe, row, preparation, deadline):
        saved = p.runtime._native_get(row['check_id'])
        assert saved['phase'] == 'intent'
        assert saved['ownership_backend'] == native_processes.BACKEND
        assert saved['native_path_contract']['execution_policy'] == checkout_boundary.POLICY
        raise DispatchReached()
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', reached)
    with pytest.raises(DispatchReached):
        p.runtime._check(p.recipe, p.recipe['checks'][0], p.outputs / 'native', 'assessor', candidate(p.source))


def test_native_profile_uses_checkout_renderer_and_retains_exact_policy(isolated, monkeypatch):
    p = isolated
    (p.state / 'native-status').mkdir()
    row = {'check_id': 'nc-simulated'}
    saved = []
    monkeypatch.setattr(checkout_boundary, 'render_native', lambda *args: 'SIMULATED-NATIVE-POLICY')
    monkeypatch.setattr(module.native_network, 'observe', lambda *args: pytest.fail('Legacy network observer used'))
    monkeypatch.setattr(p.runtime, '_native_save', lambda value: saved.append(copy.deepcopy(value)))
    argv = p.runtime._native_profile_prepare(p.recipe, row, None,
        datetime.now(timezone.utc) + timedelta(seconds=10))
    assert argv[:2] == ['/usr/bin/sandbox-exec', '-f']
    assert Path(argv[2]).read_text() == 'SIMULATED-NATIVE-POLICY'
    assert saved[0]['native_sandbox_boundary']['execution_policy'] == checkout_boundary.POLICY
    assert saved[0]['native_sandbox_boundary']['contract'] == native_paths.contract(p.recipe)


def test_cached_native_receipt_cannot_cross_a_checkout_policy_change(isolated, monkeypatch):
    p = isolated
    prior = copy.deepcopy(native_paths.contract(p.recipe))
    p.recipe['checkout_policy']['native_environment']['CARGO_BUILD_JOBS'] = '2'
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs: ([], {'filesystem': {}}))
    monkeypatch.setattr(p.runtime, '_native_get', lambda check_id:
        {'ownership_backend': native_processes.BACKEND, 'native_path_contract': prior})
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('No replay or launch'))
    with pytest.raises(Rejected, match='permissions differ'):
        p.runtime._check(p.recipe, p.recipe['checks'][0], p.outputs / 'native', 'assessor', candidate(p.source))


def test_endpoint_scan_uses_the_retained_assignment_deadline(isolated, monkeypatch):
    p = isolated
    item = record(p, resource_state=resources())
    item['startup_deadline_monotonic'] = module.time.monotonic() + 2
    p.runtime._save(item)
    deadlines = []
    def observe(*args, **kwargs):
        deadlines.append(kwargs['deadline'])
        return {'owned_bytes': 100, 'output_bytes': 0, 'free_bytes': 1000}
    monkeypatch.setattr(checkout_runtime, 'observe_resources', observe)
    p.runtime._checkout_endpoint(item, p.recipe)
    assert deadlines == [item['startup_deadline_monotonic']]


def test_expired_assignment_stops_before_scans_and_live_scan_gets_remaining_deadline(isolated, monkeypatch):
    p = isolated
    fixed = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=None) if tz is None else fixed.astimezone(tz)
    monkeypatch.setattr(module, 'datetime', FixedDatetime)
    monkeypatch.setattr(module.time, 'monotonic', lambda: 500.0)
    stage = {'expired': True}
    tree_deadlines = []
    def tree(recipe, *, deadline=None):
        assert not stage['expired'], 'Expired assignment reached the source tree scan'
        assert deadline == 505.0  # Finite five-second remainder, not a fresh allowance.
        tree_deadlines.append(deadline)
        return {'source': 2, 'outputs': 0}
    original_inventory = module.inventory
    def scan(*args, **kwargs):
        assert not stage['expired'], 'Expired assignment reached source inventory'
        assert callable(kwargs.get('check_deadline'))
        kwargs['check_deadline']()
        return original_inventory(*args, **kwargs)
    monkeypatch.setattr(checkout_boundary, 'validate_tree', tree)
    monkeypatch.setattr(module, 'inventory', scan)
    monkeypatch.setattr(checkout_runtime, 'observe_resources',
                        lambda *args, **kwargs: pytest.fail('Expired admission reached resource scan'))
    monkeypatch.setattr(module.budgets, 'reserve',
                        lambda *args, **kwargs: pytest.fail('Expired admission reached reservation'))
    monkeypatch.setattr(p.runtime, '_client',
                        lambda *args, **kwargs: pytest.fail('Expired admission reached host/model startup'))
    p.assignment['sent_at'] = (fixed - timedelta(seconds=61)).isoformat()
    with pytest.raises(Rejected, match='deadline'):
        p.runtime.start(p.assignment)
    assert tree_deadlines == []
    stage['expired'] = False
    p.assignment['sent_at'] = (fixed - timedelta(seconds=55)).isoformat()
    assert p.runtime._recipe(p.assignment) is p.recipe
    assert tree_deadlines == [505.0]
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0


def test_internal_instruction_alias_is_inventoryable_only_in_checkout_mode(isolated):
    p = isolated
    (p.source / 'AGENTS.md').write_text('retained instructions\n')
    (p.source / 'CLAUDE.md').symlink_to('AGENTS.md')
    files = inventory(p.source, recipe=p.recipe)
    assert files['CLAUDE.md'].startswith('symlink:')
    assert files['AGENTS.md'] != files['CLAUDE.md']
    assert checkout_boundary.validate_tree(p.recipe)['source'] == 4
    assert not checkout_boundary.changes_allowed(p.recipe, ['CLAUDE.md'])
    with pytest.raises(Rejected, match='link'):
        inventory(p.source)


def test_symlink_retarget_and_regular_file_replacement_change_candidate_identity(isolated):
    p = isolated
    (p.source / 'AGENTS.md').write_text('retained instructions\n')
    alias = p.source / 'CLAUDE.md'
    alias.symlink_to('AGENTS.md')
    first = candidate(p.source, recipe=p.recipe)
    alias.unlink()
    alias.symlink_to('test_example.py')
    second = candidate(p.source, recipe=p.recipe)
    assert module.changed(first['inventory'], second['inventory'])['changes'] == ['CLAUDE.md']
    alias.unlink()
    alias.write_text('test_example.py')
    third = candidate(p.source, recipe=p.recipe)
    assert module.changed(second['inventory'], third['inventory'])['changes'] == ['CLAUDE.md']
    assert len({item['identity'] for item in (first, second, third)}) == 3


@pytest.mark.parametrize('kind', ['external', 'generated', 'directory', 'dangling'])
def test_checkout_source_link_restrictions_apply_at_inventory_and_endpoints(isolated, kind):
    p = isolated
    if kind == 'external':
        (p.source.parent / 'outside').write_text('outside source')
        target = '../outside'
    elif kind == 'generated':
        generated = p.source / 'target'
        generated.mkdir()
        (generated / 'artifact').write_text('not part of source inventory')
        p.recipe['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
        target = 'target/artifact'
    elif kind == 'directory':
        (p.source / 'folder').mkdir()
        target = 'folder'
    else:
        target = 'missing-file'
    (p.source / 'alias').symlink_to(target)
    for observe in (lambda: inventory(p.source, recipe=p.recipe),
                    lambda: checkout_boundary.validate_tree(p.recipe),
                    lambda: checkout_runtime.observe_resources(p.recipe)):
        with pytest.raises(Rejected): observe()
    assert (p.source / 'alias').is_symlink()


def test_checkout_output_links_remain_forbidden(isolated):
    p = isolated
    (p.outputs / 'alias').symlink_to('../source/code.py')
    for observe in (lambda: inventory(p.outputs, allow_empty=True),
                    lambda: checkout_boundary.validate_tree(p.recipe),
                    lambda: checkout_runtime.observe_resources(p.recipe)):
        with pytest.raises(Rejected): observe()
    assert (p.source / 'code.py').read_text() == 'answer = 1\n'


@pytest.mark.parametrize('kind,repairs,cycle,retries,used,following', [
    ('implementation', 0, 0, 0, 0, 1),
    ('implementation', 1, 0, 1, 0, 4),
    ('assessment', 1, 0, 1, 0, 3),
    ('repair', 1, 1, 1, 0, 2),
    ('assessment', 1, 1, 1, 1, 0),
    ('repair', 2, 1, 2, 1, 4),
])
def test_remaining_jobs_include_reassessment_and_unused_startup_capacity(
        isolated, kind, repairs, cycle, retries, used, following):
    p = isolated
    p.contract.update(max_repairs=repairs, max_startup_retries=retries)
    p.runtime.lifecycle._run = lambda db, work: ({'cycle': cycle}, p.contract)
    p.runtime.lifecycle.startup_retries_used = lambda work: used
    assert p.runtime._checkout_following_jobs({**p.assignment, 'kind': kind}) == following


def test_startup_retry_never_discards_future_independent_assessment_capacity(isolated, monkeypatch):
    p = isolated
    p.contract.update(max_repairs=1, max_startup_retries=1)
    p.runtime.lifecycle.startup_retries_used = lambda work: 1
    p.runtime.lifecycle._run = lambda db, work: ({'cycle': 0}, p.contract)
    p.budget.update(max_assignments=4, max_seconds=240)
    record(p, attempt_id='retained-failed-startup')
    monkeypatch.setattr(p.runtime, '_checkout_before_start', lambda *args, **kwargs: resources())
    monkeypatch.setattr(p.runtime, '_client', lambda *args: pytest.fail('Capacity failure launched a client'))
    with pytest.raises(Rejected, match='required following jobs'):
        p.runtime.start(p.assignment)
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0


@pytest.mark.parametrize('cycle,used', [(2, 0), (-1, 0), (0, 2), (True, 0), (0, True)])
def test_inconsistent_remaining_capacity_stops_before_reservation(isolated, cycle, used):
    p = isolated
    p.contract.update(max_repairs=1, max_startup_retries=1)
    p.runtime.lifecycle._run = lambda db, work: ({'cycle': cycle}, p.contract)
    p.runtime.lifecycle.startup_retries_used = lambda work: used
    with pytest.raises(Rejected, match='capacity is inconsistent'):
        p.runtime._checkout_following_jobs(p.assignment)
