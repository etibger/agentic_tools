"""Private technical qualification never creates formal or model authority."""
import copy
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from agentic_tools.aos import native_processes, resource_limits
from agentic_tools.aos.native_qualification import TechnicalQualification, deadline, resource_payload
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import native_fixture


@pytest.fixture
def technical(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    p.runtime.close()
    budget = tmp_path / 'original-product-budget.sqlite3'
    budget.write_bytes(b'unchanged original charges')
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    resource_limits={'output_roots': [],
                                     'source_evidence_roots': [str(p.source), str(p.outputs)],
                                     'max_output_growth_bytes': 1024 * 1024,
                                     'max_source_evidence_growth_bytes': 1024 * 1024,
                                     'min_free_bytes': 1, 'sample_interval_seconds': 1})
    q = TechnicalQualification(tmp_path / 'qualification', 'test-qualification', budget_paths=[budget])
    yield p, q, budget
    q.close()


def read(q):
    with q.runtime._db() as db:
        return json.loads(db.execute('SELECT record FROM technical_qualification').fetchone()[0])


def test_private_phase_never_creates_model_or_product_reservation(technical):
    p, q, budget = technical
    phase = q.begin('phase-one', p.recipe)
    assert phase['state'] == 'running'
    with q.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if 'reservation' in table:
                assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert q.runtime.model_calls == 0
    assert budget.read_bytes() == b'unchanged original charges'


def test_phase_rollover_debits_growth_and_preserves_root_contract(technical):
    p, q, _ = technical
    q.begin('first', p.recipe)
    (p.outputs / 'evidence.txt').write_bytes(b'1234567')
    assert q.reconcile('first')['stopped']
    corrected = copy.deepcopy(p.recipe)
    corrected['read_roots'].append(str(Path('/bin').resolve()))
    second = q.begin('second', corrected)
    assert second['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 7
    assert read(q)['charged_growth_bytes']['source_evidence_roots'] == 7
    q.reconcile('second')
    expanded = copy.deepcopy(corrected)
    expanded['resource_limits']['max_source_evidence_growth_bytes'] += 1
    with pytest.raises(Rejected, match='Original roots'):
        q.begin('third', expanded)


def test_active_phase_and_same_phase_changed_recipe_cannot_restart(technical):
    p, q, _ = technical
    first = q.begin('first', p.recipe)
    assert q.begin('first', p.recipe)['deadline'] == first['deadline']
    with pytest.raises(Rejected, match='unresolved'):
        q.begin('second', p.recipe)
    changed = copy.deepcopy(p.recipe)
    changed['max_seconds'] += 1
    with pytest.raises(Rejected, match='changed inputs'):
        q.begin('first', changed)


def test_sticky_breach_prevents_configuration_retry_after_cleanup(technical):
    p, q, _ = technical
    p.recipe['resource_limits']['max_source_evidence_growth_bytes'] = 5
    q.begin('first', p.recipe)
    artifact = p.outputs / 'excess'
    artifact.write_bytes(b'0123456789')
    result = q.reconcile('first')
    assert result['resource_limits']['breach']['observed_bytes'] == 10
    artifact.unlink()
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        q.begin('second', p.recipe)


def test_budget_or_source_change_is_retained_and_blocks_next_phase(technical):
    p, q, budget = technical
    q.begin('first', p.recipe)
    budget.write_bytes(b'externally changed')
    receipt = q.reconcile('first')
    assert not receipt['product_budget_unchanged']
    budget.write_bytes(b'unchanged original charges')
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        q.begin('second', p.recipe)


def test_technical_payload_requires_exact_private_recipe_and_saved_clock(technical, monkeypatch):
    p, q, _ = technical
    phase = q.begin('first', p.recipe)
    recipe = copy.deepcopy(phase['recipe'])
    recipe['_technical_resource_phase'] = {'qualification_id': q.qualification_id, 'phase_id': 'first'}
    with pytest.raises(Rejected, match='does not authorize'):
        resource_payload(q.runtime.path, recipe)
    calls = []
    def inspect(*args):
        payload = resource_payload(q.runtime.path, recipe)
        assert payload['attempt_id'] == 'first'
        # datetime stores microseconds, while the retained wall clock is a float.
        assert deadline(q.runtime.path, recipe).timestamp() <= phase['deadline'] + .000001
        changed = copy.deepcopy(recipe)
        changed['checks'][0]['argv'].append('changed')
        with pytest.raises(Rejected, match='exact recipe'):
            resource_payload(q.runtime.path, changed)
        calls.append(payload)
        return {'result': 'blocked'}  # This metadata check invents no native receipt.
    monkeypatch.setattr(q.runtime, '_check', inspect)
    assert q.run('first', p.recipe)['status'] == 'blocked'
    assert len(calls) == len(recipe['checks'])


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS census fixture')
def test_real_native_qualification_and_saved_carry_forward(technical):
    p, q, budget = technical
    result = q.run('first', p.recipe, max_seconds=30)
    assert result['status'] == 'passed', result
    assert result['accepted'] is False and result['model_calls'] == 0
    assert result['source_unchanged'] and result['product_budget_unchanged']
    exported = q.export(p.recipe, q.directory / 'carry-forward.json', work_id='PRODUCT')
    assert exported['accepted'] is False
    assert exported['charged_growth_bytes']['source_evidence_roots'] > 0
    final = exported['recipe']
    (p.outputs / 'after-export.txt').write_bytes(b'x' * 17)
    db = sqlite3.connect(q.directory / 'separate-product-runtime.sqlite3')
    try:
        resource_limits.initialize(db)
        with db:
            imported = resource_limits.prepare(db, 'PRODUCT', final)
        assert imported['high_water_growth_bytes']['source_evidence_roots'] == 17
        with pytest.raises(Rejected, match='another exact work'):
            resource_limits.prepare(db, 'OTHER', final)
        with pytest.raises(Rejected, match='Exported qualification'):
            q.begin('second', p.recipe)
    finally:
        db.close()
    assert budget.read_bytes() == b'unchanged original charges'


def test_incomplete_phase_cannot_export_qualification(technical):
    p, q, _ = technical
    q.begin('first', p.recipe)
    assert q.reconcile('first')['status'] == 'blocked'
    with pytest.raises(Rejected, match='complete successful'):
        q.export(p.recipe, q.directory / 'invalid.json', work_id='PRODUCT')


def test_empty_generated_extension_keeps_original_source_budget_and_charges(technical):
    p, q, budget = technical
    first = q.begin('first', p.recipe)
    (p.outputs / 'earlier-evidence').write_bytes(b'x' * 7)
    q.reconcile('first')
    original = read(q)
    root = p.source / 'zig-pkg'
    root.mkdir()
    extended = q.extend_generated_root('add-zig-pkg', p.recipe, root, reason='Native tool writes its normal generated package here')
    assert extended['accepted'] is False
    assert extended['extension']['charged_growth_bytes']['source_evidence_roots'] == 7
    changed = extended['recipe']
    assert changed['resource_limits']['max_source_evidence_growth_bytes'] == p.recipe['resource_limits']['max_source_evidence_growth_bytes']
    assert changed['native_write_roots'] == [{'path': str(root), 'kind': 'generated'}]
    assert q.extend_generated_root('add-zig-pkg', p.recipe, root,
                                  reason='Native tool writes its normal generated package here') == extended
    saved = read(q)
    assert saved['source_baseline'] == original['source_baseline']
    assert saved['budget_before'] == original['budget_before']
    assert saved['phases'][0]['resource_id'] == first['resource_id']
    second = q.begin('second', changed)
    assert second['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 1024 * 1024 - 7
    assert read(q)['pending_generated_roots'] == []
    (root / 'generated.zig').write_bytes(b'x' * 13)
    result = q.reconcile('second')
    assert result['resource_limits']['high_water_growth_bytes']['output_roots'] == 13
    assert result['source_unchanged'] and result['product_budget_unchanged']
    assert budget.read_bytes() == b'unchanged original charges'


def test_generated_extension_cannot_replace_or_remove_earlier_roots(technical):
    p, q, _ = technical
    q.begin('first', p.recipe)
    q.reconcile('first')
    one, two = p.source / 'one', p.source / 'two'
    one.mkdir()
    two.mkdir()
    first = q.extend_generated_root('one', p.recipe, one, reason='first native output')
    with pytest.raises(Rejected, match='exact bounded prior contract'):
        q.extend_generated_root('two', p.recipe, two, reason='cannot omit the previous root')
    second = q.extend_generated_root('two', first['recipe'], two, reason='second empty native output')
    assert set(second['recipe']['resource_limits']['output_roots']) == {str(one), str(two)}
    assert read(q)['charged_growth_bytes'] == {'output_roots': 0, 'source_evidence_roots': 0}


def test_new_generated_root_must_still_be_empty_at_admission(technical):
    p, q, _ = technical
    q.begin('first', p.recipe)
    q.reconcile('first')
    root = p.source / 'zig-pkg'
    root.mkdir()
    extended = q.extend_generated_root('add', p.recipe, root, reason='native generated packages')
    artifact = root / 'too-early'
    artifact.write_bytes(b'x' * 7)
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        q.begin('second', extended['recipe'])
    assert artifact.read_bytes() == b'x' * 7
    saved = read(q)
    assert saved['charged_growth_bytes']['source_evidence_roots'] == 7
    assert saved['breach']['reason'] == 'generated_extension_not_empty'
    artifact.unlink()
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        q.begin('third', extended['recipe'])


def test_generated_extension_cannot_hide_preexisting_or_immutable_content(technical):
    p, q, _ = technical
    q.begin('first', p.recipe)
    q.reconcile('first')
    with pytest.raises(Rejected, match='strict source child'):
        q.extend_generated_root('source', p.recipe, p.source, reason='invalid whole source')
    root = p.source / 'nonempty'
    root.mkdir()
    (root / 'existing').write_text('preserved')
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        q.extend_generated_root('nonempty', p.recipe, root, reason='must not convert existing files into a baseline')
    assert (root / 'existing').read_text() == 'preserved'
    assert not read(q).get('generated_root_extensions')
