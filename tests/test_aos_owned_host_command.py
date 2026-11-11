"""Deterministic census cases: fake Popen, fake clock, no host processes."""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import types

import pytest

from agentic_tools.aos import owned_host_command as command

CONTROLLER = 41001
PS_PID = 41002
UID = 501


def valid_output(*, child_uid=UID, controller_uid=UID):
    return (f'{CONTROLLER} 1 {CONTROLLER} {controller_uid}\n'
            f'{PS_PID} {CONTROLLER} {PS_PID} {child_uid}\n').encode('ascii')


def valid_identity(*, child_uid=UID):
    return {'pid': PS_PID, 'uid': child_uid, 'birth': [100, 1234],
            'parent': CONTROLLER, 'group': PS_PID, 'status': 2}


class Pipe:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, clock, actions):
        self.pid = PS_PID
        self.returncode = None
        self.stdout, self.stderr = Pipe(), Pipe()
        self.clock, self.actions = clock, list(actions)
        self.communicate_timeouts = []
    def communicate(self, timeout):
        self.communicate_timeouts.append(timeout)
        if not self.actions:
            raise AssertionError('Unexpected extra communicate call')
        action = self.actions.pop(0)
        self.clock.now += action.get('advance', .01)
        if 'code' in action:
            self.returncode = action['code']
        if action.get('timeout'):
            raise subprocess.TimeoutExpired('/bin/ps', timeout)
        if action.get('error') is not None:
            raise action['error']
        return action.get('stdout', valid_output()), action.get('stderr', b'')


@pytest.fixture
def harness(monkeypatch):
    """Replace every host operation; no real process can be constructed."""
    module = command
    native = types.SimpleNamespace()
    native.identity = lambda pid: None
    native.same = lambda saved, current: current is not None and all(
        saved[key] == current[key] for key in ('pid', 'uid', 'birth'))
    monkeypatch.setattr(module, 'native_processes', native)
    monkeypatch.setattr(module, '_RETAINED_PROCESSES', [])
    clock = types.SimpleNamespace(now=0.0)
    signals, launches = [], []
    monkeypatch.setattr(module, 'time', types.SimpleNamespace(monotonic=lambda: clock.now))
    state = types.SimpleNamespace(module=module, clock=clock, signals=signals,
        launches=launches, audit=[], process=None, effective_uid=UID, lstats=[],
        metadata={'st_dev': 12, 'st_ino': 34, 'st_mode': 0o100755, 'st_uid': 0, 'st_gid': 0},
        metadata_sequence=None)
    def lstat(path):
        assert str(path) == '/bin/ps', 'Only the fixed system ps metadata may be exempted'
        state.lstats.append(str(path))
        if state.metadata_sequence:
            current = state.metadata_sequence.pop(0)
            state.metadata = current
        else:
            current = state.metadata
        if isinstance(current, BaseException):
            raise current
        return types.SimpleNamespace(**current)
    monkeypatch.setattr(module, 'os', types.SimpleNamespace(getpid=lambda: CONTROLLER, getuid=lambda: UID,
        geteuid=lambda: state.effective_uid, getppid=lambda: 1, getpgrp=lambda: CONTROLLER,
        lstat=lstat, kill=lambda pid, sig: signals.append((pid, sig))))
    def configure(identity=None, actions=None):
        process = FakeProcess(clock, actions if actions is not None else [{'code': 0}])
        state.process = process
        def read_identity(pid):
            assert pid == PS_PID
            if isinstance(identity, BaseException):
                raise identity
            return identity
        native.identity = read_identity
        def popen(argv, **kwargs):
            launches.append((argv, kwargs))
            assert argv == ['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=']
            assert kwargs['start_new_session'] is True
            return process
        monkeypatch.setattr(module, 'subprocess', types.SimpleNamespace(Popen=popen,
            DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
        return process
    state.configure = configure
    return state


def rejected(harness, *, deadline=5):
    with pytest.raises((RuntimeError, TimeoutError, ValueError, OSError)):
        harness.module._observe(deadline, harness.audit)
    assert len(harness.launches) == 1
    assert len(harness.audit) == 1
    assert harness.audit[0]['output_valid'] is False
    assert harness.audit[0]['ownership_basis'] is None
    return harness.audit[0]


@pytest.mark.parametrize('identity', [None, PermissionError(errno.EPERM, 'identity unavailable'),
                                     PermissionError(errno.EACCES, 'identity unavailable')])
def test_unavailable_identity_requires_clean_direct_reaping(harness, identity):
    process = harness.configure(identity=identity)
    rows = harness.module._observe(5, harness.audit)
    assert rows[PS_PID] == {'parent': CONTROLLER, 'group': PS_PID, 'uid': UID}
    assert rows[CONTROLLER]['uid'] == UID
    record = harness.audit[0]
    assert record['owner'] is None
    assert record['reaped'] and record['pipes_eof'] and record['exit_code'] == 0
    assert not record['errors'] and not record['interrupted']
    assert record['identity_diagnostic'] is not None
    assert record['output_valid'] is True
    assert record['ownership_basis'].startswith('direct-child-spawn-and-reap')
    if isinstance(identity, PermissionError):
        assert 'PermissionError' in json.dumps(record)
    assert process.stdout.closed and process.stderr.closed
    assert not harness.signals and not harness.module._RETAINED_PROCESSES


def test_known_valid_identity_still_accepts_clean_ps(harness):
    harness.configure(identity=valid_identity())
    rows = harness.module._observe(5, harness.audit)
    assert set(rows) == {PS_PID, CONTROLLER}
    assert harness.audit[0]['owner'] == valid_identity()
    assert not harness.signals


@pytest.mark.parametrize('identity', [None, PermissionError(errno.EPERM, 'identity unavailable')])
def test_unavailable_identity_running_after_timeout_stays_owned_and_unknown(harness, identity):
    process = harness.configure(identity=identity, actions=[{'timeout': True}, {'timeout': True}])
    record = rejected(harness)
    assert not record['reaped'] and not record['pipes_eof']
    assert process in harness.module._RETAINED_PROCESSES
    assert not harness.signals, 'Missing identity never authorizes SIGINT'


def test_timeout_stays_failed_when_unidentified_ps_later_exits_zero(harness):
    harness.configure(actions=[{'timeout': True}, {'code': 0}])
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0 and record['pipes_eof']
    assert not harness.signals


def test_late_zero_exit_cannot_establish_census(harness):
    harness.configure(actions=[{'code': 0, 'advance': 6}])
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0
    assert not harness.signals


def test_interrupted_zero_exit_remains_failure(harness):
    harness.configure(identity=valid_identity(), actions=[{'timeout': True}, {'code': 0}])
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0
    assert record['interrupted'] is True
    assert harness.signals == [(PS_PID, signal.SIGINT)]


@pytest.mark.parametrize('field,value', [('pid', PS_PID + 9), ('uid', UID + 1),
                                         ('parent', CONTROLLER + 1), ('group', PS_PID + 1)])
def test_actual_identity_mismatch_is_hard_even_with_clean_exit(harness, field, value):
    identity = valid_identity()
    identity[field] = value
    harness.configure(identity=identity)
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0
    assert not harness.signals, 'A mismatched identity never authorizes SIGINT'


@pytest.mark.parametrize('error', [OSError(errno.EIO, 'identity failed'), RuntimeError('identity mismatch')])
def test_other_identity_errors_are_not_reclassified(harness, error):
    harness.configure(identity=error)
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0
    assert not harness.signals


@pytest.mark.parametrize('code', [1, 65, -signal.SIGINT])
def test_nonzero_ps_exit_stays_failure(harness, code):
    harness.configure(actions=[{'code': code}])
    record = rejected(harness)
    assert record['exit_code'] == code and record['reaped']


@pytest.mark.parametrize('output', [
    b'', b'\n', b'not a ps row\n', b'1 2 3 4 5\n', b'1 x 3 4\n', b'\xff\n',
    valid_output() + f'{PS_PID} {CONTROLLER} {PS_PID} {UID}\n'.encode(),
    f'{PS_PID} {CONTROLLER} {PS_PID} {UID}\n'.encode(),
    f'{CONTROLLER} 1 {CONTROLLER} {UID}\n'.encode(),
    valid_output() + b'-1 1 1 0\n', valid_output() + b'99 -1 99 0\n',
    valid_output() + b'99 1 -1 0\n', valid_output() + b'99 1 99 -1\n',
])
def test_invalid_or_incomplete_ps_report_is_never_a_census(harness, output):
    harness.configure(actions=[{'code': 0, 'stdout': output}])
    record = rejected(harness)
    assert record['reaped'] and record['exit_code'] == 0
    assert not harness.signals


@pytest.mark.parametrize('parent,group,uid', [(CONTROLLER + 1, PS_PID, UID),
                                             (CONTROLLER, PS_PID + 1, UID),
                                             (CONTROLLER, PS_PID, UID + 1)])
def test_ps_self_row_must_match_exact_child_relationship(harness, parent, group, uid):
    output = (f'{CONTROLLER} 1 {CONTROLLER} {UID}\n{PS_PID} {parent} {group} {uid}\n').encode()
    harness.configure(actions=[{'code': 0, 'stdout': output}])
    rejected(harness)


@pytest.mark.parametrize('parent,group,uid', [(2, CONTROLLER, UID),
                                             (1, CONTROLLER + 1, UID),
                                             (1, CONTROLLER, UID + 1)])
def test_controller_row_must_match_current_relationship(harness, parent, group, uid):
    output = (f'{CONTROLLER} {parent} {group} {uid}\n'
              f'{PS_PID} {CONTROLLER} {PS_PID} {UID}\n').encode()
    harness.configure(actions=[{'code': 0, 'stdout': output}])
    rejected(harness)


def test_stderr_prevents_clean_census(harness):
    harness.configure(actions=[{'code': 0, 'stderr': b'warning: partial process report\n'}])
    record = rejected(harness)
    assert record['stderr_bytes'] > 0


def test_unreaped_child_with_eof_cannot_establish_census(harness):
    process = harness.configure(actions=[{'code': None}, {'code': None}])
    record = rejected(harness)
    assert not record['reaped']
    assert process in harness.module._RETAINED_PROCESSES
    assert not harness.signals


def test_reaped_child_without_complete_pipes_cannot_establish_census(harness):
    harness.configure(actions=[{'code': 0, 'timeout': True}])
    record = rejected(harness)
    assert record['reaped'] and not record['pipes_eof']


def test_payload_identity_permission_error_remains_fatal(harness):
    leader = {'pid': 42000, 'uid': UID, 'birth': [100, 2], 'parent': CONTROLLER,
              'group': 42000, 'status': 2}
    candidate = 42001
    def identity(pid):
        if pid == leader['pid']:
            return leader
        raise PermissionError(errno.EPERM, 'payload identity unavailable')
    rows = {candidate: {'parent': leader['pid'], 'group': leader['pid'], 'uid': UID}}
    with pytest.raises(PermissionError):
        harness.module.select_owned(rows, leader, {}, identity_reader=identity)
    assert not harness.launches and not harness.signals


def test_expired_deadline_never_starts_ps(harness):
    harness.configure()
    with pytest.raises(TimeoutError):
        harness.module._observe(0, harness.audit)
    assert not harness.launches and not harness.audit and not harness.signals


def test_signal_identity_error_does_not_skip_direct_reaping(harness):
    harness.configure(identity=valid_identity(), actions=[{'timeout': True}, {'code': 0}])
    observations = iter([valid_identity(), PermissionError(errno.EPERM, 'identity changed during cleanup')])
    def read_identity(pid):
        assert pid == PS_PID
        value = next(observations)
        if isinstance(value, BaseException):
            raise value
        return value
    harness.module.native_processes.identity = read_identity
    record = rejected(harness)
    assert record['reaped'] and record['pipes_eof'] and record['exit_code'] == 0
    assert not harness.signals
    assert any('signal ownership' in error for error in record['errors'])


@pytest.mark.parametrize('identity', [None, PermissionError(errno.EPERM, 'setuid ps identity unavailable'),
                                     valid_identity(child_uid=0)])
def test_exact_setuid_root_ps_accepts_its_effective_uid(harness, identity):
    harness.metadata['st_mode'] = 0o104755
    harness.configure(identity=identity, actions=[{'code': 0, 'stdout': valid_output(child_uid=0)}])
    rows = harness.module._observe(5, harness.audit)
    record = harness.audit[0]
    assert rows[PS_PID]['uid'] == 0 and rows[CONTROLLER]['uid'] == UID
    assert record['executable_metadata']['expected_effective_uid'] == 0
    assert record['executable_metadata']['mode'] == 0o104755
    assert record['reaped'] and record['pipes_eof'] and record['output_valid']
    assert not record['errors'] and not harness.signals
    assert harness.lstats == ['/bin/ps', '/bin/ps']


def test_normal_ps_uses_controllers_effective_not_real_uid(harness):
    harness.effective_uid = 0
    harness.configure(identity=valid_identity(child_uid=0),
        actions=[{'code': 0, 'stdout': valid_output(child_uid=0, controller_uid=0)}])
    rows = harness.module._observe(5, harness.audit)
    assert rows[PS_PID]['uid'] == 0 and rows[CONTROLLER]['uid'] == 0
    assert harness.audit[0]['executable_metadata']['expected_effective_uid'] == 0
    assert harness.audit[0]['executable_metadata']['mode'] == 0o100755
    assert not harness.signals


@pytest.mark.parametrize('metadata_change', [
    {'st_mode': 0o120777},  # Symlink, not the fixed regular executable.
    {'st_mode': 0o040755},  # Directory.
    {'st_uid': UID}, {'st_gid': 20},
    {'st_mode': 0o100775}, {'st_mode': 0o100757},
    {'st_mode': 0o102755}, {'st_mode': 0o106755},
    {'st_mode': 0o100644}, {'st_mode': 0o101755},
])
def test_untrusted_ps_metadata_rejected_before_any_launch(harness, metadata_change):
    harness.metadata.update(metadata_change)
    harness.configure()
    with pytest.raises((RuntimeError, ValueError, OSError)):
        harness.module._observe(5, harness.audit)
    assert not harness.launches and not harness.signals
    assert not harness.module._RETAINED_PROCESSES
    assert len(harness.audit) == 1 and not harness.audit[0]['output_valid']


def test_missing_ps_metadata_rejected_before_launch(harness):
    harness.metadata_sequence = [FileNotFoundError('/bin/ps')]
    harness.configure()
    with pytest.raises((RuntimeError, ValueError, OSError)):
        harness.module._observe(5, harness.audit)
    assert not harness.launches and not harness.signals


@pytest.mark.parametrize('mode,wrong_uid', [(0o104755, UID), (0o100755, 0)])
def test_child_row_uid_must_match_executable_metadata_even_after_clean_exit(harness, mode, wrong_uid):
    harness.metadata['st_mode'] = mode
    harness.configure(identity=PermissionError(errno.EPERM, 'identity unavailable'),
                      actions=[{'code': 0, 'stdout': valid_output(child_uid=wrong_uid)}])
    record = rejected(harness)
    assert record['exit_code'] == 0 and record['reaped'] and record['pipes_eof']
    assert not harness.signals


@pytest.mark.parametrize('mode,wrong_uid', [(0o104755, UID), (0o100755, 0)])
def test_actual_child_identity_uid_mismatch_remains_hard(harness, mode, wrong_uid):
    harness.metadata['st_mode'] = mode
    correct_uid = 0 if mode == 0o104755 else UID
    harness.configure(identity=valid_identity(child_uid=wrong_uid),
                      actions=[{'code': 0, 'stdout': valid_output(child_uid=correct_uid)}])
    record = rejected(harness)
    assert record['exit_code'] == 0 and record['reaped']
    assert not harness.signals


@pytest.mark.parametrize('field,value', [('st_dev', 13), ('st_ino', 35),
    ('st_mode', 0o104755), ('st_uid', 1), ('st_gid', 1)])
def test_ps_metadata_drift_rejects_otherwise_clean_observation(harness, field, value):
    before = dict(harness.metadata)
    after = {**before, field: value}
    harness.metadata_sequence = [before, after]
    harness.configure()
    record = rejected(harness)
    assert record['reaped'] and record['pipes_eof'] and record['exit_code'] == 0
    assert harness.lstats == ['/bin/ps', '/bin/ps']
    assert not harness.signals


def test_setuid_ps_without_birth_identity_still_cannot_be_signaled(harness):
    harness.metadata['st_mode'] = 0o104755
    process = harness.configure(identity=PermissionError(errno.EPERM, 'setuid ps identity unavailable'),
                                actions=[{'timeout': True}, {'timeout': True}])
    record = rejected(harness)
    assert not record['reaped'] and process in harness.module._RETAINED_PROCESSES
    assert not harness.signals


OWNER_PID = 52002
PAYLOAD_PID = 52003


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    """One held fake supervisor; files are real fixtures, processes are all fake."""
    state = types.SimpleNamespace(now=100.0, events=[], persisted=[], signals=[], launches=[],
        waits=[], census_deadlines=[], census_cleanup_deadlines=[], observations=0, first_scan_advance=0.0,
        finish_on_go=True, command_exit=0, payload_live=False, admitted=False, process=None,
        pipe_eof=True, spawn_error=None, exit_on_release=False, control=tmp_path / 'command', cwd=tmp_path,
        deadline=110.0, extra_identities={})
    uid = os.geteuid()
    def identity_record(pid, parent, group, birth):
        return {'pid': pid, 'uid': uid, 'birth': birth, 'parent': parent,
                'group': group, 'status': 2}
    state.controller = identity_record(CONTROLLER, 1, CONTROLLER, [1000, 1])
    state.owner = identity_record(OWNER_PID, CONTROLLER, OWNER_PID, [1000, 2])
    state.payload = identity_record(PAYLOAD_PID, OWNER_PID, OWNER_PID, [1000, 3])
    def identity(pid):
        if pid in state.extra_identities:
            value = state.extra_identities[pid]
            if isinstance(value, BaseException):
                raise value
            return value
        if pid == CONTROLLER:
            return state.controller
        if pid == OWNER_PID and state.process is not None and state.process.returncode is None:
            return state.owner
        if pid == PAYLOAD_PID and state.payload_live:
            return state.payload
        return None
    def same(saved, current):
        return current is not None and all(saved[key] == current[key] for key in ('pid', 'uid', 'birth'))
    native = types.SimpleNamespace(identity=identity, same=same)
    monkeypatch.setattr(command, 'native_processes', native)
    monkeypatch.setattr(command, '_RETAINED_PROCESSES', [])
    def sleep(seconds):
        assert seconds >= 0
        state.now += seconds
    monkeypatch.setattr(command, 'time', types.SimpleNamespace(monotonic=lambda: state.now,
        time=lambda: 10000 + state.now, sleep=sleep))
    def finish(code=0):
        (state.control / 'status.json').write_text(json.dumps({'exit_code': code}))
        state.payload_live = False
    def kill(pid, sig):
        state.events.append(('signal', pid))
        state.signals.append((pid, sig))
        assert sig == signal.SIGINT, 'Only positively owned SIGINT is permitted'
        if pid == PAYLOAD_PID:
            finish()
    fake_os = types.SimpleNamespace(**vars(os))
    fake_os.getpid, fake_os.getppid = lambda: CONTROLLER, lambda: 1
    fake_os.getpgrp, fake_os.kill = lambda: CONTROLLER, kill
    monkeypatch.setattr(command, 'os', fake_os)
    class Input(Pipe):
        def write(self, value):
            state.events.append(('stdin', value))
            assert value == b'G'
            state.admitted = True
            if state.finish_on_go:
                finish(state.command_exit)
            else:
                state.payload_live = True
            return len(value)
        def flush(self):
            pass
    class Supervisor:
        pid = OWNER_PID
        def __init__(self):
            self.returncode = None
            self.stdin, self.stdout, self.stderr = Input(), Pipe(), Pipe()
        def wait(self, timeout):
            state.waits.append(timeout)
            assert timeout > 0
            assert not state.admitted or (state.control / 'release').exists()
            self.returncode = 0
            return 0
    def popen(argv, **kwargs):
        state.launches.append((argv, kwargs))
        assert kwargs['start_new_session'] is True
        assert argv[1:3] == ['-I', '-B']
        assert Path(argv[3]).name == 'owned_host_supervisor.py'
        if state.spawn_error:
            raise state.spawn_error
        state.process = Supervisor()
        return state.process
    monkeypatch.setattr(command, 'subprocess', types.SimpleNamespace(Popen=popen,
        PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    class Reader:
        def __init__(self, target, args, **kwargs):
            self.target, self.args, self.ident = target, args, None
        def start(self):
            self.ident = 1
            self.target(*self.args)
        def join(self, timeout):
            assert timeout >= 0
        def is_alive(self):
            return False
    monkeypatch.setattr(command, 'threading', types.SimpleNamespace(
        Thread=Reader, Event=command.threading.Event))
    def drain(pipe, log, stream, stopped):
        stream['eof'] = state.pipe_eof
    monkeypatch.setattr(command, '_drain', drain)
    def observe(until, audit, *, cleanup_deadline=None):
        if cleanup_deadline is not None:
            state.census_cleanup_deadlines.append(cleanup_deadline)
            assert until <= cleanup_deadline <= state.deadline + 26
        if state.exit_on_release and (state.control / 'release').exists():
            state.extra_identities[OWNER_PID] = None
        state.observations += 1
        state.census_deadlines.append(until)
        assert until > state.now
        if state.observations == 1:
            state.now += state.first_scan_advance
        rows = {}
        for pid in (CONTROLLER, OWNER_PID, PAYLOAD_PID):
            current = identity(pid)
            if current is not None:
                rows[pid] = {key: current[key] for key in ('parent', 'group', 'uid')}
        audit.append({'reaped': True, 'pipes_eof': True, 'errors': []})
        return rows
    monkeypatch.setattr(command, '_observe', observe)
    def persist(snapshot):
        state.events.append(('persist', snapshot['phase']))
        state.persisted.append(json.loads(json.dumps(snapshot)))
    def run(**overrides):
        arguments = {'deadline': state.deadline, 'persist': persist}
        arguments.update(overrides)
        return command.run_owned(['/fake/check', 'literal $arg'], state.cwd,
            {'PATH': '/fake/bin', 'VALUE': 'literal'}, state.control, **arguments)
    state.run, state.persist, state.finish = run, persist, finish
    return state


@pytest.mark.parametrize('code', [1, 101, -signal.SIGINT])
def test_completed_nonzero_command_is_not_a_lifecycle_error(lifecycle, code):
    lifecycle.command_exit = code
    report = lifecycle.run()
    assert report['child']['exit_code'] == code
    assert not report['passed'] and report['cleanup_verified']
    assert report['phase'] == 'complete' and report['errors'] == []
    assert not report['cancel_requested'] and not report['timed_out']
    assert report['owner_reaped'] and report['pipes_eof']
    assert not command.verify_completed(report)['passed']


def test_clean_owned_run_saves_identity_and_admission_before_g(lifecycle):
    report = lifecycle.run()
    assert report['backend'] == 'host-owned-v1'
    assert report['passed'] and report['cleanup_verified'] and report['phase'] == 'complete'
    assert report['owner_reaped'] and report['pipes_eof'] and report['group_empty']
    assert report['deadline_monotonic'] == 110 and report['cleanup_deadline_monotonic'] == 136
    assert report['controller'] == lifecycle.controller and report['owner'] == lifecycle.owner
    assert lifecycle.events.index(('persist', 'held')) < lifecycle.events.index(('stdin', b'G'))
    assert lifecycle.events.index(('persist', 'admitting')) < lifecycle.events.index(('stdin', b'G'))
    held = next(row for row in lifecycle.persisted if row['phase'] == 'held')
    assert held['owner'] == lifecycle.owner and held['controller'] == lifecycle.controller
    assert held['argv'] == ['/fake/check', 'literal $arg'] and held['environment_sha256']
    assert held['deadline_monotonic'] == report['deadline_monotonic']
    assert len(lifecycle.launches) == 1 and not lifecycle.signals
    assert json.loads((lifecycle.control / 'receipt.json').read_text()) == report


def test_expired_original_deadline_never_constructs_supervisor(lifecycle):
    report = lifecycle.run(deadline=lifecycle.now)
    assert report['timed_out'] and not report['passed']
    assert report['no_launch'] and report['cleanup_verified']
    assert not lifecycle.launches and not lifecycle.admitted


@pytest.mark.parametrize('consume_at', ['before_go', 'admission_persistence'])
def test_preparation_and_persistence_consume_original_deadline(lifecycle, consume_at):
    def before_go(deadline):
        assert deadline == lifecycle.deadline
        if consume_at == 'before_go':
            lifecycle.now = deadline
    def persist(snapshot):
        lifecycle.persist(snapshot)
        if consume_at == 'admission_persistence' and snapshot['phase'] == 'admitting':
            lifecycle.now = lifecycle.deadline
    report = lifecycle.run(before_go=before_go, persist=persist)
    assert report['timed_out'] and not report['passed'] and report['cleanup_verified']
    assert not report['launch_authorized'] and not lifecycle.admitted
    assert report['deadline_monotonic'] == 110 and report['cleanup_deadline_monotonic'] == 136


def test_zero_status_after_census_deadline_is_still_timeout(lifecycle):
    lifecycle.first_scan_advance = 10.1
    report = lifecycle.run()
    assert report['timed_out'] and not report['passed']
    assert report['child']['exit_code'] == 0 and report['cleanup_verified']
    assert max(lifecycle.census_deadlines) <= report['cleanup_deadline_monotonic']


def test_exhausted_cleanup_cap_is_not_renewed(lifecycle):
    def before_go(deadline):
        lifecycle.now = deadline + 27
    report = lifecycle.run(before_go=before_go)
    assert report['timed_out'] and not report['passed'] and not report['cleanup_verified']
    assert report['phase'] == 'cleanup_unknown' and report['cleanup_deadline_monotonic'] == 136
    assert not lifecycle.admitted and not lifecycle.waits
    assert lifecycle.process in command._RETAINED_PROCESSES


@pytest.mark.parametrize('mechanism', ['callback', 'marker'])
@pytest.mark.parametrize('when', ['before_g', 'after_g'])
def test_cancellation_stays_failed_even_when_child_later_exits_zero(lifecycle, mechanism, when):
    lifecycle.finish_on_go = False
    def cancelled():
        return mechanism == 'callback' and (when == 'before_g' or lifecycle.admitted)
    def persist(snapshot):
        lifecycle.persist(snapshot)
        if mechanism == 'marker' and snapshot['phase'] == ('held' if when == 'before_g' else 'running'):
            command.request_cancel(lifecycle.control)
    report = lifecycle.run(cancelled=cancelled, persist=persist)
    assert report['cancel_requested'] and not report['passed'] and report['cleanup_verified']
    if when == 'before_g':
        assert not report['launch_authorized'] and not lifecycle.signals
    else:
        assert report['launch_authorized'] and report['child']['exit_code'] == 0
        assert lifecycle.signals == [(PAYLOAD_PID, signal.SIGINT)]
        assert report['signals'][0]['birth'] == lifecycle.payload['birth']


@pytest.mark.parametrize('when', ['before_g', 'after_g'])
def test_cancellation_callback_exception_is_sticky(lifecycle, when):
    lifecycle.finish_on_go = False
    def cancelled():
        if when == 'before_g' or lifecycle.admitted:
            raise RuntimeError('cancel callback unavailable')
        return False
    report = lifecycle.run(cancelled=cancelled)
    assert not report['passed'] and report['cleanup_verified']
    assert any('cancel callback unavailable' in row for row in report['errors'])
    assert report['launch_authorized'] is (when == 'after_g')


@pytest.mark.parametrize('phase', ['held', 'admitting', 'running', 'complete'])
def test_persistence_failure_never_becomes_success(lifecycle, phase):
    failed = False
    def persist(snapshot):
        nonlocal failed
        lifecycle.persist(snapshot)
        if snapshot['phase'] == phase and not failed:
            failed = True
            raise OSError('retained progress store failed')
    report = lifecycle.run(persist=persist)
    assert failed and not report['passed'] and report['cleanup_verified']
    assert report['launch_authorized'] is (phase in ('running', 'complete'))
    assert any('retained progress store failed' in row for row in report['errors'])
    assert not json.loads((lifecycle.control / 'receipt.json').read_text())['passed']


def test_before_go_failure_never_admits_payload(lifecycle):
    def before_go(deadline):
        raise RuntimeError('boundary changed')
    report = lifecycle.run(before_go=before_go)
    assert not report['boundary_rechecked'] and not report['launch_authorized']
    assert report['cleanup_verified'] and not report['passed'] and not lifecycle.admitted


def test_existing_control_cannot_replay_a_command(lifecycle):
    lifecycle.control.mkdir()
    with pytest.raises(FileExistsError):
        lifecycle.run()
    assert not lifecycle.launches


def test_ambiguous_popen_failure_is_not_positive_no_launch(lifecycle):
    lifecycle.spawn_error = OSError('spawn boundary failed without a handle')
    report = lifecycle.run()
    assert not report['no_launch'] and not report['cleanup_verified'] and not report['passed']
    assert report['phase'] == 'cleanup_unknown'


def test_missing_pipe_eof_stays_cleanup_unknown(lifecycle):
    lifecycle.pipe_eof = False
    report = lifecycle.run()
    assert report['owner_reaped'] and report['group_empty']
    assert not report['pipes_eof'] and not report['cleanup_verified'] and not report['passed']


@pytest.mark.parametrize('cleanup_cap,expected', [(115.0, 115.0), (999.0, 136.0)])
def test_cleanup_cap_can_only_reduce_original_reserve(lifecycle, cleanup_cap, expected):
    def before_go(deadline):
        lifecycle.now = deadline + 4
    report = lifecycle.run(cleanup_deadline=cleanup_cap, before_go=before_go)
    assert report['timed_out'] and not report['passed'] and report['cleanup_verified']
    assert report['deadline_monotonic'] == 110
    assert report['cleanup_deadline_monotonic'] == expected
    assert all(limit <= expected for limit in lifecycle.census_deadlines)
    assert all(limit <= expected for limit in lifecycle.census_cleanup_deadlines)
    assert all(timeout <= expected - lifecycle.now for timeout in lifecycle.waits)


@pytest.mark.parametrize('cleanup_cap', [109.0, float('nan'), float('inf'), True, '115'])
def test_invalid_cleanup_cap_is_rejected_before_launch(lifecycle, cleanup_cap):
    with pytest.raises(ValueError):
        lifecycle.run(cleanup_deadline=cleanup_cap)
    assert not lifecycle.launches and not lifecycle.control.exists()



def retained_running(lifecycle):
    """A prior controller's snapshot; creating it does not replay its command."""
    lifecycle.control.mkdir(mode=0o700)
    lifecycle.process = types.SimpleNamespace(returncode=None)
    return {'format': 'owned-host-command-v1', 'backend': 'host-owned-v1',
        'phase': 'running', 'argv': ['/fake/check'], 'cwd': str(lifecycle.cwd.resolve()),
        'control': str(lifecycle.control.resolve()), 'environment_sha256': 'f' * 64,
        'controller': lifecycle.controller, 'owner': lifecycle.owner,
        'deadline_monotonic': lifecycle.deadline, 'cleanup_deadline_monotonic': lifecycle.deadline + 26,
        'passed': False, 'cleanup_verified': False, 'launch_authorized': True,
        'owner_reaped': False, 'pipes_eof': False, 'group_empty': False,
        'observed': {}, 'owned_remaining': {}, 'child': None, 'streams': [],
        'cancel_requested': False, 'timed_out': False, 'errors': []}


@pytest.mark.parametrize('phase', ['running', 'cleanup_unknown'])
def test_restart_with_live_controller_only_requests_its_marker(lifecycle, phase):
    snapshot = retained_running(lifecycle)
    snapshot['phase'] = phase
    report = command.reconcile_owned(snapshot, deadline=120, cancel=True, persist=lifecycle.persist)
    assert report['phase'] == phase and report['cancel_requested']
    assert (lifecycle.control / 'cancel').is_file()
    assert not report['cleanup_verified'] and not report['owner_reaped'] and not report['pipes_eof']
    assert not lifecycle.launches and not lifecycle.signals and not lifecycle.census_deadlines
    command.request_cancel(lifecycle.control)  # Repeated cancellation stays the same request.
    assert (lifecycle.control / 'cancel').stat().st_size == 0


@pytest.mark.parametrize('current', ['absent', 'reused', 'unreadable'])
def test_restart_never_signals_unproven_identity_or_invents_io_proof(lifecycle, current):
    snapshot = retained_running(lifecycle)
    snapshot['observed'] = {str(PAYLOAD_PID): lifecycle.payload}
    lifecycle.extra_identities[CONTROLLER] = None
    if current == 'absent':
        lifecycle.extra_identities.update({OWNER_PID: None, PAYLOAD_PID: None})
    elif current == 'reused':
        lifecycle.extra_identities.update({OWNER_PID: {**lifecycle.owner, 'birth': [2000, 2]},
                                          PAYLOAD_PID: {**lifecycle.payload, 'birth': [2000, 3]}})
    else:
        lifecycle.extra_identities[OWNER_PID] = PermissionError(errno.EPERM, 'identity unavailable')
    report = command.reconcile_owned(snapshot, deadline=120, cancel=True)
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified']
    assert not report['owner_reaped'] and not report['pipes_eof'] and not report['passed']
    assert not lifecycle.launches and not lifecycle.signals and not lifecycle.waits
    if current == 'absent':
        assert report['observed_stopped']


def test_restart_can_stop_exact_retained_birth_without_claiming_original_reap(lifecycle):
    snapshot = retained_running(lifecycle)
    snapshot['observed'] = {str(PAYLOAD_PID): lifecycle.payload}
    lifecycle.extra_identities[CONTROLLER] = None
    lifecycle.payload_live = True
    lifecycle.exit_on_release = True
    report = command.reconcile_owned(snapshot, deadline=120, cancel=True, persist=lifecycle.persist)
    assert lifecycle.signals == [(PAYLOAD_PID, signal.SIGINT)]
    assert report['reconciliation_signals'][0]['birth'] == lifecycle.payload['birth']
    before_signal = lifecycle.events.index(('signal', PAYLOAD_PID))
    assert any(event == ('persist', 'cleanup_unknown') for event in lifecycle.events[:before_signal])
    assert report['observed_stopped'] and report['child']['exit_code'] == 0
    assert not report['owner_reaped'] and not report['pipes_eof'] and not report['cleanup_verified']
    assert not report['passed'] and not lifecycle.launches and not lifecycle.waits


def test_restart_persistence_failure_prevents_signal(lifecycle):
    snapshot = retained_running(lifecycle)
    snapshot['observed'] = {str(PAYLOAD_PID): lifecycle.payload}
    lifecycle.extra_identities[CONTROLLER] = None
    lifecycle.payload_live = True
    def persist(report):
        raise OSError('recovery ownership evidence could not be saved')
    report = command.reconcile_owned(snapshot, deadline=120, cancel=True, persist=persist)
    assert not report['cleanup_verified'] and not report['passed']
    assert any('recovery ownership evidence could not be saved' in value for value in report['errors'])
    assert not lifecycle.signals and not lifecycle.launches


def test_restart_does_not_extend_original_cleanup_allowance(lifecycle):
    snapshot = retained_running(lifecycle)
    lifecycle.now = snapshot['cleanup_deadline_monotonic'] + 1
    report = command.reconcile_owned(snapshot, deadline=999, cancel=True)
    assert report['reconciliation_deadline_monotonic'] == snapshot['cleanup_deadline_monotonic']
    assert not report['cleanup_verified'] and not report['passed']
    assert any('Original cleanup deadline expired' in value for value in report['errors'])
    assert not lifecycle.census_deadlines and not lifecycle.signals and not lifecycle.launches


def test_restart_caller_can_shorten_observation_budget(lifecycle):
    snapshot = retained_running(lifecycle)
    lifecycle.extra_identities.update({CONTROLLER: None, OWNER_PID: None})
    report = command.reconcile_owned(snapshot, deadline=102)
    assert report['reconciliation_deadline_monotonic'] == 102
    assert lifecycle.census_deadlines == [102]
    assert not report['cleanup_verified'] and not report['owner_reaped'] and not report['pipes_eof']
    assert not lifecycle.launches


def test_zero_status_and_empty_census_do_not_replace_lost_reap_or_eof(lifecycle):
    snapshot = retained_running(lifecycle)
    lifecycle.extra_identities.update({CONTROLLER: None, OWNER_PID: None})
    lifecycle.finish()
    report = command.reconcile_owned(snapshot, deadline=120)
    assert report['observed_stopped'] and report['owner_absent'] and report['group_empty']
    assert report['recovery_lost_original_io_proof']
    assert not report['cleanup_verified'] and not report['owner_reaped'] and not report['pipes_eof']
    assert not lifecycle.launches and not lifecycle.waits


def test_complete_receipt_is_revalidated_without_replay(lifecycle):
    completed = lifecycle.run()
    launches, censuses = len(lifecycle.launches), len(lifecycle.census_deadlines)
    assert command.verify_completed(completed) == completed
    assert command.reconcile_owned(completed, deadline=120) == completed
    assert len(lifecycle.launches) == launches == 1
    assert len(lifecycle.census_deadlines) == censuses
    assert not lifecycle.signals


@pytest.mark.parametrize('missing', ['owner_reaped', 'pipes_eof', 'readers_joined'])
def test_completed_receipt_requires_original_io_evidence(lifecycle, missing):
    completed = lifecycle.run()
    incomplete = {**completed, missing: False}
    (lifecycle.control / 'receipt.json').write_text(json.dumps(incomplete))
    with pytest.raises(RuntimeError):
        command.verify_completed(incomplete)
    assert len(lifecycle.launches) == 1


def test_restart_terminal_persistence_failure_overrides_stale_success_progress(lifecycle):
    def persist(snapshot):
        if snapshot['phase'] == 'complete':
            raise OSError('terminal storage failure')
    report = lifecycle.run(persist=persist)
    assert not report['passed'] and report['cleanup_verified']
    progress = json.loads((lifecycle.control / 'progress.json').read_text())
    # Even if progress was written before the callback failed, final evidence wins.
    recovered = command.reconcile_owned(progress, deadline=120)
    assert not recovered['passed'] and recovered['cleanup_verified']
    assert any('terminal storage failure' in value for value in recovered['errors'])
    assert len(lifecycle.launches) == 1


def test_old_progress_cannot_erase_retained_sticky_failure(lifecycle):
    snapshot = retained_running(lifecycle)
    older = json.loads(json.dumps(snapshot))
    snapshot.update(cancel_requested=True, timed_out=True, errors=['Original deadline failure'])
    (lifecycle.control / 'progress.json').write_text(json.dumps(older))
    report = command.reconcile_owned(snapshot, deadline=120)
    assert report['cancel_requested'] and report['timed_out']
    assert 'Original deadline failure' in report['errors'] and not report['passed']
    assert not lifecycle.launches and not lifecycle.signals



def test_census_cannot_signal_or_wait_beyond_explicit_cleanup_deadline(harness):
    process = harness.configure(identity=valid_identity(), actions=[{'timeout': True, 'advance': 5}])
    with pytest.raises(RuntimeError):
        command._observe(5, harness.audit, cleanup_deadline=5)
    assert process.communicate_timeouts == [5]
    assert not harness.signals and process in command._RETAINED_PROCESSES
    assert not harness.audit[0]['reaped'] and not harness.audit[0]['output_valid']


def test_census_reaping_uses_only_remaining_explicit_cleanup_time(harness):
    process = harness.configure(identity=valid_identity(),
        actions=[{'timeout': True, 'advance': 4.8}, {'code': 0}])
    with pytest.raises(RuntimeError):
        command._observe(5, harness.audit, cleanup_deadline=5)
    assert process.communicate_timeouts == pytest.approx([5, .2])
    assert harness.signals == [(PS_PID, signal.SIGINT)]
    assert harness.audit[0]['reaped'] and harness.audit[0]['pipes_eof']
    assert not harness.audit[0]['output_valid']



@pytest.mark.parametrize('consume_at', ['census', 'persistence'])
def test_restart_deadline_is_rechecked_before_any_owned_signal(lifecycle, consume_at):
    snapshot = retained_running(lifecycle)
    snapshot['observed'] = {str(PAYLOAD_PID): lifecycle.payload}
    lifecycle.extra_identities[CONTROLLER] = None
    lifecycle.payload_live = True
    if consume_at == 'census':
        lifecycle.first_scan_advance = 20
    def persist(report):
        lifecycle.persist(report)
        if consume_at == 'persistence':
            lifecycle.now = 120
    report = command.reconcile_owned(snapshot, deadline=120, cancel=True, persist=persist)
    assert not report['passed'] and not report['cleanup_verified']
    assert not lifecycle.signals and not lifecycle.launches
    assert any('deadline expired before owned signal' in value for value in report['errors'])



@pytest.mark.parametrize('operation', ['reconcile', 'verify_completed'])
def test_older_reconciliation_retains_sticky_history_from_both_records(lifecycle, operation):
    newer = retained_running(lifecycle) if operation == 'reconcile' else lifecycle.run()
    previous_launches = len(lifecycle.launches)
    previous_censuses = len(lifecycle.census_deadlines)
    newer.update(checkpoint_monotonic=lifecycle.now + 1,
        errors=['Newer input failure'], timed_out=True, cancel_requested=False,
        observed={str(PAYLOAD_PID): lifecycle.payload},
        reconciliation_census=[{'origin': 'newer', 'reaped': True, 'pipes_eof': True}],
        reconciliation_signals=[{'pid': PAYLOAD_PID, 'birth': lifecycle.payload['birth'], 'signal': 'SIGINT'}])
    extra = {**lifecycle.payload, 'pid': PAYLOAD_PID + 1, 'birth': [999, 4]}
    older = {**newer, 'checkpoint_monotonic': lifecycle.now,
        'errors': ['Older stored failure'], 'timed_out': False, 'cancel_requested': True,
        'observed': {str(extra['pid']): extra},
        'reconciliation_census': [{'origin': 'older', 'reaped': False, 'pipes_eof': False}],
        'reconciliation_signals': [{'pid': extra['pid'], 'birth': extra['birth'], 'signal': 'SIGINT'}]}
    (lifecycle.control / 'reconciliation.json').write_text(json.dumps(older))
    if operation == 'reconcile':
        report = command.reconcile_owned(newer, deadline=120)
    else:
        report = command.verify_completed(newer)
    assert report['checkpoint_monotonic'] == newer['checkpoint_monotonic']
    assert report['cancel_requested'] and report['timed_out'] and not report['passed']
    assert set(report['errors']) == {'Newer input failure', 'Older stored failure'}
    assert report['observed'] == {str(PAYLOAD_PID): lifecycle.payload, str(extra['pid']): extra}
    assert {row['origin'] for row in report['reconciliation_census']} == {'older', 'newer'}
    assert {row['pid'] for row in report['reconciliation_signals']} == {PAYLOAD_PID, extra['pid']}
    assert len(lifecycle.launches) == previous_launches
    assert len(lifecycle.census_deadlines) == previous_censuses
    assert not lifecycle.signals


@pytest.mark.parametrize('owner_state', ['absent', 'reused'])
def test_unrelated_reused_group_number_holds_immediately_without_signals(lifecycle, owner_state):
    snapshot = retained_running(lifecycle)
    lifecycle.extra_identities[CONTROLLER] = None
    lifecycle.extra_identities[OWNER_PID] = (
        None if owner_state == 'absent' else {**lifecycle.owner, 'birth': [2000, 2]})
    lifecycle.extra_identities[PAYLOAD_PID] = {
        **lifecycle.payload, 'birth': [2000, 3], 'parent': 59999, 'group': OWNER_PID}
    started = lifecycle.now
    report = command.reconcile_owned(snapshot, deadline=136, cancel=True)
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified']
    assert not report['passed'] and not report['observed_stopped'] and not report['group_empty']
    assert not report['owned_remaining']
    assert any('unverified reused group number' in row for row in report['errors'])
    assert lifecycle.observations == 1 and lifecycle.now == started
    assert not lifecycle.signals and not lifecycle.launches and not lifecycle.waits
    assert not (lifecycle.control / 'release').exists()


@pytest.mark.parametrize('publication_fails', [False, True])
@pytest.mark.parametrize('callback_fails', [False, True])
def test_command_failure_notification_precedes_cleanup_without_retrying_its_callback(
        lifecycle, monkeypatch, publication_fails, callback_fails):
    state = lifecycle
    state.finish_on_go = False
    early_calls, early_publications = [], []
    original_atomic, original_observe = command._atomic_json, command._observe
    def early(snapshot):
        return (snapshot.get('phase') == 'cleanup_unknown'
                and snapshot.get('controller_finished') is False
                and state.observations == 0)
    def atomic(path, snapshot):
        if Path(path).name == 'progress.json' and early(snapshot):
            early_publications.append(json.loads(json.dumps(snapshot)))
            if publication_fails:
                raise OSError('synthetic early progress publication failure')
        return original_atomic(path, snapshot)
    def observe(until, audit, **kwargs):
        state.events.append(('cleanup-census', state.observations))
        return original_observe(until, audit, **kwargs)
    def persist(snapshot):
        if early(snapshot):
            # Record callback entry even if it raises: an attempted callback
            # cannot safely be retried merely because it reported failure.
            early_calls.append(json.loads(json.dumps(snapshot)))
            state.events.append(('early-failure-notification', len(early_calls)))
            assert state.payload_live and state.admitted
            assert not state.signals and not state.waits
            assert not (state.control / 'release').exists()
            assert snapshot['passed'] is False and snapshot['cleanup_verified'] is False
            assert snapshot['controller_finished'] is False and snapshot['owner_reaped'] is False
            assert snapshot['deadline_monotonic'] == state.deadline
            assert snapshot['cleanup_deadline_monotonic'] == state.deadline + 26
            assert any('command: InterruptedError' in error for error in snapshot['errors'])
            saved = json.loads((state.control / 'progress.json').read_text())
            if publication_fails:
                assert saved['phase'] == 'running' and not saved['errors']
                assert any('synthetic early progress publication failure' in error
                           for error in snapshot['errors'])
            else:
                assert saved == snapshot
            if callback_fails:
                raise OSError('synthetic early callback failure')
        state.persist(snapshot)
    monkeypatch.setattr(command, '_atomic_json', atomic)
    monkeypatch.setattr(command, '_observe', observe)
    report = state.run(persist=persist, cancelled=lambda: state.admitted)
    assert not any('AssertionError' in error for error in report['errors'])
    assert len(early_calls) == len(early_publications) == 1
    notify = state.events.index(('early-failure-notification', 1))
    census = next(i for i, event in enumerate(state.events) if event[0] == 'cleanup-census')
    assert notify < census < state.events.index(('signal', PAYLOAD_PID))
    assert state.signals == [(PAYLOAD_PID, signal.SIGINT)]
    assert len(state.launches) == 1 and state.events.count(('stdin', b'G')) == 1
    assert (state.control / 'release').is_file() and state.waits
    assert report['owner_reaped'] and report['cleanup_verified'] and report['controller_finished']
    assert report['phase'] == 'complete' and not report['passed']
    assert report['deadline_monotonic'] == state.deadline
    assert report['cleanup_deadline_monotonic'] == state.deadline + 26
    assert all(until <= report['cleanup_deadline_monotonic'] for until in state.census_deadlines)
    if publication_fails:
        assert any('synthetic early progress publication failure' in error for error in report['errors'])
    if callback_fails:
        assert any('synthetic early callback failure' in error for error in report['errors'])
    assert json.loads((state.control / 'receipt.json').read_text()) == report


def test_before_go_failure_is_notified_before_supervisor_stdin_close_or_reap(lifecycle):
    state, notices = lifecycle, []
    def before_go(deadline):
        raise ValueError('synthetic boundary rejection')
    def persist(snapshot):
        if snapshot['phase'] == 'cleanup_unknown' and not snapshot['controller_finished']:
            notices.append(json.loads(json.dumps(snapshot)))
            assert not state.process.stdin.closed and state.process.returncode is None
            assert not state.admitted and not state.waits and not state.census_deadlines
            assert not state.signals and not (state.control / 'release').exists()
            assert snapshot['launch_authorized'] is False
            assert snapshot['cleanup_verified'] is False and snapshot['owner_reaped'] is False
            assert any('synthetic boundary rejection' in error for error in snapshot['errors'])
        state.persist(snapshot)
    report = state.run(before_go=before_go, persist=persist)
    assert not any('AssertionError' in error for error in report['errors'])
    assert len(notices) == 1
    assert state.process.stdin.closed and state.waits and report['owner_reaped']
    assert not state.admitted and not report['launch_authorized'] and not state.signals
    assert report['cleanup_verified'] and not report['passed']
    assert report['deadline_monotonic'] == state.deadline
    assert report['cleanup_deadline_monotonic'] == state.deadline + 26


def test_failure_notice_retains_uncertain_census_before_next_cleanup_observation(lifecycle, monkeypatch):
    state, notices = lifecycle, []
    state.finish_on_go = False
    original_observe = command._observe
    uncertain = None
    def observe(until, audit, **kwargs):
        nonlocal uncertain
        rows = original_observe(until, audit, **kwargs)
        if state.observations == 1:
            audit[-1].update(reaped=False, pipes_eof=False,
                             errors=['synthetic census reap unavailable'])
            uncertain = json.loads(json.dumps(audit[-1]))
            raise RuntimeError('synthetic first census failure')
        assert notices, 'Failure must be delivered before a second observation can start'
        return rows
    def persist(snapshot):
        if (snapshot['phase'] == 'cleanup_unknown' and not snapshot['controller_finished']
                and state.observations == 1):
            notices.append(json.loads(json.dumps(snapshot)))
            assert not state.signals and not state.waits and not (state.control / 'release').exists()
            assert snapshot['census_count'] == 1 and snapshot['census_commands'] == [uncertain]
            assert snapshot['cleanup_verified'] is False and snapshot['owner_reaped'] is False
            assert any('synthetic first census failure' in error for error in snapshot['errors'])
        state.persist(snapshot)
    monkeypatch.setattr(command, '_observe', observe)
    report = state.run(persist=persist)
    assert not any('AssertionError' in error for error in report['errors'])
    assert len(notices) == 1 and state.observations > 1
    assert state.signals == [(PAYLOAD_PID, signal.SIGINT)] and report['owner_reaped']
    assert (state.control / 'release').is_file()
    assert uncertain in report['census_commands']
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified'] and not report['passed']
    assert report['controller_finished']
    assert report['deadline_monotonic'] == state.deadline
    assert report['cleanup_deadline_monotonic'] == state.deadline + 26


def test_failure_notification_cannot_renew_cleanup_budget_it_consumes(lifecycle):
    state, notices = lifecycle, []
    state.finish_on_go = False
    saved_cleanup_deadline = 111.0
    def persist(snapshot):
        if snapshot['phase'] == 'cleanup_unknown' and not snapshot['controller_finished']:
            notices.append(json.loads(json.dumps(snapshot)))
            assert snapshot['cleanup_deadline_monotonic'] == saved_cleanup_deadline
            state.now = saved_cleanup_deadline
        state.persist(snapshot)
    report = state.run(cleanup_deadline=saved_cleanup_deadline,
                       cancelled=lambda: state.admitted, persist=persist)
    assert state.admitted and state.events.count(('stdin', b'G')) == 1
    assert not any('AssertionError' in error for error in report['errors'])
    assert len(notices) == 1
    assert not state.census_deadlines and not state.signals and not state.waits
    assert not (state.control / 'release').exists()
    assert state.process.stdin.closed and state.process.stdout.closed and state.process.stderr.closed
    assert state.process in command._RETAINED_PROCESSES
    assert report['deadline_monotonic'] == state.deadline
    assert report['cleanup_deadline_monotonic'] == saved_cleanup_deadline
    assert report['phase'] == 'cleanup_unknown' and not report['cleanup_verified'] and not report['passed']
    assert report['controller_finished'] and not report['owner_reaped']


def test_expected_nonzero_result_does_not_emit_command_failure_notification(lifecycle):
    state, notices = lifecycle, []
    state.command_exit = 17
    def persist(snapshot):
        if snapshot['phase'] == 'cleanup_unknown' and not snapshot['controller_finished']:
            notices.append(snapshot)
        state.persist(snapshot)
    report = state.run(persist=persist)
    assert notices == [] and report['errors'] == []
    assert report['phase'] == 'complete' and report['child']['exit_code'] == 17
    assert report['cleanup_verified'] and report['owner_reaped'] and not report['passed']
    assert len(state.launches) == 1 and state.events.count(('stdin', b'G')) == 1
