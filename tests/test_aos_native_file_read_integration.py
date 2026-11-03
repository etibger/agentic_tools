"""Native-only pinned data files are checked at launch and receipt reuse."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.store import Rejected
from test_aos_native_directory_read_integration import protected_toolchain


def pinned_file(tmp_path):
    p = protected_toolchain(tmp_path)
    p.native_file = tmp_path / 'native-config'
    p.native_file.write_text('immutable native configuration\n')
    p.recipe['native_sandbox']['file_read_paths'] = [
        {'path': str(p.native_file), 'sha256': hashlib.sha256(p.native_file.read_bytes()).hexdigest()}]
    program = '''import os,sys,unittest
from pathlib import Path
class PinnedNativeData(unittest.TestCase):
 def test_read_only_data(self):
  p=Path(sys.argv[1])
  self.assertEqual(p.read_text(),'immutable native configuration\\n')
  with self.assertRaises(PermissionError):os.close(os.open(p,os.O_WRONLY))
suite=unittest.defaultTestLoader.loadTestsFromTestCase(PinnedNativeData)
result=unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
'''
    p.check.update(argv=[sys.executable, '-I', '-S', '-c', program, str(p.native_file)],
                   result_kind='native-tests', adapter_id='native-tests-v1', suite_counts={'unittest': 1})
    p.recipe['_adapter_bindings'] = {'native-tests-v1': check_adapters.identities()['native-tests-v1']}
    return p


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native/model file permission boundary')
@pytest.mark.parametrize('role', ['implementation', 'assessment'])
def test_pinned_native_data_is_denied_to_both_model_roles(tmp_path, role):
    p = pinned_file(tmp_path)
    try:
        evidence = p.runtime._probe_sandbox(
            {'assignment': {'kind': role}, 'attempt_id': 'pinned-data-probe'}, p.recipe)
        assert evidence['checks']['native_file_read_denied_0'] is True
        assert p.native_file.read_text() == 'immutable native configuration\n'
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native file permission boundary')
def test_completed_native_check_rechecks_pinned_data_before_cached_reuse(tmp_path, monkeypatch):
    p = pinned_file(tmp_path)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass'
        row = p.runtime._native_get(result['native_check_id'])
        assert row['native_file_reads'][0]['sha256'] == p.recipe['native_sandbox']['file_read_paths'][0]['sha256']
        assert row['native_path_probe']['checks']['native_file_read_0'] is True
        monkeypatch.setattr(p.runtime, '_native_profile_prepare', lambda *a: pytest.fail('cached receipt relaunched'))
        p.native_file.write_text('changed after the completed check\n')
        with pytest.raises(Rejected, match='read-file binding changed'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert p.runtime._native_stop(row['check_id'])['cleanup']['effects_reconciled']
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native file permission boundary')
def test_pinned_data_drift_after_probe_stops_before_supervisor_launch(tmp_path, monkeypatch):
    p = pinned_file(tmp_path)
    original_probe = p.runtime._probe_sandbox

    def change_after_probe(*args, **kwargs):
        result = original_probe(*args, **kwargs)
        p.native_file.write_text('changed after the permission probe\n')
        return result

    monkeypatch.setattr(p.runtime, '_probe_sandbox', change_after_probe)
    try:
        with pytest.raises(Rejected, match='read-file binding changed'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        with p.runtime._db() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check')]
        assert len(rows) == 1
        row = rows[0]
        assert row['phase'] == 'clean'
        assert row['preparation_failure']['proof']['stage'] == 'supervisor_preparation'
        assert row['preparation_failure']['proof']['supervisor_popen'] == 'not_called'
        assert row['cleanup']['native_admission']['launched'] is False
        assert not row.get('owner') and not p.runtime._native_processes
        assert (Path(row['output']) / 'stdout.txt').read_text() == ''
    finally:
        p.runtime.close()
