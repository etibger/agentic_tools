"""Compact native identity reads use SQLite indexes, not historical receipts."""
import json
import sqlite3
from pathlib import Path

import pytest

from agentic_tools.aos import native_journal as journal
from agentic_tools.aos.store import Rejected


def connection(path=':memory:'):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE native_check (id TEXT PRIMARY KEY, attempt_id TEXT, record TEXT NOT NULL)')
    return db


def record(check_id='nc-one', *, output='/fixture/check', phase='clean', **extra):
    return {'check_id': check_id, 'attempt_id': None, 'output': output, 'phase': phase, **extra}


def insert(db, value):
    db.execute('INSERT INTO native_check VALUES(?,?,?)',
               (value['check_id'], value.get('attempt_id'), json.dumps(value)))


@pytest.fixture
def db():
    value = connection()
    assert journal.initialize(value)
    value.commit()
    try:
        yield value
    finally:
        value.close()


def test_clean_legacy_backfill_preserves_full_bytes_and_is_idempotent():
    with connection() as db:
        insert(db, record(payload='historical evidence'))
        raw = db.execute('SELECT * FROM native_check').fetchall()
        db.commit()
        assert not journal.verify(db, allow_absent=True)
        assert journal.initialize(db)
        assert journal.identities(db) == [('nc-one', '/fixture/check', 'clean')]
        assert journal.revision(db) == 0
        assert journal.initialize(db)
        assert db.execute('SELECT * FROM native_check').fetchall() == raw


@pytest.mark.parametrize('phase', ['intent', 'running', 'cleanup_unknown', None, 'unknown'])
def test_unresolved_legacy_stays_cleanup_only_until_actual_clean_update(phase):
    with connection() as db:
        insert(db, record(phase=phase))
        db.commit()
        assert not journal.initialize(db)
        assert not journal.verify(db, allow_absent=True)
        with pytest.raises(Rejected, match='legacy cleanup'):
            journal.active_rows(db)
        # This is the ordinary direct-SQL recovery route, not a rebuild/reset.
        db.execute('UPDATE native_check SET record=? WHERE id=?',
                   (json.dumps(record()), 'nc-one'))
        db.commit()
        assert journal.initialize(db)
        assert journal.active_rows(db) == []


def test_malformed_legacy_json_does_not_create_partial_metadata():
    with connection() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-bad', None, '{'))
        db.commit()
        assert not journal.initialize(db)
        assert not journal.verify(db, allow_absent=True)
        assert db.execute('SELECT record FROM native_check').fetchone()[0] == '{'


def test_every_direct_write_advances_revision_including_same_phase_and_replace(db):
    insert(db, record(phase='running'))
    assert journal.revision(db) == 1
    db.execute('UPDATE native_check SET record=? WHERE id=?',
               (json.dumps(record(phase='running', ownership='changed')), 'nc-one'))
    assert journal.revision(db) == 2
    assert journal.active_rows(db)[0]['ownership'] == 'changed'
    db.execute('UPDATE native_check SET record=? WHERE id=?', (json.dumps(record()), 'nc-one'))
    assert journal.revision(db) == 3 and journal.active_rows(db) == []
    db.execute('PRAGMA recursive_triggers=OFF')
    db.execute('INSERT OR REPLACE INTO native_check VALUES(?,?,?)',
               ('nc-one', None, json.dumps(record(phase='intent'))))
    assert journal.revision(db) == 4
    db.execute('DELETE FROM native_check WHERE id=?', ('nc-one',))
    assert journal.revision(db) == 5 and journal.identities(db) == []


@pytest.mark.parametrize('raw', [
    '{', 'null', '[]',
    '{"check_id":"nc-one","output":"/fixture/check","phase":"clean","phase":"running"}',
    r'{"check_id":"nc-one","output":"/fixture/check","phase":"clean","\u0070hase":"running"}',
    '{"check_id":"nc-one","check_id":"nc-other","output":"/fixture/check","phase":"clean"}',
    '{"check_id":"nc-one","output":"/fixture/check","output":null,"phase":"clean"}',
    '{"check_id":"nc-one","output":"/fixture/check","phase":"clean","attempt_id":null,"attempt_id":null}',
    json.dumps(record(check_id='nc-other')),
    json.dumps(record(output=None)), json.dumps(record(phase='unknown')),
    json.dumps(record(output='/fixture/../elsewhere')),
    json.dumps(record(output='relative/check')),
    json.dumps(record(attempt_id='unbound-attempt')),
])
def test_invalid_records_remain_present_and_block_compact_filtering(db, raw):
    db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-one', None, raw))
    assert journal.revision(db) == 1
    assert db.execute('SELECT record FROM native_check').fetchone()[0] == raw
    for query in (journal.identities, journal.active_rows,
                  lambda db: journal.rows_for_output(db, Path('/unrelated'))):
        with pytest.raises(Rejected, match='Native'):
            query(db)


@pytest.mark.parametrize('attempt', [{}, [], 3, True, '', '-bad', '.bad', ':bad', 'good\x00bad'])
def test_json_attempt_identity_cannot_masquerade_as_a_clean_sql_text_id(db, attempt):
    sql_attempt = json.dumps(attempt) if not isinstance(attempt, str) else attempt
    db.execute('INSERT INTO native_check VALUES(?,?,?)',
               ('nc-one', sql_attempt, json.dumps(record(attempt_id=attempt))))
    with pytest.raises(Rejected, match='ownership remains held'):
        journal.active_rows(db)


@pytest.mark.parametrize('active', ['observer', {}, 3, 'missing'])
def test_active_or_unknown_resource_observer_prevents_even_archive_scan(active):
    with connection() as db:
        db.execute('CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('CREATE TABLE technical_qualification(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        guard = {} if active == 'missing' else {'active_attempt': active}
        db.execute('INSERT INTO resource_limit VALUES(?,?)', ('guard', json.dumps(guard)))
        db.execute('INSERT INTO technical_qualification VALUES(?,?)', ('q', '{unread malformed archive'))
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        assert not journal.initialize(db)
        assert not journal.verify(db, allow_absent=True)
        assert not any('SELECT record FROM technical_qualification' in sql for sql in statements)


@pytest.mark.parametrize('archive', [
    {'active_phase': 'phase-one', 'phases': [{'state': 'running'}]},
    {'active_phase': None, 'phases': [{'state': 'cleanup_unknown'}]},
    {'active_phase': None, 'phases': [{'state': 'prepared'}]},
    {'phases': []}, {'active_phase': None}, None,
])
def test_active_or_unknown_legacy_archive_defers_migration(archive):
    with connection() as db:
        db.execute('CREATE TABLE technical_qualification(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('INSERT INTO technical_qualification VALUES(?,?)', ('q', json.dumps(archive)))
        db.commit()
        assert not journal.initialize(db)
        assert not journal.verify(db, allow_absent=True)


def test_stopped_legacy_archive_with_inactive_guard_allows_one_time_backfill():
    with connection() as db:
        db.execute('CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('INSERT INTO resource_limit VALUES(?,?)', ('guard', '{"active_attempt":null}'))
        db.execute('CREATE TABLE technical_qualification(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        db.execute('INSERT INTO technical_qualification VALUES(?,?)',
                   ('q', '{"active_phase":null,"phases":[{"state":"stopped"}]}'))
        insert(db, record())
        db.commit()
        assert journal.initialize(db)
        statements = []
        db.set_trace_callback(statements.append)
        assert journal.initialize(db)
        assert journal.active_rows(db) == []
        assert not any('SELECT record FROM technical_qualification' in sql for sql in statements)


def test_active_compact_phase_control_prevents_archive_scan():
    from agentic_tools.aos import technical_control
    with connection() as db:
        db.execute('CREATE TABLE technical_qualification(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        technical_control.initialize(db)
        db.execute('INSERT INTO technical_phase_control VALUES(?,?,?,?,?)', ('q', 'p', 1, 0, '{}'))
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        assert not journal.initialize(db)
        assert not any('SELECT record FROM technical_qualification' in sql for sql in statements)


@pytest.mark.parametrize('name', list(journal.SCHEMA))
def test_missing_schema_object_is_never_silently_recreated(db, name):
    kind = journal.SCHEMA[name][0]
    db.execute('DROP ' + kind + ' ' + name)
    for check in (lambda: journal.verify(db, allow_absent=True), lambda: journal.initialize(db)):
        with pytest.raises(Rejected, match='schema or revision triggers'):
            check()


def test_changed_or_extra_trigger_rejects_mixed_producers(db):
    name = 'native_check_control_update'
    db.execute('DROP TRIGGER ' + name)
    db.execute('CREATE TRIGGER ' + name + ' AFTER UPDATE ON native_check BEGIN SELECT 1; END')
    with pytest.raises(Rejected, match='schema or revision triggers'):
        journal.verify(db)
    db.execute('DROP TRIGGER ' + name)
    db.execute(journal.SCHEMA[name][1])
    db.execute('CREATE TRIGGER other_native_writer AFTER INSERT ON native_check BEGIN SELECT 1; END')
    with pytest.raises(Rejected, match='schema or revision triggers'):
        journal.verify(db)


def test_counter_reset_delete_replacement_and_overflow_fail_closed(db):
    insert(db, record())
    for sql in ('UPDATE native_check_revision SET revision=0',
                'DELETE FROM native_check_revision',
                'INSERT OR REPLACE INTO native_check_revision VALUES(1,0)',
                'UPDATE native_check_revision SET revision=9223372036854775808'):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert journal.revision(db) == 1


def test_compact_reads_have_no_table_column_or_json_function_opcodes(db):
    insert(db, record(payload='large history ' * 10000))
    insert(db, record('nc-active', phase='running'))
    opcodes = db.execute('EXPLAIN ' + journal.COMPACT).fetchall()
    index_root = db.execute('SELECT rootpage FROM sqlite_schema WHERE name=?', (journal.INDEX,)).fetchone()[0]
    index_cursors = {row[2] for row in opcodes if row[1] == 'OpenRead' and row[3] == index_root}
    assert index_cursors
    assert not any(row[1] in ('Function', 'PureFunc') for row in opcodes)
    columns = [row for row in opcodes if row[1] == 'Column']
    assert columns and all(row[2] in index_cursors for row in columns)
    assert len(journal.identities(db)) == 2


def test_only_selected_full_rows_are_decoded_and_path_components_bound_selection(db, monkeypatch):
    values = [record('nc-root', output='/fixture/check'),
              record('nc-child', output='/fixture/check/sub', phase='running'),
              record('nc-neighbor', output='/fixture/check-other', payload='must not decode'),
              record('nc-history', output='/elsewhere', payload='must not decode')]
    for value in values:
        insert(db, value)
    loads, decoded = journal.json.loads, []
    def exact_load(raw):
        assert 'must not decode' not in raw
        value = loads(raw)
        decoded.append(value['check_id'])
        return value
    monkeypatch.setattr(journal.json, 'loads', exact_load)
    assert [row['check_id'] for row in journal.active_rows(db)] == ['nc-child']
    assert [row['check_id'] for row in journal.rows_for_output(db, Path('/fixture/check'))] == ['nc-child', 'nc-root']
    assert decoded == ['nc-child', 'nc-child', 'nc-root']


def test_same_phase_concurrent_update_invalidates_full_row_selection(tmp_path, monkeypatch):
    path = tmp_path / 'journal.sqlite3'
    with connection(path) as db:
        assert journal.initialize(db)
        insert(db, record(phase='running'))
    db, other = sqlite3.connect(path), sqlite3.connect(path)
    original, changed = journal._all, False
    def race(connection, sql, parameters=()):
        nonlocal changed
        value = original(connection, sql, parameters)
        if sql == journal.COMPACT and not changed:
            changed = True
            other.execute('UPDATE native_check SET record=? WHERE id=?',
                          (json.dumps(record(phase='running', ownership='concurrent')), 'nc-one'))
            other.commit()
        return value
    monkeypatch.setattr(journal, '_all', race)
    try:
        with pytest.raises(Rejected, match='ownership changed'):
            journal.active_rows(db)
    finally:
        db.close()
        other.close()


def test_initialization_rolls_back_partial_schema_inside_callers_fresh_transaction(monkeypatch):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        original = journal.verify
        def fail_after_creation(db, **kwargs):
            if not kwargs.get('allow_absent'):
                raise Rejected('native_journal', 'injected schema verification failure')
            return original(db, **kwargs)
        monkeypatch.setattr(journal, 'verify', fail_after_creation)
        with pytest.raises(Rejected, match='injected'):
            journal.initialize(db)
        assert original(db, allow_absent=True) is False
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0


def test_callers_existing_history_transaction_is_not_released_or_migrated():
    with connection() as db:
        insert(db, record())
        assert db.in_transaction
        assert journal.initialize(db) is False
        assert db.in_transaction
        assert journal.verify(db, allow_absent=True) is False
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 1


def test_active_guard_is_read_without_requesting_a_busy_writer_lock(tmp_path):
    path = tmp_path / 'busy.sqlite3'
    with connection(path) as first:
        first.execute('CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        first.execute('INSERT INTO resource_limit VALUES(?,?)', ('guard', '{"active_attempt":"active"}'))
    reader = sqlite3.connect(path, timeout=0)
    writer = sqlite3.connect(path, timeout=0)
    try:
        writer.execute('BEGIN IMMEDIATE')
        statements = []
        reader.set_trace_callback(statements.append)
        assert journal.initialize(reader) is False
        assert not reader.in_transaction
        assert not any(sql.startswith('BEGIN') for sql in statements)
    finally:
        writer.rollback()
        writer.close()
        reader.close()


def test_committed_change_between_cold_read_and_writer_fence_defers_without_rescan(tmp_path, monkeypatch):
    path = tmp_path / 'race.sqlite3'
    with connection(path) as initial:
        insert(initial, record())
    reader, writer = sqlite3.connect(path), sqlite3.connect(path)
    original, scans = journal._all, []
    cold_sql = f'SELECT id,{journal.OUTPUT},{journal.PHASE} FROM native_check ORDER BY id'
    def race(db, sql, parameters=()):
        values = original(db, sql, parameters)
        if sql == cold_sql:
            scans.append(sql)
            insert(writer, record('nc-race', phase='intent'))
            writer.commit()
        return values
    monkeypatch.setattr(journal, '_all', race)
    try:
        assert journal.initialize(reader) is False
        assert not reader.in_transaction
        assert len(scans) == 1
        assert journal.verify(reader, allow_absent=True) is False
    finally:
        reader.close()
        writer.close()


def test_stopped_historical_control_may_have_an_older_archive_revision():
    from agentic_tools.aos import technical_control
    with connection() as db:
        db.execute('CREATE TABLE technical_qualification(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
        technical_control.initialize(db)
        db.execute('INSERT INTO technical_qualification VALUES(?,?)',
                   ('q', '{"active_phase":null,"phases":[{"id":"p","state":"stopped"}]}'))
        control = {'format': technical_control.FORMAT, 'qualification_id': 'q', 'active': False,
                   'archive_revision': 0, 'phase': {'id': 'p', 'state': 'stopped'}}
        db.execute('INSERT INTO technical_phase_control VALUES(?,?,?,?,?)', ('q', 'p', 0, 0, json.dumps(control)))
        db.commit()
        assert journal.initialize(db)
