"""Exact-source local archive preview and disposable fake publication journal.

There is deliberately no network publisher. A successful local receipt never
claims Confluence authority, external publication or workflow closure.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Lock

from .projection import validate_snapshot
from .store import Rejected, canonical, digest, identifier, now


def coordinator_journal(path: Path, worker_writable_roots: tuple[Path, ...]) -> Path:
    """Keep coordinator history outside every declared assigned-worker root."""
    path = path.expanduser().absolute()
    if any(path.resolve().is_relative_to(root.expanduser().resolve()) for root in worker_writable_roots):
        raise Rejected('worker_writable_history', 'Configured local history must be outside assigned-worker writable roots')
    return path


@contextmanager
def _database(path: Path, *, read_only: bool = False):
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Rejected('unsafe_archive_path', 'Local archive database path must not contain symlinks')
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, (os.O_RDONLY if read_only else os.O_CREAT | os.O_RDWR) | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Rejected('unsafe_archive_permissions', 'Local archive database must be a private user-owned regular file')
    finally:
        os.close(fd)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if read_only else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def preview(snapshot: dict, milestone: str) -> dict:
    validate_snapshot(snapshot)
    identifier(milestone)
    if snapshot['collector']['health'] != 'current':
        raise Rejected('archive_source_unavailable', 'A failed collector cannot prepare a new archive')
    payload = {'format': 'aos-local-milestone-v1', 'work_id': snapshot['work_id'], 'milestone': milestone,
               'source_revision': snapshot['source_revision'], 'source_digest': snapshot['source_digest'],
               'source_timestamp': snapshot['source_timestamp'], 'source': snapshot['source'],
               'publication_boundary': 'LOCAL PREVIEW ONLY; external destination, audience and authority unset; live publication unqualified'}
    return {'payload': payload, 'payload_digest': digest(payload),
            'destination': None, 'audience': None, 'external_publication_authorized': False}


class LocalFakeDestination:
    """Create-only versioned destination. Exact replay returns the saved version."""
    def __init__(self, path: Path):
        self.path = path
        self._fault = lambda stage: None
        with _database(path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'page'}:
                raise Rejected('archive_database_scope', 'Fake destination must be a separate local archive database')
            db.execute('''CREATE TABLE IF NOT EXISTS page (
                key TEXT PRIMARY KEY, version INTEGER NOT NULL, intent_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL, payload TEXT NOT NULL)''')

    def read(self, key: str) -> dict | None:
        with _database(self.path) as db:
            row = db.execute('SELECT * FROM page WHERE key=?', (key,)).fetchone()
            return dict(row) if row else None

    def create(self, key: str, intent_id: str, payload: dict) -> dict:
        encoded, fingerprint = canonical(payload), digest(payload)
        with _database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM page WHERE key=?', (key,)).fetchone()
            if row:
                if (row['intent_id'] != intent_id or row['payload_digest'] != fingerprint or row['payload'] != encoded):
                    raise Rejected('archive_conflict', 'Destination changed or already belongs to a different intent; never overwrite')
                return dict(row)
            self._fault('before_destination_write')
            db.execute('INSERT INTO page VALUES(?,1,?,?,?)', (key, intent_id, fingerprint, encoded))
            self._fault('after_destination_write_before_commit')
            return dict(db.execute('SELECT * FROM page WHERE key=?', (key,)).fetchone())


class ArchiveJournal:
    """Durable intent first, external effects second, verified readback last.

    This separate journal does not migrate or modify the decision/runtime ledger.
    Every failed/interrupted attempt remains alongside later retry evidence.
    """
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self._fault = lambda stage: None
        with _database(path, read_only=read_only) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'intent', 'attempt', 'sqlite_sequence'}:
                raise Rejected('archive_database_scope', 'Archive intent journal must be separate from authoritative state')
            if read_only:
                if not {'intent', 'attempt'}.issubset(tables):
                    raise Rejected('archive_unavailable', 'Local history journal schema is unavailable')
                return
            db.executescript('''
                CREATE TABLE IF NOT EXISTS intent (
                    intent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, work_id TEXT NOT NULL,
                    milestone TEXT NOT NULL, destination TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(work_id,milestone,destination));
                CREATE TABLE IF NOT EXISTS attempt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
                    detail TEXT, receipt TEXT);
            ''')

    def prepare(self, intent_id: str, snapshot: dict, milestone: str, destination: LocalFakeDestination) -> dict:
        if self.read_only:
            raise Rejected('read_only', 'Local history collection cannot prepare or publish milestones')
        identifier(intent_id)
        if type(destination) is not LocalFakeDestination:
            raise Rejected('local_destination_required', 'Only the local fake destination is implemented and qualified')
        package = preview(snapshot, milestone)
        payload = package['payload']
        target = str(destination.path.expanduser().absolute())
        if target == str(self.path.expanduser().absolute()):
            raise Rejected('separate_archive_journals', 'Intent and destination stores must be distinct')
        fingerprint = digest([payload, target])
        with _database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM intent WHERE intent_id=? OR (work_id=? AND milestone=? AND destination=?)',
                             (intent_id, snapshot['work_id'], milestone, target)).fetchone()
            if old:
                if old['intent_id'] != intent_id or old['fingerprint'] != fingerprint:
                    raise Rejected('archive_intent_conflict', 'Retain the original exact-source intent; changed source needs a new milestone')
                return self._intent(old)
            self._fault('before_intent_write')
            db.execute('INSERT INTO intent VALUES(?,?,?,?,?,?,?)',
                       (intent_id, fingerprint, snapshot['work_id'], milestone, target, canonical(payload), now()))
            self._fault('after_intent_write_before_commit')
            row = db.execute('SELECT * FROM intent WHERE intent_id=?', (intent_id,)).fetchone()
            return self._intent(row)

    @staticmethod
    def _intent(row) -> dict:
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['status'] = 'local-intent-only; external publication unqualified'
        return result

    def read(self, intent_id: str) -> dict:
        with _database(self.path, read_only=self.read_only) as db:
            row = db.execute('SELECT * FROM intent WHERE intent_id=?', (intent_id,)).fetchone()
            if row is None:
                raise Rejected('archive_intent_missing', 'Prepare and read back an exact-source intent first')
            result = self._intent(row)
            result['attempts'] = [{**dict(a), 'receipt': json.loads(a['receipt']) if a['receipt'] else None}
                                  for a in db.execute('SELECT * FROM attempt WHERE intent_id=? ORDER BY id', (intent_id,))]
            return result

    def publish_local(self, intent_id: str, destination: LocalFakeDestination) -> dict:
        if self.read_only:
            raise Rejected('read_only', 'Local history collection cannot prepare or publish milestones')
        if type(destination) is not LocalFakeDestination:
            raise Rejected('local_destination_required', 'No real external publication adapter exists')
        intent = self.read(intent_id)
        target = str(destination.path.expanduser().absolute())
        if intent['destination'] != target or intent['fingerprint'] != digest([intent['payload'], target]):
            raise Rejected('archive_intent_conflict', 'Intent payload/destination differs from durable preparation')
        payload = intent['payload']
        if digest(payload['source']) != payload['source_digest']:
            raise Rejected('archive_source_mismatch', 'Exact source bytes do not match retained identity')
        key = payload['work_id'] + ':' + payload['milestone']
        with _database(self.path) as db:
            attempt = db.execute("INSERT INTO attempt(intent_id,started_at,state) VALUES(?,?,'started')", (intent_id, now())).lastrowid
        try:
            self._fault('after_attempt_before_destination')
            saved = destination.create(key, intent_id, payload)
            self._fault('after_destination_before_readback')
            observed = destination.read(key)
            if (observed != saved or observed['version'] != 1 or observed['intent_id'] != intent_id
                    or observed['payload'] != canonical(payload) or observed['payload_digest'] != digest(payload)):
                raise Rejected('archive_readback_failed', 'Destination saved version/readback differs from exact intent')
            receipt = {'format': 'aos-local-publication-receipt-v1', 'intent_id': intent_id,
                       'destination': target, 'key': key, 'saved_version': observed['version'],
                       'payload_digest': observed['payload_digest'], 'source_digest': payload['source_digest'],
                       'source_revision': payload['source_revision'], 'readback': 'verified',
                       'external_publication': False, 'observed_at': now()}
            self._fault('after_readback_before_receipt')
            with _database(self.path) as db:
                db.execute("UPDATE attempt SET state='verified-local',finished_at=?,receipt=? WHERE id=?",
                           (now(), canonical(receipt), attempt))
                self._fault('after_receipt_write_before_commit')
            # Fresh connection readback, including commit errors, before success.
            retained = next(a for a in self.read(intent_id)['attempts'] if a['id'] == attempt)
            if retained['id'] != attempt or retained['receipt'] != receipt:
                raise Rejected('archive_receipt_readback_failed', 'Journal receipt readback differs')
            return receipt
        except Exception as error:
            with _database(self.path) as db:
                db.execute("UPDATE attempt SET state='failed',finished_at=?,detail=? WHERE id=?", (now(), type(error).__name__ + ': ' + str(error), attempt))
            raise

    def history(self, work_id: str) -> list[dict]:
        """One read transaction; no source payloads, file paths or raw errors exposed."""
        identifier(work_id)
        with _database(self.path, read_only=True) as db:
            db.execute('BEGIN')
            records = []
            for row in db.execute('SELECT * FROM intent WHERE work_id=? ORDER BY created_at,intent_id', (work_id,)):
                payload = json.loads(row['payload'])
                if (payload['work_id'] != work_id or digest(payload['source']) != payload['source_digest']
                        or row['fingerprint'] != digest([payload, row['destination']])):
                    raise Rejected('archive_source_mismatch', 'Local history source differs from its exact retained intent')
                attempts = []
                for attempt in db.execute('SELECT * FROM attempt WHERE intent_id=? ORDER BY id', (row['intent_id'],)):
                    receipt = json.loads(attempt['receipt']) if attempt['receipt'] else None
                    if receipt and (receipt['source_digest'] != payload['source_digest']
                                    or receipt['source_revision'] != payload['source_revision']
                                    or receipt['external_publication'] is not False):
                        raise Rejected('archive_receipt_readback_failed', 'Local history receipt differs from its retained source')
                    attempts.append({'id': attempt['id'], 'state': attempt['state'], 'started_at': attempt['started_at'],
                                     'finished_at': attempt['finished_at'],
                                     'error_class': attempt['detail'].split(':', 1)[0] if attempt['detail'] else None,
                                     'receipt': {key: receipt[key] for key in ('source_digest', 'source_revision', 'saved_version', 'readback', 'external_publication')}
                                                if receipt else None})
                records.append({'intent_id': row['intent_id'], 'work_id': work_id, 'milestone': row['milestone'],
                                'source_revision': payload['source_revision'], 'source_digest': payload['source_digest'],
                                'prepared_at': row['created_at'], 'attempts': attempts,
                                'status': attempts[-1]['state'] if attempts else 'prepared-local', 'external_publication': False})
            return records


class LocalHistoryCollector:
    """Read-only local history observation; retain earlier attempts on collection failure."""
    def __init__(self, path: Path, work_id: str):
        self.path, self.work_id = path, work_id
        self.last_good = None
        self._lock = Lock()

    def collect(self) -> dict:
        with self._lock:
            attempted = now()
            try:
                records = ArchiveJournal(self.path, read_only=True).history(self.work_id)
                value = {'health': 'current', 'work_id': self.work_id, 'records': records, 'records_digest': digest(records),
                         'observed_at': attempted, 'last_attempt_at': attempted, 'failure': None,
                         'boundary': 'Local preparation/fake destination only; external publication unqualified'}
                self.last_good = deepcopy(value)
                return value
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                value = deepcopy(self.last_good) if self.last_good else {
                    'health': 'unavailable', 'work_id': self.work_id, 'records': [], 'records_digest': None,
                    'observed_at': None, 'boundary': 'Local history unavailable; no external publication inferred'}
                value.update(health='failed', last_attempt_at=attempted, failure=type(error).__name__ + ': local history collection failed')
                return value
