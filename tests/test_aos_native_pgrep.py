import errno
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from agentic_tools.aos import native_pgrep as pgrep
from agentic_tools.aos import native_sandbox as sandbox


def test_argument_parser_excludes_environment_and_preserves_empty_arguments():
    path = b'/usr/bin/tool\0'
    raw = struct.pack('=i', 3) + path + b'\0' * (-len(path) % 8) + b'tool\0\0marker\0SECRET=hidden\0'
    assert pgrep._argv(raw) == b'tool  marker'
    with pytest.raises(RuntimeError):
        pgrep._argv(struct.pack('=i', 2) + b'/tool\0tool\0')


def test_empty_first_argument_does_not_consume_an_environment_entry():
    path = b'/bin/sleep\0'
    raw = struct.pack('=i', 2) + path + b'\0' * (-len(path) % 8) + b'\0' + b'15\0SECRET=hidden\0'
    assert pgrep._argv(raw) == b' 15'


def test_denied_pid_is_never_inspected_and_changed_identity_never_matches(monkeypatch):
    seen = []
    records = {701: {'pid': 701, 'uid': os.getuid(), 'birth': [1, 1], 'status': 2},
               702: {'pid': 702, 'uid': os.getuid(), 'birth': [1, 1], 'status': 2}}
    def permitted(pid, sig):
        assert sig == 0
        if pid == 700:
            raise PermissionError(errno.EPERM, 'outside')
    def identity(pid):
        assert pid != 700
        seen.append(pid)
        item = dict(records[pid])
        if pid == 702 and seen.count(pid) > 1:
            item['birth'] = [1, 2]
        return item
    processes = SimpleNamespace(all_pids=lambda: [700, 701, 702], identity=identity,
                                same=lambda a, b: a == b)
    monkeypatch.setattr(pgrep.os, 'kill', permitted)
    monkeypatch.setattr(pgrep, 'arguments', lambda pid: b'owned')
    assert pgrep.find(SimpleNamespace(matches=lambda command: True), processes) == [701]
    assert seen == [701, 701, 702, 702]


def test_unknown_process_failure_is_not_reported_as_no_match(monkeypatch):
    processes = SimpleNamespace(all_pids=lambda: [703])
    def failed(pid, sig):
        raise OSError(errno.EIO, 'unknown')
    monkeypatch.setattr(pgrep.os, 'kill', failed)
    with pytest.raises(OSError):
        pgrep.find(SimpleNamespace(matches=lambda command: True), processes)


def test_control_source_is_executed_instead_of_existing_bytecode(tmp_path, monkeypatch):
    import py_compile
    sibling = tmp_path / 'native_processes.py'
    sibling.write_text("value = 'wrong'\n")
    py_compile.compile(str(sibling), doraise=True)
    saved = sibling.stat()
    sibling.write_text("value = 'right'\n")  # same size and timestamp: ordinary import accepts stale pyc
    os.utime(sibling, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    monkeypatch.setattr(pgrep, '__file__', str(tmp_path / 'native_pgrep.py'))
    assert pgrep._processes().value == 'right'


def test_internal_failure_returns_three_without_partial_pid_output(monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(pgrep, 'Pattern', lambda text: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(pgrep, '_require_sandbox', lambda: None)
    def failure(pattern):
        raise ValueError('internal failure')
    monkeypatch.setattr(pgrep, 'find', failure)
    assert pgrep.main(['-af', 'query']) == 3
    assert capsys.readouterr().out == ''
    assert closed == [True]


@pytest.mark.parametrize('args', [[], ['-f', 'x'], ['-a', '-f', 'x'], ['-af'], ['-af', 'x', 'y']])
def test_only_exact_supported_cli_is_accepted(args, capsys):
    assert pgrep.main(args) == 2
    assert capsys.readouterr().out == ''


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin POSIX regex ABI')
def test_uses_posix_extended_regex_and_rejects_bad_pattern():
    pattern = pgrep.Pattern(r'(^| )core[[:digit:]]{2}[.]sock( |$)')
    try:
        assert pattern.matches(b'server core12.sock --option')
        assert not pattern.matches(b'server corex2.sock')
    finally:
        pattern.close()
    with pytest.raises(ValueError):
        pgrep.Pattern('[')


def test_wrapper_uses_isolated_interpreter_and_exact_source():
    import shlex
    text = pgrep.wrapper(sys.executable)
    words = shlex.split(text.splitlines()[1])
    assert words == ['exec', str(Path(sys.executable).resolve()), '-I', '-S',
                     str(Path(pgrep.__file__).resolve()), '$@']
    with pytest.raises(ValueError):
        pgrep.wrapper('python3')


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual macOS process boundary')
def test_real_helper_finds_owned_and_detached_children_but_denies_outsider(tmp_path):
    python = str(Path(sys._base_executable).resolve())
    source = Path(pgrep.__file__).resolve().parent
    control, output = tmp_path / 'control', tmp_path / 'output'
    control.mkdir()
    output.mkdir()
    executable = control / 'pgrep'
    executable.write_text(pgrep.wrapper(python))
    executable.chmod(0o700)
    marker = 'aos-owned-pgrep-' + uuid.uuid4().hex
    outside_marker = 'aos-outside-pgrep-' + uuid.uuid4().hex
    outside = subprocess.Popen([python, '-I', '-S', '-c', 'import time;time.sleep(30)', outside_marker],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    recipe = {'native_sandbox': {'version': sandbox.VERSION}}
    mapping = {':root': 'deny', ':minimal': 'read', str(source): 'read',
               str(control): 'read', str(output): 'write',
               str(Path(sys.base_prefix).resolve()): 'read'}
    profile = sandbox.render(recipe, mapping, preexisting={}, protected=[source, control])
    assert 'com.apple.sysmond' not in profile
    program = r'''
import ctypes,errno,importlib.util,json,os,signal,subprocess,sys,time
from pathlib import Path
p=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('trusted', p['helper'])
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
processes=helper._processes()
child=None; detached=None; empty_pid=None
result={}
def query(pattern, flags='-af'):
 r=subprocess.run([p['wrapper'],flags,pattern],capture_output=True,text=True,timeout=5)
 return {'code':r.returncode,'pids':[int(v) for v in r.stdout.splitlines()], 'stderr':r.stderr}
try:
 child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(20)',p['marker']],stdin=subprocess.DEVNULL)
 detached_program='import subprocess,sys; p=subprocess.Popen([sys.executable,"-I","-S","-c","import time;time.sleep(20)",sys.argv[1]],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid)'
 spawned=subprocess.run([sys.executable,'-I','-S','-c',detached_program,p['marker']],capture_output=True,text=True,timeout=5)
 if spawned.returncode:raise RuntimeError(spawned.stderr)
 detached=int(spawned.stdout);saved=processes.identity(detached)
 result['owned']=[child.pid,detached]
 result['found']=query(p['marker'])
 result['outside']=query(p['outside_marker'])
 result['no_match']=query('aos-nonexistent-'+p['marker'])
 result['unsupported']=query(p['marker'],'-l')
 result['invalid']=query('[')
 try:helper.arguments(p['outside_pid'])
 except OSError as error:result['outside_direct_errno']=error.errno
 else:result['outside_direct_errno']=0
 try:os.kill(p['outside_pid'],0)
 except OSError as error:result['outside_signal_errno']=error.errno
 else:result['outside_signal_errno']=0
 result['detached_parent']=saved['parent']
 # Darwin permits empty argv[0], although Python's spawn interface rejects it.
 # Confirm the helper does not consume this process's first environment entry.
 library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
 library.posix_spawn.argtypes=[ctypes.POINTER(ctypes.c_int),ctypes.c_char_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_char_p),ctypes.POINTER(ctypes.c_char_p)]
 library.posix_spawn.restype=ctypes.c_int
 vector=(ctypes.c_char_p*3)(b'',b'20',None)
 environment=(ctypes.c_char_p*2)(('AOS_EMPTY_ARGV_ENV='+p['marker']+'-environment-only').encode(),None)
 created=ctypes.c_int()
 error=library.posix_spawn(ctypes.byref(created),b'/bin/sleep',None,None,vector,environment)
 if error:raise OSError(error,'Empty argv fixture failed')
 empty_pid=created.value
 result['empty_argv_environment']=query(p['marker']+'-environment-only')
finally:
 if empty_pid is not None:
  os.kill(empty_pid,signal.SIGTERM);os.waitpid(empty_pid,0)
 if child is not None:child.terminate();child.wait(timeout=3)
 if detached is not None and processes.same(saved,processes.identity(detached)):
  os.kill(detached,signal.SIGTERM)
  deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   current=processes.identity(detached)
   if not processes.same(saved,current) or current['status']==5:break
   time.sleep(.02)
  else:raise RuntimeError('Detached fixture did not stop')
print(json.dumps(result))
'''
    payload = {'helper': str(source / 'native_pgrep.py'), 'wrapper': str(executable),
               'marker': marker, 'outside_marker': outside_marker, 'outside_pid': outside.pid}
    try:
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, python, '-I', '-S', '-c', program],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=20,
                                cwd=output, env={'PATH': '/usr/bin:/bin'})
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed['found']['code'] == 0 and set(observed['found']['pids']) == set(observed['owned'])
        assert observed['outside']['code'] == observed['no_match']['code'] == 1
        assert observed['outside']['pids'] == []
        assert observed['empty_argv_environment']['code'] == 1
        assert observed['empty_argv_environment']['pids'] == []
        assert observed['unsupported']['code'] == observed['invalid']['code'] == 2
        assert observed['outside_direct_errno'] == observed['outside_signal_errno'] == errno.EPERM
        assert observed['detached_parent'] == 1
        assert outside.poll() is None
    finally:
        outside.terminate()
        outside.wait(timeout=3)
