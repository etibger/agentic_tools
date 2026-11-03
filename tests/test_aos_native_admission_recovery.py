"""Rejected admission recovery proves no work started without forgiving limits."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_admission_recovery as recovery
from agentic_tools.aos import native_processes, resource_limits, technical_control
from agentic_tools.aos import native_qualification
from agentic_tools.aos.store import Rejected, canonical, digest
from agentic_tools.check_adapters import identities, select


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _artifact(path, value):
    path.write_text(canonical(value))
    return {'path': str(path), 'sha256': _sha(path.read_bytes())}


def _sync(case, *, packet=True):
    """Persist deliberate fixture changes, optionally leaving reviewed evidence stale."""
    with case.q.runtime._db() as db:
        db.execute('DELETE FROM native_check')
        for row in case.rows:
            db.execute('INSERT INTO native_check VALUES(?,?,?)',
                       (row['check_id'], row.get('attempt_id'), canonical(row)))
        db.execute('INSERT OR REPLACE INTO resource_limit VALUES(?,?)',
                   (case.phase['resource_id'], canonical(case.guard)))
    if packet:
        case.packet.update(
            qualification_digest=digest(case.record), phase_digest=digest(case.phase),
            control_digest=digest(case.control) if case.control is not None else None,
            resource_guard_raw_sha256=_sha(canonical(case.guard).encode()),
            resource_guard_digest=digest(case.guard),
            guard_baseline_digest=digest(case.guard['baseline']),
            guard_scope_digest=case.guard['scope_digest'],
            source_baseline_digest=digest(case.record['source_baseline']),
            budget_before=copy.deepcopy(case.record['budget_before']),
            prior_charged_growth_bytes=copy.deepcopy(case.record['charged_growth_bytes']),
            native_rows=[{'id': row['check_id'], 'raw_record_sha256': _sha(canonical(row).encode())}
                         for row in case.rows])
    case.packet_path.write_text(canonical(case.packet))
    case.packet_hash = _sha(case.packet_path.read_bytes())


def _validate(case):
    return recovery.validate(case.q, case.record, case.phase, case.guard,
                             recovery.evidence(case.packet_path, case.packet_hash), control=case.control)


@pytest.fixture
def admission_case(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / 'private-qualification'
    directory.mkdir(mode=0o700)
    (directory / 'native-status').mkdir()
    source, outputs = (tmp_path.resolve() / name for name in ('source', 'outputs'))
    source.mkdir()
    outputs.mkdir()
    budget = tmp_path.resolve() / 'budget-proof'
    budget.write_bytes(b'preserved original charged reservations')
    database = directory / 'runtime.sqlite3'

    @contextmanager
    def db():
        connection = sqlite3.connect(database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with db() as connection:
        connection.execute('CREATE TABLE native_check(id TEXT PRIMARY KEY, attempt_id TEXT, record TEXT)')
        connection.execute('CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY, record TEXT)')
    q = SimpleNamespace(qualification_id='fixture-qualification', directory=directory,
                        runtime=SimpleNamespace(path=database, _db=db), budget_paths=(str(budget),))
    scope = {'output_roots': [str(outputs)], 'source_evidence_roots': [str(source)],
             'generated_roots': [], 'temporary_paths': [], 'max_output_growth_bytes': 4096,
             'max_source_evidence_growth_bytes': 2048, 'min_free_bytes': 10,
             'sample_interval_seconds': 2}
    check = {'name': 'first', 'argv': ['/fixture/pytest', 'tests'], 'timeout_seconds': 30,
             'result_kind': 'pytest', 'adapter_id': 'pytest-v1'}
    recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': 30,
              'checks': [check], '_adapter_bindings': identities(),
              'resource_limits': {key: value for key, value in scope.items()
                                  if key not in ('generated_roots', 'temporary_paths')}}
    owner = {'pid': 47291, 'uid': os.getuid(), 'birth': [101, 321], 'ppid': 1, 'pgid': 47291}
    primary = {'reason': 'active_observation_gap', 'observed_at': 150.0}
    unavailable = {'reason': 'resource_observation_unavailable', 'observed_at': 151.0}
    guard = {'scope': scope, 'scope_digest': digest(scope), 'baseline': {'groups': {'output_roots': {},
               'source_evidence_roots': {}}, 'identities': {}, 'available_bytes': {'0': 10000}, 'observed_at': 100.0},
             'high_water_growth_bytes': {'output_roots': 31, 'source_evidence_roots': 17},
             'active_attempt': None, 'breach': primary,
             'last_observation': {'available_bytes': {'0': 10000}, 'observed_at': 152.0}}
    phase = {'id': 'rejected-admission', 'state': 'stopped', 'recipe': recipe,
             'recipe_digest': digest(recipe), 'input_digest': digest(recipe), 'max_seconds': 30,
             'resource_id': 'resource-rejected-admission',
             'started_at': 100.0, 'deadline': 180.0, 'started_monotonic': 100.0,
             'deadline_monotonic': 180.0, 'output': str(outputs / 'technical-rejected-admission'), 'checks': [],
             'execution_started': True, 'observer_owner': owner,
             'observer_joined': {'owner': copy.deepcopy(owner), 'observed_at': 160.0,
                                 'watcher_joined': True, 'cancellers_joined': True},
             'execution_errors': ['native_ownership: A live or unresolved native check owns an overlapping path'],
             'resource_receipt': resource_limits.receipt(guard)}
    phase['receipt'] = {'phase_id': phase['id'], 'status': 'blocked', 'stopped': True,
                        'accepted': False, 'model_calls': 0, 'source_unchanged': True,
                        'product_budget_unchanged': True, 'checks': [],
                        'resource_limits': copy.deepcopy(phase['resource_receipt'])}
    record = {'qualification_id': q.qualification_id, 'active_phase': None, 'export': None,
              'phases': [phase], 'breach': primary, 'unresolved_failures': [unavailable],
              'contract': copy.deepcopy(scope), 'budget_paths': [str(budget)],
              'budget_before': {str(budget): _sha(budget.read_bytes())},
              'source_baseline': {'input.txt': {'sha256': 'a' * 64, 'size': 17}},
              'charged_growth_bytes': {'output_roots': 7, 'source_evidence_roots': 11},
              'quiescent_gap_dispositions': [], 'prelaunch_observation_dispositions': []}
    historical = {'check_id': 'nc-prior-clean', 'attempt_id': None, 'phase': 'clean',
                  'output': str(directory / 'prior-phase' / 'check'), 'candidate': 'sha256:' + 'b' * 64,
                  'cleanup': {'stopped': True, 'effects_reconciled': True}}
    sidecar = {'work_id': phase['resource_id'], 'scope_digest': guard['scope_digest'],
               'breach': unavailable,
               'high_water_growth_bytes': {'output_roots': 29, 'source_evidence_roots': 27}}
    sidecar_path = resource_limits._failure_path(database, phase['resource_id'])
    sidecar_pin = _artifact(sidecar_path, sidecar)
    sidecar_path.chmod(0o600)
    source_map = dict(recovery.LEGACY_PRE_INTENT_SOURCES)
    source_digest = digest(source_map)
    manifest = _artifact(directory / 'historical-source.json',
                         {'files': {'src/agentic_tools/' + path: sha for path, sha in source_map.items()}})
    verification = _artifact(directory / 'historical-verification.json',
                             {'source_digest': source_digest, 'source_unchanged': True, 'pytest': {'exit_code': 0}})
    packet = {'format': 'aos-rejected-admission-observation-forensics-v1', 'read_only': True,
              'qualification_id': q.qualification_id, 'phase_id': phase['id'],
              'execution_source': {'source_digest': source_digest, 'manifest': manifest,
                                   'verification': verification, 'semantics': 'native-ownership-before-intent-v1'},
              'admission_failure': {'check': check['name'], 'error': 'native_ownership'},
              'prior_dispositions': [], 'failure_sidecar': sidecar_pin | {'content': copy.deepcopy(sidecar)}}
    case = SimpleNamespace(q=q, directory=directory, database=database, source=source, outputs=outputs,
                           budget=budget, record=record, phase=phase, guard=guard, rows=[historical],
                           control=None, packet=packet, packet_path=directory / 'reviewed-evidence.json',
                           sidecar=sidecar, sidecar_path=sidecar_path, owner=owner)
    monkeypatch.setattr(native_processes, 'identity', lambda pid: None)
    _sync(case)
    return case


def test_exact_zero_row_rejection_keeps_failures_and_accounting(admission_case):
    c = admission_case
    before = copy.deepcopy((c.record, c.phase, c.guard, c.packet))
    database, budget, sidecar = (path.read_bytes() for path in (c.database, c.budget, c.sidecar_path))
    proof = _validate(c)
    assert proof['kind'] == 'rejected-admission-observation'
    assert proof['native_rows'] == {}
    assert proof['historical_native_rows'] == {
        row['check_id']: _sha(canonical(row).encode()) for row in c.rows}
    assert proof['historical_breaches'] == [c.guard['breach'], c.sidecar['breach']]
    assert proof['minimum_high_water_growth_bytes'] == {'output_roots': 31, 'source_evidence_roots': 27}
    assert proof['original_guard_digest'] == digest(c.guard)
    assert proof['historical_phase_digest'] == digest(c.phase)
    assert proof['scope_digest'] == c.guard['scope_digest']
    assert (c.record, c.phase, c.guard, c.packet) == before
    assert [path.read_bytes() for path in (c.database, c.budget, c.sidecar_path)] == [database, budget, sidecar]


@pytest.mark.parametrize('damage', ['hash', 'json', 'format', 'read-only', 'symlink', 'hardlink', 'parent-alias'])
def test_packet_requires_exact_owned_regular_reviewed_bytes(admission_case, damage):
    c = admission_case
    path, sha = c.packet_path, c.packet_hash
    if damage == 'hash':
        sha = '0' * 64
    elif damage == 'json':
        path.write_text('{')
        sha = _sha(path.read_bytes())
    elif damage in ('format', 'read-only'):
        c.packet['format' if damage == 'format' else 'read_only'] = 'unrelated' if damage == 'format' else False
        _sync(c)
        sha = c.packet_hash
    elif damage == 'symlink':
        alias = c.directory / 'packet-alias.json'
        alias.symlink_to(path)
        path = alias
    elif damage == 'hardlink':
        os.link(path, c.directory / 'packet-hardlink.json')
    else:
        alias = c.directory.parent / 'directory-alias'
        alias.symlink_to(c.directory, target_is_directory=True)
        path = alias / path.name
    with pytest.raises(Rejected):
        recovery.evidence(path, sha)


@pytest.mark.parametrize('field', ['qualification_id', 'phase_id', 'qualification_digest', 'phase_digest', 'control_digest',
    'resource_guard_raw_sha256', 'resource_guard_digest', 'guard_baseline_digest', 'guard_scope_digest',
    'source_baseline_digest', 'budget_before', 'prior_charged_growth_bytes', 'prior_dispositions'])
def test_each_reviewed_binding_rejects_stale_evidence(admission_case, field):
    c = admission_case
    c.packet[field] = 'tampered'
    _sync(c, packet=False)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['active', 'unstopped', 'checks', 'receipt-checks', 'model-calls',
    'source', 'budget', 'success', 'error-missing', 'error-other', 'wrong-check', 'wrong-recipe',
    'empty-phases', 'empty-plan', 'boolean-model-count'])
def test_zero_rows_alone_never_prove_rejected_admission(admission_case, damage):
    c = admission_case
    if damage == 'active':
        c.record['active_phase'] = c.phase['id']
    elif damage == 'unstopped':
        c.phase['state'] = 'closing'
    elif damage in ('checks', 'receipt-checks'):
        target = c.phase if damage == 'checks' else c.phase['receipt']
        target['checks'].append({'result': 'pass'})
    elif damage == 'model-calls':
        c.phase['receipt']['model_calls'] = 1
    elif damage in ('source', 'budget'):
        c.phase['receipt']['source_unchanged' if damage == 'source' else 'product_budget_unchanged'] = False
    elif damage == 'success':
        c.phase['receipt']['status'] = 'passed'
    elif damage in ('error-missing', 'error-other'):
        c.phase['execution_errors'] = [] if damage == 'error-missing' else ['native_ownership: unknown error']
    elif damage == 'wrong-check':
        c.packet['admission_failure']['check'] = 'later'
    elif damage == 'empty-phases':
        c.record['phases'] = []
    elif damage == 'empty-plan':
        c.phase['recipe']['checks'] = []
        c.phase['recipe_digest'] = digest(c.phase['recipe'])
    elif damage == 'boolean-model-count':
        c.phase['receipt']['model_calls'] = False
    else:
        c.phase['recipe']['checks'][0]['argv'].append('changed')
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing-owner', 'missing-join', 'wrong-join-owner', 'watcher-live',
    'canceller-live', 'owner-live', 'lookup-failed', 'malformed-current', 'wrong-pid'])
def test_both_positive_join_and_exact_owner_absence_are_required(admission_case, monkeypatch, damage):
    c = admission_case
    if damage == 'missing-owner':
        c.phase.pop('observer_owner')
    elif damage == 'missing-join':
        c.phase.pop('observer_joined')
    elif damage == 'wrong-join-owner':
        c.phase['observer_joined']['owner']['birth'][1] += 1
    elif damage in ('watcher-live', 'canceller-live'):
        c.phase['observer_joined']['watcher_joined' if damage == 'watcher-live' else 'cancellers_joined'] = False
    elif damage == 'owner-live':
        monkeypatch.setattr(native_processes, 'identity', lambda pid: copy.deepcopy(c.owner))
    elif damage == 'malformed-current':
        monkeypatch.setattr(native_processes, 'identity', lambda pid: {'pid': pid})
    elif damage == 'wrong-pid':
        monkeypatch.setattr(native_processes, 'identity', lambda pid: {**c.owner, 'pid': pid + 1})
    else:
        def unavailable(pid):
            raise PermissionError('no trustworthy process observation')
        monkeypatch.setattr(native_processes, 'identity', unavailable)
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


def test_reused_pid_with_distinct_birth_does_not_adopt_or_signal_foreign_owner(admission_case, monkeypatch):
    c = admission_case
    foreign = copy.deepcopy(c.owner)
    foreign['birth'][1] += 1
    monkeypatch.setattr(native_processes, 'identity', lambda pid: foreign)
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('Recovery validator must not signal a process'))
    assert _validate(c)['native_rows'] == {}


@pytest.mark.parametrize('damage', [None, 'active', 'foreign', 'binding', 'recipe', 'join'])
def test_optional_compact_control_must_match_the_stopped_archive(admission_case, damage):
    c = admission_case
    c.control = {'format': technical_control.FORMAT, 'qualification_id': c.q.qualification_id,
                 'active': False, 'archive_binding': technical_control.binding(c.phase),
                 'phase': {key: copy.deepcopy(c.phase.get(key))
                           for key in (*technical_control.IMMUTABLE, *technical_control.MUTABLE)}}
    if damage == 'active':
        c.control['active'] = True
    elif damage == 'foreign':
        c.control['qualification_id'] = 'other-qualification'
    elif damage == 'binding':
        c.control['archive_binding'] = 'sha256:' + '0' * 64
    elif damage == 'recipe':
        c.control['phase']['recipe']['checks'][0]['argv'].append('another-command')
    elif damage == 'join':
        c.control['phase']['observer_joined']['watcher_joined'] = False
    _sync(c)
    if damage is None:
        assert _validate(c)['control_digest'] == digest(c.control)
    else:
        with pytest.raises(Rejected):
            _validate(c)


def test_prior_disposition_and_its_independent_guard_remain_intact(admission_case):
    c = admission_case
    previous_guard = copy.deepcopy(c.guard)
    previous_guard['breach'] = None
    previous_receipt = resource_limits.receipt(previous_guard)
    disposition = {'operation_id': 'old-ack', 'phase_id': 'previous-phase',
                   'historical_breach': {'reason': 'active_observation_gap', 'observed_at': 70.0},
                   'resource_id': 'old-passive-continuation', 'resource_receipt': previous_receipt,
                   'acknowledgment': {'accepted': False, 'grants_work': False, 'grants_time': False}}
    c.record['quiescent_gap_dispositions'] = [disposition]
    c.packet['prior_dispositions'] = [{key: copy.deepcopy(disposition[key])
                                       for key in ('operation_id', 'phase_id', 'resource_id', 'resource_receipt')} |
                                      {'acknowledgment_digest': digest(disposition['acknowledgment'])}]
    with c.q.runtime._db() as db:
        db.execute('INSERT INTO resource_limit VALUES(?,?)',
                   (disposition['resource_id'], canonical(previous_guard)))
    _sync(c)
    before = copy.deepcopy(disposition)
    proof = _validate(c)
    assert c.record['quiescent_gap_dispositions'] == [before]
    assert proof['prior_charged_growth_bytes'] == {'output_roots': 7, 'source_evidence_roots': 11}
    previous_guard['breach'] = {'reason': 'output_growth_exceeded', 'observed_at': 171.0}
    with c.q.runtime._db() as db:
        db.execute('UPDATE resource_limit SET record=? WHERE work_id=?',
                   (canonical(previous_guard), disposition['resource_id']))
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['held', 'unstopped', 'unreconciled', 'omitted', 'added', 'duplicate', 'phase-row'])
def test_every_historical_row_is_bound_and_phase_has_no_native_row(admission_case, damage):
    c = admission_case
    if damage == 'held':
        c.rows[0]['phase'] = 'unknown'
    elif damage in ('unstopped', 'unreconciled'):
        c.rows[0]['cleanup']['stopped' if damage == 'unstopped' else 'effects_reconciled'] = False
    elif damage == 'phase-row':
        c.rows[0]['output'] = str(Path(c.phase['output']) / 'first')
        c.rows[0]['candidate'] = c.phase['recipe_digest']
    elif damage == 'omitted':
        c.packet['native_rows'] = []
    elif damage == 'duplicate':
        c.packet['native_rows'].append(copy.deepcopy(c.packet['native_rows'][0]))
    else:
        c.rows.append({**copy.deepcopy(c.rows[0]), 'check_id': 'nc-added-after-review'})
    _sync(c, packet=damage not in ('omitted', 'added', 'duplicate'))
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('artifact', ['output', 'profile', 'status', 'release', 'cancel'])
def test_any_expected_launch_artifact_blocks_no_launch_proof(admission_case, artifact):
    c = admission_case
    check = c.phase['recipe']['checks'][0]
    output = Path(c.phase['output']) / check['name']
    command = select(check).command(check['argv'], output)
    check_id = 'nc-' + digest([str(output.resolve()), command, c.phase['recipe_digest']])[7:39]
    if artifact == 'output':
        output.mkdir(parents=True)
    else:
        suffix = {'profile': '.sb', 'status': '.json', 'release': '.release', 'cancel': '.cancel'}[artifact]
        (c.directory / 'native-status' / (check_id + suffix)).write_text('unexpected launch artifact')
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing', 'changed', 'content', 'mode', 'scope', 'work', 'wrong-reason',
                                   'same-breach', 'high-water-cap'])
def test_distinct_exact_failure_sidecar_is_required(admission_case, damage):
    c = admission_case
    if damage == 'missing':
        c.sidecar_path.unlink()
    elif damage == 'changed':
        c.sidecar_path.write_text('{}')
    elif damage == 'content':
        c.packet['failure_sidecar']['content'] = {}
    elif damage == 'mode':
        c.sidecar_path.chmod(0o644)
    else:
        if damage == 'scope':
            c.sidecar['scope_digest'] = digest({'other': True})
        elif damage == 'work':
            c.sidecar['work_id'] = 'another-phase'
        elif damage == 'wrong-reason':
            c.sidecar['breach']['reason'] = 'output_growth_exceeded'
        elif damage == 'same-breach':
            c.sidecar['breach'] = copy.deepcopy(c.guard['breach'])
        else:
            c.sidecar['high_water_growth_bytes']['output_roots'] = 4097
        c.packet['failure_sidecar'] = _artifact(c.sidecar_path, c.sidecar) | {'content': copy.deepcopy(c.sidecar)}
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['primary-reason', 'another-failure', 'output-cap', 'source-cap', 'free-floor'])
def test_resource_overruns_or_unrelated_failures_cannot_be_disposed(admission_case, damage):
    c = admission_case
    if damage == 'primary-reason':
        c.guard['breach']['reason'] = 'observation_failed'
    elif damage == 'another-failure':
        c.record['unresolved_failures'].append({'reason': 'other', 'observed_at': 170.0})
    elif damage in ('output-cap', 'source-cap'):
        c.guard['high_water_growth_bytes']['output_roots' if damage == 'output-cap' else 'source_evidence_roots'] = 9999
    else:
        c.guard['last_observation']['available_bytes'] = {'0': 9}
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing-manifest', 'changed-manifest', 'unsupported-source',
                                   'failed-tests', 'boolean-exit-code', 'source-mismatch', 'semantics'])
def test_absence_claim_is_tied_to_supported_historical_execution_source(admission_case, damage):
    c = admission_case
    execution = c.packet['execution_source']
    manifest = Path(execution['manifest']['path'])
    verification = Path(execution['verification']['path'])
    if damage == 'missing-manifest':
        manifest.unlink()
    elif damage == 'changed-manifest':
        manifest.write_text('{}')
    elif damage == 'unsupported-source':
        data = json.loads(manifest.read_text())
        data['files'][next(iter(data['files']))] = '0' * 64
        execution['manifest'] = _artifact(manifest, data)
        execution['source_digest'] = digest({key.removeprefix('src/agentic_tools/'): value
                                             for key, value in data['files'].items()})
        data = json.loads(verification.read_text())
        data['source_digest'] = execution['source_digest']
        execution['verification'] = _artifact(verification, data)
    elif damage in ('failed-tests', 'boolean-exit-code', 'source-mismatch'):
        data = json.loads(verification.read_text())
        if damage == 'failed-tests':
            data['pytest']['exit_code'] = 1
        elif damage == 'boolean-exit-code':
            data['pytest']['exit_code'] = False
        else:
            data['source_digest'] = 'sha256:' + 'f' * 64
        execution['verification'] = _artifact(verification, data)
    else:
        execution['semantics'] = 'absence-alone'
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.fixture
def admission_api(admission_case, monkeypatch):
    """Exercise the public disposition API without a phase runner or watcher."""
    c = admission_case
    q = object.__new__(native_qualification.TechnicalQualification)
    q.__dict__.update(c.q.__dict__)
    c.q = q
    c.guard['baseline']['files'] = c.guard['baseline'].pop('groups')
    c.guard['scope']['max_observation_gap_seconds'] = 10
    c.guard['scope_digest'] = digest(c.guard['scope'])
    c.phase['resource_receipt'] = resource_limits.receipt(c.guard)
    c.phase['receipt'].update(elapsed_seconds=80,
                              resource_limits=copy.deepcopy(c.phase['resource_receipt']))
    c.sidecar['scope_digest'] = c.guard['scope_digest']
    c.packet['failure_sidecar'] = _artifact(c.sidecar_path, c.sidecar) | {'content': copy.deepcopy(c.sidecar)}
    old_guard = copy.deepcopy(c.guard)
    old_guard['high_water_growth_bytes'] = {'output_roots': 7, 'source_evidence_roots': 11}
    old_guard['breach'] = {'reason': 'active_observation_gap', 'observed_at': 50.0}
    old_receipt = resource_limits.receipt(old_guard)
    old_phase = {'id': 'older-phase', 'state': 'stopped', 'resource_id': 'older-resource',
                 'resource_receipt': old_receipt,
                 'receipt': {'status': 'blocked', 'stopped': True, 'model_calls': 0,
                             'source_unchanged': True, 'product_budget_unchanged': True}}
    c.record['phases'].insert(0, old_phase)
    old_continuation = copy.deepcopy(old_guard)
    old_continuation['breach'] = None
    prior = {'operation_id': 'older-review', 'phase_id': old_phase['id'],
             'historical_breach': copy.deepcopy(old_guard['breach']),
             'resource_id': 'older-passive', 'resource_receipt': resource_limits.receipt(old_continuation),
             'acknowledgment': {'accepted': False, 'grants_work': False, 'grants_time': False}}
    c.record['quiescent_gap_dispositions'] = [prior]
    c.packet['prior_dispositions'] = [{key: copy.deepcopy(prior[key]) for key in
                                       ('operation_id', 'phase_id', 'resource_id', 'resource_receipt')} |
                                      {'acknowledgment_digest': digest(prior['acknowledgment'])}]
    c.record['charged_growth_bytes'] = {'output_roots': 38, 'source_evidence_roots': 28}
    with c.q.runtime._db() as db:
        db.execute('CREATE TABLE technical_qualification (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        technical_control.initialize(db)
        native_qualification._save(db, q.qualification_id, c.record)
        resource_limits._save(db, old_phase['resource_id'], old_guard)
        resource_limits._save(db, prior['resource_id'], old_continuation)
    c.observation = {'files': {'output_roots': {}, 'source_evidence_roots': {}}, 'identities': {},
                     'available_bytes': {'0': 10000}, 'observed_at': time.time()}
    c.observations = []

    def observe(bound):
        c.observations.append(copy.deepcopy(bound))
        return copy.deepcopy(c.observation)

    monkeypatch.setattr(resource_limits, 'observe', observe)
    monkeypatch.setattr(native_qualification, 'inventory',
                        lambda *args, **kwargs: copy.deepcopy(c.record['source_baseline']))
    _sync(c)
    return c


def _ack(case, **kwargs):
    return case.q.acknowledge_rejected_admission_observation(
        'review-rejected-admission', case.phase['id'], evidence_path=case.packet_path,
        evidence_sha256=case.packet_hash, reason=kwargs.pop('reason', 'Exact rejected-admission evidence reviewed'),
        **kwargs)


def _api_snapshot(case):
    with case.q.runtime._db() as db:
        record = native_qualification._read(db, case.q.qualification_id)
        guards = dict(db.execute('SELECT work_id,record FROM resource_limit'))
        rows = dict(db.execute('SELECT id,record FROM native_check'))
    return record, guards, rows, case.sidecar_path.read_bytes(), case.budget.read_bytes()


def test_public_disposition_preserves_history_and_carries_each_failure_once(admission_api):
    c = admission_api
    before, original_guards, original_rows, sidecar, budget = _api_snapshot(c)
    ack = _ack(c)
    after, guards, rows, after_sidecar, after_budget = _api_snapshot(c)
    assert ack['accepted'] is False and ack['grants_work'] is False and ack['grants_time'] is False
    assert ack['model_budget_reservations'] == 0
    assert ack['original_started_at'] == c.phase['started_at']
    assert ack['original_deadline'] == c.phase['deadline']
    assert ack['original_elapsed_seconds'] == 80
    assert after['phases'] == before['phases']
    assert after['breach'] == before['breach']
    assert after['unresolved_failures'] == before['unresolved_failures']
    assert after['quiescent_gap_dispositions'] == before['quiescent_gap_dispositions']
    assert after['source_baseline'] == before['source_baseline']
    assert after['budget_before'] == before['budget_before']
    assert rows == original_rows and after_sidecar == sidecar and after_budget == budget
    assert {key: guards[key] for key in original_guards} == original_guards
    disposition, = after['rejected_admission_dispositions']
    continuation = json.loads(guards[disposition['resource_id']])
    assert continuation['baseline'] == c.guard['baseline']
    assert continuation['scope'] == c.guard['scope']
    assert continuation['active_attempt'] is None and continuation['breach'] is None
    assert continuation['high_water_growth_bytes'] == {'output_roots': 31, 'source_evidence_roots': 27}
    assert continuation['historical_breach_reference']['breaches'] == [c.guard['breach'], c.sidecar['breach']]
    assert after['charged_growth_bytes'] == {'output_roots': 38, 'source_evidence_roots': 38}
    assert native_qualification._acknowledged(after, c.guard['breach'], c.phase['id'])
    assert native_qualification._acknowledged(after, c.sidecar['breach'], c.phase['id'])
    assert not native_qualification._blocking(after)
    assert len(c.observations) == 1
    assert _ack(c) == ack
    assert _api_snapshot(c) == (after, guards, rows, after_sidecar, after_budget)
    assert len(c.observations) == 1
    with pytest.raises(Rejected, match='cannot change'):
        _ack(c, reason='Changed review meaning')


def test_public_disposition_and_later_passive_refresh_never_refund_conservative_charges(admission_api):
    c = admission_api
    c.record['charged_growth_bytes'] = {'output_roots': 999, 'source_evidence_roots': 888}
    with c.q.runtime._db() as db:
        native_qualification._save(db, c.q.qualification_id, c.record)
    _sync(c)
    _ack(c)
    saved = _api_snapshot(c)[0]
    assert saved['charged_growth_bytes'] == {'output_roots': 999, 'source_evidence_roots': 888}
    with c.q.runtime._db() as db:
        charges = c.q._refresh(db, saved)
    assert charges == {'output_roots': 999, 'source_evidence_roots': 888}
    assert saved['charged_growth_bytes'] == charges


@pytest.mark.parametrize('failure', ['observe', 'free-floor', 'growth'])
def test_fresh_passive_failure_is_retained_and_does_not_acknowledge_history(admission_api, monkeypatch, failure):
    c = admission_api
    before, old_guards, old_rows, old_sidecar, old_budget = _api_snapshot(c)
    if failure == 'observe':
        def failed(bound):
            raise OSError('fixture observation failed')
        monkeypatch.setattr(resource_limits, 'observe', failed)
    elif failure == 'free-floor':
        c.observation['available_bytes'] = {'0': 9}
    else:
        c.observation['files']['output_roots'] = {str(c.outputs / 'grown'): 4097}
    with pytest.raises(Rejected, match='Fresh prospective observation failed'):
        _ack(c)
    saved, guards, rows, sidecar, budget = _api_snapshot(c)
    assert saved['phases'] == before['phases']
    assert saved['charged_growth_bytes'] == before['charged_growth_bytes']
    assert saved['breach'] == before['breach']
    assert not saved.get('rejected_admission_dispositions')
    assert len(saved['rejected_admission_failed_dispositions']) == 1
    assert not native_qualification._acknowledged(saved, c.guard['breach'], c.phase['id'])
    assert not native_qualification._acknowledged(saved, c.sidecar['breach'], c.phase['id'])
    assert native_qualification._blocking(saved)
    assert {key: guards[key] for key in old_guards} == old_guards
    assert rows == old_rows and sidecar == old_sidecar and budget == old_budget
    continuation = [json.loads(value) for key, value in guards.items() if key not in old_guards]
    assert len(continuation) == 1 and continuation[0]['breach'] is not None
    assert continuation[0]['active_attempt'] is None
    assert continuation[0]['baseline'] == c.guard['baseline']
    assert all(continuation[0]['high_water_growth_bytes'][group] >= floor
               for group, floor in {'output_roots': 31, 'source_evidence_roots': 27}.items())


@pytest.mark.parametrize('damage', ['record', 'guard', 'sidecar', 'row', 'extra-row', 'artifact', 'control'])
def test_atomic_recheck_rejects_drift_after_cold_validation(admission_api, monkeypatch, damage):
    c = admission_api
    mutated = False

    def inventory_with_drift(*args, **kwargs):
        nonlocal mutated
        assert not mutated
        mutated = True
        with c.q.runtime._db() as db:
            if damage == 'record':
                changed = copy.deepcopy(c.record)
                changed['charged_growth_bytes']['output_roots'] += 1
                native_qualification._save(db, c.q.qualification_id, changed)
            elif damage == 'guard':
                changed = copy.deepcopy(c.guard)
                changed['high_water_growth_bytes']['output_roots'] += 1
                resource_limits._save(db, c.phase['resource_id'], changed)
            elif damage in ('row', 'extra-row'):
                changed = copy.deepcopy(c.rows[0])
                if damage == 'row':
                    changed['cleanup']['effects_reconciled'] = False
                    db.execute('UPDATE native_check SET record=? WHERE id=?',
                               (canonical(changed), changed['check_id']))
                else:
                    changed['check_id'] = 'nc-late-row'
                    db.execute('INSERT INTO native_check VALUES(?,?,?)',
                               (changed['check_id'], None, canonical(changed)))
            elif damage == 'control':
                db.execute('BEGIN IMMEDIATE')
                technical_control.save(db, c.q.qualification_id, c.phase)
        if damage == 'sidecar':
            changed = copy.deepcopy(c.sidecar)
            changed['high_water_growth_bytes']['output_roots'] += 1
            c.sidecar_path.write_text(canonical(changed))
        elif damage == 'artifact':
            Path(c.phase['output']).mkdir()
        return copy.deepcopy(c.record['source_baseline'])

    monkeypatch.setattr(native_qualification, 'inventory', inventory_with_drift)
    with pytest.raises(Rejected):
        _ack(c)
    saved, guards, _, _, _ = _api_snapshot(c)
    assert mutated
    assert not saved.get('rejected_admission_dispositions')
    assert not saved.get('rejected_admission_failed_dispositions')
    assert not any(key.startswith('technical-continuation-') for key in guards)
    assert c.observations == []


@pytest.mark.parametrize('damage', ['source', 'budget'])
def test_live_source_and_budget_baseline_must_match_before_disposition(admission_api, monkeypatch, damage):
    c = admission_api
    if damage == 'source':
        monkeypatch.setattr(native_qualification, 'inventory', lambda *args, **kwargs: {'changed': True})
    else:
        c.budget.write_bytes(b'changed reserved charges')
    before = _api_snapshot(c)
    with pytest.raises(Rejected, match='Protected source or product budget'):
        _ack(c)
    assert _api_snapshot(c) == before
    assert c.observations == []
