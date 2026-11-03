"""Host-only native qualification; never an approval or a model assignment.

Each phase uses the normal native boundary in a separate private journal.
Observed growth is conservatively debited before a corrected phase can start.
The final capsule carries a saved baseline through the later approval wait.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

from . import native_paths, native_processes, resource_limits, technical_control
from . import native_journal, native_intercheck_claim
from agentic_tools.check_adapters import identities as adapter_identities
from .codex_runtime import CodexRuntime, inventory
from .store import Rejected, canonical, digest, identifier

FORMAT = 'aos-technical-native-qualification-v1'
CAPS = {'output_roots': 'max_output_growth_bytes',
        'source_evidence_roots': 'max_source_evidence_growth_bytes'}


def _failure(record, breach):
    if breach is not None:
        if record['breach'] is None:
            record['breach'] = breach
        elif record['breach'] != breach and breach not in record.setdefault('unresolved_failures', []):
            record['unresolved_failures'].append(breach)


def _dispositions(record):
    return [*record.get('quiescent_gap_dispositions', []), *record.get('prelaunch_observation_dispositions', []),
            *record.get('rejected_admission_dispositions', []),
            *record.get('intercheck_observation_dispositions', [])]


def _acknowledged(record, breach, phase_id=None):
    return breach is not None and any(
        breach in item.get('historical_breaches', [item['historical_breach']])
        and (phase_id is None or item['phase_id'] == phase_id)
        for item in _dispositions(record))


def _blocking(record):
    if (any(not _acknowledged(record, failure) for failure in record.get('unresolved_failures', []))
            or record['breach'] and not _acknowledged(record, record['breach'])):
        return True
    for phase in record['phases']:
        breach = phase.get('resource_receipt', {}).get('breach')
        if breach and not _acknowledged(record, breach, phase['id']):
            return True
    return any(item['resource_receipt']['breach'] for item in _dispositions(record))


def _contract(recipe):
    bound = resource_limits.scope(recipe)
    if bound is None:
        raise Rejected('technical_qualification', 'Explicit resource limits are required')
    return {key: value for key, value in bound.items() if key != 'temporary_paths'} | {'outputs': recipe['outputs']}


def _read(db, qualification_id):
    row = db.execute('SELECT record FROM technical_qualification WHERE id=?', (qualification_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _save(db, qualification_id, record):
    db.execute('INSERT INTO technical_qualification VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
               (qualification_id, canonical(record)))


def _require_idle_control(db, qualification_id, *, intercheck_claim=None):
    """A stale archive cannot authorize cold accounting around a live owner."""
    if technical_control.load(db, qualification_id, active_only=True) is not None:
        raise Rejected('technical_qualification', 'An active private phase must be reconciled before archival accounting')
    native_intercheck_claim.require_idle(db, qualification_id, allowed=intercheck_claim)


def _cold_read(db, qualification_id):
    _require_idle_control(db, qualification_id)
    record = _read(db, qualification_id)
    technical_control.check_completed_archive(db, qualification_id, record)
    native_intercheck_claim.check_dispositions(db, qualification_id, record)
    return record


def _phase_authority(db, recipe):
    key = recipe.get('_technical_resource_phase')
    if recipe.get('_attempt_id') or not isinstance(key, dict) or set(key) != {'qualification_id', 'phase_id'}:
        raise Rejected('technical_qualification', 'Exact private technical phase identity required')
    control = technical_control.load(db, key['qualification_id'], key['phase_id'], active_only=True)
    clean_recipe = {k: v for k, v in recipe.items() if k != '_technical_resource_phase'}
    if control is None:
        raise Rejected('technical_qualification', 'The exact private technical phase has no current control; legacy phases are cleanup-only')
    phase = control['phase']
    if (phase['state'] != 'running' or phase.get('execution_started') is not True
            or phase.get('observer_started') is not True or not isinstance(phase.get('observer_owner'), dict)
            or phase['recipe_digest'] != digest(clean_recipe) or not isinstance(phase['resource_id'], str)):
        raise Rejected('technical_qualification', 'The private technical phase does not authorize this exact recipe')
    owner = phase['observer_owner']
    try:
        live = native_processes.same(owner, native_processes.identity(owner['pid']))
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        raise Rejected('technical_qualification', 'The exact private observer identity is unavailable') from error
    if not live:
        raise Rejected('technical_qualification', 'The exact private observer is no longer alive')
    # The passive archival interval never grants native preparation authority.
    active = db.execute("SELECT json_extract(record,'$.active_attempt') FROM resource_limit WHERE work_id=?",
                        (phase['resource_id'],)).fetchone()
    if not active or active[0] != phase['id']:
        raise Rejected('technical_qualification', 'The exact private resource observer is not active')
    return phase


def resource_payload(database, recipe):
    """Called only by the private native runtime, never a workload override."""
    with sqlite3.connect(database) as db:
        phase = _phase_authority(db, recipe)
        bound = resource_limits.recorded_scope(db, phase['resource_id'], recipe)
        guard = resource_limits._read(db, phase['resource_id'])
        resource_limits.enforce(guard)
    return {'database': str(database), 'work_id': phase['resource_id'], 'bound': bound, 'attempt_id': phase['id'],
            'last_observed_at': guard['last_observation']['observed_at']}


def _phase_deadline(phase):
    """The observer retains this immutable clock, not the growing journal."""
    monotonic_now = time.monotonic()
    start, end = phase['started_monotonic'], phase['deadline_monotonic']
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end))
            or monotonic_now < start):
        raise Rejected('technical_qualification', 'The original technical phase clock cannot be resumed')
    return min(datetime.fromtimestamp(phase['deadline'], timezone.utc),
               datetime.now(timezone.utc) + timedelta(seconds=end - monotonic_now))


def deadline(database, recipe):
    with sqlite3.connect(database) as db:
        phase = _phase_authority(db, recipe)
        guard = resource_limits._read(db, phase['resource_id'])
        if guard is None:
            raise Rejected('technical_qualification', 'Original resource observation is missing')
        resource_limits.enforce(guard)
    return _phase_deadline(phase)


def _budget_bytes(paths):
    """Byte proof only: never connect to or interpret the product journal."""
    result = {}
    for value in paths:
        path = Path(value)
        if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
            raise Rejected('technical_qualification', 'Budget proof paths must be canonical and contain no aliases')
        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


class TechnicalQualification:
    def __init__(self, directory, qualification_id, *, budget_paths=()):
        identifier(qualification_id)
        self.directory = Path(directory)
        if self.directory.resolve() != self.directory or any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise Rejected('technical_qualification', 'A canonical private qualification directory is required')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Rejected('technical_qualification', 'The technical journal must be private and owned')
        self.qualification_id, self.budget_paths = qualification_id, tuple(map(str, budget_paths))
        self._observer_callbacks = {}
        # This directory has no lifecycle store, concept, decision or assignment.
        self.runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=self.directory)), {})
        with self.runtime._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS technical_qualification (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            technical_control.initialize(db)

    def close(self):
        self.runtime.close()

    def acknowledge_quiescent_gap(self, operation_id, phase_id, *, evidence_path, evidence_sha256, reason):
        return self._acknowledge_resource_observation(operation_id, phase_id, evidence_path=evidence_path,
                                                    evidence_sha256=evidence_sha256, reason=reason, prelaunch=False)

    def acknowledge_prelaunch_observation_failure(self, operation_id, phase_id, *, evidence_path, evidence_sha256, reason):
        return self._acknowledge_resource_observation(operation_id, phase_id, evidence_path=evidence_path,
                                                    evidence_sha256=evidence_sha256, reason=reason, prelaunch=True)

    def acknowledge_rejected_admission_observation(self, operation_id, phase_id, *, evidence_path, evidence_sha256, reason):
        """Review a stopped zero-row admission refusal; never authorize work."""
        return self._acknowledge_resource_observation(operation_id, phase_id, evidence_path=evidence_path,
                                                    evidence_sha256=evidence_sha256, reason=reason,
                                                    prelaunch=False, rejected_admission=True)

    def acknowledge_intercheck_observation_failure(self, operation_id, phase_id, *,
                                                   evidence_path, evidence_sha256, reason):
        """Retain an exact intercheck failure; never authorize another check.

        The exclusive operation claim precedes the fresh passive observation.
        An interrupted scan remains held, and even a failed final authority CAS
        retains the observation under that claim rather than rewriting history.
        """
        from . import native_intercheck_recovery
        identifier(operation_id)
        identifier(phase_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise Rejected('technical_intercheck', 'A bounded explicit host review reason is required')
        kind = 'technical-intercheck-observation-disposition'
        fingerprint = digest([kind, phase_id, str(evidence_path), evidence_sha256, reason])

        def replay(claim):
            if claim['phase_id'] != phase_id or claim['fingerprint'] != fingerprint:
                raise Rejected('technical_intercheck', 'The operation cannot change its exact reviewed evidence')
            if claim['outcome'] is not None:
                return copy.deepcopy(claim['outcome']['acknowledgment'])
            return {'accepted': False, 'kind': kind, 'status': 'held', 'disposition_created': False,
                    'qualification_id': self.qualification_id, 'phase_id': phase_id,
                    'operation_id': operation_id, 'claim_state': claim['state'],
                    'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0,
                    'detail': 'The original observation claim is unresolved; no scan was repeated'}

        with self.runtime._db() as db:
            db.execute('BEGIN')
            if technical_control.load(db, self.qualification_id, active_only=True) is not None:
                raise Rejected('technical_intercheck', 'An active technical phase prevents disposition')
            if previous := native_intercheck_claim.get(db, self.qualification_id, operation_id=operation_id):
                return replay(previous)
            size = db.execute('SELECT length(CAST(record AS BLOB)) FROM technical_qualification WHERE id=?',
                              (self.qualification_id,)).fetchone()
            if not size or type(size[0]) is not int or not 0 < size[0] <= 256 * 1024 * 1024:
                raise Rejected('technical_intercheck', 'The historical archive is missing or exceeds its proof bound')
            for table, maximum in (('native_check', 1024), ('resource_limit', 96)):
                count, largest, total = db.execute(
                    f'SELECT COUNT(*),COALESCE(MAX(length(CAST(record AS BLOB))),0),'
                    f'COALESCE(SUM(length(CAST(record AS BLOB))),0) FROM {table}').fetchone()
                if (not 0 <= count <= maximum or not 0 <= largest <= 64 * 1024 * 1024
                        or not 0 <= total <= 1024 * 1024 * 1024):
                    raise Rejected('technical_intercheck', 'The retained proof inventory exceeds its bounded size')
            record = _cold_read(db, self.qualification_id)
            if not record:
                raise Rejected('technical_intercheck', 'An existing private qualification is required')
            if any(item['phase_id'] == phase_id or item['operation_id'] == operation_id
                   for item in _dispositions(record)):
                raise Rejected('technical_intercheck', 'A phase or operation already has a retained disposition')
            phase = next((item for item in record['phases'] if item['id'] == phase_id), None)
            if phase is None:
                raise Rejected('technical_intercheck', 'The exact historical phase is missing')
            control = technical_control.load(db, self.qualification_id, phase_id)
            snapshot = {
                'archive_raw': db.execute('SELECT record FROM technical_qualification WHERE id=?',
                                          (self.qualification_id,)).fetchone()[0],
                'archive_revision': technical_control.revision(db, self.qualification_id),
                'native_revision': native_journal.revision(db),
                'native_rows': dict(db.execute('SELECT id,record FROM native_check')),
                'resource_guards': dict(db.execute('SELECT work_id,record FROM resource_limit'))}
            guard = json.loads(snapshot['resource_guards'][phase['resource_id']])
        packet = native_intercheck_recovery.evidence(evidence_path, evidence_sha256)
        proof = native_intercheck_recovery.validate(self, record, phase, guard, packet,
                                                    control=control, snapshot=snapshot)
        if (record['budget_paths'] != list(self.budget_paths)
                or _budget_bytes(self.budget_paths) != record['budget_before']
                or inventory(Path(phase['recipe']['source']), recipe=phase['recipe']) != record['source_baseline']):
            raise Rejected('technical_intercheck', 'Protected source or product budget differs from its original baseline')
        authority = {'archive_revision': snapshot['archive_revision'], 'native_revision': snapshot['native_revision'],
                     'archive_sha256': hashlib.sha256(snapshot['archive_raw'].encode()).hexdigest(),
                     'native_rows': {key: hashlib.sha256(raw.encode()).hexdigest()
                                     for key, raw in snapshot['native_rows'].items()},
                     'resource_guards': {key: hashlib.sha256(raw.encode()).hexdigest()
                                        for key, raw in snapshot['resource_guards'].items()}, 'control': control}
        own = native_processes.identity(os.getpid())
        if own is None:
            raise Rejected('technical_intercheck', 'The exact host operation owner is unavailable')
        owner = {key: own[key] for key in ('pid', 'uid', 'birth')}
        resource_id = 'technical-continuation-' + digest([self.qualification_id, operation_id])[7:31]
        prospective_latch = resource_limits._failure_path(str(self.runtime.path), resource_id)
        if prospective_latch.exists() or prospective_latch.is_symlink():
            raise Rejected('technical_intercheck', 'The prospective continuation has an unmerged failure latch')
        proof = {**proof, 'prospective_failure_sidecar_absent': str(prospective_latch)}
        prepared = native_intercheck_claim.prepare(
            self.qualification_id, phase_id, operation_id, fingerprint=fingerprint,
            authority_digest=digest(authority), proof_digest=digest(proof), owner=owner)

        def authority_unchanged(db, allowed=None):
            _require_idle_control(db, self.qualification_id, intercheck_claim=allowed)
            if (technical_control.revision(db, self.qualification_id) != snapshot['archive_revision']
                    or native_journal.revision(db) != snapshot['native_revision']
                    or technical_control.load(db, self.qualification_id, phase_id) != control):
                raise Rejected('technical_intercheck', 'Reviewed authority changed before disposition')
            exact = db.execute('SELECT record=? FROM technical_qualification WHERE id=?',
                               (snapshot['archive_raw'], self.qualification_id)).fetchone()
            if exact != (1,) or {key for key, in db.execute('SELECT work_id FROM resource_limit')} != set(snapshot['resource_guards']):
                raise Rejected('technical_intercheck', 'Reviewed archive or resource inventory changed')
            for key, raw in snapshot['resource_guards'].items():
                if db.execute('SELECT record=? FROM resource_limit WHERE work_id=?', (raw, key)).fetchone() != (1,):
                    raise Rejected('technical_intercheck', 'A reviewed original guard or continuation changed')
            if resource_id in snapshot['resource_guards']:
                raise Rejected('technical_intercheck', 'The prospective continuation identity already exists')
            if prospective_latch.exists() or prospective_latch.is_symlink():
                raise Rejected('technical_intercheck', 'A prospective continuation failure latch appeared')
            current_owner = native_processes.identity(owner['pid'])
            if not native_processes.same(owner, current_owner):
                raise Rejected('technical_intercheck', 'The exact passive observation owner changed')

        # Prepare invariant, potentially large values before reserving the
        # one-shot observation and before its original freshness clock starts.
        updated = copy.deepcopy(record)
        baseline_digest = digest(guard['baseline'])
        continuation = copy.deepcopy(guard)
        continuation.update(breach=None, active_attempt=None,
                            high_water_growth_bytes=copy.deepcopy(proof['minimum_high_water_growth_bytes']),
                            historical_breach_reference={'resource_id': phase['resource_id'],
                                                         'guard_digest': digest(guard), 'breach': guard['breach']})
        before_observation = continuation['last_observation']
        fresh = False

        def acknowledgment(status, error=None):
            ack = {'accepted': False, 'kind': kind, 'status': status,
                    'disposition_created': status == 'acknowledged',
                    'operation_id': operation_id, 'qualification_id': self.qualification_id,
                    'phase_id': phase_id, 'evidence': {'path': str(evidence_path), 'sha256': evidence_sha256},
                    'reason': reason, 'proof_digest': prepared['proof_digest'],
                    'historical_breach': copy.deepcopy(guard['breach']),
                    'historical_status': phase['receipt']['status'],
                    'original_started_at': phase['started_at'], 'original_deadline': phase['deadline'],
                    'original_elapsed_seconds': phase['receipt']['elapsed_seconds'],
                    'continuation_resource_id': resource_id if status == 'acknowledged' else None,
                    'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0,
                    'observed_at': time.time(), 'error': error}
            if status == 'acknowledged':
                ack.update(proof=proof, resource_limits=resource_limits.receipt(continuation))
            else:
                ack['observed_resource_breach'] = copy.deepcopy(continuation['breach'])
            # Failed outcomes retain exact scope/baseline/floors in the claim,
            # without repeating a potentially large proof or observed inventory.
            return ack

        def outcome(ack, error):
            return {'acknowledgment': ack, 'scope_digest': guard['scope_digest'],
                    'baseline_digest': baseline_digest,
                    'high_water_growth_bytes': copy.deepcopy(continuation['high_water_growth_bytes']),
                    'fresh_observation_digest': digest(continuation['last_observation']) if fresh else None,
                    'error': error, 'disposition_created': error is None}

        # Validate the complete terminal envelope before any claim or scan.
        # Reserve ample room for the small observation-dependent fields; a
        # later exceptional success encoder still falls back to the compact
        # failed envelope rather than attempting to duplicate the proof again.
        prospective_started = native_intercheck_claim.next_state(prepared, 'scan_started')
        success_shape = outcome(acknowledgment('acknowledged'), None)
        success_shape['fresh_observation_digest'] = 'sha256:' + '0' * 64
        success_envelope = native_intercheck_claim.next_state(prospective_started, 'success', outcome=success_shape)
        failure_shape = {'code': 'x' * 64, 'message': 'x' * 500}
        failure_ack_shape = acknowledgment('failed', failure_shape)
        # The producer's bounded failure details (including per-device free
        # space) must survive a failed continuation without a full inventory.
        failure_ack_shape['observed_resource_breach'] = {'reserved_encoding_space': 'x' * (64 * 1024)}
        failure_envelope = native_intercheck_claim.next_state(
            prospective_started, 'failed', outcome=outcome(failure_ack_shape, failure_shape))
        if (len(success_envelope['_raw'].encode()) > native_intercheck_claim.MAX_BYTES - 64 * 1024
                or len(failure_envelope['_raw'].encode()) > 128 * 1024):
            raise Rejected('technical_intercheck', 'The terminal observation audit exceeds its reserved encoding bound')
        native_intercheck_recovery.recheck(proof)
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            authority_unchanged(db)
            native_intercheck_recovery.recheck_sidecars(proof)
            native_intercheck_claim.initialize(db)
            claim = native_intercheck_claim.reserve(db, prepared)
        try:
            started = native_intercheck_claim.next_state(claim, 'scan_started')
            with self.runtime._db() as db:
                db.execute('BEGIN IMMEDIATE')
                authority_unchanged(db, allowed=claim)
                native_intercheck_recovery.recheck_sidecars(proof)
                claim = native_intercheck_claim.transition(db, claim, started)
            # All observation, process/artifact verification and serialization
            # occur after releasing the writer lock. The original guard never
            # receives a cleared breach or a replacement baseline.
            continuation, _ = resource_limits._sample_record(
                continuation, continuation['scope'], phase_id, None, force=True)
            fresh = continuation['last_observation'] is not before_observation
            if continuation['breach'] is not None:
                raise Rejected('resource_limit_exceeded', 'The fresh passive observation failed')
            native_intercheck_recovery.recheck(proof)
            if (_budget_bytes(self.budget_paths) != record['budget_before']
                    or inventory(Path(phase['recipe']['source']), recipe=phase['recipe']) != record['source_baseline']):
                raise Rejected('technical_intercheck', 'A protected baseline changed during passive observation')
            ack = acknowledgment('acknowledged')
            updated.setdefault('intercheck_observation_dispositions', []).append({
                'operation_id': operation_id, 'fingerprint': fingerprint, 'phase_id': phase_id,
                'historical_breach': copy.deepcopy(guard['breach']), 'resource_id': resource_id,
                'resource_receipt': ack['resource_limits'], 'acknowledgment': ack})
            contributions = {item['phase_id']: item['resource_receipt'] for item in _dispositions(updated)}
            updated['charged_growth_bytes'] = {
                group: max(record['charged_growth_bytes'][group],
                           sum(contributions.get(p['id'], p['resource_receipt'])['high_water_growth_bytes'][group]
                               for p in record['phases'])) for group in CAPS}
            guard_raw = resource_limits._bounded_record(canonical(continuation))
            archive_raw = canonical(updated)
            succeeded = native_intercheck_claim.next_state(claim, 'success', outcome=outcome(ack, None))
            age = time.time() - continuation['last_observation']['observed_at']
            if not 0 <= age <= guard['scope']['max_observation_gap_seconds']:
                raise Rejected('resource_observation', 'The fresh passive observation became stale before commit')
            with self.runtime._db() as db:
                db.execute('BEGIN IMMEDIATE')
                authority_unchanged(db, allowed=claim)
                native_intercheck_recovery.recheck_sidecars(proof)
                age = time.time() - continuation['last_observation']['observed_at']
                if not 0 <= age <= guard['scope']['max_observation_gap_seconds']:
                    raise Rejected('resource_observation', 'The fresh passive observation became stale before commit')
                db.execute('INSERT INTO resource_limit VALUES(?,?)', (resource_id, guard_raw))
                changed = db.execute('UPDATE technical_qualification SET record=? WHERE id=? AND record=?',
                                     (archive_raw, self.qualification_id, snapshot['archive_raw'])).rowcount
                if changed != 1:
                    raise Rejected('technical_intercheck', 'The original archive changed during disposition')
                native_intercheck_claim.transition(db, claim, succeeded)
            return ack
        except BaseException as error:
            fresh = continuation['last_observation'] is not before_observation
            detail = {'code': str(getattr(error, 'code', type(error).__name__))[:64],
                      'message': (str(error) or type(error).__name__)[:500]}
            try:
                failed_ack = acknowledgment('failed', detail)
                failed = native_intercheck_claim.next_state(claim, 'failed', outcome=outcome(failed_ack, detail))
                with self.runtime._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = native_intercheck_claim.get(db, self.qualification_id, operation_id=operation_id)
                    if current and current['state'] in ('success', 'failed'):
                        return replay(current)
                    native_intercheck_claim.transition(db, claim, failed)
            except BaseException as persistence_error:
                raise Rejected('technical_intercheck_pending',
                               'The durable observation claim remains held; its outcome could not be saved') from persistence_error
            if not isinstance(error, Exception):
                raise
            return failed_ack

    def _acknowledge_resource_observation(self, operation_id, phase_id, *, evidence_path, evidence_sha256, reason,
                                        prelaunch, rejected_admission=False):
        """Retain a reviewed idle-gap disposition; authorize no work or time.

        The original guard and phase remain untouched. A separate passive
        observation carries their baseline and all high-water charges forward.
        A later, separately authorized phase still uses normal begin/run rules.
        """
        from . import native_quiescence, native_admission_recovery
        disposition_key = ('rejected_admission_dispositions' if rejected_admission else
                           'prelaunch_observation_dispositions' if prelaunch else 'quiescent_gap_dispositions')
        disposition_kind = ('technical-rejected-admission-observation-disposition' if rejected_admission else
                            'technical-prelaunch-observation-disposition' if prelaunch
                            else 'technical-quiescent-gap-disposition')
        identifier(operation_id)
        identifier(phase_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise Rejected('technical_quiescence', 'A bounded explicit host review reason is required')
        fingerprint = digest([phase_id, str(evidence_path), evidence_sha256, reason])
        with self.runtime._db() as db:
            db.execute('BEGIN')
            record = _cold_read(db, self.qualification_id)
            archive_revision = technical_control.revision(db, self.qualification_id) if record else None
            if not record:
                raise Rejected('technical_quiescence', 'An existing private qualification is required')
            prior = next((item for item in _dispositions(record)
                          if item['operation_id'] == operation_id), None)
            if prior:
                if prior['fingerprint'] != fingerprint or prior['acknowledgment']['kind'] != disposition_kind:
                    raise Rejected('technical_quiescence', 'A disposition operation cannot change its reviewed evidence')
                return copy.deepcopy(prior['acknowledgment'])
            phase = next((p for p in record['phases'] if p['id'] == phase_id), None)
            if phase is None:
                raise Rejected('technical_quiescence', 'The exact historical phase is missing')
            guard = resource_limits._read(db, phase['resource_id'])
            control = technical_control.load(db, self.qualification_id, phase_id) if rejected_admission else None
        if rejected_admission:
            packet = native_admission_recovery.evidence(evidence_path, evidence_sha256)
            proof = native_admission_recovery.validate(self, record, phase, guard, packet, control=control)
        else:
            packet = native_quiescence.evidence(evidence_path, evidence_sha256, prelaunch=prelaunch)
            validator = native_quiescence.validate_prelaunch if prelaunch else native_quiescence.validate
            proof = validator(self, record, phase, guard, packet)
        if (record['budget_paths'] != list(self.budget_paths)
                or _budget_bytes(self.budget_paths) != record['budget_before']
                or inventory(Path(phase['recipe']['source']), recipe=phase['recipe']) != record['source_baseline']):
            raise Rejected('technical_quiescence', 'Protected source or product budget differs from its original baseline')
        resource_id = 'technical-continuation-' + digest([self.qualification_id, operation_id])[7:31]
        continuation = copy.deepcopy(guard)
        continuation.update(breach=None, active_attempt=None,
                            historical_breach_reference={'resource_id': phase['resource_id'],
                                                         'guard_digest': digest(guard), 'breach': guard['breach']})
        if rejected_admission:
            continuation['high_water_growth_bytes'] = copy.deepcopy(proof['minimum_high_water_growth_bytes'])
            continuation['historical_breach_reference'].update(
                breaches=copy.deepcopy(proof['historical_breaches']), failure_sidecar=copy.deepcopy(proof['failure_sidecar']))
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_idle_control(db, self.qualification_id)
            if technical_control.revision(db, self.qualification_id) != archive_revision:
                raise Rejected('technical_control', 'The reviewed archive revision changed before disposition')
            if _cold_read(db, self.qualification_id) != record or resource_limits._read(db, phase['resource_id']) != guard:
                raise Rejected('technical_quiescence', 'The reviewed qualification changed before disposition')
            if rejected_admission:
                if technical_control.load(db, self.qualification_id, phase_id) != control:
                    raise Rejected('technical_quiescence', 'The reviewed stopped control changed before disposition')
                sidecar = resource_limits._read_failure(db, phase['resource_id'], guard['scope'])
                raw = Path(proof['failure_sidecar']['path']).read_bytes()
                if (sidecar != proof['failure_sidecar']['content']
                        or hashlib.sha256(raw).hexdigest() != proof['failure_sidecar']['sha256']):
                    raise Rejected('technical_quiescence', 'The retained secondary observation failure changed')
                for path in proof['absent_artifacts']:
                    if Path(path).exists() or Path(path).is_symlink():
                        raise Rejected('technical_quiescence', 'A rejected-admission artifact appeared after review')
            current_rows = set()
            for key, output, state in db.execute("SELECT id,json_extract(record,'$.output'),"
                                                  "json_extract(record,'$.phase') FROM native_check"):
                if (not isinstance(output, str) or not Path(output).is_absolute() or str(Path(output)) != output
                        or '..' in Path(output).parts or state != 'clean'):
                    raise Rejected('technical_quiescence', 'Native ownership changed before disposition')
                if Path(output).is_relative_to(Path(phase['output'])):
                    current_rows.add(key)
            if current_rows != set(proof['native_rows']):
                raise Rejected('technical_quiescence', 'Native ownership changed before disposition')
            expected_rows = proof['historical_native_rows'] if rejected_admission else proof['native_rows']
            if rejected_admission and {row[0] for row in db.execute('SELECT id FROM native_check')} != set(expected_rows):
                raise Rejected('technical_quiescence', 'Historical native ownership changed before disposition')
            for key, expected in expected_rows.items():
                raw = db.execute('SELECT record FROM native_check WHERE id=?', (key,)).fetchone()
                if not raw or hashlib.sha256(raw[0].encode()).hexdigest() != expected:
                    raise Rejected('technical_quiescence', 'A reviewed native row changed before disposition')
            if resource_limits._read(db, resource_id) is not None:
                raise Rejected('technical_quiescence', 'A prior continuation attempt already owns this exact identity')
            resource_limits._save(db, resource_id, continuation)
            continuation = resource_limits.sample(db, resource_id, continuation['scope'], phase_id, force=True)
            observed = resource_limits.receipt(continuation)
            _failure(record, observed['breach'])
            acknowledgment = {'accepted': False, 'kind': disposition_kind,
                              'operation_id': operation_id, 'qualification_id': self.qualification_id,
                              'phase_id': phase_id, 'evidence': {'path': str(evidence_path), 'sha256': evidence_sha256},
                              'reason': reason, 'proof': proof, 'historical_breach': copy.deepcopy(guard['breach']),
                              'historical_status': phase['receipt']['status'],
                              'original_started_at': phase['started_at'], 'original_deadline': phase['deadline'],
                              'original_elapsed_seconds': phase['receipt']['elapsed_seconds'],
                              'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0,
                              'observed_at': time.time(), 'resource_limits': observed}
            if rejected_admission:
                acknowledgment['historical_breaches'] = copy.deepcopy(proof['historical_breaches'])
                acknowledgment['failure_sidecar'] = copy.deepcopy(proof['failure_sidecar'])
                for breach in proof['historical_breaches']:
                    _failure(record, breach)
            if observed['breach'] is None:
                item = {'operation_id': operation_id, 'fingerprint': fingerprint, 'phase_id': phase_id,
                     'historical_breach': copy.deepcopy(guard['breach']), 'resource_id': resource_id,
                     'resource_receipt': observed, 'acknowledgment': acknowledgment}
                if rejected_admission:
                    item['historical_breaches'] = copy.deepcopy(proof['historical_breaches'])
                record.setdefault(disposition_key, []).append(item)
                contributions = {item['phase_id']: item['resource_receipt']
                                 for item in _dispositions(record)}
                record['charged_growth_bytes'] = {
                    group: max(record['charged_growth_bytes'][group],
                               sum(contributions.get(p['id'], p['resource_receipt'])['high_water_growth_bytes'][group]
                                   for p in record['phases'])) for group in CAPS}
            else:
                key = ('rejected_admission_failed_dispositions' if rejected_admission else
                       'prelaunch_failed_dispositions' if prelaunch else 'quiescent_gap_failed_dispositions')
                record.setdefault(key, []).append(acknowledgment)
            _save(db, self.qualification_id, record)
        if observed['breach']:
            raise Rejected('resource_limit_exceeded', 'Fresh prospective observation failed; historical gap remains blocked')
        return acknowledgment

    def _refresh(self, db, record):
        _require_idle_control(db, self.qualification_id)
        technical_control.check_completed_archive(db, self.qualification_id, record)
        native_intercheck_claim.check_dispositions(db, self.qualification_id, record)
        if record['active_phase'] is not None:
            raise Rejected('technical_qualification', 'An unresolved technical phase must be reconciled first')
        if record['phases']:
            phase = record['phases'][-1]
            continuation = next((item for item in _dispositions(record)
                                 if item['phase_id'] == phase['id']), None)
            resource_id = continuation['resource_id'] if continuation else phase['resource_id']
            guard = resource_limits._read(db, resource_id)
            guard = resource_limits.sample(db, resource_id, guard['scope'], phase['id'], force=True)
            (continuation if continuation else phase)['resource_receipt'] = resource_limits.receipt(guard)
            if guard['breach']:
                _failure(record, guard['breach'])
        contributions = {item['phase_id']: item['resource_receipt']
                         for item in _dispositions(record)}
        charges = {group: max(record['charged_growth_bytes'][group],
                             sum(contributions.get(p['id'], p['resource_receipt'])['high_water_growth_bytes'][group]
                                 for p in record['phases']))
                   for group in CAPS}
        record['charged_growth_bytes'] = charges
        return charges

    @staticmethod
    def _pending_generated_failure(record, baseline=None):
        for pending in record.get('pending_generated_roots', []):
            root = Path(pending['path'])
            try:
                identity = resource_limits._identity(root)
                if identity != pending['identity'] or (baseline is not None
                        and baseline['identities'].get(str(root)) != pending['identity']):
                    return {'reason': 'generated_extension_identity_changed', 'path': str(root), 'observed_at': time.time()}
                captured = {} if baseline is None else {name: size for name, size in baseline['files']['output_roots'].items()
                                                        if Path(name).is_relative_to(root)}
                with os.scandir(root) as entries:
                    nonempty = next(entries, None) is not None
                if nonempty or captured:
                    return {'reason': 'generated_extension_not_empty', 'path': str(root),
                            'captured_bytes': sum(captured.values()), 'captured_files': len(captured),
                            'observed_at': time.time()}
            except (OSError, ValueError):
                return {'reason': 'generated_extension_identity_changed', 'path': str(root), 'observed_at': time.time()}
        return None

    def extend_generated_root(self, operation_id, recipe, root, *, reason):
        """Append one empty generated source root without resetting accounting.

        This host-only correction grants no product approval. Its pending empty
        identity is checked again against the next captured phase baseline.
        """
        identifier(operation_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise Rejected('technical_qualification', 'An explicit bounded reason for the generated root is required')
        if any(key.startswith('_') for key in recipe) or recipe.get('resource_limits_carry_forward'):
            raise Rejected('technical_qualification', 'Generated roots can only extend original technical inputs')
        path = resource_limits._path(str(root))
        source = Path(recipe['source'])
        if path == source or not path.is_relative_to(source):
            raise Rejected('technical_qualification', 'An added generated root must be a strict source child')
        fingerprint = digest([recipe, str(path), reason])
        corrected = copy.deepcopy(recipe)
        corrected.setdefault('native_write_roots', []).append({'path': str(path), 'kind': 'generated'})
        corrected['resource_limits']['output_roots'] = sorted([*corrected['resource_limits']['output_roots'], str(path)])
        protected = (self.directory, Path.home() / '.codex', *map(Path, self.budget_paths))
        native_paths.validate(corrected, protected=protected)
        resource_limits.scope(corrected, protected=protected)
        prior_contract, next_contract = _contract(recipe), _contract(corrected)
        changed = {key for key in prior_contract if prior_contract[key] != next_contract[key]}
        if changed != {'generated_roots', 'output_roots'}:
            raise Rejected('technical_qualification', 'Only one additive generated/output root is permitted')
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = _cold_read(db, self.qualification_id)
            if not record:
                raise Rejected('technical_qualification', 'A stopped existing qualification is required')
            previous = next((item for item in record.get('generated_root_extensions', [])
                             if item['extension']['operation_id'] == operation_id), None)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Rejected('technical_qualification', 'An extension operation cannot be reused with changed inputs')
                return {key: value for key, value in previous.items() if key != 'fingerprint'}
            if (record.get('export') or record['active_phase'] is not None or not record['phases']
                    or any(phase['state'] != 'stopped' or not phase.get('receipt', {}).get('stopped')
                           or not phase['receipt'].get('source_unchanged') or not phase['receipt'].get('product_budget_unchanged')
                           for phase in record['phases'])):
                raise Rejected('technical_qualification', 'Every phase must be positively stopped before an unexported extension')
            if (record['contract'] != prior_contract or record['budget_paths'] != list(self.budget_paths)
                    or len(record.get('generated_root_extensions', [])) >= 16):
                raise Rejected('technical_qualification', 'The exact bounded prior contract and budget proof are required')
            for phase in record['phases']:
                guard = resource_limits._read(db, phase['resource_id'])
                if not guard or guard['active_attempt'] is not None or guard['breach']:
                    raise Rejected('technical_qualification', 'Every prior resource observer must be stopped without a breach')
                for row in db.execute('SELECT record FROM native_check'):
                    native = json.loads(row[0])
                    if Path(native['output']).is_relative_to(Path(phase['output'])) and (
                            native['phase'] != 'clean' or native.get('cleanup', {}).get('stopped') is not True
                            or native.get('cleanup', {}).get('effects_reconciled') is not True):
                        raise Rejected('technical_qualification', 'A prior native check has unresolved effects')
            charges = self._refresh(db, record)
            _failure(record, self._pending_generated_failure(record))
            if (inventory(source, recipe=recipe) != record['source_baseline']
                    or _budget_bytes(self.budget_paths) != record['budget_before']):
                _failure(record, {'reason': 'protected_baseline_changed', 'observed_at': time.time()})
            identity = resource_limits._identity(path)
            pending = {'path': str(path), 'identity': identity, 'operation_id': operation_id}
            pending_failure = self._pending_generated_failure({'pending_generated_roots': [pending]})
            _failure(record, pending_failure)
            if not _blocking(record):
                extension = {'operation_id': operation_id, 'path': str(path), 'identity': identity,
                             'reason': reason, 'observed_at': time.time(),
                             'prior_contract_digest': digest(prior_contract), 'contract_digest': digest(next_contract),
                             'input_recipe_digest': digest(recipe), 'output_recipe_digest': digest(corrected),
                             'source_baseline_digest': digest(record['source_baseline']),
                             'charged_growth_bytes': charges, 'prior_phases': [phase['id'] for phase in record['phases']]}
                response = {'accepted': False, 'kind': 'technical-generated-root-extension',
                            'recipe': corrected, 'extension': extension}
                record.setdefault('original_contract', copy.deepcopy(prior_contract))
                record['contract'] = next_contract
                record.setdefault('generated_root_extensions', []).append({**response, 'fingerprint': fingerprint})
                record.setdefault('pending_generated_roots', []).append(pending)
            _save(db, self.qualification_id, record)
        if _blocking(record):
            raise Rejected('resource_limit_exceeded', 'The retained resource/source proof forbids a generated-root extension')
        return response

    def begin(self, phase_id, recipe, *, max_seconds=1800):
        """Durably admit one technical phase; corrections use a new phase ID."""
        identifier(phase_id)
        if type(max_seconds) is not int or not 1 <= max_seconds <= 3600:
            raise Rejected('technical_qualification', 'A bounded technical phase of at most 3600 seconds is required')
        if any(key.startswith('_') for key in recipe) or recipe.get('resource_limits_carry_forward'):
            raise Rejected('technical_qualification', 'Technical inputs cannot impersonate a runtime assignment or import a prior export')
        if recipe.get('native_process_ownership') != native_processes.BACKEND:
            raise Rejected('technical_qualification', 'Technical native execution requires normal census ownership')
        native_paths.validate(recipe, protected=(self.directory, Path.home() / '.codex', *map(Path, self.budget_paths)))
        resource_limits.scope(recipe, protected=(self.directory, Path.home() / '.codex', *map(Path, self.budget_paths)))
        current = _contract(recipe)
        original_digest = digest(recipe)
        # A live control is compact and authoritative even while its archive
        # intentionally retains the original mutable state.
        with self.runtime._db() as db:
            native_intercheck_claim.require_idle(db, self.qualification_id)
            control = technical_control.load(db, self.qualification_id)
            if control:
                prior = control['phase']
                if prior['id'] != phase_id:
                    raise Rejected('technical_qualification', 'An unresolved technical phase must be reconciled first')
                if prior['input_digest'] != original_digest or prior['max_seconds'] != max_seconds:
                    raise Rejected('technical_qualification', 'A phase ID cannot be reused with changed inputs')
                return {**copy.deepcopy(prior), 'checks': []}
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = _cold_read(db, self.qualification_id)
            if record is None:
                record = {'format': FORMAT, 'accepted': False, 'contract': current, 'phases': [],
                          'active_phase': None, 'breach': None, 'charged_growth_bytes': {group: 0 for group in CAPS},
                          'budget_paths': list(self.budget_paths), 'budget_before': _budget_bytes(self.budget_paths),
                          'source_baseline': inventory(Path(recipe['source']), recipe=recipe)}
            if record['contract'] != current or record['budget_paths'] != list(self.budget_paths):
                raise Rejected('technical_qualification', 'Original roots, ceilings and product budget proof cannot change')
            if record.get('export'):
                raise Rejected('technical_qualification', 'Exported qualification cannot spend its transferred allowance again')
            prior = next((phase for phase in record['phases'] if phase['id'] == phase_id), None)
            if prior:
                if prior['input_digest'] != original_digest or prior['max_seconds'] != max_seconds:
                    raise Rejected('technical_qualification', 'A phase ID cannot be reused with changed inputs')
                return copy.deepcopy(prior)
            charges = self._refresh(db, record)
            _failure(record, self._pending_generated_failure(record))
            if (inventory(Path(recipe['source']), recipe=recipe) != record['source_baseline']
                    or _budget_bytes(self.budget_paths) != record['budget_before']):
                _failure(record, {'reason': 'protected_baseline_changed', 'observed_at': time.time()})
            _save(db, self.qualification_id, record)
            archive_revision = technical_control.revision(db, self.qualification_id)
        # Retain any newly observed failure before rejecting further admission.
        if _blocking(record):
            raise Rejected('resource_limit_exceeded', 'Technical resource failure remains recorded')
        if len(record['phases']) >= 32:
            raise Rejected('technical_qualification', 'The bounded technical phase journal is full')
        bounded = copy.deepcopy(recipe)
        bounded['native_protected_paths'] = sorted(set(bounded.get('native_protected_paths', [])) | set(self.budget_paths))
        for group, cap in CAPS.items():
            remaining = current[cap] - charges[group]
            if remaining <= 0:
                raise Rejected('resource_limit_exceeded', 'No remaining resource allowance for another technical phase')
            bounded['resource_limits'][cap] = remaining
        bounded['_adapter_bindings'] = adapter_identities()
        phase = {'id': phase_id, 'input_digest': original_digest, 'recipe_digest': digest(bounded),
                 'recipe': bounded, 'state': 'running', 'started_at': time.time(),
                 'max_seconds': max_seconds, 'deadline': time.time() + max_seconds,
                 'started_monotonic': time.monotonic(), 'deadline_monotonic': time.monotonic() + max_seconds,
                 'resource_id': 'technical-' + digest([self.qualification_id, phase_id])[7:39],
                 'output': str(Path(recipe['outputs']) / ('technical-' + phase_id)), 'checks': []}
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_idle_control(db, self.qualification_id)
            if technical_control.revision(db, self.qualification_id) != archive_revision:
                raise Rejected('technical_control', 'The archive revision changed during admission')
            latest = _cold_read(db, self.qualification_id)
            if latest != record:
                raise Rejected('technical_qualification', 'The private qualification journal changed during admission')
            bound = resource_limits.scope(bounded)
            temporary = resource_limits._temporary_roots(bound)
            baseline = resource_limits.observe({**bound, '_temporary_baseline': temporary})
            baseline['temporary_baseline'] = temporary
            # Capture the new baseline before finalizing the old debit. Growth
            # during rollover is then in at least one observation, never hidden
            # by the next phase's freshly captured files.
            charges = self._refresh(db, latest)
            _failure(latest, self._pending_generated_failure(latest, baseline))
            exhausted = _blocking(latest) or any(current[cap] - charges[group] <= 0 for group, cap in CAPS.items())
            if not exhausted:
                for group, cap in CAPS.items():
                    bounded['resource_limits'][cap] = current[cap] - charges[group]
                phase['recipe'], phase['recipe_digest'] = bounded, digest(bounded)
                bound = resource_limits.scope(bounded)
                guard = resource_limits._initial_record(bound, baseline)
                resource_limits._save(db, phase['resource_id'], guard)
                # Archival JSON and baseline rollover run while passive. The
                # immutable clocks above already include this elapsed time.
                guard = resource_limits.sample(db, phase['resource_id'], bound, phase_id, force=True)
                phase['resource_receipt'] = resource_limits.receipt(guard)
                latest['phases'].append(phase)
                latest['active_phase'] = phase_id
                latest['pending_generated_roots'] = []
            _save(db, self.qualification_id, latest)
            if not exhausted:
                technical_control.save(db, self.qualification_id, {**phase, 'state': 'prepared'})
        if exhausted:
            raise Rejected('resource_limit_exceeded', 'No remaining technical allowance after rollover observation')
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            control = technical_control.load(db, self.qualification_id, phase_id, active_only=True)
            if control is None or control['phase']['state'] != 'prepared':
                raise Rejected('technical_qualification', 'Private phase activation changed during admission')
            if _phase_deadline(phase) <= datetime.now(timezone.utc):
                raise Rejected('technical_qualification', 'The original technical phase deadline expired during passive admission')
            guard = resource_limits.sample(db, phase['resource_id'], bound, phase_id, force=True, activate=True)
            phase['resource_receipt'] = resource_limits.receipt(guard)
            technical_control.save(db, self.qualification_id, phase, expected=control)
        resource_limits.enforce(guard)
        if _phase_deadline(phase) <= datetime.now(timezone.utc):
            raise Rejected('technical_qualification', 'The original technical phase deadline expired during activation')
        return copy.deepcopy(phase)

    def _rows(self, phase):
        from . import native_journal
        root = Path(phase['output'])
        with self.runtime._db() as db:
            if native_journal.verify(db, allow_absent=True):
                return native_journal.rows_for_output(db, root)
            # Validate compact identities for every row, but decode large
            # receipts only for this phase. Invalid/missing paths and phases
            # cannot disappear through SQL NULL or prefix filtering.
            selected = []
            for key, value, state in db.execute("SELECT id,json_extract(record,'$.output'),"
                                                "json_extract(record,'$.phase') FROM native_check"):
                if (not isinstance(value, str) or not Path(value).is_absolute()
                        or str(Path(value)) != value or '..' in Path(value).parts
                        or state not in ('intent', 'running', 'clean', 'cleanup_unknown')):
                    raise Rejected('technical_qualification', 'A native row has an unresolved phase or output identity')
                if Path(value).is_relative_to(root):
                    selected.append(key)
            return [json.loads(db.execute('SELECT record FROM native_check WHERE id=?', (key,)).fetchone()[0])
                    for key in selected]

    def _cancel(self, phase):
        for row in self._rows(phase):
            if row['phase'] != 'clean':
                self.runtime._native_stop(row['check_id'], cancel=True)

    def run(self, phase_id, recipe, *, max_seconds=1800):
        phase = self.begin(phase_id, recipe, max_seconds=max_seconds)
        if phase['state'] != 'running':
            if phase['state'] == 'stopped':
                return phase['receipt']
            raise Rejected('technical_qualification', 'An admitted phase can only continue through reconciliation')
        bounded = copy.deepcopy(phase['recipe'])
        bounded['_technical_resource_phase'] = {'qualification_id': self.qualification_id, 'phase_id': phase_id}
        stop, failures, cancellers = threading.Event(), [], []
        with self.runtime._db() as db:
            observation_bound = resource_limits.recorded_scope(db, phase['resource_id'], bounded)
        def watch():
            previous = phase['resource_receipt']['observed_at']
            while not stop.wait(bounded['resource_limits']['sample_interval_seconds']):
                try:
                    original_deadline = _phase_deadline(phase).timestamp()
                    guard = resource_limits.sample_file(str(self.runtime.path), phase['resource_id'], observation_bound, phase_id,
                                                        deadline=original_deadline, last_observed_at=previous)
                    previous = guard['observed_at']
                    resource_limits.enforce(guard)
                    if _phase_deadline(phase) <= datetime.now(timezone.utc):
                        raise Rejected('technical_qualification', 'The original technical phase deadline expired')
                except Exception as error:
                    if str(error) not in failures:
                        failures.append(str(error))
                    # Cleanup may wait for admission; observation continues.
                    if not stop.is_set() and not cancellers:
                        def cancel():
                            try:
                                self._cancel(phase)
                            except Exception as cleanup_error:
                                failures.append(str(cleanup_error))
                        canceller = threading.Thread(target=cancel, daemon=True)
                        cancellers.append(canceller)
                        canceller.start()
        watcher = threading.Thread(target=watch, daemon=True)
        joined_saved = False
        observer_owner = native_processes.identity(os.getpid())
        if observer_owner is None:
            raise Rejected('technical_qualification', 'The technical observer needs its exact process identity')
        def stop_observer():
            nonlocal joined_saved
            if joined_saved:
                return True
            stop.set()
            if watcher.ident is not None:
                watcher.join(timeout=resource_limits.SCAN_SECONDS + 5)
            if watcher.is_alive():
                failures.append('Resource observer did not stop')
            for canceller in cancellers:
                if canceller.ident is not None:
                    canceller.join(timeout=resource_limits.SCAN_SECONDS + 5)
                if canceller.is_alive():
                    failures.append('Resource cleanup owner did not stop')
            joined = not watcher.is_alive() and all(not owner.is_alive() for owner in cancellers)
            if joined:
                with self.runtime._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    control = technical_control.load(db, self.qualification_id, phase_id, active_only=True)
                    if control is None:
                        raise Rejected('technical_qualification', 'The exact observer control is missing before its join receipt')
                    saved = copy.deepcopy(control['phase'])
                    if saved.get('observer_owner') != observer_owner:
                        raise Rejected('technical_qualification', 'The observer owner changed before its join receipt')
                    saved['observer_joined'] = {'owner': observer_owner, 'observed_at': time.time(),
                                                'watcher_joined': True, 'cancellers_joined': True}
                    saved['observer_started'] = False
                    saved['execution_errors'] = list(dict.fromkeys([*saved.get('execution_errors', []), *failures]))
                    technical_control.save(db, self.qualification_id, saved, expected=control)
                joined_saved = True
            return joined
        # Owner claim is immediately followed by one unwind scope, including
        # thread-start failure and BaseException in native preparation.
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            control = technical_control.load(db, self.qualification_id, phase_id, active_only=True)
            if control is None:
                raise Rejected('technical_qualification', 'Resume the existing technical phase through reconcile()')
            saved = copy.deepcopy(control['phase'])
            if saved.get('execution_started') or saved['state'] != 'running':
                raise Rejected('technical_qualification', 'Resume the existing technical phase through reconcile()')
            saved.update(execution_started=True, observer_owner=observer_owner)
            technical_control.save(db, self.qualification_id, saved, expected=control)
            phase.update(execution_started=True, observer_owner=observer_owner)
        try:
            self._observer_callbacks[phase_id] = stop_observer
            fatal = None
            try:
                watcher.start()
                with self.runtime._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    control = technical_control.load(db, self.qualification_id, phase_id, active_only=True)
                    if (control is None or control['phase'].get('observer_owner') != observer_owner
                            or control['phase']['state'] != 'running'):
                        raise Rejected('technical_control', 'The observer lost ownership before native admission')
                    saved = copy.deepcopy(control['phase'])
                    saved['observer_started'] = True
                    technical_control.save(db, self.qualification_id, saved, expected=control)
                for check in bounded['checks']:
                    if failures or _phase_deadline(phase) <= datetime.now(timezone.utc):
                        raise Rejected('technical_qualification', 'Technical phase stopped before the next check')
                    if check['timeout_seconds'] > max_seconds or bounded['max_seconds'] > max_seconds:
                        raise Rejected('technical_qualification', 'Each exact command must fit the original technical phase deadline')
                    result = self.runtime._check(bounded, check, Path(phase['output']) / check['name'],
                                                 'technical-qualification-only', phase['recipe_digest'])
                    phase['checks'].append(result)
            except BaseException as error:
                failures.append(str(error) or type(error).__name__)
                if not isinstance(error, Exception):
                    fatal = error
            result = self.reconcile(phase_id, failures=failures, _stop_observer=stop_observer)
            if fatal is not None:
                raise fatal
            return result
        finally:
            stop_observer()
            self._observer_callbacks.pop(phase_id, None)

    @staticmethod
    def _observer_stopped(phase):
        if not phase.get('execution_started'):
            return True
        owner = phase.get('observer_owner')
        if not isinstance(owner, dict):
            return False
        joined = phase.get('observer_joined', {})
        if (joined.get('owner') == owner and joined.get('watcher_joined') is True
                and joined.get('cancellers_joined') is True):
            return True
        try:
            return not native_processes.same(owner, native_processes.identity(owner['pid']))
        except (OSError, RuntimeError, KeyError, TypeError):
            return False

    def reconcile(self, phase_id, *, failures=(), observer_stopped=True, _stop_observer=None):
        """Retry only exact owned cleanup; this cannot relaunch a check."""
        with self.runtime._db() as db:
            control = technical_control.load(db, self.qualification_id, active_only=True)
            if control and control['phase']['id'] == phase_id:
                db.execute('BEGIN IMMEDIATE')
                phase = copy.deepcopy(control['phase'])
                phase['state'] = 'closing'
                control = technical_control.save(db, self.qualification_id, phase, expected=control)
            else:
                # Legacy execution authority is never reconstructed. Historical
                # reconciliation is cleanup-only and requires the observer gone
                # before changing a large archive.
                db.execute('BEGIN')
                record = _cold_read(db, self.qualification_id)
                phase = next(p for p in record['phases'] if p['id'] == phase_id)
                if record['active_phase'] != phase_id:
                    if phase['state'] == 'stopped':
                        return phase['receipt']
                    raise Rejected('technical_qualification', 'This phase does not own current reconciliation')
                if control or not self._observer_stopped(phase):
                    raise Rejected('technical_qualification', 'Legacy active execution is cleanup-only after its exact observer stops')
                control = None
        local_callback = _stop_observer is not None and self._observer_callbacks.get(phase_id) is _stop_observer
        if _stop_observer is not None and not local_callback:
            raise Rejected('technical_qualification', 'Only the live exact observer owner may attest its thread joins')
        observer_stopped = observer_stopped and self._observer_stopped(phase)
        errors = list(dict.fromkeys([*phase.get('execution_errors', []), *failures]))
        try:
            self._cancel(phase)
        except Exception as error:
            errors.append(str(error))
        rows = self._rows(phase)
        native_stopped = all(row['phase'] == 'clean' and row.get('cleanup', {}).get('stopped')
                      and row.get('cleanup', {}).get('effects_reconciled') for row in rows)
        # Join the competing sampler after owned work is quiescent, retaining
        # its final pending observation before the one final force/finish sample.
        # Join evidence writes only compact control, never historical receipts.
        if _stop_observer is not None:
            for error in errors:
                if error not in failures:
                    failures.append(error)
            observer_stopped = _stop_observer()
            with self.runtime._db() as db:
                control = technical_control.load(db, self.qualification_id, phase_id, active_only=True)
            if control is None:
                raise Rejected('technical_qualification', 'The joined private phase lost its exact control')
            phase = copy.deepcopy(control['phase'])
        with self.runtime._db() as db:
            guard = resource_limits._read(db, phase['resource_id'])
        final_guard = resource_limits.sample_file(str(self.runtime.path), phase['resource_id'], guard['scope'],
                                                  phase_id, force=True, finish=native_stopped and observer_stopped,
                                                  last_observed_at=guard['last_observation']['observed_at'])
        rows = self._rows(phase)
        native_stopped = all(row['phase'] == 'clean' and row.get('cleanup', {}).get('stopped')
                             and row.get('cleanup', {}).get('effects_reconciled') for row in rows)
        with self.runtime._db() as db:
            final_guard = resource_limits.receipt(resource_limits._read(db, phase['resource_id']))
        errors = list(dict.fromkeys([*errors, *failures]))
        stopped = observer_stopped and native_stopped
        if not stopped:
            # Unknown cleanup cannot turn into a cold archival operation while
            # observation or owned work may still be active. Keep the small
            # admission fence, original clocks and unresolved ownership intact.
            receipt = {'format': FORMAT, 'accepted': False, 'kind': 'technical-qualification-only',
                       'qualification_id': self.qualification_id, 'phase_id': phase_id,
                       'status': 'blocked', 'stopped': False, 'source_unchanged': None,
                       'product_budget_unchanged': None, 'model_calls': self.runtime.model_calls,
                       'model_budget_reservations': 0, 'checks': [], 'errors': errors,
                       'resource_limits': final_guard,
                       'native_rows': [{'id': row['check_id'], 'state': row['phase']} for row in rows],
                       'elapsed_seconds': max(0, time.time() - phase['started_at'])}
            if control:
                with self.runtime._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    held = copy.deepcopy(phase)
                    held.update(state='cleanup_unknown', execution_errors=errors,
                                resource_receipt=final_guard, hold_receipt=receipt)
                    technical_control.save(db, self.qualification_id, held, expected=control)
            return receipt
        checks = [row['receipt'] for row in rows if row.get('receipt')]
        for check in checks:
            try:
                self.runtime._verify_check_receipt(check)
            except Exception as error:
                errors.append(str(error))
        planned = {check['name'] for check in phase['recipe']['checks']}
        complete = (len(checks) == len(planned) and {check['name'] for check in checks} == planned
                    and all(check.get('candidate') == phase['recipe_digest']
                            and check.get('executor') == 'technical-qualification-only' for check in checks))
        # Full archival decoding/serialization starts only after the sampler has
        # stopped and the final observation has deactivated this exact guard.
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if control:
                if technical_control.load(db, self.qualification_id, phase_id, active_only=True) != control:
                    raise Rejected('technical_control', 'Private phase control changed before archive reconciliation')
            else:
                _require_idle_control(db, self.qualification_id)
            stopped_guard = resource_limits._read(db, phase['resource_id'])
            if stopped_guard is None or stopped_guard['active_attempt'] is not None:
                raise Rejected('technical_control', 'The resource observer must be inactive before archival reconciliation')
            record = _read(db, self.qualification_id)
            technical_control.check_completed_archive(db, self.qualification_id, record)
            archived_phase = next(p for p in record['phases'] if p['id'] == phase_id)
            if control:
                technical_control.check_archive(control, archived_phase)
                if technical_control.load(db, self.qualification_id, phase_id, active_only=True) != control:
                    raise Rejected('technical_control', 'Private phase control changed before archive reconciliation')
            archive_revision = technical_control.revision(db, self.qualification_id)
        source_unchanged = inventory(Path(phase['recipe']['source']), recipe=phase['recipe']) == record['source_baseline']
        budget_after = _budget_bytes(self.budget_paths)
        budget_unchanged = budget_after == record['budget_before']
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not control:
                _require_idle_control(db, self.qualification_id)
            if technical_control.revision(db, self.qualification_id) != archive_revision:
                raise Rejected('technical_control', 'The historical archive changed before final reconciliation')
            if control and technical_control.load(db, self.qualification_id, phase_id, active_only=True) != control:
                raise Rejected('technical_control', 'Private phase control changed before final reconciliation')
            archived_phase = next(p for p in record['phases'] if p['id'] == phase_id)
            if record['active_phase'] != phase_id:
                raise Rejected('technical_qualification', 'Current phase ownership changed during reconciliation')
            if control:
                archived_phase.update({key: copy.deepcopy(phase[key]) for key in technical_control.MUTABLE if key in phase})
            phase = archived_phase
            phase['resource_receipt'] = final_guard
            if final_guard['breach']:
                _failure(record, final_guard['breach'])
            if not source_unchanged or not budget_unchanged:
                _failure(record, {'reason': 'protected_baseline_changed', 'observed_at': time.time()})
            phase['checks'] = checks
            errors = list(dict.fromkeys([*phase.get('execution_errors', []), *errors]))
            phase['execution_errors'] = errors
            passed = complete and bool(checks) and all(check.get('result') == 'pass' for check in checks)
            receipt = {'format': FORMAT, 'accepted': False, 'kind': 'technical-qualification-only',
                       'qualification_id': self.qualification_id, 'phase_id': phase_id,
                       'status': 'passed' if passed and stopped and not errors and not _blocking(record) else 'blocked',
                       'stopped': stopped, 'source_unchanged': source_unchanged, 'product_budget_unchanged': budget_unchanged,
                       'product_budget_before': record['budget_before'], 'product_budget_after': budget_after,
                       'model_calls': self.runtime.model_calls, 'model_budget_reservations': 0,
                       'checks': phase['checks'], 'errors': errors, 'resource_limits': phase['resource_receipt'],
                       'elapsed_seconds': max(0, time.time() - phase['started_at'])}
            phase.update(receipt=receipt, state='stopped' if stopped else 'cleanup_unknown')
            record['active_phase'] = None if stopped else phase_id
            _save(db, self.qualification_id, record)
            if control:
                technical_control.save(db, self.qualification_id, phase)
        return receipt

    def export(self, recipe, capsule_path, *, work_id):
        """Return exact smaller limits and an immutable capsule for formal binding."""
        path = Path(capsule_path)
        identifier(work_id)
        if path.parent != self.directory or path.exists():
            raise Rejected('technical_qualification', 'A new capsule in this private journal is required')
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = _cold_read(db, self.qualification_id)
            if not record or not record['phases'] or record['contract'] != _contract(recipe):
                raise Rejected('technical_qualification', 'The final accounting contract differs or has no qualification')
            if record.get('export'):
                raise Rejected('technical_qualification', 'The qualification allowance was already exported')
            last = record['phases'][-1]
            if (last['input_digest'] != digest(recipe) or last.get('receipt', {}).get('status') != 'passed'):
                raise Rejected('technical_qualification', 'The exact final recipe needs a complete successful technical phase')
            for check in last['checks']:
                self.runtime._verify_check_receipt(check)
            charges = self._refresh(db, record)
            if inventory(Path(recipe['source']), recipe=recipe) != record['source_baseline']:
                _failure(record, {'reason': 'protected_baseline_changed', 'observed_at': time.time()})
            if _budget_bytes(self.budget_paths) != record['budget_before']:
                _failure(record, {'reason': 'product_budget_changed', 'observed_at': time.time()})
            _save(db, self.qualification_id, record)
            archive_revision = technical_control.revision(db, self.qualification_id)
        if _blocking(record):
            raise Rejected('resource_limit_exceeded', 'Technical resource failure remains recorded')
        final = copy.deepcopy(recipe)
        for group, cap in CAPS.items():
            remaining = record['contract'][cap] - charges[group]
            if remaining <= 0:
                raise Rejected('resource_limit_exceeded', 'No remaining resource allowance for product execution')
            final['resource_limits'][cap] = remaining
        bound = resource_limits.scope(final)
        temporary = resource_limits._temporary_roots(bound)
        baseline = resource_limits.observe({**bound, '_temporary_baseline': temporary})
        baseline['temporary_baseline'] = temporary
        # Refresh once more after the export observation, closing the accounting
        # interval before the saved baseline. Later growth uses that baseline.
        capsule = {'format': 'aos-technical-resource-carry-forward-v1', 'accepted': False,
                   'qualification_id': self.qualification_id, 'target_work_id': work_id,
                   'all_phases_stopped': True, 'breach': None,
                   'scope': bound, 'baseline': baseline, 'charged_growth_bytes': charges,
                   'original_ceilings': {cap: record['contract'][cap] for cap in CAPS.values()},
                   'generated_root_extensions': [item['extension'] for item in record.get('generated_root_extensions', [])],
                   'historical_resource_failure': record['breach'],
                   'quiescent_gap_dispositions': copy.deepcopy(record.get('quiescent_gap_dispositions', [])),
                   'prelaunch_observation_dispositions': copy.deepcopy(record.get('prelaunch_observation_dispositions', [])),
                   'rejected_admission_dispositions': copy.deepcopy(record.get('rejected_admission_dispositions', [])),
                   'intercheck_observation_dispositions': copy.deepcopy(record.get('intercheck_observation_dispositions', [])),
                   'historical_unresolved_failures': copy.deepcopy(record.get('unresolved_failures', [])),
                   'phases': [{'id': p['id'], 'recipe_digest': p['recipe_digest'],
                               'resource_receipt': p['resource_receipt']} for p in record['phases']]}
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_idle_control(db, self.qualification_id)
            if technical_control.revision(db, self.qualification_id) != archive_revision:
                raise Rejected('technical_control', 'The archive revision changed during export')
            latest = _cold_read(db, self.qualification_id)
            refreshed = self._refresh(db, latest)
            if latest.get('export') or latest['phases'][-1]['id'] != record['phases'][-1]['id']:
                raise Rejected('technical_qualification', 'Qualification changed during export')
            _save(db, self.qualification_id, latest)
            archive_revision = technical_control.revision(db, self.qualification_id)
        if _blocking(latest) or refreshed != charges:
            raise Rejected('resource_limits', 'Resource inputs changed during export; retry with newly observed debits')
        raw = canonical(capsule).encode()
        with self.runtime._db() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_idle_control(db, self.qualification_id)
            if technical_control.revision(db, self.qualification_id) != archive_revision:
                raise Rejected('technical_control', 'The archive revision changed before export was sealed')
            if _cold_read(db, self.qualification_id) != latest:
                raise Rejected('technical_qualification', 'Qualification changed before export was sealed')
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            final['resource_limits_carry_forward'] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
            final['native_protected_paths'] = sorted(set(final.get('native_protected_paths', [])) | {str(path), *self.budget_paths})
            resource_limits._carry_forward(final, bound)
            latest['export'] = {'target_work_id': work_id, 'capsule': final['resource_limits_carry_forward']}
            _save(db, self.qualification_id, latest)
        return {'accepted': False, 'kind': 'technical-resource-carry-forward', 'recipe': final,
                'charged_growth_bytes': charges, 'capsule': final['resource_limits_carry_forward']}
