"""Opt-in macOS native-child ownership inside one kernel sandbox instance.

The trusted inner observer's stdout is a private pipe to the outer supervisor.
Test commands receive separate output files and no control descriptors. The
outer supervisor alone writes the durable status under the denied AOS root.
An incomplete census is never a successful cleanup receipt.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BACKEND = 'sandbox-census-v1'
HOST_BACKEND = 'host-owned-v1'


def validate(recipe: dict) -> None:
    backend = recipe.get('native_process_ownership')
    policy = recipe.get('checkout_policy')
    host = False
    if backend == HOST_BACKEND or isinstance(policy, dict) and 'native_validation' in policy:
        from . import checkout_boundary
        from .store import Rejected
        try:
            host = checkout_boundary.reviewed_host(recipe)
        except Rejected as error:
            raise ValueError(str(error)) from error
        if (backend == HOST_BACKEND) != host:
            raise ValueError('host-owned-v1 requires the exact reviewed-host-v1 validation tier and vice versa')
    if backend not in (None, BACKEND, HOST_BACKEND):
        raise ValueError('Unknown native process ownership backend')
    if backend and sys.platform != 'darwin':
        raise ValueError('Reviewed host ownership is qualified only on macOS' if host else
                         'Native sandbox census is qualified only on macOS')
    if backend:
        interpreter = Path(recipe.get('probe_interpreter', sys.executable))
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise ValueError('Native census requires an existing trusted Python interpreter')
        resolved = interpreter.resolve()
        prefixes = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                    interpreter.parent.parent.resolve() if interpreter.parent.name in ('bin', 'Scripts') else interpreter.parent.resolve(),
                    resolved.parent.parent if resolved.parent.name in ('bin', 'Scripts') else resolved.parent)
        for root in (Path(recipe['source']).resolve(), Path(recipe['outputs']).resolve()):
            if (interpreter.is_relative_to(root) or resolved.is_relative_to(root)
                    or any(any(os.path.samefile(p, prefix) for p in (root, *root.parents))
                           or any(os.path.samefile(p, root) for p in (prefix, *prefix.parents)) for prefix in prefixes)):
                raise ValueError('Native census interpreter must be outside model-writable roots')


class BsdInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        'nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


def _library():
    if sys.platform != 'darwin':
        raise RuntimeError('Native sandbox census is qualified only on macOS')
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                   ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_listallpids.restype = ctypes.c_int
    return library


def identity(pid: int) -> dict | None:
    """A PID is never an owner without the microsecond birth and UID."""
    info = BsdInfo()
    ctypes.set_errno(0)
    if _library().proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return None
        raise OSError(error, 'Process identity unavailable')
    if info.pid != pid:
        raise RuntimeError('Process identity mismatch')
    return {'pid': info.pid, 'uid': info.uid, 'birth': [info.start_sec, info.start_usec],
            'parent': info.ppid, 'group': info.pgid, 'status': info.status}


def same(saved: dict, current: dict | None) -> bool:
    return current is not None and all(saved[k] == current[k] for k in ('pid', 'uid', 'birth'))


def all_pids() -> list[int]:
    library = _library()
    for _ in range(3):
        count = library.proc_listallpids(None, 0)
        if count <= 0:
            raise RuntimeError('Native process enumeration failed')
        capacity = count + 1024
        pids = (ctypes.c_int * capacity)()
        actual = library.proc_listallpids(pids, ctypes.sizeof(pids))
        if 0 < actual < capacity:
            return [pid for pid in pids[:actual] if pid > 0]
    raise RuntimeError('Native process enumeration incomplete')


def census() -> tuple[list[dict], bool]:
    """Kernel same-sandbox signal permission selects members, never a name/env."""
    result, complete = [], True
    for pid in all_pids():
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            continue
        except ProcessLookupError:
            complete = False
            continue
        try:
            item = identity(pid)
        except OSError:
            # macOS may briefly deny metadata while an owned process execs.
            # Retry the census; neither an unknown identity nor that incomplete
            # scan is eligible to establish an empty, reconciled sandbox.
            complete = False
            continue
        if item is None or item['status'] == 5:
            # A listed process might have forked before disappearing/reaping.
            # Its child need not be in this initial PID list; rescan rather than
            # allowing an incomplete empty observation to establish cleanup.
            complete = False
        else:
            if item['uid'] != os.getuid():
                raise RuntimeError('Unexpected cross-UID sandbox process')
            result.append(item)
    return result, complete


def verify_absent(receipt: dict) -> None:
    if (receipt.get('backend') != BACKEND or receipt.get('reconciled') is not True
            or receipt.get('boundary_verified') is not True or receipt.get('remaining') != []
            or not isinstance(receipt.get('members'), list) or receipt.get('errors') != []):
        raise RuntimeError('No complete kernel sandbox cleanup receipt')
    for saved in [receipt['observer'], *receipt['members']]:
        current = identity(saved['pid'])
        if same(saved, current) and current['status'] != 5:
            raise RuntimeError('An exact sandbox identity remains alive')


def _inner(payload: dict) -> dict:
    own = identity(os.getpid())
    if own is None:
        raise RuntimeError('Observer identity unavailable')
    # The trusted parent retained the exact outer identity before releasing its
    # execution barrier. Native policy deliberately forbids reading outsider
    # process metadata/argv; verify only its kernel signal boundary here.
    outside = payload['outside']
    os.kill(os.getpid(), 0)
    try:
        os.kill(outside['pid'], 0)
    except PermissionError:
        pass
    else:
        raise RuntimeError('Kernel sandbox does not isolate signal authority')
    # No unrecognized process may already share this sandbox before launch.
    # The launcher may remain above us; its identity is exact ancestry, never
    # a blanket exemption for unrelated processes in the same process group.
    ancestors = []
    parent = own['parent']
    while parent > 1 and parent != outside['pid']:
        try:
            os.kill(parent, 0)
        except PermissionError:
            break  # external launcher, inaccessible to this sandbox
        current = identity(parent)
        if current is None:
            raise RuntimeError('Sandbox launcher ancestry disappeared')
        ancestors.append(current)
        parent = current['parent']
    def workload():
        rows, complete = census()
        return [item for item in rows if not any(same(a, item) for a in ancestors)], complete
    initial_started = time.monotonic()
    initial_until = initial_started + .5
    initial_scans = 0
    while True:
        initial, initial_complete = workload()
        initial_scans += 1
        if initial or initial_complete or time.monotonic() >= initial_until:
            break
        time.sleep(.025)
    if initial or not initial_complete:
        # Preserve the refusal without inventing cleanup or discarding the
        # distinction between an observed member and incomplete observation.
        return {'backend': BACKEND, 'reconciled': False, 'observer': own,
                'payload_started': False,
                'errors': ['RuntimeError: Native sandbox was not empty before command admission'],
                'initial_census': {
                    'classification': 'unexpected_members' if initial else 'incomplete_census',
                    'ancestors': ancestors, 'last_rows': initial, 'complete': initial_complete,
                    'scan_count': initial_scans,
                    'elapsed_seconds': time.monotonic() - initial_started}}
    os.set_blocking(sys.stdin.fileno(), False)
    members, errors, child, exit_code = {}, [], None, None
    boundary_verified = False
    stopped, stop_reason = False, None
    def interrupted(sig, _frame):
        nonlocal stopped, stop_reason
        stopped = True
        stop_reason = 'signal:' + str(sig)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    def observe():
        rows, complete = workload()
        for item in rows:
            key = (item['pid'], *item['birth'])
            members[key] = item
        if len(members) > 16384:
            raise RuntimeError('Native process evidence bound exceeded')
        return rows, complete
    seconds = payload['timeout_seconds']
    if payload.get('execution_deadline'):
        seconds = min(seconds, (datetime.fromisoformat(payload['execution_deadline']) - datetime.now(timezone.utc)).total_seconds())
    deadline = time.monotonic() + seconds
    try:
        if seconds <= 0:
            raise RuntimeError('Native execution deadline expired before command admission')
        with open(payload['stdout'], 'ab', buffering=0) as stdout, open(payload['stderr'], 'ab', buffering=0) as stderr:
            child = subprocess.Popen(payload['command'], cwd=payload['cwd'], stdin=subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, close_fds=True)
        first = identity(child.pid)
        if first is None:
            raise RuntimeError('Unreaped direct child identity unavailable')
        members[(first['pid'], *first['birth'])] = first
        os.kill(child.pid, 0)
        if not same(first, identity(child.pid)):
            raise RuntimeError('Direct child identity changed during signal-boundary probe')
        boundary_verified = True
        while child.poll() is None and not stopped:
            observe()
            try:
                control = os.read(sys.stdin.fileno(), 64)
                if control == b'' or b'C' in control:
                    stopped = True
                    stop_reason = 'control_lost' if control == b'' else 'cancel_requested'
            except BlockingIOError:
                pass
            if time.monotonic() >= deadline:
                stopped = True
                stop_reason = stop_reason or 'deadline'
            if not stopped:
                time.sleep(.025)
    except BaseException as error:
        errors.append(type(error).__name__ + ': ' + str(error))
    finally:
        remaining = []
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                end = time.monotonic() + (0.5 if sig == signal.SIGTERM else 3)
                empty = 0
                while time.monotonic() < end:
                    remaining, complete = observe()
                    if child is not None:
                        child.poll()  # reap the direct child; zombies cannot fork
                    empty = empty + 1 if complete and not remaining else 0
                    if empty >= 3:
                        break
                    for saved in remaining:
                        if same(saved, identity(saved['pid'])):
                            try:
                                os.kill(saved['pid'], sig)
                            except ProcessLookupError:
                                pass
                    time.sleep(.05)
                if empty >= 3:
                    break
            final_until = time.monotonic() + 1
            while True:
                remaining, complete = observe()
                if child is not None:
                    child.poll()
                if complete and not remaining:
                    break
                if time.monotonic() >= final_until:
                    raise RuntimeError('Final native process census was incomplete or still owned live processes')
                for saved in remaining:
                    if same(saved, identity(saved['pid'])):
                        try:
                            os.kill(saved['pid'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                time.sleep(.025)
            if child is not None:
                exit_code = child.wait(timeout=1)
        except BaseException as error:
            errors.append(type(error).__name__ + ': ' + str(error))
    return {'backend': BACKEND, 'observer': own, 'boundary_verified': boundary_verified,
            'members': list(members.values()), 'remaining': remaining, 'errors': errors,
            'reconciled': boundary_verified and not remaining and not errors, 'exit_code': exit_code,
            'interrupted': stopped, 'stop_reason': stop_reason}


def inner_source() -> str:
    return Path(__file__).read_text()


# This supervisor stays outside the native sandbox. Only it can write status;
# the native observer sends its receipt through a pipe never given to tests.
OUTER_SUPERVISOR = '''import json,os,selectors,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
p=json.loads(sys.argv[1])
line=sys.stdin.buffer.readline()
if not line:raise SystemExit(1)
p['inner']['outside']=json.loads(line)
argv=p['argv']+[p['interpreter'],'-I','-c',p['inner_source'],json.dumps(p['inner'])]
resource_receipt=None;resource_failure=None;resource_next_sample=0
def sample_resources(force=False):
 global resource_receipt,resource_failure,resource_next_sample
 if not p.get('resource_guard'):return
 if not force and time.monotonic()<resource_next_sample:return
 try:
  from agentic_tools.aos.resource_limits import sample_file
  guard=dict(p['resource_guard'])
  if resource_receipt:guard['last_observed_at']=resource_receipt.get('observed_at')
  resource_receipt=sample_file(**guard,force=force,
   deadline=datetime.fromisoformat(p['execution_deadline']).timestamp())
  resource_next_sample=time.monotonic()+p['resource_guard']['bound']['sample_interval_seconds']
  if resource_receipt.get('breach'):resource_failure='approved resource limit exceeded'
 except Exception as error:resource_failure='resource observation unavailable: '+type(error).__name__
sample_resources(force=True)
if resource_failure:
 status=Path(p['status']);temporary=status.with_suffix('.writing')
 temporary.write_text(json.dumps({'exit_code':None,'resource_guard':resource_receipt,
  'resource_failure':resource_failure,'native_admission':{'launched':False,'backend':'sandbox-census-v1'}}))
 os.replace(temporary,status)
 while not Path(p['release']).exists():time.sleep(.025)
 raise SystemExit(0)
child=subprocess.Popen(argv,cwd=p['cwd'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
os.set_blocking(child.stdout.fileno(),False)
data=bytearray();sent=False;timed_out=False
seconds=min(p['timeout_seconds'],(datetime.fromisoformat(p['execution_deadline'])-datetime.now(timezone.utc)).total_seconds())
deadline=time.monotonic()+max(0,seconds)+8
while child.poll() is None:
 sample_resources()
 if (Path(p['cancel']).exists() or resource_failure) and not sent:
  try:child.stdin.write(b'C');child.stdin.flush()
  except BrokenPipeError:pass
  sent=True
 try:
  chunk=os.read(child.stdout.fileno(),65536)
  data.extend(chunk)
 except BlockingIOError:pass
 if len(data)>4*1024*1024 or time.monotonic()>deadline:
  timed_out=True;child.terminate()
  try:child.wait(timeout=2)
  except subprocess.TimeoutExpired:child.kill();child.wait()
  break
 time.sleep(.025)
while True:
 try:chunk=os.read(child.stdout.fileno(),65536)
 except BlockingIOError:break
 if not chunk:break
 data.extend(chunk)
try:
 census=json.loads(data)
 if timed_out or child.returncode!=0:raise ValueError('observer did not finish normally')
 status_data={'exit_code':census.get('exit_code'),'census':census}
except Exception:
 status_data={'exit_code':None,'census':{'reconciled':False,'error':'Native observer did not retain a complete receipt'}}
sample_resources(force=True)
if p.get('resource_guard'):
 status_data['resource_guard']=resource_receipt
 status_data['resource_failure']=resource_failure
status=Path(p['status']);temporary=status.with_suffix('.writing')
temporary.write_text(json.dumps(status_data));os.replace(temporary,status)
while not Path(p['release']).exists():time.sleep(.025)
'''


if __name__ == '__main__':
    try:
        result = _inner(json.loads(sys.argv[1]))
    except BaseException as error:
        result = {'backend': BACKEND, 'reconciled': False,
                  'errors': [type(error).__name__ + ': ' + str(error)]}
    print(json.dumps(result), flush=True)
