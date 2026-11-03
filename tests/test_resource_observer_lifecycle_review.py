"""Independent history-filtering and observer-lifecycle regressions."""
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from agentic_tools.aos import native_paths, native_processes, native_qualification, native_sandbox, resource_limits as limits
from agentic_tools.aos.store import Rejected, canonical
from test_aos_native_preparation import prepared_native, run_check
from test_native_qualification_independent import technical, synthetic_private_receipts


class PreparationReached(Exception):
    """Stop a fixture before its permission probe or subprocess launch."""


def insert_native(runtime, name, record):
    with runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)',
                   (name, None, record if isinstance(record, str) else canonical(record)))


def stop_at_preparation(runtime, monkeypatch):
    def reached(*args, **kwargs):
        raise PreparationReached('fixture stops before native execution')
    monkeypatch.setattr(runtime, '_native_profile_prepare', reached)


def forbid_history_decode(monkeypatch):
    original = json.loads
    def decoded(value, *args, **kwargs):
        marker = 'irrelevant-history-payload' if isinstance(value, str) else b'irrelevant-history-payload'
        assert marker not in value, 'Unrelated historical payload was decoded in Python'
        return original(value, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', decoded)


def test_native_admission_does_not_decode_clean_historical_payload(prepared_native, monkeypatch):
    p = prepared_native
    insert_native(p.runtime, 'old-clean', {
        'check_id': 'old-clean', 'phase': 'clean', 'output': str(p.outputs / 'old-phase' / 'check'),
        'irrelevant-history-payload': 'x' * (1024 * 1024)})
    stop_at_preparation(p.runtime, monkeypatch)
    forbid_history_decode(monkeypatch)
    with pytest.raises(PreparationReached):
        run_check(p)
    assert p.calls == []


def test_native_snapshot_does_not_hold_the_resource_writer_lock(prepared_native, monkeypatch):
    p = prepared_native
    generated = p.source / 'generated'
    generated.mkdir()
    p.recipe['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    original = native_paths.snapshot
    snapshots = []
    def snapshot(*args, **kwargs):
        with sqlite3.connect(p.runtime.path, timeout=0) as observer:
            observer.execute('BEGIN IMMEDIATE')
            observer.rollback()
        snapshots.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(native_paths, 'snapshot', snapshot)
    stop_at_preparation(p.runtime, monkeypatch)
    with pytest.raises(PreparationReached):
        run_check(p)
    assert snapshots and p.calls == []


@pytest.mark.parametrize('change', ['new-clean-check', 'running-to-clean'])
def test_native_admission_rejects_ownership_change_during_snapshot(prepared_native, monkeypatch, change):
    p = prepared_native
    generated = p.source / 'generated'
    generated.mkdir()
    p.recipe['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    other = {'check_id': 'other', 'phase': 'running', 'output': str(p.outputs / 'other-phase' / 'check')}
    if change == 'running-to-clean':
        insert_native(p.runtime, 'other', other)
    original = native_paths.snapshot
    def snapshot(*args, **kwargs):
        before = original(*args, **kwargs)
        # Another runtime completes native ownership after this invocation's
        # baseline was captured. A clean final row alone cannot validate it.
        other['phase'] = 'clean'
        if change == 'new-clean-check':
            insert_native(p.runtime, 'other', other)
        else:
            with p.runtime._db() as db:
                db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(other), 'other'))
        return before
    monkeypatch.setattr(native_paths, 'snapshot', snapshot)
    stop_at_preparation(p.runtime, monkeypatch)
    with pytest.raises(Rejected, match='native_ownership'):
        run_check(p)
    assert not (p.outputs / 'check').exists() and p.calls == []


@pytest.mark.parametrize('phase', [None, 0, [], {}, 'unrecognized'])
def test_native_admission_does_not_drop_malformed_phase_projection(prepared_native, monkeypatch, phase):
    p = prepared_native
    insert_native(p.runtime, 'ambiguous', {'check_id': 'ambiguous', 'phase': phase,
                                         'output': str(p.outputs / 'ambiguous')})
    stop_at_preparation(p.runtime, monkeypatch)
    with pytest.raises((Rejected, ValueError, TypeError, KeyError, sqlite3.DatabaseError)):
        run_check(p)
    assert p.calls == []


def test_native_admission_does_not_drop_missing_phase_projection(prepared_native, monkeypatch):
    p = prepared_native
    insert_native(p.runtime, 'ambiguous', {'check_id': 'ambiguous', 'output': str(p.outputs / 'ambiguous')})
    stop_at_preparation(p.runtime, monkeypatch)
    with pytest.raises((Rejected, ValueError, TypeError, KeyError, sqlite3.DatabaseError)):
        run_check(p)
    assert p.calls == []


def test_phase_rows_filter_literal_components_without_decoding_other_history(technical, monkeypatch):
    g = technical
    root = g.outputs / 'technical_a%literal'
    expected = {'check_id': 'mine', 'phase': 'clean', 'output': str(root / 'check')}
    insert_native(g.q.runtime, 'mine', expected)
    others = [root.with_name(root.name + '-sibling'),
              root.with_name('technicalXaZliteral'), root.with_name(root.name.upper())]
    for index, path in enumerate(others):
        insert_native(g.q.runtime, 'other-' + str(index), {
            'check_id': 'other-' + str(index), 'phase': 'clean', 'output': str(path / 'check'),
            'irrelevant-history-payload': 'x' * (256 * 1024)})
    forbid_history_decode(monkeypatch)
    assert g.q._rows({'output': str(root)}) == [expected]


def test_phase_rows_do_not_lose_an_output_equal_to_the_phase_root(technical):
    g = technical
    root = g.outputs / 'technical-current'
    record = {'check_id': 'same-root', 'phase': 'running', 'output': str(root)}
    insert_native(g.q.runtime, 'same-root', record)
    assert g.q._rows({'output': str(root)}) == [record]


@pytest.mark.parametrize('record', [
    '{"broken":',
    {'check_id': 'ambiguous', 'phase': 'running'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': None},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': 3},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': []},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': 'relative/check'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': ''},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': '/outside//check'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': '/outside/../check'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': '/outside/.'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': '/outside/..'},
    {'check_id': 'ambiguous', 'phase': 'running', 'output': '/outside/'},
])
def test_phase_rows_reject_ambiguous_output_projection(technical, record):
    g = technical
    insert_native(g.q.runtime, 'ambiguous', record)
    with pytest.raises((Rejected, ValueError, TypeError, KeyError, sqlite3.DatabaseError)):
        g.q._rows({'output': str(g.outputs / 'technical-current')})


@pytest.mark.parametrize('phase', [None, 0, [], {}, 'unrecognized'])
def test_phase_rows_reject_ambiguous_phase_projection(technical, phase):
    g = technical
    root = g.outputs / 'technical-current'
    insert_native(g.q.runtime, 'ambiguous', {'check_id': 'ambiguous', 'phase': phase,
                                           'output': str(root / 'check')})
    with pytest.raises((Rejected, ValueError, TypeError, KeyError, sqlite3.DatabaseError)):
        g.q._rows({'output': str(root)})


def setup_completed_check(g, monkeypatch, before_return=None):
    g.recipe['max_seconds'] = 30
    for check in g.recipe['checks']:
        check['timeout_seconds'] = 30
    original = g.q.begin
    captured = {}
    def begin(*args, **kwargs):
        captured['phase'] = original(*args, **kwargs)
        return captured['phase']
    def completed(*args, **kwargs):
        if before_return:
            before_return()
        synthetic_private_receipts(g, captured['phase'])
        return {'result': 'pass'}
    monkeypatch.setattr(g.q, 'begin', begin)
    monkeypatch.setattr(g.q.runtime, '_check', completed)
    return captured


def test_watcher_deadline_does_not_reload_whole_qualification_history(technical, monkeypatch):
    g = technical
    observed_twice = threading.Event()
    main = threading.current_thread()
    samples, watcher_reads = [], []
    original_sample, original_read = limits.sample_file, native_qualification._read
    def sampled(*args, **kwargs):
        result = original_sample(*args, **kwargs)
        if threading.current_thread() is not main:
            samples.append(threading.current_thread())
        if len(samples) >= 2:
            observed_twice.set()
        return result
    def read(*args, **kwargs):
        if threading.current_thread() is not main:
            watcher_reads.append(True)
            raise AssertionError('Periodic observer decoded the whole qualification ledger')
        return original_read(*args, **kwargs)
    def wait_for_observer():
        assert observed_twice.wait(4), 'Observer did not complete two bounded observations'
    setup_completed_check(g, monkeypatch, wait_for_observer)
    monkeypatch.setattr(limits, 'sample_file', sampled)
    monkeypatch.setattr(native_qualification, '_read', read)
    result = g.q.run('captured-deadline', g.recipe, max_seconds=30)
    assert not watcher_reads and result['status'] == 'passed', result
    assert all(not thread.is_alive() for thread in samples)


def test_watcher_remains_active_through_cleanup_then_finishes_guard(technical, monkeypatch):
    g = technical
    captured = setup_completed_check(g, monkeypatch)
    main = threading.current_thread()
    cleaning, observed = threading.Event(), threading.Event()
    threads, first_cleanup = [], []
    original_sample, original_cancel = limits.sample_file, g.q._cancel
    def sampled(*args, **kwargs):
        result = original_sample(*args, **kwargs)
        if threading.current_thread() is not main:
            threads.append(threading.current_thread())
        if cleaning.is_set() and threading.current_thread() is not main:
            assert result['active_attempt'] == 'cleanup-monitor'
            observed.set()
        return result
    def cancel(phase):
        if threading.current_thread() is main and not first_cleanup:
            first_cleanup.append(True)
            cleaning.set()
            assert observed.wait(3), 'Observer stopped before native cleanup completed'
        return original_cancel(phase)
    monkeypatch.setattr(limits, 'sample_file', sampled)
    monkeypatch.setattr(g.q, '_cancel', cancel)
    result = g.q.run('cleanup-monitor', g.recipe, max_seconds=30)
    assert observed.is_set() and result['status'] == 'passed', result
    assert all(not thread.is_alive() for thread in threads)
    with g.q.runtime._db() as db:
        assert limits._read(db, captured['phase']['resource_id'])['active_attempt'] is None


def test_late_observer_failure_during_cleanup_cannot_be_lost_from_receipt(technical, monkeypatch):
    g = technical
    setup_completed_check(g, monkeypatch)
    main = threading.current_thread()
    cleaning, failed = threading.Event(), threading.Event()
    first_cleanup = []
    original_sample, original_cancel = limits.sample_file, g.q._cancel
    def sampled(*args, **kwargs):
        if cleaning.is_set() and threading.current_thread() is not main:
            failed.set()
            raise RuntimeError('deliberate late resource observer failure')
        return original_sample(*args, **kwargs)
    def cancel(phase):
        if threading.current_thread() is main and not first_cleanup:
            first_cleanup.append(True)
            cleaning.set()
            assert failed.wait(3), 'Observer stopped before late cleanup failure could be reported'
        return original_cancel(phase)
    monkeypatch.setattr(limits, 'sample_file', sampled)
    monkeypatch.setattr(g.q, '_cancel', cancel)
    result = g.q.run('late-observer-failure', g.recipe, max_seconds=30)
    assert failed.is_set() and result['stopped'] and result['status'] == 'blocked', result
    assert any('deliberate late resource observer failure' in error for error in result['errors'])


def observer_identity(monkeypatch):
    owner = {'pid': os.getpid(), 'uid': os.getuid(), 'birth': [123, 456],
             'parent': os.getppid(), 'group': os.getpgrp(), 'status': 2}
    def identity(pid):
        return dict(owner) if pid == owner['pid'] else None
    monkeypatch.setattr(native_processes, 'identity', identity)
    return identity


def test_external_reconcile_closes_admission_before_slow_baseline_finishes(technical, monkeypatch):
    g = technical
    observer_identity(monkeypatch)
    generated = g.source / 'generated'
    generated.mkdir()
    g.recipe.update(native_write_roots=[{'path': str(generated), 'kind': 'generated'}],
                    native_sandbox={'version': native_sandbox.VERSION}, max_seconds=30)
    g.recipe['resource_limits']['output_roots'] = [str(generated)]
    for check in g.recipe['checks']:
        check['timeout_seconds'] = 30
    in_snapshot, release_snapshot = threading.Event(), threading.Event()
    check_finished, release_run = threading.Event(), threading.Event()
    in_external = threading.Event()
    main = threading.current_thread()
    snapshots, external_rows, profile_calls, results, errors = [], [], [], [], []
    original_snapshot, original_check, original_rows = native_paths.snapshot, g.q.runtime._check, g.q._rows
    def snapshot(*args, **kwargs):
        before = original_snapshot(*args, **kwargs)
        if not snapshots:
            snapshots.append(True)
            in_snapshot.set()
            assert release_snapshot.wait(5), 'External reconciliation did not reach its row snapshot'
        return before
    def check(*args, **kwargs):
        try:
            return original_check(*args, **kwargs)
        finally:
            check_finished.set()
            assert release_run.wait(5), 'Fixture did not release the local run finalizer'
    def rows(phase):
        selected = original_rows(phase)
        if in_external.is_set() and threading.current_thread() is main:
            external_rows.append(True)
            if len(external_rows) == 2:
                assert selected == []
                release_snapshot.set()
                assert check_finished.wait(5), 'Pending admission did not resolve'
        return selected
    def profile(*args, **kwargs):
        profile_calls.append(True)
        raise PreparationReached('No profile/probe/workload is permitted after phase closing')
    def no_process(*args, **kwargs):
        raise AssertionError('This concurrency fixture must never start a subprocess')
    monkeypatch.setattr(native_paths, 'snapshot', snapshot)
    monkeypatch.setattr(g.q.runtime, '_check', check)
    monkeypatch.setattr(g.q.runtime, '_native_profile_prepare', profile)
    monkeypatch.setattr(g.q, '_rows', rows)
    monkeypatch.setattr('subprocess.Popen', no_process)
    def run():
        try:
            results.append(g.q.run('concurrent-close', g.recipe, max_seconds=30))
        except BaseException as error:
            errors.append(error)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert in_snapshot.wait(5), errors
        in_external.set()
        external = g.q.reconcile('concurrent-close', observer_stopped=True)
        in_external.clear()
        assert not profile_calls, 'A check crossed the launch-preparation boundary after closing'
        assert external['stopped'] is False, 'External reconcile claimed a live observer had joined'
        with g.q.runtime._db() as db:
            state = native_qualification._read(db, g.q.qualification_id)
            assert state['active_phase'] == 'concurrent-close'
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    finally:
        in_external.clear()
        release_snapshot.set()
        release_run.set()
        runner.join(timeout=8)
    assert not runner.is_alive() and not errors, errors
    assert results and results[0]['stopped'] and results[0]['status'] == 'blocked', results


@pytest.mark.parametrize('identity_state', ['alive', 'unknown'])
def test_external_reconcile_cannot_assert_an_active_observer_is_joined(technical, monkeypatch, identity_state):
    g = technical
    identity = observer_identity(monkeypatch)
    g.recipe['max_seconds'] = 30
    for check in g.recipe['checks']:
        check['timeout_seconds'] = 30
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    def pending_check(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'result': 'blocked'}  # No native rows or command execution.
    monkeypatch.setattr(g.q.runtime, '_check', pending_check)
    def run():
        try:
            results.append(g.q.run('live-observer', g.recipe, max_seconds=30))
        except BaseException as error:
            errors.append(error)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert entered.wait(5), errors
        if identity_state == 'unknown':
            def unknown(pid):
                raise OSError('Fixture cannot establish the saved process identity')
            monkeypatch.setattr(native_processes, 'identity', unknown)
        external = g.q.reconcile('live-observer', observer_stopped=True)
        assert external['stopped'] is False
        with g.q.runtime._db() as db:
            state = native_qualification._read(db, g.q.qualification_id)
            assert state['active_phase'] == 'live-observer'
    finally:
        monkeypatch.setattr(native_processes, 'identity', identity)
        release.set()
        runner.join(timeout=8)
    assert not runner.is_alive() and not errors, errors
    assert results and results[0]['stopped'] and results[0]['status'] == 'blocked', results


def assert_observer_unwound(g, phase_id, error):
    with g.q.runtime._db() as db:
        record = native_qualification._read(db, g.q.qualification_id)
        phase = next(phase for phase in record['phases'] if phase['id'] == phase_id)
        assert record['active_phase'] is None and phase['state'] == 'stopped'
        joined = phase['observer_joined']
        assert joined['owner'] == phase['observer_owner']
        assert joined['watcher_joined'] is True and joined['cancellers_joined'] is True
        assert any(error in item for item in phase['execution_errors'])
        assert phase['receipt']['stopped'] and phase['receipt']['status'] == 'blocked'
        assert limits._read(db, phase['resource_id'])['active_attempt'] is None
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    assert g.q.runtime.model_calls == 0


def test_observer_start_failure_retains_join_and_error_without_ownership_leak(technical, monkeypatch):
    g = technical
    observer_identity(monkeypatch)
    checked = []
    def cannot_start(thread):
        raise RuntimeError('deliberate observer Thread.start failure')
    def forbidden_process(*args, **kwargs):
        raise AssertionError('Observer-start failure must never launch a subprocess')
    monkeypatch.setattr(threading.Thread, 'start', cannot_start)
    monkeypatch.setattr(g.q.runtime, '_check', lambda *args, **kwargs: checked.append(True))
    monkeypatch.setattr('subprocess.Popen', forbidden_process)
    result = g.q.run('start-failure', g.recipe, max_seconds=5)
    assert result['status'] == 'blocked' and not checked
    assert_observer_unwound(g, 'start-failure', 'deliberate observer Thread.start failure')


def test_check_keyboard_interrupt_retains_join_and_error_before_propagation(technical, monkeypatch):
    g = technical
    observer_identity(monkeypatch)
    checked = []
    def interrupted(*args, **kwargs):
        checked.append(True)
        raise KeyboardInterrupt('deliberate check interruption')
    def forbidden_process(*args, **kwargs):
        raise AssertionError('Interrupted fixture check must never launch a subprocess')
    monkeypatch.setattr(g.q.runtime, '_check', interrupted)
    monkeypatch.setattr('subprocess.Popen', forbidden_process)
    with pytest.raises(KeyboardInterrupt, match='deliberate check interruption'):
        g.q.run('check-interruption', g.recipe, max_seconds=5)
    assert checked == [True]
    assert_observer_unwound(g, 'check-interruption', 'deliberate check interruption')
