"""Independent runtime checks for exact, check-owned temporary directory lifetime."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos import codex_runtime, native_processes, native_temporary
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected
from test_aos_native_directory_read_integration import protected_toolchain


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native temporary ownership boundary')


def owned_fixture(tmp_path, mode='complete'):
    p = protected_toolchain(tmp_path)
    p.temporary = tmp_path / 'native-temporary'
    p.temporary.mkdir()
    p.root = p.temporary / 'exact-owned'
    p.neighbor = p.temporary / 'unrelated'
    p.neighbor.write_text('preserve unrelated temporary data')
    p.recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(p.temporary), 'name': p.root.name, 'kind': 'tree'}],
        owned_temporary_roots=[str(p.root)])
    payload = {'root': str(p.root), 'outputs': str(p.outputs), 'mode': mode}
    program = '''import json,os,signal,stat,sys,time,unittest
from pathlib import Path
p=json.loads(sys.argv[1])
class OwnedTemporary(unittest.TestCase):
 def test_native_uses_exact_owned_directory(self):
  root,outputs=Path(p['root']),Path(p['outputs'])
  self.assertTrue(root.is_dir())
  self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)
  work=root/'ordinary-native-file'
  work.write_text('native temporary data')
  self.assertEqual(work.read_text(),'native temporary data')
  work.unlink()
  (outputs/'launched').write_text('one native execution')
  if p['mode']=='nonempty':(root/'retained').write_text('must not recursively delete')
  if p['mode']=='cancel':
   (outputs/'ready').write_text(str(os.getpid()))
   time.sleep(30)
  if p['mode']=='interrupt':os.kill(os.getppid(),signal.SIGTERM)
unittest.main(argv=[sys.argv[0]])
'''
    p.check.update(argv=[sys.executable, '-I', '-S', '-c', program, json.dumps(payload)],
                   result_kind='native-tests', adapter_id='native-tests-v1', suite_counts={'unittest': 1})
    p.recipe['_adapter_bindings'] = {'native-tests-v1': check_adapters.identities()['native-tests-v1']}
    return p


def run_check(p, runtime=None):
    return (runtime or p.runtime)._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)


def saved(p):
    with p.runtime._db() as db:
        rows = [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check')]
    assert len(rows) == 1
    return rows[0]


def assert_removed(p, row):
    assert not p.root.exists()
    assert p.neighbor.read_text() == 'preserve unrelated temporary data'
    owned = row['owned_temporary']
    assert owned == row['cleanup']['owned_temporary']
    assert owned['before'] == row['native_temporary_before']
    assert str(p.root) not in owned['before']
    assert len(owned['roots']) == 1
    root = owned['roots'][0]
    assert root['path'] == str(p.root) and root['state'] == 'removed'
    assert root['witness']['mode'] == 0o700 and root['witness']['uid'] == os.getuid()
    assert root['created_at'] and root['removed_at']
    assert row['cleanup']['effects_reconciled']
    return owned


def no_new_admission(monkeypatch, runtime):
    def unexpected(*args, **kwargs):
        pytest.fail('A retained native check attempted a new admission or temporary creation')
    monkeypatch.setattr(runtime, '_native_profile_prepare', unexpected)
    monkeypatch.setattr(native_temporary, 'prepare', unexpected)


@pytest.mark.parametrize('reuse', ['directory', 'symlink'])
def test_real_owned_temporary_completion_and_replay_preserve_reused_path(tmp_path, monkeypatch, reuse):
    p = owned_fixture(tmp_path)
    reopened = None
    try:
        result = run_check(p)
        assert result['result'] == 'pass'
        row = saved(p)
        owned = assert_removed(p, row)
        assert row['native_sandbox_boundary']['temporary_before'] == owned['before']
        assert row['cleanup']['native_temporary_effects']['created'] == {}
        native_processes.verify_absent(row['native_process_receipt'])
        assert row['native_process_receipt']['reconciled']
        # A later owner's reuse must not be treated as unfinished old cleanup.
        if reuse == 'symlink':
            target = p.temporary / 'later-owner-target'
            target.mkdir(mode=0o700)
            p.root.symlink_to(target, target_is_directory=True)
        else:
            p.root.mkdir(mode=0o700)
        replacement = p.root / 'new-owner-data'
        replacement.write_text('new path owner')
        identity = (p.root.stat().st_dev, p.root.stat().st_ino)
        reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
        no_new_admission(monkeypatch, reopened)
        if reuse == 'directory':
            assert run_check(p, reopened) == result
        assert reopened._native_stop(row['check_id'], cancel=True)['owned_temporary'] == owned
        assert (p.root.stat().st_dev, p.root.stat().st_ino) == identity
        assert replacement.read_text() == 'new path owner'
        assert (p.outputs / 'launched').read_text() == 'one native execution'
    finally:
        if reopened:
            reopened.close()
        p.runtime.close()


@pytest.mark.parametrize('reuse', ['directory', 'symlink'])
def test_reaped_preparation_failure_removes_owned_root_and_replay_preserves_new_owner(tmp_path, monkeypatch, reuse):
    p = owned_fixture(tmp_path)
    original_probe = p.runtime._probe_sandbox
    reopened = None
    def reject_after_probe(*args, **kwargs):
        assert p.root.is_dir()
        original_probe(*args, **kwargs)
        raise Rejected('independent_probe_failure', 'Observed nofork probe completed before rejection')
    monkeypatch.setattr(p.runtime, '_probe_sandbox', reject_after_probe)
    try:
        with pytest.raises(Rejected, match='independent_probe_failure'):
            run_check(p)
        row = saved(p)
        owned = assert_removed(p, row)
        assert row['phase'] == 'clean'
        assert row['preparation_failure']['proof']['probe']['state'] == 'reaped'
        assert row['preparation_failure']['proof']['supervisor_popen'] == 'not_called'
        assert row['preparation_failure']['proof']['owned_temporary'] == owned
        assert not row.get('owner') and not p.runtime._native_processes
        assert not (p.outputs / 'launched').exists()
        if reuse == 'symlink':
            target = p.temporary / 'later-owner-target'
            target.mkdir(mode=0o700)
            p.root.symlink_to(target, target_is_directory=True)
        else:
            p.root.mkdir(mode=0o700)
        (p.root / 'new-owner-data').write_text('new path owner')
        reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
        no_new_admission(monkeypatch, reopened)
        if reuse == 'directory':
            with pytest.raises(Rejected, match='native_preparation_failed'):
                run_check(p, reopened)
        assert reopened._native_stop(row['check_id'])['owned_temporary'] == owned
        assert (p.root / 'new-owner-data').read_text() == 'new path owner'
    finally:
        if reopened:
            reopened.close()
        p.runtime.close()


def test_real_cancellation_removes_only_empty_owned_root_after_census(tmp_path):
    p = owned_fixture(tmp_path, 'cancel')
    p.check['timeout_seconds'] = p.recipe['max_seconds'] = 20
    results, failures = [], []
    def execute():
        try:
            results.append(run_check(p))
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=execute)
    try:
        thread.start()
        deadline = time.monotonic() + 8
        while not (p.outputs / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.025)
        assert (p.outputs / 'ready').exists(), failures
        row = saved(p)
        assert row['owned_temporary']['roots'][0]['state'] == 'owned'
        assert p.root.is_dir()
        stopped = p.runtime._native_stop(row['check_id'], cancel=True)
        thread.join(timeout=10)
        assert not thread.is_alive() and not failures
        assert results[0]['result'] == 'cancelled'
        assert_removed(p, stopped)
        native_processes.verify_absent(stopped['native_process_receipt'])
    finally:
        p.runtime.close()
        thread.join(timeout=10)


def test_real_native_observer_interruption_still_reconciles_owned_root(tmp_path):
    p = owned_fixture(tmp_path, 'interrupt')
    try:
        result = run_check(p)
        assert result['result'] == 'blocked'
        row = saved(p)
        assert row['native_process_receipt']['interrupted']
        native_processes.verify_absent(row['native_process_receipt'])
        assert_removed(p, row)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('damage', ['nonempty', 'replacement'])
def test_real_native_cleanup_holds_nonempty_or_replaced_root_without_deleting(tmp_path, damage):
    p = owned_fixture(tmp_path, 'nonempty' if damage == 'nonempty' else 'complete')
    moved = p.temporary / 'original-owned-directory'
    def replace(stage):
        if stage == 'native_finished_before_cleanup':
            p.root.rename(moved)
            p.root.mkdir(mode=0o700)
    if damage == 'replacement':
        p.runtime._fault = replace
    try:
        with pytest.raises(Rejected, match='native_temporary_nonempty|native_temporary_identity'):
            run_check(p)
        row = saved(p)
        assert row['phase'] == 'cleanup_unknown' and not row.get('cleanup')
        assert row['owned_temporary']['roots'][0]['state'] == 'owned'
        native_processes.verify_absent(row['native_process_receipt'])
        assert p.root.is_dir()
        if damage == 'nonempty':
            assert (p.root / 'retained').read_text() == 'must not recursively delete'
        else:
            assert moved.stat().st_ino == row['owned_temporary']['roots'][0]['witness']['inode']
            assert p.root.stat().st_ino != moved.stat().st_ino
        assert p.neighbor.read_text() == 'preserve unrelated temporary data'
    finally:
        p.runtime.close()


@pytest.mark.parametrize('boundary', ['creation_witness_write', 'preparation_interrupt'])
def test_ambiguous_preparation_retains_root_and_never_recreates_it_on_replay(tmp_path, monkeypatch, boundary):
    p = owned_fixture(tmp_path)
    original_save = p.runtime._native_save
    if boundary == 'creation_witness_write':
        def fail_witness(row):
            if row.get('owned_temporary', {}).get('roots', [{}])[0].get('state') == 'owned':
                raise OSError('SIMULATED interrupted durable creation witness')
            original_save(row)
        monkeypatch.setattr(p.runtime, '_native_save', fail_witness)
        expected = Rejected
    else:
        def interrupt(*args, **kwargs):
            kwargs['preparation'].probe = {'state': 'launch_ambiguous'}
            raise KeyboardInterrupt('SIMULATED interruption during permission-probe launch')
        monkeypatch.setattr(p.runtime, '_probe_sandbox', interrupt)
        expected = Rejected
    try:
        with pytest.raises(expected):
            run_check(p)
        row = saved(p)
        assert row['phase'] == 'intent' and not row.get('cleanup') and not row.get('preparation_failure')
        assert row['owned_temporary']['roots'][0]['state'] == (
            'creating' if boundary == 'creation_witness_write' else 'owned')
        assert p.root.is_dir() and not p.runtime._native_processes
        assert not (p.outputs / 'launched').exists()
        identity = (p.root.stat().st_dev, p.root.stat().st_ino)
        monkeypatch.setattr(p.runtime, '_native_save', original_save)
        no_new_admission(monkeypatch, p.runtime)
        # Only shorten the runtime's wait for a nonexistent observer. No
        # process/status/ownership evidence is invented for this unfinished row.
        ticks = iter((100.0, 113.0))
        monkeypatch.setattr(codex_runtime, 'time', SimpleNamespace(
            monotonic=lambda: next(ticks), sleep=lambda _: None))
        with pytest.raises(Rejected, match='check_cleanup_unknown'):
            run_check(p)
        assert saved(p)['phase'] == 'cleanup_unknown'
        assert (p.root.stat().st_dev, p.root.stat().st_ino) == identity
        assert p.neighbor.read_text() == 'preserve unrelated temporary data'
    finally:
        # These two boundaries positively launched no process; keep ambiguous
        # fixture evidence intact without another timed cancellation attempt.
        monkeypatch.setattr(p.runtime, '_native_stop', lambda *args, **kwargs: None)
        p.runtime.close()


def test_before_probe_interrupt_reconciles_owned_root_without_native_launch(tmp_path, monkeypatch):
    p = owned_fixture(tmp_path)
    def interrupt(*args, **kwargs):
        assert kwargs['preparation'].renderer['state'] == 'reaped'
        assert kwargs['preparation'].probe == {'state': 'not_started'}
        raise KeyboardInterrupt('SIMULATED interruption before permission-probe launch')
    monkeypatch.setattr(p.runtime, '_probe_sandbox', interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_check(p)
        row = saved(p)
        assert row['phase'] == 'clean'
        assert_removed(p, row)
        proof = row['preparation_failure']['proof']
        assert proof['renderer']['state'] == 'reaped'
        assert proof['probe'] == {'state': 'not_started'}
        assert proof['supervisor_popen'] == 'not_called'
        assert not row.get('owner') and not p.runtime._native_processes
        assert not (p.outputs / 'launched').exists()
        p.root.mkdir(mode=0o700)
        replacement = p.root / 'new-owner-data'
        replacement.write_text('new path owner')
        no_new_admission(monkeypatch, p.runtime)
        with pytest.raises(Rejected, match='native_preparation_failed'):
            run_check(p)
        assert p.runtime._native_stop(row['check_id'])['preparation_failure']['proof'] == proof
        assert replacement.read_text() == 'new path owner'
    finally:
        p.runtime.close()


@pytest.mark.parametrize('scope', ['source', 'protected-tools', 'state', 'physical-state-alias'])
def test_renderer_rejects_protected_owned_root_before_privileged_creation(tmp_path, monkeypatch, scope):
    p = owned_fixture(tmp_path)
    parent = p.source if scope == 'source' else p.tools if scope == 'protected-tools' else p.state
    if scope == 'physical-state-alias':
        parent = Path('/System/Volumes/Data') / p.state.relative_to('/')
        if not parent.is_dir() or not os.path.samefile(parent, p.state):
            p.runtime.close()
            pytest.skip('Fixture state has no physical volume alias')
    forbidden = parent / 'must-never-be-created'
    p.recipe['native_sandbox'].update(
        temporary_paths=[{'parent': str(parent), 'name': forbidden.name, 'kind': 'tree'}],
        owned_temporary_roots=[str(forbidden)])
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(args)
        pytest.fail('Privileged temporary creation preceded protected-scope validation')
    monkeypatch.setattr(native_temporary, 'prepare', unexpected)
    try:
        with pytest.raises(Rejected, match='protected|denied namespace'):
            run_check(p)
        assert calls == [] and not forbidden.exists() and not p.root.exists()
        row = saved(p)
        assert row['phase'] == 'clean'
        assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
        assert not row.get('owned_temporary') and not row.get('native_sandbox_boundary')
        assert not row.get('owner') and not p.runtime._native_processes
    finally:
        p.runtime.close()
