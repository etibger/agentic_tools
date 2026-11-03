"""One bounded continuation in disposable journals with simulated observations."""
import copy
import json
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest

from agentic_tools.aos import resource_limits
from agentic_tools.aos import startup_failure_recovery as recovery
from agentic_tools.aos import startup_resource_continuation as continuation
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_lifecycle import approve
from test_aos_startup_failure_recovery import (
    failed_startup,
    recover,
    retain,
    seal,
    sha,
    sql,
    tables,
)


def guard_raw(c):
    return sql(c.runtime, "SELECT record FROM resource_limit WHERE work_id='Q1'")[0][0]


def contract_raw(c):
    return sql(c.store.path, "SELECT contract FROM lifecycle WHERE work_id='Q1'")[0][0]


def acknowledge(c, *, apply=False):
    return continuation.acknowledge(c.lifecycle, c.continuation_request, apply=apply)


def optional_metadata(c, contract, *, current):
    """Production-shaped proof references, separate from frozen execution controls."""
    label = 'current' if current else 'prior'
    def file(name, value=None):
        value = {'fixture_evidence': name, 'generation': label} if value is None else value
        raw = canonical(value).encode()
        return {**retain(c.evidence_root / (label + '-' + name + '.json'), raw), 'bytes': len(raw)}
    def frozen_file(name):
        raw = canonical({'fixture_evidence': name}).encode()
        return {**retain(c.evidence_root / ('frozen-' + name + '.json'), raw), 'bytes': len(raw)}
    coordinator, preflight, worker = file('coordinator'), file('preflight'), file('worker-configuration')
    recipe = file('runtime-recipe', contract['runtime_recipe'])
    mapping = {**file('runtime-recipes', {'Q1': contract['runtime_recipe']}),
               'recipe': {key: recipe[key] for key in ('path', 'sha256')},
               'recipe_digest': digest(contract['runtime_recipe']),
               'shape': 'work-id to exact recipe', 'work_ids': ['Q1']}
    growth = {'output_roots': 60, 'source_evidence_roots': 10} if current else {
        'output_roots': 20, 'source_evidence_roots': 5}
    evidence = {key: file(key) for key in ('actual_product_role_readiness', 'affected_verification',
                                         'feature_binding_preparation', 'frozen_aos_candidate', 'native_metadata_equivalence')}
    if not current:
        def role():
            return {'all_checks_true': True,
                    'appserver_cleanup': {'owned_group_survivors': {}, 'returncode': 0, 'verified': True},
                    'checks': 1, 'command_elapsed_seconds': 0.1, 'command_receipt': frozen_file('role-command'),
                    'complete_exact_key_set': True, 'direct_child_reaped': True,
                    'effective_configuration_verified': True, 'model_calls': 0,
                    'native_only_file_read_denials': {str(c.root / 'global-ignore'): True, str(c.root / 'gitconfig'): True},
                    'observed_cleanup_verified': True,
                    'owned_native_root_evidence': {'effective_config_verified': True,
                        'exact_model_filesystem_denies': {str(c.root / 'native-root'): 'denied'},
                        'real_root_behavioral_access_or_creation_tested': False, 'real_root_creation_attempted': False,
                        'root_absent_after': True, 'root_absent_before': True, 'scope': 'retained fixture'},
                    'pipes_reached_eof': True, 'probe_exit_code': 0}
        evidence['actual_product_role_readiness'] = {
            'accepted': False, 'base_receipt': frozen_file('role-base'),
            'binary': {'path': str(c.root / 'fixture-binary'), 'sha256': 'a' * 64, 'version': 'fixture'},
            'candidate': {'aos_source_digest': 'sha256:' + 'a' * 64,
                          'path': str(c.root / 'fixture-candidate'), 'sha256': 'a' * 64},
            'complete_role_probe_contract_verified': True, 'current_state_revalidation_still_required': True,
            'diagnostic_differences': ['retained fixture'], 'execution_authority': False,
            'limitations': ['simulated proof only'], 'model_assignments': 0, 'model_budget_reservations': 0,
            'model_turns': 0, 'observed_cleanup_scope': 'retained fixture',
            'preservation': {'configuration_unchanged': True, 'product_budget_and_lifecycle_bytes_unchanged': True,
                             'resource_capsule_unchanged': True, 'source_unchanged': True, 'wrapper_inputs_unchanged': True},
            'receipt': frozen_file('role-readiness'), 'recipe': {key: recipe[key] for key in ('path', 'sha256')},
            'roles': {'assessment': role(), 'implementation': role()}, 'scope': 'retained fixture',
            'source_observed_after': 'sha256:' + 'b' * 64, 'source_observed_before': 'sha256:' + 'b' * 64,
            'status': 'qualified fixture', 'thread_starts': 2}
        evidence['affected_verification'] = {
            'coverage_accounting': {'all_prior_failing_cases_in_current_run': True, 'current_affected_passing_checks': 1,
                                    'interpretation': 'retained fixture checks', 'single_run_r21_310_pass_claim': False,
                                    'unchanged_prior_passing_checks': 1},
            'documentation': {'basis': 'retained fixture', 'source': frozen_file('documentation'), 'strict_build': 'pass'},
            'r20_run': {'disposition': 'retained', 'failed': 1, 'junit': frozen_file('prior-junit'), 'passed': 1, 'tests': 2},
            'r21_affected_run': {'failed': 0, 'junit': frozen_file('affected-junit'), 'passed': 1,
                                 'source_change': 'retained fixture', 'tests': 1}}
        evidence['native_metadata_equivalence'] = {
            'execution_authority': False, 'os_qualification': False, 'profiles_equal': True,
            'receipt': frozen_file('native-equivalence'), 'reuse_rationale': 'retained fixture',
            'scope': 'fixture', 'serialized_native_routes_equal': True, 'verified': True}
    evidence.update(actual_folder_coordinator=coordinator, final_readiness_preflight=preflight,
                    runtime_recipe_file=recipe, worker_configuration=worker,
                    frozen_aos_source_digest='sha256:' + ('b' if current else 'a') * 64,
                    limitation='Current bounded proof' if current else 'Prior bounded proof',
                    model_permission_mode={'field': 'model_write_scope', 'value': 'existing-mutable-files',
                                           'scope': 'exact existing mutable fixture file',
                                           'isolated_fixture': frozen_file('isolated-model-permissions'),
                                           'isolated_permission_rows': 1,
                                           'actual_product_roles_status': 'current' if current else 'prior'},
                    historical_native_candidate=frozen_file('historical-native-candidate'),
                    historical_native_source_digest='sha256:' + 'c' * 64,
                    native_baseline_verification=frozen_file('native-baseline-verification'),
                    resource_export_verification=frozen_file('resource-export-verification'),
                    historical_design_evidence={'aos_issue': 'fixture-issue', 'aos_source_digest': 'sha256:' + 'd' * 64,
                                                'raw_probe': 'retained fixture probe', 'raw_probe_sha256': 'e' * 64,
                                                'recomputation_sha256': 'f' * 64})
    contract.update(
        feature_sublimit={'failed_attempts_charged': True, 'max_active_elapsed_seconds': 180,
                          'max_attempts_including_startup': 3, 'max_isolated_ui_seconds': 10,
                          'max_job_seconds': 60, 'max_native_validation_seconds': 20, 'max_reserved_seconds': 180,
                          'model_assignments': 2, 'repair_cycles': 0, 'startup_only_attempts': 1},
        execution_launch_plan={'active_elapsed_seconds_max': 180, 'feature_ceiling_attempts': 3,
                               'feature_ceiling_seconds': 180, 'historical_charged_seconds_preserved': 60,
                               'isolated_ui_seconds_max': 10, 'model_assignments': 2,
                               'native_validation_total_seconds_max': 20, 'new_reserved_seconds_planned': 120,
                               'reason': 'two remaining fixture roles', 'repair_cycles': 0, 'seconds_each': 60,
                               'standalone_startup_reservations': 0},
        runtime_recipe_status='current qualified recipe' if current else 'prior recipe',
        baseline_provenance={'admission_recheck_required': True, 'current_revalidation': preflight,
                             'fresh_current_inventory_revalidation_required': True, 'git_ref': 'a' * 40,
                             'git_ref_role': 'retained fixture baseline', 'inventory': frozen_file('baseline-inventory'),
                             'inventory_digest': 'sha256:' + 'e' * 64},
        resource_accounting_binding={
            'latest_readonly_preflight': preflight, 'minimum_available_bytes_at_preflight': 8500 if current else 9000,
            'post_export_growth_bytes_at_preflight': growth,
            'remaining_after_observed_post_export_growth_bytes': {
                'output_roots': 1024 - growth['output_roots'], 'source_evidence_roots': 2048 - growth['source_evidence_roots']},
            'exact_remaining_limits': {'max_output_growth_bytes': 1024, 'max_source_evidence_growth_bytes': 2048},
            'min_free_bytes': 100, 'capsule': {**c.recipe['resource_limits_carry_forward'],
                                              'bytes': Path(c.recipe['resource_limits_carry_forward']['path']).stat().st_size},
            'capsule_charged_growth_bytes': c.capsule['charged_growth_bytes'],
            'capsule_original_ceilings': c.capsule['original_ceilings'],
            'historical_failures_and_dispositions_preserved': True, 'historical_phase_count': 1,
            'initial_intake_debit_bytes': 0, 'original_product_ceilings': c.capsule['original_ceilings'],
            'sample_interval_seconds': 2,
            'semantics': 'bounded stop-on-observed-breach; no historical peak claim',
            'normal_import': 'current normal import' if current else 'prior normal import'},
        worker_configuration=worker,
        worker_launch_binding={
            'argv': ['fixture-worker', '--state', str(c.store.directory), '--runtime-recipes', mapping['path'], '--loop'],
            'runtime_recipes': mapping, 'launch_condition': 'current qualifications verified' if current else 'prior hold',
            'cwd': str(c.root), 'feature_owner_chat': 'fixture-feature-owner', 'single_owner_chat': 'fixture-worker-owner',
            'loop': {'interval_seconds': 2, 'max_cycles': 10, 'maximum_nominal_sleep_seconds': 20,
                     'scope': 'single foreground fixture worker'}, 'started': False},
        evidence=evidence,
        prerequisite_evidence_binding={'actual_folder_coordinator': coordinator,
                                       'current_source_budget_resource_ownership': preflight,
                                       'role_qualification': file('role-qualification'), 'worker_configuration': worker,
                                       'actual_assignment_validation_remains_required': True,
                                       'basis': 'retained readiness proof', 'readiness_only': True})


@pytest.fixture
def continuation_proposal(failed_startup, request):
    """Retain the real recovery disposition, then present a new synthetic package."""
    c = failed_startup
    c.old_decision_id = c.store.read('Q1')['decisions'][0]['decision_id']
    original_contract = json.loads(contract_raw(c))
    original_contract['max_repairs'] = 0
    budget = json.loads(sql(c.runtime, 'SELECT record FROM runtime_budget WHERE id=?',
                            (c.record['budget_id'],))[0][0])
    original_contract['product_budget'] = {
        key: budget[key] for key in ('budget_id', 'product_id', 'max_assignments', 'max_seconds', 'max_job_seconds')}
    original_contract['product_budget']['ledger_directory'] = str(c.store.directory.resolve())
    if getattr(request, 'param', None) == 'operational-metadata':
        optional_metadata(c, original_contract, current=False)
    sql(c.store.path, "UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(original_contract),))
    # Only fixture-owned sidecars are removed, before resealing and recovery.
    for path in dict.fromkeys(reference['path'] for reference in c.sidecars):
        Path(path).unlink()
    seal(c)
    c.failed_guard_raw = guard_raw(c)
    c.failed_guard_reference = retain(c.evidence_root / 'failed-guard-original.json',
                                     c.failed_guard_raw.encode(), immutable=True)
    c.recovery_result = recover(c, apply=True)
    c.recovery_audit = json.loads(sql(c.store.path, f'SELECT record FROM {recovery.TABLE} WHERE operation_id=?',
                                     (c.request['operation_id'],))[0][0])
    if getattr(request, 'param', None) == 'historical-sidecar':
        c.recovery_audit['evidence']['snapshot']['failure_sidecars'][0]['absent'] = False
        sql(c.store.path, f'UPDATE {recovery.TABLE} SET record=? WHERE operation_id=?',
            (canonical(c.recovery_audit), c.request['operation_id']))
    c.continuation_original_guard_raw = guard_raw(c)
    c.continuation_original_guard = json.loads(c.continuation_original_guard_raw)
    c.continuation_original_contract_raw = contract_raw(c)
    c.continuation_operation = 'continue-failed-startup-fixture'
    c.continuation_interval = {
        'start': json.loads(c.failed_guard_raw)['last_observation']['observed_at'],
        'end': c.recovery_result['terminal_receipt']['resource_limits']['observed_at'],
    }
    c.continuation_evidence = {
        'format': continuation.FORMAT,
        'work_id': 'Q1',
        'recovery_operation_id': c.request['operation_id'],
        'recovery_audit_digest': digest(c.recovery_audit),
        'original_guard_sha256': sha(c.continuation_original_guard_raw.encode()),
        'failed_guard': c.failed_guard_reference,
        'historical_breach': copy.deepcopy(c.continuation_original_guard['breach']),
        'unobserved_interval': c.continuation_interval,
        'acknowledged_limits': continuation.LIMITS,
    }
    c.continuation_evidence_reference = retain(
        c.evidence_root / 'continuation-evidence.json', c.continuation_evidence)
    c.continuation_binding = {
        'operation_id': c.continuation_operation,
        'original_guard_sha256': c.continuation_evidence['original_guard_sha256'],
        'evidence_sha256': c.continuation_evidence_reference['sha256'],
        'unobserved_interval': c.continuation_interval,
        'acknowledged_limits': continuation.LIMITS,
    }
    c.continuation_contract = json.loads(c.continuation_original_contract_raw)
    c.continuation_contract['revision'] += 1
    c.continuation_contract['max_assignments'] = 3
    c.continuation_contract['runtime_recipe']['resource_observation_continuation'] = c.continuation_binding
    c.continuation_contract['runtime_recipe_digest'] = digest(c.continuation_contract['runtime_recipe'])
    budget = json.loads(sql(c.runtime, 'SELECT record FROM runtime_budget WHERE id=?',
                            (c.record['budget_id'],))[0][0])
    c.continuation_contract['product_budget'] = {
        key: budget[key] for key in ('budget_id', 'product_id', 'max_assignments', 'max_seconds', 'max_job_seconds')}
    c.continuation_contract['product_budget']['ledger_directory'] = str(c.store.directory.resolve())
    if getattr(request, 'param', None) == 'operational-metadata':
        optional_metadata(c, c.continuation_contract, current=True)
    elif getattr(request, 'param', None) == 'four-assignments':
        c.continuation_contract['max_assignments'] = 4
    envelope = {key: copy.deepcopy(value) for key, value in c.continuation_contract.items()
                if key not in ('concept_revision', 'concept_digest')}
    current = c.store.read('Q1')
    package = current['concepts'][-1]['package']
    c.continuation_concept = c.store.present(
        'present-startup-continuation-fixture', 'Q1', current['revision'], package['package_id'],
        current['current_concept'] + 1,
        'SIMULATED approval of one continuation after the retained observation failure',
        ['Retain the failed interval, original charges, exact roots and original limits'],
        package['team_envelope'], envelope['authority'], execution_envelope=envelope)
    c.continuation_contract.update(
        concept_revision=c.continuation_concept['concept_revision'],
        concept_digest=c.continuation_concept['digest'])
    c.continuation_request = {
        'operation_id': c.continuation_operation,
        'work_id': 'Q1',
        'expected_work_revision': c.store.read('Q1')['revision'],
        'digest': c.continuation_concept['digest'],
        'recovery_operation_id': c.request['operation_id'],
        'decision_id': 'no-new-decision',
        'original_guard_sha256': c.continuation_evidence['original_guard_sha256'],
        'evidence_path': c.continuation_evidence_reference['path'],
        'evidence_sha256': c.continuation_evidence_reference['sha256'],
    }
    return c


@pytest.fixture
def approved_continuation(continuation_proposal):
    c = continuation_proposal
    c.continuation_decision = approve(c.store, c.continuation_concept,
                                      operation='approve-startup-continuation-fixture')
    c.continuation_request.update(decision_id=c.continuation_decision['decision_id'],
                                  expected_work_revision=c.store.read('Q1')['revision'])
    return c


def continuation_row(c):
    with c.store._connection() as db:
        return dict(db.execute(f'SELECT * FROM {continuation.TABLE} WHERE work_id=?', ('Q1',)).fetchone())


def assert_rejected_without_writes(c, *, match=None):
    before = tables(c.store.path), tables(c.runtime)
    with pytest.raises((Rejected, ValueError), match=match):
        acknowledge(c, apply=True)
    assert (tables(c.store.path), tables(c.runtime)) == before


def never_observe(*args, **kwargs):
    pytest.fail('This operation must not perform a new observation')


def test_preview_preserves_both_journals_and_does_not_observe(approved_continuation, monkeypatch):
    c = approved_continuation
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    before = c.store.path.read_bytes(), c.runtime.read_bytes()
    result = acknowledge(c)
    assert result['ready'] is True and result['applied'] is False
    assert result['accepted'] is result['grants_work'] is result['grants_time'] is False
    assert result['unobserved_interval'] == c.continuation_interval
    assert result['limits'] == continuation.LIMITS
    assert (c.store.path.read_bytes(), c.runtime.read_bytes()) == before
    assert continuation.TABLE not in tables(c.store.path)


@pytest.mark.parametrize('apply', ['false', 'true', 1, 0, None])
def test_apply_requires_an_explicit_boolean_before_claim_or_observation(approved_continuation, monkeypatch, apply):
    c = approved_continuation
    before = tables(c.store.path), tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with pytest.raises(Rejected, match='boolean'):
        acknowledge(c, apply=apply)
    assert (tables(c.store.path), tables(c.runtime)) == before
    assert continuation.TABLE not in tables(c.store.path)


@pytest.mark.parametrize('decision', ['missing', 'old', 'unverified'])
def test_only_the_new_exact_verified_approval_can_claim_a_continuation(approved_continuation, monkeypatch, decision):
    c = approved_continuation
    if decision == 'unverified':
        sql(c.store.path, 'UPDATE decision_readback SET status=? WHERE decision_id=?',
            ('blocked', c.continuation_request['decision_id']))
    else:
        c.continuation_request['decision_id'] = c.old_decision_id if decision == 'old' else 'no-saved-decision'
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert_rejected_without_writes(c)
    assert continuation.TABLE not in tables(c.store.path)


def test_pending_new_package_cannot_reuse_the_prior_approval(continuation_proposal, monkeypatch):
    c = continuation_proposal
    c.continuation_request['decision_id'] = c.old_decision_id
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert_rejected_without_writes(c)


def test_apply_archives_exact_history_and_preserves_caps_charges_and_unrelated_blockers(approved_continuation, monkeypatch):
    c = approved_continuation
    before_main, before_runtime = tables(c.store.path), tables(c.runtime)
    observed = []
    def observe(scope):
        observed.append(copy.deepcopy(scope))
        return copy.deepcopy(c.observed)
    monkeypatch.setattr(resource_limits, 'observe', observe)
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success' and result['applied'] is True
    assert result['accepted'] is result['grants_work'] is result['grants_time'] is False
    assert result['model_budget_reservations'] == 0 and len(observed) == 1
    row = continuation_row(c)
    assert row['state'] == 'success' and json.loads(row['outcome']) == result
    assert row['original_guard_raw'] == c.continuation_original_guard_raw
    assert row['original_contract_raw'] == c.continuation_original_contract_raw
    assert sha(row['original_guard_raw'].encode()) == result['original_guard_sha256']
    assert Path(c.failed_guard_reference['path']).read_bytes() == c.failed_guard_raw.encode()
    guard = json.loads(guard_raw(c))
    for key in ('baseline', 'scope', 'scope_digest', 'carry_forward', 'carry_forward_reference',
                'high_water_growth_bytes'):
        assert guard[key] == c.continuation_original_guard[key]
    assert guard['scope']['scan_seconds'] == 10
    assert guard['scope']['sample_interval_seconds'] == 2
    assert guard['scope']['max_observation_gap_seconds'] == 14
    assert guard['active_attempt'] is None and guard['breach'] is None
    assert guard['owner_generation'] == c.continuation_original_guard['owner_generation'] + 1
    assert guard['last_observation'] == c.observed
    assert guard['continuation']['historical_breach'] == c.continuation_original_guard['breach']
    assert guard['continuation']['reference'] == c.continuation_binding
    assert json.loads(contract_raw(c)) == c.continuation_contract
    assert result['registered_contract_digest'] == digest(json.loads(contract_raw(c)))
    assert result['admission_predecessor'] == 'startup-continuation:' + c.continuation_operation
    after_main, after_runtime = tables(c.store.path), tables(c.runtime)
    for table in before_runtime:
        if table != 'resource_limit':
            assert after_runtime[table] == before_runtime[table]
    assert dict(after_runtime['resource_limit'])['OTHER'] == dict(before_runtime['resource_limit'])['OTHER']
    for table in ('assignment', 'outbox', 'runtime_observation', 'owned_stop', 'concept', 'decision',
                  'decision_readback', 'worker_status', 'completion', 'resource', recovery.TABLE):
        assert after_main[table] == before_main[table]
    with c.store._connection() as db:
        run = dict(db.execute("SELECT * FROM lifecycle WHERE work_id='Q1'").fetchone())
        blockers = {r[0] for r in db.execute("SELECT code FROM technical_blocker WHERE work_id='Q1'")}
    assert run['execution'] == 'held' and run['admitted'] == 0 and run['control'] == 'none'
    assert 'startup_failure_recovered' not in blockers
    assert 'unrelated_obligation' in blockers
    assert all(not Path(reference['path']).exists() for reference in c.sidecars)


def test_a_lower_endpoint_cannot_reduce_previously_observed_high_water(approved_continuation):
    c = approved_continuation
    c.observed['files'] = copy.deepcopy(c.guard['baseline']['files'])
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success'
    assert result['resource_limits']['high_water_growth_bytes'] == c.continuation_original_guard['high_water_growth_bytes']
    assert json.loads(guard_raw(c))['high_water_growth_bytes'] == c.continuation_original_guard['high_water_growth_bytes']


def test_stable_success_replays_without_rescanning_and_blocks_a_second_work_claim(approved_continuation, monkeypatch):
    c = approved_continuation
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success'
    before = tables(c.store.path), tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert acknowledge(c, apply=True) == acknowledge(c) == result
    assert (tables(c.store.path), tables(c.runtime)) == before
    c.continuation_request['operation_id'] = 'different-continuation-same-work'
    assert_rejected_without_writes(c)
    assert len(sql(c.store.path, f'SELECT * FROM {continuation.TABLE}')) == 1


@pytest.mark.parametrize('mutation', ['guard', 'contract', 'outcome', 'delete'])
def test_success_archive_and_outcome_are_immutable(approved_continuation, mutation):
    c = approved_continuation
    assert acknowledge(c, apply=True)['status'] == 'success'
    before = continuation_row(c)
    statement = (f'DELETE FROM {continuation.TABLE}' if mutation == 'delete' else
                 f'UPDATE {continuation.TABLE} SET ' +
                 {'guard': 'original_guard_raw', 'contract': 'original_contract_raw', 'outcome': 'outcome'}[mutation] + "='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        sql(c.store.path, statement)
    assert continuation_row(c) == before


def test_future_resource_breach_remains_sticky_after_success_and_replay(approved_continuation):
    c = approved_continuation
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success'
    bound = c.continuation_original_guard['scope']
    c.observed['files']['output_roots']['new-large-file'] = bound['max_output_growth_bytes'] + 1
    first = resource_limits.sample_file(str(c.runtime), 'Q1', bound, 'future-assignment', force=True, activate=True)
    assert first['breach']['reason'] == 'max_output_growth_bytes'
    c.observed['files'] = copy.deepcopy(c.guard['baseline']['files'])
    later = resource_limits.sample_file(str(c.runtime), 'Q1', bound, 'future-assignment', force=True)
    assert later['breach'] == first['breach']
    with pytest.raises(Rejected, match='Retained resource guard violation'):
        resource_limits.enforce(later)
    assert acknowledge(c, apply=True) == result
    assert json.loads(guard_raw(c))['breach'] == first['breach']
    assert continuation_row(c)['original_guard_raw'] == c.continuation_original_guard_raw


@pytest.mark.parametrize('failure', ['unavailable', 'output-cap', 'free-space'])
def test_failed_endpoint_retains_original_guard_contract_and_nonretryable_failure(approved_continuation, monkeypatch, failure):
    c = approved_continuation
    before_runtime = tables(c.runtime)
    if failure == 'unavailable':
        def unavailable(scope):
            raise Rejected('resource_observation', 'SIMULATED unavailable observation')
        monkeypatch.setattr(resource_limits, 'observe', unavailable)
    elif failure == 'output-cap':
        c.observed['files']['output_roots']['too-large'] = 100000
    else:
        c.observed['available_bytes']['volume'] = 1
    result = acknowledge(c, apply=True)
    assert result['status'] == 'failed' and result['applied'] is False
    assert result['grants_work'] is result['grants_time'] is False
    assert result['observed_resource_limits']['breach'] is not None
    assert tables(c.runtime) == before_runtime
    assert contract_raw(c) == c.continuation_original_contract_raw
    row = continuation_row(c)
    assert row['state'] == 'failed' and json.loads(row['outcome']) == result
    assert row['original_guard_raw'] == c.continuation_original_guard_raw
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert acknowledge(c, apply=True) == acknowledge(c) == result
    c.continuation_request['operation_id'] = 'retry-after-failed-endpoint'
    assert_rejected_without_writes(c)


def test_interrupted_claim_stays_reserved_and_never_restarts_the_scan(approved_continuation, monkeypatch):
    c = approved_continuation
    class Interrupted(BaseException):
        pass
    def interrupt(stage):
        if stage == 'continuation_after_claim':
            raise Interrupted('SIMULATED abrupt owner exit')
    c.lifecycle._fault = interrupt
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with pytest.raises(Interrupted):
        acknowledge(c, apply=True)
    row = continuation_row(c)
    assert row['state'] == 'reserved' and row['outcome'] is None
    assert row['original_guard_raw'] == guard_raw(c) == c.continuation_original_guard_raw
    assert row['original_contract_raw'] == contract_raw(c) == c.continuation_original_contract_raw
    c.lifecycle._fault = lambda stage: None
    result = acknowledge(c, apply=True)
    assert result['status'] == 'held' and result['applied'] is False
    assert continuation_row(c) == row
    c.continuation_request['operation_id'] = 'second-claim-after-owner-exit'
    assert_rejected_without_writes(c)


def test_unavailable_failure_persistence_keeps_the_reserved_claim(approved_continuation, monkeypatch):
    c = approved_continuation
    def fail(stage):
        if stage == 'continuation_after_claim':
            raise RuntimeError('SIMULATED failure after durable claim')
    def unavailable(*args, **kwargs):
        raise OSError('SIMULATED failure journal unavailable')
    c.lifecycle._fault = fail
    monkeypatch.setattr(continuation, '_failed', unavailable)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with pytest.raises(Rejected, match='durably reserved and held'):
        acknowledge(c, apply=True)
    assert continuation_row(c)['state'] == 'reserved'
    assert guard_raw(c) == c.continuation_original_guard_raw
    assert contract_raw(c) == c.continuation_original_contract_raw
    c.lifecycle._fault = lambda stage: None
    assert acknowledge(c, apply=True)['status'] == 'held'


def test_failure_after_updates_rolls_back_guard_and_contract_together(approved_continuation):
    c = approved_continuation
    before_runtime = tables(c.runtime)
    def fail(stage):
        if stage == 'continuation_after_updates':
            raise RuntimeError('SIMULATED failure before atomic commit')
    c.lifecycle._fault = fail
    result = acknowledge(c, apply=True)
    assert result['status'] == 'failed'
    assert tables(c.runtime) == before_runtime
    assert contract_raw(c) == c.continuation_original_contract_raw
    assert continuation_row(c)['state'] == 'failed'
    assert sql(c.store.path, "SELECT COUNT(*) FROM technical_blocker WHERE work_id='Q1' AND code='startup_failure_recovered'")[0][0] == 1


def test_expired_monotonic_window_fails_even_when_wall_time_does_not_advance(approved_continuation, monkeypatch):
    c = approved_continuation
    before_runtime = tables(c.runtime)
    wall = continuation.time.time()
    def expire(stage):
        if stage == 'continuation_before_commit':
            later = continuation.time.monotonic() + 15
            monkeypatch.setattr(continuation.time, 'monotonic', lambda: later)
    c.lifecycle._fault = expire
    result = acknowledge(c, apply=True)
    assert continuation.time.time() == wall
    assert result['status'] == 'failed' and result['applied'] is False
    assert tables(c.runtime) == before_runtime
    assert guard_raw(c) == c.continuation_original_guard_raw
    assert contract_raw(c) == c.continuation_original_contract_raw
    assert continuation_row(c)['state'] == 'failed'


@pytest.mark.parametrize('target', ['packet', 'failed-guard', 'capsule'])
def test_changed_final_evidence_rolls_back_guard_contract_and_revision(approved_continuation, target):
    c = approved_continuation
    before_runtime = tables(c.runtime)
    before_revision = c.store.read('Q1')['revision']
    reference = {'packet': c.continuation_evidence_reference,
                 'failed-guard': c.failed_guard_reference,
                 'capsule': c.recipe['resource_limits_carry_forward']}[target]
    path = Path(reference['path'])
    def change(stage):
        if stage == 'continuation_after_updates':
            path.chmod(0o600)
            path.write_bytes(path.read_bytes() + b'changed after atomic updates')
            if target == 'failed-guard':
                path.chmod(0o400)
    c.lifecycle._fault = change
    result = acknowledge(c, apply=True)
    assert result['status'] == 'failed' and result['applied'] is False
    assert tables(c.runtime) == before_runtime
    assert guard_raw(c) == c.continuation_original_guard_raw
    assert contract_raw(c) == c.continuation_original_contract_raw
    assert c.store.read('Q1')['revision'] == before_revision
    assert continuation_row(c)['state'] == 'failed'


@pytest.mark.parametrize('journal', ['main', 'runtime'])
@pytest.mark.parametrize('arrival', ['before-claim', 'after-scan'])
def test_any_failure_sidecar_prevents_continuation_and_is_retained(approved_continuation, monkeypatch, journal, arrival):
    c = approved_continuation
    database = c.store.path if journal == 'main' else c.runtime
    path = resource_limits._failure_path(database, 'Q1')
    value = {'work_id': 'Q1', 'scope_digest': c.guard['scope_digest'],
             'breach': c.guard['breach'],
             'high_water_growth_bytes': c.continuation_original_guard['high_water_growth_bytes']}
    if arrival == 'before-claim':
        retain(path, value)
        monkeypatch.setattr(resource_limits, 'observe', never_observe)
        assert_rejected_without_writes(c)
        assert continuation.TABLE not in tables(c.store.path)
    else:
        def arrive(stage):
            if stage == 'continuation_after_scan':
                retain(path, value)
        c.lifecycle._fault = arrive
        result = acknowledge(c, apply=True)
        assert result['status'] == 'failed'
        assert continuation_row(c)['state'] == 'failed'
    assert path.read_bytes() == canonical(value).encode()
    assert guard_raw(c) == c.continuation_original_guard_raw
    assert contract_raw(c) == c.continuation_original_contract_raw


@pytest.mark.parametrize('target', ['packet', 'failed-guard'])
def test_changed_evidence_cannot_acknowledge_the_failed_interval(approved_continuation, monkeypatch, target):
    c = approved_continuation
    path = Path(c.continuation_evidence_reference['path'] if target == 'packet' else c.failed_guard_reference['path'])
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b'changed')
    if target == 'failed-guard':
        path.chmod(0o400)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('method', ['recorded_scope', 'prepare'])
@pytest.mark.parametrize('reference', ['absent', 'mismatched'])
def test_installed_guard_rejects_an_unapproved_or_different_recipe_reference(approved_continuation, monkeypatch, method, reference):
    c = approved_continuation
    assert acknowledge(c, apply=True)['status'] == 'success'
    recipe = copy.deepcopy(c.continuation_contract['runtime_recipe'])
    if reference == 'absent':
        recipe.pop('resource_observation_continuation')
    else:
        recipe['resource_observation_continuation']['operation_id'] = 'unapproved-other-continuation'
    before = tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with closing(sqlite3.connect(c.runtime)) as db, db:
        with pytest.raises(Rejected, match='exact newly approved continuation reference'):
            getattr(resource_limits, method)(db, 'Q1', recipe)
    assert tables(c.runtime) == before


@pytest.mark.parametrize('method', ['recorded_scope', 'prepare'])
def test_installed_guard_accepts_only_its_exact_new_recipe_without_rebaselining(approved_continuation, monkeypatch, method):
    c = approved_continuation
    assert acknowledge(c, apply=True)['status'] == 'success'
    recipe = c.continuation_contract['runtime_recipe']
    before = tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with closing(sqlite3.connect(c.runtime)) as db, db:
        result = getattr(resource_limits, method)(db, 'Q1', recipe)
    bound = result['scope'] if method == 'prepare' else result
    assert bound == c.continuation_original_guard['scope']
    assert tables(c.runtime) == before


@pytest.mark.parametrize('method', ['recorded_scope', 'prepare'])
@pytest.mark.parametrize('reference', ['retained', 'omitted'])
def test_installed_guard_cannot_disable_observation_by_omitting_limits(approved_continuation, monkeypatch, method, reference):
    c = approved_continuation
    assert acknowledge(c, apply=True)['status'] == 'success'
    recipe = copy.deepcopy(c.continuation_contract['runtime_recipe'])
    recipe.pop('resource_limits')
    if reference == 'omitted':
        recipe.pop('resource_observation_continuation')
    before = tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with closing(sqlite3.connect(c.runtime)) as db, db:
        with pytest.raises(Rejected):
            getattr(resource_limits, method)(db, 'Q1', recipe)
    assert tables(c.runtime) == before


@pytest.mark.parametrize('method', ['recorded_scope', 'prepare'])
def test_continuation_reference_cannot_create_a_replacement_for_a_missing_guard(approved_continuation, monkeypatch, method):
    c = approved_continuation
    assert acknowledge(c, apply=True)['status'] == 'success'
    sql(c.runtime, "DELETE FROM resource_limit WHERE work_id='Q1'")
    before = tables(c.runtime)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    with closing(sqlite3.connect(c.runtime)) as db, db:
        with pytest.raises(Rejected):
            getattr(resource_limits, method)(db, 'Q1', c.continuation_contract['runtime_recipe'])
    assert tables(c.runtime) == before
    assert not sql(c.runtime, "SELECT 1 FROM resource_limit WHERE work_id='Q1'")


def test_later_identical_scan_failure_is_a_new_sticky_breach(approved_continuation, monkeypatch):
    c = approved_continuation
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success'
    original = c.continuation_original_guard['breach']
    def unavailable(scope):
        raise Rejected(original['error_code'], original['error_message'])
    monkeypatch.setattr(resource_limits, 'observe', unavailable)
    bound = c.continuation_original_guard['scope']
    first = resource_limits.sample_file(str(c.runtime), 'Q1', bound, 'later-scan-attempt', force=True, activate=True)
    assert first['breach']['reason'] == 'observation_failed'
    assert first['breach']['error_code'] == original['error_code']
    assert first['breach']['error_message'] == original['error_message']
    assert first['breach']['observed_at'] > original['observed_at']
    monkeypatch.setattr(resource_limits, 'observe', lambda scope: copy.deepcopy(c.observed))
    later = resource_limits.sample_file(str(c.runtime), 'Q1', bound, 'later-scan-attempt', force=True)
    assert later['breach'] == first['breach']
    with pytest.raises(Rejected, match='Retained resource guard violation'):
        resource_limits.enforce(later)
    assert acknowledge(c, apply=True) == result
    assert json.loads(guard_raw(c))['breach'] == first['breach']


@pytest.mark.parametrize('continuation_proposal', ['historical-sidecar'], indirect=True)
def test_current_absence_cannot_erase_a_sidecar_recorded_by_the_recovery(approved_continuation, monkeypatch):
    c = approved_continuation
    assert all(not Path(reference['path']).exists() for reference in c.sidecars)
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert_rejected_without_writes(c, match='Historical failure sidecars must also have been positively absent')
    assert continuation.TABLE not in tables(c.store.path)


@pytest.mark.parametrize('continuation_proposal', ['operational-metadata'], indirect=True)
def test_new_approval_can_refresh_operational_proofs_without_changing_execution_controls(approved_continuation):
    c = approved_continuation
    old = json.loads(c.continuation_original_contract_raw)
    assert continuation._contract_compatible(old, c.continuation_contract)
    assert acknowledge(c)['ready'] is True
    result = acknowledge(c, apply=True)
    assert result['status'] == 'success' and result['applied'] is True
    saved = json.loads(contract_raw(c))
    assert saved == c.continuation_contract
    assert saved['evidence']['worker_configuration'] == saved['worker_configuration']
    assert saved['worker_launch_binding']['runtime_recipes']['recipe_digest'] == digest(saved['runtime_recipe'])
    assert saved['baseline_provenance']['git_ref'] == old['baseline_provenance']['git_ref']
    assert saved['resource_accounting_binding']['exact_remaining_limits'] == old['resource_accounting_binding']['exact_remaining_limits']
    assert continuation_row(c)['original_contract_raw'] == c.continuation_original_contract_raw


@pytest.mark.parametrize('continuation_proposal', ['operational-metadata'], indirect=True)
def test_unchanged_prior_evidence_summary_remains_valid(approved_continuation):
    c = approved_continuation
    old = json.loads(c.continuation_original_contract_raw)
    proposed = copy.deepcopy(c.continuation_contract)
    proposed['evidence']['affected_verification'] = copy.deepcopy(old['evidence']['affected_verification'])
    assert continuation._contract_compatible(old, proposed)


@pytest.mark.parametrize('continuation_proposal', ['operational-metadata'], indirect=True)
@pytest.mark.parametrize('path,value', [
    (('authority',), 'Wider execution authority'),
    (('feature_sublimit', 'max_active_elapsed_seconds'), 181),
    (('model',), 'different-model'),
    (('effort',), 'different-effort'),
    (('behavior_delta',), 'Different feature'),
    (('execution_launch_plan', 'seconds_each'), 61),
    (('execution_launch_plan', 'feature_ceiling_attempts'), 4),
    (('execution_launch_plan', 'repair_cycles'), 1),
    (('worker_launch_binding', 'feature_owner_chat'), 'different-owner'),
    (('worker_launch_binding', 'argv', 0), 'different-worker'),
    (('worker_launch_binding', 'argv', 2), '/different/state'),
    (('worker_launch_binding', 'cwd'), '/different/cwd'),
    (('worker_launch_binding', 'single_owner_chat'), 'different-owner'),
    (('worker_launch_binding', 'loop', 'max_cycles'), 11),
    (('worker_launch_binding', 'started'), True),
    (('worker_launch_binding', 'runtime_recipes', 'shape'), 'detached workers'),
    (('worker_launch_binding', 'runtime_recipes', 'work_ids'), ['OTHER']),
    (('baseline_provenance', 'inventory_digest'), 'sha256:' + 'f' * 64),
    (('baseline_provenance', 'git_ref'), 'f' * 40),
    (('resource_accounting_binding', 'capsule', 'sha256'), 'f' * 64),
    (('resource_accounting_binding', 'exact_remaining_limits', 'max_output_growth_bytes'), 1025),
    (('resource_accounting_binding', 'min_free_bytes'), 99),
    (('resource_accounting_binding', 'semantics'), 'No historical uncertainty'),
    (('resource_accounting_binding', 'post_export_growth_bytes_at_preflight', 'output_roots'), 19),
    (('resource_accounting_binding', 'post_export_growth_bytes_at_preflight', 'output_roots'), 1025),
    (('resource_accounting_binding', 'remaining_after_observed_post_export_growth_bytes', 'output_roots'), 1024),
    (('resource_accounting_binding', 'minimum_available_bytes_at_preflight'), 99),
    (('runtime_recipe', 'model_write_scope'), 'whole-source-tree'),
    (('evidence', 'historical_native_candidate', 'sha256'), 'f' * 64),
    (('evidence', 'historical_native_source_digest'), 'sha256:' + 'f' * 64),
    (('evidence', 'native_baseline_verification', 'sha256'), 'f' * 64),
    (('evidence', 'resource_export_verification', 'sha256'), 'f' * 64),
    (('evidence', 'historical_design_evidence', 'aos_source_digest'), 'sha256:' + 'f' * 64),
    (('evidence', 'model_permission_mode', 'value'), 'workspace-write'),
    (('prerequisite_evidence_binding', 'readiness_only'), False),
])
def test_operational_refresh_cannot_change_frozen_controls_or_accounting(approved_continuation, path, value):
    c = approved_continuation
    old = json.loads(c.continuation_original_contract_raw)
    proposed = copy.deepcopy(c.continuation_contract)
    target = proposed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert not continuation._contract_compatible(old, proposed)


@pytest.mark.parametrize('continuation_proposal', ['operational-metadata'], indirect=True)
@pytest.mark.parametrize('path', [
    (), ('baseline_provenance',), ('resource_accounting_binding',), ('evidence',),
    ('evidence', 'model_permission_mode'), ('worker_launch_binding',),
    ('worker_launch_binding', 'runtime_recipes'), ('prerequisite_evidence_binding',),
])
def test_operational_refresh_rejects_unknown_added_keys(approved_continuation, path):
    c = approved_continuation
    old = json.loads(c.continuation_original_contract_raw)
    proposed = copy.deepcopy(c.continuation_contract)
    target = proposed
    for part in path:
        target = target[part]
    target['unknown_extension'] = {'new_authority': True}
    assert not continuation._contract_compatible(old, proposed)


@pytest.mark.parametrize('continuation_proposal', ['operational-metadata'], indirect=True)
@pytest.mark.parametrize('binding', ['worker', 'preflight', 'coordinator', 'recipe-file', 'recipe-digest', 'argv-map'])
def test_refreshed_operational_proofs_must_agree_across_the_package(approved_continuation, binding):
    c = approved_continuation
    old = json.loads(c.continuation_original_contract_raw)
    proposed = copy.deepcopy(c.continuation_contract)
    if binding in ('worker', 'preflight', 'coordinator'):
        key = {'worker': 'worker_configuration', 'preflight': 'final_readiness_preflight',
               'coordinator': 'actual_folder_coordinator'}[binding]
        proposed['evidence'][key] = {**proposed['evidence'][key], 'sha256': 'f' * 64}
    elif binding == 'recipe-file':
        proposed['worker_launch_binding']['runtime_recipes']['recipe']['sha256'] = 'f' * 64
    elif binding == 'recipe-digest':
        proposed['worker_launch_binding']['runtime_recipes']['recipe_digest'] = 'sha256:' + 'f' * 64
    else:
        proposed['worker_launch_binding']['argv'][4] = '/different/runtime-recipes.json'
    assert not continuation._contract_compatible(old, proposed)


@pytest.mark.parametrize('continuation_proposal', ['four-assignments'], indirect=True)
def test_new_approval_cannot_grant_a_fourth_assignment(approved_continuation, monkeypatch):
    c = approved_continuation
    monkeypatch.setattr(resource_limits, 'observe', never_observe)
    assert_rejected_without_writes(c)
    assert continuation.TABLE not in tables(c.store.path)
