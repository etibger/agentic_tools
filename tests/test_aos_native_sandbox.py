import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys

import pytest

from agentic_tools.aos import native_sandbox as sandbox
from agentic_tools.aos.store import Rejected


@pytest.fixture
def scopes(tmp_path):
    source, outputs, temporary, state = [tmp_path / p for p in ('source', 'outputs', 'temporary', 'state')]
    for path in (source, outputs, temporary, state):
        path.mkdir()
    return source, outputs, temporary, state


def recipe(scopes):
    source, outputs, temporary, state = scopes
    return {'native_sandbox': {'version': sandbox.VERSION, 'localhost_tcp': False,
            'unix_socket_roots': [str(outputs)], 'allow_pty': False,
            'temporary_paths': [{'parent': str(temporary), 'name': 'test-{pid}-{counter}', 'kind': 'tree'}]}}


def filesystem(scopes):
    source, outputs, temporary, state = scopes
    return {':root': 'deny', ':minimal': 'read', str(source): 'read', str(outputs): 'write', str(state): 'deny'}


@pytest.mark.parametrize('template', ['*', '../secret-{pid}', '{pid}', 'a{unknown}', 'a{pid}/../secret',
                                    'a{pid}\n(allow default)', 'a{pid}.*'])
def test_rejects_unbounded_or_injected_temp_templates(scopes, template):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0]['name'] = template
    with pytest.raises(Rejected, match='Temporary|Unknown'):
        sandbox.config(candidate)


def test_declared_template_does_not_authorize_siblings_or_parent(scopes):
    candidate = recipe(scopes)
    parent = scopes[2]
    assert sandbox.writable(parent / 'test-123-0' / 'child', candidate)
    assert not sandbox.writable(parent, candidate)
    assert not sandbox.writable(parent / 'unrelated-123-0', candidate)
    assert not sandbox.writable(parent / 'test-0-0', candidate)
    assert not sandbox.writable(parent / 'test-123-0-sibling', candidate)


def test_file_template_never_authorizes_children(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0]['kind'] = 'file'
    assert sandbox.writable(scopes[2] / 'test-123-0', candidate)
    assert not sandbox.writable(scopes[2] / 'test-123-0' / 'child', candidate)


def test_relative_path_cache_is_ordered_exact_and_invocation_scoped(scopes, monkeypatch):
    original = sandbox._relative_parts_uncached
    calls = []

    def observed(path, parent, cache):
        calls.append((path, parent))
        return original(path, parent, cache)

    monkeypatch.setattr(sandbox, '_relative_parts_uncached', observed)
    root, other = scopes[2:]
    child = root / 'absent-child'
    cache = {}
    cases = [(root, root, ()), (child, root, ('absent-child',)),
             (root, child, None), (root, other, None)]
    alias = Path('/System/Volumes/Data') / root.relative_to('/')
    if alias.is_dir() and os.path.samefile(alias, root):
        cases.extend([(alias / 'absent-child', root, ('absent-child',)),
                      (root, alias, ()), (alias, root, ())])
    for path, parent, expected in cases:
        for _ in range(20):
            assert sandbox._relative_parts(path, parent, cache) == expected
    assert calls == [(path, parent) for path, parent, _expected in cases]
    assert sandbox._relative_parts(root, other, {}) is None
    assert calls[-1] == (root, other) and len(calls) == len(cases) + 1


def test_relative_path_cache_never_retains_failed_observations(scopes, monkeypatch):
    calls = []

    def unavailable(path, parent, cache):
        calls.append((path, parent))
        raise Rejected('native_scope', 'Cannot verify physical native path ancestry')

    monkeypatch.setattr(sandbox, '_relative_parts_uncached', unavailable)
    cache = {}
    for _ in range(2):
        with pytest.raises(Rejected, match='Cannot verify physical'):
            sandbox._relative_parts(scopes[0], scopes[1], cache)
    assert len(calls) == 2 and cache == {}


def test_temporary_hardlinks_reference_only_unique_declared_trees(scopes):
    candidate = recipe(scopes)
    reference = {'parent': str(scopes[2]), 'name': 'test-{pid}-{counter}'}
    candidate['native_sandbox']['temporary_hardlink_paths'] = [reference]
    text = sandbox.render(candidate, filesystem(scopes), preexisting={})
    assert '(allow file-link (regex #' in text
    for value in ('tree', [reference, reference], [reference] * 33,
                  [{'parent': str(scopes[2]), 'name': 'different-{pid}'}],
                  [{'parent': str(scopes[2]), 'name': 'test-{pid}-{counter}', 'kind': 'tree'}],
                  [{'parent': None, 'name': 'test-{pid}-{counter}'}]):
        candidate['native_sandbox']['temporary_hardlink_paths'] = value
        with pytest.raises(Rejected, match='temporary hardlinks'):
            sandbox.config(candidate)
    candidate['native_sandbox']['temporary_hardlink_paths'] = [reference]
    candidate['native_sandbox']['temporary_paths'][0]['kind'] = 'file'
    with pytest.raises(Rejected, match='tree scopes'):
        sandbox.config(candidate)


def test_owned_temporary_roots_reference_only_exact_literal_trees(scopes):
    candidate = recipe(scopes)
    root = scopes[2] / 'owned-base'
    candidate['native_sandbox']['temporary_paths'].append(
        {'parent': str(scopes[2]), 'name': 'owned-base', 'kind': 'tree'})
    candidate['native_sandbox']['owned_temporary_roots'] = [str(root)]
    sandbox.config(candidate)
    assert not root.exists()  # Admission validation must not create or adopt it.
    for roots in ([str(root), str(root)], [str(root)] * 33, str(root), [123],
                  [str(scopes[2] / 'test-123-0')], [str(scopes[2] / 'different')]):
        candidate['native_sandbox']['owned_temporary_roots'] = roots
        with pytest.raises(Rejected, match='Owned temporary roots'):
            sandbox.config(candidate)
    candidate['native_sandbox']['owned_temporary_roots'] = [str(root)]
    candidate['native_sandbox']['temporary_paths'][-1]['kind'] = 'file'
    with pytest.raises(Rejected, match='literal tree scopes'):
        sandbox.config(candidate)


def test_native_exact_read_files_are_pinned_and_disjoint(scopes):
    candidate = recipe(scopes)
    path = scopes[2] / 'tool-config'
    path.write_text('[user]\nname = Fixture\n')
    entry = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    candidate['native_sandbox']['file_read_paths'] = [entry]
    text = sandbox.render(candidate, filesystem(scopes), preexisting={})
    assert '(allow file-read* (literal ' + json.dumps(str(path)) + '))' in text
    assert '(allow file-read* file-map-executable (literal ' + json.dumps(str(path)) + '))' not in text
    assert filesystem(scopes).get(str(path)) is None
    identity = sandbox.verify_file_read_paths(candidate, filesystem(scopes))
    assert identity[0]['sha256'] == entry['sha256']
    path.write_text('changed')
    with pytest.raises(Rejected, match='binding changed'):
        sandbox.verify_file_read_paths(candidate, filesystem(scopes))
    path.write_text('[user]\nname = Fixture\n')
    for key in ('source', 'outputs'):
        with pytest.raises(Rejected, match='writable scopes'):
            sandbox.config({**candidate, key: str(path.parent)})
    with pytest.raises(Rejected, match='writable scopes'):
        sandbox.config({**candidate, 'native_write_roots': [{'path': str(path.parent), 'kind': 'cache'}]})
    for access in ('deny', 'write'):
        with pytest.raises(Rejected, match='denied namespace|writable scopes'):
            sandbox.render(candidate, {**filesystem(scopes), str(path.parent): access}, preexisting={})
    for value in ('file', [entry] * 65, [entry, entry], [{'path': str(path)}],
                  [{'path': str(path), 'sha256': 'invalid'}]):
        candidate['native_sandbox']['file_read_paths'] = value
        with pytest.raises(Rejected):
            sandbox.config(candidate)


def test_native_read_file_rejects_aliases_credentials_and_temporary_names(scopes, monkeypatch):
    candidate = recipe(scopes)
    home = scopes[2] / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    path = home / 'config'
    path.write_text('benign')
    alias = home / 'alias'
    alias.symlink_to(path)
    for selected in (alias,):
        candidate['native_sandbox']['file_read_paths'] = [
            {'path': str(selected), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        with pytest.raises(Rejected):
            sandbox.config(candidate)
    alias.unlink()
    os.link(path, alias)
    with pytest.raises(Rejected, match='inode aliases'):
        sandbox.config(candidate)
    alias.unlink()
    for name, content in (('.git-credentials', 'benign'), ('auth.json', 'benign'),
                          # Header only; assemble it to avoid a publication-scanner false positive.
                          ('key', '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----'),
                          ('config', '[credential]\nhelper = denied\n'),
                          ('config', '[http]\nextraHeader = denied\n')):
        selected = home / name
        selected.write_text(content)
        candidate['native_sandbox']['file_read_paths'] = [
            {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}]
        with pytest.raises(Rejected, match='credential|private key'):
            sandbox.render(candidate, filesystem(scopes), preexisting={})
    secret = home / '.ssh' / 'private'
    secret.parent.mkdir()
    secret.write_text('never granted')
    candidate['native_sandbox']['file_read_paths'] = [
        {'path': str(secret), 'sha256': hashlib.sha256(secret.read_bytes()).hexdigest()}]
    with pytest.raises(Rejected, match='credential'):
        sandbox.config(candidate)
    temporary = scopes[2] / 'test-123-0'
    temporary.write_text('temporary')
    candidate['native_sandbox']['file_read_paths'] = [
        {'path': str(temporary), 'sha256': hashlib.sha256(temporary.read_bytes()).hexdigest()}]
    with pytest.raises(Rejected, match='temporary scopes'):
        sandbox.render(candidate, filesystem(scopes), preexisting={})


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS exact native file-read qualification')
def test_real_native_file_read_is_literal_and_preserves_state_denials(scopes):
    source, outputs, temporary, state = scopes
    path, sibling = temporary / 'tool-config', temporary / 'sibling'
    path.write_text('bounded tool configuration')
    sibling.write_text('private sibling')
    candidate = recipe(scopes)
    candidate['native_sandbox']['file_read_paths'] = [
        {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    alias = Path('/System/Volumes/Data') / temporary.relative_to('/')
    if alias.is_dir() and os.path.samefile(alias, temporary):
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, {**mapping, str(alias): 'deny'}, preexisting={})
    saved = outputs / 'exact-read.sb'
    saved.write_text(sandbox.render(candidate, mapping, preexisting={}))
    program = '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);r={'read':(p/'tool-config').read_text()}
def attempt(name,fn):
 try:r[name]={'unexpected_success':fn()}
 except OSError as e:r[name]={'errno':e.errno}
attempt('sibling',lambda:(p/'sibling').read_text())
attempt('write',lambda:(p/'tool-config').write_text('forbidden'))
attempt('delete',lambda:(p/'tool-config').unlink())
attempt('link',lambda:os.link(p/'tool-config','output-alias'))
print(json.dumps(r))
'''
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved),
                              str(Path(sys._base_executable).resolve()), '-I', '-c', program, str(temporary)],
                             cwd=outputs, capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result.pop('read') == 'bounded tool configuration'
    assert all(value == {'errno': 1} for value in result.values()), result
    assert path.read_text() == 'bounded tool configuration'
    assert sibling.read_text() == 'private sibling'


def test_directory_create_parent_does_not_prune_preexisting_child_denial(scopes):
    candidate = recipe(scopes)
    parent = scopes[2]
    old = parent / 'test-123-0'
    old.mkdir()
    (old / 'sentinel').write_text('preserved')
    candidate['native_sandbox']['directory_create_paths'] = [str(parent)]
    candidate['native_sandbox']['temporary_hardlink_paths'] = [
        {'parent': str(parent), 'name': 'test-{pid}-{counter}'}]
    before = sandbox.temporary_snapshot(candidate)
    assert set(before) == {str(parent), str(old)}
    text = sandbox.render(candidate, filesystem(scopes), preexisting=before)
    for line in text.splitlines():
        if '(allow file-read* file-write* (require-all (regex #' in line or '(allow file-link ' in line:
            assert '(require-not (subpath ' + json.dumps(str(old)) + '))' in line


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS both-endpoint temporary hardlink qualification')
@pytest.mark.parametrize('create_parent', [False, True])
@pytest.mark.parametrize('deny_ancestor', [False, True])
def test_real_temporary_hardlinks_preserve_existing_and_denied_ancestors(scopes, create_parent, deny_ancestor):
    source, outputs, temporary, state = scopes
    original = source / 'immutable'
    original.write_text('immutable')
    old = temporary / 'test-123-0'
    old.mkdir()
    sentinel = old / 'sentinel'
    sentinel.write_text('preserved')
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_hardlink_paths'] = [
        {'parent': str(temporary), 'name': 'test-{pid}-{counter}'}]
    if create_parent:
        candidate['native_sandbox']['directory_create_paths'] = [str(temporary)]
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    if deny_ancestor:
        mapping[str(temporary)] = 'deny'
    if create_parent and deny_ancestor:
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, mapping)
        return
    before = sandbox.temporary_snapshot(candidate)
    profile = outputs / 'temporary-links.sb'
    profile.write_text(sandbox.render(candidate, mapping, preexisting=before))
    program = '''import json,os,shutil,sys
from pathlib import Path
p,original=map(Path,sys.argv[1:]);old=p/'test-123-0';new=p/('test-'+str(os.getpid())+'-0');r={}
def attempt(name,fn):
 try:r[name]={'ok':True,'value':fn()}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
attempt('create',new.mkdir)
if r['create']['ok']:
 own=new/'own';own.write_text('owned')
 attempt('owned_link',lambda:os.link(own,new/'alias'))
 attempt('outside_source',lambda:os.link(original,new/'outside'))
 attempt('outside_destination',lambda:os.link(own,original.parent/'outside'))
 attempt('old_source',lambda:os.link(old/'sentinel',new/'old'))
 attempt('old_destination',lambda:os.link(own,old/'new'))
 attempt('old_volume_alias',lambda:os.link('/System/Volumes/Data'+str(old/'sentinel'),new/'old-alias'))
 for child in new.iterdir():child.unlink()
 new.rmdir()
attempt('old_read',lambda:(old/'sentinel').read_text())
attempt('old_write',lambda:(old/'sentinel').write_text('forbidden'))
attempt('old_remove',lambda:shutil.rmtree(old))
print(json.dumps(r))
'''
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile),
                              str(Path(sys._base_executable).resolve()), '-I', '-c', program,
                              str(temporary), str(original)], cwd=source, capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    if deny_ancestor:
        assert result.pop('create') == {'ok': False, 'errno': 1}
    else:
        assert result.pop('create')['ok']
        assert result.pop('owned_link')['ok']
    assert all(value == {'ok': False, 'errno': 1} for value in result.values()), result
    assert sentinel.read_text() == 'preserved'
    assert original.read_text() == 'immutable'
    after = sandbox.temporary_snapshot(candidate)
    assert after[str(old)] == before[str(old)]
    if create_parent:
        # Creating/removing an authorized child changes parent mtime. Its
        # identity and permissions, and every protected child, must remain.
        for field in ('device', 'inode', 'uid', 'mode', 'link'):
            assert after[str(temporary)][field] == before[str(temporary)][field]
    else:
        assert after == before


def test_protect_compares_templates_not_entire_temp_parent(scopes):
    candidate = recipe(scopes)
    sandbox.protect(candidate, [scopes[2] / 'other-evidence'])
    for protected in (scopes[2], scopes[2] / 'test-123-0', scopes[2] / 'test-123-0' / 'receipt'):
        with pytest.raises(Rejected):
            sandbox.protect(candidate, [protected])


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS firmlink aliases')
def test_temp_protection_compares_physical_volume_aliases(scopes):
    candidate = recipe(scopes)
    parent = scopes[2]
    alias = Path('/System/Volumes/Data') / parent.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, parent):
        pytest.skip('Fixture path has no writable-volume alias')
    for protected in (alias, alias / 'test-123-0', alias / 'test-123-0' / 'receipt'):
        with pytest.raises(Rejected):
            sandbox.protect(candidate, [protected])
    assert sandbox.writable(alias / 'test-123-0' / 'receipt', candidate)
    sandbox.protect(candidate, [alias / 'other-evidence'])


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS firmlink aliases')
def test_socket_root_protection_compares_physical_volume_aliases(scopes):
    root = scopes[1]
    alias = Path('/System/Volumes/Data') / root.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, root):
        pytest.skip('Fixture path has no writable-volume alias')
    with pytest.raises(Rejected, match='Unix socket scope'):
        sandbox.render(recipe(scopes), filesystem(scopes), preexisting={}, protected=[alias / 'receipt'])


def test_rejects_arbitrary_contract_or_path_alias(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['raw_profile'] = '(allow default)'
    with pytest.raises(Rejected):
        sandbox.config(candidate)
    candidate = recipe(scopes)
    alias = scopes[2] / 'alias'
    alias.symlink_to(scopes[1], target_is_directory=True)
    candidate['native_sandbox']['unix_socket_roots'] = [str(alias)]
    with pytest.raises(Rejected):
        sandbox.config(candidate)


def test_render_default_deny_and_all_tcp_closed(scopes):
    text = sandbox.render(recipe(scopes), filesystem(scopes), preexisting={})
    assert '(deny default)' in text
    assert '(allow default)' not in text
    assert 'localhost' not in text
    assert '(allow network-outbound)' not in text
    assert '(allow signal (target same-sandbox))' in text


def test_preexisting_matches_are_excluded_from_file_and_socket_grants(scopes):
    old = scopes[2] / 'test-123-0'
    old.mkdir()
    before = sandbox.temporary_snapshot(recipe(scopes))
    assert set(before) == {str(old)}
    text = sandbox.render(recipe(scopes), filesystem(scopes), preexisting=before)
    assert '(require-not (subpath ' + '"' + str(old) + '"))' in text
    assert '(deny network-outbound' in text
    assert old.is_dir()


def test_more_specific_immutable_read_excluded_from_writable_ancestor(scopes):
    control = scopes[1] / 'control.py'
    control.write_text('trusted')
    mapping = {**filesystem(scopes), str(control): 'read'}
    text = sandbox.render(recipe(scopes), mapping, preexisting={})
    write = next(line for line in text.splitlines() if line.startswith('(allow file-write*') and str(scopes[1]) in line)
    assert '(require-not (literal ' + '"' + str(control) + '"))' in write


def test_uds_scope_cannot_widen_readonly_or_protected_filesystem(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['unix_socket_roots'] = [str(scopes[0])]
    with pytest.raises(Rejected):
        sandbox.render(candidate, filesystem(scopes), preexisting={})
    with pytest.raises(Rejected):
        sandbox.render(recipe(scopes), filesystem(scopes), preexisting={}, protected=[scopes[1] / 'receipt'])


def test_absent_cache_file_uses_literal_never_subtree(scopes):
    path = scopes[2] / 'cache.lock'
    candidate = recipe(scopes)
    candidate['native_write_roots'] = [{'path': str(path), 'kind': 'cache-file'}]
    text = sandbox.render(candidate, {**filesystem(scopes), str(path): 'write'}, preexisting={})
    assert '(allow file-write* (literal ' + json.dumps(str(path)) + '))' in text
    assert '(allow file-write* (subpath ' + json.dumps(str(path)) + '))' not in text
    assert '(allow file-link' not in text


def test_hardlinks_require_explicit_writable_native_directories(scopes):
    candidate = recipe(scopes)
    assert '(allow file-link' not in sandbox.render(candidate, filesystem(scopes), preexisting={})
    generated = scopes[0] / 'generated'
    generated.mkdir()
    guard = generated / 'control'
    guard.write_text('protected')
    candidate['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    with pytest.raises(Rejected, match='hardlink roots'):
        sandbox.render(candidate, filesystem(scopes), preexisting={})
    text = sandbox.render(candidate, {**filesystem(scopes), str(generated): 'write', str(guard): 'read'}, preexisting={})
    links = [line for line in text.splitlines() if line.startswith('(allow file-link')]
    assert len(links) == 1
    assert '(subpath ' + json.dumps(str(generated)) + ')' in links[0]
    assert '(require-not (literal ' + json.dumps(str(guard)) + '))' in links[0]
    assert str(scopes[1]) not in links[0]


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS hardlink endpoint qualification')
def test_real_hardlinks_require_both_endpoints_in_native_roots(scopes):
    source, outputs, temporary, state = scopes
    generated, cache = source / 'generated', temporary / 'cache'
    for directory in (generated, cache):
        directory.mkdir()
        (directory / 'input').write_text('generated')
    source_file, control = source / 'input', state / 'control'
    source_file.write_text('immutable')
    control.write_text('private')
    guarded = generated / 'guarded'
    guarded.mkdir()
    (guarded / 'input').write_text('protected')
    alias = generated / 'readonly-alias'
    alias.symlink_to(source_file)
    exact_cache_file = temporary / 'cache.lock'
    exact_cache_file.write_text('bookkeeping')
    directory_only = temporary / 'directory-only'
    candidate = recipe(scopes)
    candidate['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'},
                                       {'path': str(cache), 'kind': 'cache'},
                                       {'path': str(exact_cache_file), 'kind': 'cache-file'}]
    candidate['native_sandbox']['directory_create_paths'] = [str(directory_only)]
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read',
               str(generated): 'write', str(cache): 'write', str(guarded): 'read', str(exact_cache_file): 'write'}
    text = sandbox.render(candidate, mapping, preexisting={})
    saved = outputs / 'hardlink-profile.sb'
    saved.write_text(text)
    paths = dict(source=str(source), generated=str(generated), cache=str(cache), outputs=str(outputs),
                 temporary=str(temporary), state=str(state), directory_only=str(directory_only),
                 exact_cache_file=str(exact_cache_file))
    program = '''import json,os,sys
from pathlib import Path
p={k:Path(v) for k,v in json.loads(sys.argv[1]).items()};r={}
def link(name,src,dst):
 try:
  os.link(src,dst)
  r[name]={'ok':True,'same_inode':os.stat(src).st_ino==os.stat(dst).st_ino}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
t=p['temporary']/('test-'+str(os.getpid())+'-0');t.mkdir()
p['directory_only'].mkdir(mode=0o700)
(p['outputs']/'ordinary').write_text('output')
link('within_generated',p['generated']/'input',p['generated']/'local-link')
link('generated_to_cache',p['generated']/'input',p['cache']/'cross-link')
link('cache_to_generated',p['cache']/'input',p['generated']/'reverse-link')
link('source_to_generated',p['source']/'input',p['generated']/'source-link')
link('control_to_generated',p['state']/'control',p['generated']/'control-link')
link('guarded_to_generated',p['generated']/'guarded'/'input',p['generated']/'guarded-link')
link('generated_to_source',p['generated']/'input',p['source']/'forbidden-link')
link('generated_to_control',p['generated']/'input',p['state']/'forbidden-link')
link('generated_to_guarded',p['generated']/'input',p['generated']/'guarded'/'forbidden-link')
link('generated_to_outputs',p['generated']/'input',p['outputs']/'forbidden-link')
link('outputs_to_generated',p['outputs']/'ordinary',p['generated']/'output-link')
link('generated_to_temp',p['generated']/'input',t/'forbidden-link')
link('generated_to_directory_child',p['generated']/'input',p['directory_only']/'forbidden-link')
link('cache_file_to_generated',p['exact_cache_file'],p['generated']/'bookkeeping-link')
link('source_alias_to_generated',Path('/System/Volumes/Data'+str(p['source']))/'input',p['generated']/'alias-source-link')
link('generated_to_source_alias',p['generated']/'input',Path('/System/Volumes/Data'+str(p['source']))/'alias-forbidden-link')
link('generated_alias_to_cache_alias',Path('/System/Volumes/Data'+str(p['generated']))/'input',Path('/System/Volumes/Data'+str(p['cache']))/'alias-positive')
link('source_symlink_to_generated',p['generated']/'readonly-alias',p['generated']/'symlink-source-link')
print(json.dumps(r))
'''
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
                              '-I', '-c', program, json.dumps(paths)], cwd=source,
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    allowed = {'within_generated', 'generated_to_cache', 'cache_to_generated', 'generated_alias_to_cache_alias'}
    assert all(observed[name] == {'ok': True, 'same_inode': True} for name in allowed), observed
    assert all(row == {'ok': False, 'errno': 1} for name, row in observed.items() if name not in allowed), observed
    assert source_file.read_text() == 'immutable'
    assert control.read_text() == 'private'
    assert (guarded / 'input').read_text() == 'protected'


@pytest.mark.parametrize('ports', [[True], [0], [65536], [123, 123], ['123'], '*'])
def test_denied_ports_are_exact_bounded_integers(scopes, ports):
    candidate = recipe(scopes)
    candidate['native_sandbox']['denied_tcp_ports'] = ports
    with pytest.raises(Rejected):
        sandbox.config(candidate)


def test_unqualified_tcp_never_reaches_renderer(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['localhost_tcp'] = True
    with pytest.raises(Rejected, match='not qualified'):
        sandbox.render(candidate, filesystem(scopes))


@pytest.mark.parametrize('name,kind,expected', [('.tmp{alnum6}', 'tree', '.tmpA1b2c3'),
                                              ('{token8}', 'file', 'ab_012cd'),
                                              ('herdr-missing-product-announcement.json', 'file',
                                               'herdr-missing-product-announcement.json')])
def test_supported_native_temporary_names(scopes, name, kind, expected):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0].update(name=name, kind=kind)
    assert sandbox.writable(scopes[2] / expected, candidate)
    assert not sandbox.writable(scopes[2] / (expected + '-other'), candidate)


def test_template_protection_rejects_case_alias(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0].update(name='.CODEX', kind='file')
    with pytest.raises(Rejected):
        sandbox.protect(candidate, [scopes[2] / '.codex'])


def test_temp_parent_requires_actual_directory_spelling(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0]['parent'] = str(scopes[2].with_name('TEMPORARY'))
    with pytest.raises(Rejected):
        sandbox.config(candidate)


def test_iokit_capability_is_explicit_and_exact(scopes):
    candidate = recipe(scopes)
    assert 'iokit-open' not in sandbox.render(candidate, filesystem(scopes))
    candidate['native_sandbox']['iokit_user_clients'] = ['AppleSMCClient']
    assert '(allow iokit-open (iokit-user-client-class "AppleSMCClient"))' in sandbox.render(candidate, filesystem(scopes))
    for invalid in ('AppleSMCClient', ['*'], ['IOUserClient'], ['AppleSMCClient', 'AppleSMCClient']):
        candidate['native_sandbox']['iokit_user_clients'] = invalid
        with pytest.raises(Rejected):
            sandbox.config(candidate)


@pytest.mark.parametrize('template,example', [('tmp.{alnum10}', 'tmp.aB0cD1eF2g'),
                                             ('herdr-r-{pid}-herdr-pr-{hex16}.sock', 'herdr-r-123-herdr-pr-0123456789abcdef.sock'),
                                             ('herdr-disconnected-open-w{crockford32}', 'herdr-disconnected-open-w10ABCXZ'),
                                             ('herdr-disconnected-open-w{crockford32}.checkout', 'herdr-disconnected-open-wZ.checkout'),
                                             ('herdr-client-drop-space test-{pid}-{counter}.png', 'herdr-client-drop-space test-123-0.png')])
def test_additional_bounded_native_forms(scopes, template, example):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0]['name'] = template
    sandbox.config(candidate)
    assert re.fullmatch(sandbox._name_regex(template), example)
    assert not re.fullmatch(sandbox._name_regex(template), example + 'extra')


@pytest.mark.parametrize('value', ['', 'I', 'L', 'O', 'U', 'a', 'A' * 14, '*', 'A/B', 'A\nB'])
def test_public_base32_token_has_exact_alphabet_and_length(value):
    assert not re.fullmatch(sandbox._name_regex('test-{crockford32}'), 'test-' + value)


def test_fixed_trees_and_sockets_are_exact_and_prior_matches_stay_denied(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'] = [
        {'parent': str(scopes[2]), 'name': 'a' * 80, 'kind': 'tree'},
        {'parent': str(scopes[2]), 'name': 'fixed.sock', 'kind': 'socket'}]
    old = scopes[2] / ('a' * 80)
    old.mkdir()
    before = sandbox.temporary_snapshot(candidate)
    assert sandbox.writable(old / 'child', candidate)
    assert not sandbox.writable(scopes[2] / ('a' * 81), candidate)
    assert not sandbox.writable(scopes[2] / 'fixed.sock.child', candidate)
    rendered = sandbox.render(candidate, filesystem(scopes), preexisting=before)
    assert '(require-not (subpath ' + json.dumps(str(old)) + '))' in rendered


def test_temp_entry_count_is_bounded_without_wildcard_compression(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'] = [
        {'parent': str(scopes[2]), 'name': 'test-' + str(i) + '-{pid}', 'kind': 'tree'} for i in range(1024)]
    assert len(sandbox.config(candidate)['temporary_paths']) == 1024
    candidate['native_sandbox']['temporary_paths'].append(
        {'parent': str(scopes[2]), 'name': 'last-{pid}', 'kind': 'tree'})
    with pytest.raises(Rejected):
        sandbox.config(candidate)


def test_unprefixed_random_name_is_not_a_tree_or_socket(scopes):
    candidate = recipe(scopes)
    for kind in ('tree', 'socket'):
        candidate['native_sandbox']['temporary_paths'][0].update(name='{token8}', kind=kind)
        with pytest.raises(Rejected):
            sandbox.config(candidate)


def test_pruning_retains_every_permission_transition(scopes):
    source, outputs, temporary, state = scopes
    hidden = source / '.codex'
    hidden.mkdir()
    instruction = hidden / 'AGENTS.md'
    instruction.write_text('immutable')
    build = source / 'target'
    build.mkdir()
    immutable = build / 'guard'
    immutable.write_text('immutable')
    redundant = source / 'ordinary.rs'
    redundant.write_text('source')
    grants = {str(source): 'read', str(redundant): 'read', str(hidden): 'deny', str(instruction): 'read',
              str(build): 'write', str(immutable): 'read', str(state): 'deny'}
    pruned = sandbox._prune_grants(grants, set())
    assert str(redundant) not in pruned
    assert pruned == {p: mode for p, mode in grants.items() if p != str(redundant)}
    text = sandbox.render(recipe(scopes), {**filesystem(scopes), **grants}, preexisting={})
    assert str(redundant) not in text
    assert '(require-not (subpath ' + json.dumps(str(hidden)) + '))' in text
    assert '(allow file-read* file-map-executable (literal ' + json.dumps(str(instruction)) + '))' in text
    assert '(require-not (literal ' + json.dumps(str(immutable)) + '))' in text


def test_directory_create_paths_are_literal_bounded_and_protected(scopes):
    candidate = recipe(scopes)
    path = scopes[2] / 'persistent-501'
    candidate['native_sandbox']['directory_create_paths'] = [str(path)]
    assert sandbox.writable(path, candidate)
    assert not sandbox.writable(path / 'child', candidate)
    with pytest.raises(Rejected):
        sandbox.protect(candidate, [path])
    with pytest.raises(Rejected, match='broader native write'):
        sandbox.render(candidate, {**filesystem(scopes), str(path): 'write'}, preexisting={})
    for paths in ([str(path), str(path)], [str(path)] * 33, [123], 'path'):
        candidate['native_sandbox']['directory_create_paths'] = paths
        with pytest.raises(Rejected):
            sandbox.config(candidate)


def test_directory_read_paths_are_exact_existing_bounded_and_protected(scopes):
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'] = []
    directory = scopes[2] / 'installed-tools'
    directory.mkdir()
    candidate['native_sandbox']['directory_read_paths'] = [str(directory)]
    assert not sandbox.writable(directory, candidate)
    assert sandbox.temporary_snapshot(candidate) == {}
    text = sandbox.render(candidate, filesystem(scopes), preexisting={})
    assert '(allow file-read-data (require-all (literal ' + json.dumps(str(directory)) + ') (vnode-type DIRECTORY)))' in text
    for protected in (directory, directory.parent, directory / 'child'):
        sandbox.protect(candidate, [protected])
        sandbox.render(candidate, filesystem(scopes), preexisting={}, protected=[protected])
    for denied in (directory, directory.parent):
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, {**filesystem(scopes), str(denied): 'deny'}, preexisting={})
    ordinary = directory / 'file'
    ordinary.write_text('file')
    for paths in ([str(directory), str(directory)], [str(directory)] * 33, [123], 'directory',
                  [str(directory / 'missing')], [str(ordinary)]):
        candidate['native_sandbox']['directory_read_paths'] = paths
        with pytest.raises(Rejected):
            sandbox.config(candidate)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS firmlink aliases')
def test_directory_read_protection_and_deny_precedence_use_physical_aliases(scopes):
    candidate = recipe(scopes)
    directory = scopes[2] / 'installed-tools'
    directory.mkdir()
    alias = Path('/System/Volumes/Data') / directory.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, directory):
        pytest.skip('Fixture path has no writable-volume alias')
    candidate['native_sandbox']['directory_read_paths'] = [str(directory)]
    sandbox.protect(candidate, [alias / 'protected'])
    for denied in (alias, alias.parent):
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, {**filesystem(scopes), str(denied): 'deny'}, preexisting={})


def test_directory_write_access_is_bound_to_existing_template_parent(scopes):
    candidate = recipe(scopes)
    parent = scopes[2]
    candidate['native_sandbox']['directory_write_access_paths'] = [str(parent)]
    text = sandbox.render(candidate, filesystem(scopes), preexisting={})
    assert '(allow file-write-data (require-all (literal ' + json.dumps(str(parent)) + ') (vnode-type DIRECTORY)))' in text
    assert not sandbox.writable(parent, candidate)
    assert sandbox.temporary_snapshot(candidate) == {}
    sandbox.protect(candidate, [parent / 'unrelated-protected-file'])
    for protected in (parent, parent.parent, parent / 'test-123-0'):
        with pytest.raises(Rejected):
            sandbox.protect(candidate, [protected])
    for denied in (parent, parent.parent):
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, {**filesystem(scopes), str(denied): 'deny'}, preexisting={})
    for paths in ([str(parent), str(parent)], [str(parent)] * 33, [123], str(parent),
                  [str(scopes[0])], [str(parent / 'missing')]):
        candidate['native_sandbox']['directory_write_access_paths'] = paths
        with pytest.raises(Rejected):
            sandbox.config(candidate)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS firmlink aliases')
def test_directory_write_access_deny_and_template_protection_use_physical_aliases(scopes):
    candidate = recipe(scopes)
    parent = scopes[2]
    alias = Path('/System/Volumes/Data') / parent.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, parent):
        pytest.skip('Fixture path has no writable-volume alias')
    candidate['native_sandbox']['directory_write_access_paths'] = [str(parent)]
    for protected in (alias, alias.parent, alias / 'test-123-0' / 'receipt'):
        with pytest.raises(Rejected):
            sandbox.protect(candidate, [protected])
    for denied in (alias, alias.parent):
        with pytest.raises(Rejected, match='denied namespace'):
            sandbox.render(candidate, {**filesystem(scopes), str(denied): 'deny'}, preexisting={})


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS directory write-access qualification')
def test_real_directory_write_access_preserves_parent_and_other_files(scopes):
    source, outputs, temporary, state = scopes
    neighbor, old = temporary / 'neighbor', temporary / 'test-123-0'
    neighbor.write_text('neighbor')
    old.write_text('preexisting')
    mode = temporary.stat().st_mode
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'][0]['kind'] = 'file'
    candidate['native_sandbox']['directory_write_access_paths'] = [str(temporary)]
    before = sandbox.temporary_snapshot(candidate)
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    text = sandbox.render(candidate, mapping, preexisting=before)
    saved = outputs / 'directory-access-profile.sb'
    saved.write_text(text)
    program = '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);r={}
def attempt(name,fn):
 try:r[name]={'ok':True,'value':fn()}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
r['writable']=os.access(p,os.W_OK)
new=p/('test-'+str(os.getpid())+'-0');new.write_text('allowed');new.unlink()
for filename in ('neighbor','test-123-0'):
 q=p/filename
 attempt(filename+'_read',lambda:q.read_text())
 attempt(filename+'_write',lambda:q.write_text('forbidden'))
 attempt(filename+'_chmod',lambda:q.chmod(0o600))
 attempt(filename+'_unlink',q.unlink)
attempt('new_neighbor',lambda:(p/'unrelated').write_text('forbidden'))
attempt('bad_suffix',lambda:(p/'test-123-0-suffix').write_text('forbidden'))
attempt('new_directory',lambda:(p/'other-directory').mkdir())
attempt('parent_list',lambda:os.listdir(p))
attempt('parent_chmod',lambda:p.chmod(0o777))
attempt('parent_remove',p.rmdir)
attempt('parent_rename',lambda:p.rename(p.with_name('moved')))
print(json.dumps(r))
'''
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
                              '-I', '-c', program, str(temporary)], cwd=source,
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    assert observed.pop('writable') is True
    assert all(row == {'ok': False, 'errno': 1} for row in observed.values()), observed
    assert neighbor.read_text() == 'neighbor'
    assert old.read_text() == 'preexisting'
    assert temporary.stat().st_mode == mode
    assert sandbox.temporary_snapshot(candidate) == before


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Bash normal-directory heredoc qualification')
def test_real_bash_heredoc_uses_normal_temporary_directory(scopes):
    source, outputs, temporary, state = scopes
    parent = Path('/private/var/tmp')
    candidate = recipe(scopes)
    candidate['native_sandbox']['temporary_paths'] = [
        {'parent': str(parent), 'name': 'sh-thd-{counter}', 'kind': 'file'}]
    candidate['native_sandbox']['directory_write_access_paths'] = [str(parent)]
    before = sandbox.temporary_snapshot(candidate)
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    text = sandbox.render(candidate, mapping, preexisting=before)
    saved = outputs / 'bash-heredoc-profile.sb'
    saved.write_text(text)
    program = ('import fcntl,json,os,stat,sys;print(json.dumps({"path":fcntl.fcntl(0,50,b"\\0"*1024).split(b"\\0")[0].decode(),'
               '"mode":oct(os.fstat(0).st_mode),"text":sys.stdin.read(),"TMPDIR":os.environ.get("TMPDIR")}))')
    command = (shlex.quote(str(Path(sys._base_executable).resolve())) + ' -I -c ' + shlex.quote(program)
               + " <<'EOF'\nbounded native heredoc\nEOF\n")
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved), '/bin/bash', '-c', command], cwd=source,
                             capture_output=True, text=True, timeout=10,
                             env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(Path.home()),
                                  'TMPDIR': os.environ['TMPDIR']})
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    assert re.fullmatch(r'/private/var/tmp/sh-thd-[0-9]+', observed['path'])
    assert observed['mode'] == '0o100600'
    assert observed['text'] == 'bounded native heredoc\n'
    assert observed['TMPDIR'] == os.environ['TMPDIR']
    assert not Path(observed['path']).exists()
    assert sandbox.temporary_snapshot(candidate) == before


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS literal directory-read qualification')
def test_real_directory_read_does_not_grant_children_siblings_or_writes(scopes):
    source, outputs, temporary, state = scopes
    directory, sibling = temporary / 'installed-tools', temporary / 'sibling'
    directory.mkdir()
    sibling.mkdir()
    (directory / 'child-directory').mkdir()
    (directory / 'child-file').write_text('private')
    (temporary / 'sibling-file').write_text('private')
    candidate = recipe(scopes)
    candidate['native_sandbox']['directory_read_paths'] = [str(directory)]
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    text = sandbox.render(candidate, mapping, preexisting={})
    saved = outputs / 'directory-read-profile.sb'
    saved.write_text(text)
    program = '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);r={}
def attempt(name,fn):
 try:r[name]={'ok':True,'value':fn()}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
attempt('root_list',lambda:sorted(os.listdir(p/'installed-tools')))
attempt('child_list',lambda:os.listdir(p/'installed-tools'/'child-directory'))
attempt('child_read',lambda:(p/'installed-tools'/'child-file').read_text())
attempt('sibling_list',lambda:os.listdir(p/'sibling'))
attempt('sibling_read',lambda:(p/'sibling-file').read_text())
attempt('child_write',lambda:(p/'installed-tools'/'new-file').write_text('forbidden'))
attempt('child_create',lambda:(p/'installed-tools'/'new-directory').mkdir())
attempt('alias_child_read',lambda:(Path('/System/Volumes/Data'+str(p))/'installed-tools'/'child-file').read_text())
print(json.dumps(r))
'''
    process = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
                              '-I', '-c', program, str(temporary)], cwd=outputs,
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    observed = json.loads(process.stdout)
    assert observed.pop('root_list') == {'ok': True, 'value': ['child-directory', 'child-file']}
    assert all(row == {'ok': False, 'errno': 1} for row in observed.values()), observed


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS directory-create qualification')
def test_real_directory_creation_preserves_existing_and_denies_children(scopes):
    source, outputs, temporary, state = scopes
    old, new, regular = [temporary / name for name in ('persistent-old', 'persistent-new', 'not-a-file')]
    old.mkdir(mode=0o700)
    sentinel = old / 'sentinel'
    sentinel.write_text('preserved')
    candidate = recipe(scopes)
    candidate['native_sandbox']['directory_create_paths'] = list(map(str, (old, new, regular)))
    before = sandbox.temporary_snapshot(candidate)
    assert str(old) in before
    mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
    rendered = sandbox.render(candidate, mapping, preexisting=before)
    saved = outputs / 'directory-profile.sb'
    saved.write_text(rendered)
    program = '''import errno,json,os,socket,stat,sys
from pathlib import Path
p=Path(sys.argv[1]);r={}
def attempt(name,fn):
 try:r[name]={'ok':True,'value':fn()}
 except OSError as e:r[name]={'ok':False,'errno':e.errno}
attempt('create',lambda:os.mkdir(p/'persistent-new',0o700))
attempt('repeat_new',lambda:os.mkdir(p/'persistent-new',0o700))
attempt('repeat_old',lambda:os.mkdir(p/'persistent-old',0o700))
attempt('metadata',lambda:stat.S_IMODE((p/'persistent-new').stat().st_mode))
attempt('chmod',lambda:os.chmod(p/'persistent-new',0o755))
attempt('child_write',lambda:(p/'persistent-new'/'file').write_text('forbidden'))
attempt('old_child_write',lambda:(p/'persistent-old'/'sentinel').write_text('forbidden'))
attempt('old_child_read',lambda:(p/'persistent-old'/'sentinel').read_text())
attempt('regular_file',lambda:(p/'not-a-file').write_text('forbidden'))
def bind():
 os.chdir(p/'persistent-new')
 with socket.socket(socket.AF_UNIX) as s:s.bind('child.sock')
attempt('child_socket',bind)
attempt('remove',lambda:os.rmdir(p/'persistent-new'))
print(json.dumps(r))
'''
    result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
                             '-I', '-c', program, str(temporary)], cwd=outputs,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['create']['ok']
    assert observed['metadata']['value'] == 0o700
    for name in ('repeat_new', 'repeat_old'):
        assert observed[name]['errno'] == 17
    for name in ('chmod', 'child_write', 'old_child_write', 'old_child_read', 'regular_file', 'child_socket', 'remove'):
        assert observed[name]['errno'] == 1
    assert sentinel.read_text() == 'preserved'
    assert old.stat().st_mode & 0o777 == 0o700
    assert new.stat().st_mode & 0o777 == 0o700
    assert set(sandbox.temporary_snapshot(candidate)) == {str(old), str(new)}


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Seatbelt qualification')
def test_real_native_profile_enforces_files_ipc_pty_and_alias_denial(scopes):
    source, outputs, temporary, state = scopes
    source_file, secret = source / 'input.txt', state / 'secret'
    source_file.write_text('immutable')
    secret.write_text('private')
    old = temporary / 'test-123-0'
    old.mkdir()
    (old / 'sentinel').write_text('untouched')
    python = str(Path(sys._base_executable).resolve())
    protected_socket = outputs / 'protected.sock'
    helper_code = ('import os,socket,sys; s=socket.socket(socket.AF_UNIX);'
                   's.bind("protected.sock");s.listen(1);print(os.getpid(),flush=True);sys.stdin.readline()')
    helper = subprocess.Popen([python, '-I', '-c', helper_code], cwd=outputs,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    listener = socket.socket()
    try:
        helper_pid = int(helper.stdout.readline())
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        protected_port = listener.getsockname()[1]
        candidate = recipe(scopes)
        candidate['native_sandbox'].update(allow_pty=True, denied_tcp_ports=[protected_port],
                                           denied_unix_sockets=[str(protected_socket)])
        before = sandbox.temporary_snapshot(candidate)
        mapping = {**filesystem(scopes), str(Path(sys.base_prefix).resolve()): 'read'}
        text = sandbox.render(candidate, mapping, preexisting=before)
        payload = {'source': str(source_file), 'secret': str(secret), 'old': str(old),
                   'temporary': str(temporary), 'helper': helper_pid, 'port': protected_port}
        program = '''import json,os,pty,socket,subprocess,sys
from pathlib import Path
p=json.loads(sys.argv[1]); r={}
def run(name, action):
 try: r[name]={'ok':True,'value':action()}
 except OSError as error:r[name]={'ok':False,'errno':error.errno}
def tcp():
 with socket.socket() as s:
  s.bind(('127.0.0.1',0));s.listen(1)
  with socket.create_connection(s.getsockname(),timeout=.5) as c:
   with s.accept()[0] as peer:c.sendall(b'ok');return peer.recv(2).decode()
def connect(host, port):
 with socket.create_connection((host,port),timeout=.3):return True
def uds(name):
 with socket.socket(socket.AF_UNIX) as s:
  s.bind(name);s.listen(1)
  with socket.socket(socket.AF_UNIX) as c:
   c.connect(name)
   with s.accept()[0] as peer:c.sendall(b'ok');answer=peer.recv(2).decode()
 os.unlink(name);return answer
def blocked_uds():
 with socket.socket(socket.AF_UNIX) as s:s.connect('protected.sock')
def tty():
 a,b=pty.openpty();os.close(a);os.close(b);return True
def temporary():
 q=Path(p['temporary'])/('test-'+str(os.getpid())+'-0');q.mkdir();(q/'file').write_text('native');return str(q)
run('source_read',lambda:Path(p['source']).read_text())
run('source_write',lambda:Path(p['source']).write_text('forbidden'))
run('state_read',lambda:Path(p['secret']).read_text())
run('state_volume_alias_read',lambda:Path('/System/Volumes/Data'+p['secret']).read_text())
run('source_volume_alias_write',lambda:Path('/System/Volumes/Data'+p['source']).write_text('forbidden'))
run('output_write',lambda:Path('result').write_text('native'))
run('source_hardlink',lambda:os.link(p['source'],'alias'))
run('old_temp_write',lambda:(Path(p['old'])/'sentinel').write_text('forbidden'))
run('new_temp_write',temporary)
run('unscoped_temp_write',lambda:(Path(p['temporary'])/'unrelated').write_text('forbidden'))
run('tcp_local',tcp)
run('tcp_external',lambda:connect('192.0.2.1',9))
run('tcp_protected',lambda:connect('127.0.0.1',p['port']))
run('uds_allowed',lambda:uds('allowed.sock'))
run('uds_protected',blocked_uds)
run('pty',tty)
run('signal_outer',lambda:os.kill(p['helper'],0))
child=subprocess.Popen(['/bin/sleep','10'])
try:run('signal_child',lambda:os.kill(child.pid,0))
finally:child.terminate();child.wait(timeout=3)
nested=subprocess.run(['/usr/bin/sandbox-exec','-p','(version 1)(allow default)','/usr/bin/true'],capture_output=True,text=True)
r['nested_sandbox_denied']=nested.returncode!=0
print(json.dumps(r))
'''
        run = subprocess.run(['/usr/bin/sandbox-exec', '-p', text, python, '-I', '-c', program, json.dumps(payload)],
                             cwd=outputs, capture_output=True, text=True, timeout=10,
                             env={'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
        assert run.returncode == 0, run.stderr
        result = json.loads(run.stdout)
        for name in ('source_read', 'output_write', 'new_temp_write', 'uds_allowed', 'pty', 'signal_child'):
            assert result[name]['ok'], (name, result)
        for name in ('source_write', 'state_read', 'state_volume_alias_read', 'source_volume_alias_write',
                     'source_hardlink', 'old_temp_write', 'unscoped_temp_write',
                     'tcp_local', 'tcp_external', 'tcp_protected', 'uds_protected', 'signal_outer'):
            assert result[name] == {'ok': False, 'errno': 1}, (name, result)
        assert result['nested_sandbox_denied']
        assert source_file.read_text() == 'immutable'
        assert (old / 'sentinel').read_text() == 'untouched'
        assert sandbox.temporary_snapshot(candidate)[str(old)] == before[str(old)]
    finally:
        helper.communicate('\n', timeout=3)
        listener.close()
