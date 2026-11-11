"""One exact feature amendment before the registered feature's first dispatch.

Preparation retains the original contract and accounting. A separately saved
technical decision is required before the guarded apply operation changes the
contract. This is not a general replan or an execution/approval shortcut.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import time

from pydantic import BaseModel, ConfigDict, Field

from . import instructions, readiness, legacy_recovery, assessment_evidence
from . import startup_failure_recovery as fencing
from .codex_runtime import candidate
from .coordinator import ConceptRequest, _unique_object, _nonfinite
from .lifecycle import stop_conflicts_in, stopped_with_effects
from .store import Rejected, Store, canonical, digest, identifier, now

FORMAT = 'aos-predispatch-feature-amendment-v1'
TABLE = 'predispatch_feature_amendment'
SCHEMA = {
    TABLE: 'CREATE TABLE predispatch_feature_amendment (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,record TEXT NOT NULL)',
    TABLE + '_update': "CREATE TRIGGER predispatch_feature_amendment_update BEFORE UPDATE ON predispatch_feature_amendment BEGIN SELECT RAISE(ABORT,'Settings rebind history is immutable'); END",
    TABLE + '_delete': "CREATE TRIGGER predispatch_feature_amendment_delete BEFORE DELETE ON predispatch_feature_amendment BEGIN SELECT RAISE(ABORT,'Settings rebind history is immutable'); END",
}
CODE_FIELDS = ('adapter_sha256', 'controller_sha256', 'live_qualification_sha256')
SLOTS = {'qualification-1': 900, 'qualification-2': 900, 'stage1-storage': 900,
    'stage1-runtime': 4500, 'stage2-import': 900, 'repair': 900, 'colima-transition': 3600,
    'dependency-generation-1': 900, 'dependency-generation-2': 900,
    'initial-imports-replay': 10800, 'initial-backup-restore': 7200, 'initial-final-verification': 3600,
    'runtime-recovery-1': 1800, 'runtime-recovery-2': 1800, 'native-handoff-qualification': 5400,
    'corrected-imports-replay': 10800, 'corrected-backup-restore': 7200, 'corrected-final-verification': 3600}
PLAN = {'format': 'rnf-coordinator-reservation-plan-v1', 'historical_seconds': 3300,
        'slots': SLOTS, 'planned_lifetime_seconds': 69900, 'effective_limit_seconds': 72000,
        'unallocated_seconds': 2100}
NEW_CONTROL_FIELDS = {'accounting_origin', 'coordinator_allowance_amendment', 'coordinator_reservation_plan',
    'checkpoint_wait_seconds', 'assessment_evidence_gate', 'native_handoff', 'native_handoff_sha256',
    'approved_di', 'approved_task_plan', 'native_catalog', 'native_catalog_sha256'}


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    decision_id: str
    original_controller_contract_path: str
    controller_contract_path: str
    original_readiness_receipt_path: str
    readiness_directory: str
    destination_directory: str
    recipe_path: str
    concept_update_path: str


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    decision_id: str
    packet_path: str
    packet_sha256: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    receipt_digest: str


def _require(condition, message):
    if not condition:
        raise Rejected('predispatch_feature_amendment', message)


def _file(path, *, immutable=False):
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path and
             not any(p.is_symlink() for p in (path, *path.parents)), 'Exact ordinary file path required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
                 before.st_nlink == 1 and before.st_size <= 16 * 1024 * 1024 and
                 (not immutable or stat.S_IMODE(before.st_mode) == 0o400), 'Invalid retained file ownership, mode or size')
        raw = stream.read()
    after = path.stat()
    _require(all(getattr(before, k) == getattr(after, k) for k in
                 ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')), 'File changed during readback')
    return raw, {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def _json(path, *, immutable=False):
    raw, reference = _file(path, immutable=immutable)
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    _require(isinstance(value, dict), 'Retained JSON must be an object')
    return value, reference


def _protected(store, path, *, directory=False):
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path and
             not any(p.is_symlink() for p in (path, *path.parents)), 'Private path aliases are forbidden')
    _require(all(path != root and not path.is_relative_to(root) and not root.is_relative_to(path)
                 for root in map(Path, store.worker_writable_roots)), 'Private evidence overlaps worker scope')
    target = path if directory else path.parent
    info = target.stat()
    _require(target.is_dir() and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
             'Existing private evidence directory must have mode 0700')
    return path


def _retain(path, value):
    raw = (canonical(value) + '\n').encode()
    if path.exists() or path.is_symlink():
        prior, reference = _file(path, immutable=True)
        _require(prior == raw, 'Immutable preparation already differs')
        return reference
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return _file(path, immutable=True)[1]


@contextmanager
def _accounting_lock(store, clock_path, *, shared=False):
    """Use the already-owned coordinator lock; never create replacement state."""
    path = _protected(store, Path(clock_path).parent / 'clock.lock')
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
                 before.st_nlink == 1 and not stat.S_IMODE(before.st_mode) & 0o077, 'Original clock lock is not protected')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('predispatch_feature_amendment', 'Coordinator accounting is currently owned') from error
        def check():
            after = path.stat()
            _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), 'Original clock lock identity changed')
        check()
        yield check
        check()
    finally:
        os.close(fd)


def _schema(db, *, create=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'predispatch_feature_amendment*' OR (tbl_name=? AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))", (TABLE,)).fetchall())
    if not found and create:
        for sql in SCHEMA.values():
            db.execute(sql)
        return
    _require(not found or found == SCHEMA, 'Immutable rebind audit schema changed')
    if found:
        return True
    return False


def _approval(store, decision_id, work_id, concept_digest):
    reader = Store(store.directory, store.worker_writable_roots, read_only=True)
    decision = reader.verify_decision(decision_id)
    with reader._connection() as db:
        row = db.execute('SELECT status FROM decision_readback WHERE decision_id=?', (decision_id,)).fetchone()
        challenge = db.execute('SELECT status FROM challenge WHERE challenge_id=?', (decision['challenge_id'],)).fetchone()
    _require(decision['work_id'] == work_id and decision['digest'] == concept_digest and
             decision['outcome'] == 'approved' and decision['provenance_kind'] == 'qualified-host-response' and
             decision['source_channel'] != 'simulated-host' and row and row['status'] == 'verified' and
             challenge and challenge['status'] == 'approved', 'Exact verified saved technical approval is required')
    approved_at = datetime.fromisoformat(decision['created_at'])
    _require(approved_at.tzinfo is not None and approved_at.timestamp() <= time.time(), 'Saved approval is later than the current clock')
    return decision


def _effective_contract(db, package, concept_digest, *, floor=0, ceiling=None):
    expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': concept_digest}
    prerequisites = dict(expected['prerequisites'])
    for row in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='prerequisites_reconciled' AND event_id>? AND (? IS NULL OR event_id<=?) ORDER BY event_id", (package['work_id'], floor, ceiling, ceiling)):
        event = json.loads(row[0])
        _require(event.get('operation_id') and isinstance(event.get('evidence'), dict), 'Malformed prerequisite history')
        for key, fact in event['evidence'].items():
            _require(key in prerequisites and set(fact) == {'satisfied', 'reference', 'observed_at'} and
                     type(fact['satisfied']) is bool and fact['reference'] and
                     datetime.fromisoformat(fact['observed_at']).tzinfo is not None, 'Unbound prerequisite history')
            prerequisites[key] = fact['satisfied']
    return {**expected, 'prerequisites': prerequisites}


def _tables(db, schema='main'):
    return {r[0] for r in db.execute(f"SELECT name FROM {schema}.sqlite_schema WHERE type='table'")}


def _retired_foreign(lifecycle, db, work_id, attempt_id):
    """Recognize a maintained retirement, retaining its unknown historical stop.

The retirement's proof is a prospective disposition, not positive historical
cleanup. No older state, runtime record, budget or adopted source is changed.
"""
    run, contract = lifecycle._run(db, work_id)
    if (run['execution'], run['control'], run['admitted']) != ('retired', 'retired', 0):
        return False
    events = list(db.execute("SELECT * FROM lifecycle_event WHERE work_id=? AND kind='legacy_retired'", (work_id,)))
    _require(len(events) == 1 and not stop_conflicts_in(db, work_id) and
             not db.execute('SELECT 1 FROM resource WHERE work_id=?', (work_id,)).fetchone() and
             not db.execute('SELECT 1 FROM lifecycle_event WHERE work_id=? AND event_id>?', (work_id, events[0]['event_id'])).fetchone(),
             'Historical retirement has missing proof or later ownership/conflicts')
    event = events[0]; detail = json.loads(event['detail'])
    _require(detail.get('attempt_id') == attempt_id and detail.get('historical_startup_outcome') == 'unknown' and
             detail.get('runtime_and_budget_modified') is False and detail.get('new_work_admitted') is False,
             'Historical retirement disposition differs')
    evidence = json.loads(legacy_recovery._protected(lifecycle.store,
        {'path': detail['evidence_path'], 'sha256': detail['evidence_sha256']}))
    _require(evidence == detail['evidence'] and evidence.get('format') == 'aos-legacy-recovery-v1' and
             evidence['work_id'] == work_id and evidence['attempt_id'] == attempt_id,
             'Historical retirement evidence changed')
    for ref in (evidence['host_provenance'], evidence['authority'], evidence['review']['report'], evidence['adoption']['delivery_inventory']):
        legacy_recovery._protected(lifecycle.store, ref)
    snapshot = evidence['snapshot']
    review = json.loads(legacy_recovery._protected(lifecycle.store, evidence['review']['report']))
    required_review = {'format': 'aos-legacy-current-safety-review-v1', 'work_id': work_id, 'attempt_id': attempt_id,
        'host': evidence['host'], 'snapshot_digest': digest(snapshot), 'author': evidence['review']['author'],
        'reviewer': evidence['review']['reviewer'], 'verdict': 'pass', 'historical_startup_outcome': 'unknown',
        'current_content_adoption': True, 'local_execution_provenance_verified': True}
    _require(all(review.get(k) == v for k, v in required_review.items()) and
             review['author'] != review['reviewer'] and
             json.loads(legacy_recovery._protected(lifecycle.store, evidence['adoption']['delivery_inventory'])) == snapshot['source_inventory'],
             'Historical independent retirement review differs')
    assignments = list(db.execute('SELECT * FROM assignment WHERE work_id=?', (work_id,)))
    _require(len(assignments) == 1 and assignments[0]['attempt_id'] == attempt_id and
             dict(assignments[0]) == {**snapshot['assignment'], 'state': 'cancelled', 'terminal_at': assignments[0]['terminal_at']} and
             assignments[0]['terminal_at'] and digest(contract) == snapshot['contract_digest'],
             'Historical retired assignment or contract changed')
    try:
        times = [datetime.fromisoformat(value) for value in
                 (assignments[0]['created_at'], assignments[0]['sent_at'] or assignments[0]['created_at'],
                  assignments[0]['terminal_at'], event['created_at'])]
    except (TypeError, ValueError) as error:
        raise Rejected('predispatch_feature_amendment', 'Historical retirement time is malformed') from error
    _require(all(value.tzinfo is not None for value in times) and
             max(times[:2]) <= times[2] <= times[3] <= datetime.now(timezone.utc),
             'Historical retirement times do not bound the cancelled assignment')
    _, _, accounting = legacy_recovery._runtime(lifecycle.store, attempt_id, snapshot['runtime_record_sha256'])
    original = snapshot['budget']
    _require({k: v for k, v in accounting.items() if k != 'reservations'} ==
             {k: v for k, v in original.items() if k != 'reservations'} and
             all(row in accounting['reservations'] for row in original['reservations']),
             'Historical charged allowance or original reservations changed')
    operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (detail['operation_id'],)).fetchone()
    _require(operation is not None, 'Historical retirement operation is missing')
    result = json.loads(operation[0])
    request = {'operation_id': detail['operation_id'], 'work_id': work_id,
        'expected_work_revision': result['work_revision'] - 1, 'digest': contract['concept_digest'],
        'attempt_id': attempt_id, 'runtime_record_sha256': snapshot['runtime_record_sha256'],
        'evidence_path': detail['evidence_path'], 'evidence_sha256': detail['evidence_sha256']}
    expected_result = {'work_id': work_id, 'work_revision': result['work_revision'], 'applied': True,
        'execution': 'retired', 'historical_startup_outcome': 'unknown', 'runtime_and_budget_modified': False,
        'new_work_admitted': False, 'evidence_sha256': detail['evidence_sha256']}
    work = lifecycle.store._work(db, work_id)
    _require(result == expected_result and lifecycle.store._prior(db, detail['operation_id'], ['recover-legacy-cancellation', request]) == result and
             work['revision'] == result['work_revision'] and work['current_concept'] == contract['concept_revision'],
             'Historical retirement operation fingerprint or current identity changed')
    return True


def _cancelled_foreign(lifecycle, db, assignment, attempt):
    work_id, aid = assignment['work_id'], assignment['attempt_id']
    run, _ = lifecycle._run(db, work_id)
    if (assignment['state'] != 'cancelled' or assignment['result'] is not None or
            (run['execution'], run['control'], run['admitted']) != ('cancelled', 'cancelled', 0)):
        return False
    stop = db.execute('SELECT * FROM owned_stop WHERE work_id=? AND attempt_id=?', (work_id, aid)).fetchone()
    event = db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='cancel_reconciled' ORDER BY event_id DESC LIMIT 1", (work_id,)).fetchone()
    receipt = attempt.get('stop_receipt')
    _require(stop and stop['state'] == 'stopped' and isinstance(receipt, dict) and
             stopped_with_effects(receipt) and receipt.get('attempt_id') == aid and
             json.loads(stop['observation']) == receipt and event and receipt in json.loads(event[0]) and
             not stop_conflicts_in(db, work_id), 'Saved foreign cancellation lacks matching positive cleanup')
    return True


def _passive(lifecycle, db, work_id):
    _require(not lifecycle.store.recovery_hold(), 'A recovered ledger cannot rebind execution')
    run, contract = lifecycle._run(db, work_id)
    _require(run['execution'] == 'held' and run['control'] == 'none' and not run['admitted'] and
             run['cycle'] == 0 and run['candidate'] is None and run['delivery'] is None,
             'Only untouched held pre-dispatch work can refresh settings')
    for sql, args in [
        ('SELECT 1 FROM assignment WHERE work_id=?', (work_id,)),
        ("SELECT 1 FROM assignment WHERE state NOT IN ('completed','failed','cancelled')", ()),
        ('SELECT 1 FROM lifecycle WHERE admitted=1', ()),
        ('SELECT 1 FROM resource', ()),
        ("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (work_id,)),
        ("SELECT 1 FROM worker_status WHERE state<>'stopped'", ()),
    ]:
        _require(not db.execute(sql, args).fetchone(), 'Existing assignment, admission or ownership forbids settings refresh')
    _require(all(not stop_conflicts_in(db, row['work_id']) for row in db.execute('SELECT work_id FROM lifecycle')),
             'Unresolved stop conflict forbids settings refresh')
    retired = set()
    for stop in db.execute("SELECT work_id,attempt_id FROM owned_stop WHERE work_id<>? AND state<>'stopped'", (work_id,)):
        _require(_retired_foreign(lifecycle, db, stop['work_id'], stop['attempt_id']), 'Foreign stop ownership remains unresolved')
        retired.add((stop['work_id'], stop['attempt_id']))
    tables = _tables(db, 'runtime')
    _require({'attempt', 'native_check', 'runtime_budget', 'product_budget_scope'} <= tables,
             'Original complete runtime journal is required')
    for row in db.execute('SELECT id,record FROM runtime.attempt'):
        attempt = json.loads(row['record'])
        _require(attempt.get('work_id', attempt.get('assignment', {}).get('work_id')) != work_id,
                 'A retained runtime attempt cannot become a fresh pre-dispatch feature')
        assignment = db.execute('SELECT * FROM assignment WHERE attempt_id=?', (row['id'],)).fetchone()
        _require(assignment and not stop_conflicts_in(db, assignment['work_id']), 'Foreign runtime attempt has no assignment or has contradictory cleanup')
        _require(attempt.get('work_id', attempt.get('assignment', {}).get('work_id')) == assignment['work_id'] and
                 attempt.get('attempt_id', row['id']) == row['id'], 'Foreign runtime identity differs from its assignment')
        key = (assignment['work_id'], row['id'])
        if key in retired or _retired_foreign(lifecycle, db, *key):
            continue
        if _cancelled_foreign(lifecycle, db, assignment, attempt):
            continue
        retained = json.loads(assignment['result'] or '{}')
        _require(assignment['state'] in ('completed', 'failed', 'cancelled') and stopped_with_effects(retained),
                 'Foreign runtime ownership lacks positive terminal cleanup')
    for row in db.execute('SELECT attempt_id,record FROM runtime.native_check'):
        native = json.loads(row['record'])
        owner = db.execute('SELECT record FROM runtime.attempt WHERE id=?', (row['attempt_id'],)).fetchone()
        _require(owner is not None and native.get('phase') == 'clean' and native.get('work_id') != work_id,
                 'Native evidence has product or unresolved ownership')
    if 'resource_limit' in tables:
        for row in db.execute('SELECT work_id,record FROM runtime.resource_limit'):
            guard = json.loads(row['record'])
            _require(row['work_id'] != work_id and not guard.get('active_attempt'), 'Existing product resource guard forbids refresh')
    if 'technical_qualification' in tables:
        for row in db.execute('SELECT record FROM runtime.technical_qualification'):
            value = json.loads(row[0])
            _require('active_phase' in value and value['active_phase'] is None and
                     all(p.get('state') == 'stopped' for p in value.get('phases', [])),
                     'Technical native ownership remains unresolved')
    if 'technical_phase_control' in tables:
        _require(not db.execute('SELECT 1 FROM runtime.technical_phase_control WHERE active=1').fetchone(),
                 'Technical native control remains active')
    return contract


def _budget_snapshot(db):
    return {table: [dict(r) for r in db.execute(f'SELECT * FROM runtime.{table} ORDER BY 1')]
            for table in ('runtime_budget', 'product_budget_scope')}


def _inputs(store, controls):
    entries = controls.get('input_files')
    _require(isinstance(entries, list) and 1 <= len(entries) <= 128 and
             len({r['path'] for r in entries}) == len(entries), 'Exact distinct pinned input list required')
    for entry in entries:
        _require(set(entry) == {'path', 'sha256'}, 'Unexpected input binding fields')
        _protected(store, entry['path'])
        _require(_file(entry['path'])[1] == entry, 'Pinned input changed')


def observe_accounting(store, controls, contract, decision):
    """Read the settled original clock and receipts; never initialize or advance it."""
    clock_path = _protected(store, Path(controls['control_root']) / 'clock.json')
    clock, clock_ref = _json(clock_path)
    approved_at = datetime.fromisoformat(decision['created_at']).timestamp()
    _require(set(clock) == {'identity', 'approved_at', 'deadline', 'active_limit', 'spent', 'last', 'open', 'held'} and
             clock['identity'] == contract['concept_digest'] and clock['approved_at'] == approved_at and
             clock['deadline'] == approved_at + controls['wall_seconds'] and
             clock['active_limit'] == controls['active_seconds'] and
             type(clock['spent']) is int and 0 <= clock['spent'] <= clock['active_limit'] <= 21600 and
             type(clock['last']) in (int, float) and math.isfinite(clock['last']) and
             approved_at <= clock['last'] <= time.time() < clock['deadline'] and
             controls['wall_seconds'] <= 172800 and clock['open'] is None and clock['held'] is False,
             'Original settled unexpired accounting is required')
    receipts, total = [], 0
    root = clock_path.parent
    starts = sorted(root.glob('operation-*.json'))
    _require(len(starts) <= 100 and len(list(root.glob('completed-*.json'))) == len(starts), 'Incomplete operation history')
    for path in starts:
        start, ref = _json(path)
        operation = start.get('operation')
        _require(set(start) == {'identity', 'operation', 'seconds', 'started_at'} and
                 operation in controls['coordinator_operations'] and path.name == 'operation-' + operation + '.json' and
                 start.get('identity') == clock['identity'] and type(start.get('seconds')) is int and start['seconds'] > 0,
                 'Original coordinator reservation differs')
        done, done_ref = _json(root / ('completed-' + operation + '.json'))
        times = (start['started_at'], done.get('completed_at'))
        _require(set(done) == {'start_digest', 'completed_at'} and done.get('start_digest') == digest(start) and
                 all(type(t) in (int, float) and math.isfinite(t) for t in times) and
                 approved_at <= times[0] <= times[1] <= min(times[0] + start['seconds'], clock['last']) and
                 times[0] + start['seconds'] <= clock['deadline'], 'Coordinator completion differs from its original bounded reservation')
        receipts.extend((ref, done_ref))
        total += start['seconds']
    _require(total == clock['spent'], 'Clock spending differs from retained reservation history')
    origin = {'work_id': controls['work_id'], 'state_dir': str(store.directory), 'decision_id': decision['decision_id'],
              'concept_digest': contract['concept_digest'], 'recipe_digest': contract['runtime_recipe_digest'],
              'approved_at': decision['created_at'], 'clock_path': str(clock_path), 'clock_identity': clock['identity'],
              'deadline': clock['deadline'], 'active_limit': clock['active_limit'], 'clock_sha256': clock_ref['sha256'],
              'operation_receipts': receipts, 'product_budget': copy.deepcopy(contract['product_budget'])}
    return origin, clock


def _manifest(old, new):
    _require(set(old) == set(new) and 'startup_project_registration' not in old, 'Instruction manifest structure cannot change')
    before, after = copy.deepcopy(old), copy.deepcopy(new)
    left, right = before.pop('configs'), after.pop('configs')
    _require(before == after and len(left) == len(right) > 0, 'Instruction discovery, policy or permission scope changed')
    for a, b in zip(left, right):
        _require(set(a) == set(b) and {k: v for k, v in a.items() if k not in ('sha256', 'bytes')} ==
                 {k: v for k, v in b.items() if k not in ('sha256', 'bytes')}, 'Configuration path or aliases changed')
    _require(left != right, 'No actual pinned configuration drift to refresh')


def _readiness_pair(approved_reference, journal_entry):
    """Bind two retained representations without replacing either raw identity."""
    approved, approved_ref = _json(approved_reference['path'])
    actual, actual_ref = _json(journal_entry['receipt'])
    _require(approved_ref == approved_reference and actual_ref == {
        'path': journal_entry['receipt'], 'sha256': journal_entry['sha256']},
        'Original readiness copy or journal receipt changed')
    _require(actual.get('id') == journal_entry['id'] and actual.get('status') == journal_entry['state'] and
             canonical(approved) == canonical(actual),
             'Approved readiness copy differs from its exact journal receipt')
    return approved


def _journal(directory, prior_reference):
    value, reference = _json(directory / 'control/readiness.json')
    _require(value.get('format') == 'aos-readiness-v1' and type(value.get('max_seconds')) is int and
             30 <= value['max_seconds'] <= 600 and type(value.get('max_attempts')) is int and
             1 <= value['max_attempts'] <= 3 and isinstance(value.get('runs'), list), 'Original bounded readiness journal required')
    for index, entry in enumerate(value['runs'], 1):
        expected_id = 'readiness-' + str(index)
        expected_path = directory / 'control' / (expected_id + '.json')
        _require(entry['id'] == expected_id and entry['state'] in ('passed', 'failed') and
                 entry['receipt'] == str(expected_path), 'Readiness history identity or path changed')
        receipt, ref = _json(expected_path)
        _require(ref['sha256'] == entry['sha256'] and receipt.get('id') == expected_id and
                 receipt.get('status') == entry['state'] and
                 receipt.get('resource_output') == str(directory / 'outputs' / expected_id) and
                 canonical({k: receipt.get('limits', {}).get(k) for k in ('max_attempts', 'max_seconds')}) ==
                 canonical({k: value[k] for k in ('max_attempts', 'max_seconds')}) and
                 receipt.get('cleanup_verified') is True, 'Readiness history or ownership changed')
        readiness._verify_cached_cleanup(receipt, directory / 'runtime')
    _require(value['runs'], 'Pinned prior readiness is not the journal tip')
    _readiness_pair(prior_reference, value['runs'][-1])
    return value, reference


def _ready_result(result, recipe, *, historical_engine_manifest=None):
    roles = result.get('roles', [])
    _require(len(roles) == 2 and {r.get('role') for r in roles} == {'implementation', 'assessment'} and
             all(r.get('passed') is True and r.get('configuration_verified') is True and r.get('model_turns') == 0 and
                 isinstance(r.get('thread_id'), str) and r['thread_id'] and r.get('cleanup', {}).get('verified') is True for r in roles),
             'Both actual metadata roles and their cleanup are required')
    _require(result.get('ready') is True and result.get('status') == 'passed' and result.get('simulated') is False and
             all(type(result.get(k)) is int and result[k] == 0 for k in ('model_turns', 'product_attempts', 'product_reservations')) and
             result.get('cleanup_verified') is True and result.get('readiness_scope') == 'model-metadata-only' and
             result.get('requested_native_checks') == [] and result.get('native', {}).get('checks') == [] and
             result.get('native', {}).get('execution') == 'not_requested' and
             result.get('prepared_recipe') == recipe and (result.get('runtime_identity') == readiness._runtime_identity() if historical_engine_manifest is None else result.get('runtime_identity', {}).get('runtime_source_digest') == digest(historical_engine_manifest)),
             'Actual bounded metadata readiness did not pass for this recipe')


def _baseline(recipe, controls):
    frozen = candidate(Path(recipe['source']), recipe=recipe)
    _require(frozen['inventory'] == {k: v['sha256'] for k, v in controls['baseline_inventory'].items()} and
             all((Path(recipe['source']) / name).stat().st_size == row['size'] and
                 ('100755' if (Path(recipe['source']) / name).stat().st_mode & 0o111 else '100644') == row['mode']
                 for name, row in controls['baseline_inventory'].items()), 'Pre-dispatch source differs from the reviewed baseline')
    return frozen


def _package(request):
    return {'format': 'aos-concept-v1', **{k: copy.deepcopy(v) for k, v in request.items()
            if k not in ('operation_id', 'expected_work_revision')}}


def _allowance(origin, clock):
    _require(origin['active_limit'] == 21600 and type(clock['spent']) is int and
             3300 <= clock['spent'] <= 5400, 'Selected amendment cannot erase charges or exceed remaining named plan capacity')
    return {'format': 'rnf-coordinator-allowance-amendment-v1', 'origin_limit_seconds': 21600,
        'effective_limit_seconds': 72000, 'added_seconds': 50400, 'charged_floor_seconds': clock['spent'],
        'planned_lifetime_reservation_seconds': 69900, 'unallocated_seconds': 2100,
        'reservation_plan_digest': digest(PLAN)}


def _product_budget(old):
    budget = copy.deepcopy(old['product_budget'])
    _require(budget['max_seconds'] == 144000, 'Original40h product allowance must be retained')
    return {**budget, 'max_assignments': 5, 'max_job_seconds': 28800}


def _empty_product(db, contract):
    budget = contract['product_budget']
    _require(not db.execute('SELECT 1 FROM runtime.runtime_budget WHERE id=?', (budget['budget_id'],)).fetchone(),
             'A materialized product allowance cannot be amended by this predispatch operation')
    for scope in ('product:' + budget['product_id'], 'source:' + contract['runtime_recipe']['source']):
        _require(not db.execute('SELECT 1 FROM runtime.product_budget_scope WHERE scope=?', (scope,)).fetchone(),
                 'An existing product/source allowance scope cannot be rebound')
    for row in db.execute('SELECT record FROM runtime.attempt'):
        _require(json.loads(row[0]).get('budget_id') != budget['budget_id'], 'Original product reservation already exists')


def _authority(old):
    # Preserve every original exclusion. Only the selected runtime/finite-limit
    # sentences change; the full generated text is included in the new decision.
    value = old.replace('task-owned Lima/Docker', 'task-owned Colima/Docker')
    value = value.replace('Ten charged assignments of at most 14400 seconds each; two corrections with independent triage/reassessment, two known-zero-work startup retries; no reset/refund/enlargement.',
        'Five charged assignments of at most28800 seconds each; one correction with independent triage/reassessment and zero startup retries; unchanged144000-second product total; no reset/refund or automatic enlargement.')
    value = value.replace('Checkpoint wait ceiling is 900 seconds within the unchanged assignment deadline.',
        'Checkpoint wait ceiling is4500 seconds for stage1-runtime and900 for all other product checkpoints, within the unchanged assignment deadline; each synthetic qualification wait remains900 seconds.')
    value = value.replace('Coordinator runtime operations have 21600 cumulative active seconds within 172800 seconds from saved approval, independent of the unchanged 144000-second AOS product reservation; no restart resets either limit.',
        'Coordinator runtime operations have a separately approved effective72000-second lifetime cap and exact69900-second named reservation plan. Original physical21600 clock cap, approval start,172800-second window and all charges remain unchanged; no restart resets accounting.')
    return value + (' The exact predispatch feature amendment authorizes only its frozen DI, controller, native handoff and purpose-aware assessment barriers. '
        'Coordinator evidence must precede any assessor claim; current genuine negative evidence may authorize independent failure review but never fabricated success or delivery. '
        'No product execution follows until the exact saved amendment, actual runtime/handoff proof, reconciliation and admission.')


def _recipe_delta(old, new, controls):
    allowed = {'instruction_manifest', 'max_seconds', 'implementation_prompt', 'assessment_prompt'}
    _require(set(new) == set(old) and all(new[k] == old[k] for k in set(old) - allowed) and
             type(new['max_seconds']) is int and new['max_seconds'] == 28800,
             'Recipe changed outside the exact selected prompts, configuration hashes and homogeneous ceiling')
    _manifest(old['instruction_manifest'], new['instruction_manifest'])
    for role in ('implementation_prompt', 'assessment_prompt'):
        _require(isinstance(new[role], str) and 1 <= len(new[role]) <= 131072 and
                 controls['approved_di']['path'] in new[role] and controls['approved_task_plan']['path'] in new[role],
                 'Both roles must read the exact pinned revised DI and task plan')


def _controller_delta(old, new, origin, clock):
    changed = {*CODE_FIELDS, 'input_files', 'active_seconds', 'coordinator_operations'}
    _require(set(new) == set(old) | NEW_CONTROL_FIELDS and
             all(new[k] == old[k] for k in set(old) - changed) and
             new['accounting_origin'] == origin and new['active_seconds'] == 72000 and
             new['coordinator_allowance_amendment'] == _allowance(origin, clock) and
             new['coordinator_reservation_plan'] == PLAN and
             new['checkpoint_wait_seconds'] == {'stage1-storage': 900, 'stage1-runtime': 4500, 'stage2-import': 900, 'repair': 900} and
             all(row in new['input_files'] for row in old['input_files']),
             'Controller changed outside the selected original-origin amendment')
    _require(isinstance(new['coordinator_operations'], list) and
             len(set(new['coordinator_operations'])) == len(new['coordinator_operations']) and
             set(new['coordinator_operations']) <= set(old['coordinator_operations']) | set(SLOTS),
             'Coordinator operation outside the selected named plan')
    for key in (*CODE_FIELDS, 'native_handoff_sha256'):
        _require(isinstance(new[key], str) and len(new[key]) == 64 and
                 any(row['sha256'] == new[key] for row in new['input_files']), 'Updated code is not pinned')
    for key in ('approved_di', 'approved_task_plan'):
        _require(new[key] in new['input_files'] and set(new[key]) == {'path', 'sha256'}, 'New approved input is not retained')
    native = new['native_handoff']
    _require(isinstance(native, dict) and set(native) == {'format', 'module_path', 'schema_path', 'task_root', 'prepared_root'} and
             native['format'] == 'rnf-native-handoff-controller-v1' and
             Path(native['module_path']).name == 'rnf_native_handoff.py' and
             {'path': native['module_path'], 'sha256': new['native_handoff_sha256']} in new['input_files'] and
             any(r['path'] == native['schema_path'] for r in new['input_files']) and
             Path(native['prepared_root']) == Path(native['task_root']) / 'private/metadata/native-prepared' and
             Path(new['assessment_evidence_gate']['native_handoff_root']) == Path(native['task_root']) / 'private/checkpoint-acks/native',
             'Exact approved native helper/schema and task-local custody paths are required')
    _require(new['approved_di'] != old.get('approved_di') and new['approved_task_plan'] != old.get('approved_task_plan'),
             'Exact revised DI and task plan are required')
    _require(isinstance(new['native_catalog_sha256'], str) and
             re.fullmatch(r'[0-9a-f]{64}', new['native_catalog_sha256']) and
             Path(new['native_catalog']) == Path(native['task_root']) / 'private/metadata/native-input-catalog-v2/catalog.json' and
             {'path': new['native_catalog'], 'sha256': new['native_catalog_sha256']} in new['input_files'],
             'Exact private structural catalog path and saved integrity pin are required')


def _concept(m, request):
    contract, package, old, new = m['original_contract'], m['original_package'], m['original_controller'], m['controller_contract']
    envelope = {k: copy.deepcopy(v) for k, v in contract.items() if k not in ('concept_revision', 'concept_digest')}
    prerequisites = dict(contract['prerequisites'])
    for key in ('native_runtime_ready', 'scoped_dispatch_controls_qualified', 'checkpoint_controls_qualified',
                'coordinator_resource_accounting_qualified', 'assessment_evidence_gate_qualified', 'native_handoff_qualified'):
        prerequisites[key] = False
    prerequisites['host_readiness'] = True
    envelope.update(revision=contract['revision'] + 1, runtime_recipe=m['recipe'], runtime_recipe_digest=digest(m['recipe']),
        resources=[('rnf-controller:' + digest(new)) if r == 'rnf-controller:' + digest(old)
                   else r.replace('owned-lima-native-lane', 'owned-colima-native-lane') for r in contract['resources']],
        max_seconds=28800, max_assignments=5, max_repairs=1, max_startup_retries=0,
        product_budget=_product_budget(contract), prerequisites=prerequisites, authority=_authority(contract['authority']),
        assessment_evidence_gate=new['assessment_evidence_gate'],
        predispatch_feature_amendment={'operation_id': m['operation_id'], 'metadata_digest': digest(m)})
    update = m['concept_update']
    _require(set(update) == {'body', 'decision_context', 'recommendation', 'alternative', 'consequence'},
             'Concept update may change only the exact reviewed body and decision brief')
    result = {k: copy.deepcopy(v) for k, v in package.items() if k not in ('format', 'execution_envelope')}
    result.update(update)
    result.update(operation_id=request['operation_id'] + '-present', expected_work_revision=request['expected_work_revision'],
        revision=package['revision'] + 1, execution_envelope=envelope, authority=envelope['authority'])
    return result


def prepare(lifecycle, request):
    """Retain a concrete packet and one existing-allowance metadata refresh only."""
    PrepareRequest.model_validate(request, strict=True)
    identifier(request['operation_id']); identifier(request['work_id'])
    store, work = lifecycle.store, request['work_id']
    destination = _protected(store, request['destination_directory'], directory=True)
    packet_path = destination / 'packet.json'
    if packet_path.exists():
        packet, reference = _json(packet_path, immutable=True)
        _require(packet['request'] == request, 'Preparation retry has different arguments')
        _validate_packet(store, packet)
        return {'packet_path': reference['path'], 'packet_sha256': reference['sha256'], 'packet': packet}
    clock_path = Path(_json(request['original_controller_contract_path'])[0]['control_root']) / 'clock.json'
    with fencing._worker_fence(store) as fence, _accounting_lock(store, clock_path) as clock_fence:
        with fencing._connection(store) as (db, _):
            lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
            contract = _passive(lifecycle, db, work)
            _empty_product(db, contract)
            if _schema(db):
                _require(not db.execute(f'SELECT 1 FROM {TABLE} WHERE work_id=?', (work,)).fetchone(),
                         'A feature may refresh settings only once before dispatch')
            package = json.loads(db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?',
                                (work, contract['concept_revision'])).fetchone()[0])
            _require(contract == _effective_contract(db, package, request['digest']), 'Original registered contract changed')
            original_budget = _budget_snapshot(db)
            event_ceiling = db.execute('SELECT coalesce(max(event_id),0) FROM lifecycle_event WHERE work_id=?', (work,)).fetchone()[0]
        decision = _approval(store, request['decision_id'], work, request['digest'])
        old, old_ref = _json(request['original_controller_contract_path'])
        new, template_ref = _json(request['controller_contract_path'])
        _require(old.get('format') == 'rnf-controller-contract-v1' and old['work_id'] == work and
                 old['state_dir'] == str(store.directory) and old['worker_roots'] == list(map(str, store.worker_writable_roots)) and
                 'rnf-controller:' + digest(old) in contract['resources'], 'Original approved controller is not bound')
        _inputs(store, old)
        origin, original_clock = observe_accounting(store, old, contract, decision)
        new.setdefault('accounting_origin', origin)
        new.setdefault('coordinator_allowance_amendment', _allowance(origin, original_clock))
        _controller_delta(old, new, origin, original_clock)
        _inputs(store, new)
        gate = assessment_evidence.configuration({'assessment_evidence_gate': new['assessment_evidence_gate']}, store)
        _require(gate['feature_deadline'] == origin['deadline'], 'Assessment gate cannot renew the original window')
        recipe, recipe_template_ref = _json(_protected(store, request['recipe_path']))
        concept_update, concept_update_ref = _json(_protected(store, request['concept_update_path']))
        recipe['instruction_manifest'] = instructions.discover(Path(recipe['source']),
            support_files=[r['path'] for r in recipe['instruction_manifest']['support']],
            project_doc_max_bytes=recipe['instruction_manifest'].get('runtime_overrides', {}).get('project_doc_max_bytes'))
        _recipe_delta(contract['runtime_recipe'], recipe, new)
        prior, prior_ref = _json(request['original_readiness_receipt_path'])
        _require(prior_ref in old['input_files'] and
                 canonical(prior.get('prepared_recipe')) == canonical(contract['runtime_recipe']) and
                 prior.get('status') == 'passed', 'Original approved readiness receipt differs')
        directory = _protected(store, request['readiness_directory'], directory=True)
        _require(Path(prior['resource_output']) == directory / 'outputs' / prior['id'], 'Readiness must use the original approved journal directory')
        journal, journal_ref = _journal(directory, prior_ref)
        _require(journal['state_dir'] == str(store.directory) and journal['source'] == recipe['source'] and
                 len(journal['runs']) < journal['max_attempts'] and
                 canonical({k: journal[k] for k in ('max_attempts', 'max_seconds')}) ==
                 canonical({k: prior.get('limits', {}).get(k) for k in ('max_attempts', 'max_seconds')}),
                 'Original readiness allowance is exhausted or changed')
        frozen = _baseline(recipe, old)
        result = readiness.run_readiness(recipe, state_dir=store.directory, readiness_dir=directory,
            model=contract['model'], effort=contract['effort'], max_seconds=journal['max_seconds'],
            max_attempts=journal['max_attempts'], native_checks=[])
        _ready_result(result, recipe)
        _require(all(result.get('limits', {}).get(k) == journal[k] for k in ('max_attempts', 'max_seconds')),
                 'New metadata receipt changed the original readiness limits')
        after, after_ref = _json(directory / 'control/readiness.json')
        _require({k: v for k, v in after.items() if k != 'runs'} == {k: v for k, v in journal.items() if k != 'runs'} and
                 after['runs'][:-1] == journal['runs'] and len(after['runs']) == len(journal['runs']) + 1,
                 'Readiness allowance/history was not preserved')
        actual_result, actual_ref = _json(after['runs'][-1]['receipt'])
        _require(actual_result == result and actual_ref['sha256'] == after['runs'][-1]['sha256'], 'New readiness evidence differs')
        prepared_ref = _retain(destination / 'prepared-recipe.json', recipe)
        receipt_ref = _retain(destination / 'readiness-receipt.json', result)
        for ref in (prepared_ref, receipt_ref):
            if ref not in new['input_files']:
                new['input_files'].append(ref)
        meta = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': work,
                'state_dir': str(store.directory), 'worker_roots': list(map(str, store.worker_writable_roots)),
                'original_contract': contract, 'original_controller': old, 'controller_contract': new,
                'original_prerequisite_event_ceiling': event_ceiling,
                'original_controller_reference': old_ref, 'controller_template_reference': template_ref,
                'recipe_template_reference': recipe_template_ref, 'concept_update_reference': concept_update_ref,
                'concept_update': concept_update,
                'original_decision_id': decision['decision_id'], 'original_package': package,
                'accounting_origin': origin, 'original_clock': original_clock, 'original_budget': original_budget,
                'source_candidate': frozen, 'recipe': recipe, 'engine_digest': result['runtime_identity']['runtime_source_digest'],
                'readiness_before': journal, 'readiness_after': after, 'readiness_journal': after_ref,
                'readiness_receipt': actual_ref, 'prepared_recipe': prepared_ref, 'prepared_receipt': receipt_ref}
        concept = _concept(meta, request)
        ConceptRequest.model_validate(concept, strict=True)
        packet = {'format': FORMAT, 'request': request, 'metadata': meta, 'concept': concept}
        _validate_packet(store, packet)
        with fencing._connection(store) as (db, _):
            lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
            _require(_passive(lifecycle, db, work) == contract and _budget_snapshot(db) == original_budget, 'Authority or accounting changed during preparation')
            _empty_product(db, contract)
        fence()
        clock_fence()
        reference = _retain(packet_path, packet)
        return {'packet_path': reference['path'], 'packet_sha256': reference['sha256'], 'packet': packet}


def _validate_packet(store, packet, *, live_accounting=True, historical_engine_manifest=None):
    _require(packet.get('format') == FORMAT, 'Unsupported settings refresh packet')
    m = packet['metadata']; old = m['original_contract']; origin = m['accounting_origin']
    _require(m['state_dir'] == str(store.directory) and m['worker_roots'] == list(map(str, store.worker_writable_roots)) and
             (readiness._runtime_identity()['runtime_source_digest'] if historical_engine_manifest is None else digest(historical_engine_manifest)) == m['engine_digest'], 'Ledger, roots or qualified engine changed')
    _require(packet['concept'] == _concept(m, packet['request']), 'Technical concept exceeds the exact allowed feature amendment')
    _recipe_delta(old['runtime_recipe'], m['recipe'], m['controller_contract'])
    expected_recipe = _json(m['recipe_template_reference']['path'])[0]
    expected_recipe['instruction_manifest'] = m['recipe']['instruction_manifest']
    _require(m['recipe'] == expected_recipe and (historical_engine_manifest is not None or
             instructions.readback(m['recipe']['instruction_manifest'], Path(m['recipe']['source']))['manifest'] == m['recipe']['instruction_manifest']), 'Recipe or current instructions changed')
    _require(_json(m['concept_update_reference']['path'])[0] == m['concept_update'], 'Exact revised concept body changed')
    for key in ('original_controller_reference', 'controller_template_reference', 'recipe_template_reference', 'concept_update_reference', 'readiness_journal', 'readiness_receipt', 'prepared_recipe', 'prepared_receipt'):
        _require(_file(m[key]['path'])[1] == m[key], 'Retained preparation input changed')
    _require(_json(m['original_controller_reference']['path'])[0] == m['original_controller'] and
             'rnf-controller:' + digest(m['original_controller']) in old['resources'], 'Original controller is not approved')
    _controller_delta(m['original_controller'], m['controller_contract'], origin, m['original_clock'])
    gate = assessment_evidence.configuration({'assessment_evidence_gate': m['controller_contract']['assessment_evidence_gate']}, store)
    _require(gate['feature_deadline'] == origin['deadline'], 'Assessment evidence window differs from original accounting')
    template = _json(m['controller_template_reference']['path'])[0]
    template.setdefault('accounting_origin', origin)
    template.setdefault('coordinator_allowance_amendment', _allowance(origin, m['original_clock']))
    for ref in (m['prepared_recipe'], m['prepared_receipt']):
        if ref not in template['input_files']:
            template['input_files'].append(ref)
    _require(template == m['controller_contract'], 'Approved controller differs from the supplied immutable template')
    before, after = m['readiness_before'], m['readiness_after']
    _require({k: v for k, v in after.items() if k != 'runs'} == {k: v for k, v in before.items() if k != 'runs'} and
             after['runs'][:-1] == before['runs'] and len(after['runs']) == len(before['runs']) + 1 and
             len(after['runs']) <= after['max_attempts'] <= 3 and after['max_seconds'] <= 600 and
             _json(m['readiness_journal']['path'])[0] == after, 'Original readiness allowance or append-only history differs')
    result = _json(m['readiness_receipt']['path'])[0]
    _require(_json(m['prepared_recipe']['path'])[0] == m['recipe'] and _json(m['prepared_receipt']['path'])[0] == result,
             'Retained readiness copies differ')
    _ready_result(result, m['recipe'], historical_engine_manifest=historical_engine_manifest)
    _require(all(result.get('limits', {}).get(k) == after[k] for k in ('max_attempts', 'max_seconds')),
             'Prepared metadata allowance changed')
    directory = Path(m['readiness_journal']['path']).parent.parent
    # Both raw references already belong to the immutable packet: the original
    # approved controller inputs and the unmodified pre-refresh journal. Re-read
    # them on prepare replay, apply and verify; never normalize historical bytes.
    originals = [ref for ref in m['original_controller']['input_files']
                 if ref['path'] == packet['request']['original_readiness_receipt_path']]
    _require(len(originals) == 1 and before['runs'], 'Original approved readiness reference is missing')
    original = _readiness_pair(originals[0], before['runs'][-1])
    _require(original.get('status') == 'passed' and
             canonical(original.get('prepared_recipe')) == canonical(old['runtime_recipe']) and
             Path(original['resource_output']) == directory / 'outputs' / original['id'] and
             Path(before['runs'][-1]['receipt']).parent == directory / 'control' and
             canonical({k: before[k] for k in ('max_attempts', 'max_seconds')}) ==
             canonical({k: original.get('limits', {}).get(k) for k in ('max_attempts', 'max_seconds')}),
             'Original approved readiness identity, directory or allowance changed')
    _require(Path(result['resource_output']) == directory / 'outputs' / result['id'] and
             after['runs'][-1]['receipt'] == m['readiness_receipt']['path'] and
             after['runs'][-1]['sha256'] == m['readiness_receipt']['sha256'], 'Readiness result moved outside its original journal')
    _journal(directory, m['readiness_receipt'])
    _inputs(store, m['controller_contract'])
    decision = _approval(store, m['original_decision_id'], m['work_id'], old['concept_digest'])
    _require(json.loads(decision['package_snapshot']) == m['original_package'], 'Original approved package differs')
    with store._connection() as db:
        _require(old == _effective_contract(db, m['original_package'], old['concept_digest'],
                 ceiling=m.get('original_prerequisite_event_ceiling')), 'Original contract differs from its exact approval/history')
    if live_accounting:
        actual_origin, actual_clock = observe_accounting(store, m['original_controller'], old, decision)
        _require(actual_origin == origin and actual_clock == m['original_clock'] and
                 _baseline(m['recipe'], m['original_controller']) == m['source_candidate'], 'Original clock or pre-dispatch source changed')
    for ref in origin['operation_receipts']:
        _require(_file(ref['path'])[1] == ref, 'Original operation receipt changed')
    _require(time.time() < origin['deadline'], 'Original feature window expired')


def apply(lifecycle, request):
    """Apply one exact approved packet atomically; never admit or dispatch it."""
    ApplyRequest.model_validate(request, strict=True)
    store = lifecycle.store
    packet, reference = _json(_protected(store, request['packet_path']), immutable=True)
    _require(reference['sha256'] == request['packet_sha256'], 'Preparation hash differs')
    m = packet['metadata']; work = request['work_id']; package = _package(packet['concept'])
    _require(work == m['work_id'] and request['operation_id'] == m['operation_id'] and request['digest'] == digest(package), 'Apply does not identify the prepared concept')
    _validate_packet(store, packet, live_accounting=False)
    decision = _approval(store, request['decision_id'], work, request['digest'])
    _require(datetime.fromisoformat(m['accounting_origin']['approved_at']).timestamp() <=
             datetime.fromisoformat(decision['created_at']).timestamp(), 'Technical approval predates its original accounting origin')
    payload = ['predispatch_feature_amendment', request]
    with store._connection() as db:
        if _schema(db):
            prior = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (request['operation_id'],)).fetchone()
            if prior:
                receipt = json.loads(prior[0])
                _require(receipt['apply_request'] == request, 'Applied refresh retry differs')
                return verify(store, {'operation_id': request['operation_id'], 'work_id': work, 'receipt_digest': digest(receipt)})
    with fencing._worker_fence(store) as fence, _accounting_lock(store, m['accounting_origin']['clock_path']) as clock_fence:
        with fencing._connection(store, write=True) as (db, _):
            db.execute('BEGIN IMMEDIATE')
            if _schema(db):
                prior = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (request['operation_id'],)).fetchone()
                if prior:
                    raise Rejected('predispatch_feature_amendment', 'Another caller applied this refresh; read its original receipt')
            lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
            _require(_passive(lifecycle, db, work) == m['original_contract'] and _budget_snapshot(db) == m['original_budget'], 'Original contract or budget changed')
            _empty_product(db, m['original_contract'])
            current = db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?', (work, package['revision'])).fetchone()
            _require(current and json.loads(current[0]) == package and decision['concept_revision'] == package['revision'], 'Saved current technical concept differs')
            _validate_packet(store, packet)
            targets = ('lifecycle', 'work', 'lifecycle_event', 'operation')
            _require(not db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN (" + ','.join('?' for _ in targets) + ')', targets).fetchone(), 'Unsupported mutation triggers')
            expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': request['digest']}
            _schema(db, create=True)
            db.execute("UPDATE lifecycle SET contract=? WHERE work_id=? AND contract=? AND admitted=0 AND control='none'", (canonical(expected), work, canonical(m['original_contract'])))
            _require(db.execute('SELECT changes()').fetchone()[0] == 1, 'Registered contract compare-and-swap failed')
            lifecycle._touch(db, work, 'predispatch_feature_amended', {'operation_id': request['operation_id'], 'packet_digest': digest(packet)})
            event_floor = db.execute('SELECT max(event_id) FROM lifecycle_event WHERE work_id=?', (work,)).fetchone()[0]
            receipt = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': work,
                'state_dir': str(store.directory), 'apply_request': request, 'packet': reference, 'packet_digest': digest(packet),
                'from_decision_id': m['original_decision_id'], 'from_concept_digest': m['original_contract']['concept_digest'],
                'from_recipe_digest': m['original_contract']['runtime_recipe_digest'], 'to_decision_id': decision['decision_id'],
                'to_concept_digest': request['digest'], 'to_recipe_digest': expected['runtime_recipe_digest'],
                'engine_digest': m['engine_digest'], 'accounting_origin': m['accounting_origin'], 'original_clock': m['original_clock'],
                'coordinator_allowance_amendment': m['controller_contract']['coordinator_allowance_amendment'],
                'amended_product_budget': expected['product_budget'],
                'registered_contract': expected, 'prerequisite_event_floor': event_floor, 'created_at': now()}
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)', (request['operation_id'], work, canonical(receipt)))
            store._record(db, request['operation_id'], payload, {'applied': True, 'receipt_digest': digest(receipt), 'work_id': work})
            fence()
            clock_fence()
            db.commit()
    return receipt


def verify(store, reference):
    """Read-only verification after normal dispatch and coordinator clock progress."""
    _require(set(reference) == {'operation_id', 'work_id', 'receipt_digest'}, 'Exact immutable rebind reference required')
    with store._connection() as db:
        _require(_schema(db), 'Applied settings refresh is missing')
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=? AND work_id=?', (reference['operation_id'], reference['work_id'])).fetchone()
        _require(row is not None, 'Applied settings refresh is missing')
        receipt = json.loads(row[0])
        operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (reference['operation_id'],)).fetchone()
        _require(digest(receipt) == reference['receipt_digest'] and operation and
                 json.loads(operation[0]) == {'applied': True, 'receipt_digest': digest(receipt), 'work_id': reference['work_id']}, 'Rebind audit differs from its operation receipt')
        work = store._work(db, reference['work_id'])
        package_row = db.execute('SELECT package,digest FROM concept WHERE work_id=? AND revision=?', (reference['work_id'], work['current_concept'])).fetchone()
        _require(package_row and package_row['digest'] == receipt['to_concept_digest'], 'A different concept superseded this technical decision')
        package = json.loads(package_row['package'])
        contract = json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (reference['work_id'],)).fetchone()[0])
        _require(contract == _effective_contract(db, package, receipt['to_concept_digest'], floor=receipt['prerequisite_event_floor']), 'Registered contract differs beyond supported later prerequisite observations')
    packet, packet_ref = _json(receipt['packet']['path'], immutable=True)
    _require(packet_ref == receipt['packet'] and digest(packet) == receipt['packet_digest'], 'Applied immutable preparation changed')
    _validate_packet(store, packet, live_accounting=False)
    _approval(store, receipt['to_decision_id'], receipt['work_id'], receipt['to_concept_digest'])
    current_accounting(store, receipt)
    return receipt


def current_accounting(store, receipt, *, settled=False, at=None):
    """Read the original physical clock and reconcile every exact later charge.

    Authority verification permits one valid open named reservation. Release
    publication explicitly requests settled=True; neither path writes a clock.
    """
    origin, before = receipt['accounting_origin'], receipt['original_clock']
    at = time.time() if at is None else at
    _require(type(at) in (int, float) and math.isfinite(at), 'Invalid accounting observation time')
    with _accounting_lock(store, origin['clock_path'], shared=True):
        current, clock_ref = _json(origin['clock_path'])
        _require(set(current) == set(before) and
                 all(current.get(k) == before[k] for k in ('identity', 'approved_at', 'deadline', 'active_limit')) and
                 current['active_limit'] == 21600 and receipt['coordinator_allowance_amendment'] == _allowance(origin, before) and
                 type(current['spent']) is int and before['spent'] <= current['spent'] <= 72000 and
                 type(current['last']) in (int, float) and math.isfinite(current['last']) and
                 before['last'] <= current['last'] <= at < current['deadline'] and current['held'] is False,
                 'Original accounting identity, window or monotonic spending changed')
        root = Path(origin['clock_path']).parent
        _require(not any(root.glob('clock.json.next*')), 'Uncertain partial clock publication')
        paths = sorted(p for p in root.iterdir() if p.name.startswith(('operation-', 'completed-')))
        _require(len(paths) <= 256 and all(p.suffix == '.json' for p in paths), 'Partial or excessive operation history')
        rows, refs = {}, {}
        original = {Path(r['path']).name: r for r in origin['operation_receipts']}
        for path in paths:
            _protected(store, path)
            rows[path.name], refs[path.name] = _json(path, immutable=path.name not in original)
        original = {Path(r['path']).name: r for r in origin['operation_receipts']}
        _require(all(refs.get(k) == v for k, v in original.items()), 'Original operation history changed')
        total, slots, opened, paired, timeline, operations = 0, set(), [], set(), [], {}
        for name, start in rows.items():
            if not name.startswith('operation-'):
                continue
            operation = start.get('operation')
            _require(isinstance(operation, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', operation) and
                     name == 'operation-' + operation + '.json' and start.get('identity') == origin['clock_identity'] and
                     type(start.get('seconds')) is int and start['seconds'] > 0 and
                     type(start.get('started_at')) in (int, float) and math.isfinite(start['started_at']),
                     'Invalid operation identity, charge or time')
            if name in original:
                _require(set(start) == {'identity', 'operation', 'seconds', 'started_at'}, 'Original reservation shape changed')
            else:
                _require(set(start) == {'identity', 'operation', 'seconds', 'started_at', 'slot', 'context', 'authority_receipt_digest'} and
                         start['authority_receipt_digest'] == digest(receipt), 'New charge lacks exact amendment authority')
                _require(datetime.fromisoformat(receipt['created_at']).timestamp() <= start['started_at'],
                         'New coordinator operation predates applied amendment authority')
                slot, context = start['slot'], start['context']
                _require(slot in SLOTS and slot not in slots and start['seconds'] <= SLOTS[slot], 'Named slot repeated or exceeded')
                slots.add(slot)
                base = {'work_id': receipt['work_id'], 'concept_digest': receipt['to_concept_digest'],
                        'recipe_digest': receipt['to_recipe_digest'], 'slot': slot}
                _require(isinstance(context, dict) and all(context.get(k) == v for k, v in base.items()), 'Foreign operation context')
                if slot in ('stage1-storage', 'stage1-runtime', 'stage2-import', 'repair') or slot.startswith('qualification-'):
                    fields = set(base) | {'kind', 'assignment_id', 'attempt_id', 'request_digest', 'ordinal', 'increment',
                                          'requested_at', 'assignment_deadline', 'feature_deadline'}
                    _require(set(context) == fields and isinstance(context['request_digest'], str) and
                             re.fullmatch(r'sha256:[0-9a-f]{64}', context['request_digest']) and type(context['ordinal']) is int and
                             all(type(context[k]) in (int, float) and math.isfinite(context[k]) for k in
                                 ('requested_at', 'assignment_deadline', 'feature_deadline')) and
                             context['feature_deadline'] == origin['deadline'] and
                             current['approved_at'] <= context['requested_at'] <= start['started_at'] <
                             min(context['assignment_deadline'], context['feature_deadline']), 'Invalid bound checkpoint context')
                    with store._connection() as db:
                        assignment = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND attempt_id=? AND work_id=?',
                            (context['assignment_id'], context['attempt_id'], receipt['work_id'])).fetchone()
                    _require(assignment and assignment['kind'] in ('implementation', 'repair') and assignment['sent_at'] and
                             assignment['concept_digest'] == receipt['to_concept_digest'] and
                             datetime.fromisoformat(assignment['sent_at']).timestamp() + 28800 == context['assignment_deadline'],
                             'Checkpoint operation lacks its actual original writer assignment')
                    if slot.startswith('qualification-'):
                        _require(context['kind'] == 'qualification' and context['increment'] == 'qualification' and
                                 context['ordinal'] == int(slot[-1]) and operation == 'first-writer-' + slot,
                                 'Synthetic operation differs from its slot')
                    else:
                        _require(context['kind'] == 'checkpoint' and context['increment'] == slot and
                                 context['ordinal'] == ['stage1-storage', 'stage1-runtime', 'stage2-import', 'repair'].index(slot) + 1 and
                                 operation == f"checkpoint-{context['ordinal']:04d}", 'Checkpoint operation differs from its slot')
                else:
                    _require(context == {**base, 'kind': 'coordinator'} and operation == slot, 'Coordinator slot context differs')
            done_name = 'completed-' + operation + '.json'
            done = rows.get(done_name)
            if done is None:
                opened.append(start)
                end = current['last']
            else:
                _require(set(done) == {'start_digest', 'completed_at'} and done['start_digest'] == digest(start) and
                         type(done['completed_at']) in (int, float) and math.isfinite(done['completed_at']), 'Invalid completion receipt')
                end = done['completed_at']; paired.add(done_name)
            _require(current['approved_at'] <= start['started_at'] <= end <= current['last'] and
                     end <= start['started_at'] + start['seconds'] <= current['deadline'], 'Operation time exceeds its original reservation')
            if name not in original and start['context']['kind'] != 'coordinator':
                context = start['context']
                _require(end < min(context['requested_at'] + SLOTS[start['slot']], context['assignment_deadline'], origin['deadline']),
                         'Checkpoint exceeded its original request window')
            timeline.append((start['started_at'], end)); total += start['seconds']
            operations[operation] = {'start': refs[name], 'completion': refs.get(done_name), 'reservation': start, 'result': done}
        _require(paired == {k for k in rows if k.startswith('completed-')} and total == current['spent'], 'Charges do not reconcile all retained receipts')
        _require(len(opened) <= 1 and ((not opened and current['open'] is None) or len(opened) == 1 and
                 current['open'] == {'id': opened[0]['operation'], 'reserved': opened[0]['seconds'], 'started_at': opened[0]['started_at']} and
                 at <= opened[0]['started_at'] + opened[0]['seconds']), 'Uncertain or overdue current operation')
        ordered = sorted(timeline)
        _require(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), 'Overlapping coordinator operations')
        _require(not settled or current['open'] is None, 'Assessment release requires settled coordinator operations')
        return {'clock': current, 'clock_reference': clock_ref, 'operations': operations, 'receipt_digest': digest(refs)}
