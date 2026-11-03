"""Native directory reads coexist with immutable toolchains, never denied state."""
import json
import os
from pathlib import Path
import sys

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos import native_processes, native_sandbox
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import native_fixture


def protected_toolchain(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    tools = tmp_path / 'installed-toolchain'
    (tools / 'usr').mkdir(parents=True)
    (tools / 'private-directory').mkdir()
    (tools / 'usr' / 'tool-input').write_text('approved tool input')
    (tools / 'private-file').write_text('ungranted toolchain content')
    p.tools = tools
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    probe_interpreter=sys.executable,
                    native_protected_paths=[str(tools)],
                    native_sandbox={'version': native_sandbox.VERSION,
                                    'directory_read_paths': [str(tools)]})
    p.recipe['read_roots'].append(str(tools / 'usr'))
    return p


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native directory-read boundary')
def test_real_native_profile_reads_protected_tool_directory_without_widening_access(tmp_path):
    p = protected_toolchain(tmp_path)
    program = '''import json,os,unittest,sys
from pathlib import Path
p=Path(json.loads(sys.argv[1]))
class ToolchainDirectory(unittest.TestCase):
 def test_explicit_read_and_immutable_children(self):
  self.assertEqual(sorted(os.listdir(p)),['private-directory','private-file','usr'])
  self.assertEqual((p/'usr'/'tool-input').read_text(),'approved tool input')
  for operation in (lambda:(p/'private-file').read_text(),
                    lambda:os.listdir(p/'private-directory'),
                    lambda:(p/'new-file').write_text('forbidden'),
                    lambda:(p/'usr'/'tool-input').write_text('forbidden'),
                    lambda:os.chmod(p,0o777)):
   with self.assertRaises(PermissionError):operation()
unittest.main(argv=[sys.argv[0]])
'''
    p.check.update(argv=[sys.executable, '-I', '-S', '-c', program, json.dumps(str(p.tools))],
                   result_kind='native-tests', adapter_id='native-tests-v1',
                   suite_counts={'unittest': 1})
    p.recipe['_adapter_bindings'] = {'native-tests-v1': check_adapters.identities()['native-tests-v1']}
    before_mode = p.tools.stat().st_mode
    try:
        # The exact-directory extension belongs only to the dedicated native
        # renderer. It never becomes a broader model filesystem grant.
        _, model = p.runtime._profile(p.recipe)
        assert str(p.tools) not in model['filesystem']
        assert model['filesystem'][str(p.tools / 'usr')] == 'read'
        assert model['filesystem'][str(p.state)] == 'deny'
        assert model['filesystem'][str(Path.home() / '.codex')] == 'deny'
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass'
        assert result['cleanup']['effects_reconciled']
        row = p.runtime._native_get(result['native_check_id'])
        assert row['native_path_probe']['checks']['state_read_denied']
        assert all(row['native_path_probe']['checks'].values())
        assert row['native_sandbox_boundary']['contract']['directory_read_paths'] == [str(p.tools)]
        assert row['native_path_contract']['native_protected_paths'] == [str(p.tools)]
        assert row['native_process_receipt']['reconciled']
        assert (p.tools / 'usr' / 'tool-input').read_text() == 'approved tool input'
        assert (p.tools / 'private-file').read_text() == 'ungranted toolchain content'
        assert p.tools.stat().st_mode == before_mode
        assert not (p.tools / 'new-file').exists()
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native directory-read boundary')
@pytest.mark.parametrize('scope', ['state', 'credentials', 'physical-state-alias'])
def test_real_native_preparation_cannot_read_explicitly_denied_directory(tmp_path, scope):
    p = protected_toolchain(tmp_path)
    denied = p.state if scope != 'credentials' else Path.home() / '.codex'
    if scope == 'physical-state-alias':
        denied = Path('/System/Volumes/Data') / p.state.relative_to('/')
        if not denied.is_dir() or not os.path.samefile(denied, p.state):
            p.runtime.close()
            pytest.skip('Fixture state has no writable-volume alias')
    p.recipe['native_sandbox']['directory_read_paths'] = [str(denied)]
    try:
        with pytest.raises(Rejected, match='explicit denied namespace'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert not p.runtime._native_processes
        with p.runtime._db() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check')]
        assert len(rows) == 1
        row = rows[0]
        assert row['phase'] == 'clean'
        assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
        assert row['preparation_failure']['proof']['supervisor_popen'] == 'not_called'
        assert not row.get('native_sandbox_boundary')
        assert not row.get('native_process_receipt')
    finally:
        p.runtime.close()
