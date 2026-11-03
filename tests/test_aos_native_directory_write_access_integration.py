"""Shell temporary selection uses exact directory access, never broader writes."""
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.store import Rejected
from test_aos_native_directory_read_integration import protected_toolchain


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native shell temporary boundary')
@pytest.mark.parametrize('directory_access', [False, True])
def test_real_native_shell_uses_normal_temp_parent_with_exact_access(tmp_path, directory_access):
    p = protected_toolchain(tmp_path)
    capabilities = p.recipe['native_sandbox']
    capabilities['temporary_paths'] = [
        {'parent': '/private/var/tmp', 'name': 'sh-thd-{counter}', 'kind': 'file'}]
    if directory_access:
        capabilities['directory_write_access_paths'] = ['/private/var/tmp']
    child = ('import fcntl,json,os,stat,sys\n'
             "print(json.dumps({'path':fcntl.fcntl(0,50,b'\\0'*1024).split(b'\\0')[0].decode(),"
             "'regular':stat.S_ISREG(os.fstat(0).st_mode),'mode':stat.S_IMODE(os.fstat(0).st_mode),"
             "'tmpdir':os.environ.get('TMPDIR'),'input':sys.stdin.read()}))\n")
    script = (shlex.quote(sys.executable) + ' -I -S -c ' + shlex.quote(child)
              + " <<'AOS_RUNTIME_EOF'\nAOS owned runtime heredoc\nAOS_RUNTIME_EOF\n")
    payload = {'script': script, 'enabled': directory_access,
               'tmpdir': os.environ.get('TMPDIR'), 'tools': str(p.tools)}
    program = '''import json,os,re,subprocess,sys,unittest
from pathlib import Path
p=json.loads(sys.argv[1])
class NativeShellTemporary(unittest.TestCase):
 def test_normal_parent_and_bound_file(self):
  self.assertFalse(os.access('.',os.W_OK))
  self.assertEqual(os.access('/private/var/tmp',os.W_OK),p['enabled'])
  self.assertEqual((Path(p['tools'])/'usr'/'tool-input').read_text(),'approved tool input')
  env=dict(os.environ)
  if p['tmpdir'] is None:env.pop('TMPDIR',None)
  else:env['TMPDIR']=p['tmpdir']
  result=subprocess.run(['/bin/sh','-c',p['script']],env=env,capture_output=True,text=True,timeout=5)
  if not p['enabled']:
   self.assertNotEqual(result.returncode,0)
   self.assertRegex(result.stderr,'here[- ]document')
   return
  self.assertEqual(result.returncode,0,result.stderr)
  observed=json.loads(result.stdout)
  self.assertRegex(observed['path'],r'^/private/var/tmp/sh-thd-[0-9]+$')
  self.assertTrue(observed['regular'])
  self.assertEqual(observed['mode'],0o600)
  self.assertEqual(observed['tmpdir'],p['tmpdir'])
  self.assertEqual(observed['input'],'AOS owned runtime heredoc\\n')
  self.assertFalse(Path(observed['path']).exists())
unittest.main(argv=[sys.argv[0]])
'''
    p.check.update(argv=[sys.executable, '-I', '-S', '-c', program, json.dumps(payload)],
                   result_kind='native-tests', adapter_id='native-tests-v1',
                   suite_counts={'unittest': 1})
    p.recipe['_adapter_bindings'] = {'native-tests-v1': check_adapters.identities()['native-tests-v1']}
    try:
        _, model = p.runtime._profile(p.recipe)
        assert '/private/var/tmp' not in model['filesystem']
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass', result
        assert result['cleanup']['effects_reconciled']
        row = p.runtime._native_get(result['native_check_id'])
        assert row['native_process_receipt']['reconciled']
        assert row['native_process_receipt']['remaining'] == []
        assert row['native_path_contract']['native_protected_paths'] == [str(p.tools)]
        assert row['cleanup']['native_temporary_effects']['preexisting_preserved']
        assert row['cleanup']['native_temporary_effects']['created'] == {}
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native shell temporary boundary')
@pytest.mark.parametrize('scope', ['protected-tools', 'state', 'credentials', 'physical-state-alias'])
def test_native_directory_write_access_keeps_protected_scopes_immutable(tmp_path, scope):
    p = protected_toolchain(tmp_path)
    parent = p.tools if scope == 'protected-tools' else Path.home() / '.codex' if scope == 'credentials' else p.state
    if scope == 'physical-state-alias':
        parent = Path('/System/Volumes/Data') / p.state.relative_to('/')
        if not parent.is_dir() or not os.path.samefile(parent, p.state):
            p.runtime.close()
            pytest.skip('Fixture state has no writable-volume alias')
    p.recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(parent), 'name': 'sh-thd-{counter}', 'kind': 'file'}],
        directory_write_access_paths=[str(parent)])
    try:
        with pytest.raises(Rejected, match='protected|denied namespace'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert not p.runtime._native_processes
        with p.runtime._db() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check')]
        assert len(rows) == 1
        assert rows[0]['phase'] == 'clean'
        assert rows[0]['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
        assert not rows[0].get('native_sandbox_boundary')
    finally:
        p.runtime.close()
