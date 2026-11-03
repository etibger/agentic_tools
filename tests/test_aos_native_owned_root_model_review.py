"""Independent review of native-owned root denials in both model profiles."""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
from codex_cli_bin import bundled_codex_path

from agentic_tools.aos import native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime, FEATURES, PROFILE, clean_environment
from agentic_tools.aos.store import Rejected


def fixture(tmp_path):
    source, outputs, state, parent = [tmp_path / name for name in ('source', 'outputs', 'state', 'temporary')]
    for directory in (source, outputs, state, parent):
        directory.mkdir()
    existing, absent = parent / 'owned-existing', parent / 'owned-absent'
    existing.mkdir(mode=0o700)
    (existing / 'control').write_text('native-owned control\n')
    descendant = existing / 'nested-readable'
    descendant.mkdir()
    (descendant / 'control').write_text('native-owned nested control\n')
    alias_descendant = existing / 'alias-readable'
    alias_descendant.mkdir()
    (alias_descendant / 'control').write_text('native-owned alias control\n')
    alias_grant = Path('/System/Volumes/Data') / alias_descendant.relative_to('/')
    if not alias_grant.is_dir() or not os.path.samefile(alias_grant, alias_descendant):
        alias_grant = alias_descendant
    (source / 'control').write_text('immutable source\n')
    (outputs / 'control').write_text('permitted output\n')
    recipe = {
        'source': str(source), 'outputs': str(outputs),
        # The exact model denial must take precedence over a legitimate native
        # read root; this fixture deliberately exercises that ordering.
        'read_roots': [str(Path(sys.base_prefix).resolve()), str(existing), str(descendant), str(alias_grant)],
        'immutable': {'control': hashlib.sha256((source / 'control').read_bytes()).hexdigest()},
        'native_sandbox': {
            'version': native_sandbox.VERSION,
            'temporary_paths': [{'parent': str(parent), 'name': root.name, 'kind': 'tree'}
                                for root in (existing, absent)],
            'owned_temporary_roots': [str(existing), str(absent)],
        },
    }
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = state
    return SimpleNamespace(source=source, outputs=outputs, state=state, existing=existing,
                           absent=absent, descendant=descendant, alias_grant=alias_grant,
                           recipe=recipe, runtime=runtime)


def effective_config(profile):
    return {'approval_policy': 'never', 'web_search': 'disabled', 'default_permissions': PROFILE,
            'features': {name: False for name in FEATURES}, 'mcp_servers': {}, 'plugins': {},
            'permissions': {PROFILE: {'filesystem': copy.deepcopy(profile['filesystem']),
                                      'network': copy.deepcopy(profile['network'])}},
            'shell_environment_policy': {'inherit': 'none', 'set': copy.deepcopy(profile['shell_environment'])}}


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
def test_model_owned_root_exact_denies_and_effective_config_guard(tmp_path, assessment):
    p = fixture(tmp_path)
    _, model = p.runtime._profile(p.recipe, assessment=assessment)
    _, native = p.runtime._profile(p.recipe, assessment=True, native=True)
    for path in (p.existing, p.absent, p.descendant, p.alias_grant):
        assert model['filesystem'][str(path)] == 'deny'
        assert native['filesystem'].get(str(path)) != 'deny'
    config = effective_config(model)
    p.runtime._verify_config(config, model)
    for path in (p.existing, p.absent, p.descendant, p.alias_grant):
        for altered in (None, 'read', 'write'):
            changed = copy.deepcopy(config)
            if altered is None:
                del changed['permissions'][PROFILE]['filesystem'][str(path)]
            else:
                changed['permissions'][PROFILE]['filesystem'][str(path)] = altered
            with pytest.raises(Rejected, match='Effective filesystem'):
                p.runtime._verify_config(changed, model)
    assert not p.absent.exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS model permission profile')
@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
def test_real_model_profile_denies_existing_native_owned_root(tmp_path, assessment):
    p = fixture(tmp_path)
    overrides, model = p.runtime._profile(p.recipe, assessment=assessment)
    _, native = p.runtime._profile(p.recipe, assessment=True, native=True)
    before = (p.existing / 'control').read_bytes()
    mode = stat.S_IMODE(p.existing.stat().st_mode)
    alias = Path('/System/Volumes/Data') / p.existing.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, p.existing):
        pytest.skip('Fixture has no writable-volume alias')
    program = '''import json,os,sys
from pathlib import Path
root,source,outputs,alias=map(Path,sys.argv[1:5]);mode=int(sys.argv[5]);result={}
def attempt(name, action):
 try:action();result[name]={'allowed':True}
 except OSError as error:result[name]={'allowed':False,'errno':error.errno}
def open_close(path, flags):
 fd=os.open(path,flags,0o600);os.close(fd)
attempt('list',lambda:os.listdir(root))
attempt('read',lambda:open_close(root/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('write',lambda:open_close(root/'control',os.O_WRONLY|os.O_NOFOLLOW))
attempt('create',lambda:open_close(root/'new-child',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW))
attempt('root_metadata_read',lambda:os.stat(root,follow_symlinks=False))
attempt('root_metadata_write',lambda:os.chmod(root,mode))
attempt('child_metadata',lambda:os.stat(root/'control',follow_symlinks=False))
attempt('descendant_list',lambda:os.listdir(root/'nested-readable'))
attempt('descendant_read',lambda:open_close(root/'nested-readable'/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('alias_descendant_list',lambda:os.listdir(root/'alias-readable'))
attempt('alias_descendant_read',lambda:open_close(root/'alias-readable'/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('child_rename',lambda:os.rename(root/'control',root/'renamed-control'))
attempt('child_unlink',lambda:os.unlink(root/'control'))
attempt('alias_read',lambda:open_close(alias/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('alias_create',lambda:open_close(alias/'alias-child',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW))
attempt('source_read_control',lambda:open_close(source/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('output_read_control',lambda:open_close(outputs/'control',os.O_RDONLY|os.O_NOFOLLOW))
attempt('output_write_control',lambda:open_close(outputs/'control',os.O_WRONLY|os.O_NOFOLLOW))
print(json.dumps(result))
'''
    argv = [str(bundled_codex_path())]
    for override in overrides:
        argv.extend(['-c', override])
    argv.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', str(p.source),
                 str(Path(sys._base_executable).resolve()), '-I', '-S', '-c', program,
                 str(p.existing), str(p.source), str(p.outputs), str(alias), str(mode)])
    run = subprocess.run(argv, cwd=p.source, capture_output=True, text=True,
                         env=clean_environment(), timeout=20)
    observed = json.loads(run.stdout) if run.returncode == 0 else None
    evidence = {'role': 'assessment' if assessment else 'implementation',
                'exit_code': run.returncode, 'stderr': run.stderr, 'stdout': run.stdout,
                'observations': observed, 'model_profile': model, 'native_profile': native,
                'existing_root': str(p.existing), 'absent_root': str(p.absent),
                'absent_root_still_absent': not p.absent.exists(),
                'runtime_source_sha256': hashlib.sha256(Path(inspect.getfile(CodexRuntime)).read_bytes()).hexdigest(),
                'test_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'model_calls': 0, 'shared_journals_used': False}
    (tmp_path / 'owned-root-model-proof.json').write_text(json.dumps(evidence, indent=2) + '\n')
    assert run.returncode == 0, run.stderr
    assert observed
    permitted = {'source_read_control', 'output_read_control', 'output_write_control'}
    assert all(observed[name] == {'allowed': True} for name in permitted), observed
    metadata_observations = {'root_metadata_read', 'child_metadata'}
    assert all(row == {'allowed': False, 'errno': 1} for name, row in observed.items()
               if name not in permitted | metadata_observations), observed
    assert (p.existing / 'control').read_bytes() == before
    assert stat.S_IMODE(p.existing.stat().st_mode) == mode
    assert not (p.existing / 'new-child').exists()
    assert not (p.existing / 'alias-child').exists()
    assert not p.absent.exists()
