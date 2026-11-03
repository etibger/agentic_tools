"""Independent native temporary-boundary regression probes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentic_tools.aos import native_sandbox
from agentic_tools.aos.store import Rejected


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='actual macOS native permissions')


def _run(tmp_path, recipe, mapping, program, arguments, *, before=None):
    try:
        profile = native_sandbox.render(recipe, mapping, preexisting=before)
    except Rejected:
        return None
    saved = tmp_path / 'independent-profile.sb'
    saved.write_text(profile)
    process = subprocess.run(
        ['/usr/bin/sandbox-exec', '-f', str(saved), str(Path(sys._base_executable).resolve()),
         '-I', '-c', program, *map(str, arguments)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def _base(tmp_path):
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    return temporary, {'native_sandbox': {'version': native_sandbox.VERSION}}, {
        ':root': 'deny', ':minimal': 'read', str(Path(sys.base_prefix).resolve()): 'read'}


def _alias(path, use_alias):
    if not use_alias:
        return path
    alias = Path('/System/Volumes/Data') / path.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, path):
        pytest.skip('fixture has no writable-volume alias')
    return alias


@pytest.mark.parametrize('use_alias', [False, True])
def test_create_only_path_cannot_override_explicit_denied_ancestor(tmp_path, use_alias):
    temporary, recipe, mapping = _base(tmp_path)
    forbidden = temporary / 'protected-new'
    recipe['native_sandbox']['directory_create_paths'] = [str(forbidden)]
    mapping[str(_alias(temporary, use_alias))] = 'deny'
    result = _run(tmp_path, recipe, mapping, '''import json,os,sys
try:
 os.mkdir(sys.argv[1]);r={'created':True}
except OSError as e:r={'created':False,'errno':e.errno}
print(json.dumps(r))
''', [forbidden])
    assert result is None or result == {'created': False, 'errno': 1}, result
    assert not forbidden.exists()


@pytest.mark.parametrize('use_alias', [False, True])
def test_temporary_hardlinks_cannot_override_denied_parent_alias(tmp_path, use_alias):
    temporary, recipe, mapping = _base(tmp_path)
    recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(temporary), 'name': 'native-{pid}', 'kind': 'tree'}],
        temporary_hardlink_paths=[{'parent': str(temporary), 'name': 'native-{pid}'}],
        directory_create_paths=[str(temporary)])
    mapping[str(_alias(temporary, use_alias))] = 'deny'
    result = _run(tmp_path, recipe, mapping, '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1])/('native-'+str(os.getpid()))
try:
 p.mkdir();r={'created':True}
except OSError as e:r={'created':False,'errno':e.errno}
print(json.dumps(r))
''', [temporary])
    assert result is None or result == {'created': False, 'errno': 1}, result
    assert list(temporary.iterdir()) == []


def test_hardlinks_do_not_spread_to_unselected_temporary_trees(tmp_path):
    temporary, recipe, mapping = _base(tmp_path)
    recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(temporary), 'name': name, 'kind': 'tree'}
                         for name in ('linked-{pid}', 'unlinked-{pid}')],
        temporary_hardlink_paths=[{'parent': str(temporary), 'name': 'linked-{pid}'}])
    result = _run(tmp_path, recipe, mapping, '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);a=p/('linked-'+str(os.getpid()));b=p/('unlinked-'+str(os.getpid()))
a.mkdir();b.mkdir();(a/'input').write_text('a');(b/'input').write_text('b');r={}
for label,source,target in [('inside',a/'input',a/'same'),('outgoing',a/'input',b/'from-a'),('incoming',b/'input',a/'from-b')]:
 try:
  os.link(source,target);r[label]={'ok':True}
 except OSError as e:r[label]={'ok':False,'errno':e.errno}
print(json.dumps(r))
''', [temporary])
    assert result == {'inside': {'ok': True}, 'outgoing': {'ok': False, 'errno': 1},
                      'incoming': {'ok': False, 'errno': 1}}


@pytest.mark.parametrize('read_parent', [False, True])
@pytest.mark.parametrize('use_alias', [False, True])
def test_explicit_readonly_descendant_stays_readonly_under_temporary_template(tmp_path, read_parent, use_alias):
    temporary, recipe, mapping = _base(tmp_path)
    root = temporary / 'owned-new'
    guard = root / 'readonly'
    link_guard = root / 'readonly-link'
    recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(temporary), 'name': 'owned-new', 'kind': 'tree'}],
        temporary_hardlink_paths=[{'parent': str(temporary), 'name': 'owned-new'}])
    mapped_root = _alias(temporary, use_alias) / root.name
    mapping[str(mapped_root / guard.name)] = 'read'
    mapping[str(mapped_root / link_guard.name)] = 'read'
    if read_parent:
        mapping[str(temporary)] = 'read'
    result = _run(tmp_path, recipe, mapping, '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir();(p/'allowed').write_text('a');r={}
for label,fn in [('write',lambda:(p/'readonly').write_text('forbidden')),
                 ('link',lambda:os.link(p/'allowed',p/'readonly-link'))]:
 try:
  fn();r[label]={'ok':True}
 except OSError as e:r[label]={'ok':False,'errno':e.errno}
print(json.dumps(r))
''', [root])
    assert result is None or result == {'write': {'ok': False, 'errno': 1},
                                       'link': {'ok': False, 'errno': 1}}, result
    assert not guard.exists()
    assert not link_guard.exists()


@pytest.mark.parametrize('use_alias', [False, True])
def test_explicit_denied_descendant_blocks_temporary_writes_and_links(tmp_path, use_alias):
    temporary, recipe, mapping = _base(tmp_path)
    root = temporary / 'owned-new'
    mapped_root = _alias(temporary, use_alias) / root.name
    recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(temporary), 'name': root.name, 'kind': 'tree'}],
        temporary_hardlink_paths=[{'parent': str(temporary), 'name': root.name}])
    mapping.update({str(mapped_root / name): 'deny' for name in ('denied-write', 'denied-link')})
    result = _run(tmp_path, recipe, mapping, '''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir();(p/'allowed').write_text('a');r={}
for label,fn in [('write',lambda:(p/'denied-write').write_text('forbidden')),
                 ('link',lambda:os.link(p/'allowed',p/'denied-link'))]:
 try:
  fn();r[label]={'ok':True}
 except OSError as e:r[label]={'ok':False,'errno':e.errno}
print(json.dumps(r))
''', [root])
    assert result is None or result == {'write': {'ok': False, 'errno': 1},
                                       'link': {'ok': False, 'errno': 1}}, result
    assert not (root / 'denied-write').exists()
    assert not (root / 'denied-link').exists()


@pytest.mark.parametrize('access', ['read', 'deny'])
def test_absent_case_alias_guard_stays_protected_after_tree_creation(tmp_path, access):
    temporary, recipe, mapping = _base(tmp_path)
    if not temporary.with_name(temporary.name.upper()).is_dir():
        pytest.skip('fixture filesystem is case-sensitive')
    root = temporary / 'owned-new'
    recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(temporary), 'name': root.name, 'kind': 'tree'}],
        temporary_hardlink_paths=[{'parent': str(temporary), 'name': root.name}])
    mapping[str(temporary / root.name.upper() / 'guard')] = access
    result = _run(tmp_path, recipe, mapping, '''import json,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir();r={}
try:
 (p/'guard').write_text('forbidden');r={'written':True}
except OSError as e:r={'written':False,'errno':e.errno}
print(json.dumps(r))
''', [root])
    assert result is None or result == {'written': False, 'errno': 1}, result
    assert not (root / 'guard').exists()
