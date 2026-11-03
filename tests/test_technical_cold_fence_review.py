"""Cold accounting cannot use an obsolete archive around a current owner.

All journals and check receipts are synthetic unit fixtures. No native command,
permission probe, product reservation, or model turn is executed here.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import sqlite3

import pytest

from agentic_tools.aos import native_admission_recovery, native_quiescence
from agentic_tools.aos import native_qualification as qualification
from agentic_tools.aos import resource_limits, technical_control
from agentic_tools.aos.store import Rejected
from test_native_qualification_independent import synthetic_private_receipts, technical


def record(g):
    with g.q.runtime._db() as db:
        return qualification._read(db, g.q.qualification_id)


def raw_record(g):
    with g.q.runtime._db() as db:
        return db.execute('SELECT record FROM technical_qualification WHERE id=?',
                          (g.q.qualification_id,)).fetchone()[0]


def counts(g):
    with g.q.runtime._db() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                     for table in ('resource_limit', 'native_check', 'technical_phase_control'))


def compact_bytes(g, phase_id='current'):
    with g.q.runtime._db() as db:
        return db.execute('SELECT record FROM technical_phase_control WHERE qualification_id=? AND phase_id=?',
                          (g.q.qualification_id, phase_id)).fetchone()[0]


def passed_history(g):
    phase = g.q.begin('passed', g.recipe, max_seconds=30)
    synthetic_private_receipts(g, phase)
    receipt = g.q.reconcile('passed')
    assert receipt['status'] == 'passed' and receipt['stopped']
    return record(g)


def owner_phase(g, monkeypatch, state='running', *, q=None):
    q = q or g.q
    if state == 'prepared':
        sample = resource_limits.sample

        def before_activation(*args, **kwargs):
            if kwargs.get('activate'):
                raise Rejected('synthetic_pause', 'Stop this fixture before resource activation')
            return sample(*args, **kwargs)

        with monkeypatch.context() as change:
            change.setattr(resource_limits, 'sample', before_activation)
            with pytest.raises(Rejected, match='synthetic_pause'):
                q.begin('current', g.recipe, max_seconds=30)
    else:
        q.begin('current', g.recipe, max_seconds=30)
        if state != 'running':
            with q.runtime._db() as db:
                db.execute('BEGIN IMMEDIATE')
                before = technical_control.load(db, q.qualification_id)
                changed = {**copy.deepcopy(before['phase']), 'state': state}
                technical_control.save(db, q.qualification_id, changed, expected=before)
    return compact_bytes(g)


def forbidden_read(*args, **kwargs):
    raise AssertionError('The stale full archive was read before the compact owner fence')


def call_ack(g, kind):
    method = {
        'ack-gap': g.q.acknowledge_quiescent_gap,
        'ack-prelaunch': g.q.acknowledge_prelaunch_observation_failure,
        'ack-rejected': g.q.acknowledge_rejected_admission_observation,
    }[kind]
    return method('review-idle', 'passed', evidence_path=g.q.directory / 'not-read.json',
                  evidence_sha256='0' * 64, reason='Synthetic cold-fence unit test')


def cold_call(g, kind, previous):
    if kind == 'begin':
        return g.q.begin('next', g.recipe, max_seconds=30)
    if kind == 'export':
        return g.q.export(g.recipe, g.q.directory / 'must-not-export.json', work_id='feature')
    if kind == 'extension':
        return g.q.extend_generated_root('add-output', g.recipe, g.source / 'generated', reason='Fixture extension')
    if kind.startswith('ack-'):
        return call_ack(g, kind)
    if kind == 'refresh':
        with g.q.runtime._db() as db:
            return g.q._refresh(db, copy.deepcopy(previous))
    if kind == 'historical-reconcile':
        return g.q.reconcile('passed')
    raise AssertionError(kind)


@pytest.mark.parametrize('state', ['prepared', 'running', 'closing'])
@pytest.mark.parametrize('operation', [
    'begin', 'export', 'extension', 'ack-gap', 'ack-prelaunch', 'ack-rejected', 'refresh', 'historical-reconcile'])
def test_stale_legacy_archive_cannot_bypass_any_cold_owner_fence(technical, monkeypatch, state, operation):
    g = technical
    previous = passed_history(g)
    current = owner_phase(g, monkeypatch, state)
    (g.source / 'generated').mkdir()
    # Exercise the real old-producer write, including its archive-revision trigger.
    with g.q.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, previous)
    before = raw_record(g), counts(g)
    with monkeypatch.context() as change:
        change.setattr(qualification, '_read', forbidden_read)
        with pytest.raises(Rejected, match='archive binding|active private phase'):
            cold_call(g, operation, previous)
    assert (raw_record(g), counts(g)) == before
    assert compact_bytes(g) == current
    assert not (g.q.directory / 'must-not-export.json').exists()
    assert not json.loads(raw_record(g)).get('generated_root_extensions')
    assert not any(key.endswith('_dispositions') and value for key, value in json.loads(raw_record(g)).items())


def test_archive_aba_bytes_do_not_restore_old_control_epoch(technical, monkeypatch):
    g = technical
    previous = passed_history(g)
    owner_phase(g, monkeypatch)
    live = record(g)
    original = raw_record(g)
    with g.q.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, previous)
        qualification._save(db, g.q.qualification_id, live)
    assert raw_record(g) == original
    with monkeypatch.context() as change:
        change.setattr(qualification, '_read', forbidden_read)
        with pytest.raises(Rejected, match='archive binding'):
            g.q.export(g.recipe, g.q.directory / 'aba.json', work_id='feature')
    assert not (g.q.directory / 'aba.json').exists()


def test_begin_rechecks_owner_between_fast_lookup_and_first_cold_transaction(technical, monkeypatch):
    g = technical
    passed_history(g)
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    load, read = technical_control.load, qualification._read
    injected, inside = [], []

    def racing_load(db, qid, phase_id=None, **kwargs):
        found = load(db, qid, phase_id, **kwargs)
        if phase_id is None and found is None and not injected and not inside:
            inside.append(True)
            try:
                owner_phase(g, monkeypatch, q=other)
            finally:
                inside.pop()
            injected.append(True)
        return found

    def checked_read(*args):
        assert not injected, 'The first cold transaction read the archive after another owner appeared'
        return read(*args)

    try:
        with monkeypatch.context() as change:
            change.setattr(technical_control, 'load', racing_load)
            change.setattr(qualification, '_read', checked_read)
            with pytest.raises(Rejected, match='active private phase'):
                g.q.begin('victim', g.recipe, max_seconds=30)
        assert injected == [True]
        assert json.loads(compact_bytes(g))['phase']['id'] == 'current'
        assert len(record(g)['phases']) == 2
    finally:
        other.close()


def inject_after_context(g, monkeypatch, ordinal, action):
    original = g.q.runtime._db
    exited = []

    @contextmanager
    def controlled():
        with original() as db:
            yield db
        exited.append(True)
        if len(exited) == ordinal:
            action()

    monkeypatch.setattr(g.q.runtime, '_db', controlled)
    return exited


@pytest.mark.parametrize('operation,context_number', [('begin', 2), ('export', 1), ('export', 2)])
def test_cold_transactions_recheck_owner_after_each_intermediate_commit(
        technical, monkeypatch, operation, context_number):
    g = technical
    passed_history(g)
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    read, injected = qualification._read, []

    def race():
        owner_phase(g, monkeypatch, q=other)
        injected.append(True)

    def checked_read(*args):
        assert not injected, 'A later cold transaction read history before checking its current owner'
        return read(*args)

    try:
        with monkeypatch.context() as change:
            inject_after_context(g, change, context_number, race)
            change.setattr(qualification, '_read', checked_read)
            with pytest.raises(Rejected, match='active private phase'):
                if operation == 'begin':
                    g.q.begin('victim', g.recipe, max_seconds=30)
                else:
                    g.q.export(g.recipe, g.q.directory / 'race.json', work_id='feature')
        assert injected == [True]
        assert not (g.q.directory / 'race.json').exists()
        assert not record(g).get('export')
        assert json.loads(compact_bytes(g))['phase']['id'] == 'current'
    finally:
        other.close()


@pytest.mark.parametrize('kind', ['ack-gap', 'ack-prelaunch', 'ack-rejected'])
def test_acknowledgment_rechecks_owner_after_review_before_commit(technical, monkeypatch, kind):
    g = technical
    previous = passed_history(g)
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    read, injected = qualification._read, []
    before_count = counts(g)[0]

    def validated(*args, **kwargs):
        # This fixture isolates the commit fence, not forensic proof validation.
        owner_phase(g, monkeypatch, q=other)
        injected.append(True)
        return {'native_rows': {}, 'historical_native_rows': {}, 'historical_breaches': [],
                'minimum_high_water_growth_bytes': {'output_roots': 0, 'source_evidence_roots': 0},
                'failure_sidecar': {'path': str(g.q.directory / 'not-read-sidecar'), 'sha256': '0' * 64,
                                    'content': {}}, 'absent_artifacts': []}

    def checked_read(*args):
        assert not injected, 'Acknowledgment commit read the archive before checking the new owner'
        return read(*args)

    try:
        with monkeypatch.context() as change:
            change.setattr(native_quiescence, 'evidence', lambda *args, **kwargs: {})
            change.setattr(native_quiescence, 'validate', validated)
            change.setattr(native_quiescence, 'validate_prelaunch', validated)
            change.setattr(native_admission_recovery, 'evidence', lambda *args, **kwargs: {})
            change.setattr(native_admission_recovery, 'validate', validated)
            change.setattr(qualification, '_read', checked_read)
            with pytest.raises(Rejected, match='active private phase'):
                call_ack(g, kind)
        assert injected == [True]
        after = record(g)
        assert all(after.get(key) == previous.get(key) for key in (
            'quiescent_gap_dispositions', 'prelaunch_observation_dispositions', 'rejected_admission_dispositions'))
        assert counts(g)[0] == before_count + 1, 'Only the racing phase, never a continuation, may add a guard'
    finally:
        other.close()


def test_inactive_historical_archive_remains_readable_and_exportable(technical):
    g = technical
    previous = passed_history(g)
    # A stopped legacy same-byte save invalidates only a stale inactive control.
    with g.q.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, previous)
    assert g.q.reconcile('passed') == previous['phases'][0]['receipt']
    result = g.q.export(g.recipe, g.q.directory / 'valid.json', work_id='feature')
    path = g.q.directory / 'valid.json'
    assert path.is_file() and result['capsule']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result['accepted'] is False and json.loads(path.read_text())['all_phases_stopped']


def test_matching_closing_owner_can_finalize_its_own_archive(technical, monkeypatch):
    g = technical
    passed_history(g)
    owner_phase(g, monkeypatch, 'closing')
    before = json.loads(compact_bytes(g))
    result = g.q.reconcile('current')
    after = json.loads(compact_bytes(g))
    assert result['stopped'] and result['status'] == 'blocked'
    assert after['phase']['state'] == 'stopped' and after['active'] is False
    for key in ('started_at', 'deadline', 'started_monotonic', 'deadline_monotonic', 'recipe_digest'):
        assert after['phase'][key] == before['phase'][key]
    assert record(g)['active_phase'] is None


def test_closing_owner_cas_is_checked_before_cold_archive_read(technical, monkeypatch):
    g = technical
    passed_history(g)
    owner_phase(g, monkeypatch, 'closing')
    sample = resource_limits.sample_file
    changed = []

    def sampled(*args, **kwargs):
        result = sample(*args, **kwargs)
        if kwargs.get('finish') and not changed:
            with g.q.runtime._db() as db:
                db.execute('BEGIN IMMEDIATE')
                before = technical_control.load(db, g.q.qualification_id)
                held = {**copy.deepcopy(before['phase']), 'state': 'cleanup_unknown',
                        'execution_errors': ['Concurrent exact-owner hold']}
                technical_control.save(db, g.q.qualification_id, held, expected=before)
            changed.append(True)
        return result

    with monkeypatch.context() as change:
        change.setattr(resource_limits, 'sample_file', sampled)
        change.setattr(qualification, '_read', forbidden_read)
        with pytest.raises(Rejected, match='control changed before archive reconciliation'):
            g.q.reconcile('current')
    assert changed == [True]
    assert json.loads(compact_bytes(g))['phase']['state'] == 'cleanup_unknown'
    assert record(g)['phases'][-1]['state'] == 'running'


@pytest.mark.parametrize('operation', ['ack-gap', 'ack-prelaunch', 'ack-rejected', 'historical-reconcile'])
def test_preflight_fence_and_archive_read_share_one_sqlite_snapshot(technical, monkeypatch, operation):
    g = technical
    previous = passed_history(g)
    original, witnessed = qualification._read, []

    class AfterPreflight(Exception):
        pass

    def snapshot_read(db, *args):
        assert db.in_transaction, 'The owner fence and full archive read need the same read transaction'
        peer = sqlite3.connect(g.q.runtime.path, timeout=0)
        try:
            peer.execute('BEGIN IMMEDIATE')
            peer.execute('UPDATE technical_qualification SET record=record WHERE id=?', (g.q.qualification_id,))
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                peer.commit()
            peer.rollback()
        finally:
            peer.close()
        witnessed.append(True)
        return original(db, *args)

    def stop_after_read(*args, **kwargs):
        raise AfterPreflight

    with monkeypatch.context() as change:
        change.setattr(qualification, '_read', snapshot_read)
        change.setattr(native_quiescence, 'evidence', stop_after_read)
        change.setattr(native_admission_recovery, 'evidence', stop_after_read)
        if operation == 'historical-reconcile':
            assert g.q.reconcile('passed') == previous['phases'][0]['receipt']
        else:
            with pytest.raises(AfterPreflight):
                call_ack(g, operation)
    assert witnessed == [True]
    assert record(g) == previous


@pytest.mark.parametrize('damage', ['omission', 'replacement', 'duplicate', 'not-stopped', 'missing-stop-receipt'])
@pytest.mark.parametrize('operation', ['begin', 'export', 'refresh'])
def test_inactive_completed_control_cannot_disappear_or_change_in_archive(
        technical, monkeypatch, damage, operation):
    g = technical
    previous = passed_history(g)
    changed = copy.deepcopy(previous)
    if damage == 'omission':
        changed['phases'] = []
    elif damage == 'replacement':
        changed['phases'][0]['deadline'] += 1
    elif damage == 'duplicate':
        changed['phases'].append(copy.deepcopy(changed['phases'][0]))
    elif damage == 'not-stopped':
        changed['phases'][0]['state'] = 'running'
    else:
        changed['phases'][0]['receipt']['stopped'] = False
    with g.q.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, changed)
    before = raw_record(g), counts(g)
    with pytest.raises(Rejected, match='retained stopped phase|phase identities|retained control binding'):
        cold_call(g, operation, changed)
    assert (raw_record(g), counts(g)) == before
    assert not (g.q.directory / 'must-not-export.json').exists()


def test_deleting_entire_archive_cannot_reinitialize_retained_completed_controls(technical):
    g = technical
    passed_history(g)
    with g.q.runtime._db() as db:
        db.execute('DELETE FROM technical_qualification WHERE id=?', (g.q.qualification_id,))
    before = counts(g)
    with pytest.raises(Rejected, match='archive|retained|completed'):
        g.q.begin('new-history', g.recipe, max_seconds=30)
    assert counts(g) == before
    with g.q.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM technical_qualification').fetchone()[0] == 0


def test_completed_archive_check_is_inclusion_not_control_set_equality(technical):
    g = technical
    previous = passed_history(g)
    legacy = copy.deepcopy(previous['phases'][0])
    legacy['id'] = 'legacy-archive-only'
    legacy['receipt']['phase_id'] = legacy['id']
    previous['phases'].insert(0, legacy)
    with g.q.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, previous)
        assert db.execute('SELECT COUNT(*) FROM technical_phase_control').fetchone()[0] == 1
        technical_control.check_completed_archive(db, g.q.qualification_id, previous)
    # The retained old stopped control has an earlier epoch; legacy history is
    # still admissible without manufacturing a second compact control.
    next_phase = g.q.begin('next', g.recipe, max_seconds=30)
    assert next_phase['id'] == 'next'
    assert g.q.reconcile('next')['stopped']
    assert [phase['id'] for phase in record(g)['phases']] == ['legacy-archive-only', 'passed', 'next']


def completed_owner_then_identical_legacy_overwrite(g, other):
    with other.runtime._db() as db:
        before = db.execute('SELECT record FROM technical_qualification WHERE id=?',
                            (g.q.qualification_id,)).fetchone()[0]
        epoch = technical_control.revision(db, g.q.qualification_id)
    other.begin('intervening', g.recipe, max_seconds=30)
    assert other.reconcile('intervening')['stopped']
    with other.runtime._db() as db:
        qualification._save(db, g.q.qualification_id, json.loads(before))
        assert technical_control.revision(db, g.q.qualification_id) > epoch
        assert db.execute('SELECT COUNT(*) FROM technical_phase_control WHERE active=1').fetchone()[0] == 0
        assert db.execute('SELECT record FROM technical_qualification WHERE id=?',
                          (g.q.qualification_id,)).fetchone()[0] == before
    return before


@pytest.mark.parametrize('operation,context_number', [('begin', 2), ('export', 1), ('export', 2)])
def test_completed_owner_archive_aba_rejects_before_later_cold_read(
        technical, monkeypatch, operation, context_number):
    g = technical
    passed_history(g)
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    read, intervened = qualification._read, []

    def race():
        intervened.append(completed_owner_then_identical_legacy_overwrite(g, other))

    def checked_read(*args):
        assert not intervened, 'Archive epoch must reject before decoding identical stale history'
        return read(*args)

    try:
        with monkeypatch.context() as change:
            inject_after_context(g, change, context_number, race)
            change.setattr(qualification, '_read', checked_read)
            with pytest.raises(Rejected, match='archive revision changed'):
                if operation == 'begin':
                    g.q.begin('victim', g.recipe, max_seconds=30)
                else:
                    g.q.export(g.recipe, g.q.directory / 'completed-aba.json', work_id='feature')
        assert len(intervened) == 1 and raw_record(g) == intervened[0]
        assert not (g.q.directory / 'completed-aba.json').exists()
        assert json.loads(compact_bytes(g, 'intervening'))['active'] is False
    finally:
        other.close()


@pytest.mark.parametrize('kind', ['ack-gap', 'ack-prelaunch', 'ack-rejected'])
def test_ack_commit_rejects_completed_owner_archive_aba_before_read(technical, monkeypatch, kind):
    g = technical
    passed_history(g)
    other = qualification.TechnicalQualification(g.q.directory, g.q.qualification_id)
    read, intervened = qualification._read, []

    def validated(*args, **kwargs):
        intervened.append(completed_owner_then_identical_legacy_overwrite(g, other))
        return {'native_rows': {}, 'historical_native_rows': {}, 'historical_breaches': [],
                'minimum_high_water_growth_bytes': {'output_roots': 0, 'source_evidence_roots': 0},
                'failure_sidecar': {'path': str(g.q.directory / 'not-read-sidecar'), 'sha256': '0' * 64,
                                    'content': {}}, 'absent_artifacts': []}

    def checked_read(*args):
        assert not intervened, 'Acknowledgment must reject an ABA epoch before cold history decoding'
        return read(*args)

    try:
        with monkeypatch.context() as change:
            change.setattr(native_quiescence, 'evidence', lambda *args, **kwargs: {})
            change.setattr(native_quiescence, 'validate', validated)
            change.setattr(native_quiescence, 'validate_prelaunch', validated)
            change.setattr(native_admission_recovery, 'evidence', lambda *args, **kwargs: {})
            change.setattr(native_admission_recovery, 'validate', validated)
            change.setattr(qualification, '_read', checked_read)
            with pytest.raises(Rejected, match='archive revision changed'):
                call_ack(g, kind)
        assert len(intervened) == 1 and raw_record(g) == intervened[0]
        assert counts(g)[0] == 2, 'Only the completed intervening phase may add a resource guard'
    finally:
        other.close()
