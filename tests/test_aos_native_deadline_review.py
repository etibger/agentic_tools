"""Independent checks that native execution never receives a fresh assignment clock."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time

import pytest

from agentic_tools.aos import codex_runtime, native_processes
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate
from agentic_tools.aos.store import Rejected, canonical
from test_aos_codex_runtime import native_fixture


def retained_attempt(p, *, deadline=None, cleanup=0, sent_at=None):
    stamp = datetime.now(timezone.utc)
    record = {
        'attempt_id': p.recipe['_attempt_id'],
        'created_at': stamp.isoformat(),
        'assignment': {'sent_at': (sent_at or stamp).isoformat()},
        'startup_cleanup_reserve': cleanup,
    }
    if deadline is not None:
        record['startup_deadline'] = deadline.isoformat() if isinstance(deadline, datetime) else deadline
    with p.runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    return record


def test_expired_attempt_has_no_native_admission_effects(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child', attempt_id='expired-before-native')
    retained_attempt(p, deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    effects = []
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *a, **k: effects.append('probe'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: effects.append('process'))
    try:
        with pytest.raises(Rejected, match='assignment_deadline'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert effects == []
        assert list(p.outputs.iterdir()) == []
        assert not (p.state / 'native-status').exists()
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    finally:
        p.runtime.close()


def test_native_deadline_retains_original_assignment_cleanup_margin(tmp_path):
    p = native_fixture(tmp_path, 'no-child', attempt_id='remaining-budget')
    p.check['timeout_seconds'] = p.recipe['max_seconds'] = 60
    original = datetime.now(timezone.utc) + timedelta(seconds=9)
    retained_attempt(p, deadline=original, cleanup=2)
    try:
        assert p.runtime._native_deadline(p.recipe, p.check) == original - timedelta(seconds=2)
        row_deadline = original - timedelta(seconds=5)
        row = {'created_at': datetime.now(timezone.utc).isoformat(),
               'execution_deadline': row_deadline.isoformat()}
        assert p.runtime._native_deadline(p.recipe, p.check, row) == row_deadline
    finally:
        p.runtime.close()


def test_check_timeout_can_be_stricter_than_assignment_deadline(tmp_path):
    p = native_fixture(tmp_path, 'no-child', attempt_id='short-check')
    p.recipe['max_seconds'] = 60
    p.check['timeout_seconds'] = 2
    retained_attempt(p, deadline=datetime.now(timezone.utc) + timedelta(seconds=50), cleanup=5)
    try:
        before = datetime.now(timezone.utc)
        actual = p.runtime._native_deadline(p.recipe, p.check)
        after = datetime.now(timezone.utc)
        assert before + timedelta(seconds=2) <= actual <= after + timedelta(seconds=2)
        assert actual.tzinfo is not None
    finally:
        p.runtime.close()


def test_legacy_deadlines_use_original_timestamps_not_recovery_time(tmp_path):
    p = native_fixture(tmp_path, 'no-child', attempt_id='legacy-clock')
    original = datetime.now(timezone.utc) - timedelta(seconds=40)
    retained_attempt(p, sent_at=original, cleanup=1)
    try:
        # A legacy attempt has no startup_deadline, but its saved sent_at still
        # fences the job after a restart. No new ten-second window is granted.
        assert p.runtime._native_deadline(p.recipe, p.check) == original + timedelta(seconds=9)
        p.recipe.pop('_attempt_id')
        row = {'created_at': original.isoformat()}
        assert p.runtime._native_deadline(p.recipe, p.check, row) == original + timedelta(seconds=10)
    finally:
        p.runtime.close()


def test_new_runtime_keeps_persisted_native_deadline(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    p.recipe['max_seconds'] = p.check['timeout_seconds'] = 60
    saved = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = {'check_id': 'retained-native-clock', 'attempt_id': None, 'phase': 'running',
           'created_at': (saved - timedelta(seconds=60)).isoformat(),
           'execution_deadline': saved.isoformat()}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
    reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
    try:
        retained = reopened._native_get(row['check_id'])
        assert reopened._native_deadline(p.recipe, p.check, retained) == saved
        assert reopened._native_deadline(p.recipe, p.check, retained) < datetime.now(timezone.utc)
    finally:
        reopened.close()
        p.runtime.close()


@pytest.mark.parametrize('wall_remaining,monotonic_remaining,reserve,expected', [
    (3600, 5, 1, 4),  # Wall clock rolled back; only four original seconds remain.
    (2, 50, 1, 1),    # Wall clock moved forward; its stricter bound still wins.
    (3600, -1, 0, -1),
    (3600, 1, 2, -1), # Cleanup time is reserved even after a clock change.
])
def test_native_deadline_uses_stricter_original_clock_after_restart(
        tmp_path, monkeypatch, wall_remaining, monotonic_remaining, reserve, expected):
    p = native_fixture(tmp_path, 'no-child', attempt_id='retained-monotonic')
    p.recipe['max_seconds'] = p.check['timeout_seconds'] = 60
    utc_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    record = retained_attempt(p, deadline=utc_now + timedelta(seconds=wall_remaining), cleanup=reserve)
    record.update(startup_monotonic=50, startup_deadline_monotonic=100 + monotonic_remaining)
    p.runtime._save(record)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return utc_now

    monkeypatch.setattr(codex_runtime, 'datetime', Clock)
    monkeypatch.setattr(codex_runtime.time, 'monotonic', lambda: 100)
    reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
    try:
        assert reopened._native_deadline(p.recipe, p.check) == utc_now + timedelta(seconds=expected)
        row = {'created_at': utc_now.isoformat(),
               'execution_deadline': (utc_now + timedelta(seconds=expected - 1)).isoformat()}
        assert reopened._native_deadline(p.recipe, p.check, row) == utc_now + timedelta(seconds=expected - 1)
        assert reopened._timers == {}
    finally:
        reopened.close()
        p.runtime.close()


def test_restarted_monotonic_origin_cannot_resume_old_allowance(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child', attempt_id='clock-origin-changed')
    record = retained_attempt(p, deadline=datetime.now(timezone.utc) + timedelta(hours=1))
    record.update(startup_monotonic=200, startup_deadline_monotonic=260)
    p.runtime._save(record)
    monkeypatch.setattr(codex_runtime.time, 'monotonic', lambda: 100)
    try:
        with pytest.raises(Rejected, match='assignment_deadline'):
            p.runtime._native_deadline(p.recipe, p.check)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('field,value', [
    ('startup_cleanup_reserve', -1),
    ('startup_cleanup_reserve', float('nan')),
    ('startup_cleanup_reserve', float('inf')),
    ('startup_cleanup_reserve', True),
    ('startup_cleanup_reserve', '1'),
    ('startup_monotonic', float('nan')),
    ('startup_deadline_monotonic', float('inf')),
    ('startup_deadline_monotonic', True),
])
def test_invalid_retained_reserve_or_monotonic_values_fail_closed(tmp_path, monkeypatch, field, value):
    p = native_fixture(tmp_path, 'no-child', attempt_id='invalid-clock-metadata')
    record = retained_attempt(p, deadline=datetime.now(timezone.utc) + timedelta(hours=1))
    record.update(startup_monotonic=50, startup_deadline_monotonic=150)
    record[field] = value
    # The store normally rejects NaN during serialization. Exercise the reader's
    # own guard as well, without writing malformed data into even this fixture.
    monkeypatch.setattr(p.runtime, '_get', lambda aid: record)
    monkeypatch.setattr(codex_runtime.time, 'monotonic', lambda: 100)
    try:
        with pytest.raises(Rejected, match='assignment_deadline'):
            p.runtime._native_deadline(p.recipe, p.check)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('where', ['attempt', 'row', 'legacy-row'])
@pytest.mark.parametrize('timestamp', ['not-a-timestamp', '2030-01-01T00:00:00'])
def test_malformed_or_naive_retained_timestamps_fail_closed(tmp_path, where, timestamp):
    p = native_fixture(tmp_path, 'no-child', attempt_id='bad-clock')
    row = None
    if where == 'attempt':
        retained_attempt(p, deadline=timestamp)
    else:
        p.recipe.pop('_attempt_id')
        row = {'created_at': timestamp}
        if where == 'row':
            row['execution_deadline'] = timestamp
    try:
        with pytest.raises((Rejected, ValueError, TypeError)):
            p.runtime._native_deadline(p.recipe, p.check, row)
    finally:
        p.runtime.close()


def test_preparation_cannot_spend_past_deadline_then_launch(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child', attempt_id='deadline-during-probe')
    cache = tmp_path / 'cache'
    cache.mkdir()
    p.recipe['native_write_roots'] = [{'path': str(cache), 'kind': 'cache'}]
    record = retained_attempt(p, deadline=datetime.now(timezone.utc) + timedelta(seconds=10))
    calls = []

    def probe(*args, **kwargs):
        calls.append('probe')
        # Observe the durable deadline before changing only this disposable
        # fixture's original attempt deadline to simulate preparation expiry.
        with p.runtime._db() as db:
            saved = json.loads(db.execute('SELECT record FROM native_check').fetchone()[0])
        assert saved['execution_deadline']
        record['startup_deadline'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        p.runtime._save(record)
        return {'simulated': True}

    def forbidden_process(*args, **kwargs):
        calls.append('process')
        raise AssertionError('Expired preparation launched a subprocess')

    monkeypatch.setattr(p.runtime, '_probe_sandbox', probe)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_process)
    try:
        with pytest.raises(Rejected, match='assignment_deadline'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', candidate(p.source, recipe=p.recipe))
        assert calls == ['probe']
        assert not (p.outputs / 'check' / 'stdout.txt').exists()
        with p.runtime._db() as db:
            saved = json.loads(db.execute('SELECT record FROM native_check').fetchone()[0])
        assert saved['phase'] == 'intent' and 'owner' not in saved
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_actual_census_obeys_remaining_assignment_time_and_replays_receipt(tmp_path):
    p = native_fixture(tmp_path, 'no-child', attempt_id='actual-deadline')
    p.check['timeout_seconds'] = p.recipe['max_seconds'] = 20
    p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.recipe['probe_interpreter'] = sys.executable
    started_file = p.outputs / 'command-started'
    p.check['argv'] = [sys.executable, '-I', '-c',
                       'from pathlib import Path;import time;'
                       f'Path({str(started_file)!r}).write_text("started");time.sleep(20)']
    original = datetime.now(timezone.utc) + timedelta(seconds=4)
    record = retained_attempt(p, deadline=original, cleanup=1)
    began = time.monotonic()
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        elapsed = time.monotonic() - began
        assert started_file.read_text() == 'started'
        assert result['result'] == 'timeout'
        assert result['cleanup']['effects_reconciled']
        assert elapsed < 10, 'Native check received a new twenty-second clock'
        row = p.runtime._native_get(result['native_check_id'])
        assert datetime.fromisoformat(row['execution_deadline']) == original - timedelta(seconds=1)
        native_processes.verify_absent(row['native_process_receipt'])
        assert not p.runtime._timers, 'This fixture must exercise a restarted runtime without its model timer'

        # A verified terminal receipt is evidence, not a new execution. It can
        # be reread after the assignment deadline without spawning the command.
        record['startup_deadline'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        p.runtime._save(record)
        before = started_file.stat().st_mtime_ns
        replay = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert replay == result and started_file.stat().st_mtime_ns == before
    finally:
        p.runtime.close()
