"""Durable claim unit tests use only disposable SQLite journals; no scans run."""
import copy
import json
import os
import sqlite3

import pytest

from agentic_tools.aos import native_intercheck_claim as claims
from agentic_tools.aos.store import Rejected, canonical


def sha(character='a'):
    return 'sha256:' + character * 64


def prepared(*, qid='qualification', phase='phase', operation='operation'):
    return claims.prepare(qid, phase, operation, fingerprint=sha('a'), authority_digest=sha('b'),
                          proof_digest=sha('c'), owner={'pid': 1234, 'uid': os.getuid(), 'birth': [123, 456]})


def outcome(*, success=True):
    return {'acknowledgment': {'kind': 'technical-intercheck-observation-failure-disposition',
                              'qualification_id': 'qualification', 'phase_id': 'phase', 'operation_id': 'operation',
                              'accepted': False, 'grants_work': False, 'grants_time': False,
                              'model_budget_reservations': 0,
                              'historical_breach': {'reason': 'observation_failed', 'observed_at': 123.0},
                              'continuation_resource_id': 'continuation'},
            'scope_digest': sha('d'), 'baseline_digest': sha('e'),
            'high_water_growth_bytes': {'output_roots': 17, 'source_evidence_roots': 23},
            'fresh_observation_digest': sha('f') if success else None,
            'error': None if success else {'code': 'resource_observation', 'message': 'Exact failure retained'},
            'disposition_created': success}


@pytest.fixture
def db(tmp_path):
    connection = sqlite3.connect(tmp_path / 'claims.sqlite3')
    connection.execute('BEGIN')
    claims.initialize(connection)
    connection.commit()
    yield connection
    connection.close()


def reserve(db, claim=None):
    value = claim or prepared()
    db.execute('BEGIN IMMEDIATE')
    saved = claims.reserve(db, value)
    db.commit()
    return saved


def move(db, claim, state, *, result=None):
    next_value = claims.next_state(claim, state, outcome=result)
    db.execute('BEGIN IMMEDIATE')
    saved = claims.transition(db, claim, next_value)
    db.commit()
    return saved


def successful(db):
    return move(db, move(db, reserve(db), 'scan_started'), 'success', result=outcome())


def disposition(claim):
    return {'operation_id': claim['operation_id'], 'phase_id': claim['phase_id'],
            'fingerprint': claim['fingerprint'], 'resource_id': 'continuation',
            'historical_breach': copy.deepcopy(claim['outcome']['acknowledgment']['historical_breach']),
            'acknowledgment': copy.deepcopy(claim['outcome']['acknowledgment']),
            'resource_receipt': {'scope_digest': claim['outcome']['scope_digest'],
                                 'high_water_growth_bytes': {'output_roots': 17, 'source_evidence_roots': 23}}}


def test_lifecycle_has_one_scan_and_immutable_terminal_replay(db):
    initial = reserve(db)
    assert claims.pending(db, 'qualification') == [initial]
    scan = move(db, initial, 'scan_started')
    assert scan['revision'] == 1 and scan['outcome'] is None
    completed = move(db, scan, 'success', result=outcome())
    assert completed['revision'] == 2 and claims.pending(db, 'qualification') == []
    assert claims.get(db, 'qualification', operation_id='operation') == completed
    assert claims.get(db, 'qualification', phase_id='phase') == completed
    with pytest.raises(Rejected, match='cannot be reset'):
        claims.next_state(completed, 'scan_started')
    claims.require_idle(db, 'qualification')


@pytest.mark.parametrize('start_scan', [False, True])
def test_failed_claim_is_a_permanent_fence_even_after_reopen(db, start_scan):
    claim = reserve(db)
    if start_scan:
        claim = move(db, claim, 'scan_started')
    failed = move(db, claim, 'failed', result=outcome(success=False))
    path = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as reopened:
        assert claims.get(reopened, 'qualification', operation_id='operation') == failed
        with pytest.raises(Rejected, match='remain held'):
            claims.require_idle(reopened, 'qualification')
        with pytest.raises(Rejected, match='unfinished caller'):
            claims.require_idle(reopened, 'qualification', allowed=failed)
        with pytest.raises(Rejected, match='cannot be reset'):
            claims.next_state(failed, 'reserved')


def test_crash_after_scan_started_cannot_resume_or_reserve_again(db):
    started = move(db, reserve(db), 'scan_started')
    with pytest.raises(Rejected, match='cannot be reset'):
        claims.next_state(started, 'scan_started')
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(Rejected, match='cannot be reserved again'):
        claims.reserve(db, prepared())
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == started


@pytest.mark.parametrize('other', [dict(phase='phase', operation='different'),
                                 dict(phase='different', operation='operation')])
def test_phase_and_operation_each_have_one_owner(db, other):
    before = reserve(db)
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(Rejected, match='cannot be reserved again'):
        claims.reserve(db, prepared(**other))
    db.rollback()
    assert claims.pending(db, 'qualification') == [before]


def test_distinct_qualification_names_are_independent(db):
    first = reserve(db)
    second = reserve(db, prepared(qid='another'))
    claims.require_idle(db, 'qualification', allowed=first)
    claims.require_idle(db, 'another', allowed=second)


def test_reservation_cannot_create_a_second_pending_claim(db):
    first = reserve(db)
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(Rejected, match='remain held'):
        claims.reserve(db, prepared(phase='another', operation='another'))
    db.rollback()
    claims.require_idle(db, 'qualification', allowed=first)
    assert claims.pending(db, 'qualification') == [first]


def test_durable_calls_neither_begin_nor_commit_the_callers_transaction(db):
    value = prepared()
    with pytest.raises(Rejected, match='caller transaction'):
        claims.reserve(db, value)
    db.execute('BEGIN')
    claims.reserve(db, value)
    assert db.in_transaction
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') is None
    value = reserve(db, value)
    nxt = claims.next_state(value, 'scan_started')
    with pytest.raises(Rejected, match='caller transaction'):
        claims.transition(db, value, nxt)
    db.execute('BEGIN')
    claims.transition(db, value, nxt)
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == value


def test_success_and_archive_acknowledgment_can_commit_atomically(db):
    started = move(db, reserve(db), 'scan_started')
    terminal = claims.next_state(started, 'success', outcome=outcome())
    db.execute('CREATE TABLE synthetic_archive (record TEXT NOT NULL)')
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO synthetic_archive VALUES(?)', (canonical(disposition(terminal)),))
    claims.transition(db, started, terminal)
    db.rollback()
    assert db.execute('SELECT COUNT(*) FROM synthetic_archive').fetchone()[0] == 0
    assert claims.get(db, 'qualification', phase_id='phase') == started
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO synthetic_archive VALUES(?)', (canonical(disposition(terminal)),))
    claims.transition(db, started, terminal)
    db.commit()
    claims.check_dispositions(db, 'qualification', {'intercheck_observation_dispositions': [disposition(terminal)]})


def test_stale_snapshot_cannot_finalize_a_newer_claim(db):
    initial = reserve(db)
    stale_failure = claims.next_state(initial, 'failed', outcome=outcome(success=False))
    scan = move(db, initial, 'scan_started')
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(Rejected, match='retained claim changed'):
        claims.transition(db, initial, stale_failure)
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == scan


@pytest.mark.parametrize('field,value', [('owner', {'pid': 9, 'uid': 9, 'birth': [9, 9]}),
                                        ('phase_id', 'another'), ('fingerprint', sha('1'))])
def test_mutating_snapshot_fields_without_raw_bytes_is_rejected(db, field, value):
    original = reserve(db)
    forged = copy.deepcopy(original)
    forged[field] = value
    with pytest.raises(Rejected, match='encoded snapshot'):
        claims.next_state(forged, 'failed', outcome=outcome(success=False))
    assert claims.get(db, 'qualification', phase_id='phase') == original


def test_even_consistently_reencoded_next_state_cannot_change_owner(db):
    initial = reserve(db)
    forged = claims.next_state(initial, 'scan_started')
    forged['owner']['pid'] += 1
    forged['_raw'] = canonical({key: value for key, value in forged.items() if key != '_raw'})
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(Rejected, match='identity or lifecycle changed'):
        claims.transition(db, initial, forged)
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == initial


@pytest.mark.parametrize('other', [{}, {'operation': 'new'}, {'phase': 'new'}])
def test_sqlite_replace_cannot_reset_a_failed_claim(db, other):
    failed = move(db, reserve(db), 'failed', result=outcome(success=False))
    fresh = prepared(**other)
    with pytest.raises(sqlite3.IntegrityError, match='cannot be replaced'):
        db.execute('INSERT OR REPLACE INTO native_intercheck_claim VALUES(?,?,?,?,?,?)',
                   tuple(fresh[key] for key in ('qualification_id', 'phase_id', 'operation_id', 'state', 'revision'))
                   + (fresh['_raw'],))
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == failed


def test_direct_sql_cannot_delete_or_reset_the_scan_state(db):
    scan = move(db, reserve(db), 'scan_started')
    with pytest.raises(sqlite3.IntegrityError, match='cannot be removed'):
        db.execute('DELETE FROM native_intercheck_claim')
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match='Invalid intercheck claim transition'):
        db.execute("UPDATE native_intercheck_claim SET state='reserved',revision=2")
    db.rollback()
    assert claims.get(db, 'qualification', phase_id='phase') == scan


def test_durable_calls_do_not_encode_payloads_inside_writer(db, monkeypatch):
    initial = prepared()
    next_value = claims.next_state(initial, 'scan_started')

    def forbidden(*args):
        raise AssertionError('Payload encoding must precede the caller writer transaction')

    monkeypatch.setattr(claims, 'canonical', forbidden)
    reserve(db, initial)
    db.execute('BEGIN IMMEDIATE')
    claims.transition(db, initial, next_value)
    db.commit()
    assert claims.get(db, 'qualification', phase_id='phase') == next_value


@pytest.mark.parametrize('damage', ['missing-trigger', 'extra-trigger', 'extra-index', 'different-table'])
def test_schema_change_is_not_repaired_or_silently_accepted(db, damage):
    reserve(db)
    if damage == 'missing-trigger':
        db.execute('DROP TRIGGER native_intercheck_claim_update')
    elif damage == 'extra-trigger':
        db.execute('CREATE TRIGGER foreign_claim_writer AFTER UPDATE ON native_intercheck_claim BEGIN SELECT 1; END')
    elif damage == 'extra-index':
        db.execute('CREATE INDEX foreign_claim_index ON native_intercheck_claim(state)')
    else:
        db.execute('ALTER TABLE native_intercheck_claim ADD COLUMN unexpected TEXT')
    db.execute('BEGIN')
    with pytest.raises(Rejected, match='schema'):
        claims.initialize(db)
    db.rollback()
    with pytest.raises(Rejected, match='schema'):
        claims.pending(db, 'qualification')


def test_malformed_rows_cannot_disappear_from_the_pending_fence(db):
    value = prepared()
    raw = json.loads(value['_raw'])
    del raw['proof_digest']
    db.execute('INSERT INTO native_intercheck_claim VALUES(?,?,?,?,?,?)',
               ('qualification', 'phase', 'operation', 'reserved', 0, canonical(raw)))
    db.commit()
    with pytest.raises(Rejected, match='Malformed'):
        claims.require_idle(db, 'qualification')


def test_duplicate_json_fields_are_not_accepted_as_an_exact_snapshot(db):
    value = prepared()
    value['_raw'] = value['_raw'][:-1] + ',"state":"reserved"}'
    db.execute('BEGIN')
    with pytest.raises(Rejected, match='Duplicate'):
        claims.reserve(db, value)
    db.rollback()


@pytest.mark.parametrize('field,value', [('accepted', True), ('grants_work', True), ('grants_time', True),
                                        ('model_budget_reservations', 1), ('model_budget_reservations', False)])
def test_outcome_cannot_create_work_time_or_model_authority(db, field, value):
    started = move(db, reserve(db), 'scan_started')
    result = outcome()
    result['acknowledgment'][field] = value
    with pytest.raises(Rejected, match='cannot grant'):
        claims.next_state(started, 'success', outcome=result)


@pytest.mark.parametrize('damage', ['missing-observation', 'error-on-success', 'no-disposition', 'negative-floor'])
def test_success_requires_a_complete_nonnegative_observation_outcome(db, damage):
    started = move(db, reserve(db), 'scan_started')
    result = outcome()
    if damage == 'missing-observation':
        result['fresh_observation_digest'] = None
    elif damage == 'error-on-success':
        result['error'] = {'code': 'failed', 'message': 'failed'}
    elif damage == 'no-disposition':
        result['disposition_created'] = False
    else:
        result['high_water_growth_bytes']['output_roots'] = -1
    with pytest.raises(Rejected):
        claims.next_state(started, 'success', outcome=result)


def test_large_acknowledgment_is_rejected_before_reservation_mutation(db):
    initial = reserve(db)
    result = outcome(success=False)
    result['acknowledgment']['unbounded'] = 'x' * claims.MAX_BYTES
    with pytest.raises(Rejected, match='encoded bound'):
        claims.next_state(initial, 'failed', outcome=result)
    assert claims.get(db, 'qualification', phase_id='phase') == initial


@pytest.mark.parametrize('damage', ['phase', 'operation', 'fingerprint', 'resource', 'acknowledgment', 'duplicate', 'omission'])
def test_successful_claim_and_immutable_archive_audit_must_agree(db, damage):
    completed = successful(db)
    item = disposition(completed)
    items = [item]
    if damage == 'phase':
        item['phase_id'] = 'another'
    elif damage == 'operation':
        item['operation_id'] = 'another'
    elif damage == 'fingerprint':
        item['fingerprint'] = sha('1')
    elif damage == 'resource':
        item['resource_id'] = 'another'
    elif damage == 'acknowledgment':
        item['acknowledgment']['reason'] = 'unreviewed replacement'
    elif damage == 'duplicate':
        items.append(copy.deepcopy(item))
    else:
        items = []
    with pytest.raises(Rejected, match='disposition|omitted'):
        claims.check_dispositions(db, 'qualification', {'intercheck_observation_dispositions': items})


def test_passive_continuation_growth_does_not_rewrite_the_original_success_audit(db):
    completed = successful(db)
    item = disposition(completed)
    item['resource_receipt']['high_water_growth_bytes']['output_roots'] += 1000
    claims.check_dispositions(db, 'qualification', {'intercheck_observation_dispositions': [item]})
    assert claims.get(db, 'qualification', operation_id='operation') == completed


@pytest.mark.parametrize('damage', ['different-breach', 'plural-breaches'])
def test_successful_disposition_cannot_acknowledge_an_unrelated_failure(db, damage):
    completed = successful(db)
    item = disposition(completed)
    unrelated = {'reason': 'unreviewed_other_failure', 'observed_at': 124.0}
    if damage == 'different-breach':
        item['historical_breach'] = unrelated
    else:
        item['historical_breaches'] = [item['historical_breach'], unrelated]
    with pytest.raises(Rejected, match='immutable successful claim'):
        claims.check_dispositions(db, 'qualification', {'intercheck_observation_dispositions': [item]})


@pytest.mark.parametrize('field', ['qualification_id', 'phase_id', 'operation_id'])
def test_terminal_acknowledgment_cannot_name_a_different_claim(db, field):
    initial = reserve(db)
    result = outcome(success=False)
    result['acknowledgment'][field] = 'different'
    with pytest.raises(Rejected, match='exact immutable claim'):
        claims.next_state(initial, 'failed', outcome=result)


@pytest.mark.parametrize('damage', ['scope', 'output-floor', 'source-floor', 'noninteger'])
def test_current_continuation_cannot_lower_immutable_claim_floors(db, damage):
    completed = successful(db)
    item = disposition(completed)
    if damage == 'scope':
        item['resource_receipt']['scope_digest'] = sha('1')
    else:
        key = 'source_evidence_roots' if damage == 'source-floor' else 'output_roots'
        item['resource_receipt']['high_water_growth_bytes'][key] = False if damage == 'noninteger' else 0
    with pytest.raises(Rejected, match='scope or lower'):
        claims.check_dispositions(db, 'qualification', {'intercheck_observation_dispositions': [item]})


def test_missing_archive_or_claim_cannot_hide_successful_disposition(db):
    completed = successful(db)
    with pytest.raises(Rejected, match='archive is missing'):
        claims.check_dispositions(db, 'qualification', None)
    with sqlite3.connect(':memory:') as absent:
        with pytest.raises(Rejected, match='no unique successful claim'):
            claims.check_dispositions(absent, 'qualification', {'intercheck_observation_dispositions': [disposition(completed)]})


def test_old_archives_do_not_require_new_claim_schema():
    with sqlite3.connect(':memory:') as empty:
        claims.require_idle(empty, 'old')
        claims.check_dispositions(empty, 'old', {'quiescent_gap_dispositions': [{'legacy': True}]})
        assert claims.get(empty, 'old', phase_id='old-phase') is None
        assert not empty.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'native_intercheck_claim%'").fetchone()
