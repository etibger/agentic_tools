"""Real nested supervisors with synthetic observation leaves, never a host census.

The public runners, selector, mutex, durable receipts, child launch, pipes and
reaping are real. Only process identity/census answers are a closed fixture world
of our direct children. No /bin/ps, VM, model, live ledger or host scan is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import types

# The harmless nested interpreter uses -I and imports this exact test checkout.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from agentic_tools.aos import owned_host_command as command

VIRTUAL_HELPER = 900000001  # A synthetic candidate, never a signal target.


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.writing')
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def read(path):
    return json.loads(path.read_text())


def who(pid, parent, group, uid=None):
    return {'pid': pid, 'parent': parent, 'group': group,
            'uid': os.geteuid() if uid is None else uid, 'birth': [pid, 17], 'status': 2}


def make_binding(root):
    directory = root / 'coordination'; directory.mkdir(mode=0o700)
    path = directory / 'observation.lock'; path.touch(mode=0o600)
    info = path.stat()
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
            'uid': info.st_uid, 'mode': info.st_mode, 'nlink': info.st_nlink}


class SyntheticNestedWorld:
    """A closed observation leaf; real supervisor Popen handles prove reaping."""
    def __init__(self, root, role, binding):
        self.root, self.role, self.binding = root, role, binding
        self.handles = {}
        self.callback_events = []
        self.signal_attempts = []
        self.leaf_returns = 0
        self.classification_seen = False
        self.first_inner_observation = False
        self.real_popen = subprocess.Popen
        self.real_flock = fcntl.flock
        self.real_same = command.native_processes.same
        self.controller = (read(root / 'inner-controller.json') if role == 'inner'
                           else who(os.getpid(), os.getppid(), os.getpgrp()))

    @contextmanager
    def installed(self):
        prior = command.native_processes, command._observe, command.subprocess, command.fcntl, command.os
        command.native_processes = types.SimpleNamespace(identity=self.identity, same=self.real_same)
        command._observe = self.observe
        command.subprocess = types.SimpleNamespace(Popen=self.popen, PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired)
        command.fcntl = types.SimpleNamespace(flock=self.flock, LOCK_EX=fcntl.LOCK_EX,
            LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN)
        # Synthetic identities can never authorize a numeric signal, even on
        # unexpected failure paths. Keep filesystem and self-read APIs real.
        safe_os = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
        safe_os.kill = lambda pid, sig: self.reject_signal('kill', pid, sig)
        safe_os.killpg = lambda group, sig: self.reject_signal('killpg', group, sig)
        command.os = safe_os
        try:
            yield self
        finally:
            command.native_processes, command._observe, command.subprocess, command.fcntl, command.os = prior

    def reject_signal(self, api, target, sig):
        self.signal_attempts.append({'api': api, 'target': target, 'signal': int(sig)})
        raise AssertionError('Synthetic ownership cannot authorize numeric signals')

    def popen(self, argv, **kwargs):
        assert argv[:4] == [sys.executable, '-I', '-B', str(command.SUPERVISOR)]
        assert kwargs['start_new_session'] is True
        process = self.real_popen(argv, **kwargs)
        self.handles[process.pid] = process
        return process

    def active_marker(self):
        path = self.root / 'inner-active.json'
        return read(path) if path.exists() else None

    def flock(self, descriptor, operation):
        # This instrumentation always delegates the real kernel flock. A busy
        # result records an actual public _observation_fence wait, not a sleep.
        if self.role == 'inner' and operation == fcntl.LOCK_UN and self.active_marker() is not None:
            assert self.classification_seen, 'Marker must survive through the actual selector'
            assert Path(self.binding['path']).read_bytes() == b'', 'Fresh helper cleanup must clear ACTIVE'
            save(self.root / 'inner-classified.json', {'selector_ran': True,
                'marker_present_during_selector': True, 'monotonic': time.monotonic()})
            (self.root / 'inner-active.json').unlink()
        try:
            return self.real_flock(descriptor, operation)
        except BlockingIOError:
            marker = self.active_marker()
            if self.role == 'outer' and marker is not None and not (self.root / 'outer-wait.json').exists():
                caller = inspect.currentframe().f_back.f_code.co_name
                assert caller == '_observation_fence'
                claim = read(Path(self.binding['path']))
                assert claim['format'] == 'owned-observation-active-v1'
                assert claim['controller_pid'] == marker['parent']
                save(self.root / 'outer-wait.json', {'caller': caller, 'candidate_uid': marker['uid'],
                    'active_claim': claim, 'monotonic': time.monotonic()})
            raise

    def known(self):
        identities = {self.controller['pid']: self.controller}
        for pid, handle in self.handles.items():
            if handle.poll() is None:
                identities[pid] = who(pid, os.getpid(), pid)
        controller = self.root / 'inner-controller.json'
        if self.role == 'outer' and controller.exists() and not (self.root / 'outer-owned/status.json').exists():
            item = read(controller); identities[item['pid']] = item
        inner_owner = self.root / 'inner-owned/owner.json'
        if self.role == 'outer' and inner_owner.exists() and not (self.root / 'inner-done.json').exists():
            item = read(inner_owner); identities[item['pid']] = item
        payload = self.root / 'payload-pid.json'
        if payload.exists() and inner_owner.exists() and not (self.root / 'inner-owned/status.json').exists():
            owner = read(inner_owner); pid = read(payload)['pid']
            identities[pid] = who(pid, owner['pid'], owner['pid'])
        marker = self.active_marker()
        if marker is not None:
            identities[marker['pid']] = marker
        return identities

    def identity(self, pid):
        if (self.role == 'inner' and self.active_marker() is not None and self.leaf_returns
                and pid in self.handles):
            names = []
            frame = inspect.currentframe().f_back
            for _ in range(4):
                if frame is None: break
                names.append(frame.f_code.co_name); frame = frame.f_back
            if 'select_owned' in names:
                claim = read(Path(self.binding['path']))
                assert claim['controller_pid'] == os.getpid()
                self.classification_seen = True
        # There is deliberately no kernel fallback for unknown or unrelated PIDs.
        return self.known().get(pid)

    def observe(self, deadline, audit, *, cleanup_deadline=None):
        assert time.monotonic() < deadline
        claim = read(Path(self.binding['path']))
        assert claim['format'] == 'owned-observation-active-v1'
        assert claim['controller_pid'] == os.getpid()
        assert claim['observation_lock'] == self.binding
        if self.role == 'inner' and not self.first_inner_observation:
            self.first_inner_observation = True
            marker = who(VIRTUAL_HELPER, self.controller['pid'], VIRTUAL_HELPER, uid=0)
            save(self.root / 'inner-active.json', marker)
            until = min(deadline, time.monotonic() + 3)
            while not (self.root / 'outer-wait.json').exists() and time.monotonic() < until:
                time.sleep(.005)
            assert (self.root / 'outer-wait.json').exists(), 'No real overlapping outer lock attempt occurred'
            assert self.active_marker() == marker
        if self.role == 'outer':
            assert self.active_marker() is None, 'The outer census overlapped the inner census/classifier'
        identities = self.known()
        audit.append({'synthetic_observation_leaf': True, 'reaped': True, 'pipes_eof': True,
            'stdout_closed': True, 'stderr_closed': True, 'output_valid': True,
            'errors': [], 'exit_code': 0, 'interrupted': False})
        self.leaf_returns += 1
        return {pid: {key: item[key] for key in ('parent', 'group', 'uid')}
                for pid, item in identities.items()}

    def callback(self, name):
        raw = Path(self.binding['path']).read_bytes()
        if raw:
            try:
                claim = json.loads(raw)
            except json.JSONDecodeError:
                claim = {}  # Another process can be publishing its claim.
            assert claim.get('controller_pid') != os.getpid(), 'Callback ran inside its own census fence'
        self.callback_events.append(name)

    def before_go(self, deadline):
        self.callback('before_go')
        # At this boundary no inner observation is waiting for the outer runner.
        descriptor = os.open(self.binding['path'], os.O_RDWR)
        try:
            until = min(deadline, time.monotonic() + 2)
            while True:
                try:
                    self.real_flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    assert time.monotonic() < until, 'before_go could not acquire its released fence'
                    time.sleep(.005)
            self.real_flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def cancelled(self):
        self.callback('cancelled'); return False

    def persist(self, snapshot):
        self.callback('persist:' + snapshot['phase'])

    def finish_owned_handles(self):
        # Failure cleanup addresses only this test's exact known control paths
        # and direct Popen handles. No census or PID-based signalling is used.
        controls = ('inner-owned',) if self.role == 'inner' else ('inner-owned', 'outer-owned')
        for name in controls:
            directory = self.root / name
            if directory.exists():
                (directory / 'release').touch(exist_ok=True)
        for handle in self.handles.values():
            if handle.stdin is not None and not handle.stdin.closed:
                handle.stdin.close()
            handle.wait(timeout=20)


def inner_entry(root, binding):
    outer = read(root / 'outer-owned/owner.json')
    save(root / 'inner-controller.json', who(os.getpid(), outer['pid'], outer['pid']))
    world = SyntheticNestedWorld(root, 'inner', binding)
    payload = ('import json,os,time;from pathlib import Path;'
               'Path(' + repr(str(root / 'payload-pid.json')) + ').write_text(json.dumps({"pid":os.getpid()}));'
               'print("nested harmless payload",flush=True);time.sleep(0.05)')
    with world.installed():
        report = command.run_owned([sys.executable, '-I', '-B', '-c', payload], root,
            {'PATH': '/usr/bin:/bin', 'LANG': 'C'}, root / 'inner-owned',
            deadline=time.monotonic() + 10, cleanup_deadline=time.monotonic() + 22,
            before_go=world.before_go, cancelled=world.cancelled, persist=world.persist,
            observation_lock=binding)
        save(root / 'inner-result.json', report)
        checked = command.verify_completed(report)
        assert checked['passed'] and checked['cleanup_verified'], checked['errors']
    world.finish_owned_handles()
    assert not world.signal_attempts
    save(root / 'inner-done.json', {'passed': report['passed'], 'callbacks': world.callback_events,
                                  'signal_attempts': world.signal_attempts})
    return 0


def test_uncoordinated_nested_uid_zero_candidate_is_rejected():
    import pytest
    leader = who(41001, 40000, 41001, uid=501)
    nested = who(41002, leader['pid'], leader['pid'], uid=501)
    helper = who(VIRTUAL_HELPER, nested['pid'], VIRTUAL_HELPER, uid=0)
    identities = {item['pid']: item for item in (leader, nested, helper)}
    rows = {pid: {key: item[key] for key in ('parent', 'group', 'uid')}
            for pid, item in identities.items()}
    with pytest.raises(RuntimeError, match='cross-UID'):
        command.select_owned(rows, leader, {}, identity_reader=identities.get)


def test_public_nested_runners_share_census_fence_and_retain_both_receipts(tmp_path):
    root = tmp_path.resolve(); root.chmod(0o700)
    binding = make_binding(root)
    assert binding['mode'] == stat.S_IFREG | 0o600
    world = SyntheticNestedWorld(root, 'outer', binding)
    try:
        with world.installed():
            report = command.run_owned([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                '--inner', str(root), json.dumps(binding)], root,
                {'PATH': '/usr/bin:/bin', 'LANG': 'C'}, root / 'outer-owned',
                deadline=time.monotonic() + 18, cleanup_deadline=time.monotonic() + 30,
                before_go=world.before_go, cancelled=world.cancelled, persist=world.persist,
                observation_lock=binding)
            checked = command.verify_completed(report)
            assert checked['passed'], checked['errors']
            inner = read(root / 'inner-result.json')
            assert command.verify_completed(inner)['passed']
        wait, classified = read(root / 'outer-wait.json'), read(root / 'inner-classified.json')
        assert wait['caller'] == '_observation_fence' and wait['candidate_uid'] == 0
        assert wait['monotonic'] <= classified['monotonic']
        assert classified['selector_ran'] and classified['marker_present_during_selector']
        assert Path(binding['path']).read_bytes() == b''
        assert not (root / 'inner-active.json').exists()
        for name, result in (('outer-owned', report), ('inner-owned', inner)):
            assert result['observation_lock'] == binding
            assert result['observation_coordination'] == {'failed': False}
            assert result['passed'] and not result['errors'] and not result['signals']
            assert result['child']['exit_code'] == result['owner_exit'] == 0
            assert all(result[key] for key in ('cleanup_verified', 'owner_reaped', 'owner_absent',
                'group_empty', 'pipes_eof', 'readers_joined', 'stdin_closed', 'output_files_closed'))
            assert read(root / name / 'receipt.json') == result
            assert all(row['synthetic_observation_leaf'] for row in result['census_commands'])
        assert (root / 'inner-owned/stdout.log').read_text() == 'nested harmless payload\n'
        assert world.signal_attempts == []
        assert read(root / 'inner-done.json')['signal_attempts'] == []
        for callbacks in (world.callback_events, read(root / 'inner-done.json')['callbacks']):
            assert 'before_go' in callbacks and 'cancelled' in callbacks and 'persist:complete' in callbacks
    finally:
        world.finish_owned_handles()


if __name__ == '__main__':
    assert len(sys.argv) == 4 and sys.argv[1] == '--inner'
    raise SystemExit(inner_entry(Path(sys.argv[2]), json.loads(sys.argv[3])))
