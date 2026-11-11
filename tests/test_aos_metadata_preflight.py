"""Static preflight checks the real role/probe profiles without side effects."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import checkout_boundary, checkout_runtime, instructions, readiness
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, digest


@pytest.fixture
def p(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    source, outputs, state = (tmp_path / name for name in ('source', 'outputs', 'state'))
    for path in (home, source, outputs, state):
        path.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    (source / '.git').mkdir()
    (source / 'app.py').write_text('product = True\n')
    (source / 'AGENTS.md').write_text('Retain exact product scope.\n')
    (state / 'aos.sqlite3').write_bytes(b'Never open or mutate this historical ledger')
    temporary = tmp_path / 'native-tmp'
    temporary.mkdir()
    monkeypatch.setenv('TMPDIR', str(temporary))
    environment = {}
    for key, name in [('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
                      ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'run')]:
        (outputs / name).mkdir()
        environment[key] = str(outputs / name)
    recipe = {'execution_policy': 'isolated-checkout-v1', 'source': str(source),
        'outputs': str(outputs), 'read_roots': [], 'checks': [], 'max_seconds': 300,
        'probe_interpreter': str(Path(sys.executable).resolve()),
        'native_process_ownership': 'host-owned-v1',
        'instruction_manifest': instructions.discover(source),
        'checkout_policy': {'change_roots': ['app.py'], 'protected_roots': [],
            'native_validation': 'reviewed-host-v1', 'native_environment': environment,
            'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 1024,
                'max_output_bytes': 1024}}}
    result = SimpleNamespace(recipe=recipe, source=source, outputs=outputs, state=state,
        home=home, directory=tmp_path / 'successor-package/prepared/readiness', root=tmp_path)
    result.run = lambda **kwargs: readiness.preflight_metadata(result.recipe,
        state_dir=state, readiness_dir=result.directory, model='exact-model', effort='high',
        max_seconds=300, max_attempts=1, **kwargs)
    return result


def files(root):
    return {str(p.relative_to(root)): (p.is_dir(), None if p.is_dir() else p.read_bytes())
            for p in root.rglob('*')}


def test_exact_future_role_and_probe_plan_creates_nothing(p, monkeypatch):
    before = files(p.root)
    original = copy.deepcopy(p.recipe)
    monkeypatch.setattr(Path, 'mkdir', lambda *a, **kw: pytest.fail('preflight mkdir'))
    monkeypatch.setattr(os, 'mkdir', lambda *a, **kw: pytest.fail('preflight mkdir'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('preflight process'))
    monkeypatch.setattr(readiness, '_probe', lambda *a, **kw: pytest.fail('preflight probe'))
    monkeypatch.setattr(CodexRuntime, '__init__', lambda *a, **kw: pytest.fail('preflight runtime'))
    plan = p.run()
    assert plan['runtime_qualified'] is False
    assert plan['native_profile_schema']['execution'] == 'not_requested'
    assert plan['native_profile_schema']['profile']['filesystem'][str(p.source)] == 'read'
    assert plan['plan_digest'] == digest({k: v for k, v in plan.items() if k != 'plan_digest'})
    assert [r['role'] for r in plan['roles']] == ['implementation', 'assessment']
    for role in plan['roles']:
        assert (role['model'], role['effort']) == ('exact-model', 'high')
        assert len(role['profiles']) == 4
        for row in role['profiles']:
            fs = row['profile']['filesystem']
            assert fs[str(p.source)] == ('write' if role['role'] == 'implementation' else 'read')
            assert fs[role['output']] == 'write'
            assert fs[str(p.state)] == fs[str(p.home / '.codex')] == 'deny'
            assert fs[str(p.source / 'AGENTS.md')] == 'read'
            assert row['profile']['network'] == {'enabled': False}
    assert files(p.root) == before and p.recipe == original and not p.directory.exists()


def test_full_fifteen_plus_controls_plus_probe_cardinality_is_preserved(p):
    guards = [str(p.state)] + [str(p.root / ('guard-' + str(n))) for n in range(14)]
    p.recipe['checkout_policy']['protected_roots'] = guards
    plan = p.run()
    for role in plan['roles']:
        for row in role['profiles']:
            assert len(row['protected_roots']) == (17 if row['phase'] == 'metadata' else 18)
            assert all(row['profile']['filesystem'][path] == 'deny' for path in row['protected_roots'])
    assert p.recipe['checkout_policy']['protected_roots'] == guards
    assert not p.directory.exists()


def test_probe_expansion_over_cardinality_bound_fails_before_any_output(p):
    # With the maintained32-root ceiling, the metadata profile fits exactly;
    # adding the actual run control for its permission probe must still fail.
    p.recipe['checkout_policy']['protected_roots'] = [str(p.state)] + [
        str(p.root / ('guard-' + str(n))) for n in range(29)]
    before = files(p.root)
    with pytest.raises(Rejected, match='Protected roots'):
        p.run()
    assert files(p.root) == before and not p.directory.exists()


@pytest.mark.parametrize('location', ['credentials', 'source', 'outputs', 'state', 'read-root'])
def test_wrong_prepared_location_rejected_without_reserving(p, location):
    if location == 'credentials':
        p.directory = p.home / '.codex/worktrees/recovery/readiness'
    elif location == 'read-root':
        read = p.root / 'tools'
        read.mkdir()
        p.recipe['read_roots'] = [str(read)]
        p.directory = read / 'readiness'
    else:
        p.directory = getattr(p, location) / 'readiness'
    before = files(p.root)
    with pytest.raises(Rejected):
        p.run()
    assert files(p.root) == before and not p.directory.exists()


@pytest.mark.parametrize('kind', ['file', 'ancestor-file', 'symlink', 'ancestor-symlink'])
def test_planned_output_cannot_alias_or_descend_through_file(p, kind):
    if kind == 'file':
        p.directory = p.root / 'not-a-directory'
        p.directory.write_text('preserve')
    elif kind == 'ancestor-file':
        ancestor = p.root / 'not-a-directory'
        ancestor.write_text('preserve')
        p.directory = ancestor / 'readiness'
    elif kind == 'symlink':
        p.directory = p.root / 'alias'
        p.directory.symlink_to(p.outputs, target_is_directory=True)
    else:
        ancestor = p.root / 'alias'
        ancestor.symlink_to(p.outputs, target_is_directory=True)
        p.directory = ancestor / 'readiness'
    with pytest.raises(Rejected):
        p.run()


@pytest.mark.parametrize('invalid', ['future-source', 'future-read', 'future-native', 'bad-native-schema', 'bad-ownership'])
def test_only_output_and_owned_xdg_existence_can_be_planned(p, invalid):
    if invalid == 'future-source':
        p.recipe['source'] = str(p.root / 'absent-source')
    elif invalid == 'future-read':
        p.recipe['read_roots'] = [str(p.root / 'absent-read')]
    elif invalid == 'future-native':
        p.recipe['native_write_roots'] = [{'kind': 'generated', 'path': str(p.source / 'absent-build')}]
    elif invalid == 'bad-native-schema':
        p.recipe['checks'] = [{'name': 'ambiguous', 'argv': [], 'timeout_seconds': 1, 'scope': 'invalid'}]
    else:
        p.recipe['native_process_ownership'] = 'sandbox-census-v1'
    with pytest.raises((Rejected, ValueError, FileNotFoundError)):
        p.run()
    assert not p.directory.exists()


def test_changed_instructions_or_profile_change_the_bound_plan(p):
    plan = p.run()
    (p.source / 'AGENTS.md').write_text('Changed instructions.\n')
    with pytest.raises(Rejected, match='changed'):
        p.run()
    p.recipe['instruction_manifest'] = instructions.discover(p.source)
    changed = p.run()
    assert changed['plan_digest'] != plan['plan_digest']
    assert changed['instruction_manifest_digest'] != plan['instruction_manifest_digest']
    p.recipe['checkout_policy']['protected_roots'].append(str(p.root / 'new-deny'))
    assert p.run()['plan_digest'] != changed['plan_digest']


def test_runtime_profile_still_requires_existing_outputs_and_xdg(p):
    scoped = readiness._scoped(p.recipe, p.directory / 'future-output',
        (p.state, p.directory / 'control'), planned_outputs=True)
    runtime = object.__new__(CodexRuntime)
    runtime.directory = p.state
    with pytest.raises(Rejected, match='existing'):
        runtime._profile(scoped)
    runtime._profile(scoped, planned_outputs=True)
    assert not p.directory.exists()
    with pytest.raises(Rejected, match='cannot prepare'):
        checkout_boundary.scoped_outputs(p.recipe, p.directory, prepare=True, planned_outputs=True)


def test_plan_binds_exact_model_effort_run_and_bounds(p):
    first = p.run()
    second = readiness.preflight_metadata(p.recipe, state_dir=p.state, readiness_dir=p.directory,
        model='other-exact-model', effort='xhigh', max_seconds=600, max_attempts=2, run_id='readiness-2')
    assert first['plan_digest'] != second['plan_digest']
    assert second['roles'][0]['output'].endswith('/readiness-2/implementation')
    assert second['limits'] == {'max_seconds': 600, 'max_attempts': 2}
    with pytest.raises(Rejected, match='exact bounded'):
        p.run(run_id='readiness-2')


def test_planned_profiles_equal_actual_readiness_and_probe_construction(p, monkeypatch):
    """Run the real orchestration/profile paths, replacing only execution facts."""
    p.recipe['checkout_policy']['protected_roots'] = [str(p.state)] + [
        str(p.root / ('guard-' + str(n))) for n in range(14)]
    plan = p.run()
    original_profile = CodexRuntime._profile
    observed = []
    in_probe = {'value': False}

    class ProbeProfileObserved(Exception):
        pass

    def capture(runtime, recipe, assessment=False, **kwargs):
        result = original_profile(runtime, recipe, assessment, **kwargs)
        observed.append({'role': 'assessment' if assessment else 'implementation',
            'phase': 'permission-probe' if in_probe['value'] else 'metadata',
            'after_startup': kwargs.get('after_startup', False), 'profile': result[1],
            'recipe_digest': digest(recipe), 'overrides_digest': readiness._overrides_digest(result[0])})
        if in_probe['value']:
            raise ProbeProfileObserved()
        return result

    def probe(recipe, **kwargs):
        in_probe['value'] = True
        try:
            # Exercise checkout_runtime's own scope expansion. Stop exactly
            # after its actual profile is built and before canary/process code.
            with pytest.raises(ProbeProfileObserved):
                checkout_runtime._probe(recipe, **kwargs)
            return {'checks': {'synthetic': True}, 'cleanup': {'verified': True}}
        finally:
            in_probe['value'] = False

    class Client:
        def __init__(self, binary, overrides, cwd, timeout):
            self.cwd = str(cwd)
        def initialize(self):
            pass
        def request(self, method, params):
            if method == 'config/read':
                return {'config': {}}
            assert method == 'thread/start'
            return {'thread': {'id': 'synthetic-metadata'}, 'model': params['model'],
                'reasoningEffort': params['config']['model_reasoning_effort'],
                'approvalPolicy': 'never', 'cwd': self.cwd}

    class Owner:
        def __init__(self, client):
            self.owner = {'pid': 1, 'uid': 1, 'birth': [1, 1]}
        def close(self, deadline):
            return {'verified': True, 'owner': self.owner, 'members': []}

    monkeypatch.setattr(CodexRuntime, '_profile', capture)
    monkeypatch.setattr(CodexRuntime, '_verify_config', staticmethod(lambda *a: None))
    monkeypatch.setattr(readiness, '_runtime_identity', lambda: {'binary': '/unused/synthetic-codex'})
    monkeypatch.setattr(readiness, '_resources', lambda *a: {'synthetic': True})
    monkeypatch.setattr(readiness, '_enforce_resources', lambda *a: {'passed': True})
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('unexpected process'))
    result = readiness.run_readiness(p.recipe, state_dir=p.state, readiness_dir=p.directory,
        model='exact-model', effort='high', max_seconds=300, max_attempts=1,
        transport_factory=Client, owner_factory=Owner, role_probe=probe)
    assert result['ready'] and result['simulated'] and result['cleanup_verified']
    assert len(observed) == 8
    for actual in observed:
        role = next(r for r in plan['roles'] if r['role'] == actual['role'])
        planned = next(r for r in role['profiles'] if (r['phase'], r['after_startup']) ==
                       (actual['phase'], actual['after_startup']))
        assert all(planned[key] == actual[key] for key in ('profile', 'recipe_digest', 'overrides_digest'))
    assert (p.state / 'aos.sqlite3').read_bytes() == b'Never open or mutate this historical ledger'
    assert not (p.directory / 'runtime/readiness-1/codex-runtime.sqlite3').exists()


def test_override_fingerprint_normalizes_only_table_order():
    first = ['permissions.test={filesystem={"/source"="read","/output"="write"},network={enabled=false}}']
    second = ['permissions.test={network={enabled=false},filesystem={"/output"="write","/source"="read"}}']
    assert readiness._overrides_digest(first) == readiness._overrides_digest(second)
    assert first != second


def test_override_fingerprint_preserves_duplicate_precedence_and_raw_key_spelling():
    first = ['setting="first"', 'setting="last"']
    assert readiness._overrides_digest(first) != readiness._overrides_digest(list(reversed(first)))
    assert readiness._overrides_digest(first) != readiness._overrides_digest([first[-1]])
    assert readiness._overrides_digest(['path.key=true']) != readiness._overrides_digest(['"path.key"=true'])
    assert readiness._overrides_digest(['path.key=true']) != readiness._overrides_digest(['path .key=true'])


@pytest.mark.parametrize('arguments', [None, 'setting=true', [3], ['=true'], ['setting'],
    ['setting='], ['setting={broken'], ['setting=true\nextra=false'], ['setting=2026-11-11'],
    ['setting=nan'], ['setting={nested=inf}'], ['set\nting=true']])
def test_override_fingerprint_rejects_malformed_extra_or_unsupported_values(arguments):
    with pytest.raises(Rejected, match='override'):
        readiness._overrides_digest(arguments)


def test_full_preflight_is_identical_in_distinct_hash_seed_processes(p):
    """Fresh interpreters catch unordered permission-table serialization."""
    p.recipe['checkout_policy']['protected_roots'] = [str(p.state)] + [
        str(p.root / ('guard-' + str(n))) for n in range(14)]
    request = {'recipe': p.recipe, 'state_dir': str(p.state), 'readiness_dir': str(p.directory),
        'home': str(p.home), 'module_root': str(Path(readiness.__file__).parents[2])}
    program = '''
import json,sys
from pathlib import Path
request=json.loads(sys.stdin.read())
Path.home=classmethod(lambda cls:Path(request['home']))
sys.path.insert(0,request['module_root'])
from agentic_tools.aos.readiness import preflight_metadata
plan=preflight_metadata(request['recipe'],state_dir=Path(request['state_dir']),
    readiness_dir=Path(request['readiness_dir']),model='exact-model',effort='high',
    max_seconds=300,max_attempts=1)
print(json.dumps(plan,sort_keys=True))
'''
    before = files(p.root)
    results = []
    for seed in ('1', '73', '8642'):
        environment = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', '-c', program], input=json.dumps(request),
            text=True, capture_output=True, env=environment, check=True, timeout=30)
        results.append(json.loads(result.stdout))
    assert results[0] == results[1] == results[2] == p.run()
    assert files(p.root) == before and not p.directory.exists()
