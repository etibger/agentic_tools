"""Portable authority and native-owner regressions; no real SDK/model calls."""
import copy
import json

import pytest

from agentic_tools.aos.store import Rejected, canonical
from agentic_tools.aos.codex_runtime import PROFILE
from test_aos_codex_runtime import prepared, claim, native_fixture


@pytest.mark.parametrize('field,value', [
    ('assignment_id', 'foreign-assignment'), ('attempt_id', 'foreign-attempt'),
    ('executor', 'assessor'), ('requested_model', 'unapproved-model'),
    ('requested_effort', 'unapproved-effort'), ('scope', '{}'),
])
def test_saved_assignment_identity_cannot_be_overridden_before_transport(prepared, monkeypatch, field, value):
    p = prepared
    assignment = claim(p)
    monkeypatch.setattr(p.runtime, '_client', lambda *args, **kwargs: pytest.fail('Rejected authority must never reach transport'))
    with pytest.raises(Rejected):
        p.runtime.start({**assignment, field: value})
    assert not p.backend.calls and p.runtime.model_calls == 0


@pytest.mark.parametrize('field,value', [
    ('budget_id', 'foreign-budget'), ('ledger_directory', '/unapproved-budget-ledger'),
    ('max_assignments', 9), ('max_assignments', True), ('max_seconds', 2401),
])
def test_shared_budget_identity_and_limits_remain_bound_to_approved_package(prepared, field, value):
    p = prepared
    assignment = claim(p)
    with p.store._transaction() as db:
        package = json.loads(db.execute("SELECT package FROM concept WHERE work_id='Q2'").fetchone()[0])
        changed = copy.deepcopy(package)
        changed['execution_envelope']['shared_qualification_budget'][field] = value
        db.execute("UPDATE concept SET package=? WHERE work_id='Q2'", (canonical(changed),))
    with pytest.raises(Rejected):
        p.runtime._budget(assignment)
    assert not p.backend.calls and p.runtime.model_calls == 0


@pytest.mark.parametrize('mutation', [
    'tool', 'plugin', 'permission_inheritance', 'workspace_root', 'filesystem',
    'network', 'missing_feature', 'environment_inheritance', 'extra_environment',
])
def test_effective_profile_cannot_widen_inherited_capabilities(prepared, mutation):
    p = prepared
    overrides, expected = p.runtime._profile(p.recipe)
    config = p.backend.factory(None, overrides, p.source).config
    if mutation == 'tool':
        config['mcp_servers']['synthetic-tool'] = {'enabled': True}
    elif mutation == 'plugin':
        config['plugins']['synthetic-plugin'] = {}
    elif mutation == 'permission_inheritance':
        config['permissions'][PROFILE]['extends'] = 'broad'
    elif mutation == 'workspace_root':
        config['permissions'][PROFILE]['workspace_roots'] = ['/']
    elif mutation == 'filesystem':
        config['permissions'][PROFILE]['filesystem']['/unapproved'] = 'read'
    elif mutation == 'network':
        config['permissions'][PROFILE]['network']['enabled'] = True
    elif mutation == 'missing_feature':
        config['features'].pop('hooks')
    elif mutation == 'environment_inheritance':
        config['shell_environment_policy']['inherit'] = 'all'
    else:
        config['shell_environment_policy']['set']['SYNTHETIC_EXTRA'] = 'nonsecret fixture'
    with pytest.raises(Rejected):
        p.runtime._verify_config(config, expected)
    assert p.runtime.model_calls == 0


def test_native_check_without_durable_owner_never_signals_or_relaunches(prepared, monkeypatch):
    p = prepared
    record = {'check_id': 'nc-missing-owner', 'attempt_id': 'missing-owner', 'phase': 'intent', 'members': {}}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (record['check_id'], record['attempt_id'], canonical(record)))
    signals = []
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.os.kill', lambda *args: signals.append(args))
    with pytest.raises(Rejected, match='no durable OS owner'):
        p.runtime._native_stop(record['check_id'])
    assert signals == [] and not p.backend.calls
    assert p.runtime._native_get(record['check_id'])['phase'] == 'cleanup_unknown'


def test_interruption_before_native_execution_barrier_never_runs_workload(tmp_path):
    p = native_fixture(tmp_path, 'child', attempt_id='before-native-barrier')
    def interrupt(stage):
        if stage == 'native_owner_before_go':
            raise OSError('SIMULATED interruption before native go')
    p.runtime._fault = interrupt
    try:
        with pytest.raises(OSError, match='SIMULATED'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'synthetic-assessor', p.frozen)
        assert not (p.outputs / 'child.pid').exists()
        with p.runtime._db() as db:
            record = json.loads(db.execute('SELECT record FROM native_check').fetchone()[0])
        cleaned = p.runtime._native_stop(record['check_id'], cancel=True)
        assert cleaned['phase'] == 'clean' and cleaned['cleanup']['owned_processes'] == []
    finally:
        p.runtime.close()
