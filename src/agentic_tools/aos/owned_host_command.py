"""Own one independently reviewed command running with normal user access.

This records process lifecycle evidence; it does not isolate the command from
the user's files, services or credentials. Observation covers birth-checked
lineage and the held process group, not unobserved detached descendants.
No function in this module retries a command.
"""
from __future__ import annotations

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
_RETAINED_PROCESSES = []  # Preserve direct ownership if bounded cleanup cannot finish.


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
              before_go=None, cancelled=None, persist=None) -> dict:
    """Run literal argv once under its original absolute monotonic deadline.

    persist(snapshot) records prepared, verified-owner, admission, progress and
    terminal states. Verified ownership must be saved before G. Callback errors
    stop admission or remain sticky failures after admission. Host-mode control
    files are cooperative records, not protection against same-user commands.
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

    def checkpoint(phase, *, terminal=False):
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
        _atomic_json(control / 'progress.json', snapshot)
        if persist is not None:
            persist(_copy(snapshot))
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
        rows = _observe(until, census_commands, cleanup_deadline=report['cleanup_deadline_monotonic'])
        live, members = select_owned(rows, owner, owned)
        previous_count = len(owned)
        for pid, item in members.items():
            owned.setdefault(pid, item)
        if len(owned) > MAX_OBSERVED:
            raise RuntimeError('Owned process evidence bound exceeded')
        group_members = {pid: row for pid, row in rows.items()
                         if row['group'] == owner['pid'] and pid != owner['pid']}
        report['owned_remaining'] = {str(pid): item for pid, item in members.items()}
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
                    rows = _observe(time.monotonic() + remaining(5), census_commands,
                                    cleanup_deadline=report['cleanup_deadline_monotonic'])
                    report['group_empty'] = not any(row['group'] == process.pid for row in rows.values())
                    remaining_owners = {}
                    for pid, saved in owned.items():
                        current = native_processes.identity(pid)
                        if _same(current, saved):
                            remaining_owners[str(pid)] = current
                    report['owned_remaining'] = remaining_owners
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


def verify_completed(snapshot):
    """Revalidate exact clean evidence without launching the original command."""
    control = _validate_snapshot(snapshot)
    saved = _read_saved(control / 'receipt.json', snapshot)
    if (control / 'reconciliation.json').exists():
        saved = _read_saved(control / 'reconciliation.json', saved)
    if saved.get('cleanup_verified') is not True or saved.get('phase') != 'complete':
        raise RuntimeError('No verified terminal host cleanup receipt')
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
        return verify_completed(result)
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
            rows = _observe(until, audit, cleanup_deadline=until)
            live, members = select_owned(rows, owner, retained)
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
