"""Retained prelaunch failure recovery never invents a completed native run."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_paths, native_preparation, native_sandbox, native_temporary, resource_limits
from agentic_tools.aos.native_qualification import _blocking, _read, _save
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_native_qualification import technical
from test_native_quiescence_recovery_review import gap_case, _ack, _file, _guard, _record


def _write_packet(case):
    case.packet_path.write_text(canonical(case.packet))
    case.packet_hash = hashlib.sha256(case.packet_path.read_bytes()).hexdigest()


def _save_row(case):
    row = case.row
    failure = row.get('preparation_failure')
    if failure:
        failure['proof']['identity'] = native_preparation._identity(row)
        failure['effects'] = copy.deepcopy(row['cleanup'])
        failure['receipt_digest'] = digest([failure['proof'], failure['effects']])
    with case.q.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                   (row['check_id'], None, canonical(row)))
    case.packet['native_rows'] = [{'id': row['check_id'],
                                   'raw_record_sha256': hashlib.sha256(canonical(row).encode()).hexdigest(),
                                   'status': _file(row['status']), 'release': _file(row['release']),
                                   'cancel': _file(row['cancel_path']),
                                   'profile': _file(row['native_sandbox_boundary']['path'])
                                   if row.get('native_sandbox_boundary') else None}]
    _write_packet(case)


def _prelaunch_ack(case):
    return case.q.acknowledge_prelaunch_observation_failure(
        'review-prelaunch-observation', 'prelaunch', evidence_path=case.packet_path,
        evidence_sha256=case.packet_hash, reason='Retained prelaunch-only observation failure reviewed')


@pytest.fixture
def prelaunch_case(gap_case):
    old = gap_case
    first_ack = _ack(old)
    temporary_parent = old.q.directory.parent / 'fixture-temporary'
    temporary_parent.mkdir()
    temporary_root = temporary_parent / 'owned-native-root'
    recipe = copy.deepcopy(old.p.recipe)
    recipe['native_sandbox'] = {'version': native_sandbox.VERSION,
                               'temporary_paths': [{'parent': str(temporary_parent),
                                                    'name': temporary_root.name, 'kind': 'tree'}],
                               'owned_temporary_roots': [str(temporary_root)]}
    phase = old.q.begin('prelaunch', recipe)
    growth = old.p.outputs / 'prelaunch-controller-evidence'
    growth.write_bytes(b'y' * 5)
    old.q.reconcile('prelaunch')
    record = _record(old.q)
    phase = record['phases'][-1]
    guard = _guard(old.q, phase['resource_id'])
    breach = {'reason': 'observation_failed', 'error': 'Rejected', 'observed_at': time.time()}
    guard['breach'] = copy.deepcopy(breach)
    phase['resource_receipt'] = resource_limits.receipt(guard)
    phase['receipt']['resource_limits'] = copy.deepcopy(phase['resource_receipt'])
    record.setdefault('unresolved_failures', []).append(copy.deepcopy(breach))
    first_disposition = copy.deepcopy(record['quiescent_gap_dispositions'][0])
    before_temporary = native_sandbox.temporary_snapshot(recipe)
    owned_journal = old.q.directory / 'synthetic-owned-temporary.json'
    save = lambda value: owned_journal.write_text(canonical(value))
    check_id = 'nc-prelaunch-only-fixture'
    owned = native_temporary.prepare(recipe, check_id, before_temporary, save)
    removed = native_temporary.remove(recipe, check_id, owned, save, quiescent=True)
    output = Path(phase['output']) / 'not-started'
    output.mkdir(parents=True)
    row = {'check_id': check_id, 'attempt_id': None, 'phase': 'clean',
           'source': str(old.p.source), 'outputs_root': str(old.p.outputs), 'output': str(output),
           'status': str(old.q.directory / 'prelaunch-no-status.json'),
           'release': str(old.q.directory / 'prelaunch-no-release'),
           'cancel_path': str(old.q.directory / 'prelaunch-no-cancel'),
           'command': ['synthetic-prelaunch-not-executed'], 'candidate': phase['recipe_digest'],
           'adapter': {}, 'native_path_contract': native_paths.contract(recipe),
           'execution_deadline': '2000-01-01T00:00:00+00:00',
           'before_source': copy.deepcopy(record['source_baseline']), 'before_outputs': {},
           'before_native_paths': {}, 'native_temporary_before': before_temporary,
           'ownership_backend': 'sandbox-census-v1', 'members': {}, 'owned_temporary': removed}
    cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
               'native_admission': {'launched': False, 'reason': 'preparation_failed'},
               'native_path_effects': {'after': {}}, 'source_after': copy.deepcopy(record['source_baseline']),
               'native_temporary_effects': {'observed': True, 'preexisting_preserved': True,
                                            'before': before_temporary, 'after': before_temporary,
                                            'attribution': 'filesystem observations; no deletion'},
               'owned_temporary': removed}
    proof = {'format': native_preparation.FORMAT, 'check_id': check_id,
             'identity': native_preparation._identity(row), 'supervisor_popen': 'not_called',
             'stage': 'profile', 'probe': {'state': 'not_started'}, 'profile': None,
             'native_observer_timeouts': [], 'owned_temporary': removed}
    row.update(cleanup=cleanup, preparation_failure={'proof': proof, 'effects': cleanup,
                                                     'receipt_digest': digest([proof, cleanup])})
    with old.q.runtime._db() as db:
        resource_limits._save(db, phase['resource_id'], guard)
        _save(db, old.q.qualification_id, record)
        raw_guard = db.execute('SELECT record FROM resource_limit WHERE work_id=?',
                               (phase['resource_id'],)).fetchone()[0]
    receipt_path = old.q.directory / 'prelaunch-phase-receipt.json'
    receipt_path.write_text(canonical(phase['receipt']))
    packet = {'format': 'aos-phase-prelaunch-observation-forensics-v1', 'read_only': True,
              'qualification_id': old.q.qualification_id, 'phase_id': phase['id'],
              'source_baseline_digest': digest(record['source_baseline']),
              'phase': {key: phase[key] for key in ('id', 'recipe_digest', 'deadline', 'started_at')},
              'phase_result': copy.deepcopy(phase['receipt']), 'phase_receipt': _file(receipt_path),
              'resource_guard_digest': digest(guard),
              'resource_guard_raw_sha256': hashlib.sha256(raw_guard.encode()).hexdigest(),
              'guard_baseline_digest': digest(guard['baseline']), 'guard_scope_digest': guard['scope_digest'],
              'prior_charged_growth_bytes': copy.deepcopy(record['charged_growth_bytes']),
              'prior_dispositions': [{'operation_id': item['operation_id'], 'phase_id': item['phase_id'],
                                      'acknowledgment_digest': digest(item['acknowledgment']),
                                      'resource_id': item['resource_id'],
                                      'resource_receipt': copy.deepcopy(item['resource_receipt'])}
                                     for item in [*record.get('quiescent_gap_dispositions', []),
                                                  *record.get('prelaunch_observation_dispositions', [])]],
              'historical_execution_artifacts': copy.deepcopy(old.packet['historical_execution_artifacts'])}
    case = SimpleNamespace(p=old.p, q=old.q, budget=old.budget, old=old, first_ack=first_ack,
                           first_disposition=first_disposition, phase=copy.deepcopy(phase),
                           guard=copy.deepcopy(guard), record=copy.deepcopy(record), recipe=recipe,
                           row=row, growth=growth, temporary_root=temporary_root,
                           packet=packet, packet_path=old.q.directory / 'prelaunch-reviewed-packet.json',
                           packet_hash=None)
    _save_row(case)
    return case


def test_prelaunch_ack_preserves_each_old_failure_and_prior_disposition(prelaunch_case):
    c = prelaunch_case
    acknowledgment = _prelaunch_ack(c)
    saved = _record(c.q)
    assert acknowledgment['accepted'] is False
    assert acknowledgment['grants_work'] is False and acknowledgment['grants_time'] is False
    assert saved['breach'] == c.old.guard['breach']
    assert c.guard['breach'] in saved['unresolved_failures']
    assert saved['phases'][0] == c.old.phase and saved['phases'][1] == c.phase
    assert _guard(c.q, c.old.phase['resource_id']) == c.old.guard
    assert _guard(c.q, c.phase['resource_id']) == c.guard
    assert saved['quiescent_gap_dispositions'][0] == c.first_disposition
    assert not _blocking(saved)
    assert _ack(c.old) == c.first_ack
    assert _prelaunch_ack(c) == acknowledgment
    assert c.q.runtime.model_calls == 0
    assert c.budget.read_bytes() == b'unchanged original charges'
    with c.q.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0


def test_prelaunch_growth_carries_both_historical_debits_into_later_phases(prelaunch_case):
    c = prelaunch_case
    c.growth.write_bytes(b'y' * 11)
    _prelaunch_ack(c)
    assert _record(c.q)['charged_growth_bytes']['source_evidence_roots'] == 18
    c.growth.write_bytes(b'y' * 19)
    later = c.q.begin('after-prelaunch-repair', c.recipe)
    assert later['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 26
    c.growth.write_bytes(b'y' * 24)
    c.q.reconcile('after-prelaunch-repair')
    final = c.q.begin('another-distinct-phase', c.recipe)
    assert final['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 31
    assert _guard(c.q, c.old.phase['resource_id']) == c.old.guard
    assert _guard(c.q, c.phase['resource_id']) == c.guard


def test_prelaunch_disposition_rejects_even_a_qualified_reaped_probe(prelaunch_case):
    c = prelaunch_case
    profile = c.q.directory / 'synthetic-prelaunch.sb'
    profile.write_text('synthetic retained profile, never executed')
    boundary = {'path': str(profile), 'sha256': hashlib.sha256(profile.read_bytes()).hexdigest(),
                'temporary_before': copy.deepcopy(c.row['native_temporary_before'])}
    c.row['native_sandbox_boundary'] = boundary
    c.row['cleanup']['native_temporary_effects']['profile_sha256'] = boundary['sha256']
    proof = c.row['preparation_failure']['proof']
    proof.update(stage='permission_probe', profile=boundary,
                 probe={'state': 'reaped', 'outcome': 'returned', 'exit_code': 0,
                        'isolated': True, 'site_disabled': True, 'nofork': True,
                        'interpreter': str(Path(sys.executable).resolve()),
                        'interpreter_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                        'profile_sha256': boundary['sha256']})
    _save_row(c)
    native_preparation.verify(c.row)
    with pytest.raises(Rejected):
        _prelaunch_ack(c)


@pytest.mark.parametrize('damage', ['owner', 'launched', 'ambiguous-probe', 'missing-reaping',
                                   'unremoved-owned-root', 'wrong-owned-check'])
def test_prelaunch_proof_cannot_cover_launched_or_unreconciled_effects(prelaunch_case, damage):
    c = prelaunch_case
    if damage == 'owner':
        c.row['owner'] = {'pid': 123, 'started': 'unqualified synthetic owner'}
    elif damage == 'launched':
        c.row.pop('preparation_failure')
        c.row['receipt'] = {'result': 'pass'}
    elif damage == 'ambiguous-probe':
        c.row['preparation_failure']['proof']['probe'] = {'state': 'launch_ambiguous'}
    elif damage == 'missing-reaping':
        c.row['preparation_failure']['proof']['probe'] = {'state': 'reaped', 'outcome': 'returned',
                                                        'isolated': True, 'site_disabled': True, 'nofork': False}
    else:
        owned = copy.deepcopy(c.row['owned_temporary'])
        if damage == 'unremoved-owned-root':
            owned['roots'][0]['state'] = 'owned'
        else:
            owned['check_id'] = 'nc-different-check'
        c.row['owned_temporary'] = owned
        c.row['cleanup']['owned_temporary'] = owned
        c.row['preparation_failure']['proof']['owned_temporary'] = owned
    _save_row(c)
    with pytest.raises(Rejected):
        _prelaunch_ack(c)
    assert _blocking(_record(c.q))
    assert not c.temporary_root.exists()


@pytest.mark.parametrize('breach', [{'reason': 'active_observation_gap'},
                                   {'reason': 'observation_failed', 'error': 'OSError'},
                                   {'reason': 'max_source_evidence_growth_bytes'},
                                   {'reason': 'min_free_bytes'}])
def test_prelaunch_method_is_not_an_arbitrary_failure_override(prelaunch_case, breach):
    c = prelaunch_case
    record, guard = _record(c.q), _guard(c.q, c.phase['resource_id'])
    changed = {**breach, 'observed_at': c.guard['breach']['observed_at']}
    guard['breach'] = changed
    phase = record['phases'][-1]
    phase['resource_receipt'] = resource_limits.receipt(guard)
    phase['receipt']['resource_limits'] = copy.deepcopy(phase['resource_receipt'])
    record['unresolved_failures'] = [changed]
    with c.q.runtime._db() as db:
        resource_limits._save(db, phase['resource_id'], guard)
        _save(db, c.q.qualification_id, record)
    c.packet['phase_result'] = copy.deepcopy(phase['receipt'])
    receipt_path = Path(c.packet['phase_receipt']['path'])
    receipt_path.write_text(canonical(phase['receipt']))
    c.packet['phase_receipt'] = _file(receipt_path)
    _write_packet(c)
    with pytest.raises(Rejected):
        _prelaunch_ack(c)


def test_another_unresolved_failure_cannot_hide_behind_prelaunch_ack(prelaunch_case):
    c = prelaunch_case
    with c.q.runtime._db() as db:
        record = _read(db, c.q.qualification_id)
        record['unresolved_failures'].append({'reason': 'protected_baseline_changed', 'observed_at': time.time()})
        _save(db, c.q.qualification_id, record)
    with pytest.raises(Rejected):
        _prelaunch_ack(c)
    assert _blocking(_record(c.q))


def test_new_prospective_failure_preserves_both_historical_dispositions(prelaunch_case):
    c = prelaunch_case
    c.growth.write_bytes(b'y' * (1024 * 1024 + 1))
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        _prelaunch_ack(c)
    saved = _record(c.q)
    assert saved['breach'] == c.old.guard['breach']
    assert saved['quiescent_gap_dispositions'][0] == c.first_disposition
    assert c.guard['breach'] in saved['unresolved_failures']
    assert any(failure.get('reason') == 'max_source_evidence_growth_bytes' for failure in saved['unresolved_failures'])
    assert _guard(c.q, c.phase['resource_id']) == c.guard
    c.growth.write_bytes(b'y' * 5)
    with pytest.raises(Rejected):
        c.q.begin('still-blocked', c.recipe)


@pytest.mark.parametrize('kind', ['packet-hash', 'native-row', 'phase-identity'])
def test_prelaunch_review_is_bound_to_exact_retained_evidence(prelaunch_case, kind):
    c = prelaunch_case
    if kind == 'packet-hash':
        c.packet_path.write_text(c.packet_path.read_text() + ' ')
    elif kind == 'native-row':
        with c.q.runtime._db() as db:
            changed = copy.deepcopy(c.row)
            changed['unreviewed_change'] = True
            db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(changed), changed['check_id']))
    else:
        c.packet['phase_id'] = 'different-phase'
        _write_packet(c)
    with pytest.raises(Rejected):
        _prelaunch_ack(c)
    saved = _record(c.q)
    assert _blocking(saved)
    assert not saved.get('prelaunch_observation_dispositions')


@pytest.mark.parametrize('kind', ['underlying-baseline', 'prior-acknowledgment'])
def test_prelaunch_review_binds_hidden_baseline_and_prior_accounting(prelaunch_case, kind):
    c = prelaunch_case
    with c.q.runtime._db() as db:
        if kind == 'underlying-baseline':
            guard = resource_limits._read(db, c.phase['resource_id'])
            files = guard['baseline']['files']['source_evidence_roots']
            files[next(iter(files))] += 99
            resource_limits._save(db, c.phase['resource_id'], guard)
        else:
            record = _read(db, c.q.qualification_id)
            record['quiescent_gap_dispositions'][0]['acknowledgment']['reason'] = 'Unreviewed changed reason'
            _save(db, c.q.qualification_id, record)
    with pytest.raises(Rejected, match='guard, baseline or prior accounting evidence changed'):
        _prelaunch_ack(c)
    saved = _record(c.q)
    assert not saved.get('prelaunch_observation_dispositions')
    assert c.guard['breach'] in saved['unresolved_failures']


def test_later_identical_kind_of_observation_failure_still_blocks(prelaunch_case):
    c = prelaunch_case
    _prelaunch_ack(c)
    phase = c.q.begin('new-observation-failure', c.recipe)
    later_breach = {**c.guard['breach'], 'observed_at': c.guard['breach']['observed_at'] + 1}
    with c.q.runtime._db() as db:
        guard = resource_limits._read(db, phase['resource_id'])
        guard['breach'] = later_breach
        resource_limits._save(db, phase['resource_id'], guard)
    assert c.q.reconcile('new-observation-failure')['status'] == 'blocked'
    saved = _record(c.q)
    assert c.guard['breach'] in saved['unresolved_failures']
    assert later_breach in saved['unresolved_failures']
    assert _blocking(saved)
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        c.q.begin('must-not-replay-old-disposition', c.recipe)


def test_product_capsule_retains_both_distinct_dispositions(prelaunch_case):
    c = prelaunch_case
    _prelaunch_ack(c)
    phase = c.q.begin('passing-correction', c.recipe)
    transcript = Path(phase['output']) / c.recipe['checks'][0]['name'] / 'output.txt'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('pass')
    receipt = {'name': c.recipe['checks'][0]['name'], 'result': 'pass',
               'candidate': phase['recipe_digest'], 'executor': 'technical-qualification-only',
               'output': str(transcript), 'output_sha256': hashlib.sha256(transcript.read_bytes()).hexdigest()}
    row = {'check_id': 'nc-later-passing-fixture', 'attempt_id': None, 'phase': 'clean',
           'candidate': phase['recipe_digest'], 'output': str(transcript.parent),
           'receipt': receipt, 'cleanup': {'stopped': True, 'effects_reconciled': True}}
    with c.q.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
    assert c.q.reconcile('passing-correction')['status'] == 'passed'
    exported = c.q.export(c.recipe, c.q.directory / 'both-histories-capsule.json', work_id='PRODUCT')
    capsule = json.loads(Path(exported['capsule']['path']).read_text())
    assert capsule['quiescent_gap_dispositions'][0] == c.first_disposition
    disposition = capsule['prelaunch_observation_dispositions'][0]
    assert disposition['historical_breach'] == c.guard['breach']
    assert disposition['acknowledgment']['historical_status'] == 'blocked'
    assert capsule['phases'][0]['resource_receipt'] == c.old.phase['resource_receipt']
    assert capsule['phases'][1]['resource_receipt'] == c.phase['resource_receipt']
    assert exported['charged_growth_bytes']['source_evidence_roots'] == 16
    assert exported['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 16
