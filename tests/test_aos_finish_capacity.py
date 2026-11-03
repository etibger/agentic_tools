"""Prospective finish capacity never refunds or reserves historical work."""
import json
import sqlite3

import pytest

from agentic_tools.aos import budgets
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from test_aos_lifecycle import approve, contract, setup
from test_aos_store import TEAM

POLICY = 'isolated-checkout-v1'


def allowance(directory, **changes):
    return {'budget_id': 'finish-product-v1', 'kind': 'product', 'product_id': 'finish-product',
            'ledger_directory': str(directory.resolve()), 'max_assignments': 4,
            'max_seconds': 240, 'max_job_seconds': 60, **changes}


def preview(budget, **changes):
    return budgets.finish_capacity(budget, **{'max_repairs': 1, 'max_seconds': 60,
        'max_assignments': 4, **changes})


def journal(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE attempt(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
    budgets.initialize(db)
    db.commit()
    return db


def charge(db, budget, source, aid, *, following=0, seconds=60, phase='failed'):
    with db:
        db.execute('BEGIN IMMEDIATE')
        budgets.reserve(db, budget, {'source': str(source), 'max_seconds': seconds}, following_jobs=following)
        db.execute('INSERT INTO attempt VALUES(?,?)', (aid, canonical({
            'budget_id': budget['budget_id'], 'budget_seconds': seconds, 'phase': phase})))


def rows(db):
    return {table: list(db.execute('SELECT * FROM ' + table + ' ORDER BY 1'))
            for table in ('attempt', 'runtime_budget', 'product_budget_scope')}


@pytest.mark.parametrize('repairs,jobs', [(0, 2), (1, 4), (2, 6)])
def test_each_correction_keeps_an_independent_reassessment(tmp_path, repairs, jobs):
    budget = allowance(tmp_path, max_assignments=jobs, max_seconds=jobs * 60)
    result = preview(budget, max_repairs=repairs, max_assignments=jobs)
    assert result['ready'] and not result['blockers']
    assert result['required_assignments'] == jobs
    assert result['required_seconds'] == jobs * 60
    assert result['reservation_performed'] is False


def test_failed_unknown_and_legacy_attempts_keep_full_charges(tmp_path):
    budget = allowance(tmp_path, max_assignments=6, max_seconds=420)
    attempts = [{'budget_id': budget['budget_id'], 'budget_seconds': 60, 'phase': 'failed'},
                {'budget_id': budget['budget_id'], 'budget_seconds': 120, 'phase': 'unknown'},
                {'budget_id': 'unrelated', 'budget_seconds': 999}]
    result = preview(budget, attempts=attempts)
    assert result['ready'] and result['attempts_reserved'] == 2 and result['seconds_reserved'] == 180
    qualification = {**budget, 'budget_id': budgets.QUALIFICATION_ID, 'kind': 'qualification',
                     'max_seconds': 600, 'max_job_seconds': 300}
    result = preview(qualification, attempts=[{'phase': 'legacy-unknown'}])
    assert result['ready'] and result['seconds_reserved'] == 300


@pytest.mark.parametrize('budget_change,scope,fragment', [
    ({'max_assignments': 3}, {}, 'Shared attempt'),
    ({'max_seconds': 239}, {}, 'Shared reserved-time'),
    ({}, {'max_assignments': 3}, 'Lifecycle assignment'),
    ({}, {'lifecycle_assignments': 1}, 'Lifecycle assignment'),
    ({'max_job_seconds': 59}, {}, 'per-job'),
])
def test_each_independent_capacity_dimension_can_hold(tmp_path, budget_change, scope, fragment):
    result = preview(allowance(tmp_path, **budget_change), **scope)
    assert not result['ready'] and any(fragment in item for item in result['blockers'])
    assert result['reservation_performed'] is False


@pytest.mark.parametrize('scope', [
    {'max_repairs': True}, {'max_repairs': 3}, {'max_seconds': 0},
    {'max_assignments': False}, {'lifecycle_assignments': -1},
    {'attempts': [{'budget_id': 'finish-product-v1', 'budget_seconds': -1}]},
    {'attempts': [{'budget_id': 'finish-product-v1', 'budget_seconds': True}]},
])
def test_invalid_accounting_never_becomes_available_capacity(tmp_path, scope):
    result = preview(allowance(tmp_path), **scope)
    assert not result['ready'] and result['blockers']


def test_absent_journal_preview_does_not_create_a_runtime(tmp_path):
    path = tmp_path / 'codex-runtime.sqlite3'
    result = budgets.read_finish_capacity(path, allowance(tmp_path), source=str(tmp_path / 'source'),
                                          max_repairs=1, max_seconds=60, max_assignments=4)
    assert result['ready']
    assert list(tmp_path.iterdir()) == []


def test_existing_preview_preserves_all_rows_bytes_and_failed_reservations(tmp_path):
    path = tmp_path / 'codex-runtime.sqlite3'
    budget = allowance(tmp_path, max_assignments=5, max_seconds=300)
    source = tmp_path / 'source'
    with journal(path) as db:
        charge(db, budget, source, 'failed-launch')
        before = rows(db)
    encoded, modified = path.read_bytes(), path.stat().st_mtime_ns
    result = budgets.read_finish_capacity(path, budget, source=str(source),
                                          max_repairs=1, max_seconds=60, max_assignments=4)
    assert result['ready'] and result['attempts_reserved'] == 1 and result['seconds_reserved'] == 60
    assert path.read_bytes() == encoded and path.stat().st_mtime_ns == modified
    with sqlite3.connect(path) as db:
        assert rows(db) == before


@pytest.mark.parametrize('change', [
    {'budget_id': 'renamed'}, {'max_assignments': 6}, {'max_seconds': 360}, {'product_id': 'renamed'},
])
def test_read_only_preview_cannot_rename_or_enlarge_saved_allowance(tmp_path, change):
    path = tmp_path / 'codex-runtime.sqlite3'
    budget = allowance(tmp_path, max_assignments=5, max_seconds=300)
    source = tmp_path / 'source'
    with journal(path) as db:
        charge(db, budget, source, 'retained')
        before = rows(db)
    result = budgets.read_finish_capacity(path, {**budget, **change}, source=str(source),
                                          max_repairs=0, max_seconds=60, max_assignments=2)
    assert not result['ready'] and any('accounting unavailable' in item for item in result['blockers'])
    with sqlite3.connect(path) as db:
        assert rows(db) == before


def test_corrupt_or_wrong_journal_is_a_hold_without_repair(tmp_path):
    path = tmp_path / 'codex-runtime.sqlite3'
    path.write_bytes(b'not a sqlite database')
    scope = dict(source=str(tmp_path / 'source'), max_repairs=0, max_seconds=60, max_assignments=2)
    result = budgets.read_finish_capacity(path, allowance(tmp_path), **scope)
    assert not result['ready'] and path.read_bytes() == b'not a sqlite database'
    result = budgets.read_finish_capacity(tmp_path / 'different.sqlite3', allowance(tmp_path), **scope)
    assert not result['ready'] and not (tmp_path / 'different.sqlite3').exists()


def test_following_jobs_are_headroom_not_extra_reservations(tmp_path):
    budget = allowance(tmp_path)
    with journal(tmp_path / 'codex-runtime.sqlite3') as db:
        for number, following in enumerate((3, 2, 1, 0)):
            charge(db, budget, tmp_path / 'source', 'job-' + str(number), following=following)
            receipt = budgets.receipt(db, tmp_path, budget['budget_id'])
            assert receipt['attempts_reserved'] == number + 1
            assert receipt['seconds_reserved'] == (number + 1) * 60


def test_atomic_reserve_refuses_to_strand_assessment_but_keeps_legacy_default(tmp_path):
    budget = allowance(tmp_path, max_assignments=2, max_seconds=120)
    with journal(tmp_path / 'codex-runtime.sqlite3') as db:
        charge(db, budget, tmp_path / 'source', 'retained')
        before = rows(db)
        with pytest.raises(Rejected) as error:
            charge(db, budget, tmp_path / 'source', 'cannot-review', following=1)
        assert error.value.code == 'runtime_capacity' and rows(db) == before
        charge(db, budget, tmp_path / 'source', 'legacy-one-job')
        assert budgets.receipt(db, tmp_path, budget['budget_id'])['attempts_reserved'] == 2


def test_atomic_reservation_rechecks_capacity_after_a_successful_preview(tmp_path):
    budget = allowance(tmp_path)
    path = tmp_path / 'codex-runtime.sqlite3'
    scope = dict(source=str(tmp_path / 'source'), max_repairs=1, max_seconds=60, max_assignments=4)
    with journal(path) as db:
        assert budgets.read_finish_capacity(path, budget, **scope)['ready']
        charge(db, budget, tmp_path / 'source', 'competing-retained')
        with pytest.raises(Rejected, match='following jobs'):
            charge(db, budget, tmp_path / 'source', 'first-writer', following=3)
        assert budgets.receipt(db, tmp_path, budget['budget_id'])['attempts_reserved'] == 1


def prepared(tmp_path, *, jobs=4, repairs=1, budget_jobs=4, budget_seconds=240, policy=POLICY):
    source = tmp_path / 'source'
    source.mkdir()
    store = Store(tmp_path / 'state', (source,))
    bounds = contract({'digest': 'not-self-referential'}, 'feature-change',
                      max_assignments=jobs, max_repairs=repairs, max_seconds=60)
    bounds.pop('concept_revision'); bounds.pop('concept_digest')
    recipe = {'source': str(source), 'max_seconds': 60, 'execution_policy': policy}
    budget = allowance(store.directory, max_assignments=budget_jobs, max_seconds=budget_seconds)
    budget.pop('kind')
    bounds.update(execution_policy=policy, runtime_recipe=recipe, runtime_recipe_digest=digest(recipe), product_budget=budget)
    team = [{**TEAM[0], 'name': name, 'role': role}
            for name, role in [('writer', 'Implementer'), ('assessor', 'Independent assessor')]]
    store.introduce('intro', 'FINISH', 'Simulated admission only', ['source'], team)
    current = store.present('present', 'FINISH', 1, 'SIMULATED-DI', 1, 'Simulated capacity check',
                            ['No model or native commands'], team, bounds['authority'], execution_envelope=bounds)
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'FINISH', {**bounds, 'concept_revision': 1, 'concept_digest': current['digest']})
    approve(store, current, 'FINISH')
    return store, lifecycle, current, budget, source


def test_opt_in_admission_requires_complete_path_before_creating_intent(tmp_path):
    store, lifecycle, current, _, _ = prepared(tmp_path)
    result = lifecycle.admit('admit', 'FINISH', expected_work_revision=store.read('FINISH')['revision'],
                             expected_digest=current['digest'])
    assert result['execution'] == 'eligible'
    assert len(lifecycle.assignments()) == 1
    assert not (store.directory / 'codex-runtime.sqlite3').exists()


@pytest.mark.parametrize('settings,fragment', [
    ({'jobs': 3}, 'Lifecycle assignment'),
    ({'budget_jobs': 3}, 'Shared attempt'),
    ({'budget_seconds': 239}, 'Shared reserved-time'),
])
def test_capacity_hold_is_durable_without_resources_intents_or_reservations(tmp_path, settings, fragment):
    store, lifecycle, current, _, _ = prepared(tmp_path, **settings)
    revision = store.read('FINISH')['revision']
    result = lifecycle.admit('held', 'FINISH', expected_work_revision=revision, expected_digest=current['digest'])
    assert result['execution'] == 'held' and any(fragment in x for x in result['blockers'])
    held_revision = store.read('FINISH')['revision']
    assert lifecycle.admit('held', 'FINISH', expected_work_revision=revision, expected_digest=current['digest']) == result
    assert store.read('FINISH')['revision'] == held_revision
    snapshot = lifecycle.snapshot('FINISH')
    assert snapshot['assignments'] == [] and snapshot['resources'] == []
    assert not (store.directory / 'codex-runtime.sqlite3').exists()


def test_admission_counts_previous_failed_product_reservation(tmp_path):
    store, lifecycle, current, budget, source = prepared(tmp_path)
    path = store.directory / 'codex-runtime.sqlite3'
    with journal(path) as db:
        charge(db, {**budget, 'kind': 'product'}, source, 'retained-failure')
        before = rows(db)
    result = lifecycle.admit('held', 'FINISH', expected_work_revision=store.read('FINISH')['revision'],
                             expected_digest=current['digest'])
    assert result['execution'] == 'held'
    assert any('Shared attempt' in item for item in result['blockers'])
    with sqlite3.connect(path) as db:
        assert rows(db) == before


def test_legacy_contract_keeps_prior_admission_behavior(tmp_path):
    _, lifecycle, _ = setup(tmp_path, admitted=False, max_assignments=1)
    assert lifecycle.admit('legacy-admit', 'Q1')['execution'] == 'eligible'
    assert len(lifecycle.assignments()) == 1


@pytest.mark.parametrize('record', ['[]', 'null', 'false', '"invalid"'])
def test_non_object_saved_attempt_returns_a_hold(tmp_path, record):
    path = tmp_path / 'codex-runtime.sqlite3'
    with journal(path) as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', ('broken', record))
    encoded = path.read_bytes()
    result = budgets.read_finish_capacity(path, allowance(tmp_path), source=str(tmp_path / 'source'),
                                          max_repairs=0, max_seconds=60, max_assignments=2)
    assert not result['ready'] and result['blockers']
    assert path.read_bytes() == encoded


def test_new_policy_cannot_be_added_without_approval(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    with store._transaction() as db:
        row = db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()
        changed = json.loads(row[0])
        changed['execution_policy'] = POLICY
        db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(changed),))
    result = lifecycle.admit('unapproved-policy', 'Q1')
    assert result['execution'] == 'held'
    assert any('approved execution_policy' in item for item in result['blockers'])
    assert lifecycle.assignments() == []


def test_opt_in_recipe_digest_must_stay_approved(tmp_path):
    store, lifecycle, _, _, _ = prepared(tmp_path)
    with store._transaction() as db:
        row = db.execute("SELECT contract FROM lifecycle WHERE work_id='FINISH'").fetchone()
        changed = json.loads(row[0])
        changed['runtime_recipe_digest'] = 'sha256:unapproved'
        db.execute("UPDATE lifecycle SET contract=? WHERE work_id='FINISH'", (canonical(changed),))
    result = lifecycle.admit('unapproved-recipe', 'FINISH')
    assert result['execution'] == 'held'
    assert any('approved runtime_recipe_digest' in item for item in result['blockers'])
    assert lifecycle.assignments() == []



@pytest.mark.parametrize('repairs,startups,jobs', [
    (0, 0, 2), (0, 1, 3), (0, 2, 4),
    (1, 0, 5), (1, 1, 6), (1, 2, 7),
    (2, 0, 8), (2, 1, 9), (2, 2, 10),
])
def test_reviewed_host_capacity_counts_triage_without_a_final_diagnostic(tmp_path, repairs, startups, jobs):
    budget = allowance(tmp_path, max_assignments=jobs, max_seconds=jobs * 60)
    result = preview(budget, max_repairs=repairs, max_startup_retries=startups,
                     max_assignments=jobs, reviewed_host=True)
    assert result['ready'] and not result['blockers']
    assert result['required_assignments'] == jobs and result['required_seconds'] == jobs * 60
    assert result['reservation_performed'] is False
    assert list(tmp_path.iterdir()) == []


def test_reviewed_host_capacity_does_not_borrow_a_startup_or_repair_slot(tmp_path):
    budget = allowance(tmp_path, max_assignments=5, max_seconds=300)
    existing = preview(budget, max_startup_retries=1, max_assignments=5)
    explicit_existing = preview(budget, max_startup_retries=1, max_assignments=5, reviewed_host=False)
    reviewed = preview(budget, max_startup_retries=1, max_assignments=5, reviewed_host=True)
    assert existing == explicit_existing and existing['ready']
    assert not reviewed['ready'] and reviewed['required_assignments'] == 6
    assert reviewed['required_seconds'] == 360
    assert all('independent failure triage' in reason for reason in reviewed['blockers'])


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_reviewed_host_capacity_flag_is_strict_and_read_only(tmp_path, value):
    path = tmp_path / 'codex-runtime.sqlite3'
    result = budgets.read_finish_capacity(path, allowance(tmp_path), source=str(tmp_path / 'source'),
                                          max_repairs=1, max_seconds=60, max_assignments=4,
                                          reviewed_host=value)
    assert not result['ready'] and result['required_assignments'] is None
    assert result['blockers'] and result['reservation_performed'] is False
    assert list(tmp_path.iterdir()) == []


def test_reviewed_host_read_only_capacity_preserves_failed_charges(tmp_path):
    path = tmp_path / 'codex-runtime.sqlite3'
    source = tmp_path / 'source'
    budget = allowance(tmp_path, max_assignments=6, max_seconds=360)
    with journal(path) as db:
        charge(db, budget, source, 'retained-failed-job')
        original_rows = rows(db)
    original_bytes, original_mtime = path.read_bytes(), path.stat().st_mtime_ns
    result = budgets.read_finish_capacity(path, budget, source=str(source), max_repairs=1,
                                          max_seconds=60, max_assignments=5, reviewed_host=True)
    assert result['ready'] and result['required_assignments'] == 5
    assert result['attempts_reserved'] == 1 and result['seconds_reserved'] == 60
    assert result['remaining_assignments'] == 5 and result['remaining_seconds'] == 300
    assert path.read_bytes() == original_bytes and path.stat().st_mtime_ns == original_mtime
    with sqlite3.connect(path) as db:
        assert rows(db) == original_rows
