"""Bounded, read-only native profile rendering in one trusted nofork child.

This is coordinator work, not a product command or a worker permission grant.
Only fixed source modules are loaded. The audit hook guards trusted-code
regressions; it is not a security boundary for arbitrary Python source.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time

from . import native_paths, native_processes
from .store import Rejected, canonical, digest


FORMAT = 'aos-native-renderer-v1'
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
MAX_STDERR = 64 * 1024
REAP_SECONDS = 2
MODULES = ('store', 'native_paths', 'native_network', 'native_sandbox')

BOOTSTRAP = r'''import hashlib,json,os,signal,sys,time,types
from pathlib import Path
def main():
 raw=sys.stdin.buffer.read(16*1024*1024+1)
 if len(raw)>16*1024*1024:raise ValueError('Renderer input exceeds bound')
 data=json.loads(raw)
 left=min(data['remaining_seconds'],data['deadline_timestamp']-time.time())
 if left<=0:raise TimeoutError('Original rendering deadline expired')
 def expired(*_):raise TimeoutError('Original rendering deadline expired')
 signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,left)
 writes=os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND
 forbidden={'subprocess.Popen','os.fork','os.forkpty','os.posix_spawn','os.exec','os.system',
  'os.mkdir','os.remove','os.rmdir','os.rename','os.link','os.symlink','os.truncate',
  'os.chmod','os.chown','os.utime','os.chdir','sqlite3.connect'}
 def audit(event,args):
  if event in forbidden or event.startswith('socket.'):
   raise RuntimeError('Trusted renderer forbids side effect: '+event)
  if event=='open' and ((isinstance(args[1],str) and any(x in args[1] for x in 'wax+'))
      or (isinstance(args[2],int) and args[2]&writes)):
   raise RuntimeError('Trusted renderer forbids filesystem writes')
 sys.addaudithook(audit)
 package=types.ModuleType('agentic_tools');package.__path__=[]
 aos=types.ModuleType('agentic_tools.aos');aos.__path__=[]
 package.aos=aos
 sys.modules['agentic_tools']=package;sys.modules['agentic_tools.aos']=aos
 for name in ('store','native_paths','native_network','native_sandbox'):
  item=data['modules'][name];source=item['source'].encode('utf-8')
  if hashlib.sha256(source).hexdigest()!=item['sha256']:raise ValueError('Renderer source digest differs')
  fullname='agentic_tools.aos.'+name
  module=types.ModuleType(fullname);module.__file__=item['path'];module.__package__='agentic_tools.aos'
  sys.modules[fullname]=module;setattr(aos,name,module)
  exec(compile(source,item['path'],'exec'),module.__dict__)
 value=aos.native_sandbox.render(data['recipe'],data['filesystem'],preexisting=data['preexisting'],
  protected=[Path(p) for p in data['protected']],network_observation=data['network_observation'])
 raw=value.encode('utf-8')
 if not raw or len(raw)>64*1024*1024:raise ValueError('Renderer output exceeds bound')
 sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
try:main()
except BaseException as error:
 sys.stderr.write(json.dumps({'code':getattr(error,'code','native_renderer'),
  'message':str(error)[:4000],'type':type(error).__name__})+'\n')
 sys.exit(1)
'''


def is_launcher(argv) -> bool:
    """Recognize only this fixed trusted helper, never a product supervisor."""
    return argv == [sys.executable, '-I', '-S', '-B', '-c', BOOTSTRAP]


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    if path.is_symlink():
        raise Rejected('native_renderer_source', 'Trusted renderer code cannot be a symlink')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    current = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (not stat.S_ISREG(before.st_mode) or len(raw) > 1024 * 1024
            or identity(before) != identity(after) or identity(before) != identity(current)):
        raise Rejected('native_renderer_source', 'Trusted renderer source is unavailable or changed')
    return raw


def source_hashes():
    base = Path(__file__).resolve().parent
    return {'aos/' + name + '.py': _hash(_read(base / (name + '.py')))
            for name in (*MODULES, 'native_renderer')}


def _sources(expected):
    base = Path(__file__).resolve().parent
    raw = {name: _read(base / (name + '.py')) for name in (*MODULES, 'native_renderer')}
    hashes = {'aos/' + name + '.py': _hash(value) for name, value in raw.items()}
    expected = hashes if expected is None else expected
    if (not isinstance(expected, dict) or any(expected.get(name) != value for name, value in hashes.items())
            or any(not isinstance(k, str) or not _sha(v) for k, v in expected.items())):
        raise Rejected('native_renderer_source', 'Renderer source differs from the trusted coordinator snapshot')
    modules = {name: {'source': raw[name].decode('utf-8'), 'path': str(base / (name + '.py')),
                      'sha256': hashes['aos/' + name + '.py']} for name in MODULES}
    return modules, hashes, copy.deepcopy(expected)


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def verify_receipt(receipt, *, require_success=False):
    """Validate historical reaping evidence without reopening current code/data."""
    if not isinstance(receipt, dict) or receipt.get('format') != FORMAT:
        raise Rejected('native_renderer_receipt', 'Exact renderer receipt required')
    if receipt == {'format': FORMAT, 'state': 'not_started'} and not require_success:
        return receipt
    state = receipt.get('state')
    hashes = receipt.get('source_hashes', {})
    component = receipt.get('component_sources', {})
    if (state not in ('not_started', 'reaped')
            or not isinstance(hashes, dict) or set(hashes) != {'aos/' + n + '.py' for n in (*MODULES, 'native_renderer')}
            or not isinstance(component, dict) or any(not isinstance(k, str) or not _sha(v) for k, v in component.items())
            or any(component.get(k) != v or not _sha(v) for k, v in hashes.items())
            or receipt.get('component_source_digest') != digest(component)
            or any(not _sha(receipt.get(k)) for k in ('bootstrap_sha256', 'input_sha256'))
            or not isinstance(receipt.get('interpreter'), dict) or not _sha(receipt['interpreter'].get('sha256'))
            or not Path(receipt['interpreter'].get('path', '')).is_absolute()
            or any(receipt.get(k) is not True for k in ('isolated', 'site_disabled', 'bytecode_disabled', 'nofork', 'read_only'))
            or type(receipt.get('input_bytes')) is not int or not 0 < receipt['input_bytes'] <= MAX_INPUT):
        raise Rejected('native_renderer_receipt', 'Renderer source/input boundary is incomplete')
    try:
        deadline = datetime.fromisoformat(receipt['execution_deadline'])
        if deadline.tzinfo is None or not math.isfinite(deadline.timestamp()):
            raise ValueError('deadline')
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise Rejected('native_renderer_receipt', 'Renderer original deadline is missing') from error
    for key in ('recipe_contract_digest', 'preexisting_digest', 'network_observation_digest', 'filesystem_digest', 'protected_digest'):
        if not isinstance(receipt.get(key), str) or not receipt[key].startswith('sha256:') or not _sha(receipt[key][7:]):
            raise Rejected('native_renderer_receipt', 'Renderer input bindings are missing')
    if state == 'reaped':
        owner = receipt.get('owner_identity', {})
        if (type(receipt.get('exit_code')) is not int or type(receipt.get('pid')) is not int or receipt['pid'] <= 0
                or not isinstance(owner, dict) or owner.get('pid') != receipt['pid']
                or type(receipt.get('coordinator_uid')) is not int or receipt['coordinator_uid'] < 0
                or owner.get('uid') != receipt['coordinator_uid']
                or not isinstance(owner.get('birth'), list) or len(owner['birth']) != 2
                or any(type(n) is not int for n in owner['birth']) or owner['birth'][0] <= 0
                or not 0 <= owner['birth'][1] < 1000000
                or receipt.get('outcome') not in ('returned', 'success', 'rejected', 'failed', 'timeout', 'interrupted', 'output_limit')
                or any(not _sha(receipt.get(k)) for k in ('stdout_sha256', 'stderr_sha256'))
                or type(receipt.get('output_bytes')) is not int or not 0 <= receipt['output_bytes'] <= MAX_OUTPUT):
            raise Rejected('native_renderer_receipt', 'Renderer exact exit/reaping evidence is incomplete')
    if require_success or receipt.get('outcome') == 'success':
        if (state != 'reaped' or receipt.get('outcome') != 'success' or receipt.get('exit_code') != 0
                or receipt.get('output_validated') is not True or receipt.get('outputs_complete') is not True
                or not receipt.get('output_bytes') or receipt.get('profile_sha256') != receipt.get('stdout_sha256')):
            raise Rejected('native_renderer_receipt', 'No complete successful rendered profile')
    return receipt


class _OutputLimit(Rejected):
    def __init__(self):
        super().__init__('native_renderer_output', 'Native renderer exceeded its bounded output')


def _drain(process, raw, end, stdout, stderr):
    offset = 0
    with selectors.DefaultSelector() as selector:
        for stream, events, label in ((process.stdin, selectors.EVENT_WRITE, 'input'),
                                       (process.stdout, selectors.EVENT_READ, 'output'),
                                       (process.stderr, selectors.EVENT_READ, 'error')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, label)
        while selector.get_map():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('trusted native renderer', 0)
            for key, _ in selector.select(min(remaining, .1)):
                stream = key.fileobj
                if key.data == 'input':
                    try:
                        offset += os.write(stream.fileno(), memoryview(raw)[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        offset = len(raw)
                    if offset == len(raw):
                        selector.unregister(stream); stream.close()
                else:
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(stream); stream.close()
                        continue
                    target, bound = (stdout, MAX_OUTPUT) if key.data == 'output' else (stderr, MAX_STDERR)
                    if len(target) + len(data) > bound:
                        raise _OutputLimit()
                    target.extend(data)


def render(recipe, filesystem, *, preexisting, protected, network_observation, deadline,
           observe, expected_sources=None) -> bytes:
    """Render without holding the coordinator GIL during expensive path checks."""
    observe({'format': FORMAT, 'state': 'not_started'})
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise Rejected('native_renderer_deadline', 'An aware original execution deadline is required')
    remaining = deadline.timestamp() - datetime.now(timezone.utc).timestamp()
    if not math.isfinite(remaining) or remaining <= 0:
        raise Rejected('assignment_deadline', 'Original rendering deadline expired')
    end = time.monotonic() + remaining
    modules, hashes, component = _sources(expected_sources)
    interpreter = Path(sys.executable).resolve()
    interpreter_hash = _hash(interpreter.read_bytes())
    protected = list(map(str, protected))
    payload = {'modules': modules, 'recipe': recipe, 'filesystem': filesystem, 'preexisting': preexisting,
               'protected': protected, 'network_observation': network_observation,
               'deadline_timestamp': deadline.timestamp(), 'remaining_seconds': end - time.monotonic()}
    raw = canonical(payload).encode('utf-8')
    if not raw or len(raw) > MAX_INPUT:
        raise Rejected('native_renderer_input', 'Native renderer input exceeds its bounded size')
    receipt = {'format': FORMAT, 'state': 'not_started', 'source_hashes': hashes, 'component_sources': component,
               'component_source_digest': digest(component), 'bootstrap_sha256': _hash(BOOTSTRAP.encode()),
               'interpreter': {'path': str(interpreter), 'sha256': interpreter_hash},
               'input_sha256': _hash(raw), 'input_bytes': len(raw), 'execution_deadline': deadline.isoformat(),
               'recipe_contract_digest': digest(native_paths.contract(recipe)), 'preexisting_digest': digest(preexisting),
               'network_observation_digest': digest(network_observation), 'filesystem_digest': digest(filesystem),
               'protected_digest': digest(protected), 'isolated': True, 'site_disabled': True,
               'bytecode_disabled': True, 'nofork': True, 'read_only': True, 'coordinator_uid': os.getuid()}
    observe(copy.deepcopy(receipt))
    if end <= time.monotonic():
        raise Rejected('assignment_deadline', 'Original rendering deadline expired before launch')
    receipt['state'] = 'launch_ambiguous'
    observe(copy.deepcopy(receipt))
    argv = [sys.executable, '-I', '-S', '-B', '-c', BOOTSTRAP]
    try:
        process = subprocess.Popen(argv, cwd=str(Path(__file__).resolve().parent),
            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8', 'HOME': str(Path.home())},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    except BaseException as error:
        raise Rejected('native_renderer_launch_unknown', 'Renderer launch/reaping is unproved') from error
    receipt['pid'] = process.pid
    stdout, stderr = bytearray(), bytearray()
    error = None
    identified = False
    try:
        owner = native_processes.identity(process.pid)
        if owner is None or owner['uid'] != os.getuid() or owner['status'] == 5:
            raise Rejected('native_renderer_launch_unknown', 'Exact live renderer identity is unavailable')
        receipt['owner_identity'] = owner
        identified = True
        # The fixed child blocks reading stdin until this exact identity has
        # been observed. No source bundle or rendering input is released earlier.
        observe(copy.deepcopy(receipt))
        _drain(process, raw, end, stdout, stderr)
        process.wait(timeout=max(0, end - time.monotonic()))
    except BaseException as failure:
        error = failure
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=REAP_SECONDS)
        except BaseException as unknown:
            raise Rejected('native_renderer_launch_unknown', 'Renderer exact exit/reaping is unproved') from unknown
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if type(process.returncode) is not int:
        raise Rejected('native_renderer_launch_unknown', 'Renderer exact exit code is unavailable')
    if not identified:
        raise Rejected('native_renderer_launch_unknown', 'Renderer identity was not proved before input release') from error
    stdout, stderr = bytes(stdout), bytes(stderr)
    outcome = ('timeout' if isinstance(error, subprocess.TimeoutExpired) else
               'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else
               'output_limit' if isinstance(error, _OutputLimit) else 'failed' if error else
               'returned' if process.returncode == 0 else 'rejected')
    receipt.update(state='reaped', outcome=outcome, exit_code=process.returncode,
                   stdout_sha256=_hash(stdout), stderr_sha256=_hash(stderr), output_bytes=len(stdout),
                   outputs_complete=error is None)
    observe(copy.deepcopy(receipt))
    if error is not None:
        raise error
    if process.returncode != 0:
        try:
            failure = json.loads(stderr)
            code, message = failure['code'], failure['message']
            if not isinstance(code, str) or not isinstance(message, str):
                raise ValueError('error shape')
        except (ValueError, KeyError, TypeError):
            code, message = 'native_renderer', 'Trusted renderer did not complete successfully'
        raise Rejected(code, message)
    # Never accept stdout from a helper whose code or interpreter changed while
    # it ran. A completed reaping receipt still supports preparation-only cleanup.
    _sources(component)
    if _hash(interpreter.read_bytes()) != interpreter_hash:
        raise Rejected('native_renderer_source', 'Trusted renderer interpreter changed')
    if end <= time.monotonic():
        raise Rejected('assignment_deadline', 'Original rendering deadline expired before acceptance')
    try:
        value = stdout.decode('utf-8')
    except UnicodeDecodeError as failure:
        raise Rejected('native_renderer_output', 'Renderer profile is not complete UTF-8') from failure
    if not value.startswith('(version 1)\n') or not value.endswith('\n'):
        raise Rejected('native_renderer_output', 'Renderer profile is incomplete')
    receipt.update(outcome='success', output_validated=True, profile_sha256=_hash(stdout))
    observe(copy.deepcopy(receipt))
    verify_receipt(receipt, require_success=True)
    return stdout
