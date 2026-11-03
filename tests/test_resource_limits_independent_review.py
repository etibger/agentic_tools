"""Independent resource monitor fault/lifetime regressions; synthetic state only."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from agentic_tools.aos import resource_limits as limits
from agentic_tools.aos import native_processes
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical
from test_aos_codex_runtime import claim, native_fixture, prepared
from test_aos_resource_limits import guarded


def configured(g):
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=g.state)), {'WORK': g.recipe})
    record = {'attempt_id': 'independent-monitor', 'assignment': {'work_id': 'WORK'}}
    with runtime._db() as db:
        limits.prepare(db, 'WORK', g.recipe)
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    return runtime, record


def test_temporarily_missing_root_remains_a_breach_after_restoration(guarded, monkeypatch):
    g = guarded
    runtime, record = configured(g)
    cancelled = threading.Event()
    monkeypatch.setattr(runtime, 'cancel', lambda _: cancelled.set())
    moved = g.outputs.with_name('temporarily-moved-output')
    try:
        runtime._resource_watch(record, g.recipe)
        g.outputs.rename(moved)
        assert cancelled.wait(5), 'A damaged monitored root must stop the exact attempt'
        moved.rename(g.outputs)
        with runtime._db() as db:
            retained = limits._read(db, 'WORK')
        assert retained['breach']['reason'] == 'observation_failed'
        with pytest.raises(Rejected, match='resource_limit_exceeded'):
            runtime._resource_sample(record, g.recipe, force=True)
    finally:
        if moved.exists():
            moved.rename(g.outputs)
        runtime.close()


def test_finished_monitor_cannot_issue_late_cancellation(guarded, monkeypatch):
    g = guarded
    runtime, record = configured(g)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = runtime._resource_sample
    def sample(*args, **kwargs):
        if not kwargs.get('activate') and not kwargs.get('finish') and not kwargs.get('force'):
            entered.set()
            assert release.wait(5)
            raise Rejected('resource_limits', 'Late synthetic observation error')
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, '_resource_sample', sample)
    monkeypatch.setattr(runtime, 'cancel', lambda _: cancelled.set())
    try:
        runtime._resource_watch(record, g.recipe)
        assert entered.wait(5)
        stop, _ = runtime._resource_monitors[record['attempt_id']]
        def finish_observation():
            assert stop.wait(5)
            release.set()
        finisher = threading.Thread(target=finish_observation)
        finisher.start()
        final = runtime._resource_finish(record, g.recipe)
        finisher.join(timeout=5)
        assert final['breach'] is None and final['active_attempt'] is None
        release.set()
        assert not cancelled.wait(.5), 'A monitor stopped by successful finalization must not cancel afterward'
    finally:
        release.set()
        runtime.close()


def test_low_disk_receipt_retains_the_actual_failed_observation(guarded, monkeypatch):
    g = guarded
    with g.db:
        limits.sample(g.db, 'WORK', g.bound, 'a1', activate=True)
    monkeypatch.setattr(limits.os, 'statvfs', lambda _: SimpleNamespace(f_bavail=0, f_frsize=4096))
    with g.db:
        failed = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
    monkeypatch.setattr(limits.os, 'statvfs', lambda _: SimpleNamespace(f_bavail=100, f_frsize=4096))
    with g.db:
        recovered = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, finish=True)
    assert recovered['breach'] == failed['breach']
    assert recovered['breach']['observed_bytes'] == 0
    assert recovered['breach']['limit_bytes'] == g.bound['min_free_bytes']


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS exact native owner identity')
def test_unavailable_initial_native_observation_rejects_before_admission(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child')
    marker = p.outputs / 'command-launched'
    p.check['argv'] = [sys.executable, '-c', f'from pathlib import Path;Path({str(marker)!r}).touch()']
    p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 100_000,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    monkeypatch.setattr(p.runtime, '_resource_native_payload', lambda _: {
        'database': str(p.state / 'missing-directory' / 'runtime.sqlite3'),
        'work_id': 'WORK', 'bound': limits.scope(p.recipe), 'attempt_id': 'independent-no-launch'})
    try:
        with pytest.raises(sqlite3.OperationalError, match='unable to open database'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert not marker.exists()
        assert not (p.outputs / 'check').exists()
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    finally:
        p.runtime.close()


def test_observed_breach_before_model_send_does_not_start_model(prepared, monkeypatch):
    p = prepared
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 100_000,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    # This test isolates the already-admitted runtime path, using only scripted RPC.
    monkeypatch.setattr(p.runtime, '_recipe', lambda _: p.recipe)
    cancelled = threading.Event()
    original = p.runtime.cancel
    def cancel(attempt):
        try:
            return original(attempt)
        finally:
            cancelled.set()
    monkeypatch.setattr(p.runtime, 'cancel', cancel)
    def fail_before_send(stage):
        if stage == 'before_turn_request':
            (p.outputs / 'oversized-startup-log').write_bytes(b'x' * 100_001)
            assert cancelled.wait(5), 'The guard must observe the oversized startup output'
    p.runtime._fault = fail_before_send
    p.runtime.start(claim(p))
    with p.runtime._db() as db:
        assert limits._read(db, 'Q2')['breach']['reason'] == 'max_source_evidence_growth_bytes'
    assert p.backend.count('turn/start') == 0, 'A model may not start after its guard observed a breach'


def test_resource_breach_poll_reconciles_a_pending_model_stop(prepared, monkeypatch):
    p = prepared
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 100_000,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    monkeypatch.setattr(p.runtime, '_recipe', lambda _: p.recipe)
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    client = p.runtime.clients[record['attempt_id']]
    original_request = client.request
    def pending_interrupt(method, params=None):
        if method == 'turn/interrupt':
            return {}  # Remote interrupt ACK can precede terminal state.
        return original_request(method, params)
    monkeypatch.setattr(client, 'request', pending_interrupt)
    cancelled = threading.Event()
    original_cancel = p.runtime.cancel
    def cancel(attempt):
        try:
            return original_cancel(attempt)
        finally:
            cancelled.set()
    monkeypatch.setattr(p.runtime, 'cancel', cancel)
    (p.outputs / 'oversized-model-log').write_bytes(b'x' * 100_001)
    assert cancelled.wait(5)
    p.backend.finish(record['thread_id'], 'interrupted')
    result = p.runtime.poll(record['attempt_id'])
    assert result['state'] == 'failed', 'A retained breach must allow stop reconciliation on later polls'
    assert result['result']['effects_reconciled']
    assert result['result']['resource_limits']['breach']['reason'] == 'max_source_evidence_growth_bytes'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_native_post_launch_resource_breach_stops_and_reconciles_child(tmp_path):
    p = native_fixture(tmp_path, 'no-child', attempt_id='independent-native-growth')
    marker = p.outputs / 'oversized-native-log'
    p.check['argv'] = [sys.executable, '-c',
                      f'import time;from pathlib import Path;Path({str(marker)!r}).write_bytes(b"x"*32768);time.sleep(20)']
    p.check['timeout_seconds'] = 6
    p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 16_384,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    record = {'attempt_id': p.recipe['_attempt_id'], 'assignment': {'work_id': 'N'},
              'created_at': datetime.now(timezone.utc).isoformat()}
    with p.runtime._db() as db:
        limits.prepare(db, 'N', p.recipe)
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    p.runtime._resource_sample(record, p.recipe, force=True, activate=True)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert marker.stat().st_size == 32_768
        assert result['result'] == 'blocked' and result['cleanup']['effects_reconciled']
        assert result['resource_guard']['breach']['reason'] == 'max_source_evidence_growth_bytes'
        row = p.runtime._native_get(result['native_check_id'])
        native_processes.verify_absent(row['native_process_receipt'])
        final = p.runtime._resource_finish(record, p.recipe)
        assert final['active_attempt'] is None and final['breach'] == result['resource_guard']['breach']
    finally:
        p.runtime.close()


def test_initial_watch_breach_has_saved_paths_and_proven_no_client_stop(prepared, monkeypatch):
    p = prepared
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 100_000,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    # Isolate the already-authorized startup path, using only the scripted fixture.
    monkeypatch.setattr(p.runtime, '_recipe', lambda _: p.recipe)
    original = p.runtime._resource_watch
    growth = p.outputs / 'growth-after-admission'
    def fail_initial_watch(record, recipe):
        saved = p.runtime._get(record['attempt_id'])
        assert saved['startup_paths'] and saved['startup_outputs'] == {}
        growth.write_bytes(b'x' * 100_001)
        return original(record, recipe)
    monkeypatch.setattr(p.runtime, '_resource_watch', fail_initial_watch)
    assignment = claim(p)
    result = p.runtime.start(assignment)
    assert result['state'] == 'startup_failed'
    assert result['result']['stopped'] and result['result']['effects_reconciled']
    breach = result['result']['resource_limits']['breach']
    assert breach['reason'] == 'max_source_evidence_growth_bytes'
    assert p.runtime.clients == {} and p.backend.clients == [] and p.runtime.model_calls == 0
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
    reserved = p.runtime.budget_receipt()
    assert reserved['attempts_reserved'] == 1 and reserved['seconds_reserved'] == p.recipe['max_seconds']
    growth.unlink()
    assert p.runtime.start(assignment)['state'] == 'startup_failed'
    assert p.runtime.budget_receipt() == reserved
    with p.runtime._db() as db:
        assert limits._read(db, 'Q2')['breach'] == breach


def test_existing_healthy_baseline_is_resampled_before_new_reservation(prepared, monkeypatch):
    p = prepared
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 1, 'max_source_evidence_growth_bytes': 100_000,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    monkeypatch.setattr(p.runtime, '_recipe', lambda _: p.recipe)
    with p.runtime._db() as db:
        assert limits.prepare(db, 'Q2', p.recipe)['breach'] is None
    growth = p.outputs / 'growth-after-previous-observation'
    growth.write_bytes(b'x' * 100_001)
    assignment = claim(p)
    denied = p.runtime.start(assignment)
    assert denied['state'] == 'startup_failed'
    assert denied['result']['startup_outcome']['admission_denial'] is True
    assert denied['result']['stopped'] and denied['result']['effects_reconciled']
    assert denied['result']['no_launch']['runtime_attempt_created'] is False
    growth.unlink()
    assert p.runtime.start(assignment) == denied
    with p.runtime._db() as db:
        assert limits._read(db, 'Q2')['breach']['reason'] == 'max_source_evidence_growth_bytes'
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM admission_denial').fetchone()[0] == 1
    assert p.runtime.clients == {} and p.backend.calls == [] and p.runtime.model_calls == 0
