"""Historical no-launch observations survive later OS cache changes."""
import copy
import hashlib
import json
import sqlite3

import pytest

from agentic_tools.aos import native_preparation, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, digest
from test_aos_native_preparation import prepared_native, run_check, saved


def raw_record(p):
    with p.runtime._db() as db:
        return db.execute('SELECT record FROM native_check WHERE id=?', (p.check_id,)).fetchone()[0]


def complete(p, monkeypatch, *, profile=False):
    p.os_cache = p.temporary / 'test-9876'
    p.os_cache.write_bytes(b'original OS cache')
    def fail_profile(recipe, row, *args):
        if profile:
            path = p.state / 'retained-profile.sb'
            path.write_text('(version 1)\n(deny default)\n')
            row['native_sandbox_boundary'] = {
                'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'temporary_before': native_sandbox.temporary_snapshot(recipe),
                'contract': recipe['native_sandbox'], 'network_observation': None}
            p.runtime._native_save(row)
        raise Rejected('native_sandbox', 'SIMULATED failure before any child')
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', fail_profile)
    with pytest.raises(Rejected, match='native_sandbox'):
        run_check(p)
    row = saved(p)
    p.check_id = row['check_id']
    assert row['phase'] == 'clean'
    assert p.calls == [] and p.renderers == []
    assert str(p.os_cache) in row['cleanup']['native_temporary_effects']['before']
    return row


@pytest.mark.parametrize('later_change', ['write', 'delete', 'replace', 'symlink'])
def test_completed_failure_survives_later_os_change_and_actual_close(prepared_native, monkeypatch, later_change):
    p = prepared_native
    row = complete(p, monkeypatch)
    retained_raw = raw_record(p)
    if later_change == 'write':
        p.os_cache.write_bytes(b'later legitimate cache contents')
    elif later_change == 'delete':
        p.os_cache.unlink()
    else:
        original = p.temporary / 'preserved-old-cache'
        p.os_cache.rename(original)
        if later_change == 'replace':
            p.os_cache.write_bytes(b'new inode')
        else:
            p.os_cache.symlink_to(original)
    assert p.runtime._native_stop(p.check_id) == row
    reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
    assert reopened._native_stop(p.check_id) == row
    reopened.close()  # Exercise the real historical sweep, not fixture teardown's stub.
    p.runtime.close()
    assert raw_record(p) == retained_raw
    assert p.calls == [] and p.renderers == []


def test_live_preparation_still_rejects_changed_preexisting_resource(prepared_native, monkeypatch):
    p = prepared_native
    protected = p.temporary / 'test-9876'
    protected.write_text('before')
    def change_during_preparation(*args):
        protected.write_text('changed during this preparation')
        raise Rejected('native_sandbox', 'SIMULATED early failure')
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', change_during_preparation)
    with pytest.raises(Rejected, match='protected preexisting temporary resource changed'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'cleanup' not in row and 'preparation_failure' not in row
    assert p.calls == [] and p.renderers == []


@pytest.mark.parametrize('damage', ['observed', 'preserved', 'before-type', 'after-missing', 'malformed',
                                    'metadata', 'profile-before', 'profile-hash', 'row-before'])
def test_coherently_rehashed_invalid_historical_observation_is_rejected(prepared_native, monkeypatch, damage):
    p = prepared_native
    row = complete(p, monkeypatch, profile=True)
    effects = row['cleanup']['native_temporary_effects']
    if damage == 'observed':
        effects['observed'] = False
    elif damage == 'preserved':
        effects['preexisting_preserved'] = False
    elif damage == 'before-type':
        effects['before'] = []
    elif damage == 'after-missing':
        effects['after'].pop(str(p.os_cache))
    elif damage == 'metadata':
        effects['after'][str(p.os_cache)]['size'] += 1
    elif damage == 'malformed':
        effects['before'][str(p.os_cache)] = effects['after'][str(p.os_cache)] = {'size': 1}
    elif damage == 'profile-before':
        row['native_sandbox_boundary']['temporary_before'] = {}
        row['preparation_failure']['proof']['profile']['temporary_before'] = {}
    elif damage == 'profile-hash':
        effects['profile_sha256'] = 'f' * 64
    else:
        row['native_temporary_before'] = {}
        row['preparation_failure']['proof']['identity'] = native_preparation._identity(row)
    row['preparation_failure']['effects'] = copy.deepcopy(row['cleanup'])
    row['preparation_failure']['receipt_digest'] = digest([row['preparation_failure']['proof'], row['cleanup']])
    with pytest.raises(Rejected, match='temporary'):
        native_preparation.verify(row)


def held(p, monkeypatch):
    from agentic_tools.aos import native_journal
    completed = complete(p, monkeypatch)
    with p.runtime._db() as db:
        native_journal.initialize(db)
        assert native_journal.verify(db)
    broken = copy.deepcopy(completed)
    broken['phase'] = 'cleanup_unknown'
    broken.pop('cleanup')
    p.runtime._native_save(broken)
    request = {'operation_id': 'restore-preparation-1', 'check_id': p.check_id,
               'expected_record_sha256': hashlib.sha256(raw_record(p).encode()).hexdigest(),
               'receipt_digest': broken['preparation_failure']['receipt_digest']}
    return completed, broken, request


def tables(p):
    with p.runtime._db() as db:
        return [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def other_records(p):
    with p.runtime._db() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                 if r[0] not in ('native_check', 'native_preparation_restoration', 'native_check_revision')]
        return {name: list(db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')) for name in names}


def test_restoration_preview_atomic_apply_and_exact_idempotent_replay(prepared_native, monkeypatch):
    p = prepared_native
    completed, broken, request = held(p, monkeypatch)
    p.os_cache.unlink()  # Legitimate later disappearance cannot erase old evidence.
    before, before_tables, unrelated = raw_record(p), tables(p), other_records(p)
    with p.runtime._db() as db:
        revision = db.execute('SELECT revision FROM native_check_revision').fetchone()[0]
    preview = native_preparation.restore_completed_failure(p.runtime.path, request)
    assert preview['applied'] is False and raw_record(p) == before and tables(p) == before_tables
    audit = native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
    assert audit['applied'] is True and audit['changed_fields'] == ['phase', 'cleanup']
    assert saved(p) == completed
    assert saved(p)['preparation_failure'] == broken['preparation_failure']
    assert other_records(p) == unrelated
    with p.runtime._db() as db:
        assert db.execute('SELECT revision FROM native_check_revision').fetchone()[0] == revision + 1
    retained = raw_record(p)
    # Completed-operation replay returns the saved historical audit without
    # needing fresh filesystem observations or rewriting its exact row.
    monkeypatch.setattr(native_preparation, 'verify', lambda row: pytest.fail('Replayed operation must not revalidate'))
    assert native_preparation.restore_completed_failure(p.runtime.path, request, apply=True) == audit
    assert raw_record(p) == retained
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_preparation_restoration').fetchone()[0] == 1
    assert p.calls == [] and p.renderers == []


def test_restoration_supports_wholly_absent_legacy_projection(prepared_native, monkeypatch):
    p = prepared_native
    completed, _, request = held(p, monkeypatch)
    with p.runtime._db() as db:
        for suffix in ('insert', 'update', 'delete'):
            db.execute('DROP TRIGGER native_check_control_' + suffix)
        db.execute('DROP INDEX native_check_control')
        db.execute('DROP TABLE native_check_revision')
    assert native_preparation.restore_completed_failure(p.runtime.path, request)['applied'] is False
    assert native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)['applied'] is True
    assert saved(p) == completed and 'native_check_revision' not in tables(p)


@pytest.mark.parametrize('missing', ['index', 'trigger', 'revision'])
def test_restoration_rejects_partial_compact_projection(prepared_native, monkeypatch, missing):
    p = prepared_native
    _, _, request = held(p, monkeypatch)
    with p.runtime._db() as db:
        db.execute({'index': 'DROP INDEX native_check_control',
                    'trigger': 'DROP TRIGGER native_check_control_update',
                    'revision': 'DROP TABLE native_check_revision'}[missing])
    before = raw_record(p)
    for apply in (False, True):
        with pytest.raises(Rejected, match='native_journal'):
            native_preparation.restore_completed_failure(p.runtime.path, request, apply=apply)
    assert raw_record(p) == before and 'native_preparation_restoration' not in tables(p)
    close = p.runtime.close
    def close_with_corrupt_schema():
        with pytest.raises(Rejected) as error:
            close()
        assert error.value.code == 'native_journal'
        assert raw_record(p) == before and 'native_preparation_restoration' not in tables(p)
    monkeypatch.setattr(p.runtime, 'close', close_with_corrupt_schema)


@pytest.mark.parametrize('damage', ['record', 'receipt', 'check', 'operation-conflict'])
def test_restoration_rejects_stale_or_conflicting_exact_request(prepared_native, monkeypatch, damage):
    p = prepared_native
    _, _, request = held(p, monkeypatch)
    if damage == 'operation-conflict':
        native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
        request['receipt_digest'] = 'sha256:' + '0' * 64
    else:
        key, value = {'record': ('expected_record_sha256', '0' * 64),
                      'receipt': ('receipt_digest', 'sha256:' + '0' * 64),
                      'check': ('check_id', 'nc-missing')}[damage]
        request[key] = value
    before = raw_record(p)
    with p.runtime._db() as db:
        revision = db.execute('SELECT revision FROM native_check_revision').fetchone()[0]
    with pytest.raises(Rejected):
        native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
    assert raw_record(p) == before
    with p.runtime._db() as db:
        assert db.execute('SELECT revision FROM native_check_revision').fetchone()[0] == revision


@pytest.mark.parametrize('damage', ['intent', 'owner', 'status', 'receipt', 'effects'])
def test_restoration_never_completes_unknown_launch_or_unproven_effects(prepared_native, monkeypatch, damage):
    p = prepared_native
    _, broken, request = held(p, monkeypatch)
    if damage == 'intent':
        broken['phase'] = 'intent'
    elif damage == 'owner':
        broken['owner'] = {'pid': 123}
    elif damage == 'status':
        from pathlib import Path
        Path(broken['status']).write_text('{}')
    elif damage == 'receipt':
        broken['preparation_failure']['proof']['supervisor_popen'] = 'possibly_called'
    else:
        broken['preparation_failure']['effects']['native_temporary_effects']['preexisting_preserved'] = False
    p.runtime._native_save(broken)
    request['expected_record_sha256'] = hashlib.sha256(raw_record(p).encode()).hexdigest()
    before = raw_record(p)
    with pytest.raises(Rejected):
        native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
    assert raw_record(p) == before
    assert 'native_preparation_restoration' not in tables(p)


def test_restoration_rechecks_exact_row_after_validation(prepared_native, monkeypatch):
    p = prepared_native
    _, broken, request = held(p, monkeypatch)
    verify = native_preparation.verify
    def concurrent_change(row):
        verify(row)
        broken['cancelled'] = True
        p.runtime._native_save(broken)
    monkeypatch.setattr(native_preparation, 'verify', concurrent_change)
    with pytest.raises(Rejected, match='changed before restoration commit'):
        native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
    assert saved(p) == broken
    assert 'native_preparation_restoration' not in tables(p)


def test_audit_failure_rolls_back_the_restored_row(prepared_native, monkeypatch):
    p = prepared_native
    _, _, request = held(p, monkeypatch)
    with p.runtime._db() as db:
        db.execute('CREATE TABLE native_preparation_restoration '
                   '(operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, record TEXT NOT NULL)')
        db.execute("CREATE TRIGGER interrupt_restoration BEFORE INSERT ON native_preparation_restoration "
                   "BEGIN SELECT RAISE(ABORT, 'SIMULATED interrupted audit'); END")
        revision = db.execute('SELECT revision FROM native_check_revision').fetchone()[0]
    before = raw_record(p)
    with pytest.raises(sqlite3.IntegrityError, match='interrupted audit'):
        native_preparation.restore_completed_failure(p.runtime.path, request, apply=True)
    assert raw_record(p) == before
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_preparation_restoration').fetchone()[0] == 0
        assert db.execute('SELECT revision FROM native_check_revision').fetchone()[0] == revision
