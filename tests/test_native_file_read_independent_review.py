"""Independent exact native-read permission and mutation probes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentic_tools.aos import native_sandbox
from agentic_tools.aos.store import Rejected


def _case(tmp_path):
    root = tmp_path / 'native-input'
    root.mkdir()
    selected = root / 'tool-settings'
    selected.write_bytes(b'[tool]\nmode = bounded\n')
    entry = {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}
    return selected, {'native_sandbox': {'version': native_sandbox.VERSION,
                                       'file_read_paths': [entry]}}, {':root': 'deny', ':minimal': 'read'}


def _alias(path):
    alternate = Path('/System/Volumes/Data') / path.relative_to('/')
    if sys.platform != 'darwin' or not alternate.exists() or not os.path.samefile(path, alternate):
        pytest.skip('fixture has no physical volume alias')
    return alternate


@pytest.mark.parametrize('access', ['deny', 'write'])
@pytest.mark.parametrize('use_alias', [False, True])
def test_conflicting_scope_is_rejected_before_selected_content_is_opened(tmp_path, monkeypatch, access, use_alias):
    selected, recipe, mapping = _case(tmp_path)
    guard = _alias(selected.parent) if use_alias else selected.parent
    mapping[str(guard)] = access
    opened = []
    original_open = os.open

    def observe_open(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', observe_open)
    with pytest.raises(Rejected, match='denied namespace|writable scopes'):
        native_sandbox.verify_file_read_paths(recipe, mapping)
    assert opened == []


def test_temporary_alias_overlap_is_rejected_before_content_open(tmp_path, monkeypatch):
    selected, recipe, mapping = _case(tmp_path)
    recipe['native_sandbox']['temporary_paths'] = [
        {'parent': str(_alias(selected.parent)), 'name': selected.name, 'kind': 'file'}]
    opened = []
    original_open = os.open

    def observe_open(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', observe_open)
    with pytest.raises(Rejected, match='temporary scopes'):
        native_sandbox.verify_file_read_paths(recipe, mapping)
    assert opened == []


@pytest.mark.parametrize('change', ['content', 'inode', 'mode', 'hardlink'])
def test_mutation_during_bound_file_read_is_rejected(tmp_path, monkeypatch, change):
    selected, recipe, mapping = _case(tmp_path)
    original_fdopen = os.fdopen

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            assert count == 1024 * 1024 + 1
            data = self.stream.read(count)
            if change == 'content':
                selected.write_bytes(b'changed bytes')
            elif change == 'inode':
                replacement = selected.with_name('replacement')
                replacement.write_bytes(data)
                replacement.replace(selected)
            elif change == 'mode':
                selected.chmod(0o700)
            else:
                os.link(selected, selected.with_name('extra-link'))
            return data

    monkeypatch.setattr(os, 'fdopen', lambda *args, **kwargs: MutatingStream(original_fdopen(*args, **kwargs)))
    with pytest.raises(Rejected, match='identity changed'):
        native_sandbox.verify_file_read_paths(recipe, mapping)


def test_file_read_size_limit_rejects_before_content_open(tmp_path, monkeypatch):
    selected, recipe, mapping = _case(tmp_path)
    with selected.open('r+b') as stream:
        stream.truncate(1024 * 1024 + 1)
    opened = []
    original_open = os.open

    def observe_open(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', observe_open)
    with pytest.raises(Rejected, match='one MiB'):
        native_sandbox.verify_file_read_paths(recipe, mapping)
    assert opened == []


def test_pin_is_reverified_by_render_after_configuration_validation(tmp_path):
    selected, recipe, mapping = _case(tmp_path)
    native_sandbox.config(recipe)
    selected.write_text('changed after validation')
    with pytest.raises(Rejected, match='binding changed'):
        native_sandbox.render(recipe, mapping, preexisting={})


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual native data-only mapping permission')
def test_actual_native_file_read_does_not_allow_executable_mapping_or_neighbors(tmp_path):
    selected, recipe, mapping = _case(tmp_path)
    sibling = selected.with_name('private-neighbor')
    sibling.write_text('private')
    mapping[str(Path(sys.base_prefix).resolve())] = 'read'
    original_mapping = dict(mapping)
    profile = native_sandbox.render(recipe, mapping, preexisting={})
    assert mapping == original_mapping
    saved = tmp_path / 'data-only-profile.sb'
    saved.write_text(profile)
    program = '''import json,mmap,os,sys
from pathlib import Path
p=Path(sys.argv[1]);r={'read':p.read_text()}
def attempt(name,fn):
 try:r[name]={'ok':True,'value':fn()}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
def executable_map():
 with p.open('rb') as f:
  with mmap.mmap(f.fileno(),0,prot=mmap.PROT_READ|mmap.PROT_EXEC):return True
attempt('executable_map',executable_map)
attempt('neighbor',lambda:p.with_name('private-neighbor').read_text())
attempt('directory',lambda:os.listdir(p.parent))
attempt('write',lambda:p.write_text('forbidden'))
attempt('chmod',lambda:p.chmod(0o600))
print(json.dumps(r))
'''
    process = subprocess.run(
        ['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
         '-I', '-c', program, str(selected)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result.pop('read') == '[tool]\nmode = bounded\n'
    assert all(value == {'ok': False, 'errno': 1} for value in result.values()), result
    assert selected.read_bytes() == b'[tool]\nmode = bounded\n'
    assert sibling.read_text() == 'private'
