"""Own one independently reviewed command running with normal user access.

This records process lifecycle evidence; it does not isolate the command from
the user's files, services or credentials. Observation covers birth-checked
lineage and the held process group, not unobserved detached descendants.
No function in this module retries a command.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import threading
import time

from . import native_processes

HERE = Path(__file__).resolve().parent
SUPERVISOR = HERE / 'owned_host_supervisor.py'
BACKEND = 'host-owned-v1'
CLEANUP_RESERVE_SECONDS = 26
CLEANUP_SECONDS = 12
REAP_SECONDS = 3
PIPE_SECONDS = 2
MAX_STREAM_BYTES = 128 * 1024 * 1024
MAX_OBSERVED = 16384
OBSERVATION_SECONDS = 5
MAX_OBSERVATION_STATE = 4096
_RETAINED_PROCESSES = []  # Preserve direct ownership if bounded cleanup cannot finish.
_RETAINED_OBSERVATION_LOCKS = []  # Process-lifetime hold when durable poison cannot be retained.


def _error(errors, label, error):
    errors.append(label + ': ' + type(error).__name__ + ': ' + str(error))


def _close(handle, errors, label):
    if handle is not None and not handle.closed:
        try:
            handle.close()
        except BaseException as error:
            _error(errors, label, error)


def _same(current, original):
    return native_processes.same(original, current)


def _observation_binding(value):
    """Validate data only; a poisoned binding must remain readable as evidence."""
    fields = {'path', 'device', 'inode', 'uid', 'mode', 'nlink'}
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value['path'], str) or not value['path']
            or '\x00' in value['path']
            or any(type(value[key]) is not int for key in fields - {'path'})
            or value['device'] < 0 or value['inode'] <= 0 or value['uid'] < 0
            or value['mode'] != stat.S_IFREG | 0o600 or value['nlink'] != 1):
        raise ValueError('Expected an exact private observation lock binding')
    path = Path(value['path'])
    if not path.is_absolute() or str(path) != value['path']:
        raise ValueError('Observation lock path must be absolute and canonical')
    return dict(value)


def _check_observation_file(binding, descriptor=None):
    path = Path(binding['path'])
    if path.resolve(strict=True) != path:
        raise RuntimeError('Observation lock path changed or contains a symlink')
    parent = path.parent.lstat()
    if parent.st_mode != stat.S_IFDIR | 0o700 or parent.st_uid != os.geteuid():
        raise RuntimeError('Observation lock requires its exact private parent directory')
    info = path.lstat()
    actual = {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
              'uid': info.st_uid, 'mode': info.st_mode, 'nlink': info.st_nlink}
    if actual != binding or info.st_uid != os.geteuid():
        raise RuntimeError('Observation lock file identity or permissions changed')
    if descriptor is not None:
        opened = os.fstat(descriptor)
        if any(getattr(opened, name) != getattr(info, name) for name in
               ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink')):
            raise RuntimeError('Observation lock descriptor no longer names its bound file')
        return opened
    return info


def _observation_time(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('Original observation budget expired')


def _observation_identity(deadline):
    def read(pid):
        _observation_time(deadline)
        value = native_processes.identity(pid)
        _observation_time(deadline)
        return value
    return read


def _check_observation_claim(descriptor, claim):
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.read(descriptor, MAX_OBSERVATION_STATE + 1) != claim:
        raise RuntimeError('Active observation coordination claim changed')


def _poison_empty_observation(binding, descriptor):
    """Restore a hold only on the same unexpectedly emptied coordination inode."""
    if _check_observation_file(binding, descriptor).st_size != 0:
        return
    poison = json.dumps({'format': 'owned-observation-poison-v1',
        'observation_lock': binding, 'controller_pid': os.getpid(),
        'helper_cleanup_verified': False}, sort_keys=True, separators=(',', ':')).encode()
    if len(poison) > MAX_OBSERVATION_STATE:
        raise ValueError('Observation poison record exceeds its bound')
    os.lseek(descriptor, 0, os.SEEK_SET)
    written = os.write(descriptor, poison)
    os.fsync(descriptor)  # Even a partial nonempty write must retain its hold.
    if written != len(poison):
        raise OSError('Incomplete observation poison record')
    _check_observation_file(binding, descriptor)
    _check_observation_claim(descriptor, poison)


@contextmanager
def _observation_fence(binding, deadline, audit=None, *, status=None):
    """Serialize complete census decisions, retaining unknown helper cleanup.

    An empty, precreated inode is CLEAN. Before a helper can be spawned, ACTIVE
    is written and synced without truncating that inode. Only fresh direct
    reap/EOF/closed-handle evidence may clear it. A crash or unknown cleanup
    therefore leaves a durable hold; this API never reclaims or resets it.
    deadline=None is solely a nonblocking CLEAN check without any observation.
    """
    if binding is None:
        yield deadline
        return
    binding = _observation_binding(binding)
    if deadline is None and audit is not None:
        raise ValueError('An observation requires its original deadline')
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('A finite original observation deadline is required')
    until = None if deadline is None else min(deadline, time.monotonic() + OBSERVATION_SECONDS)
    status = {} if status is None else status
    status.setdefault('failed', False)
    descriptor = None
    locked = entered = False
    start = len(audit) if audit is not None else None
    try:
        _check_observation_file(binding)
        descriptor = os.open(binding['path'], os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        os.set_inheritable(descriptor, False)
        while True:
            _check_observation_file(binding, descriptor)
            if until is not None:
                _observation_time(until)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if until is None:
                    raise RuntimeError('Observation coordination is busy; completion remains held')
                available = until - time.monotonic()
                if available <= 0:
                    raise TimeoutError('Original observation budget expired waiting for coordination')
                time.sleep(min(.025, available))
        if _check_observation_file(binding, descriptor).st_size != 0:
            raise RuntimeError('Observation coordination retains ACTIVE or unknown helper cleanup')
        if until is not None:
            _observation_time(until)
        if audit is not None:
            claim = json.dumps({'format': 'owned-observation-active-v1',
                'observation_lock': binding, 'controller_pid': os.getpid(),
                'deadline_monotonic': until}, sort_keys=True, separators=(',', ':')).encode()
            if len(claim) > MAX_OBSERVATION_STATE:
                raise ValueError('Observation coordination claim exceeds its bound')
            # The inode was empty. Never truncate before writing ACTIVE: a
            # partial write must itself remain a durable nonempty hold.
            if os.write(descriptor, claim) != len(claim):
                raise OSError('Incomplete observation coordination claim')
            os.fsync(descriptor)
            _check_observation_file(binding, descriptor)
            _check_observation_claim(descriptor, claim)
            _observation_time(until)
        entered = True
        try:
            yield until
        finally:
            fresh = audit[start:] if audit is not None else []
            helper_clean = bool(fresh) and all(all(item.get(key) is True for key in
                ('reaped', 'pipes_eof', 'stdout_closed', 'stderr_closed')) for item in fresh)
            try:
                _check_observation_file(binding, descriptor)
                if audit is not None:
                    _check_observation_claim(descriptor, claim)
                    if not helper_clean:
                        raise RuntimeError('Observation helper cleanup is unknown; coordination remains ACTIVE')
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                elif os.fstat(descriptor).st_size != 0:
                    raise RuntimeError('Observation coordination changed during its CLEAN check')
                _check_observation_file(binding, descriptor)
            except BaseException:
                status['failed'] = True
                if audit is not None and not helper_clean:
                    try:
                        _poison_empty_observation(binding, descriptor)
                    except BaseException as error:
                        held = False
                        try:
                            held = _check_observation_file(binding, descriptor).st_size == 0
                        except BaseException:
                            pass  # A substituted path must never be changed or adopted.
                        if held:
                            _RETAINED_OBSERVATION_LOCKS.append(descriptor)
                            descriptor = None
                        protection = ('; exact locked descriptor retained for process lifetime only'
                                      if held else '')
                        raise RuntimeError('Observation helper cleanup remains unknown; durable POISON '
                            'could not be verified' + protection + ': ' + type(error).__name__ + ': '
                            + str(error)) from error
                raise
    except BaseException:
        if not entered:
            status['failed'] = True
        raise
    finally:
        if descriptor is not None:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except BaseException:
                status['failed'] = True
                raise
            finally:
                try:
                    os.close(descriptor)
                except BaseException:
                    status['failed'] = True
                    raise


def select_owned(rows, leader, retained, identity_reader=None):
    """Use ps only for candidates; current kernel birth/UID establishes ownership."""
    identity_reader = identity_reader or native_processes.identity
    leader_now = identity_reader(leader['pid'])
    leader_live = _same(leader_now, leader)
    confirmed = {leader['pid']: leader_now} if leader_live else {}
    changed_birth = set()
    for pid, saved in retained.items():
        current = identity_reader(pid)
        if _same(current, saved):
            confirmed[pid] = current
        elif current is not None:
            changed_birth.add(pid)
    while True:
        added = False
        for pid, row in rows.items():
            if pid in confirmed or pid in changed_birth or pid == leader['pid']:
                continue
            if row['parent'] not in confirmed and not (leader_live and row['group'] == leader['pid']):
                continue
            current = identity_reader(pid)
            if current is None:
                continue
            parent = confirmed.get(current['parent'])
            parent_live = parent is not None and _same(identity_reader(parent['pid']), parent)
            group_live = current['group'] == leader['pid'] and _same(identity_reader(leader['pid']), leader)
            if parent_live or group_live:
                if current['uid'] != leader['uid']:
                    raise RuntimeError('Unexpected cross-UID owned candidate')
                confirmed[pid] = current
                added = True
        if not added:
            break
    return leader_live, {pid: item for pid, item in confirmed.items() if pid != leader['pid']}


def _ps_metadata():
    """Bind only the fixed system ps executable, including its real privilege."""
    info = os.lstat('/bin/ps')
    modes = {stat.S_IFREG | 0o755, stat.S_IFREG | stat.S_ISUID | 0o755}
    if info.st_mode not in modes or info.st_uid != 0 or info.st_gid != 0:
        raise RuntimeError('Unexpected trusted /bin/ps executable metadata')
    return {'path': '/bin/ps', 'device': info.st_dev, 'inode': info.st_ino,
            'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid,
            'expected_effective_uid': info.st_uid if info.st_mode & stat.S_ISUID else os.geteuid()}


def _observe(deadline, audit, *, cleanup_deadline=None):
    """Bound our exact ps child; unavailable birth data is never a live owner."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Census deadline expired')
    cleanup_deadline = deadline + 1 if cleanup_deadline is None else cleanup_deadline
    record = {'owner': None, 'exit_code': None, 'reaped': False, 'pipes_eof': False,
              'stdout_closed': False, 'stderr_closed': False,
              'interrupted': False, 'errors': [], 'identity_diagnostic': None,
              'ownership_basis': None, 'output_valid': False, 'executable_metadata': None}
    process = None
    output = b''
    try:
        metadata = _ps_metadata()
        record['executable_metadata'] = metadata
        process = subprocess.Popen(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid='],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, start_new_session=True)
        record['pid'] = process.pid
        try:
            owner = native_processes.identity(process.pid)
        except PermissionError as error:
            owner = None
            record['identity_diagnostic'] = {'type': type(error).__name__,
                'errno': error.errno, 'message': str(error)}
        if owner is None:
            if record['identity_diagnostic'] is None:
                record['identity_diagnostic'] = {'type': 'identity_unavailable',
                    'errno': None, 'message': 'The direct child had no readable birth identity'}
        else:
            if (owner['pid'] != process.pid or owner['uid'] != metadata['expected_effective_uid']
                    or owner['parent'] != os.getpid() or owner['group'] != process.pid):
                raise RuntimeError('Census child identity mismatch')
            record['owner'] = owner
        budget = min(5, deadline - time.monotonic())
        if budget <= 0:
            raise TimeoutError('Census deadline expired before communication')
        try:
            output, error_output = process.communicate(timeout=budget)
            record['pipes_eof'] = True
            record['stderr_bytes'] = len(error_output)
            if error_output:
                record['errors'].append('Census command wrote stderr')
            if time.monotonic() > deadline:
                record['errors'].append('Census command completed after its original deadline')
        except subprocess.TimeoutExpired:
            record['errors'].append('Census command deadline expired')
        if process.returncode not in (None, 0):
            record['errors'].append('Census command exit ' + str(process.returncode))
    except BaseException as error:
        _error(record['errors'], 'census', error)
    finally:
        if process is not None and process.returncode is None:
            # A retained Popen handle can be waited independently of a PID
            # observation. Sending any signal still requires its verified birth.
            try:
                owner = record['owner']
                if (time.monotonic() < cleanup_deadline and owner is not None
                        and _same(native_processes.identity(process.pid), owner)):
                    if time.monotonic() >= cleanup_deadline:
                        raise TimeoutError('Original cleanup deadline expired before census signal')
                    record['interrupted'] = True
                    os.kill(process.pid, signal.SIGINT)
            except BaseException as error:
                _error(record['errors'], 'census signal ownership', error)
            try:
                remaining_cleanup = cleanup_deadline - time.monotonic()
                if remaining_cleanup <= 0:
                    raise TimeoutError('Original cleanup deadline expired during census cleanup')
                output, error_output = process.communicate(timeout=min(1, remaining_cleanup))
                record['pipes_eof'] = True
                record['stderr_bytes'] = len(error_output)
                if error_output:
                    record['errors'].append('Census command wrote stderr during cleanup')
            except BaseException as error:
                _error(record['errors'], 'census cleanup', error)
        if process is not None:
            record['exit_code'] = process.returncode
            record['reaped'] = process.returncode is not None
            _close(process.stdout, record['errors'], 'census stdout close')
            _close(process.stderr, record['errors'], 'census stderr close')
            record['stdout_closed'] = process.stdout is not None and process.stdout.closed
            record['stderr_closed'] = process.stderr is not None and process.stderr.closed
            if not record['reaped']:
                _RETAINED_PROCESSES.append(process)
        record['stdout_bytes'] = len(output)
        audit.append(record)
    if (record['errors'] or record['interrupted'] or not record['reaped']
            or not record['pipes_eof'] or record['exit_code'] != 0):
        raise RuntimeError('Census command did not finish cleanly; see census_commands')
    try:
        if _ps_metadata() != metadata:
            raise ValueError('Trusted /bin/ps metadata changed during observation')
        if len(output) > 8 * 1024 * 1024:
            raise ValueError('Census output exceeded its bounded size')
        rows = {}
        for line in output.decode('ascii').splitlines():
            _observation_time(deadline)
            fields = line.split()
            if len(fields) != 4:
                raise ValueError('Malformed census row')
            pid, parent, group, uid = map(int, fields)
            if pid <= 0 or min(parent, group, uid) < 0 or pid in rows:
                raise ValueError('Invalid or duplicate census PID/identity field')
            rows[pid] = {'parent': parent, 'group': group, 'uid': uid}
        expected_child = {'parent': os.getpid(), 'group': process.pid,
                          'uid': metadata['expected_effective_uid']}
        expected_controller = {'parent': os.getppid(), 'group': os.getpgrp(), 'uid': os.geteuid()}
        if not rows or rows.get(process.pid) != expected_child or rows.get(os.getpid()) != expected_controller:
            raise ValueError('Expected direct ps/controller rows are missing or disagree')
    except (ValueError, UnicodeError, OSError, RuntimeError) as error:
        _error(record['errors'], 'census output', error)
        raise RuntimeError('Census output is incomplete or invalid; see census_commands') from error
    record['output_valid'] = True
    record['ownership_basis'] = ('verified-birth-and-direct-reap' if record['owner'] is not None
                                 else 'direct-child-spawn-and-reap; birth identity unavailable')
    return rows


def _drain(pipe, destination, state, stopped):
    try:
        while not stopped.is_set():
            if not select.select([pipe.fileno()], [], [], .1)[0]:
                continue
            block = os.read(pipe.fileno(), 65536)
            if not block:
                state['eof'] = True
                return
            state['bytes'] += len(block)
            if state['error'] is not None:
                continue  # Still drain a failed output stream so the payload can exit.
            if state['bytes'] > MAX_STREAM_BYTES:
                state['error'] = 'Retained output exceeded 128 MiB stream bound'
                continue
            try:
                written = destination.write(block)
                if written != len(block):
                    raise OSError('Short output write')
                state['retained_bytes'] += written
            except BaseException as error:
                state['error'] = type(error).__name__ + ': ' + str(error)
    except BaseException as error:
        state['error'] = state['error'] or type(error).__name__ + ': ' + str(error)


def _status(path):
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise RuntimeError('Supervisor status exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'exit_code'} or type(value['exit_code']) is not int:
        raise RuntimeError('Invalid supervisor status')
    return value


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.writing')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _copy(value):
    return json.loads(json.dumps(value))


def _cancelled(control, callback):
    try:
        info = (control / 'cancel').lstat()
    except FileNotFoundError:
        marked = False
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size:
            raise RuntimeError('Invalid owned cancellation marker')
        marked = True
    requested = callback() if callback is not None else False
    if type(requested) is not bool:
        raise ValueError('Cancellation callback must return a bool')
    return marked or requested


def request_cancel(control):
    """Ask the retained controller to stop; never signal or relaunch a PID."""
    control = Path(control).resolve(strict=True)
    info = control.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('Cancellation requires an owned control directory')
    try:
        descriptor = os.open(control / 'cancel', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _cancelled(control, None)
    else:
        os.close(descriptor)


def run_owned(argv, cwd, environment, control, *, deadline, cleanup_deadline=None,
              before_go=None, cancelled=None, persist=None, observation_lock=None) -> dict:
    """Run literal argv once under its original absolute monotonic deadline.

    persist(snapshot) records prepared, verified-owner, admission, progress and
    terminal states. Verified ownership must be saved before G. Callback errors
    stop admission or remain sticky failures after admission. Host-mode control
    files are cooperative records, not protection against same-user commands.
    Nested observers may explicitly share one precreated private observation
    lock binding. The lock covers census and ownership classification only;
    unknown ancillary cleanup leaves its durable ACTIVE claim unreclaimable.
    """
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(value, str) or not value or '\x00' in value for value in argv)):
        raise ValueError('A nonempty literal argv is required')
    for name, callback in [('before_go', before_go), ('cancelled', cancelled), ('persist', persist)]:
        if callback is not None and not callable(callback):
            raise ValueError(name + ' must be a callable or None')
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError('A finite original monotonic deadline is required')
    if cleanup_deadline is not None and (
            type(cleanup_deadline) not in (int, float) or not math.isfinite(cleanup_deadline)
            or cleanup_deadline < deadline):
        raise ValueError('Cleanup deadline must bound the original command deadline')
    cleanup_deadline = min(deadline + CLEANUP_RESERVE_SECONDS,
                           deadline + CLEANUP_RESERVE_SECONDS if cleanup_deadline is None else cleanup_deadline)
    if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not key or '=' in key or '\x00' in key
            or not isinstance(value, str) or '\x00' in value for key, value in environment.items()):
        raise ValueError('An explicit string environment is required')
    if not SUPERVISOR.is_file():
        raise ValueError('Maintained supervisor sibling is required')
    if observation_lock is not None:
        observation_lock = _observation_binding(observation_lock)
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('Command cwd must be an existing directory')
    control = Path(control).absolute()
    control.parent.resolve(strict=True)
    control.mkdir(mode=0o700, exist_ok=False)
    control = control.resolve(strict=True)
    errors, owned, census_commands = [], {}, []
    report = {'format': 'owned-host-command-v1', 'backend': BACKEND,
        'phase': 'prepared', 'passed': False, 'cleanup_verified': False,
        'controller': native_processes.identity(os.getpid()),
        'controller_finished': False,
        'cancel_requested': False, 'timed_out': False,
        'deadline_monotonic': deadline, 'cleanup_deadline_monotonic': cleanup_deadline,
        'boundary_rechecked': False,
        'argv': list(argv), 'cwd': str(cwd), 'control': str(control),
        'cleanup_reserve_seconds': CLEANUP_RESERVE_SECONDS,
        'started_at': time.time(), 'owner': None, 'owner_exit': None, 'owner_reaped': False,
        'owner_absent': False, 'group_empty': False, 'child': None, 'owned_remaining': {},
        'errors': errors, 'census_commands': census_commands,
        'environment_sha256': hashlib.sha256(json.dumps(dict(environment), sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'limitation': 'Observed owners only; does not exclude unobserved detached escapes.'}
    coordination = None
    if observation_lock is not None:
        report['observation_lock'] = observation_lock
        coordination = report['observation_coordination'] = {'failed': False}
    status_path, release_path = control / 'status.json', control / 'release'
    process = owner = None
    logs, readers, streams = [], [], []
    stopped = threading.Event()
    launched = False
    spawn_attempted = False
    cleaning = False
    released = False
    started = time.monotonic()
    last_checkpoint = started

    def checkpoint(phase, *, terminal=False, notification=None):
        nonlocal last_checkpoint
        report['phase'] = phase
        report['launch_authorized'] = launched
        report['observed'] = {str(pid): item for pid, item in owned.items()}
        report['checkpoint_at'] = time.time()
        report['checkpoint_monotonic'] = time.monotonic()
        if terminal:
            snapshot = _copy(report)
        else:
            # Keep progress small; preserve every uncertain ancillary child.
            snapshot = _copy({key: value for key, value in report.items() if key != 'census_commands'})
            snapshot['census_count'] = len(census_commands)
            snapshot['census_commands'] = _copy([item for item in census_commands[:-1]
                if item['errors'] or not item['reaped'] or not item['pipes_eof']] + census_commands[-1:])
        if notification is not None:
            notification['snapshot'] = snapshot
        _atomic_json(control / 'progress.json', snapshot)
        if persist is not None:
            callback_snapshot = _copy(snapshot)
            if notification is not None:
                notification['callback_attempted'] = True
            persist(callback_snapshot)
        last_checkpoint = time.monotonic()

    def check_cancel():
        if _cancelled(control, cancelled):
            report['cancel_requested'] = True
            raise InterruptedError('Owned command cancellation requested')

    def remaining(limit):
        available = report['cleanup_deadline_monotonic'] - time.monotonic()
        if available <= 0:
            raise TimeoutError('Original cleanup deadline expired')
        return min(limit, available)

    def observe(until):
        with _observation_fence(observation_lock, until, census_commands,
                                status=coordination) as observation_deadline:
            rows = _observe(observation_deadline, census_commands,
                            cleanup_deadline=report['cleanup_deadline_monotonic'])
            reader = _observation_identity(observation_deadline) if observation_lock is not None else None
            live, members = select_owned(rows, owner, owned, identity_reader=reader)
            previous_count = len(owned)
            for pid, item in members.items():
                owned.setdefault(pid, item)
            if len(owned) > MAX_OBSERVED:
                raise RuntimeError('Owned process evidence bound exceeded')
            group_members = {pid: row for pid, row in rows.items()
                             if row['group'] == owner['pid'] and pid != owner['pid']}
            report['owned_remaining'] = {str(pid): item for pid, item in members.items()}
            if observation_lock is not None:
                _observation_time(observation_deadline)
        if len(owned) != previous_count or time.monotonic() - last_checkpoint >= 1:
            try:
                checkpoint('running' if not errors else 'cleanup_unknown')
            except BaseException as error:
                if not cleaning:
                    raise
                _error(errors, 'cleanup persistence', error)
        return live, members, group_members
    try:
        if report['controller'] is None:
            raise RuntimeError('No verified controller identity')
        checkpoint('prepared')
        check_cancel()
        with _observation_fence(observation_lock, deadline, status=coordination):
            pass  # Known poison must hold before any supervisor/payload launch.
        if time.monotonic() >= deadline:
            raise TimeoutError('Command deadline expired before supervisor launch')
        (control / 'command.json').write_text(json.dumps({
            'argv': list(argv), 'cwd': str(cwd), 'deadline_monotonic': deadline,
            'environment_sha256': report['environment_sha256']}, indent=2) + '\n')
        for name in ('stdout', 'stderr'):
            logs.append((control / (name + '.log')).open('xb', buffering=0))
        payload = {'argv': list(argv), 'cwd': str(cwd), 'status': str(status_path), 'release': str(release_path)}
        spawn_attempted = True
        process = subprocess.Popen([sys.executable, '-I', '-B', str(SUPERVISOR), json.dumps(payload)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(environment), start_new_session=True)
        owner = native_processes.identity(process.pid)
        if (owner is None or owner['uid'] != os.geteuid() or owner['group'] != process.pid
                or owner['parent'] != os.getpid()):
            raise RuntimeError('No verified supervisor birth/UID/group/parent')
        report['owner'] = owner
        for name, pipe, destination in zip(('stdout', 'stderr'), (process.stdout, process.stderr), logs):
            state = {'name': name, 'bytes': 0, 'retained_bytes': 0, 'eof': False, 'error': None}
            streams.append(state)
            reader = threading.Thread(target=_drain, args=(pipe, destination, state, stopped),
                                      name='host-owned-' + name, daemon=True)
            readers.append(reader)
            reader.start()
        _atomic_json(control / 'owner.json', owner)
        checkpoint('held')
        if before_go is not None:
            before_go(deadline)
            report['boundary_rechecked'] = True
        check_cancel()
        current_owner = native_processes.identity(process.pid)
        if not _same(current_owner, owner) or current_owner['status'] == 5:
            raise RuntimeError('Supervisor identity/liveness changed before admission')
        if time.monotonic() >= deadline:
            raise TimeoutError('Command deadline expired before admission')
        # Persist the admission frontier before any control write. The local
        # flag stays false until immediately before G, so pre-G failures have
        # positive no-payload-launch evidence even if the saved claim is held.
        report['launch_claimed'] = True
        checkpoint('admitting')
        check_cancel()
        # Callbacks above may have waited while another observer left a hold.
        # Keep the final CLEAN check and actual admission write in one fence;
        # no callback or persistence hook may run inside this critical region.
        with _observation_fence(observation_lock, deadline,
                                status=coordination) as admission_deadline:
            if observation_lock is not None:
                current_owner = _observation_identity(admission_deadline)(process.pid)
                if (not _same(current_owner, owner) or current_owner['status'] == 5
                        or any(current_owner[key] != owner[key] for key in ('parent', 'group'))):
                    raise RuntimeError('Supervisor identity/liveness changed at final admission')
                if _cancelled(control, None):  # Read the marker without invoking a callback.
                    report['cancel_requested'] = True
                    raise InterruptedError('Owned command cancellation requested at final admission')
                _observation_time(admission_deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError('Command deadline expired while saving admission')
            launched = True  # A partial control write must be treated as possibly admitted.
            process.stdin.write(b'G')
            process.stdin.flush()
            process.stdin.close()
        checkpoint('running')
        while True:
            check_cancel()
            if time.monotonic() >= deadline:
                raise TimeoutError('Original command deadline expired')
            live, members, group_members = observe(deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError('Original command deadline expired during census')
            check_cancel()
            if not live or native_processes.identity(owner['pid'])['status'] == 5:
                raise RuntimeError('Supervisor identity/liveness lost before child status')
            if any(state['error'] for state in streams):
                raise RuntimeError('Command output could not be retained completely')
            if status_path.exists():
                report['child'] = _status(status_path)
                check_cancel()
                # The adapter classifies a completed command's nonzero exit.
                # Lifecycle errors are reserved for runner/cleanup failures.
                # Completion may have raced the earlier census. Cleanup takes
                # a fresh post-status census before classifying any leftovers.
                break
            time.sleep(min(.15, max(0, deadline - time.monotonic())))
    except BaseException as error:
        if isinstance(error, TimeoutError):
            report['timed_out'] = True
        _error(errors, 'command', error)
        notification = {}
        try:
            checkpoint('cleanup_unknown', notification=notification)
        except BaseException as notification_error:
            _error(errors, 'failure notification persistence', notification_error)
            # A failed progress write must not hide the dirty state from the
            # caller. This is a nonterminal notice, never proof of publication
            # or cleanup. Do not retry a callback that was already invoked.
            if (persist is not None and 'snapshot' in notification
                    and not notification.get('callback_attempted')):
                try:
                    snapshot = _copy(notification['snapshot'])
                    snapshot['errors'] = list(errors)
                    persist(snapshot)
                except BaseException as callback_error:
                    _error(errors, 'failure notification callback', callback_error)
    finally:
        cleaning = True
        if process is not None:
            _close(process.stdin, errors, 'supervisor stdin close')
        cleanup_deadline = min(time.monotonic() + CLEANUP_SECONDS, report['cleanup_deadline_monotonic'])
        sent, empty = set(), 0
        if process is not None and owner is not None and launched:
            while time.monotonic() < cleanup_deadline:
                try:
                    live, members, group_members = observe(cleanup_deadline)
                    if not live:
                        errors.append('Supervisor identity lost during cleanup')
                        break
                    if status_path.exists() and report['child'] is None:
                        report['child'] = _status(status_path)
                    if members or group_members:
                        if not errors:
                            errors.append('Owned descendants required interruption after command exit')
                        empty = 0
                        for pid, saved in members.items():
                            key = (pid, *saved['birth'])
                            if key in sent:
                                continue
                            if _same(native_processes.identity(pid), saved):
                                try:
                                    if time.monotonic() >= cleanup_deadline:
                                        raise TimeoutError('Owned cleanup deadline expired before signal')
                                    os.kill(pid, signal.SIGINT)
                                    sent.add(key)
                                except ProcessLookupError:
                                    pass
                                except BaseException as error:
                                    _error(errors, 'owned SIGINT', error)
                    else:
                        empty += 1
                    if empty >= 2 and report['child'] is not None:
                        if not _same(native_processes.identity(owner['pid']), owner):
                            raise RuntimeError('Supervisor identity changed before release')
                        if time.monotonic() >= cleanup_deadline:
                            raise TimeoutError('Owned cleanup deadline expired before release')
                        with release_path.open('x') as stream:
                            stream.write('release verified owner\n')
                        released = True
                        try:
                            process.wait(timeout=remaining(REAP_SECONDS))
                        except BaseException as error:
                            _error(errors, 'supervisor reaping', error)
                        break
                except BaseException as error:
                    _error(errors, 'owned census cleanup', error)
                    break
                time.sleep(min(.1, max(0, cleanup_deadline - time.monotonic())))
            if not released:
                errors.append('Supervisor retained: no complete empty census plus child status')
        elif process is not None:
            # No G was authorized: EOF makes the waiting supervisor exit before spawn.
            try:
                process.wait(timeout=remaining(REAP_SECONDS))
            except BaseException as error:
                _error(errors, 'unstarted supervisor reaping', error)
        try:
            if process is not None:
                report['owner_exit'] = process.returncode
                report['owner_reaped'] = process.returncode is not None
                if report['owner_reaped']:
                    report['owner_absent'] = owner is not None and not _same(native_processes.identity(process.pid), owner)
                    with _observation_fence(observation_lock, time.monotonic() + remaining(5),
                                            census_commands, status=coordination) as observation_deadline:
                        rows = _observe(observation_deadline, census_commands,
                                        cleanup_deadline=report['cleanup_deadline_monotonic'])
                        report['group_empty'] = not any(row['group'] == process.pid for row in rows.values())
                        remaining_owners = {}
                        reader = (_observation_identity(observation_deadline) if observation_lock is not None
                                  else native_processes.identity)
                        for pid, saved in owned.items():
                            current = reader(pid)
                            if _same(current, saved):
                                remaining_owners[str(pid)] = current
                        report['owned_remaining'] = remaining_owners
                        if observation_lock is not None:
                            _observation_time(observation_deadline)
                else:
                    _RETAINED_PROCESSES.append(process)
            else:
                # A failed Popen boundary is ambiguous; absence of a returned
                # handle must not be converted to positive no-launch evidence.
                report['no_launch'] = not spawn_attempted
        except BaseException as error:
            _error(errors, 'final ownership observation', error)
        try:
            pipe_deadline = time.monotonic() + remaining(PIPE_SECONDS)
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=max(0, pipe_deadline - time.monotonic()))
            stopped.set()
            pipe_deadline = time.monotonic() + remaining(PIPE_SECONDS)
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=max(0, pipe_deadline - time.monotonic()))
        except BaseException as error:
            _error(errors, 'output reader cleanup', error)
        finally:
            stopped.set()
            joined = all(not reader.is_alive() for reader in readers)
            if process is not None:
                _close(process.stdout, errors, 'supervisor stdout close')
                _close(process.stderr, errors, 'supervisor stderr close')
            if joined:
                for log in logs:
                    _close(log, errors, 'command log close')
            else:
                errors.append('Output reader remains alive')
            report['streams'] = streams
            report['readers_joined'] = joined
            report['pipes_eof'] = len(streams) == 2 and all(state['eof'] for state in streams)
            report['stdin_closed'] = process is None or process.stdin.closed
            report['output_files_closed'] = all(log.closed for log in logs)
            for state in streams:
                if state['error']:
                    errors.append(state['name'] + ': ' + state['error'])
        report['observed'] = {str(pid): item for pid, item in owned.items()}
        report['signals'] = [{'pid': item[0], 'birth': list(item[1:]), 'signal': 'SIGINT'} for item in sorted(sent)]
        report['launch_authorized'] = launched
        report['release_written'] = released
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        ancillary_clean = all(row['reaped'] and row['pipes_eof'] for row in census_commands)
        report['cleanup_verified'] = bool(report.get('no_launch') or (
            report['owner_reaped'] and report['owner_absent'] and report['group_empty']
            and not report['owned_remaining'] and report['pipes_eof'] and report['readers_joined']
            and report['stdin_closed'] and report['output_files_closed'] and ancillary_clean))
        if coordination is not None and coordination['failed']:
            report['cleanup_verified'] = False
        try:
            check_cancel()
        except BaseException as error:
            _error(errors, 'final cancellation', error)
        report['passed'] = bool(not errors and launched and released and report['child']
            and report['child']['exit_code'] == 0 and report['owner_exit'] == 0 and report['cleanup_verified'])
        report['error'] = errors[0] if errors else None
        # Only this terminal handoff permits another controller to reconcile.
        # A cleanup_unknown progress phase alone never transfers ownership.
        report['controller_finished'] = True
        report['phase'] = 'complete' if report['cleanup_verified'] else 'cleanup_unknown'
        try:
            checkpoint(report['phase'], terminal=True)
        except BaseException as error:
            _error(errors, 'terminal persistence', error)
            report['error'] = errors[0]
            report['passed'] = False
        try:
            _atomic_json(control / 'receipt.json', report)
        except BaseException as error:
            _error(errors, 'receipt write', error)
            report['error'] = errors[0]
            report['passed'] = False
    return report


_BINDINGS = ('backend', 'argv', 'cwd', 'control', 'environment_sha256',
             'controller', 'deadline_monotonic', 'cleanup_deadline_monotonic')


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('backend') != BACKEND:
        raise ValueError('Expected a retained host-owned-v1 snapshot')
    for key in _BINDINGS:
        if key not in snapshot:
            raise ValueError('Missing retained ownership binding: ' + key)
    if 'observation_lock' in snapshot:
        _observation_binding(snapshot['observation_lock'])
        coordination = snapshot.get('observation_coordination')
        if (not isinstance(coordination, dict) or set(coordination) != {'failed'}
                or type(coordination['failed']) is not bool):
            raise ValueError('Missing retained observation coordination state')
    elif 'observation_coordination' in snapshot:
        raise ValueError('Observation coordination requires its exact lock binding')
    start, end = snapshot['deadline_monotonic'], snapshot['cleanup_deadline_monotonic']
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end)
            or not start <= end <= start + CLEANUP_RESERVE_SECONDS):
        raise ValueError('Invalid retained original deadlines')
    control = Path(snapshot['control'])
    if not control.is_absolute() or control.resolve(strict=True) != control:
        raise ValueError('Expected the exact retained control directory')
    return control


def _read_saved(path, reference):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_size > MAX_STREAM_BYTES):
        raise RuntimeError('Invalid retained host ownership record')
    value = json.loads(path.read_bytes())
    _validate_snapshot(value)
    if any(value[key] != reference[key] for key in _BINDINGS):
        raise RuntimeError('Retained host command bindings changed')
    if (('observation_lock' in value) != ('observation_lock' in reference)
            or value.get('observation_lock') != reference.get('observation_lock')):
        raise RuntimeError('Retained observation lock binding changed')
    if reference.get('owner') is not None and value.get('owner') != reference['owner']:
        raise RuntimeError('Retained supervisor identity changed')
    stored = value
    if stored.get('checkpoint_monotonic', 0) < reference.get('checkpoint_monotonic', 0):
        value = _copy(reference)
    # An older checkpoint must not erase a recorded cancellation, failure or
    # ownership discovery. Reconciliation checkpoints are ordered on the same
    # retained monotonic clock; neither path may renew its original deadline.
    value['errors'] = list(dict.fromkeys([*reference.get('errors', []), *stored.get('errors', [])]))
    for key in ('cancel_requested', 'timed_out'):
        value[key] = bool(reference.get(key) or stored.get(key))
    if 'observation_lock' in value:
        value['observation_coordination']['failed'] = bool(
            reference['observation_coordination']['failed'] or stored['observation_coordination']['failed'])
        if value['observation_coordination']['failed']:
            value['cleanup_verified'] = False
            value['passed'] = False
    retained = dict(reference.get('observed', {}))
    for pid, item in stored.get('observed', {}).items():
        if pid in retained and not _same(item, retained[pid]):
            raise RuntimeError('Retained owned PID birth changed')
        retained[pid] = item
    value['observed'] = retained
    for key in ('reconciliation_census', 'reconciliation_signals'):
        earlier, later = reference.get(key, []), stored.get(key, [])
        if earlier == later[:len(earlier)]:
            combined = later
        elif later == earlier[:len(later)]:
            combined = earlier
        else:
            combined = [*earlier, *later]
        if combined:
            value[key] = _copy(combined)
    if value['errors'] or value['cancel_requested'] or value['timed_out']:
        value['passed'] = False
    value['error'] = value['errors'][0] if value['errors'] else None
    return value


def verify_completed(snapshot, *, deadline=None):
    """Revalidate clean evidence; an optional deadline only tightens its wait."""
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('A finite optional verification deadline is required')
    control = _validate_snapshot(snapshot)
    saved = _read_saved(control / 'receipt.json', snapshot)
    if (control / 'reconciliation.json').exists():
        saved = _read_saved(control / 'reconciliation.json', saved)
    if saved.get('cleanup_verified') is not True or saved.get('phase') != 'complete':
        raise RuntimeError('No verified terminal host cleanup receipt')
    if 'observation_lock' in saved:
        # A legitimate outer observer may be finishing its own census. Wait
        # only within this receipt's still-live original cleanup allowance;
        # historical checks after expiry remain nonblocking, without a helper.
        original = saved['cleanup_deadline_monotonic']
        if deadline is not None:
            original = min(original, deadline)
        verification_deadline = original if time.monotonic() < original else None
        with _observation_fence(saved['observation_lock'], verification_deadline,
                                status=saved['observation_coordination']):
            pass
    if saved.get('no_launch') is True:
        if saved.get('owner') is not None or saved.get('launch_authorized'):
            raise RuntimeError('Contradictory no-launch ownership evidence')
        return saved
    if not all(saved.get(key) is True for key in (
            'owner_reaped', 'owner_absent', 'group_empty', 'pipes_eof',
            'readers_joined', 'stdin_closed', 'output_files_closed')):
        raise RuntimeError('Incomplete original pipe/reap/ownership evidence')
    if saved.get('owned_remaining'):
        raise RuntimeError('Retained command still reports owned descendants')
    owner = saved.get('owner')
    if owner is None or _same(native_processes.identity(owner['pid']), owner):
        raise RuntimeError('Retained supervisor is not verified absent')
    for item in saved.get('observed', {}).values():
        if _same(native_processes.identity(item['pid']), item):
            raise RuntimeError('A retained owned process is still present')
    # Original group/EOF/reap proof remains in the receipt. Revalidation reads
    # exact retained kernel identities only; it creates no new helper process
    # whose lifecycle would itself need another durable cleanup obligation.
    return saved


def reconcile_owned(snapshot, *, deadline, persist=None, cancel=False):
    """Observe or stop retained work without replay or a renewed cleanup clock.

    A live controller receives its existing cancellation marker. If that
    controller has disappeared or returned an unknown-cleanup receipt, only
    birth-verified owned descendants may receive SIGINT. Losing the original
    controller also loses its direct reap/pipe proof: observing no processes
    after recovery never upgrades that history to a clean receipt.
    """
    control = _validate_snapshot(snapshot)
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError('A finite reconciliation deadline is required')
    if type(cancel) is not bool or (persist is not None and not callable(persist)):
        raise ValueError('Invalid reconciliation callbacks or cancellation')
    result = _copy(snapshot)
    for path in (control / 'progress.json', control / 'receipt.json', control / 'reconciliation.json'):
        if path.exists():
            result = _read_saved(path, result)
    if result.get('cleanup_verified') is True:
        return verify_completed(result, deadline=deadline)
    until = min(deadline, result['cleanup_deadline_monotonic'])
    errors = result.setdefault('errors', [])
    result['passed'] = False
    result['reconciliation_deadline_monotonic'] = until

    def publish(*, advance=True):
        result['error'] = errors[0] if errors else None
        if advance:
            result['checkpoint_at'] = time.time()
            result['checkpoint_monotonic'] = time.monotonic()
        _atomic_json(control / 'reconciliation.json', result)
        if persist is not None:
            persist(_copy(result))

    try:
        if time.monotonic() >= until:
            raise TimeoutError('Original cleanup deadline expired; no new stop allowance')
        controller = result['controller']
        controller_live = controller is not None and _same(
            native_processes.identity(controller['pid']), controller)
        if controller_live and result.get('controller_finished') is not True:
            if cancel:
                request_cancel(control)
                result['cancel_requested'] = True
            # The original controller remains responsible for pipes and reaping.
            # Do not become a concurrent owner or signal its payload independently.
            # Do not make a stale observation newer than a terminal checkpoint
            # that the still-live controller may be saving concurrently.
            publish(advance=False)
            return result
        result['phase'] = 'cleanup_unknown'
        result['cleanup_verified'] = False
        result['recovery_lost_original_io_proof'] = True
        result['owner_reaped'] = False
        result['pipes_eof'] = False
        note = 'Original direct-child reap/pipe proof is unavailable; recovery observations cannot replace it'
        if note not in errors:
            errors.append(note)
        owner = result.get('owner')
        if owner is None:
            raise RuntimeError('No verified saved supervisor identity; no signal is authorized')
        retained = {int(pid): item for pid, item in result.get('observed', {}).items()}
        audit = result.setdefault('reconciliation_census', [])
        signals = result.setdefault('reconciliation_signals', [])
        empty = 0
        while time.monotonic() < until:
            binding = result.get('observation_lock')
            with _observation_fence(binding, until, audit,
                                    status=result.get('observation_coordination')) as observation_deadline:
                rows = _observe(observation_deadline, audit, cleanup_deadline=until)
                reader = _observation_identity(observation_deadline) if binding is not None else None
                live, members = select_owned(rows, owner, retained, identity_reader=reader)
                for pid, item in members.items():
                    retained.setdefault(pid, item)
                if len(retained) > MAX_OBSERVED:
                    raise RuntimeError('Owned process evidence bound exceeded')
                result['observed'] = {str(pid): item for pid, item in retained.items()}
                result['owned_remaining'] = {str(pid): item for pid, item in members.items()}
                group = [pid for pid, item in rows.items()
                         if item['group'] == owner['pid'] and pid != owner['pid']]
                result['owner_absent'] = not live
                result['group_empty'] = not group and not live
                if binding is not None:
                    _observation_time(observation_deadline)
            publish()  # New ownership evidence must be durable before a signal.
            if not live and not members:
                result['observed_stopped'] = not group
                if group:
                    errors.append('Only an unverified reused group number remains; no signal is authorized')
                break
            if not cancel:
                break
            result['cancel_requested'] = True
            for pid, item in members.items():
                if time.monotonic() >= until:
                    raise TimeoutError('Original cleanup deadline expired before owned signal')
                if any(sent['pid'] == pid and sent['birth'] == item['birth'] for sent in signals):
                    continue
                if _same(native_processes.identity(pid), item):
                    if time.monotonic() >= until:
                        raise TimeoutError('Original cleanup deadline expired before owned signal')
                    try:
                        os.kill(pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    else:
                        signals.append({'pid': pid, 'birth': item['birth'], 'signal': 'SIGINT'})
            empty = empty + 1 if not members and not group else 0
            if empty >= 2 and live and (control / 'status.json').exists():
                result['child'] = _status(control / 'status.json')
                if not _same(native_processes.identity(owner['pid']), owner):
                    raise RuntimeError('Supervisor changed before recovered release')
                if time.monotonic() >= until:
                    raise TimeoutError('Original cleanup deadline expired before recovered release')
                try:
                    descriptor = os.open(control / 'release', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            time.sleep(min(.1, max(0, until - time.monotonic())))
    except BaseException as error:
        _error(errors, 'reconcile', error)
        result['phase'] = 'cleanup_unknown'
        result['cleanup_verified'] = False
    try:
        publish()
    except BaseException as error:
        _error(errors, 'reconciliation persistence', error)
        result['error'] = errors[0]
        result['phase'] = 'cleanup_unknown'
        result['cleanup_verified'] = False
    return result
