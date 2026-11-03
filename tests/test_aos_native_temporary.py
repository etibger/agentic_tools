"""Exact temporary roots are created exclusively and removed only when empty."""
import copy
import json
import os
import stat
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_sandbox, native_temporary
from agentic_tools.aos.store import Rejected
from test_aos_native_preparation import prepared_native, probe, run_check, saved


@pytest.fixture
def owned(tmp_path):
    parent = tmp_path / 'temporary'
    parent.mkdir()
    root, existing = parent / 'owned-root', parent / 'existing-root'
    existing.mkdir()
    recipe = {'native_sandbox': {'version': native_sandbox.VERSION,
        'temporary_paths': [{'parent': str(parent), 'name': p.name, 'kind': 'tree'} for p in (root, existing)],
        'owned_temporary_roots': [str(root)]}}
    journal = tmp_path / 'journal.json'
    writes = []
    def save(receipt):
        journal.write_text(json.dumps(receipt))
        writes.append(copy.deepcopy(receipt))
    return SimpleNamespace(recipe=recipe, parent=parent, root=root, existing=existing, journal=journal,
                           before=native_sandbox.temporary_snapshot(recipe), save=save, writes=writes)


def current(p):
    return json.loads(p.journal.read_text())


def prepare(p, check='nc-first', **kwargs):
    return native_temporary.prepare(p.recipe, check, p.before, kwargs.pop('save', p.save), **kwargs)


def remove(p, receipt, check='nc-first', **kwargs):
    return native_temporary.remove(p.recipe, check, receipt, kwargs.pop('save', p.save), quiescent=True, **kwargs)


def test_exclusive_creation_retains_original_snapshot_and_witness(owned):
    p = owned
    receipt = prepare(p)
    assert [r['roots'][0]['state'] for r in p.writes] == ['planned', 'creating', 'owned']
    witness = receipt['roots'][0]['witness']
    info = p.root.stat()
    assert (witness['device'], witness['inode'], witness['uid'], witness['mode']) == (
        info.st_dev, info.st_ino, os.getuid(), 0o700)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert receipt['before'] == p.before and str(p.root) not in receipt['before']
    assert str(p.existing) in receipt['before']
    rendered = native_sandbox.render(p.recipe, {':root': 'deny', ':minimal': 'read'}, preexisting=receipt['before'])
    assert json.dumps(str(p.root)) not in rendered
    assert json.dumps(str(p.existing)) in rendered
    assert prepare(p, previous=receipt) == receipt
    assert len(p.writes) == 3
    removed = remove(p, receipt)
    assert [r['roots'][0]['state'] for r in p.writes][-2:] == ['removing', 'removed']
    assert not p.root.exists() and p.existing.is_dir()
    assert native_temporary.verify_removed(p.recipe, 'nc-first', removed) == removed


@pytest.mark.parametrize('kind', ['empty', 'nonempty', 'symlink'])
def test_preexisting_root_is_preserved_never_adopted(owned, kind):
    p = owned
    if kind == 'symlink':
        p.root.symlink_to(p.existing, target_is_directory=True)
    else:
        p.root.mkdir()
        if kind == 'nonempty':
            (p.root / 'precious').write_text('preserve')
    with pytest.raises(Rejected):
        prepare(p)
    assert p.root.exists() and p.existing.is_dir()
    if kind == 'nonempty':
        assert (p.root / 'precious').read_text() == 'preserve'
    assert not p.writes and not p.journal.exists()


def test_all_roots_are_checked_before_any_creation(owned):
    p = owned
    p.recipe['native_sandbox']['owned_temporary_roots'].append(str(p.existing))
    with pytest.raises(Rejected, match='preexisting'):
        prepare(p)
    assert not p.root.exists() and p.existing.is_dir() and not p.writes


@pytest.mark.parametrize('damage', ['swapped', 'symlink', 'mode', 'missing', 'parent'])
def test_changed_identity_never_removes_current_path(owned, damage):
    p = owned
    receipt = prepare(p)
    if damage in ('swapped', 'symlink'):
        original = p.parent / 'original-owned-root'
        p.root.rename(original)
        if damage == 'swapped':
            p.root.mkdir(mode=0o700)
        else:
            p.root.symlink_to(p.existing, target_is_directory=True)
    elif damage == 'mode':
        p.root.chmod(0o755)
    elif damage == 'missing':
        p.root.rmdir()
    else:
        p.parent.rename(p.parent.with_name('original-parent'))
        p.parent.mkdir()
    prior = p.journal.read_bytes()
    with pytest.raises(Rejected):
        remove(p, receipt)
    assert p.journal.read_bytes() == prior
    if damage in ('swapped', 'symlink', 'mode'):
        assert p.root.exists()


def test_nonempty_root_has_no_recursive_cleanup(owned):
    p = owned
    receipt = prepare(p)
    nested = p.root / 'nested'
    nested.mkdir()
    artifact = nested / 'evidence'
    artifact.write_text('preserve native residue')
    with pytest.raises(Rejected, match='nonempty'):
        remove(p, receipt)
    assert artifact.read_text() == 'preserve native residue'
    assert current(p)['roots'][0]['state'] == 'owned'


@pytest.mark.parametrize('quiescent', [False, None, 1])
def test_no_removal_without_verified_quiescence(owned, quiescent):
    p = owned
    receipt = prepare(p)
    with pytest.raises(Rejected, match='quiescence'):
        native_temporary.remove(p.recipe, 'nc-first', receipt, p.save, quiescent=quiescent)
    assert p.root.is_dir() and current(p)['roots'][0]['state'] == 'owned'


def test_crash_after_mkdir_before_witness_remains_ambiguous(owned, monkeypatch):
    p = owned
    mkdir = os.mkdir
    def interrupted(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode, dir_fd=dir_fd)
        if path == p.root.name and dir_fd is not None:
            raise RuntimeError('SIMULATED crash after exclusive mkdir')
    monkeypatch.setattr(os, 'mkdir', interrupted)
    with pytest.raises(RuntimeError, match='after exclusive mkdir'):
        prepare(p)
    receipt = current(p)
    assert receipt['roots'][0]['state'] == 'creating' and p.root.is_dir()
    with pytest.raises(Rejected, match='ambiguous'):
        prepare(p, previous=receipt)
    with pytest.raises(Rejected, match='ambiguous'):
        remove(p, receipt)
    assert p.root.is_dir()


def test_failed_witness_save_cannot_be_used_for_cleanup(owned):
    p = owned
    def fail_witness(receipt):
        if receipt['roots'][0]['state'] == 'owned':
            raise OSError('SIMULATED witness journal failure')
        p.save(receipt)
    with pytest.raises(OSError, match='witness journal failure'):
        prepare(p, save=fail_witness)
    assert current(p)['roots'][0]['state'] == 'creating' and p.root.is_dir()
    with pytest.raises(Rejected, match='ambiguous'):
        remove(p, current(p))


@pytest.mark.parametrize('failure', ['before-rmdir', 'after-rmdir'])
def test_removal_save_failures_never_infer_completion(owned, failure):
    p = owned
    receipt = prepare(p)
    def fail_save(candidate):
        if candidate['roots'][0]['state'] == ('removing' if failure == 'before-rmdir' else 'removed'):
            raise OSError('SIMULATED removal journal failure')
        p.save(candidate)
    with pytest.raises(OSError, match='removal journal failure'):
        remove(p, receipt, save=fail_save)
    retained = current(p)
    if failure == 'before-rmdir':
        assert p.root.is_dir() and retained['roots'][0]['state'] == 'owned'
    else:
        assert not p.root.exists() and retained['roots'][0]['state'] == 'removing'
        p.root.mkdir(mode=0o700)
        with pytest.raises(Rejected, match='ambiguous'):
            remove(p, retained)
        assert p.root.is_dir()


def test_later_check_can_reuse_root_without_old_receipt_removing_it(owned):
    p = owned
    first = remove(p, prepare(p))
    second = prepare(p, check='nc-second')
    writes = len(p.writes)
    assert remove(p, first) == first
    assert len(p.writes) == writes and p.root.is_dir()
    assert native_temporary.verify(p.recipe, 'nc-second', second) == second
    remove(p, second, check='nc-second')
    third = prepare(p, check='nc-third')
    assert third['roots'][0]['state'] == 'owned'
    assert remove(p, first) == first and p.root.is_dir()


def test_historical_removed_receipt_does_not_inspect_later_symlink(owned):
    p = owned
    receipt = remove(p, prepare(p))
    p.root.symlink_to(p.existing, target_is_directory=True)
    writes = len(p.writes)
    assert native_temporary.verify_removed(p.recipe, 'nc-first', receipt) == receipt
    assert remove(p, receipt) == receipt
    assert p.root.is_symlink() and p.existing.is_dir() and len(p.writes) == writes


@pytest.mark.parametrize('damage', ['check', 'contract', 'snapshot', 'retained-before'])
def test_exact_journal_binding_and_original_snapshot_are_required(owned, damage):
    p = owned
    receipt = prepare(p)
    if damage == 'check':
        receipt['check_id'] = 'nc-other'
    elif damage == 'contract':
        receipt['contract_digest'] = 'sha256:wrong'
    elif damage == 'retained-before':
        receipt['before'] = {}
    else:
        p.before = native_sandbox.temporary_snapshot(p.recipe)
    with pytest.raises(Rejected):
        prepare(p, previous=receipt)
    assert p.root.is_dir()


def owned_runtime(p):
    root = p.temporary / 'owned-root'
    p.recipe['native_sandbox']['temporary_paths'].append({'parent': str(p.temporary), 'name': root.name, 'kind': 'tree'})
    p.recipe['native_sandbox']['owned_temporary_roots'] = [str(root)]
    return root


def test_no_launch_failure_removes_owned_empty_root_and_replays_historically(prepared_native, monkeypatch):
    p = prepared_native
    root = owned_runtime(p)
    probe(p, monkeypatch)
    with pytest.raises(Rejected, match='runtime_sandbox'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'clean' and not root.exists()
    assert row['owned_temporary']['roots'][0]['state'] == 'removed'
    assert row['preparation_failure']['proof']['owned_temporary'] == row['owned_temporary']
    assert str(root) not in row['native_temporary_before']
    target = p.temporary / 'later-unrelated'
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'
    assert root.is_symlink() and target.is_dir() and p.calls == ['probe']


def test_no_launch_failure_cannot_remove_nonempty_owned_root(prepared_native, monkeypatch):
    import subprocess
    p = prepared_native
    root = owned_runtime(p)
    def denied(args, **kwargs):
        p.calls.append('probe')
        (root / 'retained-evidence').write_text('preserve')
        return subprocess.CompletedProcess(args, 1, '{"access":false}', '')
    monkeypatch.setattr(subprocess, 'run', denied)
    with pytest.raises(Rejected, match='nonempty'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'cleanup' not in row and 'preparation_failure' not in row
    assert row['owned_temporary']['roots'][0]['state'] == 'owned'
    assert (root / 'retained-evidence').read_text() == 'preserve'
    assert p.calls == ['probe']


def test_interrupted_creation_cannot_be_reconciled_by_no_launch_failure(prepared_native, monkeypatch):
    p = prepared_native
    root = owned_runtime(p)
    original = os.mkdir
    def interrupted(path, mode=0o777, *, dir_fd=None):
        original(path, mode, dir_fd=dir_fd)
        if path == root.name and dir_fd is not None:
            raise RuntimeError('SIMULATED creation interruption')
    monkeypatch.setattr(os, 'mkdir', interrupted)
    with pytest.raises(Rejected, match='ambiguous'):
        run_check(p)
    row = saved(p)
    assert row['owned_temporary']['roots'][0]['state'] == 'creating'
    assert row['phase'] == 'intent' and 'cleanup' not in row and root.is_dir()
    assert p.calls == []


def test_runtime_does_not_publish_an_unsaved_owned_witness(prepared_native, monkeypatch):
    p = prepared_native
    root = owned_runtime(p)
    original = p.runtime._native_save
    def fail_owned(candidate):
        owned = candidate.get('owned_temporary')
        if owned and owned['roots'][0]['state'] == 'owned':
            raise OSError('SIMULATED durable witness write failure')
        original(candidate)
    monkeypatch.setattr(p.runtime, '_native_save', fail_owned)
    with pytest.raises(Rejected, match='ambiguous'):
        run_check(p)
    row = saved(p)
    assert row['owned_temporary']['roots'][0]['state'] == 'creating'
    assert row['phase'] == 'intent' and 'cleanup' not in row and root.is_dir()
    assert p.calls == []
