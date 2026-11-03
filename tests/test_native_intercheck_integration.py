"""Intercheck claim/accounting integration with disposable stopped journals.

Forensic eligibility and process identity are controlled inputs here; their
independent validator tests cover real evidence. No command, permission probe,
supervisor, model call or original qualification journal is used by this file.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from agentic_tools.aos import native_intercheck_claim as claims
from agentic_tools.aos import native_intercheck_recovery as recovery
from agentic_tools.aos import native_qualification as qualification
from agentic_tools.aos import resource_limits as limits, technical_control
from agentic_tools.aos.store import Rejected, canonical, digest
from test_native_qualification_independent import technical, synthetic_private_receipts


def archive(g):
    with g.q.runtime._db() as db:
        return qualification._read(db, g.q.qualification_id)


def raw_archive(g):
    with g.q.runtime._db() as db:
        return db.execute('SELECT record FROM technical_qualification WHERE id=?',
                          (g.q.qualification_id,)).fetchone()[0]


def audit(g):
    with g.q.runtime._db() as db:
        return claims.get(db, g.q.qualification_id, operation_id='review-intercheck')


def invoke(g, **changes):
    arguments = {'operation_id': 'review-intercheck', 'phase_id': 'interrupted',
                 'evidence_path': g.q.directory / 'synthetic-forensics.json',
                 'evidence_sha256': 'a' * 64, 'reason': 'Synthetic intercheck accounting review'}
    arguments.update(changes)
    return g.q.acknowledge_intercheck_observation_failure(**arguments)


@pytest.fixture
def stopped(technical, monkeypatch):
    g = technical
    g.q.begin('interrupted', g.recipe, max_seconds=30)
    assert g.q.reconcile('interrupted')['stopped'] is True
    breach = {'reason': 'observation_failed', 'error': 'Rejected',
              'error_code': 'resource_observation', 'error_message': 'Synthetic bounded scan failure',
              'observed_at': 1.0}
    with g.q.runtime._db() as db:
        db.execute('BEGIN IMMEDIATE')
        record = qualification._read(db, g.q.qualification_id)
        phase = record['phases'][0]
        guard = limits._read(db, phase['resource_id'])
        guard['breach'] = copy.deepcopy(breach)
        guard['high_water_growth_bytes']['source_evidence_roots'] = 3
        limits._save(db, phase['resource_id'], guard)
        phase['resource_receipt'] = limits.receipt(guard)
        phase['receipt']['resource_limits'] = copy.deepcopy(phase['resource_receipt'])
        record['breach'] = copy.deepcopy(breach)
        record['charged_growth_bytes']['source_evidence_roots'] = 3
        qualification._save(db, g.q.qualification_id, record)
        technical_control.save(db, g.q.qualification_id, phase)
    g.original = copy.deepcopy(record)
    g.original_guard = copy.deepcopy(guard)
    g.resource_id = phase['resource_id']
    g.calls = {'evidence': 0, 'validate': 0, 'recheck': 0, 'sidecars': 0}

    def evidence(*args):
        g.calls['evidence'] += 1
        return {'synthetic_unit_input': True}

    def validate(*args, **kwargs):
        g.calls['validate'] += 1
        return {'synthetic_unit_proof': True,
                'minimum_high_water_growth_bytes': copy.deepcopy(guard['high_water_growth_bytes'])}

    def recheck(proof):
        g.calls['recheck'] += 1

    def sidecars(proof):
        g.calls['sidecars'] += 1

    monkeypatch.setattr(recovery, 'evidence', evidence)
    monkeypatch.setattr(recovery, 'validate', validate)
    monkeypatch.setattr(recovery, 'recheck', recheck)
    monkeypatch.setattr(recovery, 'recheck_sidecars', sidecars)
    monkeypatch.setattr(qualification.native_processes, 'identity',
                        lambda pid: {'pid': pid, 'uid': os.getuid(), 'birth': [1234, 5678]})
    monkeypatch.setattr(g.q.runtime, '_check', lambda *args: pytest.fail('No native check is authorized'))
    return g


def assert_historical_guard_unchanged(g):
    with g.q.runtime._db() as db:
        assert limits._read(db, g.resource_id) == g.original_guard
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
    assert g.q.runtime.model_calls == 0


def continuation_rows(g):
    with g.q.runtime._db() as db:
        return dict(db.execute("SELECT work_id,record FROM resource_limit WHERE work_id LIKE 'technical-continuation-%'"))


def test_success_keeps_original_guard_and_charges_real_fresh_growth(stopped):
    g = stopped
    (g.outputs / 'fresh-growth').write_bytes(b'x' * 17)
    before = raw_archive(g)
    result = invoke(g)
    assert result['status'] == 'acknowledged' and result['disposition_created'] is True
    assert result['accepted'] is result['grants_work'] is result['grants_time'] is False
    assert result['model_budget_reservations'] == 0
    assert result['resource_limits']['high_water_growth_bytes']['source_evidence_roots'] == 17
    retained = audit(g)
    assert retained['state'] == 'success' and retained['revision'] == 2
    assert retained['outcome']['fresh_observation_digest']
    after = archive(g)
    assert raw_archive(g) != before and after['phases'] == g.original['phases']
    assert after['breach'] == g.original['breach']
    assert after['charged_growth_bytes']['source_evidence_roots'] == 17
    continuation = json.loads(continuation_rows(g)[result['continuation_resource_id']])
    assert continuation['baseline'] == g.original_guard['baseline']
    assert continuation['scope'] == g.original_guard['scope']
    assert continuation['active_attempt'] is continuation['breach'] is None
    assert_historical_guard_unchanged(g)


@pytest.mark.parametrize('failure', ['scan-error', 'cap-overrun', 'min-free'])
def test_fresh_scan_failure_retains_outcome_and_never_rescans(stopped, monkeypatch, failure):
    g = stopped
    before = raw_archive(g)
    observe, sample = limits.observe, limits._sample_record
    sampled_breaches = []

    def capture_failure(*args, **kwargs):
        value, changed = sample(*args, **kwargs)
        sampled_breaches.append(copy.deepcopy(value['breach']))
        return value, changed

    monkeypatch.setattr(limits, '_sample_record', capture_failure)
    if failure == 'scan-error':
        def unavailable(*args):
            raise Rejected('resource_observation', 'Synthetic observation unavailable')
        monkeypatch.setattr(limits, 'observe', unavailable)
    elif failure == 'cap-overrun':
        (g.outputs / 'overrun').write_bytes(b'x' * 101)
    else:
        def insufficient_free(bound):
            value = observe(bound)
            value['available_bytes'] = {key: 0 for key in value['available_bytes']}
            return value
        monkeypatch.setattr(limits, 'observe', insufficient_free)
    result = invoke(g)
    assert result['status'] == 'failed' and result['disposition_created'] is False
    retained = audit(g)
    assert retained['state'] == 'failed' and retained['outcome']['error']
    assert retained['outcome']['high_water_growth_bytes']['source_evidence_roots'] >= 3
    assert len(sampled_breaches) == 1 and sampled_breaches[0]
    assert result['observed_resource_breach'] == sampled_breaches[0]
    assert retained['outcome']['acknowledgment']['observed_resource_breach'] == sampled_breaches[0]
    if failure == 'cap-overrun':
        assert retained['outcome']['high_water_growth_bytes']['source_evidence_roots'] == 101
        assert retained['outcome']['fresh_observation_digest']
        assert result['observed_resource_breach']['reason'] == 'max_source_evidence_growth_bytes'
        assert result['observed_resource_breach']['observed_bytes'] == 101
        assert result['observed_resource_breach']['limit_bytes'] == 100
    elif failure == 'scan-error':
        assert retained['outcome']['fresh_observation_digest'] is None
        assert result['observed_resource_breach']['reason'] == 'observation_failed'
        assert result['observed_resource_breach']['error_code'] == 'resource_observation'
        assert result['observed_resource_breach']['error_message'] == 'Synthetic observation unavailable'
    else:
        assert retained['outcome']['fresh_observation_digest']
        assert result['observed_resource_breach']['reason'] == 'min_free_bytes'
        assert result['observed_resource_breach']['observed_bytes'] == 0
        assert result['observed_resource_breach']['limit_bytes'] == 1
        assert result['observed_resource_breach']['available_bytes']
        assert set(result['observed_resource_breach']['available_bytes'].values()) == {0}
    assert raw_archive(g) == before and not continuation_rows(g)
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('Terminal replay must not scan'))
    monkeypatch.setattr(recovery, 'evidence', lambda *args: pytest.fail('Terminal replay is historical'))
    assert invoke(g) == result and audit(g) == retained
    assert_historical_guard_unchanged(g)


def test_success_replay_does_not_reopen_changed_artifacts_or_scan(stopped, monkeypatch):
    g = stopped
    result = invoke(g)
    before = raw_archive(g)
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('Successful replay cannot scan'))
    monkeypatch.setattr(recovery, 'evidence', lambda *args: pytest.fail('Historical replay cannot reread evidence'))
    assert invoke(g) == result and raw_archive(g) == before
    with pytest.raises(Rejected, match='exact reviewed evidence'):
        invoke(g, reason='Changed review')


@pytest.mark.parametrize('state', ['reserved', 'scan_started', 'failed'])
@pytest.mark.parametrize('operation', ['new-ack', 'old-gap', 'old-prelaunch', 'old-rejected', 'begin', 'export',
                                      'reconcile', 'refresh'])
def test_pending_or_failed_claim_fences_other_cold_operations(stopped, monkeypatch, state, operation):
    g = stopped
    original_transition = claims.transition

    def persist_only_start(db, expected, prepared):
        if prepared['state'] == 'failed' or state == 'reserved':
            raise sqlite3.OperationalError('Synthetic persistence interruption')
        return original_transition(db, expected, prepared)

    def unavailable(*args):
        raise Rejected('resource_observation', 'Synthetic observation unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(limits, 'observe', unavailable)
        if state != 'failed':
            patch.setattr(claims, 'transition', persist_only_start)
            with pytest.raises(Rejected, match='durable observation claim remains held'):
                invoke(g)
        else:
            assert invoke(g)['status'] == 'failed'
    assert audit(g)['state'] == state
    monkeypatch.setattr(qualification, '_read', lambda *args: pytest.fail('The cold fence must precede archive reads'))
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('A held claim cannot scan again'))
    options = dict(evidence_path=g.q.directory / 'not-read', evidence_sha256='a' * 64, reason='Held fence')
    with pytest.raises(Rejected, match='remain held'):
        if operation == 'new-ack':
            invoke(g, operation_id='another-review')
        elif operation.startswith('old-'):
            method = {'old-gap': g.q.acknowledge_quiescent_gap,
                      'old-prelaunch': g.q.acknowledge_prelaunch_observation_failure,
                      'old-rejected': g.q.acknowledge_rejected_admission_observation}[operation]
            method('another-review', 'interrupted', **options)
        elif operation == 'begin':
            g.q.begin('next', g.recipe, max_seconds=30)
        elif operation == 'reconcile':
            g.q.reconcile('interrupted')
        elif operation == 'refresh':
            with g.q.runtime._db() as db:
                g.q._refresh(db, copy.deepcopy(g.original))
        else:
            g.q.export(g.recipe, g.q.directory / 'must-not-exist.json', work_id='feature')
    assert not (g.q.directory / 'must-not-exist.json').exists()


@pytest.mark.parametrize('state', ['reserved', 'scan_started'])
def test_held_same_operation_returns_exact_state_without_resuming(stopped, monkeypatch, state):
    g = stopped
    original_transition = claims.transition

    def interrupt(db, expected, prepared):
        if state == 'reserved' or prepared['state'] == 'failed':
            raise sqlite3.OperationalError('Synthetic interrupted persistence')
        return original_transition(db, expected, prepared)

    with monkeypatch.context() as patch:
        patch.setattr(claims, 'transition', interrupt)
        patch.setattr(limits, 'observe', lambda *args: (_ for _ in ()).throw(Rejected('observe', 'Synthetic failure')))
        with pytest.raises(Rejected, match='durable observation claim remains held'):
            invoke(g)
    retained = audit(g)
    assert retained['state'] == state
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('Held replay cannot scan'))
    monkeypatch.setattr(recovery, 'evidence', lambda *args: pytest.fail('Held replay cannot revalidate to resume'))
    result = invoke(g)
    assert result['status'] == 'held' and result['claim_state'] == state
    assert audit(g) == retained and not continuation_rows(g)


def change_authority(g, kind):
    with g.q.runtime._db() as db:
        if kind in ('archive', 'archive-aba'):
            saved = qualification._read(db, g.q.qualification_id)
            updated = {**saved, 'external_change': True}
            qualification._save(db, g.q.qualification_id, updated)
            if kind == 'archive-aba':
                qualification._save(db, g.q.qualification_id, saved)
        elif kind == 'native':
            row = {'check_id': 'external-check', 'phase': 'clean',
                   'cleanup': {'stopped': True, 'effects_reconciled': True}}
            db.execute('INSERT INTO native_check VALUES(?,?,?)', ('external-check', None, canonical(row)))
        elif kind == 'guard':
            guard = limits._read(db, g.resource_id)
            guard['high_water_growth_bytes']['source_evidence_roots'] += 1
            limits._save(db, g.resource_id, guard)
        elif kind == 'resource-inventory':
            limits._save(db, 'external-guard', copy.deepcopy(g.original_guard))
        else:
            raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['archive', 'archive-aba', 'native', 'guard', 'resource-inventory', 'sidecar', 'owner'])
def test_stale_authority_after_observation_keeps_failure_and_observed_floor(stopped, monkeypatch, kind):
    g = stopped
    observe = limits.observe
    scans = []

    def observed(bound):
        result = observe(bound)
        scans.append(result)
        if kind == 'sidecar':
            resource_id = 'technical-continuation-' + digest([g.q.qualification_id, 'review-intercheck'])[7:31]
            limits._failure_path(str(g.q.runtime.path), resource_id).write_text('synthetic new latch')
        elif kind == 'owner':
            monkeypatch.setattr(qualification.native_processes, 'identity', lambda pid: None)
        else:
            change_authority(g, kind)
        return result

    (g.outputs / 'fresh-growth').write_bytes(b'x' * 19)
    monkeypatch.setattr(limits, 'observe', observed)
    result = invoke(g)
    assert len(scans) == 1 and result['status'] == 'failed'
    retained = audit(g)
    assert retained['state'] == 'failed'
    assert retained['outcome']['high_water_growth_bytes']['source_evidence_roots'] == 19
    assert retained['outcome']['fresh_observation_digest'] == digest(scans[0])
    assert not archive(g).get('intercheck_observation_dispositions') and not continuation_rows(g)


def test_observation_and_large_payload_encoding_run_outside_writer(stopped, monkeypatch):
    g = stopped
    observe = limits.observe
    encode = qualification.canonical
    calls = {'observe': 0, 'encode': 0}

    def writer_is_free():
        with sqlite3.connect(g.q.runtime.path, timeout=0) as other:
            other.execute('BEGIN IMMEDIATE')
            other.rollback()

    def observed(bound):
        writer_is_free()
        with g.q.runtime._db() as db:
            assert claims.get(db, g.q.qualification_id, operation_id='review-intercheck')['state'] == 'scan_started'
        calls['observe'] += 1
        return observe(bound)

    def encoded(value):
        writer_is_free()
        calls['encode'] += 1
        return encode(value)

    monkeypatch.setattr(limits, 'observe', observed)
    monkeypatch.setattr(qualification, 'canonical', encoded)
    monkeypatch.setattr(claims, 'canonical', encoded)
    assert invoke(g)['status'] == 'acknowledged'
    assert calls['observe'] == 1 and calls['encode'] >= 5


def test_completed_observation_cannot_be_published_after_original_freshness_bound(stopped, monkeypatch):
    g = stopped
    observed = limits.observe
    snapshots = []

    def stale(bound):
        value = observed(bound)
        value['observed_at'] -= bound['max_observation_gap_seconds'] + 1
        snapshots.append(copy.deepcopy(value))
        return value

    (g.outputs / 'fresh-growth').write_bytes(b'x' * 29)
    monkeypatch.setattr(limits, 'observe', stale)
    result = invoke(g)
    assert result['status'] == 'failed'
    assert 'became stale' in result['error']['message']
    retained = audit(g)['outcome']
    assert retained['fresh_observation_digest'] == digest(snapshots[0])
    assert retained['high_water_growth_bytes']['source_evidence_roots'] == 29
    assert not continuation_rows(g) and not archive(g).get('intercheck_observation_dispositions')


def test_failed_terminal_transition_rolls_back_archive_and_continuation_atomically(stopped, monkeypatch):
    g = stopped
    before = raw_archive(g)
    original = claims.transition

    def reject_success(db, expected, prepared):
        if prepared['state'] == 'success':
            assert db.execute("SELECT COUNT(*) FROM resource_limit WHERE work_id LIKE 'technical-continuation-%'").fetchone()[0] == 1
            assert json.loads(db.execute('SELECT record FROM technical_qualification').fetchone()[0]).get('intercheck_observation_dispositions')
            raise sqlite3.OperationalError('Synthetic final claim write failure')
        return original(db, expected, prepared)

    monkeypatch.setattr(claims, 'transition', reject_success)
    assert invoke(g)['status'] == 'failed'
    assert raw_archive(g) == before and not continuation_rows(g)
    assert audit(g)['state'] == 'failed'


def test_unknown_failure_persistence_leaves_exact_started_claim_held(stopped, monkeypatch):
    g = stopped
    before = raw_archive(g)
    original = claims.transition

    def reject_terminals(db, expected, prepared):
        if prepared['state'] in ('success', 'failed'):
            raise sqlite3.OperationalError('Synthetic unknown durable outcome')
        return original(db, expected, prepared)

    monkeypatch.setattr(claims, 'transition', reject_terminals)
    with pytest.raises(Rejected, match='durable observation claim remains held'):
        invoke(g)
    assert raw_archive(g) == before and not continuation_rows(g)
    assert audit(g)['state'] == 'scan_started' and audit(g)['outcome'] is None


def test_interruption_after_scan_start_is_retained_and_reraised(stopped, monkeypatch):
    g = stopped
    monkeypatch.setattr(limits, 'observe', lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        invoke(g)
    assert audit(g)['state'] == 'failed'
    assert audit(g)['outcome']['error']['code'] == 'KeyboardInterrupt'


def test_second_operation_cannot_acknowledge_same_successful_phase(stopped):
    g = stopped
    first = invoke(g)
    before = raw_archive(g)
    with pytest.raises(Rejected, match='retained disposition'):
        invoke(g, operation_id='second-operation')
    assert invoke(g) == first and raw_archive(g) == before


def test_successful_continuation_growth_keeps_original_acknowledgment_and_floors(stopped):
    g = stopped
    first = invoke(g)
    initial = audit(g)
    (g.outputs / 'later-passive-growth').write_bytes(b'x' * 23)
    with g.q.runtime._db() as db:
        db.execute('BEGIN IMMEDIATE')
        record = qualification._cold_read(db, g.q.qualification_id)
        assert g.q._refresh(db, record)['source_evidence_roots'] == 23
        qualification._save(db, g.q.qualification_id, record)
    item = archive(g)['intercheck_observation_dispositions'][0]
    assert item['resource_receipt']['high_water_growth_bytes']['source_evidence_roots'] == 23
    assert item['acknowledgment'] == first and audit(g) == initial


def test_prior_dispositions_and_unrelated_failures_are_preserved(stopped):
    g = stopped
    older_breach = {'reason': 'active_observation_gap', 'observed_at': 0.5}
    unrelated_breach = {'reason': 'unreviewed-other-failure', 'observed_at': 0.7}
    with g.q.runtime._db() as db:
        db.execute('BEGIN IMMEDIATE')
        record = qualification._read(db, g.q.qualification_id)
        earlier = copy.deepcopy(record['phases'][0])
        earlier.update(id='earlier', resource_id='earlier-resource')
        earlier['resource_receipt']['breach'] = copy.deepcopy(older_breach)
        earlier['receipt']['phase_id'] = 'earlier'
        earlier['receipt']['resource_limits'] = copy.deepcopy(earlier['resource_receipt'])
        prior_guard = copy.deepcopy(g.original_guard)
        prior_guard['breach'] = None
        prior_ack = {'accepted': False, 'kind': 'legacy-gap-disposition', 'phase_id': 'earlier',
                     'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0}
        prior = {'operation_id': 'earlier-review', 'phase_id': 'earlier', 'fingerprint': digest('earlier'),
                 'historical_breach': older_breach, 'resource_id': 'earlier-continuation',
                 'resource_receipt': limits.receipt(prior_guard), 'acknowledgment': prior_ack}
        limits._save(db, 'earlier-continuation', prior_guard)
        record['phases'].insert(0, earlier)
        record['quiescent_gap_dispositions'] = [prior]
        record['unresolved_failures'] = [copy.deepcopy(unrelated_breach)]
        qualification._save(db, g.q.qualification_id, record)
        technical_control.save(db, g.q.qualification_id, record['phases'][-1])
    result = invoke(g)
    assert result['status'] == 'acknowledged'
    retained = archive(g)
    assert retained['quiescent_gap_dispositions'] == [prior]
    assert retained['unresolved_failures'] == [unrelated_breach]
    assert retained['phases'] == record['phases']
    assert retained['charged_growth_bytes']['source_evidence_roots'] >= 6
    assert qualification._blocking(retained) is True
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        g.q.begin('still-blocked', g.recipe, max_seconds=30)


def test_export_carries_new_optional_audit_and_old_capsule_remains_readable(stopped):
    g = stopped
    result = invoke(g)
    phase = g.q.begin('successful-final', g.recipe, max_seconds=30)
    synthetic_private_receipts(g, phase)
    assert g.q.reconcile('successful-final')['status'] == 'passed'
    exported = g.q.export(g.recipe, g.q.directory / 'carry.json', work_id='feature')
    final = exported['recipe']
    path = Path(final['resource_limits_carry_forward']['path'])
    capsule = json.loads(path.read_text())
    assert capsule['intercheck_observation_dispositions'][0]['acknowledgment'] == result
    assert capsule['charged_growth_bytes']['source_evidence_roots'] >= 3
    bound = limits.scope(final)
    assert limits._carry_forward(final, bound)['intercheck_observation_dispositions'] == capsule['intercheck_observation_dispositions']
    legacy = copy.deepcopy(capsule)
    legacy.pop('intercheck_observation_dispositions')
    legacy_path = g.q.directory / 'legacy-carry.json'
    raw = canonical(legacy).encode()
    legacy_path.write_bytes(raw)
    legacy_path.chmod(0o600)
    old = copy.deepcopy(final)
    old['native_protected_paths'].append(str(legacy_path))
    old['resource_limits_carry_forward'] = {'path': str(legacy_path), 'sha256': hashlib.sha256(raw).hexdigest()}
    assert 'intercheck_observation_dispositions' not in limits._carry_forward(old, bound)


def test_preclaim_stale_archive_rejects_without_any_new_claim_or_scan(stopped, monkeypatch):
    g = stopped
    recheck = recovery.recheck

    def change_before_reservation(proof):
        recheck(proof)
        change_authority(g, 'archive-aba')

    monkeypatch.setattr(recovery, 'recheck', change_before_reservation)
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('Rejected preclaim authority cannot scan'))
    with pytest.raises(Rejected, match='authority changed'):
        invoke(g)
    assert audit(g) is None and not continuation_rows(g)


def test_intervening_sidecar_recheck_rejects_before_claim_publication(stopped, monkeypatch):
    g = stopped
    monkeypatch.setattr(recovery, 'recheck_sidecars',
                        lambda proof: (_ for _ in ()).throw(Rejected('technical_intercheck', 'Synthetic latch changed')))
    with pytest.raises(Rejected, match='latch changed'):
        invoke(g)
    assert audit(g) is None and not continuation_rows(g)


@pytest.mark.parametrize('size,expected', [
    (claims.MAX_BYTES, 'encoded bound'),
    (claims.MAX_BYTES - 32 * 1024, 'reserved encoding bound'),
])
def test_oversized_terminal_envelope_rejects_before_claim_or_scan(stopped, monkeypatch, size, expected):
    g = stopped
    before = raw_archive(g)
    validate = recovery.validate

    def oversized(*args, **kwargs):
        return {**validate(*args, **kwargs), 'opaque_fixture_artifact': 'x' * size}

    monkeypatch.setattr(recovery, 'validate', oversized)
    monkeypatch.setattr(claims, 'reserve', lambda *args: pytest.fail('The oversized envelope cannot reserve'))
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('The oversized envelope cannot observe'))
    with pytest.raises(Rejected, match=expected):
        invoke(g)
    assert raw_archive(g) == before and audit(g) is None and not continuation_rows(g)


@pytest.mark.parametrize('encoder', ['archive-canonical', 'success-claim'])
def test_success_encoding_failure_keeps_small_failed_audit_and_exact_observation(stopped, monkeypatch, encoder):
    g = stopped
    before = raw_archive(g)
    observe, validate = limits.observe, recovery.validate
    encode, next_state = qualification.canonical, claims.next_state
    scans = []

    def large_proof(*args, **kwargs):
        return {**validate(*args, **kwargs), 'opaque_fixture_artifact': 'x' * (96 * 1024)}

    def observed(bound):
        value = observe(bound)
        scans.append(copy.deepcopy(value))
        return value

    def broken_archive(value):
        if scans and isinstance(value, dict) and 'phases' in value:
            raise ValueError('Synthetic success archive encoding failure')
        return encode(value)

    def broken_success(expected, state, **kwargs):
        if scans and state == 'success':
            raise ValueError('Synthetic success claim encoding failure')
        return next_state(expected, state, **kwargs)

    (g.outputs / 'fresh-growth').write_bytes(b'x' * 37)
    monkeypatch.setattr(recovery, 'validate', large_proof)
    monkeypatch.setattr(limits, 'observe', observed)
    if encoder == 'archive-canonical':
        monkeypatch.setattr(qualification, 'canonical', broken_archive)
    else:
        monkeypatch.setattr(claims, 'next_state', broken_success)
    result = invoke(g)
    retained = audit(g)
    assert len(scans) == 1 and result['status'] == 'failed'
    assert retained['state'] == 'failed' and retained['outcome']['error']['code'] == 'ValueError'
    assert retained['outcome']['high_water_growth_bytes']['source_evidence_roots'] == 37
    assert retained['outcome']['fresh_observation_digest'] == digest(scans[0])
    assert result['proof_digest'] == retained['proof_digest']
    assert result['observed_resource_breach'] is None
    assert len(retained['_raw'].encode()) < 64 * 1024
    assert 'proof' not in result and 'resource_limits' not in result
    assert 'opaque_fixture_artifact' not in retained['_raw']
    assert 'files' not in retained['outcome'] and 'baseline' not in retained['outcome']
    assert raw_archive(g) == before and not continuation_rows(g)
    assert_historical_guard_unchanged(g)


def test_failed_encoder_after_actual_scan_keeps_scan_started_held(stopped, monkeypatch):
    g = stopped
    before = raw_archive(g)
    observe, next_state = limits.observe, claims.next_state
    scans = []

    def observed(bound):
        value = observe(bound)
        scans.append(copy.deepcopy(value))
        return value

    def unavailable_failure(expected, state, **kwargs):
        if scans and state == 'failed':
            raise ValueError('Synthetic failed audit encoder unavailable')
        return next_state(expected, state, **kwargs)

    (g.outputs / 'cap-overrun').write_bytes(b'x' * 101)
    monkeypatch.setattr(limits, 'observe', observed)
    monkeypatch.setattr(claims, 'next_state', unavailable_failure)
    with pytest.raises(Rejected) as error:
        invoke(g)
    assert error.value.code == 'technical_intercheck_pending'
    assert len(scans) == 1 and scans[0]['files']['source_evidence_roots']
    retained = audit(g)
    assert retained['state'] == 'scan_started' and retained['outcome'] is None
    assert raw_archive(g) == before and not continuation_rows(g)
    monkeypatch.setattr(limits, 'observe', lambda *args: pytest.fail('An uncertain outcome must not rescan'))
    assert invoke(g)['status'] == 'held' and audit(g) == retained


def test_full_archive_copy_finishes_before_one_shot_claim_and_observation(stopped, monkeypatch):
    g = stopped
    # A bounded retained payload makes this assertion cover the large archive
    # operation, rather than an incidental small nested copy.
    with g.q.runtime._db() as db:
        db.execute('BEGIN IMMEDIATE')
        record = qualification._read(db, g.q.qualification_id)
        record['synthetic_retained_payload'] = 'x' * (256 * 1024)
        qualification._save(db, g.q.qualification_id, record)
        technical_control.save(db, g.q.qualification_id, record['phases'][0])
    deepcopy, reserve, observe = copy.deepcopy, claims.reserve, limits.observe
    order = []

    def copied(value, memo=None):
        if isinstance(value, dict) and 'synthetic_retained_payload' in value:
            assert order == [], 'The full archive copy cannot follow reservation or observation'
            assert audit(g) is None
            order.append('archive-copy')
        return deepcopy(value, memo)

    def reserved(db, value):
        assert order == ['archive-copy']
        order.append('reserve')
        return reserve(db, value)

    def observed(bound):
        assert order == ['archive-copy', 'reserve']
        order.append('observe')
        return observe(bound)

    monkeypatch.setattr(copy, 'deepcopy', copied)
    monkeypatch.setattr(claims, 'reserve', reserved)
    monkeypatch.setattr(limits, 'observe', observed)
    assert invoke(g)['status'] == 'acknowledged'
    assert order == ['archive-copy', 'reserve', 'observe']
