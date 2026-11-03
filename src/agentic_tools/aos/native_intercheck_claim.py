"""One durable, non-resumable passive observation claim per technical phase.

Builders are pure and encode outside the caller's writer transaction. Durable
operations neither commit nor inspect processes, and never resume a scan.
"""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
import time

from .store import Rejected, canonical, identifier

FORMAT = 'aos-native-intercheck-claim-v1'
TABLE = 'native_intercheck_claim'
MAX_BYTES = 1024 * 1024
FIELDS = {'format', 'qualification_id', 'phase_id', 'operation_id', 'fingerprint',
          'authority_digest', 'proof_digest', 'owner', 'state', 'revision',
          'created_at', 'updated_at', 'outcome'}
IMMUTABLE = FIELDS - {'state', 'revision', 'updated_at', 'outcome'}
STATES = {'reserved', 'scan_started', 'success', 'failed'}
NEXT = {'reserved': {'scan_started', 'failed'}, 'scan_started': {'success', 'failed'},
        'success': set(), 'failed': set()}
OUTCOME = {'acknowledgment', 'scope_digest', 'baseline_digest', 'high_water_growth_bytes',
           'fresh_observation_digest', 'error', 'disposition_created'}
SCHEMA = {
    TABLE: "CREATE TABLE native_intercheck_claim (qualification_id TEXT NOT NULL, phase_id TEXT NOT NULL, operation_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','scan_started','success','failed')), revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0), record TEXT NOT NULL, PRIMARY KEY(qualification_id,phase_id), UNIQUE(qualification_id,operation_id))",
}
_immutable_sql = ' OR '.join(
    f"json_extract(NEW.record,'$.{key}') IS NOT json_extract(OLD.record,'$.{key}')" for key in sorted(IMMUTABLE))
_column_sql = " OR ".join(
    f"json_extract(NEW.record,'$.{key}') IS NOT NEW.{key}"
    for key in ('qualification_id', 'phase_id', 'operation_id', 'state', 'revision'))
SCHEMA.update({
    'native_intercheck_claim_insert': f"""CREATE TRIGGER native_intercheck_claim_insert BEFORE INSERT ON native_intercheck_claim BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM native_intercheck_claim WHERE qualification_id=NEW.qualification_id
            AND (phase_id=NEW.phase_id OR operation_id=NEW.operation_id))
            THEN RAISE(ABORT,'Intercheck claims cannot be replaced') END;
        SELECT CASE WHEN NOT json_valid(NEW.record) THEN RAISE(ABORT,'Malformed intercheck claim') END;
        SELECT CASE WHEN NEW.state!='reserved' OR NEW.revision!=0 OR {_column_sql}
            THEN RAISE(ABORT,'Intercheck claim must start reserved') END;
    END""",
    'native_intercheck_claim_update': f"""CREATE TRIGGER native_intercheck_claim_update BEFORE UPDATE ON native_intercheck_claim BEGIN
        SELECT CASE WHEN NOT json_valid(NEW.record) THEN RAISE(ABORT,'Malformed intercheck claim') END;
        SELECT CASE WHEN NEW.qualification_id IS NOT OLD.qualification_id OR NEW.phase_id IS NOT OLD.phase_id
            OR NEW.operation_id IS NOT OLD.operation_id OR NEW.revision!=OLD.revision+1
            OR NOT ((OLD.state='reserved' AND NEW.state IN ('scan_started','failed'))
                    OR (OLD.state='scan_started' AND NEW.state IN ('success','failed')))
            OR {_column_sql} OR {_immutable_sql}
            THEN RAISE(ABORT,'Invalid intercheck claim transition') END;
    END""",
    'native_intercheck_claim_delete': """CREATE TRIGGER native_intercheck_claim_delete BEFORE DELETE ON native_intercheck_claim BEGIN
        SELECT RAISE(ABORT,'Intercheck claims cannot be removed');
    END""",
})


def _reject(message):
    raise Rejected('technical_intercheck_claim', message)


def verify(db, *, allow_absent=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'native_intercheck_claim*' "
                            "OR (tbl_name='native_intercheck_claim' AND "
                            "(type='trigger' OR (type='index' AND sql IS NOT NULL)))").fetchall())
    if not found and allow_absent:
        return False
    if found != SCHEMA:
        _reject('The exact intercheck claim schema is missing or changed')
    return True


def initialize(db):
    if not db.in_transaction:
        _reject('Claim schema initialization requires a caller transaction')
    if verify(db, allow_absent=True):
        return
    for sql in SCHEMA.values():
        db.execute(sql)


def _identifier(value):
    if not isinstance(value, str):
        _reject('Claim identities must be bounded strings')
    return identifier(value)


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _outcome(value, state):
    if state in ('reserved', 'scan_started'):
        if value is not None:
            _reject('An unfinished claim cannot contain a terminal outcome')
        return
    if not isinstance(value, dict) or set(value) != OUTCOME:
        _reject('A terminal claim requires its exact compact outcome')
    if not all(_digest(value[key]) for key in ('scope_digest', 'baseline_digest')):
        _reject('Terminal scope and original baseline digests are required')
    growth = value['high_water_growth_bytes']
    if (not isinstance(growth, dict) or set(growth) != {'output_roots', 'source_evidence_roots'}
            or any(type(number) is not int or number < 0 for number in growth.values())):
        _reject('Terminal high-water floors must be nonnegative integer bytes')
    fresh = value['fresh_observation_digest']
    if fresh is not None and not _digest(fresh):
        _reject('The fresh observation must have an exact digest')
    acknowledgment = value['acknowledgment']
    if (not isinstance(acknowledgment, dict) or acknowledgment.get('accepted') is not False
            or acknowledgment.get('grants_work') is not False or acknowledgment.get('grants_time') is not False
            or type(acknowledgment.get('model_budget_reservations')) is not int
            or acknowledgment['model_budget_reservations'] != 0):
        _reject('A claim outcome cannot grant work, time or model reservations')
    error = value['error']
    if state == 'success':
        if value['disposition_created'] is not True or error is not None or fresh is None:
            _reject('Success requires a fresh observation and its atomic disposition')
        _identifier(acknowledgment.get('continuation_resource_id'))
    elif (value['disposition_created'] is not False or not isinstance(error, dict)
          or set(error) != {'code', 'message'} or not isinstance(error['code'], str)
          or not 1 <= len(error['code']) <= 64 or not isinstance(error['message'], str)
          or not 1 <= len(error['message']) <= 2000):
        _reject('A failed claim must retain a bounded error and grant no disposition')


def _validate(record):
    if not isinstance(record, dict) or set(record) != FIELDS or record['format'] != FORMAT:
        _reject('Malformed intercheck claim record')
    for key in ('qualification_id', 'phase_id', 'operation_id'):
        _identifier(record[key])
    if not all(_digest(record[key]) for key in ('fingerprint', 'authority_digest', 'proof_digest')):
        _reject('Exact operation, authority and proof digests are required')
    owner = record['owner']
    if (not isinstance(owner, dict) or set(owner) != {'pid', 'uid', 'birth'}
            or type(owner['pid']) is not int or owner['pid'] <= 0
            or type(owner['uid']) is not int or owner['uid'] < 0
            or not isinstance(owner['birth'], list) or len(owner['birth']) != 2
            or any(type(number) is not int for number in owner['birth'])
            or owner['birth'][0] <= 0 or not 0 <= owner['birth'][1] < 1_000_000):
        _reject('The claim requires an exact PID, UID and microsecond birth identity')
    if (not isinstance(record['state'], str) or record['state'] not in STATES
            or type(record['revision']) is not int or record['revision'] < 0
            or not _timestamp(record['created_at']) or not _timestamp(record['updated_at'])
            or record['updated_at'] < record['created_at']):
        _reject('Malformed claim state, revision or timestamps')
    _outcome(record['outcome'], record['state'])
    if record['outcome'] is not None and any(
            record['outcome']['acknowledgment'].get(key) != record[key]
            for key in ('qualification_id', 'phase_id', 'operation_id')):
        _reject('The terminal acknowledgment must name its exact immutable claim')


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _reject('Duplicate fields in a claim record')
        result[key] = value
    return result


def _decode(raw):
    if not isinstance(raw, str) or len(raw) > MAX_BYTES or len(raw.encode()) > MAX_BYTES:
        _reject('A compact claim exceeds its encoded bound')
    try:
        record = json.loads(raw, object_pairs_hook=_pairs,
                            parse_constant=lambda value: _reject('Non-finite claim data'))
        _validate(record)
        return record
    except Rejected:
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise Rejected('technical_intercheck_claim', 'Malformed encoded claim') from error


def _prepared(value):
    if not isinstance(value, dict) or set(value) != FIELDS | {'_raw'}:
        _reject('An exact pre-encoded claim snapshot is required')
    record = _decode(value['_raw'])
    supplied = {key: item for key, item in value.items() if key != '_raw'}
    _validate(supplied)
    if record != supplied:
        _reject('Claim fields differ from the exact encoded snapshot')
    return record


def _encode(record):
    _validate(record)
    raw = canonical(record)
    if len(raw.encode()) > MAX_BYTES:
        _reject('A compact claim exceeds its encoded bound')
    return {**copy.deepcopy(record), '_raw': raw}


def prepare(qualification_id, phase_id, operation_id, *, fingerprint, authority_digest, proof_digest, owner):
    created = time.time()
    return _encode({'format': FORMAT, 'qualification_id': qualification_id, 'phase_id': phase_id,
                    'operation_id': operation_id, 'fingerprint': fingerprint,
                    'authority_digest': authority_digest, 'proof_digest': proof_digest,
                    'owner': copy.deepcopy(owner), 'state': 'reserved', 'revision': 0,
                    'created_at': created, 'updated_at': created, 'outcome': None})


def next_state(expected, state, *, outcome=None):
    record = _prepared(expected)
    if state not in NEXT[record['state']]:
        _reject('The claim cannot be reset, resumed or transitioned again')
    return _encode({**record, 'state': state, 'revision': record['revision'] + 1,
                    'updated_at': time.time(), 'outcome': copy.deepcopy(outcome)})


def _row(row):
    record = _decode(row[5])
    if tuple(record[key] for key in ('qualification_id', 'phase_id', 'operation_id', 'state', 'revision')) != row[:5]:
        _reject('Claim index fields differ from the retained record')
    return {**record, '_raw': row[5]}


def get(db, qualification_id, *, phase_id=None, operation_id=None):
    _identifier(qualification_id)
    if (phase_id is None) == (operation_id is None):
        _reject('Select exactly one phase or operation identity')
    value, field = (phase_id, 'phase_id') if phase_id is not None else (operation_id, 'operation_id')
    _identifier(value)
    if not verify(db, allow_absent=True):
        return None
    row = db.execute('SELECT qualification_id,phase_id,operation_id,state,revision,record FROM ' + TABLE +
                     ' WHERE qualification_id=? AND ' + field + '=?', (qualification_id, value)).fetchone()
    return _row(row) if row else None


def _all(db, qualification_id):
    _identifier(qualification_id)
    if not verify(db, allow_absent=True):
        return []
    rows = db.execute('SELECT qualification_id,phase_id,operation_id,state,revision,record FROM ' + TABLE +
                      ' WHERE qualification_id=? ORDER BY phase_id', (qualification_id,)).fetchall()
    return [_row(row) for row in rows]


def pending(db, qualification_id):
    # Validate every row before filtering. A malformed terminal label cannot
    # conceal an unfinished or failed audit.
    return [claim for claim in _all(db, qualification_id) if claim['state'] != 'success']


def require_idle(db, qualification_id, *, allowed=None):
    if allowed is not None:
        _prepared(allowed)
        if allowed['qualification_id'] != qualification_id or allowed['state'] not in ('reserved', 'scan_started'):
            _reject('Only the exact unfinished caller claim can pass its own fence')
    held = pending(db, qualification_id)
    if any(claim != allowed for claim in held) or allowed is not None and held != [allowed]:
        _reject('An unfinished or failed intercheck claim must remain held')


def reserve(db, prepared):
    record = _prepared(prepared)
    verify(db)
    if not db.in_transaction:
        _reject('Claim reservation requires a caller transaction')
    if record['state'] != 'reserved' or record['revision'] != 0:
        _reject('Only a fresh reservation can be inserted')
    if (get(db, record['qualification_id'], phase_id=record['phase_id']) is not None
            or get(db, record['qualification_id'], operation_id=record['operation_id']) is not None):
        _reject('A retained phase or operation claim cannot be reserved again')
    require_idle(db, record['qualification_id'])
    try:
        db.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?,?)',
                   tuple(record[key] for key in ('qualification_id', 'phase_id', 'operation_id', 'state', 'revision'))
                   + (prepared['_raw'],))
    except sqlite3.IntegrityError as error:
        raise Rejected('technical_intercheck_claim', 'Another exact phase or operation already owns the claim') from error
    return copy.deepcopy(prepared)


def transition(db, expected, prepared):
    before, after = _prepared(expected), _prepared(prepared)
    verify(db)
    if not db.in_transaction:
        _reject('Claim transition requires a caller transaction')
    if (any(before[key] != after[key] for key in IMMUTABLE)
            or after['state'] not in NEXT[before['state']] or after['revision'] != before['revision'] + 1
            or after['updated_at'] < before['updated_at']):
        _reject('The exact claim identity or lifecycle changed before transition')
    changed = db.execute('UPDATE ' + TABLE + ' SET state=?,revision=?,record=? WHERE qualification_id=? '
                         'AND phase_id=? AND operation_id=? AND state=? AND revision=? AND record=?',
                         (after['state'], after['revision'], prepared['_raw'], before['qualification_id'],
                          before['phase_id'], before['operation_id'], before['state'], before['revision'], expected['_raw'])).rowcount
    if changed != 1:
        _reject('The exact retained claim changed before transition')
    return copy.deepcopy(prepared)


def check_dispositions(db, qualification_id, record):
    claims = _all(db, qualification_id)
    if record is None:
        if claims:
            _reject('The archive is missing while intercheck claims remain')
        return
    items = record.get('intercheck_observation_dispositions', [])
    if not isinstance(items, list):
        _reject('Malformed intercheck disposition archive')
    success = {claim['operation_id']: claim for claim in claims if claim['state'] == 'success'}
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('operation_id') not in success or item['operation_id'] in seen:
            _reject('An intercheck disposition has no unique successful claim')
        claim = success[item['operation_id']]
        ack = claim['outcome']['acknowledgment']
        if (item.get('phase_id') != claim['phase_id'] or item.get('fingerprint') != claim['fingerprint']
                or item.get('acknowledgment') != ack
                or item.get('resource_id') != ack['continuation_resource_id']
                or 'historical_breach' not in ack or 'historical_breach' not in item
                or item['historical_breach'] != ack['historical_breach']
                or 'historical_breaches' in item):
            _reject('The intercheck disposition differs from its immutable successful claim')
        receipt = item.get('resource_receipt')
        growth = receipt.get('high_water_growth_bytes') if isinstance(receipt, dict) else None
        floors = claim['outcome']['high_water_growth_bytes']
        if (not isinstance(receipt, dict) or receipt.get('scope_digest') != claim['outcome']['scope_digest']
                or not isinstance(growth, dict) or set(growth) != set(floors)
                or any(type(growth[key]) is not int or growth[key] < floor for key, floor in floors.items())):
            _reject('The current continuation cannot change scope or lower its retained byte floors')
        seen.add(item['operation_id'])
    if seen != set(success):
        _reject('The archive omitted a successful intercheck claim')
