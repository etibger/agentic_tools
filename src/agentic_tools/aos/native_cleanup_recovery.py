"""Restore only completed native receipts downgraded by historical housekeeping.

An immutable prior journal proves the entire original row. Current census and
artifact checks still have to prove completion. This cannot finish a running
check, observe new filesystem effects, or release a resource/phase/budget hold.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from . import native_journal, native_processes
from .codex_runtime import CodexRuntime
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-native-cleanup-restoration-v1'
TABLE = 'native_cleanup_restoration'
SCHEMA = ('CREATE TABLE native_cleanup_restoration '
          '(operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, record TEXT NOT NULL)')
MAX_CHECKS = 64
MAX_ROW_BYTES = 64 * 1024 * 1024
MAX_SELECTED_BYTES = 512 * 1024 * 1024
MAX_PRIOR_BYTES = 4 * 1024 * 1024 * 1024


def _reject(message, *, conflict=False):
    raise Rejected('cleanup_restore_conflict' if conflict else 'cleanup_restore', message)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject('Duplicate evidence keys cannot establish exact historical completion')
        result[key] = value
    return result


def _identity(info):
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink',
                                               'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or path.resolve() != path
            or any(part.is_symlink() for part in (path, *path.parents))):
        _reject('An exact canonical non-aliased journal path is required')
    return path


def _journal(path, expected_sha=None):
    """Pin existing rollback-format evidence without creating SQLite auxiliaries."""
    path = _path(str(path))
    for suffix in ('-wal', '-shm', '-journal'):
        auxiliary = Path(str(path) + suffix)
        if auxiliary.exists() or auxiliary.is_symlink():
            _reject('A journal with pending SQLite auxiliaries cannot establish frozen recovery evidence')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_mode & 0o022 or before.st_size > MAX_PRIOR_BYTES):
            _reject('Recovery requires an owned bounded regular journal protected from other writers')
        header = stream.read(100)
        if len(header) != 100 or header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
            _reject('Recovery requires the original rollback-format SQLite journal')
        if expected_sha is not None:
            hashed = hashlib.sha256(header)
            total = len(header)
            while block := stream.read(1024 * 1024):
                total += len(block)
                if total > MAX_PRIOR_BYTES:
                    _reject('Prior journal exceeds its bounded evidence size')
                hashed.update(block)
            if hashed.hexdigest() != expected_sha:
                _reject('The immutable prior journal hash differs from the reviewed request')
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(path.stat(follow_symlinks=False)):
        _reject('Journal identity changed while reading recovery evidence')
    return _identity(after)


def _request(request, apply):
    if (type(apply) is not bool or not isinstance(request, dict)
            or set(request) != {'operation_id', 'prior_journal_path', 'prior_journal_sha256', 'checks'}):
        _reject('Exact batch restoration identities and prior journal evidence are required')
    identifier(request['operation_id'])
    if (not isinstance(request['prior_journal_path'], str)
            or not isinstance(request['prior_journal_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', request['prior_journal_sha256'])
            or not isinstance(request['checks'], list) or not 1 <= len(request['checks']) <= MAX_CHECKS):
        _reject('A bounded exact restoration batch and pinned prior journal are required')
    selected = set()
    for item in request['checks']:
        if not isinstance(item, dict) or set(item) != {'check_id', 'expected_record_sha256', 'prior_record_sha256', 'receipt_digest'}:
            _reject('Each selected check needs exact current, prior and full-receipt hashes')
        identifier(item['check_id'])
        if (item['check_id'] in selected or any(not isinstance(item[key], str)
                or not re.fullmatch('[0-9a-f]{64}', item[key]) for key in ('expected_record_sha256', 'prior_record_sha256'))
                or not isinstance(item['receipt_digest'], str)
                or not re.fullmatch('sha256:[0-9a-f]{64}', item['receipt_digest'])):
            _reject('Duplicate or malformed selected check evidence')
        selected.add(item['check_id'])
    return selected


def _saved(db, operation_id, fingerprint):
    objects = db.execute("SELECT type,name,sql FROM sqlite_schema WHERE name=? OR (type='trigger' AND tbl_name=?)",
                         (TABLE, TABLE)).fetchall()
    if not objects:
        return None
    if objects != [('table', TABLE, SCHEMA)]:
        _reject('Cleanup restoration audit schema differs from the supported exact table')
    row = db.execute('SELECT fingerprint,record FROM native_cleanup_restoration WHERE operation_id=?',
                     (operation_id,)).fetchone()
    if row is None:
        return None
    if row[0] != fingerprint:
        _reject('The restoration operation was already used with different evidence', conflict=True)
    audit = json.loads(row[1], object_pairs_hook=_unique)
    if audit.get('format') != FORMAT or audit.get('operation_id') != operation_id or audit.get('applied') is not True:
        _reject('The retained restoration audit is incomplete')
    return audit


def _passive(db, selected):
    native_journal.verify(db)
    if not native_journal._passive(db):
        _reject('An active resource observer or technical phase prevents historical restoration')
    identities = native_journal.identities(db)
    unresolved = {key for key, _, phase in identities if phase != 'clean'}
    if unresolved != selected or any(phase != 'cleanup_unknown' for key, _, phase in identities if key in selected):
        _reject('The complete unresolved native inventory must be exactly the reviewed cleanup batch')
    return native_journal.revision(db)


def _row(db, check_id):
    size = db.execute('SELECT length(CAST(record AS BLOB)) FROM native_check WHERE id=?', (check_id,)).fetchone()
    if not size or type(size[0]) is not int or not 0 < size[0] <= MAX_ROW_BYTES:
        _reject('A selected native row is missing or exceeds its evidence bound')
    found = db.execute('SELECT attempt_id,record FROM native_check WHERE id=?', (check_id,)).fetchone()
    if len(found[1].encode()) > MAX_ROW_BYTES:
        _reject('A selected native row exceeds its encoded byte bound')
    return found


def restore_completed_cleanup(journal_path, request, *, apply=False):
    """Preview or atomically restore one exact completed-cleanup batch.

    Current queries prove stopped identities only; they do not re-observe
    filesystem contents or invent cleanup. Idempotent retries return the saved
    historical audit and never repeat restoration.
    """
    try:
        return _restore(journal_path, request, apply=apply)
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as error:
        raise Rejected('cleanup_restore', 'Incomplete or changed historical cleanup evidence') from error


def _restore(journal_path, request, *, apply):
    selected = _request(request, apply)
    path = _path(str(journal_path))
    journal_identity = _journal(path)
    fingerprint = digest(['restore-completed-native-cleanup', request])
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as current:
        current.execute('PRAGMA query_only=ON')
        native_journal.verify(current)
        if saved := _saved(current, request['operation_id'], fingerprint):
            return saved
        revision = _passive(current, selected)
        prior_path = _path(request['prior_journal_path'])
        if prior_path == path or prior_path.samefile(path):
            _reject('Prior completion evidence must be independent of the current journal')
        prior_identity = _journal(prior_path, request['prior_journal_sha256'])
        prepared, audit_checks, total = [], [], 0
        with closing(sqlite3.connect(prior_path.as_uri() + '?mode=ro', uri=True)) as previous:
            previous.execute('PRAGMA query_only=ON')
            for item in request['checks']:
                attempt_id, raw = _row(current, item['check_id'])
                prior_attempt, prior_raw = _row(previous, item['check_id'])
                total += len(raw.encode()) + len(prior_raw.encode())
                if total > 2 * MAX_SELECTED_BYTES:
                    _reject('The selected batch exceeds its aggregate evidence bound')
                if hashlib.sha256(raw.encode()).hexdigest() != item['expected_record_sha256']:
                    _reject('A current native row differs from the reviewed raw record', conflict=True)
                if hashlib.sha256(prior_raw.encode()).hexdigest() != item['prior_record_sha256']:
                    _reject('A prior native row differs from the independently pinned evidence')
                row = json.loads(raw, object_pairs_hook=_unique)
                receipt = row.get('receipt', {})
                cleanup = receipt.get('cleanup', {})
                if (row.get('check_id') != item['check_id'] or row.get('attempt_id') != attempt_id
                        or attempt_id != prior_attempt or row.get('phase') != 'cleanup_unknown' or 'cleanup' in row
                        or 'preparation_failure' in row or row.get('ownership_backend') != native_processes.BACKEND
                        or not isinstance(row.get('native_sandbox_boundary'), dict) or not row['native_sandbox_boundary']
                        or not isinstance(row.get('native_process_receipt'), dict) or not row['native_process_receipt']
                        or 'native_launch_disposition' in row or 'native_admission' in cleanup
                        or receipt.get('native_check_id') != item['check_id'] or digest(receipt) != item['receipt_digest']
                        or cleanup.get('stopped') is not True or cleanup.get('effects_reconciled') is not True
                        or cleanup.get('owned_processes') != [] or cleanup.get('process_ownership') != row['native_process_receipt']):
                    _reject('Only an ordinary completed kernel-census receipt can restore historical cleanup')
                restored = copy.deepcopy(row)
                restored.update(phase='clean', cleanup=copy.deepcopy(cleanup))
                restored_raw = canonical(restored)
                if restored_raw != prior_raw:
                    _reject('The current row has changes beyond the exact phase/cleanup housekeeping transform')
                CodexRuntime._verify_check_receipt(receipt)
                if CodexRuntime.verify_completed_native(restored) != restored:
                    _reject('Current completion verification changed the historical receipt')
                prepared.append((item['check_id'], attempt_id, raw, restored_raw))
                audit_checks.append({'check_id': item['check_id'], 'attempt_id': attempt_id,
                                     'prior_record_sha256': item['expected_record_sha256'],
                                     'restored_record_sha256': item['prior_record_sha256'],
                                     'original_receipt_digest': item['receipt_digest']})
        if (_journal(prior_path) != prior_identity or _journal(path) != journal_identity
                or _passive(current, selected) != revision):
            _reject('Journal or evidence identity changed during completion verification', conflict=True)
    audit = {'format': FORMAT, 'operation_id': request['operation_id'], 'request': copy.deepcopy(request),
             'prior_journal': {'path': str(prior_path), 'sha256': request['prior_journal_sha256']},
             'checks': audit_checks, 'changed_fields': ['phase', 'cleanup'],
             'native_revision_before': revision, 'native_revision_after': revision + len(prepared),
             'effects_reobserved': False, 'budget_or_resource_changes': False,
             'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0}
    if not apply:
        return {**audit, 'applied': False}
    if _journal(path) != journal_identity:
        _reject('The current journal identity changed before opening restoration', conflict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if _journal(path) != journal_identity:
            _reject('The current journal identity changed before restoration commit', conflict=True)
        native_journal.verify(db)
        if saved := _saved(db, request['operation_id'], fingerprint):
            return saved
        if _passive(db, selected) != revision or _journal(prior_path) != prior_identity:
            _reject('Passive ownership or prior completion evidence changed before restoration', conflict=True)
        for check_id, attempt_id, raw, _ in prepared:
            if db.execute('SELECT attempt_id,record FROM native_check WHERE id=?', (check_id,)).fetchone() != (attempt_id, raw):
                _reject('A native row changed before restoration commit', conflict=True)
        if not db.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (TABLE,)).fetchone():
            db.execute(SCHEMA)
        for check_id, _, raw, restored_raw in prepared:
            updated = db.execute('UPDATE native_check SET record=? WHERE id=? AND record=?', (restored_raw, check_id, raw))
            if updated.rowcount != 1:
                _reject('A native row changed during restoration', conflict=True)
        if native_journal.revision(db) != audit['native_revision_after']:
            _reject('The native identity revision did not cover the exact restored batch')
        audit.update(applied=True, recorded_at=now())
        db.execute('INSERT INTO native_cleanup_restoration VALUES(?,?,?)',
                   (request['operation_id'], fingerprint, canonical(audit)))
    return audit
