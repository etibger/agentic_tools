"""Independent carry-forward and technical-qualification evidence regressions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_processes, resource_limits as limits
from agentic_tools.aos.native_qualification import TechnicalQualification
from agentic_tools.aos.store import Rejected, canonical
from test_aos_codex_runtime import native_fixture


@pytest.fixture
def technical(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.recipe['max_seconds'] = p.check['timeout_seconds'] = 5
    p.recipe['resource_limits'] = {
        'output_roots': [], 'source_evidence_roots': [str(p.source), str(p.outputs)],
        'max_output_growth_bytes': 100, 'max_source_evidence_growth_bytes': 100,
        'min_free_bytes': 1, 'sample_interval_seconds': 1}
    q = TechnicalQualification(tmp_path / 'qualification', 'independent-q')
    db = sqlite3.connect(tmp_path / 'synthetic-feature.sqlite3')
    limits.initialize(db)
    yield SimpleNamespace(q=q, recipe=p.recipe, source=p.source, outputs=p.outputs, db=db)
    db.close()
    q.close()
    p.runtime.close()


def synthetic_private_receipts(g, phase, *, count=None, content=b''):
    """Isolate accounting with private fixture evidence; no native execution claim."""
    for index, check in enumerate(phase['recipe']['checks'][:count]):
        directory = Path(phase['output']) / check['name']
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / 'output.txt'
        output.write_bytes(content)
        check_id = f'synthetic-{phase["id"]}-{index}'
        receipt = {'name': check['name'], 'result': 'pass', 'candidate': phase['recipe_digest'],
                   'executor': 'technical-qualification-only', 'output': str(output),
                   'output_sha256': hashlib.sha256(content).hexdigest(), 'raw_outputs': {},
                   'native_check_id': check_id}
        row = {'check_id': check_id, 'output': str(directory), 'phase': 'clean',
               'cleanup': {'stopped': True, 'effects_reconciled': True}, 'receipt': receipt}
        with g.q.runtime._db() as db:
            db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, None, canonical(row)))


def export_accounting(g):
    phase = g.q.begin('phase-1', g.recipe, max_seconds=5)
    synthetic_private_receipts(g, phase)
    (g.outputs / 'qualification-growth').write_bytes(b'x' * 7)
    stopped = g.q.reconcile('phase-1')
    assert stopped['stopped'] and stopped['accepted'] is False
    exported = g.q.export(g.recipe, g.q.directory / 'carry.json', work_id='feature')
    assert exported['charged_growth_bytes']['source_evidence_roots'] == 7
    return exported['recipe']


def test_export_import_keeps_saved_baseline_and_charges_approval_wait_growth(technical):
    g = technical
    final = export_accounting(g)
    capsule = json.loads(Path(final['resource_limits_carry_forward']['path']).read_text())
    assert capsule['target_work_id'] == 'feature'
    (g.outputs / 'approval-wait-growth').write_bytes(b'x' * 5)
    with g.db:
        imported = limits.prepare(g.db, 'feature', final)
    assert imported['baseline'] == capsule['baseline']
    assert imported['scope']['max_source_evidence_growth_bytes'] == 93
    assert imported['high_water_growth_bytes']['source_evidence_roots'] == 5
    assert imported['carry_forward_reference'] == final['resource_limits_carry_forward']
    assert imported['breach'] is None


def test_post_export_overrun_survives_cleanup_and_second_prepare(technical):
    g = technical
    final = export_accounting(g)
    growth = g.outputs / 'approval-wait-overrun'
    growth.write_bytes(b'x' * 94)
    with g.db:
        imported = limits.prepare(g.db, 'feature', final)
    assert imported['breach']['reason'] == 'max_source_evidence_growth_bytes'
    growth.unlink()
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        limits.prepare(g.db, 'feature', final)


def test_carry_capsule_tampering_cannot_establish_a_fresh_baseline(technical):
    g = technical
    final = export_accounting(g)
    capsule = Path(final['resource_limits_carry_forward']['path'])
    capsule.write_bytes(capsule.read_bytes() + b' ')
    with pytest.raises(Rejected, match='capsule changed'):
        limits.prepare(g.db, 'feature', final)
    assert limits._read(g.db, 'feature') is None


def test_carry_reference_cannot_change_after_first_import(technical):
    g = technical
    final = export_accounting(g)
    with g.db:
        limits.prepare(g.db, 'feature', final)
    changed = copy.deepcopy(final)
    original = Path(final['resource_limits_carry_forward']['path'])
    replacement = original.with_name('second-capsule.json')
    replacement.write_bytes(original.read_bytes())
    os.chmod(replacement, 0o600)
    changed['resource_limits_carry_forward']['path'] = str(replacement)
    changed['native_protected_paths'].append(str(replacement))
    with pytest.raises(Rejected, match='cannot be reset'):
        limits.prepare(g.db, 'feature', changed)


def test_root_replacement_between_export_and_import_is_retained_failure(technical):
    g = technical
    final = export_accounting(g)
    g.outputs.rename(g.outputs.with_name('old-outputs'))
    g.outputs.mkdir()
    with g.db:
        imported = limits.prepare(g.db, 'feature', final)
    assert imported['breach']['reason'] == 'observation_failed'


def test_recipe_marker_alone_cannot_authorize_technical_resource_payload(technical):
    from agentic_tools.aos.native_qualification import resource_payload
    g = technical
    g.q.begin('phase-1', g.recipe, max_seconds=5)
    fake = {**g.recipe, '_technical_resource_phase': {'qualification_id': 'independent-q', 'phase_id': 'phase-1'}}
    with pytest.raises(Rejected, match='does not authorize'):
        resource_payload(g.q.runtime.path, fake)


def test_reconcile_cannot_turn_an_incomplete_failed_phase_into_pass(technical):
    g = technical
    g.recipe['checks'].append({**g.recipe['checks'][0], 'name': 'second'})
    phase = g.q.begin('phase-1', g.recipe, max_seconds=5)
    synthetic_private_receipts(g, phase, count=1)
    failed = g.q.reconcile('phase-1', failures=['second command never produced a receipt'])
    assert failed['status'] == 'blocked'
    retried = g.q.reconcile('phase-1')
    assert retried['status'] == 'blocked', 'Cleanup retry must not erase missing-check failure evidence'
    assert 'second command never produced a receipt' in retried['errors']


def test_caller_supplied_pass_without_any_native_check_cannot_qualify(technical):
    g = technical
    g.q.begin('phase-1', g.recipe, max_seconds=5)
    invented = {'name': 'native', 'result': 'pass', 'status': 'passed'}
    with pytest.raises(TypeError):
        g.q.reconcile('phase-1', checks=[invented])
    result = g.q.reconcile('phase-1')
    assert result['status'] == 'blocked', 'A pass must be supported by the exact private native receipt'


def test_growth_during_phase_rollover_is_never_absorbed_by_new_baseline(technical, monkeypatch):
    g = technical
    phase1 = g.q.begin('phase-1', g.recipe, max_seconds=5)
    (g.outputs / 'first-growth').write_bytes(b'x' * 7)
    g.q.reconcile('phase-1')
    original = limits.observe
    def grow_before_new_baseline(bound):
        if bound['max_source_evidence_growth_bytes'] == 93:
            (g.outputs / 'rollover-growth').write_bytes(b'x' * 5)
        return original(bound)
    monkeypatch.setattr(limits, 'observe', grow_before_new_baseline)
    try:
        phase2 = g.q.begin('phase-2', g.recipe, max_seconds=5)
    except Rejected:
        with g.q.runtime._db() as db:
            first = limits._read(db, phase1['resource_id'])
        assert first['high_water_growth_bytes']['source_evidence_roots'] >= 12
        return
    g.q.reconcile('phase-2')
    with g.q.runtime._db() as db:
        first = limits._read(db, phase1['resource_id'])
        second = limits._read(db, phase2['resource_id'])
    assert sum(item['high_water_growth_bytes']['source_evidence_roots'] for item in (first, second)) >= 12


def test_historical_reconciliation_cannot_release_the_current_phase(technical):
    from agentic_tools.aos.native_qualification import _read
    g = technical
    g.q.begin('phase-1', g.recipe, max_seconds=5)
    g.q.reconcile('phase-1')
    g.q.begin('phase-2', g.recipe, max_seconds=5)
    with pytest.raises(Rejected, match='active private phase'):
        g.q.reconcile('phase-1')
    with g.q.runtime._db() as db:
        assert _read(db, g.q.qualification_id)['active_phase'] == 'phase-2'
    with pytest.raises(Rejected, match='unresolved'):
        g.q.begin('phase-3', g.recipe, max_seconds=5)


def test_export_seal_and_target_work_prevent_borrowing_transferred_allowance(technical):
    g = technical
    final = export_accounting(g)
    with pytest.raises(Rejected, match='transferred allowance'):
        g.q.begin('phase-2', g.recipe, max_seconds=5)
    with pytest.raises(Rejected, match='work'):
        limits.prepare(g.db, 'different-feature', final)


def test_export_rechecks_native_evidence_after_successful_reconciliation(technical):
    g = technical
    phase = g.q.begin('phase-1', g.recipe, max_seconds=5)
    synthetic_private_receipts(g, phase, content=b'good')
    result = g.q.reconcile('phase-1')
    assert result['status'] == 'passed'
    artifact = Path(result['checks'][0]['output'])
    artifact.write_bytes(b'evil')  # Equal-size changes are invisible to growth accounting.
    with pytest.raises(Rejected, match='evidence|changed'):
        g.q.export(g.recipe, g.q.directory / 'carry.json', work_id='feature')


def test_two_runtime_instances_cannot_execute_one_phase_twice(technical, monkeypatch):
    g = technical
    second = TechnicalQualification(g.q.directory, g.q.qualification_id)
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def first_check(*args):
        calls.append('first')
        entered.set()
        assert release.wait(5)
        return {'result': 'blocked'}
    def duplicate_check(*args):
        calls.append('second')
        raise AssertionError('Duplicate private phase execution')
    monkeypatch.setattr(g.q.runtime, '_check', first_check)
    monkeypatch.setattr(second.runtime, '_check', duplicate_check)
    runner = threading.Thread(target=lambda: results.append(g.q.run('phase-1', g.recipe, max_seconds=5)))
    try:
        runner.start()
        assert entered.wait(5)
        with pytest.raises(Rejected, match='existing technical phase'):
            second.run('phase-1', g.recipe, max_seconds=5)
        release.set()
        runner.join(timeout=5)
        assert not runner.is_alive() and calls == ['first']
        assert results[0]['status'] == 'blocked'  # No private native receipt was fabricated.
    finally:
        release.set()
        runner.join(timeout=5)
        second.close()
