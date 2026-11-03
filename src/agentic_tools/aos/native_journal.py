"""Compact, SQLite-maintained native identities; full receipts stay authoritative.

Only passive construction may install the index on entirely clean legacy
history. An absent index on unresolved legacy history permits cleanup readers,
but never new native admission. Every authoritative row mutation advances the
revision, including writes from older workers and direct-SQL recovery.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .store import Rejected, identifier


PHASES = ('intent', 'running', 'cleanup_unknown', 'clean')
OUTPUT = """CASE WHEN json_valid(record) THEN
    CASE WHEN json_type(record,'$.output')='text'
    THEN json_extract(record,'$.output') END END"""
# json_remove removes the first matching key. A remaining key (including a
# JSON null) detects duplicates that JSON1 and Python would otherwise disagree
# about. All parsing is guarded by CASE; invalid records remain visible as NULL.
PHASE = """CASE WHEN json_valid(record) THEN CASE WHEN
    json_type(record)='object'
    AND json_type(record,'$.check_id')='text'
    AND json_extract(record,'$.check_id') IS id
    AND json_type(record,'$.output')='text'
    AND json_type(record,'$.phase')='text'
    AND json_extract(record,'$.phase') IN ('intent','running','cleanup_unknown','clean')
    AND json_type(json_remove(record,'$.check_id'),'$.check_id') IS NULL
    AND json_type(json_remove(record,'$.output'),'$.output') IS NULL
    AND json_type(json_remove(record,'$.phase'),'$.phase') IS NULL
    AND json_type(json_remove(record,'$.attempt_id'),'$.attempt_id') IS NULL
    AND (json_type(record,'$.attempt_id') IS NULL
         OR json_type(record,'$.attempt_id') IN ('text','null'))
    AND (attempt_id IS NULL OR (typeof(attempt_id)='text' AND length(attempt_id) BETWEEN 1 AND 128
         AND substr(attempt_id,1,1) GLOB '[A-Za-z0-9]' AND instr(attempt_id,char(0))=0
         AND attempt_id NOT GLOB '*[^A-Za-z0-9_.:-]*'))
    AND json_extract(record,'$.attempt_id') IS attempt_id
    THEN json_extract(record,'$.phase') END END"""

TABLE = 'native_check_revision'
INDEX = 'native_check_control'
SCHEMA = {
    TABLE: ('table', "CREATE TABLE native_check_revision (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0))"),
    INDEX: ('index', f'CREATE INDEX {INDEX} ON native_check(id,{OUTPUT},{PHASE})'),
}
for event in ('INSERT', 'UPDATE', 'DELETE'):
    name = 'native_check_control_' + event.lower()
    SCHEMA[name] = ('trigger', f"""CREATE TRIGGER {name} AFTER {event} ON native_check BEGIN
        UPDATE native_check_revision SET revision=revision+1 WHERE singleton=1;
        SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'Native journal revision missing') END;
    END""")
# Guard the counter against accidental direct reset/removal. Incrementing it
# without a row change is conservative: it can only invalidate a read fence.
SCHEMA['native_check_revision_update'] = ('trigger', """CREATE TRIGGER native_check_revision_update BEFORE UPDATE ON native_check_revision BEGIN
    SELECT CASE WHEN NEW.singleton IS NOT OLD.singleton OR typeof(NEW.revision)!='integer'
        OR NEW.revision IS NOT OLD.revision+1 THEN RAISE(ABORT,'Native journal revision cannot be reset') END;
END""")
SCHEMA['native_check_revision_delete'] = ('trigger', """CREATE TRIGGER native_check_revision_delete BEFORE DELETE ON native_check_revision BEGIN
    SELECT RAISE(ABORT,'Native journal revision cannot be removed');
END""")
SCHEMA['native_check_revision_insert'] = ('trigger', """CREATE TRIGGER native_check_revision_insert BEFORE INSERT ON native_check_revision BEGIN
    SELECT RAISE(ABORT,'Native journal revision already initialized');
END""")
COMPACT = f'SELECT id,{OUTPUT},{PHASE} FROM native_check INDEXED BY {INDEX} ORDER BY id'


def _reject(message):
    raise Rejected('native_journal', message)


def _all(db, sql, parameters=()):
    cursor = db.execute(sql, parameters)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _counter(db):
    rows = _all(db, 'SELECT singleton,revision FROM native_check_revision')
    if (len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int
            or rows[0][1] < 0):
        _reject('Native journal revision is missing or invalid')
    return rows[0][1]


def verify(db: sqlite3.Connection, *, allow_absent: bool = False) -> bool:
    """Validate compact schema without reading or decoding historical records."""
    try:
        objects = {name: (kind, sql) for name, kind, sql in _all(db, """
            SELECT name,type,sql FROM sqlite_schema
            WHERE name GLOB 'native_check_control*' OR name GLOB 'native_check_revision*'
                OR (type='trigger' AND tbl_name='native_check')""")}
        if not objects:
            if allow_absent:
                return False
            _reject('Native identity index is absent; legacy cleanup is required before admission')
        if objects != SCHEMA:
            _reject('Native identity index schema or revision triggers are missing or changed')
        _counter(db)
        # Both counts use compact indexes. SQLite maintains their membership
        # atomically; the expression index cannot be directly edited by SQL.
        counts = [_all(db, 'SELECT COUNT(*) FROM native_check INDEXED BY ' + index)[0][0]
                  for index in (INDEX, 'sqlite_autoindex_native_check_1')]
        if counts[0] != counts[1]:
            _reject('Native identity index does not cover the authoritative rows')
        return True
    except sqlite3.Error as error:
        raise Rejected('native_journal', 'Native identity index cannot be verified') from error


def _path(value):
    if (not isinstance(value, str) or not value or '\x00' in value
            or '\n' in value or '\r' in value):
        _reject('Native output identity is missing or invalid')
    path = Path(value)
    if not path.is_absolute() or str(path) != value or '..' in path.parts:
        _reject('Native output identity must be an exact absolute path')
    return path


def _validate(rows):
    for check_id, output, phase in rows:
        try:
            identifier(check_id)
        except (Rejected, TypeError):
            _reject('Native check identity is missing or invalid')
        _path(output)
        if phase not in PHASES:
            _reject('Native record has invalid JSON, identity, or phase; ownership remains held')
    return rows


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate legacy control/archive key')
        value[key] = item
    return value


def _passive(db):
    """Refuse a cold migration while an observer or phase could still run."""
    tables = {row[0] for row in _all(db, "SELECT name FROM sqlite_schema WHERE type='table'")}
    if 'resource_limit' in tables:
        inactive = _all(db, """SELECT CASE WHEN json_valid(record) THEN
            json_type(record)='object' AND json_type(record,'$.active_attempt')='null'
            AND json_type(json_remove(record,'$.active_attempt'),'$.active_attempt') IS NULL
            END FROM resource_limit""")
        if any(row[0] != 1 for row in inactive):
            return False
    if 'technical_phase_control' in tables:
        from . import technical_control
        try:
            technical_control.verify(db)
            controls = _all(db, 'SELECT qualification_id,phase_id,active,record FROM technical_phase_control')
            if any(active != 0 for _, _, active, _ in controls):
                return False
            for qualification_id, phase_id, _, raw in controls:
                if len(raw.encode()) > technical_control.MAX_BYTES:
                    return False
                control = json.loads(raw, object_pairs_hook=_unique)
                # Older stopped controls legitimately retain an older archive
                # revision. This check proves passivity, never execution
                # authority; the cold archive scan below validates all phases.
                if (not isinstance(control, dict) or control.get('format') != technical_control.FORMAT
                        or not isinstance(control.get('phase'), dict)
                        or control.get('qualification_id') != qualification_id or control.get('active') is not False
                        or control.get('phase', {}).get('id') != phase_id
                        or control.get('phase', {}).get('state') != 'stopped'):
                    return False
        except (Rejected, ValueError, TypeError, KeyError, sqlite3.Error):
            return False
    elif 'technical_archive_revision' in tables:
        return False  # Partial phase metadata cannot establish passivity.
    if 'technical_qualification' in tables:
        # Only after compact guards prove no active observer/control is a cold
        # legacy archive scan permitted. This executes once, not on hot reads.
        for (raw,) in _all(db, 'SELECT record FROM technical_qualification'):
            try:
                archive = json.loads(raw, object_pairs_hook=_unique)
                if (not isinstance(archive, dict) or 'active_phase' not in archive
                        or archive['active_phase'] is not None or not isinstance(archive.get('phases'), list)
                        or any(not isinstance(phase, dict) or phase.get('state') != 'stopped'
                               for phase in archive['phases'])):
                    return False
            except (ValueError, TypeError):
                return False
    return True


def initialize(db: sqlite3.Connection) -> bool:
    """Install once on clean legacy history; caller owns transaction commit.

    False means wholly absent legacy metadata with unresolved rows, not an
    invitation to rebuild. Cleanup readers may explicitly allow this state.
    """
    if verify(db, allow_absent=True):
        return True
    owned_transaction = not db.in_transaction
    try:
        if not owned_transaction:
            # Never commit/release somebody else's transaction just to scan
            # old evidence. Only a freshly empty journal can initialize here.
            tables = {row[0] for row in _all(db, "SELECT name FROM sqlite_schema WHERE type='table'")}
            for name in ('native_check', 'resource_limit', 'technical_qualification', 'technical_phase_control'):
                if name in tables and _all(db, 'SELECT 1 FROM ' + name + ' LIMIT 1'):
                    return False
        version = _all(db, 'PRAGMA data_version')[0][0]
        if not _passive(db):
            return False
        legacy = _all(db, f'SELECT id,{OUTPUT},{PHASE} FROM native_check ORDER BY id')
        try:
            _validate(legacy)
        except Rejected:
            return False
        if any(phase != 'clean' for _, _, phase in legacy):
            return False
        if owned_transaction:
            # Close every read cursor before requesting the writer lock. Any
            # intervening commit invalidates the entire preflight; no rescan
            # or retry runs under this lock.
            db.execute('BEGIN IMMEDIATE')
            if (_all(db, 'PRAGMA data_version')[0][0] != version
                    or verify(db, allow_absent=True)):
                db.rollback()
                return False
        db.execute('SAVEPOINT native_journal_initialize')
        try:
            db.execute(SCHEMA[TABLE][1])
            db.execute('INSERT INTO native_check_revision VALUES(1,0)')
            for name, (_, sql) in SCHEMA.items():
                if name != TABLE:
                    db.execute(sql)
            verify(db)
        except BaseException:
            db.execute('ROLLBACK TO native_journal_initialize')
            db.execute('RELEASE native_journal_initialize')
            raise
        db.execute('RELEASE native_journal_initialize')
        return True
    except BaseException:
        if owned_transaction:
            db.rollback()
        raise


def identities(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Read every identity from the covering index; never omit invalid rows."""
    verify(db)
    return _validate(_all(db, COMPACT))


def revision(db: sqlite3.Connection) -> int:
    verify(db)
    return _counter(db)


def _selected(db, predicate):
    before = revision(db)
    selected = [row for row in identities(db) if predicate(row)]
    # The compact cursor is closed before any full receipt is read; every full
    # row is selected by its exact primary key, never a JSON/history predicate.
    records = []
    for check_id, output, phase in selected:
        found = _all(db, 'SELECT attempt_id,record FROM native_check WHERE id=?', (check_id,))
        if len(found) != 1:
            _reject('Native ownership changed during compact selection')
        records.append((check_id, output, phase, *found[0]))
    if revision(db) != before:
        _reject('Native ownership changed during compact selection')
    result = []
    for check_id, output, phase, attempt_id, raw in records:
        try:
            record = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise Rejected('native_journal', 'Selected native record is invalid') from error
        if (not isinstance(record, dict) or record.get('check_id') != check_id
                or record.get('output') != output or record.get('phase') != phase
                or record.get('attempt_id') != attempt_id):
            _reject('Selected native record differs from its indexed identity')
        result.append(record)
    return result


def active_rows(db: sqlite3.Connection) -> list[dict]:
    return _selected(db, lambda row: row[2] != 'clean')


def rows_for_output(db: sqlite3.Connection, root: Path) -> list[dict]:
    root = _path(str(root))
    return _selected(db, lambda row: Path(row[1]).is_relative_to(root))
