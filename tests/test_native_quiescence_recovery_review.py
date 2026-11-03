"""Isolated retained-evidence tests; synthetic receipts launch no workloads."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_paths, native_preparation, native_quiescence, resource_limits
from agentic_tools.aos.native_qualification import _blocking, _read, _save
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_native_qualification import technical


def _file(path):
    path = Path(path)
    if not path.exists():
        return {'path': str(path), 'exists': False}
    info = path.stat()
    value = {'path': str(path), 'bytes': info.st_size, 'mtime': info.st_mtime,
             'device': info.st_dev, 'inode': info.st_ino}
    if path.is_file():
        value['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def _record(q):
    with q.runtime._db() as db:
        return _read(db, q.qualification_id)


def _guard(q, resource_id):
    with q.runtime._db() as db:
        return resource_limits._read(db, resource_id)


def _write_packet(case):
    case.packet_path.write_text(canonical(case.packet))
    case.packet_hash = hashlib.sha256(case.packet_path.read_bytes()).hexdigest()


def _ack(case, **kwargs):
    return case.q.acknowledge_quiescent_gap(
        'review-idle-gap', 'historical', evidence_path=case.packet_path,
        evidence_sha256=case.packet_hash, reason='Exact isolated no-workload interval reviewed', **kwargs)


@pytest.fixture
def gap_case(technical):
    p, q, budget = technical
    phase = q.begin('historical', p.recipe)
    growth = p.outputs / 'controller-evidence'
    growth.write_bytes(b'x' * 7)
    q.reconcile('historical')
    record = _record(q)
    phase = record['phases'][0]
    guard = _guard(q, phase['resource_id'])
    breach = {'reason': 'active_observation_gap', 'observed_at': time.time()}
    guard['breach'] = copy.deepcopy(breach)
    phase['resource_receipt'] = resource_limits.receipt(guard)
    phase['receipt']['resource_limits'] = copy.deepcopy(phase['resource_receipt'])
    record['breach'] = copy.deepcopy(breach)
    source = copy.deepcopy(record['source_baseline'])
    status = q.directory / 'synthetic-completed-status.json'
    sample = resource_limits.receipt(guard)
    sample.update(breach=None, observed_at=breach['observed_at'] - 1)
    status.write_text(canonical({'census': {'boundary_verified': True, 'reconciled': True,
                                          'remaining': [], 'errors': []},
                                 'resource_guard': sample, 'exit_code': 0}))
    transcript = Path(phase['output']) / 'completed' / 'output.txt'
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'')
    anchor = {'check_id': 'nc-completed-fixture', 'attempt_id': None, 'phase': 'clean',
              'candidate': phase['recipe_digest'], 'output': str(transcript.parent), 'status': str(status),
              'cleanup': {'stopped': True, 'effects_reconciled': True,
                          'native_path_effects': {'after': {}}, 'source_after': source},
              'receipt': {'output': str(transcript), 'output_sha256': hashlib.sha256(b'').hexdigest()}}
    unstarted = Path(phase['output']) / 'not-started'
    unstarted.mkdir()
    row = {'check_id': 'nc-no-launch-fixture', 'attempt_id': None, 'phase': 'clean',
           'source': str(p.source), 'outputs_root': str(p.outputs), 'output': str(unstarted),
           'status': str(q.directory / 'never-created-status.json'),
           'release': str(q.directory / 'never-created-release'),
           'cancel_path': str(q.directory / 'never-created-cancel'),
           'command': ['fixture-only-not-executed'], 'candidate': phase['recipe_digest'],
           'adapter': {}, 'native_path_contract': native_paths.contract(p.recipe),
           'execution_deadline': '2000-01-01T00:00:00+00:00', 'before_source': source,
           'before_outputs': {}, 'before_native_paths': {}, 'ownership_backend': 'sandbox-census-v1',
           'members': {}}
    cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
               'native_admission': {'launched': False, 'reason': 'preparation_failed'},
               'native_path_effects': {'after': {}}, 'source_after': source,
               'native_temporary_effects': {'observed': True, 'preexisting_preserved': True,
                                            'before': {}, 'after': {},
                                            'attribution': 'filesystem observations; no deletion'}}
    proof = {'format': native_preparation.FORMAT, 'check_id': row['check_id'],
             'identity': native_preparation._identity(row), 'supervisor_popen': 'not_called',
             'stage': 'profile', 'probe': {'state': 'not_started'}, 'profile': None,
             'native_observer_timeouts': []}
    row.update(cleanup=cleanup, preparation_failure={'proof': proof, 'effects': cleanup,
                                                     'receipt_digest': digest([proof, cleanup])})
    rows = [anchor, row]
    with q.runtime._db() as db:
        resource_limits._save(db, phase['resource_id'], guard)
        _save(db, q.qualification_id, record)
        for item in rows:
            db.execute('INSERT INTO native_check VALUES(?,?,?)', (item['check_id'], None, canonical(item)))
    phase_receipt = q.directory / 'historical-receipt.json'
    phase_receipt.write_text(canonical(phase['receipt']))
    source_evidence = q.directory / 'historical-source.json'
    source_evidence.write_text(canonical({'files': {
        str(Path('src/agentic_tools/aos') / name): hashlib.sha256(
            (Path('src/agentic_tools/aos') / name).read_bytes()).hexdigest()
        for name in ('native_processes.py', 'codex_runtime.py', 'native_preparation.py')}}))
    verification_evidence = q.directory / 'historical-verification.json'
    manifest = json.loads(source_evidence.read_text())
    source_digest = digest({name.removeprefix('src/agentic_tools/'): value
                            for name, value in manifest['files'].items()})
    verification_evidence.write_text(canonical({'source_unchanged': True, 'source_digest': source_digest,
                                                'pytest': {'exit_code': 0}}))
    packet = {'format': 'aos-phase-gap-forensics-v1', 'read_only': True,
              'qualification_id': q.qualification_id, 'phase_id': phase['id'],
              'source_baseline_digest': digest(source),
              'phase': {key: phase[key] for key in ('id', 'recipe_digest', 'deadline', 'started_at')},
              'phase_result': copy.deepcopy(phase['receipt']), 'phase_receipt': _file(phase_receipt),
              'historical_execution_artifacts': {
                  str(path): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              'artifact': json.loads(path.read_text())}
                  for path in (source_evidence, verification_evidence)},
              'native_rows': [{'id': item['check_id'],
                               'raw_record_sha256': hashlib.sha256(canonical(item).encode()).hexdigest(),
                               'status': _file(item['status'])} for item in rows]}
    case = SimpleNamespace(p=p, q=q, budget=budget, phase=copy.deepcopy(phase), guard=copy.deepcopy(guard),
                           rows=rows, packet=packet, packet_path=q.directory / 'reviewed-packet.json',
                           packet_hash=None, growth=growth, record=copy.deepcopy(record))
    _write_packet(case)
    return case


def test_exact_disposition_preserves_history_and_replays_without_work(gap_case):
    c = gap_case
    before_budget = c.budget.read_bytes()
    acknowledgment = _ack(c)
    saved = _record(c.q)
    assert acknowledgment['accepted'] is False
    assert acknowledgment['grants_work'] is False and acknowledgment['grants_time'] is False
    assert saved['phases'][0] == c.phase
    assert _guard(c.q, c.phase['resource_id']) == c.guard
    assert saved['breach'] == c.record['breach']
    assert saved['charged_growth_bytes']['source_evidence_roots'] == 7
    continuation = _guard(c.q, saved['quiescent_gap_dispositions'][0]['resource_id'])
    assert continuation['baseline'] == c.guard['baseline']
    assert continuation['scope'] == c.guard['scope']
    assert continuation['high_water_growth_bytes'] == c.guard['high_water_growth_bytes']
    assert continuation['breach'] is None
    assert _ack(c) == acknowledgment
    assert c.budget.read_bytes() == before_budget
    assert c.q.runtime.model_calls == 0
    with c.q.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
    with pytest.raises(Rejected, match='cannot change'):
        c.q.acknowledge_quiescent_gap('review-idle-gap', 'historical', evidence_path=c.packet_path,
                                    evidence_sha256=c.packet_hash, reason='Changed review')


@pytest.mark.parametrize('flag', ['observed', 'preexisting_preserved'])
@pytest.mark.parametrize('damage', ['missing', 'non-boolean'])
def test_rehashed_preparation_without_exact_observation_flags_stays_blocked(gap_case, flag, damage):
    c = gap_case
    row = copy.deepcopy(c.rows[1])
    effects = row['cleanup']['native_temporary_effects']
    if damage == 'missing':
        effects.pop(flag)
    else:
        effects[flag] = 'true'
    failure = row['preparation_failure']
    failure['effects'] = copy.deepcopy(row['cleanup'])
    failure['receipt_digest'] = digest([failure['proof'], failure['effects']])
    raw = canonical(row)
    with c.q.runtime._db() as db:
        db.execute('UPDATE native_check SET record=? WHERE id=?', (raw, row['check_id']))
    evidence = next(item for item in c.packet['native_rows'] if item['id'] == row['check_id'])
    evidence['raw_record_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    _write_packet(c)
    with pytest.raises(Rejected, match='temporary preservation observation is incomplete'):
        _ack(c)
    assert not _record(c.q).get('quiescent_gap_dispositions')
    assert c.q.runtime.model_calls == 0


def test_growth_before_disposition_and_rollover_never_refunds_history(gap_case):
    c = gap_case
    c.growth.write_bytes(b'x' * 11)
    _ack(c)
    assert _record(c.q)['charged_growth_bytes']['source_evidence_roots'] == 11
    c.growth.write_bytes(b'x' * 19)
    second = c.q.begin('corrected', c.p.recipe)
    assert second['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 19
    c.growth.write_bytes(b'x' * 24)
    c.q.reconcile('corrected')
    third = c.q.begin('another-correction', c.p.recipe)
    assert third['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 24
    saved = _record(c.q)
    assert saved['charged_growth_bytes']['source_evidence_roots'] == 24
    assert saved['phases'][0] == c.phase
    assert _guard(c.q, c.phase['resource_id']) == c.guard


@pytest.mark.parametrize('kind', ['output-cap', 'source-cap', 'free-floor'])
def test_original_limit_violation_cannot_hide_under_gap(gap_case, kind):
    c = gap_case
    guard = copy.deepcopy(c.guard)
    if kind == 'free-floor':
        guard['last_observation']['available_bytes'] = {'fixture': 0}
    else:
        group = 'output_roots' if kind == 'output-cap' else 'source_evidence_roots'
        key = 'max_output_growth_bytes' if kind == 'output-cap' else 'max_source_evidence_growth_bytes'
        guard['high_water_growth_bytes'][group] = guard['scope'][key] + 1
    with pytest.raises(Rejected, match='violation cannot be acknowledged'):
        native_quiescence.validate(c.q, c.record, c.phase, guard, c.packet)


@pytest.mark.parametrize('kind', ['packet', 'row', 'status', 'inventory'])
def test_changed_reviewed_evidence_rejects_without_continuation(gap_case, kind):
    c = gap_case
    if kind == 'packet':
        c.packet_path.write_text(c.packet_path.read_text() + ' ')
    elif kind == 'row':
        with c.q.runtime._db() as db:
            row = copy.deepcopy(c.rows[0])
            row['unreviewed_change'] = True
            db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(row), row['check_id']))
    elif kind == 'status':
        Path(c.rows[0]['status']).write_text('{}')
    else:
        c.packet['native_rows'].pop()
        _write_packet(c)
    with pytest.raises(Rejected):
        _ack(c)
    assert not _record(c.q).get('quiescent_gap_dispositions')
    assert _guard(c.q, c.phase['resource_id']) == c.guard


def test_new_prospective_cap_failure_stays_blocked_after_cleanup(gap_case):
    c = gap_case
    c.growth.write_bytes(b'x' * (1024 * 1024 + 1))
    with pytest.raises(Rejected, match='Fresh prospective observation failed'):
        _ack(c)
    failed = _record(c.q)
    assert failed['breach'] == c.guard['breach']
    assert failed['unresolved_failures'][0]['reason'] == 'max_source_evidence_growth_bytes'
    assert not failed.get('quiescent_gap_dispositions')
    assert failed['quiescent_gap_failed_dispositions'][0]['resource_limits']['breach']
    assert _guard(c.q, c.phase['resource_id']) == c.guard
    c.growth.write_bytes(b'x' * 7)
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        c.q.begin('forbidden-retry', c.p.recipe)


@pytest.mark.parametrize('kind', ['source', 'budget', 'new-cap', 'new-gap'])
def test_later_failure_is_not_hidden_by_acknowledged_gap(gap_case, kind):
    c = gap_case
    _ack(c)
    if kind == 'source':
        next(c.p.source.iterdir()).write_text('source drift')
    elif kind == 'budget':
        c.budget.write_bytes(b'changed charges')
    elif kind == 'new-cap':
        c.growth.write_bytes(b'x' * (1024 * 1024 + 1))
    else:
        phase = c.q.begin('later', c.p.recipe)
        with c.q.runtime._db() as db:
            guard = resource_limits._read(db, phase['resource_id'])
            guard['last_observation']['observed_at'] -= 60
            resource_limits._save(db, phase['resource_id'], guard)
        result = c.q.reconcile('later')
        assert result['status'] == 'blocked'
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        c.q.begin('must-remain-blocked', c.p.recipe)
    saved = _record(c.q)
    assert saved['breach'] == c.guard['breach']
    assert saved['unresolved_failures']
    assert _blocking(saved)


@pytest.mark.parametrize('kind', ['free-floor', 'observation-error'])
def test_new_prospective_observation_failure_is_durable(gap_case, monkeypatch, kind):
    c = gap_case
    original = resource_limits.observe

    def failed_observation(*args, **kwargs):
        if kind == 'observation-error':
            raise OSError('isolated read failure')
        observed = original(*args, **kwargs)
        observed['available_bytes'] = {key: 0 for key in observed['available_bytes']}
        return observed

    monkeypatch.setattr(resource_limits, 'observe', failed_observation)
    with pytest.raises(Rejected, match='Fresh prospective observation failed'):
        _ack(c)
    saved = _record(c.q)
    expected = 'min_free_bytes' if kind == 'free-floor' else 'observation_failed'
    assert saved['unresolved_failures'][0]['reason'] == expected
    assert saved['breach'] == c.guard['breach']
    assert _guard(c.q, c.phase['resource_id']) == c.guard
    assert not saved.get('quiescent_gap_dispositions')


def test_export_preserves_blocked_history_and_carried_charges(gap_case):
    c = gap_case
    _ack(c)
    c.growth.write_bytes(b'x' * 19)
    phase = c.q.begin('corrected', c.p.recipe)
    transcript = Path(phase['output']) / c.p.recipe['checks'][0]['name'] / 'output.txt'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('pass')
    receipt = {'name': c.p.recipe['checks'][0]['name'], 'result': 'pass',
               'candidate': phase['recipe_digest'], 'executor': 'technical-qualification-only',
               'output': str(transcript), 'output_sha256': hashlib.sha256(transcript.read_bytes()).hexdigest()}
    row = {'check_id': 'nc-corrected-fixture', 'attempt_id': None, 'phase': 'clean',
           'candidate': phase['recipe_digest'], 'output': str(transcript.parent),
           'receipt': receipt, 'cleanup': {'stopped': True, 'effects_reconciled': True}}
    with c.q.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
    assert c.q.reconcile('corrected')['status'] == 'passed'
    exported = c.q.export(c.p.recipe, c.q.directory / 'reviewed-carry-forward.json', work_id='PRODUCT')
    capsule = json.loads(Path(exported['capsule']['path']).read_text())
    assert capsule['historical_resource_failure'] == c.guard['breach']
    assert capsule['phases'][0]['resource_receipt'] == c.phase['resource_receipt']
    disposition = capsule['quiescent_gap_dispositions'][0]
    assert disposition['resource_receipt']['high_water_growth_bytes']['source_evidence_roots'] == 19
    assert disposition['acknowledgment']['historical_status'] == 'blocked'
    assert disposition['acknowledgment']['original_deadline'] == c.phase['deadline']
    assert exported['charged_growth_bytes']['source_evidence_roots'] == 23
    assert exported['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 23
    assert _record(c.q)['phases'][0] == c.phase
    assert _guard(c.q, c.phase['resource_id']) == c.guard


@pytest.mark.parametrize('kind', ['missing', 'changed', 'embedded-mismatch', 'unrelated-source-digest'])
def test_historical_execution_artifacts_are_required_and_bound(gap_case, kind):
    c = gap_case
    artifacts = c.packet['historical_execution_artifacts']
    if kind == 'missing':
        c.packet.pop('historical_execution_artifacts')
        _write_packet(c)
    elif kind == 'changed':
        Path(next(iter(artifacts))).write_text('{}')
    elif kind == 'embedded-mismatch':
        next(iter(artifacts.values()))['artifact'] = {'unreviewed': 'replacement'}
        _write_packet(c)
    else:
        name, saved = next((name, saved) for name, saved in artifacts.items() if 'pytest' in saved['artifact'])
        changed = copy.deepcopy(saved['artifact'])
        changed['source_digest'] = digest({'unrelated': 'passing verification'})
        Path(name).write_text(canonical(changed))
        saved.update(artifact=changed, sha256=hashlib.sha256(Path(name).read_bytes()).hexdigest())
        _write_packet(c)
    with pytest.raises(Rejected):
        _ack(c)
    assert not _record(c.q).get('quiescent_gap_dispositions')


@pytest.mark.parametrize('kind', ['phase-row', 'relative-output'])
def test_native_row_added_after_review_blocks_transactional_disposition(gap_case, monkeypatch, kind):
    c = gap_case
    validate = native_quiescence.validate

    def validate_then_new_row(*args, **kwargs):
        proof = validate(*args, **kwargs)
        row = copy.deepcopy(c.rows[0])
        row['check_id'] = 'nc-concurrent-native-row'
        if kind == 'relative-output':
            row['output'] = 'relative'
        with c.q.runtime._db() as db:
            db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
        return proof

    monkeypatch.setattr(native_quiescence, 'validate', validate_then_new_row)
    with pytest.raises(Rejected):
        _ack(c)
    assert not _record(c.q).get('quiescent_gap_dispositions')
