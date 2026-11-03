"""Opt-in exact model writes; fake runner tests are not OS qualification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import codex_runtime, instructions, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source, outputs, state, home = [root / name for name in ('source', 'outputs', 'state', 'home')]
    for path in (source, outputs, state, home):
        path.mkdir()
    (source / 'src').mkdir()
    names = [f'src/file-{index}.txt' for index in range(15)]
    for name in [*names, 'immutable.txt']:
        (source / name).write_bytes(('fixture ' + name + '\n').encode())
    recipe = {
        'source': str(source), 'outputs': str(outputs), 'read_roots': [], 'mutable': names,
        'immutable': {'immutable.txt': hashlib.sha256((source / 'immutable.txt').read_bytes()).hexdigest()},
        'probe_interpreter': sys.executable, 'checks': [{'argv': [sys.executable]}],
        'model_write_scope': 'existing-mutable-files',
    }
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = state
    runtime._save = lambda record: None
    monkeypatch.setattr(Path, 'home', lambda: home)
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: False if path.name == 'config.toml'
                        and path.parent.name == '.codex' else exists(path))
    monkeypatch.setattr(instructions, 'runtime_overrides', lambda manifest: [])
    return SimpleNamespace(root=root, source=source, outputs=outputs, state=state,
                           home=home, recipe=recipe, runtime=runtime)


def expected_checks(count=15):
    names = {'state_read_denied', 'state_write_denied', 'credentials_denied',
             'credential_config_denied', 'immutable_write_denied', 'output_write',
             'source_access', 'network_denied'}
    for index in range(count):
        names.update({f'mutable_access_{index}', f'mutable_unchanged_{index}'})
    return {name: True for name in names}


def profile(p, assessment=False):
    return p.runtime._profile(p.recipe, assessment)[1]['filesystem']


def probe(p, monkeypatch, *, assessment=False, answer=None, action=None, native=False):
    captured = []
    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        if action:
            action(argv)
        value = expected_checks() if answer is None else answer
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')
    monkeypatch.setattr(codex_runtime.subprocess, 'run', run)
    result = p.runtime._probe_sandbox(
        {'attempt_id': 'owned-exact-probe', 'assignment': {'kind': 'assessment' if assessment else 'implementation'}},
        p.recipe, native=native)
    return result, captured


@pytest.mark.parametrize('assessment', [False, True])
def test_exact_map_keeps_only_declared_writer_leaves_and_output_writable(fixture, assessment):
    p = fixture
    before = copy.deepcopy(p.recipe)
    mapping = profile(p, assessment)
    assert p.recipe == before
    assert mapping[str(p.source)] == 'read'
    assert mapping[str(p.outputs)] == 'write'
    assert mapping[str(p.state)] == 'deny'
    assert str(p.source / 'immutable.txt') not in mapping  # R19 redundant-read pruning.
    for name in p.recipe['mutable']:
        assert CodexRuntime._model_path_access(mapping, p.source / name) == ('read' if assessment else 'write')
    for name in ('unlisted.txt', 'src/.temporary-sibling', 'src/new-file.txt'):
        assert CodexRuntime._model_path_access(mapping, p.source / name) == 'read'


@pytest.mark.parametrize('value', [None, False, '', 'all-source', [], {}])
def test_unsupported_scope_fails_closed(fixture, value):
    p = fixture
    p.recipe['model_write_scope'] = value
    with pytest.raises(Rejected, match='Unsupported'):
        profile(p)
    with pytest.raises(Rejected, match='Unsupported'):
        CodexRuntime._existing_mutable_targets(p.recipe)


@pytest.mark.parametrize('bad', ['missing', 'directory', 'symlink', 'parent-symlink', 'hardlink',
                               'fifo', 'duplicate', 'absolute', 'dotdot', 'immutable'])
def test_invalid_existing_targets_never_fall_back_to_broad_writes(fixture, bad):
    p = fixture
    target = p.source / p.recipe['mutable'][0]
    if bad == 'missing':
        target.unlink()
    elif bad == 'directory':
        target.unlink(); target.mkdir()
    elif bad == 'symlink':
        target.unlink(); target.symlink_to(p.source / p.recipe['mutable'][1])
    elif bad == 'parent-symlink':
        alias = p.source / 'alias'; alias.symlink_to(p.source / 'src', target_is_directory=True)
        p.recipe['mutable'][0] = 'alias/file-0.txt'
    elif bad == 'hardlink':
        os.link(target, p.outputs / 'link')
    elif bad == 'fifo':
        target.unlink(); os.mkfifo(target)
    elif bad == 'duplicate':
        p.recipe['mutable'].append(p.recipe['mutable'][0])
    elif bad == 'absolute':
        p.recipe['mutable'][0] = str(target)
    elif bad == 'dotdot':
        p.recipe['mutable'][0] = 'src/../src/file-0.txt'
    else:
        p.recipe['mutable'].append('immutable.txt')
    with pytest.raises(Rejected):
        profile(p)


@pytest.mark.parametrize('access', ['read', 'deny'])
@pytest.mark.parametrize('alias', [False, True])
def test_explicit_nonwritable_boundary_cannot_be_reopened(fixture, access, alias):
    p = fixture
    target = p.source / p.recipe['mutable'][0]
    grant = target
    if alias:
        grant = p.root / 'alias'; grant.symlink_to(target)
    mapping = {':root': 'deny', str(p.source): 'write', str(p.outputs): 'write', str(grant): access}
    with pytest.raises(Rejected, match='cannot reopen'):
        CodexRuntime._existing_mutable_map(p.recipe, mapping, False)


def test_instruction_read_cannot_be_reopened_by_exact_writer(fixture, monkeypatch):
    p = fixture
    selected = p.source / p.recipe['mutable'][0]
    p.recipe['instruction_manifest'] = {'chain': [], 'support': [], 'configs': []}
    monkeypatch.setattr(instructions, 'validate', lambda *args, **kwargs: [str(selected)])
    with pytest.raises(Rejected, match='cannot reopen'):
        profile(p)


@pytest.mark.parametrize('assessment', [False, True])
def test_native_file_deny_and_readable_neighbor_survive_opt_in(fixture, assessment):
    p = fixture
    parent = p.root / 'native-data'; parent.mkdir()
    selected = parent / 'private'; selected.write_bytes(b'native fixture')
    p.recipe['read_roots'].append(str(parent))
    p.recipe['native_sandbox'] = {'version': native_sandbox.VERSION, 'file_read_paths': [
        {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}]}
    mapping = profile(p, assessment)
    assert mapping[str(selected)] == 'deny'
    assert mapping[str(parent)] == 'read'
    assert CodexRuntime._model_path_access(mapping, parent / 'neighbor') == 'read'


def test_local_relation_cache_is_shared_across_targets(fixture, monkeypatch):
    p = fixture
    seen = []
    original = CodexRuntime._model_path_access
    def access(filesystem, path, cache=None):
        seen.append(cache)
        return original(filesystem, path, cache)
    monkeypatch.setattr(CodexRuntime, '_model_path_access', staticmethod(access))
    profile(p)
    assert len(seen) == 15 and seen[0] is not None
    assert all(cache is seen[0] for cache in seen)


@pytest.mark.parametrize('assessment', [False, True])
def test_probe_binds_all_files_and_preserves_every_byte(fixture, monkeypatch, assessment):
    p = fixture
    before = {name: (p.source / name).read_bytes() for name in p.recipe['mutable']}
    result, calls = probe(p, monkeypatch, assessment=assessment)
    assert result['checks'] == expected_checks()
    assert len(calls) == 1 and 0 < calls[0][1]['timeout'] <= 15
    contract = json.loads(calls[0][0][-1])
    assert [item['path'] for item in contract] == [str(p.source / name) for name in p.recipe['mutable']]
    assert all(item['write'] is not assessment for item in contract)
    assert all(item['state']['sha256'] == hashlib.sha256(before[name]).hexdigest()
               for name, item in zip(p.recipe['mutable'], contract, strict=True))
    assert before == {name: (p.source / name).read_bytes() for name in before}


@pytest.mark.parametrize('defect', ['missing-first', 'missing-last', 'false', 'integer', 'extra', 'list'])
def test_incomplete_or_false_model_evidence_cannot_pass(fixture, monkeypatch, defect):
    answer = expected_checks()
    if defect == 'missing-first': answer.pop('mutable_access_0')
    elif defect == 'missing-last': answer.pop('mutable_unchanged_14')
    elif defect == 'false': answer['mutable_access_14'] = False
    elif defect == 'integer': answer['mutable_access_0'] = 1
    elif defect == 'extra': answer['unrequested'] = True
    else: answer = [True]
    with pytest.raises(Rejected):
        probe(fixture, monkeypatch, answer=answer)


@pytest.mark.parametrize('missing', [None, 'instruction_read_0', 'instruction_write_denied_0',
                                   'native_path_access_0', 'native_file_read_denied_0'])
def test_nonempty_prior_controls_remain_mandatory(fixture, monkeypatch, missing):
    p = fixture
    immutable = p.source / 'immutable.txt'
    p.recipe['instruction_manifest'] = {'chain': [{'path': str(immutable),
        'sha256': hashlib.sha256(immutable.read_bytes()).hexdigest()}], 'support': [], 'configs': []}
    monkeypatch.setattr(instructions, 'validate', lambda *args, **kwargs: [str(immutable)])
    cache = p.root / 'cache'; cache.mkdir()
    p.recipe['native_write_roots'] = [{'path': str(cache), 'kind': 'cache'}]
    selected = p.root / 'native-data'; selected.write_bytes(b'native fixture')
    p.recipe['native_sandbox'] = {'version': native_sandbox.VERSION, 'file_read_paths': [
        {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}]}
    answer = {**expected_checks(), 'instruction_read_0': True, 'instruction_write_denied_0': True,
              'native_path_access_0': True, 'native_file_read_denied_0': True}
    if missing:
        answer.pop(missing)
        with pytest.raises(Rejected, match='incomplete'):
            probe(p, monkeypatch, answer=answer)
    else:
        assert probe(p, monkeypatch, answer=answer)[0]['checks'] == answer


@pytest.mark.parametrize('change', ['content', 'replacement'])
def test_parent_rechecks_mutable_hash_and_identity_after_probe(fixture, monkeypatch, change):
    p = fixture
    target = p.source / p.recipe['mutable'][0]
    def changed(argv):
        if change == 'content':
            target.write_bytes(b'fixture drift')
        else:
            substitute = p.outputs / 'replacement'
            substitute.write_bytes(target.read_bytes())
            substitute.replace(target)
    with pytest.raises(Rejected, match='changed'):
        probe(p, monkeypatch, action=changed)


@pytest.mark.parametrize('assessment', [False, True])
def test_model_source_payload_uses_only_nondestructive_mutable_opens(fixture, monkeypatch, assessment):
    p = fixture
    _, calls = probe(p, monkeypatch, assessment=assessment)
    argv = calls[0][0]
    index = next(index for index, value in enumerate(argv) if value.startswith('import hashlib,json,os,socket,sys\n'))
    code = argv[index]
    block = code[code.index('import stat\n'):code.index('for index,item in enumerate(json.loads(sys.argv[9])):')]
    contract = json.loads(argv[-1]); original = os.open; opened = []
    targets = {item['path'] for item in contract}
    before = {path: Path(path).read_bytes() for path in targets}
    def guarded(path, flags, *args):
        if flags & os.O_WRONLY:
            if str(path) not in targets:
                raise PermissionError('simulated unlisted-source denial')
            opened.append((str(path), flags))
            assert flags == os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if assessment:
                raise PermissionError('simulated assessor denial')
        return original(path, flags, *args)
    answer = {}
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', guarded)
        exec(compile(block, '<exact-model-source-probe-fixture>', 'exec'), {
            'source': p.source, 'Path': Path, 'os': os, 'json': json, 'hashlib': hashlib,
            'sys': SimpleNamespace(argv=['-c', *argv[index + 1:]]), 'answer': answer,
        })
    assert len(opened) == 15 and len(answer) == 31 and all(answer.values())
    assert before == {path: Path(path).read_bytes() for path in before}
    assert not (p.source / 'probe-owned-exact-probe.txt').exists()


def source_block(p, monkeypatch, assessment=False):
    _, calls = probe(p, monkeypatch, assessment=assessment)
    argv = calls[0][0]
    index = next(index for index, value in enumerate(argv) if value.startswith('import hashlib,json,os,socket,sys\n'))
    code = argv[index]
    block = code[code.index('import stat\n'):code.index('for index,item in enumerate(json.loads(sys.argv[9])):')]
    answer = {}
    environment = {'source': p.source, 'Path': Path, 'os': os, 'json': json, 'hashlib': hashlib,
                   'sys': SimpleNamespace(argv=['-c', *argv[index + 1:]]), 'answer': answer}
    return block, environment, answer


def test_successful_source_create_followed_by_cleanup_denial_is_not_permission_denial(fixture, monkeypatch):
    p = fixture
    block, environment, answer = source_block(p, monkeypatch)
    canary = p.source / 'probe-owned-exact-probe.txt'
    unlink = Path.unlink
    def deny_cleanup(path, *args, **kwargs):
        if path == canary:
            raise PermissionError('simulated denied cleanup after successful creation')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', deny_cleanup)
        with pytest.raises(PermissionError, match='cleanup'):
            exec(compile(block, '<post-create-failure>', 'exec'), environment)
    assert answer['source_access'] is False
    assert canary.read_bytes() == b''
    canary.unlink()


@pytest.mark.parametrize('assessment', [False, True])
def test_successful_mutable_open_followed_by_metadata_denial_cannot_pass(fixture, monkeypatch, assessment):
    p = fixture
    block, environment, answer = source_block(p, monkeypatch, assessment)
    original_open, original_stat, original_close = os.open, os.fstat, os.close
    writable = set()
    def opened(path, flags, *args):
        if Path(path).name == 'probe-owned-exact-probe.txt':
            raise PermissionError('simulated initial creation denial')
        fd = original_open(path, flags, *args)
        if flags & os.O_WRONLY:
            writable.add(fd)
        return fd
    def failed_metadata(fd):
        if fd in writable:
            raise PermissionError('simulated post-open metadata denial')
        return original_stat(fd)
    def closed(fd):
        writable.discard(fd)
        return original_close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', opened)
        patch.setattr(os, 'fstat', failed_metadata)
        patch.setattr(os, 'close', closed)
        with pytest.raises(PermissionError, match='metadata'):
            exec(compile(block, '<post-open-failure>', 'exec'), environment)
    assert answer == {'source_access': True}
    assert not writable


@pytest.mark.parametrize('assessment', [False, True])
def test_valid_opt_in_does_not_change_native_profile_payload_or_argv(fixture, monkeypatch, assessment):
    p = fixture
    p.recipe['native_sandbox'] = {'version': native_sandbox.VERSION}
    old = copy.deepcopy(p.recipe); old.pop('model_write_scope')
    profile_before = p.runtime._profile(old, assessment, native=True)
    assert p.runtime._profile(p.recipe, assessment, native=True) == profile_before
    captured = []
    monkeypatch.setattr(codex_runtime.subprocess, 'run', lambda argv, **kwargs:
                        captured.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout='{"native_fixture":true}', stderr=''))
    for recipe in (old, p.recipe):
        p.runtime._probe_sandbox({'attempt_id': 'same', 'assignment': {'kind': 'assessment' if assessment else 'implementation'}},
            recipe, native=True, native_profile=str(p.outputs / 'fixed-native.sb'), network_observation={'denied_ports': [43123]})
    assert captured[0] == captured[1]
    assert 'mutable_access_' not in next(value for value in captured[0][0] if value.startswith('import hashlib,json,os,socket,sys\n'))


def test_scope_absence_keeps_original_writer_creation_and_probe_shape(fixture, monkeypatch):
    p = fixture
    p.recipe.pop('model_write_scope')
    assert profile(p)[str(p.source)] == 'write'
    _, calls = probe(p, monkeypatch, answer={'legacy_fixture': True})
    code = next(value for value in calls[0][0] if value.startswith('import hashlib,json,os,socket,sys\n'))
    assert "p.unlink(); answer['source_access']=not reviewer" in code
    assert 'mutable_access_' not in code
    assert calls[0][1]['timeout'] == 15


@pytest.mark.parametrize('invalid', ['hardlink', 'unsupported'])
def test_invalid_target_is_rejected_before_model_reservation(tmp_path, monkeypatch, invalid):
    from test_aos_check_adapters import prepared_checks, claim
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=lambda recipe: recipe.update(
        model_write_scope='unsupported' if invalid == 'unsupported' else 'existing-mutable-files'))
    target = Path(p.recipe['source']) / p.recipe['mutable'][0]
    # A second link preserves file bytes/baseline but invalidates exact-file authority.
    if invalid == 'hardlink':
        os.link(target, tmp_path / 'second-link')
    try:
        with pytest.raises(Rejected, match='Unsupported|ordinary targets'):
            p.runtime.start(claim(p))
        assert p.runtime.budget_receipt()['attempts_reserved'] == 0
        assert not p.backend.calls
    finally:
        p.runtime.close()
