"""Explicit startup allowance is additional to the complete reviewed delivery path."""
import copy
import sqlite3

import pytest
from pydantic import ValidationError

from agentic_tools.aos import budgets
from agentic_tools.aos.coordinator import ExecutionEnvelope
from agentic_tools.aos.store import Rejected
from test_aos_finish_capacity import allowance, charge, journal, rows
from test_aos_lifecycle import contract
from test_aos_project import setup as project_setup  # pytest fixture, no host execution
from test_aos_startup_retry import POLICY, setup


@pytest.mark.parametrize('repairs', [0, 1, 2])
@pytest.mark.parametrize('retries', [0, 1, 2])
def test_capacity_is_two_roles_plus_paired_corrections_plus_startup_retries(tmp_path, repairs, retries):
    jobs = 2 + 2 * repairs + retries
    budget = allowance(tmp_path, max_assignments=jobs, max_seconds=jobs * 60)
    result = budgets.finish_capacity(budget, max_repairs=repairs, max_startup_retries=retries,
                                     max_seconds=60, max_assignments=jobs)
    assert result['ready'] and result['required_assignments'] == jobs
    assert result['required_seconds'] == jobs * 60 and result['reservation_performed'] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('retries', [-1, 3, True, 1.0, '1', None])
def test_invalid_startup_allowance_never_passes_pure_capacity(tmp_path, retries):
    result = budgets.finish_capacity(allowance(tmp_path), max_repairs=0, max_startup_retries=retries,
                                     max_seconds=60, max_assignments=4)
    assert not result['ready'] and result['blockers']


@pytest.mark.parametrize('changes', [
    {'max_assignments': 4}, {'max_seconds': 299}, {'max_job_seconds': 59},
])
def test_shared_capacity_must_include_startup_slot_before_launch(tmp_path, changes):
    budget = allowance(tmp_path, **{'max_assignments': 5, 'max_seconds': 300, **changes})
    result = budgets.finish_capacity(budget, max_repairs=1, max_startup_retries=1,
                                     max_seconds=60, max_assignments=5)
    assert not result['ready'] and result['required_assignments'] == 5


def test_retained_retry_failure_remains_charged_in_readonly_preview(tmp_path):
    path = tmp_path / 'codex-runtime.sqlite3'
    source = tmp_path / 'source'
    budget = allowance(tmp_path, max_assignments=4, max_seconds=240)
    with journal(path) as db:
        charge(db, budget, source, 'retained-startup-failure')
        before = rows(db)
    encoded = path.read_bytes()
    result = budgets.read_finish_capacity(path, budget, source=str(source), max_repairs=0,
        max_startup_retries=1, max_seconds=60, max_assignments=3)
    assert result['ready'] and result['attempts_reserved'] == 1 and result['seconds_reserved'] == 60
    assert result['required_assignments'] == 3 and path.read_bytes() == encoded
    with sqlite3.connect(path) as db:
        assert rows(db) == before


def test_lifecycle_capacity_checks_startup_allowance_before_first_intent(tmp_path):
    store, lifecycle, _, _, _ = setup(tmp_path, retries=1, repairs=1, jobs=4, admitted=False)
    result = lifecycle.admit('admit', 'RETRY')
    assert result['execution'] == 'held'
    assert any('Lifecycle assignment' in message for message in result['blockers'])
    assert lifecycle.assignments() == [] and lifecycle.snapshot('RETRY')['resources'] == []
    assert not (store.directory / 'codex-runtime.sqlite3').exists()


@pytest.mark.parametrize('policy', [None, 'legacy-per-file', 'wrong'])
def test_positive_startup_allowance_requires_explicit_isolated_policy(tmp_path, policy):
    bounds = contract({'digest': 'unused'}, max_startup_retries=1)
    if policy is not None:
        bounds['execution_policy'] = policy
    with pytest.raises(ValidationError, match='isolated-checkout-v1'):
        ExecutionEnvelope.model_validate(bounds)
    with pytest.raises(Rejected, match='startup retries'):
        setup(tmp_path, retries=1, policy=policy)


@pytest.mark.parametrize('retries', [-1, 3, True, 1.0, '1', None])
def test_envelope_and_lifecycle_reject_invalid_retry_count(tmp_path, retries):
    bounds = contract({'digest': 'unused'}, execution_policy=POLICY, max_startup_retries=retries)
    with pytest.raises(ValidationError):
        ExecutionEnvelope.model_validate(bounds)
    # Pass a normal overall capacity so only the explicit retry field is invalid.
    with pytest.raises(Rejected, match='startup retries'):
        setup(tmp_path, retries=retries, jobs=4)


def test_old_envelope_defaults_to_no_retry_without_adding_policy():
    envelope = ExecutionEnvelope.model_validate(contract({'digest': 'unused'}))
    assert envelope.max_startup_retries == 0
    assert getattr(envelope, 'execution_policy', None) is None


def test_project_capacity_includes_retry_before_any_readiness(project_setup):
    p = project_setup
    bounds = p.request['concept']['execution_envelope']
    bounds.update(execution_policy=POLICY, max_startup_retries=1)
    p.save()
    result = p.run()
    assert not result['ready'] and result['blocker']['code'] == 'project_finish_capacity'
    assert result['finish_capacity']['required_assignments'] == 5
    assert p.calls == [] and not (p.state / 'codex-runtime.sqlite3').exists()
    bounds['max_assignments'] = 5
    bounds['product_budget'].update(max_assignments=5, max_seconds=300)
    p.save()
    result = p.run()
    assert result['ready'], result['blocker']
    assert result['finish_capacity']['required_assignments'] == 5 and len(p.calls) == 1
    assert result['concept']['execution_envelope']['max_startup_retries'] == 1


def test_registered_retry_limit_cannot_exceed_saved_human_envelope(tmp_path):
    store, lifecycle, _, _, _ = setup(tmp_path, retries=1, admitted=False)
    with store._transaction() as db:
        run, bounds = lifecycle._run(db, 'RETRY')
        changed = copy.deepcopy(bounds)
        changed['max_startup_retries'] = 2
        assert 'Execution contract exceeds approved max_startup_retries' in lifecycle._approved_envelope_errors(db, 'RETRY', changed)
