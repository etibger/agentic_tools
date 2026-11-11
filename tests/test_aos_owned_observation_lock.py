"""Observation coordination using fixture files, synthetic census and identities only."""
from copy import deepcopy
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import types

import pytest

from agentic_tools.aos import owned_host_command as command
from test_aos_owned_host_command import (
    CONTROLLER, OWNER_PID, PS_PID, harness, lifecycle, retained_running, valid_output,
)


@pytest.fixture
def binding(tmp_path):
    parent = tmp_path / 'observation'
    parent.mkdir(mode=0o700)
    path = parent / 'shared.lock'
    path.touch(mode=0o600)
    info = path.lstat()
    return {'path': str(path.resolve()), 'device': info.st_dev, 'inode': info.st_ino,
            'uid': info.st_uid, 'mode': info.st_mode, 'nlink': info.st_nlink}


def clean_audit(**overrides):
    return {'reaped': True, 'pipes_eof': True, 'stdout_closed': True,
            'stderr_closed': True, **overrides}


def forbidden(*args, **kwargs):
    raise AssertionError('Host census is forbidden in this test')


@pytest.fixture
def locked_lifecycle(lifecycle, monkeypatch):
    """Keep the existing fake census, adding actual producer close evidence."""
    leaf = command._observe
    def observe(deadline, audit, **kwargs):
        result = leaf(deadline, audit, **kwargs)
        audit[-1].update(stdout_closed=True, stderr_closed=True)
        return result
    monkeypatch.setattr(command, '_observe', observe)
    monkeypatch.setattr(command, '_ps_metadata', forbidden)
    return lifecycle


def fake_time(monkeypatch, start=100.0):
    state = types.SimpleNamespace(now=start, sleeps=[])
    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
    monkeypatch.setattr(command, 'time', types.SimpleNamespace(
        monotonic=lambda: state.now, sleep=sleep))
    return state


def read_lock(binding):
    return Path(binding['path']).read_bytes()


def hold_lock(binding):
    fd = os.open(binding['path'], os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def release_lock(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_clean_preflight_does_not_claim_or_observe(binding, monkeypatch):
    monkeypatch.setattr(command, '_observe', forbidden)
    state = fake_time(monkeypatch)
    status = {'failed': False}
    with command._observation_fence(binding, 130, status=status) as until:
        assert until == 105 and read_lock(binding) == b''
    with command._observation_fence(binding, None, status=status) as until:
        assert until is None and read_lock(binding) == b''
    assert state.now == 100 and status == {'failed': False}


def test_active_claim_precedes_census_and_survives_classification(binding, monkeypatch):
    fake_time(monkeypatch)
    audit, status = [], {'failed': False}
    with command._observation_fence(binding, 103, audit, status=status) as until:
        claim = json.loads(read_lock(binding))
        assert claim['format'] == 'owned-observation-active-v1'
        assert claim['observation_lock'] == binding
        assert claim['deadline_monotonic'] == until == 103
        audit.append(clean_audit())
        assert read_lock(binding), 'Claim spans selection and group classification too'
    assert read_lock(binding) == b'' and status == {'failed': False}


def test_classifier_failure_clears_only_positively_reaped_helper(binding, monkeypatch):
    fake_time(monkeypatch)
    audit, status = [], {'failed': False}
    with pytest.raises(ValueError, match='classification failed'):
        with command._observation_fence(binding, 110, audit, status=status):
            audit.append(clean_audit())
            raise ValueError('classification failed')
    assert read_lock(binding) == b'' and status['failed'] is False


@pytest.mark.parametrize('field,value', [
    ('reaped', False), ('pipes_eof', False), ('stdout_closed', False),
    ('stderr_closed', False), ('reaped', 1), ('stdout_closed', None),
])
def test_incomplete_fresh_helper_poison_is_not_reclaimable(binding, monkeypatch, field, value):
    fake_time(monkeypatch)
    audit, status = [clean_audit()], {'failed': False}
    with pytest.raises(RuntimeError, match='cleanup is unknown'):
        with command._observation_fence(binding, 110, audit, status=status):
            audit.append(clean_audit(**{field: value}))
    poisoned = read_lock(binding)
    assert poisoned and status['failed'] is True
    with pytest.raises(RuntimeError, match='ACTIVE or unknown'):
        with command._observation_fence(binding, 110, []):
            pytest.fail('A second helper must never start')
    assert read_lock(binding) == poisoned


def test_previous_clean_audit_cannot_clear_interrupted_new_observation(binding, monkeypatch):
    fake_time(monkeypatch)
    audit, status = [clean_audit()], {'failed': False}
    with pytest.raises(RuntimeError, match='cleanup is unknown'):
        with command._observation_fence(binding, 110, audit, status=status):
            raise KeyboardInterrupt('interrupted before fresh proof')
    assert read_lock(binding) and status['failed']


def test_missing_close_field_is_unknown(binding, monkeypatch):
    fake_time(monkeypatch)
    audit = []
    with pytest.raises(RuntimeError, match='cleanup is unknown'):
        with command._observation_fence(binding, 110, audit):
            audit.append({'reaped': True, 'pipes_eof': True, 'stdout_closed': True})
    assert read_lock(binding)


@pytest.mark.parametrize('fault', ['partial_write', 'initial_fsync'])
def test_claim_write_failure_never_releases_a_clean_inode(binding, monkeypatch, fault):
    fake_time(monkeypatch)
    fake_os = types.SimpleNamespace(**vars(os))
    if fault == 'partial_write':
        fake_os.write = lambda fd, data: os.write(fd, data[:11])
    else:
        def failed_sync(fd):
            raise OSError(errno.EIO, 'claim sync failed')
        fake_os.fsync = failed_sync
    monkeypatch.setattr(command, 'os', fake_os)
    status = {'failed': False}
    with pytest.raises(OSError):
        with command._observation_fence(binding, 110, [], status=status):
            pytest.fail('No census may start before durable ACTIVE')
    assert read_lock(binding) and status['failed']


def test_clear_sync_failure_is_sticky_after_positive_helper_cleanup(binding, monkeypatch):
    fake_time(monkeypatch)
    fake_os, calls, retained = types.SimpleNamespace(**vars(os)), [], []
    monkeypatch.setattr(command, '_RETAINED_OBSERVATION_LOCKS', retained, raising=False)
    def fsync(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError(errno.EIO, 'clear sync failed')
        return os.fsync(fd)
    fake_os.fsync = fsync
    monkeypatch.setattr(command, 'os', fake_os)
    status, audit = {'failed': False}, []
    with pytest.raises(OSError, match='clear sync failed'):
        with command._observation_fence(binding, 110, audit, status=status):
            audit.append(clean_audit())
    assert status['failed'] and audit == [clean_audit()] and retained == []
    # All helper resources are positively closed. Visible empty state cannot
    # overlap a lingering helper, even though this command remains unverified.
    assert read_lock(binding) == b''


def test_truncated_active_with_unknown_helper_retains_nonempty_hold(binding, monkeypatch):
    fake_time(monkeypatch)
    audit, status = [], {'failed': False}
    with pytest.raises(RuntimeError, match='claim changed'):
        with command._observation_fence(binding, 110, audit, status=status):
            Path(binding['path']).write_bytes(b'')
            audit.append(clean_audit(reaped=False))
    assert status['failed'] and read_lock(binding)
    with pytest.raises(RuntimeError, match='ACTIVE or unknown'):
        with command._observation_fence(binding, 110, []):
            pytest.fail('An unknown prior helper must exclude the next one')


@pytest.mark.parametrize('field,value', [
    ('inode', True), ('device', -1), ('uid', -1), ('nlink', 2),
    ('mode', stat.S_IFREG | 0o644), ('path', 'relative.lock'),
])
def test_binding_is_exact_typed_private_file(binding, field, value):
    with pytest.raises(ValueError):
        command._observation_binding({**binding, field: value})


@pytest.mark.parametrize('drift', ['replacement', 'symlink', 'hardlink', 'mode', 'parent_mode', 'uid'])
def test_file_identity_or_boundary_drift_blocks_before_observation(binding, monkeypatch, drift):
    fake_time(monkeypatch)
    path = Path(binding['path'])
    if drift == 'replacement':
        path.rename(path.with_name('old.lock'))
        path.touch(mode=0o600)
    elif drift == 'symlink':
        path.rename(path.with_name('old.lock'))
        path.symlink_to(path.with_name('old.lock'))
    elif drift == 'hardlink':
        os.link(path, path.with_name('alias.lock'))
    elif drift == 'mode':
        path.chmod(0o640)
    elif drift == 'parent_mode':
        path.parent.chmod(0o750)
    else:
        binding = {**binding, 'uid': binding['uid'] + 1}
    status = {'failed': False}
    with pytest.raises(RuntimeError):
        with command._observation_fence(binding, 110, [], status=status):
            pytest.fail('Drift must not authorize census')
    assert status['failed']


def test_replaced_inode_is_never_cleared_during_failure(binding, monkeypatch):
    fake_time(monkeypatch)
    path, old = Path(binding['path']), Path(binding['path']).with_name('retained.lock')
    audit, status = [], {'failed': False}
    with pytest.raises(RuntimeError, match='identity or permissions'):
        with command._observation_fence(binding, 110, audit, status=status):
            path.rename(old)
            path.touch(mode=0o600)
            path.write_bytes(b'unrelated replacement')
            audit.append(clean_audit())
    assert old.read_bytes() and path.read_bytes() == b'unrelated replacement'
    assert status['failed']


def test_every_participant_opens_fresh_noninherited_descriptor(binding, monkeypatch):
    fake_time(monkeypatch)
    fake_os, calls = types.SimpleNamespace(**vars(os)), []
    def opened(path, flags):
        fd = os.open(path, flags)
        calls.append((fd, flags, os.get_inheritable(fd)))
        return fd
    fake_os.open = opened
    monkeypatch.setattr(command, 'os', fake_os)
    for _ in range(2):
        with command._observation_fence(binding, 110):
            pass
    assert len(calls) == 2
    assert all(flags & os.O_CLOEXEC and not inherited for _, flags, inherited in calls)
    for fd, _, _ in calls:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('deadline,expected', [(100.08, 100.08), (130, 105)])
def test_contended_original_budget_expires_without_new_allowance(binding, monkeypatch, deadline, expected):
    state = fake_time(monkeypatch)
    fd = hold_lock(binding)
    try:
        with pytest.raises(TimeoutError):
            with command._observation_fence(binding, deadline, []):
                pytest.fail('A blocked helper cannot launch')
        assert state.now == pytest.approx(expected)
        assert sum(state.sleeps) == pytest.approx(expected - 100)
        assert read_lock(binding) == b''
    finally:
        release_lock(fd)


def test_acquisition_and_census_share_one_five_second_budget(binding, monkeypatch):
    state = fake_time(monkeypatch)
    fd = hold_lock(binding)
    released = False
    def sleep(seconds):
        nonlocal released
        state.now += seconds
        if state.now >= 102 and not released:
            release_lock(fd)
            released = True
    monkeypatch.setattr(command.time, 'sleep', sleep)
    audit = []
    try:
        with command._observation_fence(binding, 130, audit) as until:
            assert released and state.now >= 102
            assert until == 105, 'Wait time must not renew the helper allowance'
            audit.append(clean_audit())
    finally:
        if not released:
            release_lock(fd)
    assert read_lock(binding) == b''


def test_public_run_records_binding_and_verify_never_adds_census(locked_lifecycle, binding):
    state = locked_lifecycle
    report = state.run(observation_lock=binding)
    assert report['passed'] and report['cleanup_verified']
    assert report['observation_lock'] == binding
    assert report['observation_coordination'] == {'failed': False}
    assert read_lock(binding) == b''
    scans = len(state.census_deadlines)
    assert command.verify_completed(report)['passed']
    assert len(state.census_deadlines) == scans and len(state.launches) == 1


def test_poisoned_preflight_means_no_launch_and_unknown_shared_cleanup(locked_lifecycle, binding):
    Path(binding['path']).write_bytes(b'ACTIVE from unreaped prior observer')
    report = locked_lifecycle.run(observation_lock=binding)
    assert report['no_launch'] and not report['passed'] and not report['cleanup_verified']
    assert report['observation_coordination']['failed']
    assert not locked_lifecycle.launches and not locked_lifecycle.census_deadlines


def test_public_classification_cannot_outlive_same_observation_budget(locked_lifecycle, binding, monkeypatch):
    state = locked_lifecycle
    original = command.select_owned
    selections = []
    def select(*args, **kwargs):
        result = original(*args, **kwargs)
        selections.append(state.now)
        if len(selections) == 1:
            state.now += 5.01
        return result
    monkeypatch.setattr(command, 'select_owned', select)
    report = state.run(observation_lock=binding)
    assert state.admitted and len(state.launches) == 1
    assert not report['passed'] and report['timed_out']
    assert any('observation budget expired' in error for error in report['errors'])
    assert read_lock(binding) == b'', 'A classification timeout does not invent unknown helper IO'


@pytest.mark.parametrize('change', ['remove', 'add', 'inode'])
def test_saved_binding_presence_and_value_are_checked_before_checkpoint_age(locked_lifecycle, binding, change):
    report = locked_lifecycle.run(observation_lock=binding if change != 'add' else None)
    saved = deepcopy(report)
    saved['checkpoint_monotonic'] = report['checkpoint_monotonic'] - 1
    if change == 'remove':
        saved.pop('observation_lock')
        saved.pop('observation_coordination')
    elif change == 'add':
        saved.update(observation_lock=binding, observation_coordination={'failed': False})
    else:
        saved['observation_lock']['inode'] += 1
    (locked_lifecycle.control / 'receipt.json').write_text(json.dumps(saved))
    with pytest.raises(RuntimeError, match='observation lock binding changed'):
        command.verify_completed(report)


def test_sticky_coordination_failure_cannot_be_erased_by_newer_receipt(locked_lifecycle, binding):
    report = locked_lifecycle.run(observation_lock=binding)
    older = deepcopy(report)
    older['observation_coordination']['failed'] = True
    older['checkpoint_monotonic'] -= 1
    with pytest.raises(RuntimeError, match='No verified terminal'):
        command.verify_completed(older)


def test_legacy_completed_receipt_remains_compatible_without_lock(locked_lifecycle):
    report = locked_lifecycle.run()
    assert 'observation_lock' not in report
    assert command.verify_completed(report)['passed']


@pytest.mark.parametrize('expired', [False, True])
def test_completed_verification_accepts_clean_unlocked_original_inode(locked_lifecycle, binding, expired):
    report = locked_lifecycle.run(observation_lock=binding)
    if expired:
        locked_lifecycle.now = report['cleanup_deadline_monotonic'] + 100
    count = len(locked_lifecycle.census_deadlines)
    assert command.verify_completed(report)['passed']
    assert len(locked_lifecycle.census_deadlines) == count


def test_completed_verification_waits_for_healthy_holder_within_original_budget(locked_lifecycle, binding, monkeypatch):
    state = locked_lifecycle
    report = state.run(observation_lock=binding)
    fd, released = hold_lock(binding), False
    Path(binding['path']).write_bytes(b'ACTIVE current healthy observer')
    entry = state.now
    count = len(state.census_deadlines)
    def sleep(seconds):
        nonlocal released
        state.now += seconds
        if state.now >= entry + .075 and not released:
            Path(binding['path']).write_bytes(b'')
            release_lock(fd)
            released = True
    monkeypatch.setattr(command.time, 'sleep', sleep)
    try:
        assert command.verify_completed(report)['passed']
        assert released and entry < state.now <= entry + .101
        assert len(state.census_deadlines) == count
    finally:
        if not released:
            release_lock(fd)


@pytest.mark.parametrize('expired', [False, True])
def test_completed_verification_never_renews_original_cleanup_deadline(locked_lifecycle, binding, expired):
    state = locked_lifecycle
    report = state.run(observation_lock=binding)
    state.now = report['cleanup_deadline_monotonic'] + (1 if expired else -.05)
    entry, count = state.now, len(state.census_deadlines)
    fd = hold_lock(binding)
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            command.verify_completed(report)
        assert state.now == pytest.approx(entry if expired else report['cleanup_deadline_monotonic'])
        assert len(state.census_deadlines) == count
    finally:
        release_lock(fd)


def test_reconciliation_keeps_binding_without_inventing_lost_io_proof(locked_lifecycle, binding):
    state = locked_lifecycle
    snapshot = retained_running(state)
    snapshot.update(observation_lock=binding, observation_coordination={'failed': False})
    state.extra_identities.update({CONTROLLER: None, OWNER_PID: None})
    report = command.reconcile_owned(snapshot, deadline=120)
    assert report['observation_lock'] == binding and report['observed_stopped']
    assert report['recovery_lost_original_io_proof']
    assert not report['cleanup_verified'] and not report['owner_reaped'] and not report['pipes_eof']
    assert state.census_deadlines == [105]
    assert read_lock(binding) == b'' and not state.launches and not state.signals


def add_file_operations_to_raw_harness(harness, monkeypatch):
    fake_os = types.SimpleNamespace(**{**vars(os), **vars(command.os)})
    # The lock has real fixture ownership; fake census rows use that same UID.
    harness.effective_uid = os.geteuid()
    monkeypatch.setattr(command, 'os', fake_os)


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_actual_observe_close_failure_poisons_shared_observation(harness, binding, monkeypatch, stream):
    add_file_operations_to_raw_harness(harness, monkeypatch)
    process = harness.configure(actions=[{'code': 0, 'stdout': valid_output(
        child_uid=os.geteuid(), controller_uid=os.geteuid())}])
    def fail_close():
        raise OSError(errno.EIO, 'synthetic pipe close failed')
    getattr(process, stream).close = fail_close
    status = {'failed': False}
    with pytest.raises(RuntimeError, match='cleanup is unknown'):
        with command._observation_fence(binding, 5, harness.audit, status=status) as deadline:
            command._observe(deadline, harness.audit)
    assert harness.audit[0]['reaped'] and harness.audit[0]['pipes_eof']
    assert harness.audit[0][stream + '_closed'] is False
    assert read_lock(binding) and status['failed']
    assert len(harness.launches) == 1 and not harness.signals


def test_actual_clean_setuid_census_without_birth_identity_clears_lock(harness, binding, monkeypatch):
    add_file_operations_to_raw_harness(harness, monkeypatch)
    harness.metadata['st_mode'] = stat.S_IFREG | stat.S_ISUID | 0o755
    harness.configure(identity=PermissionError(errno.EPERM, 'synthetic unavailable birth'),
                      actions=[{'code': 0, 'stdout': valid_output(
                          child_uid=0, controller_uid=os.geteuid())}])
    status = {'failed': False}
    with command._observation_fence(binding, 5, harness.audit, status=status) as deadline:
        rows = command._observe(deadline, harness.audit)
        assert rows[PS_PID]['uid'] == 0
    assert read_lock(binding) == b'' and status['failed'] is False
    assert harness.audit[0]['stdout_closed'] and harness.audit[0]['stderr_closed']
    assert harness.audit[0]['owner'] is None and not harness.signals


@pytest.mark.parametrize('expired', [False, True])
def test_terminal_reconciliation_respects_tighter_caller_deadline(locked_lifecycle, binding, expired):
    state = locked_lifecycle
    report = state.run(observation_lock=binding)
    entry, scans = state.now, len(state.census_deadlines)
    caller_deadline = entry - .01 if expired else entry + .05
    fd = hold_lock(binding)
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            command.reconcile_owned(report, deadline=caller_deadline)
        assert state.now == pytest.approx(entry if expired else caller_deadline)
        assert len(state.census_deadlines) == scans
        assert report['cleanup_deadline_monotonic'] == state.deadline + 26
    finally:
        release_lock(fd)


def test_identity_verification_consumes_the_same_observation_deadline(monkeypatch):
    state = fake_time(monkeypatch)
    calls = []
    def slow_identity(pid):
        calls.append(pid)
        state.now += 1.01
        return {'pid': pid}
    monkeypatch.setattr(command, 'native_processes', types.SimpleNamespace(identity=slow_identity))
    with pytest.raises(TimeoutError, match='observation budget expired'):
        command._observation_identity(101)(12345)
    assert calls == [12345] and state.now == pytest.approx(101.01)


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), '110'])
def test_invalid_deadline_never_enters_coordination(binding, deadline):
    with pytest.raises(ValueError, match='finite original observation deadline'):
        with command._observation_fence(binding, deadline, []):
            pytest.fail('Invalid deadlines cannot authorize an observer')
    assert read_lock(binding) == b''


def test_nonblocking_check_cannot_authorize_an_observer(binding):
    with pytest.raises(ValueError, match='requires its original deadline'):
        with command._observation_fence(binding, None, []):
            pytest.fail('A helper always needs its saved finite deadline')
    assert read_lock(binding) == b''


# These harmless workers exercise real interprocess flock contention. Process
# observations and every identity lookup remain entirely synthetic.
_OBS_A, _OBS_B, _OBS_HELPER = 900000001, 900000002, 900000003


class _ObservationWorkerStop(Exception):
    pass


def _observation_receive(connection, expected=None, timeout=5.0):
    if not connection.poll(timeout):
        raise TimeoutError(f'Timed out waiting for {expected!r}')
    message = connection.recv()
    if message == ('stop',):
        raise _ObservationWorkerStop()
    if expected is not None and message != expected:
        raise AssertionError(f'Expected {expected!r}, received {message!r}')
    return message


def _observation_expect(connection, kind):
    message = _observation_receive(connection)
    assert message[0] == kind, message
    return message


def _observation_worker(connection, actor, binding, marker_path):
    import contextlib
    import time
    import traceback

    marker = Path(marker_path)
    state = {'mode': None, 'contentions': 0}
    original_same, real_flock = command.native_processes.same, fcntl.flock
    identities = {
        _OBS_A: {'pid': _OBS_A, 'uid': 501, 'birth': [100, 1],
                 'parent': 900000000, 'group': _OBS_A, 'status': 2},
        _OBS_B: {'pid': _OBS_B, 'uid': 501, 'birth': [100, 2],
                 'parent': _OBS_A, 'group': _OBS_B, 'status': 2},
        _OBS_HELPER: {'pid': _OBS_HELPER, 'uid': 0, 'birth': [100, 3],
                     'parent': _OBS_B, 'group': _OBS_HELPER, 'status': 2},
    }
    def identity(pid):
        if pid == _OBS_HELPER and not marker.exists():
            return None
        if pid in identities:
            return dict(identities[pid])
        raise AssertionError(f'Unexpected native identity request: {pid}')
    command.native_processes = types.SimpleNamespace(identity=identity, same=original_same)
    command._ps_metadata = forbidden
    command.subprocess = types.SimpleNamespace(**vars(command.subprocess))
    command.subprocess.Popen = forbidden

    def tracked_flock(fd, operation):
        try:
            return real_flock(fd, operation)
        except BlockingIOError:
            current = os.fstat(fd)
            if (actor == 'b' and state['mode'] == 'locked'
                    and current.st_dev == binding['device'] and current.st_ino == binding['inode']
                    and operation & fcntl.LOCK_EX and operation & fcntl.LOCK_NB):
                state['contentions'] += 1
                connection.send(('contended', state['contentions']))
                _observation_receive(connection, ('retry',))
            raise
    command.fcntl = types.SimpleNamespace(**vars(fcntl))
    command.fcntl.flock = tracked_flock

    def observe(deadline, audit, *, cleanup_deadline=None):
        assert deadline <= cleanup_deadline
        if state['mode'] == 'locked':
            assert read_lock(binding), 'ACTIVE must precede synthetic helper start'
        if actor == 'b':
            marker.write_text('synthetic nested UID0 census helper\n')
            try:
                connection.send(('helper-live', state['mode']))
                _observation_receive(connection, ('finish-census',))
            finally:
                marker.unlink(missing_ok=True)
        rows = {pid: dict(identities[pid]) for pid in (_OBS_A, _OBS_B)}
        if marker.exists():
            rows[_OBS_HELPER] = dict(identities[_OBS_HELPER])
        audit.append(clean_audit())
        return rows
    command._observe = observe
    try:
        connection.send(('ready', actor))
        while True:
            request = _observation_receive(connection)
            assert request in {('run', 'unlocked'), ('run', 'locked')}
            mode = request[1]
            state.update(mode=mode, contentions=0)
            audit, status = [], {'failed': False}
            deadline = time.monotonic() + 30
            scope = (command._observation_fence(binding, deadline, audit, status=status)
                     if mode == 'locked' else contextlib.nullcontext(deadline))
            rejection = None
            with scope as until:
                rows = command._observe(until, audit, cleanup_deadline=until)
                leader = identities[_OBS_A if actor == 'a' else _OBS_B]
                if actor == 'a' and mode == 'unlocked':
                    try:
                        command.select_owned(rows, leader, {})
                    except RuntimeError as error:
                        rejection = str(error)
                        assert 'cross-UID' in rejection
                    else:
                        raise AssertionError('Uncoordinated nested UID0 helper was accepted')
                else:
                    if actor == 'a':
                        connection.send(('observed',))
                        _observation_receive(connection, ('select',))
                    live, members = command.select_owned(rows, leader, {})
                    assert live and set(members) == ({_OBS_B} if actor == 'a' else set())
                    if actor == 'a':
                        assert read_lock(binding)
                        connection.send(('selected', sorted(members)))
                        _observation_receive(connection, ('group',))
                    group = {pid for pid, row in rows.items()
                             if pid != leader['pid'] and row['group'] == leader['pid']}
                    assert not group
                    if actor == 'a':
                        assert read_lock(binding)
                        connection.send(('grouped',))
                        _observation_receive(connection, ('leave',))
                    if mode == 'locked':
                        command._observation_time(until)
            assert audit and status['failed'] is False
            connection.send(('done', mode, rejection, status['failed']))
    except _ObservationWorkerStop:
        return
    except BaseException:
        connection.send(('error', actor, traceback.format_exc()))
        raise
    finally:
        if actor == 'b':
            marker.unlink(missing_ok=True)
        connection.close()


def test_real_interprocess_lock_spans_census_selection_and_group_decision(binding):
    import multiprocessing

    marker = Path(binding['path']).with_name('synthetic-helper-live')
    context = multiprocessing.get_context('spawn')
    peers, exit_codes = [], []
    try:
        for actor in ('a', 'b'):
            parent, child = context.Pipe()
            process = context.Process(target=_observation_worker,
                                      args=(child, actor, binding, str(marker)))
            process.start()
            child.close()
            peers.append((process, parent))
        a, b = peers[0][1], peers[1][1]
        assert _observation_expect(a, 'ready') == ('ready', 'a')
        assert _observation_expect(b, 'ready') == ('ready', 'b')
        # Positive baseline: this exact synthetic overlap must cause rejection.
        b.send(('run', 'unlocked'))
        assert _observation_expect(b, 'helper-live') == ('helper-live', 'unlocked')
        a.send(('run', 'unlocked'))
        baseline = _observation_expect(a, 'done')
        assert baseline[1] == 'unlocked' and 'cross-UID' in baseline[2]
        b.send(('finish-census',))
        assert _observation_expect(b, 'done') == ('done', 'unlocked', None, False)
        assert not marker.exists() and read_lock(binding) == b''
        # Workers are already started before either bounded acquisition begins.
        a.send(('run', 'locked'))
        _observation_expect(a, 'observed')
        b.send(('run', 'locked'))
        assert _observation_expect(b, 'contended') == ('contended', 1)
        assert not marker.exists()
        a.send(('select',))
        assert _observation_expect(a, 'selected') == ('selected', [_OBS_B])
        b.send(('retry',))
        assert _observation_expect(b, 'contended') == ('contended', 2)
        assert not marker.exists()
        a.send(('group',))
        _observation_expect(a, 'grouped')
        b.send(('retry',))
        assert _observation_expect(b, 'contended') == ('contended', 3)
        assert not marker.exists()
        a.send(('leave',))
        assert _observation_expect(a, 'done') == ('done', 'locked', None, False)
        assert read_lock(binding) == b''
        b.send(('retry',))
        assert _observation_expect(b, 'helper-live') == ('helper-live', 'locked')
        b.send(('finish-census',))
        assert _observation_expect(b, 'done') == ('done', 'locked', None, False)
        assert not marker.exists() and read_lock(binding) == b''
    finally:
        for process, connection in peers:
            try:
                connection.send(('stop',))
            except (BrokenPipeError, EOFError, OSError):
                pass
        for process, connection in peers:
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()  # Exact retained harmless fixture child only.
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            exit_codes.append(process.exitcode)
            connection.close()
            if not process.is_alive():
                process.close()
    assert exit_codes == [0, 0]


@pytest.mark.parametrize('failure', ['write_error', 'zero_write'])
def test_failed_poison_restoration_retains_exact_noninherited_locked_fd(binding, monkeypatch, failure):
    fake_time(monkeypatch)
    retained, audit, status, writes = [], [], {'failed': False}, []
    monkeypatch.setattr(command, '_RETAINED_OBSERVATION_LOCKS', retained, raising=False)
    fake_os = types.SimpleNamespace(**vars(os))
    def write(fd, data):
        writes.append(data)
        if len(writes) == 1:
            return os.write(fd, data)
        if failure == 'zero_write':
            return 0
        raise OSError(errno.EIO, 'synthetic poison write failed')
    fake_os.write = write
    monkeypatch.setattr(command, 'os', fake_os)
    contender = None
    try:
        with pytest.raises((RuntimeError, OSError)):
            with command._observation_fence(binding, 110, audit, status=status):
                Path(binding['path']).write_bytes(b'')
                audit.append(clean_audit(reaped=False))
        assert status['failed'] and read_lock(binding) == b''
        assert len(retained) == 1
        descriptor = retained[0]
        assert type(descriptor) is int and not os.get_inheritable(descriptor)
        info = os.fstat(descriptor)
        assert (info.st_dev, info.st_ino) == (binding['device'], binding['inode'])
        contender = os.open(binding['path'], os.O_RDWR | os.O_CLOEXEC)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if contender is not None:
            os.close(contender)
        # There is no real helper. The test owns these exact descriptors and
        # releases them only after proving the process-lifetime protection.
        for descriptor in retained:
            release_lock(descriptor)
        retained.clear()


def test_completed_receipt_cannot_override_later_poison(locked_lifecycle, binding):
    state = locked_lifecycle
    report = state.run(observation_lock=binding)
    scans = len(state.census_deadlines)
    Path(binding['path']).write_bytes(b'ACTIVE from another incomplete observer')
    with pytest.raises(RuntimeError, match='ACTIVE or unknown'):
        command.verify_completed(report)
    assert len(state.census_deadlines) == scans and len(state.launches) == 1
    assert read_lock(binding)


def test_reconciliation_poison_prevents_census_and_never_claims_cleanup(locked_lifecycle, binding):
    state = locked_lifecycle
    snapshot = retained_running(state)
    snapshot.update(observation_lock=binding, observation_coordination={'failed': False})
    state.extra_identities.update({CONTROLLER: None, OWNER_PID: None})
    Path(binding['path']).write_bytes(b'ACTIVE incomplete helper')
    report = command.reconcile_owned(snapshot, deadline=120)
    assert report['observation_coordination']['failed']
    assert not report['cleanup_verified'] and not report['passed']
    assert not state.census_deadlines and not state.launches and not state.signals
    assert read_lock(binding) == b'ACTIVE incomplete helper'


def test_durable_claim_time_counts_before_helper_launch(binding, monkeypatch):
    state = fake_time(monkeypatch)
    fake_os = types.SimpleNamespace(**vars(os))
    def slow_sync(fd):
        os.fsync(fd)
        state.now += 5.01
    fake_os.fsync = slow_sync
    monkeypatch.setattr(command, 'os', fake_os)
    status = {'failed': False}
    with pytest.raises(TimeoutError):
        with command._observation_fence(binding, 130, [], status=status):
            pytest.fail('Claim durability cannot renew the observation deadline')
    assert read_lock(binding) and status['failed']


def test_changed_nonempty_claim_is_preserved_without_overwrite(binding, monkeypatch):
    fake_time(monkeypatch)
    audit, status = [], {'failed': False}
    with pytest.raises(RuntimeError, match='claim changed'):
        with command._observation_fence(binding, 110, audit, status=status):
            Path(binding['path']).write_bytes(b'unknown other retained claim')
            audit.append(clean_audit(reaped=False))
    assert status['failed']
    assert read_lock(binding) == b'unknown other retained claim'


def test_later_clean_audit_cannot_erase_earlier_incomplete_helper(binding, monkeypatch):
    fake_time(monkeypatch)
    audit = []
    with pytest.raises(RuntimeError, match='cleanup is unknown'):
        with command._observation_fence(binding, 110, audit):
            audit.extend([clean_audit(reaped=False), clean_audit()])
    assert read_lock(binding)


def _crash_after_active_claim(connection, binding):
    import time

    command._ps_metadata = forbidden
    command.subprocess = types.SimpleNamespace(**vars(command.subprocess))
    command.subprocess.Popen = forbidden
    with command._observation_fence(binding, time.monotonic() + 5, []):
        connection.send(('active', read_lock(binding)))
        os._exit(73)  # Simulate loss of this harmless fixture owner, without finally.


def test_process_exit_leaves_durable_active_and_never_authorizes_new_helper(binding):
    import multiprocessing
    import time

    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_crash_after_active_claim, args=(sender, binding))
    process.start()
    sender.close()
    try:
        assert receiver.poll(5), 'Harmless worker must publish ACTIVE before it exits'
        phase, retained = receiver.recv()
        assert phase == 'active' and retained
        process.join(timeout=3)
        assert not process.is_alive() and process.exitcode == 73
        assert read_lock(binding) == retained
        fd = hold_lock(binding)  # Kernel released the dead owner's flock.
        release_lock(fd)
        with pytest.raises(RuntimeError, match='ACTIVE or unknown'):
            with command._observation_fence(binding, time.monotonic() + 1, []):
                pytest.fail('A durable ACTIVE claim cannot be reclaimed after a crash')
        assert read_lock(binding) == retained
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        receiver.close()
        if not process.is_alive():
            process.close()


@pytest.mark.parametrize('injection', ['before_go', 'admitting_persist'])
def test_final_admission_rechecks_poison_created_by_callback(locked_lifecycle, binding, injection):
    state = locked_lifecycle
    poison = b'ACTIVE unknown helper introduced after initial preflight'
    callbacks = []
    def before_go(deadline):
        callbacks.append(('before_go', deadline))
        if injection == 'before_go':
            Path(binding['path']).write_bytes(poison)
    def persist(snapshot):
        state.persist(snapshot)
        if snapshot['phase'] == 'admitting' and injection == 'admitting_persist':
            callbacks.append(('admitting_persist', snapshot['deadline_monotonic']))
            Path(binding['path']).write_bytes(poison)
    report = state.run(observation_lock=binding, before_go=before_go, persist=persist)
    assert callbacks and len(state.launches) == 1  # Held supervisor exists, payload does not.
    assert not state.admitted and ('stdin', b'G') not in state.events
    assert not report['launch_authorized'] and report['child'] is None
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified']
    assert not report['passed'] and report['observation_coordination']['failed']
    assert not state.signals and not state.census_deadlines
    assert report['deadline_monotonic'] == state.deadline
    assert read_lock(binding) == poison
    saved = json.loads((state.control / 'receipt.json').read_text())
    assert saved == report and saved['controller_finished'] is True


def test_final_admission_waits_for_clean_holder_and_writes_g_under_same_flock(
        locked_lifecycle, binding, monkeypatch):
    state = locked_lifecycle
    holder = {'fd': None, 'released': False, 'entry': None, 'g_at': None}
    def before_go(deadline):
        holder['entry'] = state.now
        holder['fd'] = hold_lock(binding)  # User callback runs outside the fence.
        Path(binding['path']).write_bytes(b'ACTIVE synthetic healthy observer')
        original_write = state.process.stdin.write
        def write(value):
            assert holder['released'] and read_lock(binding) == b''
            assert holder['entry'] < state.now < deadline
            contender = os.open(binding['path'], os.O_RDWR | os.O_CLOEXEC)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)
            holder['g_at'] = state.now
            return original_write(value)
        state.process.stdin.write = write
    def sleep(seconds):
        state.now += seconds
        if (holder['fd'] is not None and not holder['released']
                and state.now >= holder['entry'] + .075):
            Path(binding['path']).write_bytes(b'')
            release_lock(holder['fd'])
            holder.update(fd=None, released=True)
    monkeypatch.setattr(command.time, 'sleep', sleep)
    try:
        report = state.run(observation_lock=binding, before_go=before_go)
        assert report['passed'] and report['cleanup_verified']
        assert holder['released'] and holder['g_at'] is not None
        assert state.admitted and state.events.count(('stdin', b'G')) == 1
        assert report['observation_coordination'] == {'failed': False}
        assert not state.signals and read_lock(binding) == b''
    finally:
        if holder['fd'] is not None:
            release_lock(holder['fd'])


@pytest.mark.parametrize('deadline,admission_expiry', [(100.08, 100.08), (130, 105)])
def test_final_admission_contention_never_renews_original_budget(
        locked_lifecycle, binding, deadline, admission_expiry):
    state = locked_lifecycle
    state.deadline = deadline
    holder = {'fd': None, 'reap_at': None}
    def before_go(given_deadline):
        assert given_deadline == deadline
        holder['fd'] = hold_lock(binding)
        Path(binding['path']).write_bytes(b'ACTIVE synthetic healthy observer')
        original_wait = state.process.wait
        def wait(timeout):
            # Reaping a never-admitted supervisor begins only after the final
            # admission gate expires. Release the synthetic observer during
            # cleanup, without renewing the payload's admission opportunity.
            holder['reap_at'] = state.now
            assert not state.admitted and state.now == pytest.approx(admission_expiry)
            Path(binding['path']).write_bytes(b'')
            release_lock(holder['fd'])
            holder['fd'] = None
            return original_wait(timeout)
        state.process.wait = wait
    try:
        report = state.run(observation_lock=binding, before_go=before_go)
        assert holder['reap_at'] == pytest.approx(admission_expiry)
        assert not state.admitted and ('stdin', b'G') not in state.events
        assert not report['launch_authorized'] and report['child'] is None
        assert report['timed_out'] and not report['passed'] and not report['cleanup_verified']
        assert report['phase'] == 'cleanup_unknown' and report['observation_coordination']['failed']
        assert report['deadline_monotonic'] == deadline
        assert report['cleanup_deadline_monotonic'] == deadline + 26
        assert not state.signals and read_lock(binding) == b''
        assert json.loads((state.control / 'receipt.json').read_text()) == report
    finally:
        if holder['fd'] is not None:
            release_lock(holder['fd'])


def test_final_admission_rechecks_poison_from_admitting_cancellation_callback(
        locked_lifecycle, binding):
    state = locked_lifecycle
    frontier = {'admitting': False, 'injected': False}
    poison = b'ACTIVE introduced by cancellation callback after admitting persistence'
    def persist(snapshot):
        state.persist(snapshot)
        if snapshot['phase'] == 'admitting':
            frontier['admitting'] = True
    def cancelled():
        if frontier['admitting'] and not frontier['injected']:
            fd = hold_lock(binding)  # The user callback must run outside the fence.
            release_lock(fd)
            Path(binding['path']).write_bytes(poison)
            frontier['injected'] = True
        return False
    report = state.run(observation_lock=binding, persist=persist, cancelled=cancelled)
    assert frontier['injected'] and len(state.launches) == 1
    assert not state.admitted and ('stdin', b'G') not in state.events
    assert not report['launch_authorized'] and report['child'] is None
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified']
    assert report['observation_coordination']['failed'] and not report['passed']
    assert not state.signals and not state.census_deadlines
    assert read_lock(binding) == poison
    assert json.loads((state.control / 'receipt.json').read_text()) == report


@pytest.mark.parametrize('field', ['parent', 'group', 'status'])
def test_final_admission_rechecks_supervisor_relationship_after_persistence(
        locked_lifecycle, binding, monkeypatch, field):
    state = locked_lifecycle
    frontier = {'admitting': False, 'checked_inside': False}
    original_identity = command.native_processes.identity
    def persist(snapshot):
        state.persist(snapshot)
        if snapshot['phase'] == 'admitting':
            frontier['admitting'] = True
    def identity(pid):
        current = original_identity(pid)
        if pid == OWNER_PID and frontier['admitting'] and not frontier['checked_inside']:
            contender = os.open(binding['path'], os.O_RDWR | os.O_CLOEXEC)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)
            frontier['checked_inside'] = True
            return {**current, field: 5 if field == 'status' else current[field] + 1}
        return current
    monkeypatch.setattr(command.native_processes, 'identity', identity)
    report = state.run(observation_lock=binding, persist=persist)
    assert frontier['checked_inside'] and len(state.launches) == 1
    assert not state.admitted and ('stdin', b'G') not in state.events
    assert not report['launch_authorized'] and report['child'] is None and not report['passed']
    assert any('changed at final admission' in error for error in report['errors'])
    assert not state.signals and read_lock(binding) == b''
    assert json.loads((state.control / 'receipt.json').read_text()) == report
