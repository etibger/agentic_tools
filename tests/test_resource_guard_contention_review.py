"""Independent lock-contention and observation-age resource-guard checks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_processes, resource_limits as limits
from agentic_tools.aos.store import Rejected
from test_aos_resource_limits import guarded
from test_native_qualification_independent import technical
from test_resource_limits_independent_review import configured


def activate(g):
    with g.db:
        return limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True)


@contextmanager
def held_writer(g, *, exclusive=False, change=None):
    entered, release = threading.Event(), threading.Event()
    errors = []
    def lock():
        try:
            with sqlite3.connect(g.path, timeout=1) as db:
                db.execute('BEGIN EXCLUSIVE' if exclusive else 'BEGIN IMMEDIATE')
                if change:
                    change(db)
                entered.set()
                assert release.wait(15)
        except BaseException as error:
            errors.append(error)
            entered.set()
    thread = threading.Thread(target=lock)
    thread.start()
    assert entered.wait(3) and not errors
    try:
        yield release
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and not errors


def test_not_due_sample_does_not_open_writer_or_rewrite_large_guard(guarded):
    g = guarded
    before = activate(g)
    changes, statements = g.db.total_changes, []
    g.db.set_trace_callback(statements.append)
    with g.db:
        result = limits.sample(g.db, 'WORK', g.bound, 'a1')
    assert result == before and g.db.total_changes == changes
    assert not any(sql.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'INSERT', 'UPDATE', 'DELETE'))
                   for sql in statements)


def test_lock_longer_than_old_sqlite_timeout_recovers_within_existing_gap(guarded):
    g = guarded
    before = activate(g)
    with held_writer(g) as release:
        timer = threading.Timer(5.3, release.set)
        timer.start()
        started = time.monotonic()
        try:
            result = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                                        last_observed_at=before['last_observation']['observed_at'])
        finally:
            timer.cancel()
        assert 5 <= time.monotonic() - started < 8
    assert result['breach'] is None
    assert limits._read(g.db, 'WORK')['scope'] == g.bound


def test_persistent_lock_still_fails_by_original_execution_deadline(guarded):
    g = guarded
    before = activate(g)
    with held_writer(g):
        started = time.monotonic()
        with pytest.raises((Rejected, sqlite3.OperationalError)):
            limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                               deadline=time.time() + .2,
                               last_observed_at=before['last_observation']['observed_at'])
        assert time.monotonic() - started < .9


def test_unreadable_initial_guard_without_a_known_timestamp_fails_closed(guarded):
    g = guarded
    activate(g)
    with held_writer(g, exclusive=True):
        started = time.monotonic()
        with pytest.raises((Rejected, sqlite3.OperationalError)):
            limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
        assert time.monotonic() - started < 1.5


def test_non_lock_database_error_is_not_retried_or_returned_as_health(guarded, monkeypatch):
    g = guarded
    calls = []
    error = sqlite3.OperationalError('deliberate I/O failure')
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    def unavailable(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(limits.sqlite3, 'connect', unavailable)
    with pytest.raises(sqlite3.OperationalError, match='I/O failure'):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    assert calls == [True]


def test_retry_rereads_a_breach_committed_by_the_other_observer(guarded):
    g = guarded
    before = activate(g)
    breach = {'reason': 'max_output_growth_bytes', 'observed_bytes': 17,
              'limit_bytes': g.bound['max_output_growth_bytes'], 'observed_at': time.time()}
    def record_breach(db):
        current = limits._read(db, 'WORK')
        current['breach'] = breach
        limits._save(db, 'WORK', current)
    with held_writer(g, change=record_breach) as release:
        timer = threading.Timer(.2, release.set)
        timer.start()
        try:
            result = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                                        last_observed_at=before['last_observation']['observed_at'])
        finally:
            timer.cancel()
    assert result['breach'] == breach


def test_busy_commit_retries_same_observation_without_losing_a_breach(guarded, monkeypatch):
    g = guarded
    before = activate(g)
    oversized = g.target / 'observed-overrun'
    oversized.write_bytes(b'x' * 11)
    connect, observe = sqlite3.connect, limits.observe
    commits, observations = [], []
    class Connection:
        def __init__(self, raw):
            self.raw = raw
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def commit(self):
            commits.append(True)
            if len(commits) == 1:
                oversized.unlink()
                error = sqlite3.OperationalError('synthetic commit reader contention')
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return self.raw.commit()
    def observed(bound):
        observations.append(True)
        return observe(bound)
    monkeypatch.setattr(limits.sqlite3, 'connect', lambda *args, **kwargs: Connection(connect(*args, **kwargs)))
    monkeypatch.setattr(limits, 'observe', observed)
    result = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                                last_observed_at=before['last_observation']['observed_at'])
    assert result['breach']['reason'] == 'max_output_growth_bytes'
    assert result['high_water_growth_bytes']['output_roots'] == 11
    assert len(commits) == 2 and len(observations) == 1


def test_expired_commit_retains_breach_through_cleanup_restart_and_rollback(guarded, monkeypatch):
    g = guarded
    before = activate(g)
    oversized = g.target / 'observed-overrun'
    oversized.write_bytes(b'x' * 11)
    connect = sqlite3.connect
    class Connection:
        def __init__(self, raw):
            self.raw = raw
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def commit(self):
            if oversized.exists():
                oversized.unlink()
            error = sqlite3.OperationalError('synthetic persistent commit contention')
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
    with monkeypatch.context() as fault:
        fault.setattr(limits.sqlite3, 'connect', lambda *args, **kwargs: Connection(connect(*args, **kwargs)))
        with pytest.raises(Rejected, match='unavailable'):
            limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                               deadline=time.time() + .15,
                               last_observed_at=before['last_observation']['observed_at'])
    assert not oversized.exists() and limits._read(g.db, 'WORK')['breach'] is None
    fallback = limits._failure_path(g.path, 'WORK')
    assert fallback.is_file() and fallback.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match='rollback fixture'):
        with g.db:
            pending = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
            assert pending['breach']['reason'] == 'max_output_growth_bytes'
            raise RuntimeError('rollback fixture')
    with sqlite3.connect(g.path) as restarted:
        result = limits.sample(restarted, 'WORK', g.bound, 'a1', force=True)
    assert result['breach']['reason'] == 'max_output_growth_bytes'
    assert result['high_water_growth_bytes']['output_roots'] == 11
    assert fallback.is_file()


@pytest.mark.parametrize('stage', ['update', 'commit'])
def test_non_busy_write_failure_retains_observed_breach_without_retry(guarded, monkeypatch, stage):
    g = guarded
    before = activate(g)
    oversized = g.target / 'observed-overrun'
    oversized.write_bytes(b'x' * 11)
    connect, calls = sqlite3.connect, []
    def fail(*args):
        calls.append(True)
        oversized.unlink()
        error = sqlite3.OperationalError('synthetic I/O failure after observation')
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error
    class Connection:
        def __init__(self, raw):
            self.raw = raw
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def commit(self):
            fail()
    with monkeypatch.context() as fault:
        if stage == 'update':
            fault.setattr(limits, '_cas_save', fail)
        else:
            fault.setattr(limits.sqlite3, 'connect', lambda *args, **kwargs: Connection(connect(*args, **kwargs)))
        with pytest.raises(sqlite3.OperationalError, match='I/O failure'):
            limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True,
                               last_observed_at=before['last_observation']['observed_at'])
    assert calls == [True]
    with g.db:
        retained = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
    assert retained['breach']['reason'] == 'max_output_growth_bytes'
    assert retained['high_water_growth_bytes']['output_roots'] == 11


@pytest.mark.parametrize('invalid_breach', [None, {}])
def test_malformed_failure_latch_cannot_be_read_as_healthy(guarded, invalid_breach):
    g = guarded
    activate(g)
    limits._retain_failure(g.path, 'WORK', g.bound, None)
    path = limits._failure_path(g.path, 'WORK')
    malformed = json.loads(path.read_text())
    malformed['breach'] = invalid_breach
    path.write_text(json.dumps(malformed))
    with pytest.raises((Rejected, ValueError, TypeError, KeyError)):
        with g.db:
            limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)


def test_scan_completion_cannot_hide_a_real_active_observation_gap(guarded, monkeypatch):
    g = guarded
    before = activate(g)
    moment = [before['last_observation']['observed_at']]
    original = limits.observe
    monkeypatch.setattr(limits.time, 'time', lambda: moment[0])
    def delayed_observation(bound):
        observed = original(bound)
        moment[0] += bound['max_observation_gap_seconds'] + 1
        observed['observed_at'] = moment[0]
        return observed
    monkeypatch.setattr(limits, 'observe', delayed_observation)
    with g.db:
        result = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
    assert result['breach']['reason'] == 'active_observation_gap'
    assert limits._read(g.db, 'WORK')['breach'] == result['breach']


def test_runtime_sampling_uses_the_shared_bounded_contention_helper(guarded, monkeypatch):
    runtime, record = configured(guarded)
    calls = []
    original = limits.sample_file
    def sampled(*args, **kwargs):
        calls.append(kwargs.copy())
        return original(*args, **kwargs)
    monkeypatch.setattr(limits, 'sample_file', sampled)
    try:
        result = runtime._resource_sample(record, guarded.recipe, force=True, activate=True)
        assert result['breach'] is None and calls and calls[0]['activate'] is True
        runtime._resource_finish(record, guarded.recipe)
        assert calls[-1]['finish'] is True
    finally:
        runtime.close()


def test_qualification_watcher_uses_deadline_and_last_receipt_with_shared_helper(technical, monkeypatch):
    g = technical
    sampled_event, calls = threading.Event(), []
    original = limits.sample_file
    def sampled(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(kwargs.copy())
        sampled_event.set()
        return result
    def await_watcher(*args):
        assert sampled_event.wait(3)
        return {'result': 'blocked'}  # No native evidence is invented by this fixture.
    monkeypatch.setattr(limits, 'sample_file', sampled)
    monkeypatch.setattr(g.q.runtime, '_check', await_watcher)
    result = g.q.run('phase-1', g.recipe, max_seconds=5)
    assert result['status'] == 'blocked'
    assert calls and calls[0]['deadline'] is not None and calls[0]['last_observed_at'] is not None


def test_outer_supervisor_samples_at_declared_interval_not_25ms(guarded, monkeypatch):
    g = guarded
    clock, calls = [0.0], []
    wall = time.time()
    status, release = g.state / 'synthetic-status.json', g.state / 'synthetic-release'
    release.touch()
    payload = {'inner': {}, 'argv': ['synthetic-only'], 'interpreter': 'unused', 'inner_source': '',
               'cwd': str(g.source), 'status': str(status), 'release': str(release),
               'cancel': str(g.state / 'synthetic-cancel'), 'timeout_seconds': 10,
               'execution_deadline': datetime.fromtimestamp(wall + 10, timezone.utc).isoformat(),
               'resource_guard': {'database': str(g.path), 'work_id': 'WORK', 'bound': g.bound, 'attempt_id': 'a1'}}
    class Child:
        stdout = SimpleNamespace(fileno=lambda: 999)
        stdin = io.BytesIO()
        returncode = None
        def poll(self):
            if clock[0] >= 2.2:
                self.returncode = 0
            return self.returncode
    child, output = Child(), [b'{"exit_code":0,"reconciled":true}']
    def sampled(*args, **kwargs):
        calls.append((clock[0], kwargs.copy()))
        return {'breach': None, 'observed_at': wall + clock[0]}
    monkeypatch.setattr(limits, 'sample_file', sampled)
    monkeypatch.setattr('subprocess.Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr('sys.argv', ['synthetic-supervisor', json.dumps(payload)])
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(b'{}\n')))
    monkeypatch.setattr('os.set_blocking', lambda *args: None)
    monkeypatch.setattr('os.read', lambda *args: output.pop() if output else b'')
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'time', lambda: wall + clock[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    exec(native_processes.OUTER_SUPERVISOR, {})
    periodic = [at for at, kwargs in calls if not kwargs.get('force')]
    assert len(calls) <= 4 and len(periodic) == 2
    assert periodic[1] - periodic[0] >= 1
    assert all(kwargs.get('deadline') is not None for _, kwargs in calls)
