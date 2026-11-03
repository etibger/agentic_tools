"""Independent tests for one additive, empty generated-root declaration."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import resource_limits as limits
from agentic_tools.aos.native_qualification import TechnicalQualification, _read
from agentic_tools.aos.store import Rejected
from test_native_qualification_independent import export_accounting, technical


def stopped(g):
    phase = g.q.begin('phase-1', g.recipe, max_seconds=5)
    (g.outputs / 'prior-growth').write_bytes(b'x' * 7)
    assert g.q.reconcile('phase-1')['stopped']
    return phase


def read(g):
    with g.q.runtime._db() as db:
        return _read(db, g.q.qualification_id)


def extend(g, root, operation='extend-1'):
    return g.q.extend_generated_root(operation, g.recipe, root, reason='Normal isolated build output')


def test_extension_adds_only_one_root_and_keeps_history_caps_and_budget(technical):
    g = technical
    phase = stopped(g)
    before = read(g)
    with g.q.runtime._db() as db:
        original_baseline = limits._read(db, phase['resource_id'])['baseline']
    root = g.source / 'new-generated'
    root.mkdir()
    result = extend(g, root)
    expected = copy.deepcopy(g.recipe)
    expected.setdefault('native_write_roots', []).append({'path': str(root), 'kind': 'generated'})
    expected['resource_limits']['output_roots'].append(str(root))
    assert result['accepted'] is False and result['kind'] == 'technical-generated-root-extension'
    assert result['recipe'] == expected
    after = read(g)
    assert after['source_baseline'] == before['source_baseline']
    assert after['budget_before'] == before['budget_before']
    assert [p['id'] for p in after['phases']] == [phase['id']]
    assert after['phases'][0]['recipe'] == before['phases'][0]['recipe']
    assert after['charged_growth_bytes']['source_evidence_roots'] == 7
    assert len(after['generated_root_extensions']) == len(after['pending_generated_roots']) == 1
    assert after['pending_generated_roots'][0]['path'] == str(root)
    with g.q.runtime._db() as db:
        assert limits._read(db, phase['resource_id'])['baseline'] == original_baseline
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0


def test_extension_operation_replay_is_exact_and_never_double_charges(technical):
    g = technical
    stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    first = extend(g, root)
    assert extend(g, root) == first
    assert len(read(g)['generated_root_extensions']) == 1
    with pytest.raises(Rejected):
        g.q.extend_generated_root('extend-1', g.recipe, root, reason='Changed request')
    assert read(g)['charged_growth_bytes']['source_evidence_roots'] == 7


def test_pending_root_growth_is_charged_and_cannot_be_cleared_by_cleanup(technical):
    g = technical
    phase = stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    corrected = extend(g, root)['recipe']
    early = root / 'early-object'
    early.write_bytes(b'x' * 5)
    with pytest.raises(Rejected):
        g.q.begin('phase-2', corrected, max_seconds=5)
    state = read(g)
    assert state['breach']['reason'] == 'generated_extension_not_empty'
    with g.q.runtime._db() as db:
        assert limits._read(db, phase['resource_id'])['high_water_growth_bytes']['source_evidence_roots'] >= 12
    early.unlink()
    (g.outputs / 'later-overrun').write_bytes(b'x' * 101)
    with pytest.raises(Rejected):
        g.q.begin('phase-2', corrected, max_seconds=5)
    assert len(read(g)['phases']) == 1
    assert read(g)['breach']['reason'] == 'generated_extension_not_empty'


def test_captured_pending_root_growth_survives_disappearance_before_old_refresh(technical, monkeypatch):
    g = technical
    stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    corrected = extend(g, root)['recipe']
    original = limits.observe
    injected = []
    def observe(bound):
        if str(root) in bound['generated_roots'] and not injected:
            injected.append(True)
            early = root / 'transient-object'
            early.write_bytes(b'x' * 5)
            captured = original(bound)
            early.unlink()
            return captured
        return original(bound)
    monkeypatch.setattr(limits, 'observe', observe)
    with pytest.raises(Rejected):
        g.q.begin('phase-2', corrected, max_seconds=5)
    assert injected and not any(root.iterdir())
    assert read(g)['breach']['reason'] == 'generated_extension_not_empty'
    with pytest.raises(Rejected):
        g.q.begin('phase-2', corrected, max_seconds=5)


def test_pending_root_identity_replacement_is_sticky(technical):
    g = technical
    stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    corrected = extend(g, root)['recipe']
    root.rename(root.with_name('old-generated'))
    root.mkdir()
    with pytest.raises(Rejected):
        g.q.begin('phase-2', corrected, max_seconds=5)
    assert read(g)['breach']['reason'] == 'generated_extension_identity_changed'


def test_second_extension_cannot_ignore_nonempty_existing_pending_root(technical):
    g = technical
    phase = stopped(g)
    first, second = g.source / 'first-generated', g.source / 'second-generated'
    first.mkdir()
    second.mkdir()
    corrected = extend(g, first)['recipe']
    (first / 'early-object').write_bytes(b'x' * 5)
    with pytest.raises(Rejected):
        g.q.extend_generated_root('extend-2', corrected, second, reason='Another isolated build output')
    state = read(g)
    assert state['breach']['reason'] == 'generated_extension_not_empty'
    assert len(state['generated_root_extensions']) == 1
    with g.q.runtime._db() as db:
        assert limits._read(db, phase['resource_id'])['high_water_growth_bytes']['source_evidence_roots'] >= 12


def test_clean_pending_root_is_consumed_without_resetting_old_charges(technical):
    g = technical
    stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    corrected = extend(g, root)['recipe']
    phase = g.q.begin('phase-2', corrected, max_seconds=5)
    state = read(g)
    assert state['pending_generated_roots'] == []
    assert len(state['phases']) == 2 and state['charged_growth_bytes']['source_evidence_roots'] == 7
    assert phase['recipe']['resource_limits']['max_source_evidence_growth_bytes'] == 93
    with g.q.runtime._db() as db:
        guard = limits._read(db, phase['resource_id'])
    assert not any(Path(name).is_relative_to(root) for name in guard['baseline']['files']['output_roots'])


@pytest.mark.parametrize('state', ['active', 'breach', 'exported'])
def test_extension_rejects_active_failed_or_exported_allowance(technical, state):
    g = technical
    if state == 'exported':
        export_accounting(g)
    else:
        g.q.begin('phase-1', g.recipe, max_seconds=5)
        if state == 'breach':
            (g.outputs / 'overrun').write_bytes(b'x' * 101)
            g.q.reconcile('phase-1')
    root = g.source / 'new-generated'
    root.mkdir()
    with pytest.raises(Rejected):
        extend(g, root)
    assert not read(g).get('generated_root_extensions')


@pytest.mark.parametrize('name', ['.git', '.codex'])
def test_empty_protected_source_root_cannot_become_generated(technical, name):
    g = technical
    stopped(g)
    root = g.source / name
    root.mkdir()
    with pytest.raises(Rejected):
        extend(g, root)
    assert not read(g).get('generated_root_extensions')


def test_extension_rejects_nonempty_outside_and_alias_roots(technical):
    g = technical
    stopped(g)
    nonempty = g.source / 'not-empty'
    outside = g.outputs / 'outside-source'
    real = g.source / 'real-generated'
    alias = g.source / 'alias-generated'
    for root in (nonempty, outside, real):
        root.mkdir()
    (nonempty / 'existing').write_bytes(b'preserve')
    alias.symlink_to(real, target_is_directory=True)
    for root in (nonempty, outside, alias):
        with pytest.raises(Rejected):
            extend(g, root)
    assert (nonempty / 'existing').read_bytes() == b'preserve'
    assert not read(g).get('generated_root_extensions')


def test_extension_cannot_change_existing_resource_caps(technical):
    g = technical
    stopped(g)
    root = g.source / 'new-generated'
    root.mkdir()
    changed = copy.deepcopy(g.recipe)
    changed['resource_limits']['max_output_growth_bytes'] += 1
    with pytest.raises(Rejected):
        g.q.extend_generated_root('extend-1', changed, root, reason='Changed cap')
    assert not read(g).get('generated_root_extensions')


@pytest.mark.parametrize('tamper', [False, True])
def test_extension_retains_exact_original_budget_bytes(technical, tmp_path, tamper):
    budget = tmp_path / 'original-budget-proof'
    original = b'unchanged original reservation evidence'
    budget.write_bytes(original)
    q = TechnicalQualification(tmp_path / 'budgeted-qualification', 'budget-review', budget_paths=[budget])
    g = SimpleNamespace(q=q, recipe=technical.recipe, source=technical.source, outputs=technical.outputs)
    try:
        stopped(g)
        before = read(g)
        root = g.source / 'new-generated'
        root.mkdir()
        if tamper:
            budget.write_bytes(b'changed outside the qualification wrapper')
            with pytest.raises(Rejected):
                extend(g, root)
            assert not read(g).get('generated_root_extensions')
            assert read(g)['breach']['reason'] == 'protected_baseline_changed'
        else:
            extend(g, root)
            assert budget.read_bytes() == original
        assert read(g)['budget_before'] == before['budget_before']
        with q.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM runtime_budget').fetchone()[0] == 0
    finally:
        q.close()
