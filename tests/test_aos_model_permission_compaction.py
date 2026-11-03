"""Model-only map compaction retains edits and every explicit boundary.

Map tests use fake effective-config readback. Two Darwin-only cases run a
fixed no-fork permission probe through the pinned CLI, using owned fixtures;
neither case calls a model or accesses a product checkout or shared journal.
"""
import copy
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import tomllib

import pytest
from codex_cli_bin import bundled_codex_path

from agentic_tools.aos import instructions, native_paths, native_processes, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime, FEATURES, PIN, PROFILE, clean_environment
from agentic_tools.aos.store import Rejected


@pytest.fixture
def tree(tmp_path):
    root = tmp_path.resolve()
    source, outputs, state, home = [root / name for name in ('source', 'outputs', 'state', 'home')]
    for directory in (source, outputs, state, home):
        directory.mkdir()
    mutable = ['src/core/cpu.rs']
    immutable = [
        'README.txt', 'src/core/nearby_test.rs', 'src/core/fixtures/sample.txt',
        'src/library/first.rs', 'src/library/nested/second.rs',
        'tests/first.rs', 'tests/nested/second.rs',
    ]
    for relative in [*mutable, *immutable]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('owned fixture: ' + relative + '\n')
    recipe = {
        'source': str(source), 'outputs': str(outputs), 'read_roots': [],
        'mutable': mutable,
        'immutable': {relative: hashlib.sha256((source / relative).read_bytes()).hexdigest()
                      for relative in immutable},
    }
    return SimpleNamespace(source=source, outputs=outputs, state=state, home=home, recipe=recipe)


def permission_map(p, *, assessment=False, extra=None):
    explicit = {':root': 'deny', ':minimal': 'read',
                str(p.source): 'read' if assessment else 'write',
                str(p.outputs): 'write', str(p.state): 'deny', str(p.home / '.codex'): 'deny'}
    explicit.update(extra or {})
    filesystem = dict(explicit)
    filesystem.update({str(p.source / relative): 'read' for relative in p.recipe['immutable']})
    # Model-native alias closure can override immutable leaves. The caller's
    # final map, not an earlier immutable read, is authoritative.
    filesystem.update(extra or {})
    return filesystem, set(explicit)


def compact(p, filesystem, explicit):
    before = copy.deepcopy((p.recipe, filesystem, explicit))
    result = CodexRuntime._compact_model_immutable_reads(p.recipe, filesystem, explicit)
    assert (p.recipe, filesystem, explicit) == before
    assert isinstance(result, dict)
    return result


def effective(filesystem, path):
    """Independent longest literal-prefix oracle for canonical fixture paths."""
    path = Path(path)
    grants = [(len(Path(root).parts), access) for root, access in filesystem.items()
              if not root.startswith(':') and path.is_relative_to(root)]
    return max(grants)[1] if grants else filesystem[':root']


def assert_explicit_preserved(before, after, explicit):
    assert {path: after[path] for path in explicit} == {path: before[path] for path in explicit}


def test_writer_groups_maximal_immutable_branches_but_keeps_atomic_edit_parents(tree):
    p = tree
    before, explicit = permission_map(p)
    after = compact(p, before, explicit)
    assert_explicit_preserved(before, after, explicit)
    assert after[str(p.source)] == 'write'
    expected_groups = [p.source / 'tests', p.source / 'src/library', p.source / 'src/core/fixtures']
    for directory in expected_groups:
        assert after[str(directory)] == 'read'
        assert all(str(p.source / relative) not in after for relative in p.recipe['immutable']
                   if (p.source / relative).is_relative_to(directory))
    assert after[str(p.source / 'README.txt')] == 'read'
    assert after[str(p.source / 'src/core/nearby_test.rs')] == 'read'
    assert str(p.source / 'src') not in after
    assert str(p.source / 'src/core') not in after
    for relative in ('src/core/cpu.rs', 'src/core/.cpu.rs.tmp', 'src/.atomic-edit.tmp', '.atomic-edit.tmp'):
        assert effective(after, p.source / relative) == 'write'
    for relative in p.recipe['immutable']:
        assert effective(after, p.source / relative) == 'read'
    assert effective(after, p.outputs / 'created-result') == 'write'
    assert effective(after, p.state / 'private-state') == 'deny'
    assert len(after) < len(before)


@pytest.mark.parametrize('relative', ['tests/new.rs', 'tests/future/deep/new.rs'])
def test_absent_mutable_destinations_keep_every_ancestor_writable(tree, relative):
    p = tree
    p.recipe['mutable'].append(relative)
    destination = p.source / relative
    assert not destination.exists()
    before, explicit = permission_map(p)
    after = compact(p, before, explicit)
    assert str(p.source / 'tests') not in after
    assert after[str(p.source / 'tests/first.rs')] == 'read'
    assert after[str(p.source / 'tests/nested')] == 'read'
    assert effective(after, destination) == 'write'
    for parent in [destination.parent, *destination.parent.parents]:
        if parent.is_relative_to(p.source):
            assert effective(after, parent / '.new.rs.atomic-tmp') == 'write'
    assert not destination.exists(), 'Permission construction must not create the destination'


def test_assessor_only_removes_redundant_leaves_under_existing_read_directories(tree):
    p = tree
    before, explicit = permission_map(p, assessment=True)
    after = compact(p, before, explicit)
    assert after == {path: before[path] for path in explicit}
    assert set(after).issubset(before), 'Assessment must not introduce any directory grants'
    probes = [p.source, p.outputs / 'new', p.state / 'secret', p.home / '.codex/secret']
    probes += [p.source / relative for relative in [*p.recipe['mutable'], *p.recipe['immutable']]]
    probes += [p.source / 'src/new.rs', p.source / 'new/deep/file']
    for path in probes:
        assert effective(after, path) == effective(before, path)


@pytest.mark.parametrize('access', ['deny', 'write'])
def test_assessor_does_not_prune_reads_across_an_explicit_access_transition(tree, access):
    p = tree
    parent = p.source / 'tests'
    before, explicit = permission_map(p, assessment=True, extra={str(parent): access})
    after = compact(p, before, explicit)
    assert_explicit_preserved(before, after, explicit)
    for relative in ('tests/first.rs', 'tests/nested/second.rs'):
        assert after[str(p.source / relative)] == 'read'
        assert effective(after, p.source / relative) == 'read'
    assert effective(after, parent / 'not-immutable') == access
    assert set(after).issubset(before)


@pytest.mark.parametrize('access', ['read', 'write', 'deny'])
def test_writer_retains_nonimmutable_descendant_transitions(tree, access):
    p = tree
    transition = p.source / 'src/library/control'
    transition.mkdir()
    before, explicit = permission_map(p, extra={str(transition): access})
    after = compact(p, before, explicit)
    assert_explicit_preserved(before, after, explicit)
    assert str(p.source / 'src/library') not in after
    assert after[str(p.source / 'src/library/first.rs')] == 'read'
    assert effective(after, transition / 'future') == access
    assert effective(after, p.source / 'src/library/first.rs') == 'read'


@pytest.mark.parametrize('assessment', [False, True])
def test_immutable_instruction_support_remains_explicit(tree, assessment):
    p = tree
    instruction = p.source / 'tests/first.rs'
    before, explicit = permission_map(p, assessment=assessment)
    explicit.add(str(instruction))
    after = compact(p, before, explicit)
    assert after[str(instruction)] == 'read'
    assert_explicit_preserved(before, after, explicit)
    if not assessment:
        assert str(p.source / 'tests') not in after


@pytest.mark.parametrize('assessment', [False, True])
def test_final_immutable_deny_is_not_reopened_by_compaction(tree, assessment):
    p = tree
    denied = p.source / 'src/library/first.rs'
    before, explicit = permission_map(p, assessment=assessment, extra={str(denied): 'deny'})
    after = compact(p, before, explicit)
    assert after[str(denied)] == 'deny'
    assert effective(after, denied) == 'deny'
    assert_explicit_preserved(before, after, explicit)
    if not assessment:
        assert str(p.source / 'src/library') not in after


def test_physical_alias_deny_prevents_covering_immutable_directory(tree):
    p = tree
    protected = p.source / 'src/library'
    alias = p.source.parent / 'denied-alias'
    alias.symlink_to(protected, target_is_directory=True)
    before, explicit = permission_map(p, extra={str(alias): 'deny'})
    after = compact(p, before, explicit)
    assert_explicit_preserved(before, after, explicit)
    assert str(protected) not in after
    for relative in ('src/library/first.rs', 'src/library/nested/second.rs'):
        assert after[str(p.source / relative)] == 'read'


@pytest.mark.parametrize('failure', ['permission', 'not-directory'])
def test_uncertain_directory_keeps_original_immutable_leaf_grants(tree, monkeypatch, failure):
    p = tree
    protected = p.source / 'tests'
    original = Path.lstat

    def uncertain(path, *args, **kwargs):
        if path == protected:
            if failure == 'permission':
                raise PermissionError('Fixture directory identity unavailable')
            raise NotADirectoryError('Fixture directory identity changed')
        return original(path, *args, **kwargs)

    before, explicit = permission_map(p)
    monkeypatch.setattr(Path, 'lstat', uncertain)
    after = compact(p, before, explicit)
    assert str(protected) not in after
    for relative in ('tests/first.rs', 'tests/nested/second.rs'):
        assert after[str(p.source / relative)] == 'read'


@pytest.mark.parametrize('mutable', ['../outside.rs', '/outside.rs', 'src/../outside.rs'])
def test_unsafe_mutable_spellings_leave_the_original_map_unchanged(tree, mutable):
    p = tree
    p.recipe['mutable'].append(mutable)
    before, explicit = permission_map(p)
    assert compact(p, before, explicit) == before


def test_symlinked_source_falls_back_without_reinterpreting_source_identity(tree):
    p = tree
    source = p.source
    alias = source.parent / 'source-alias'
    alias.symlink_to(source, target_is_directory=True)
    p.source = alias
    p.recipe['source'] = str(alias)
    before, explicit = permission_map(p)
    assert compact(p, before, explicit) == before


def profile_fixture(p, monkeypatch):
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = p.state
    monkeypatch.setattr(Path, 'home', lambda: p.home)
    exists = Path.exists

    def no_host_config(path):
        if path.name == 'config.toml' and path.parent.name == '.codex':
            return False
        return exists(path)

    monkeypatch.setattr(Path, 'exists', no_host_config)
    monkeypatch.setattr(instructions, 'runtime_overrides', lambda manifest: [])
    return runtime


def fake_effective_config(profile):
    return {
        'approval_policy': 'never', 'web_search': 'disabled', 'default_permissions': PROFILE,
        'features': {feature: False for feature in FEATURES},
        'permissions': {PROFILE: copy.deepcopy(profile)},
        'shell_environment_policy': {'inherit': 'none', 'set': profile['shell_environment']},
    }


@pytest.mark.parametrize('assessment', [False, True])
def test_model_profile_serializes_the_compacted_map_and_verifies_exact_readback(tree, monkeypatch, assessment):
    p = tree
    runtime = profile_fixture(p, monkeypatch)
    overrides, profile = runtime._profile(p.recipe, assessment=assessment)
    serialized = next(value for value in overrides if value.startswith('permissions.' + PROFILE + '='))
    assert tomllib.loads(serialized)['permissions'][PROFILE]['filesystem'] == profile['filesystem']
    assert profile['filesystem'][str(p.source)] == ('read' if assessment else 'write')
    assert profile['filesystem'][str(p.outputs)] == 'write'
    assert str(p.source / 'tests/first.rs') not in profile['filesystem']
    if not assessment:
        assert profile['filesystem'][str(p.source / 'tests')] == 'read'
    config = fake_effective_config(profile)
    runtime._verify_config(config, profile)
    config['permissions'][PROFILE]['filesystem'][str(p.source / 'tests/first.rs')] = 'read'
    with pytest.raises(Rejected, match='Effective filesystem'):
        runtime._verify_config(config, profile)


@pytest.mark.parametrize('assessment', [False, True])
def test_profile_compacts_only_after_native_alias_closure_and_instruction_support(tree, monkeypatch, assessment):
    p = tree
    runtime = profile_fixture(p, monkeypatch)
    private = p.source.parent / 'native-private'
    private.mkdir()
    selected = private / 'selected'
    selected.write_text('owned native-only fixture')
    alias = p.source.parent / 'native-alias'
    alias.symlink_to(private, target_is_directory=True)
    alias_file = alias / selected.name
    support = p.source / 'tests/first.rs'
    p.recipe['read_roots'] = [str(private), str(alias_file)]
    p.recipe['instruction_manifest'] = {'test-support': str(support)}
    p.recipe['native_sandbox'] = {'version': native_sandbox.VERSION}
    monkeypatch.setattr(instructions, 'validate', lambda *args, **kwargs: [str(support)])
    monkeypatch.setattr(native_sandbox, 'config', lambda recipe: {
        'file_read_paths': [{'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}]})
    original = CodexRuntime._compact_model_immutable_reads
    captured = []

    def after_closure(recipe, filesystem, explicit_paths):
        assert filesystem[str(selected)] == filesystem[str(alias_file)] == 'deny'
        assert filesystem[str(support)] == 'read'
        assert {str(support), str(private), str(alias_file)}.issubset(explicit_paths)
        captured.append(copy.deepcopy(filesystem))
        return original(recipe, filesystem, explicit_paths)

    monkeypatch.setattr(CodexRuntime, '_compact_model_immutable_reads', staticmethod(after_closure))
    _, profile = runtime._profile(p.recipe, assessment=assessment)
    assert len(captured) == 1
    assert profile['filesystem'][str(selected)] == profile['filesystem'][str(alias_file)] == 'deny'
    assert profile['filesystem'][str(support)] == 'read'
    assert profile['filesystem'][str(private)] == 'read'


@pytest.mark.parametrize('assessment', [False, True])
def test_native_profile_bypasses_compaction_and_keeps_original_serialized_bytes(tree, monkeypatch, assessment):
    p = tree
    runtime = profile_fixture(p, monkeypatch)
    generated, cache = p.source / 'target', p.source.parent / 'native-cache'
    generated.mkdir()
    cache.mkdir()
    declarations = [{'path': str(generated), 'kind': 'generated'}, {'path': str(cache), 'kind': 'cache'}]
    monkeypatch.setattr(native_paths, 'validate', lambda *args, **kwargs: declarations)
    monkeypatch.setattr(CodexRuntime, '_compact_model_immutable_reads', staticmethod(
        lambda recipe, filesystem, explicit_paths: dict(filesystem)))
    previous = runtime._profile(p.recipe, assessment=assessment, native=True)

    def must_not_run(*args, **kwargs):
        pytest.fail('Model compaction must never run for a native profile')

    monkeypatch.setattr(CodexRuntime, '_compact_model_immutable_reads', staticmethod(must_not_run))
    current = runtime._profile(p.recipe, assessment=assessment, native=True)
    assert current == previous
    filesystem = current[1]['filesystem']
    assert filesystem[str(p.source)] == ('read' if assessment else 'write')
    assert filesystem[str(generated)] == filesystem[str(cache)] == 'write'
    for relative in p.recipe['immutable']:
        assert filesystem[str(p.source / relative)] == 'read'
    assert str(p.source / 'tests') not in filesystem


MODEL_PERMISSION_PROGRAM = r'''
import errno,hashlib,json,os,sys
from pathlib import Path
if sys.stdin.readline() != 'release\n':
 raise SystemExit(125)
p=json.loads(sys.argv[1]); rows={}; owned=[]
def denied(name,action):
 try:result=action()
 except OSError as error:
  rows[name]={'allowed':False,'errno':error.errno,'type':type(error).__name__}
 else:
  # Descriptor handling is outside the denial catch: failure to close cannot
  # masquerade as a denied open.
  if isinstance(result,int):os.close(result)
  rows[name]={'allowed':True}
def identity(path):
 info=Path(path).lstat()
 return {'dev':info.st_dev,'ino':info.st_ino,'uid':info.st_uid,'mode':info.st_mode}
for item in p['victims']:
 path=item['path']; action=item['action']; name=item['name']
 rows[name+'_read']={'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}
 if action=='write':denied(name,lambda path=path:os.open(path,os.O_WRONLY))
 elif action=='unlink':denied(name,lambda path=path:os.unlink(path))
 elif action=='rename':denied(name,lambda path=path,item=item:os.rename(path,item['destination']))
 elif action=='hardlink':denied(name,lambda path=path,item=item:os.link(path,item['destination']))
 else:raise AssertionError('Unexpected fixed fixture action')
mutable=Path(p['mutable']); replacement=Path(p['replacement'])
new_parent=Path(p['new_parent']); new_nested=Path(p['new_nested']); new_file=Path(p['new_file'])
if p['assessment']:
 denied('mutable_write',lambda:os.open(mutable,os.O_WRONLY))
 denied('atomic_temporary',lambda:os.open(replacement,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600))
 denied('new_parent',lambda:os.mkdir(new_parent,0o700))
else:
 fd=os.open(replacement,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:os.write(fd,b'authorized atomic replacement\n')
 finally:os.close(fd)
 os.replace(replacement,mutable)
 rows['atomic_replace']={'sha256':hashlib.sha256(mutable.read_bytes()).hexdigest(),'identity':identity(mutable)}
 os.mkdir(new_parent,0o700);owned.append({'path':str(new_parent),'identity':identity(new_parent)})
 os.mkdir(new_nested,0o700);owned.append({'path':str(new_nested),'identity':identity(new_nested)})
 fd=os.open(new_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:os.write(fd,b'authorized absent mutable file\n')
 finally:os.close(fd)
 owned.append({'path':str(new_file),'identity':identity(new_file)})
 rows['new_mutable']={'sha256':hashlib.sha256(new_file.read_bytes()).hexdigest()}
output=Path(p['output'])
fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
try:os.write(fd,b'authorized output\n')
finally:os.close(fd)
owned.append({'path':str(output),'identity':identity(output)})
rows['output']={'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
print(json.dumps({'pid':os.getpid(),'parent':os.getppid(),'rows':rows,'owned':owned},sort_keys=True),flush=True)
'''


def fixture_identity(path):
    info = path.lstat()
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid, 'mode': info.st_mode}


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual pinned macOS model permission boundary')
@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
def test_actual_model_compaction_preserves_immutable_aliases_and_authorized_edits(tree, assessment):
    p = tree
    assert version('openai-codex') == version('openai-codex-cli-bin') == PIN
    binary = Path(bundled_codex_path()).resolve()
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    interpreter = Path(sys._base_executable).resolve()
    p.recipe['probe_interpreter'] = str(interpreter)
    p.recipe['read_roots'] = sorted({str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()),
                                    str(interpreter.parent.parent)})
    p.recipe['mutable'].append('future/deep/new.rs')
    protected = p.source / 'src/library'
    directory_link = p.source / 'src/core/linked-library'
    directory_link.symlink_to(protected, target_is_directory=True)
    victim_parent = protected / 'probes'
    victim_parent.mkdir()
    seeds, victims = {}, []
    for spelling in ('canonical', 'case', 'firmlink', 'symlink'):
        for action in ('write', 'unlink', 'rename', 'hardlink'):
            name = spelling + '_' + action
            canonical = victim_parent / (name + '.txt')
            canonical.write_text('immutable owned fixture: ' + name + '\n')
            sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
            p.recipe['immutable'][str(canonical.relative_to(p.source))] = sha
            seeds[str(canonical)] = {'identity': fixture_identity(canonical), 'sha256': sha}
            if spelling == 'canonical':
                alias = canonical
            elif spelling == 'case':
                alias = p.source / 'sRc/LiBrArY/PrObEs' / canonical.name.upper()
            elif spelling == 'firmlink':
                alias = Path('/System/Volumes/Data') / canonical.relative_to('/')
            else:
                alias = directory_link / 'probes' / canonical.name
            if not alias.exists() or not alias.samefile(canonical):
                pytest.skip('Required case/firmlink alias is unavailable on this fixture volume')
            victims.append({'name': name, 'action': action, 'path': str(alias), 'sha256': sha,
                            'destination': str(p.outputs / (name + '-unexpected'))})
    roots_before = {str(path): fixture_identity(path) for path in (p.source, p.outputs, p.state, protected)}
    link_before = fixture_identity(directory_link)
    mutable = p.source / p.recipe['mutable'][0]
    mutable_before = {'identity': fixture_identity(mutable), 'sha256': hashlib.sha256(mutable.read_bytes()).hexdigest()}
    payload = {'assessment': assessment, 'victims': victims, 'mutable': str(mutable),
               'replacement': str(mutable.parent / '.cpu.rs.atomic-owned'),
               'new_parent': str(p.source / 'future'), 'new_nested': str(p.source / 'future/deep'),
               'new_file': str(p.source / 'future/deep/new.rs'), 'output': str(p.outputs / 'owned-result')}
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = p.state
    overrides, profile = runtime._profile(p.recipe, assessment=assessment)
    assert all(path not in profile['filesystem'] for path in seeds)
    if assessment:
        assert profile['filesystem'][str(p.source)] == 'read'
    else:
        assert profile['filesystem'][str(p.source)] == 'write'
        assert profile['filesystem'][str(protected)] == 'read'
    argv = [str(binary)]
    for override in overrides:
        argv.extend(['-c', override])
    argv.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', str(p.source),
                 str(interpreter), '-I', '-S', '-B', '-c', MODEL_PERMISSION_PROGRAM, json.dumps(payload)])
    stdout_path, stderr_path = p.state / 'model-probe.stdout', p.state / 'model-probe.stderr'
    child = saved = None
    launch_attempted = reaped = False
    result = None
    error = None
    cleanup_errors = []
    started = time.monotonic()
    try:
        with stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr:
            launch_attempted = True
            child = subprocess.Popen(argv, cwd=p.source, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                     env=clean_environment(), start_new_session=True)
            saved = native_processes.identity(child.pid)
            assert saved and saved['uid'] == os.getuid() and saved['parent'] == os.getpid()
            assert saved['group'] == child.pid
            child.stdin.write(b'release\n')
            child.stdin.close()
            child.wait(timeout=max(0.1, 20 - (time.monotonic() - started)))
            reaped = True
    except BaseException as failure:
        error = {'type': type(failure).__name__, 'message': str(failure)}
    finally:
        if child is not None:
            if child.stdin and not child.stdin.closed:
                try:
                    child.stdin.close()
                except OSError as failure:
                    cleanup_errors.append({'type': type(failure).__name__, 'message': str(failure)})
            try:
                if child.poll() is None:
                    # The Popen handle still owns this unreaped direct child;
                    # never target a process group or a separately discovered PID.
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
            except BaseException as failure:
                cleanup_errors.append({'type': type(failure).__name__, 'message': str(failure)})
            reaped = child.returncode is not None
        evidence = {'assessment': assessment, 'profile': profile, 'command_sha256': hashlib.sha256(
                    json.dumps(argv).encode()).hexdigest(), 'binary_sha256': binary_hash,
                    'runtime_source_sha256': hashlib.sha256(Path(sys.modules[CodexRuntime.__module__].__file__).read_bytes()).hexdigest(),
                    'test_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    'launch_attempted': launch_attempted, 'direct_child_identity': saved,
                    'direct_child_reaped': reaped, 'returncode': child.returncode if child else None,
                    'error': error, 'cleanup_errors': cleanup_errors,
                    'model_calls': 0, 'shared_journals_used': False,
                    'descendant_cleanup_claimed': False, 'elapsed_seconds': time.monotonic() - started,
                    'fixture_roots': roots_before, 'immutable_before': seeds}
        (p.state / 'model-compaction-permission-proof.json').write_text(json.dumps(evidence, indent=2) + '\n')
    assert error is None, error
    assert not cleanup_errors, cleanup_errors
    assert reaped and child.returncode == 0, stderr_path.read_text()[:8192]
    assert stdout_path.stat().st_size <= 65536 and stderr_path.stat().st_size <= 65536
    result = json.loads(stdout_path.read_text())
    assert isinstance(result['pid'], int) and result['pid'] > 0
    rows = result['rows']
    for victim in victims:
        assert rows[victim['name'] + '_read']['sha256'] == victim['sha256']
        assert rows[victim['name']]['allowed'] is False, (victim['name'], rows[victim['name']])
        assert rows[victim['name']]['errno'] in (1, 13), (victim['name'], rows[victim['name']])
        assert rows[victim['name']]['type'] == 'PermissionError'
        assert not Path(victim['destination']).exists()
    for path, before in seeds.items():
        assert fixture_identity(Path(path)) == before['identity']
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before['sha256']
    assert fixture_identity(directory_link) == link_before
    assert {path: fixture_identity(Path(path)) for path in roots_before} == roots_before
    if assessment:
        for name in ('mutable_write', 'atomic_temporary', 'new_parent'):
            assert rows[name]['allowed'] is False and rows[name]['errno'] in (1, 13)
            assert rows[name]['type'] == 'PermissionError'
        assert fixture_identity(mutable) == mutable_before['identity']
        assert hashlib.sha256(mutable.read_bytes()).hexdigest() == mutable_before['sha256']
        assert not Path(payload['new_parent']).exists()
    else:
        assert mutable.read_bytes() == b'authorized atomic replacement\n'
        assert fixture_identity(mutable) == rows['atomic_replace']['identity']
        assert fixture_identity(mutable)['ino'] != mutable_before['identity']['ino']
        assert Path(payload['new_file']).read_bytes() == b'authorized absent mutable file\n'
    assert not Path(payload['replacement']).exists()
    assert Path(payload['output']).read_bytes() == b'authorized output\n'
    expected_owned = [payload['output']] if assessment else [payload[key] for key in (
        'new_parent', 'new_nested', 'new_file', 'output')]
    assert [item['path'] for item in result['owned']] == expected_owned
    for owned in result['owned']:
        assert fixture_identity(Path(owned['path'])) == owned['identity']
        assert owned['identity']['uid'] == os.getuid()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == binary_hash
    # Retain the fixture and complete output for the normal pytest temporary
    # directory lifecycle. No recursive or post-timeout cleanup is performed.
    evidence['observed'] = result
    evidence['immutable_preserved'] = True
    evidence['owned_outputs_verified'] = True
    (p.state / 'model-compaction-permission-proof.json').write_text(json.dumps(evidence, indent=2) + '\n')
