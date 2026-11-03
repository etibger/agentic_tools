"""Independent private-journal control tests; no native or model workload runs."""
import copy
import json
import os
import threading
import time

import pytest

from agentic_tools.aos import native_qualification as qualification
from agentic_tools.aos import resource_limits, technical_control
from agentic_tools.aos.store import Rejected
from test_native_qualification_independent import technical


def current(g, phase_id='phase-one'):
    with g.q.runtime._db() as db:
        return technical_control.load(db, g.q.qualification_id, phase_id)


def archive(g):
    with g.q.runtime._db() as db:
        return qualification._read(db, g.q.qualification_id)


def guard(g, phase):
    with g.q.runtime._db() as db:
        return resource_limits._read(db, phase['resource_id'])


def exact_recipe(g, phase):
    return {**copy.deepcopy(phase['recipe']), '_technical_resource_phase': {
        'qualification_id': g.q.qualification_id, 'phase_id': phase['id']}}


def change_control(g, **changes):
    with g.q.runtime._db() as db:
        db.execute('BEGIN IMMEDIATE')
        before = technical_control.load(db, g.q.qualification_id)
        phase = {**copy.deepcopy(before['phase']), **changes}
        return technical_control.save(db, g.q.qualification_id, phase, expected=before)


def owner():
    return {'pid': os.getpid(), 'uid': os.getuid(), 'birth': [123, 456],
            'group': os.getpid(), 'parent': 1, 'status': 2}


def claim_synthetic_observer(g, monkeypatch):
    """Control-unit fixture only; this does not claim a real watcher ran."""
    identity = owner()
    monkeypatch.setattr(qualification.native_processes, 'identity', lambda pid: identity)
    change_control(g, execution_started=True, observer_started=True, observer_owner=identity)
    return identity


def forbid_active_archive(monkeypatch):
    calls = []
    original_read, original_save = qualification._read, qualification._save

    def checked(fn, kind, db, *args):
        assert not db.execute("SELECT 1 FROM resource_limit WHERE "
                              "json_extract(record,'$.active_attempt') IS NOT NULL LIMIT 1").fetchone(), kind
        calls.append(kind)
        return fn(db, *args)

    monkeypatch.setattr(qualification, '_read', lambda db, *args: checked(original_read, 'read', db, *args))
    monkeypatch.setattr(qualification, '_save', lambda db, *args: checked(original_save, 'save', db, *args))
    return calls


def test_passive_archive_delay_does_not_spend_an_active_observation_gap(technical, monkeypatch):
    g = technical
    offset, observed = [0.0], []
    wall, monotonic = time.time, time.monotonic
    monkeypatch.setattr(time, 'time', lambda: wall() + offset[0])
    monkeypatch.setattr(time, 'monotonic', lambda: monotonic() + offset[0])
    save = qualification._save

    def delayed_archive(db, qid, record):
        if record['active_phase'] == 'phase-one':
            phase = record['phases'][-1]
            captured = resource_limits._read(db, phase['resource_id'])
            assert captured['active_attempt'] is None
            observed.append(copy.deepcopy(phase))
            offset[0] += captured['scope']['max_observation_gap_seconds'] + 1
        return save(db, qid, record)

    monkeypatch.setattr(qualification, '_save', delayed_archive)
    phase = g.q.begin('phase-one', g.recipe, max_seconds=60)
    saved = guard(g, phase)
    assert observed and saved['active_attempt'] == 'phase-one' and saved['breach'] is None
    assert phase['started_at'] == observed[0]['started_at']
    assert phase['deadline'] == observed[0]['deadline']
    assert phase['started_monotonic'] == observed[0]['started_monotonic']
    assert phase['deadline_monotonic'] == observed[0]['deadline_monotonic']
    assert time.time() > phase['started_at'] + saved['scope']['max_observation_gap_seconds']


def test_passive_archive_delay_cannot_extend_the_original_phase_deadline(technical, monkeypatch):
    g = technical
    offset = [0.0]
    wall, monotonic = time.time, time.monotonic
    monkeypatch.setattr(time, 'time', lambda: wall() + offset[0])
    monkeypatch.setattr(time, 'monotonic', lambda: monotonic() + offset[0])
    save = qualification._save

    def delayed_archive(db, qid, record):
        if record['active_phase'] == 'phase-one':
            offset[0] += 30
        return save(db, qid, record)

    monkeypatch.setattr(qualification, '_save', delayed_archive)
    with pytest.raises(Rejected, match='original technical phase deadline expired'):
        g.q.begin('phase-one', g.recipe, max_seconds=5)
    saved = current(g)
    assert saved['phase']['state'] == 'prepared'
    assert guard(g, saved['phase'])['active_attempt'] is None
    assert not saved['phase'].get('execution_started')
    assert saved['phase']['deadline'] - saved['phase']['started_at'] <= 5.01


def test_begin_alone_does_not_authorize_native_preparation(technical):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    assert guard(g, phase)['active_attempt'] == 'phase-one'
    with pytest.raises(Rejected, match='does not authorize'):
        qualification.resource_payload(g.q.runtime.path, exact_recipe(g, phase))


@pytest.mark.parametrize('missing', ['execution_started', 'observer_started', 'observer_owner'])
def test_authority_requires_complete_observer_claim(technical, monkeypatch, missing):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    claim_synthetic_observer(g, monkeypatch)
    changes = {missing: None if missing == 'observer_owner' else False}
    change_control(g, **changes)
    with pytest.raises(Rejected, match='does not authorize'):
        qualification.resource_payload(g.q.runtime.path, exact_recipe(g, phase))


@pytest.mark.parametrize('live', [None, {'pid': 1, 'uid': 1, 'birth': [1, 2]}])
def test_dead_or_reused_observer_cannot_authorize_native_preparation(technical, monkeypatch, live):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    claim_synthetic_observer(g, monkeypatch)
    monkeypatch.setattr(qualification.native_processes, 'identity', lambda pid: live)
    with pytest.raises(Rejected, match='observer'):
        qualification.resource_payload(g.q.runtime.path, exact_recipe(g, phase))


def test_real_run_publishes_authority_only_after_watcher_start(technical, monkeypatch):
    g = technical
    seen = []

    def check(recipe, check, output, executor, candidate):
        seen.append(qualification.resource_payload(g.q.runtime.path, recipe))
        control = current(g)
        assert control['phase']['observer_started'] is True
        assert control['phase']['execution_started'] is True
        assert seen[-1]['attempt_id'] == 'phase-one'
        return {'result': 'blocked'}  # No synthetic native receipt is admitted.

    monkeypatch.setattr(g.q.runtime, '_check', check)
    result = g.q.run('phase-one', g.recipe, max_seconds=30)
    assert len(seen) == 1 and result['stopped'] is True and result['status'] == 'blocked'
    saved = current(g)['phase']
    assert saved['observer_started'] is False
    assert saved['observer_joined']['watcher_joined'] is True
    assert saved['observer_joined']['cancellers_joined'] is True


def test_active_run_and_finish_never_decode_or_save_the_full_archive(technical, monkeypatch):
    g = technical
    calls = forbid_active_archive(monkeypatch)
    monkeypatch.setattr(g.q.runtime, '_check', lambda *args: {'result': 'blocked'})
    result = g.q.run('phase-one', g.recipe, max_seconds=30)
    assert result['stopped'] and result['source_unchanged'] and result['product_budget_unchanged']
    assert result['status'] == 'blocked' and calls.count('read') > 0 and calls.count('save') > 0
    saved = current(g)['phase']
    assert saved['state'] == 'stopped' and guard(g, saved)['active_attempt'] is None


@pytest.mark.parametrize('unknown_native', [False, True])
def test_unresolved_owner_stays_compact_without_archival_work(technical, monkeypatch, unknown_native):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    before = archive(g)
    if unknown_native:
        monkeypatch.setattr(g.q, '_rows', lambda p: [{'check_id': 'synthetic-held', 'phase': 'cleanup_unknown'}])
        monkeypatch.setattr(g.q, '_cancel', lambda p: None)
    else:
        claim_synthetic_observer(g, monkeypatch)
    calls = forbid_active_archive(monkeypatch)
    result = g.q.reconcile('phase-one')
    assert not result['stopped'] and result['status'] == 'blocked' and calls == []
    assert result['source_unchanged'] is None and result['product_budget_unchanged'] is None
    retained = current(g)
    assert retained['phase']['state'] == 'cleanup_unknown'
    assert retained['phase']['hold_receipt'] == result
    assert guard(g, phase)['active_attempt'] == 'phase-one'
    with g.q.runtime._db() as db:
        raw = json.loads(db.execute('SELECT record FROM technical_qualification').fetchone()[0])
    assert raw == before, 'Unknown cleanup must not rewrite the historical archive'


def test_watcher_start_failure_is_joined_without_launching_a_check(technical, monkeypatch):
    g = technical
    calls = []

    class Unstarted:
        ident = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError('injected observer thread start failure')

        def is_alive(self):
            return False

        def join(self, **kwargs):
            raise AssertionError('An unstarted thread cannot be joined')

    monkeypatch.setattr(qualification.threading, 'Thread', Unstarted)
    monkeypatch.setattr(g.q.runtime, '_check', lambda *args: calls.append('unexpected'))
    result = g.q.run('phase-one', g.recipe, max_seconds=30)
    assert calls == [] and result['stopped'] and result['status'] == 'blocked'
    assert any('thread start failure' in error for error in result['errors'])
    saved = current(g)['phase']
    assert saved['observer_joined']['watcher_joined'] is True
    assert saved['observer_joined']['cancellers_joined'] is True
    assert guard(g, saved)['active_attempt'] is None


@pytest.mark.parametrize('alteration', ['missing-trigger', 'wrong-trigger', 'wrong-index', 'wrong-table'])
def test_incomplete_or_replaced_control_schema_fails_closed(technical, alteration):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    with g.q.runtime._db() as db:
        if alteration in ('missing-trigger', 'wrong-trigger'):
            db.execute('DROP TRIGGER technical_archive_update_v1')
            if alteration == 'wrong-trigger':
                db.execute('CREATE TRIGGER technical_archive_update_v1 AFTER UPDATE ON technical_qualification BEGIN SELECT 1; END')
        elif alteration == 'wrong-index':
            db.execute('DROP INDEX technical_active_phase_v1')
            db.execute('CREATE INDEX technical_active_phase_v1 ON technical_phase_control(phase_id)')
        else:
            db.execute('ALTER TABLE technical_phase_control ADD COLUMN unsupported TEXT')
    with pytest.raises(Rejected, match='mechanism|schema'):
        qualification.resource_payload(g.q.runtime.path, exact_recipe(g, phase))
    assert guard(g, phase)['active_attempt'] == 'phase-one'


def test_direct_archive_sql_update_invalidates_executable_control(technical, monkeypatch):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    claim_synthetic_observer(g, monkeypatch)
    recipe = exact_recipe(g, phase)
    qualification.resource_payload(g.q.runtime.path, recipe)
    with g.q.runtime._db() as db:
        db.execute('UPDATE technical_qualification SET record=record WHERE id=?', (g.q.qualification_id,))
    with pytest.raises(Rejected, match='archive binding'):
        qualification.resource_payload(g.q.runtime.path, recipe)
    assert guard(g, phase)['active_attempt'] == 'phase-one'


def test_expected_cas_cannot_change_original_phase_clocks(technical):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    before = current(g)
    with pytest.raises(Rejected, match='immutable|binding|clock|changed before transition'):
        change_control(g, deadline=phase['deadline'] + 60)
    assert current(g) == before


def test_missing_control_cannot_be_backfilled_into_execution(technical, monkeypatch):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    with g.q.runtime._db() as db:
        db.execute('DELETE FROM technical_phase_control')
    calls = []
    monkeypatch.setattr(g.q.runtime, '_check', lambda *args: calls.append('unexpected'))
    with pytest.raises(Rejected, match='reconcile|cleanup-only|current control'):
        g.q.run('phase-one', g.recipe, max_seconds=30)
    assert calls == []
    with pytest.raises(Rejected, match='current control|cleanup-only'):
        qualification.resource_payload(g.q.runtime.path, exact_recipe(g, phase))


def test_stale_running_archive_cannot_override_closing_control(technical):
    g = technical
    phase = g.q.begin('phase-one', g.recipe, max_seconds=30)
    change_control(g, state='closing')
    before = current(g)
    assert archive(g)['phases'][0]['state'] == 'running'
    with pytest.raises(Rejected, match='reconciliation'):
        g.q.run('phase-one', g.recipe, max_seconds=30)
    with pytest.raises(Rejected, match='unresolved'):
        g.q.begin('phase-two', g.recipe, max_seconds=30)
    assert current(g) == before
    assert current(g)['phase']['deadline'] == phase['deadline']


def test_historical_reconcile_cannot_clear_current_closing_control(technical):
    g = technical
    g.q.begin('older', g.recipe, max_seconds=30)
    g.q.reconcile('older')
    g.q.begin('phase-one', g.recipe, max_seconds=30)
    change_control(g, state='closing')
    before = current(g)
    with pytest.raises(Rejected, match='active private phase'):
        g.q.reconcile('older')
    assert current(g) == before
    assert archive(g)['active_phase'] == 'phase-one'


def test_two_runtime_contenders_have_one_observer_and_one_check(technical, monkeypatch):
    g = technical
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    entered, release = threading.Event(), threading.Event()
    calls, results, errors = [], [], []

    def check(*args):
        calls.append('first')
        entered.set()
        assert release.wait(5)
        return {'result': 'blocked'}

    def run():
        try:
            results.append(g.q.run('phase-one', g.recipe, max_seconds=30))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(g.q.runtime, '_check', check)
    monkeypatch.setattr(other.runtime, '_check', lambda *args: calls.append('second'))
    thread = threading.Thread(target=run)
    try:
        thread.start()
        assert entered.wait(5), errors
        with pytest.raises(Rejected, match='existing technical phase'):
            other.run('phase-one', g.recipe, max_seconds=30)
    finally:
        release.set()
        thread.join(timeout=5)
        other.close()
    assert not thread.is_alive() and errors == []
    assert calls == ['first'] and len(results) == 1 and results[0]['stopped']
