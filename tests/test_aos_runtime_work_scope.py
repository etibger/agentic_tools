"""Simulated per-work runtime ownership; no process, model or native command."""
from types import SimpleNamespace

import pytest

from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical


def lifecycle(directory):
    return SimpleNamespace(store=SimpleNamespace(directory=directory))


@pytest.fixture
def runtime(tmp_path):
    value = CodexRuntime(lifecycle(tmp_path), {'OWNED': {}}, work_id='OWNED')
    yield value


def insert_attempt(runtime, aid, work_id):
    record = {'attempt_id': aid, 'assignment': {'work_id': work_id}, 'phase': 'running'}
    with runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (aid, canonical(record)))
    return record


def insert_native(runtime, check_id, attempt_id, phase='running'):
    row = {'check_id': check_id, 'attempt_id': attempt_id, 'phase': phase,
           'output': str(runtime.directory / check_id)}
    with runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, attempt_id, canonical(row)))
    return row


def rows(runtime):
    with runtime._db() as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                for table in ('attempt', 'native_check', 'admission_denial')}


@pytest.mark.parametrize('recipes', [{}, {'FOREIGN': {}}, {'OWNED': {}, 'FOREIGN': {}}])
def test_scope_must_bind_one_exact_recipe_before_creating_runtime(tmp_path, recipes):
    with pytest.raises(Rejected, match='exactly its one work'):
        CodexRuntime(lifecycle(tmp_path), recipes, work_id='OWNED')
    assert not (tmp_path / 'codex-runtime.sqlite3').exists()


@pytest.mark.parametrize('method', ['lookup', 'poll', 'cancel'])
def test_foreign_attempt_cannot_be_observed_or_cancelled(runtime, method):
    insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    before = rows(runtime)
    with pytest.raises(Rejected, match='only its exact work'):
        getattr(runtime, method)('foreign-attempt')
    assert rows(runtime) == before


def test_foreign_start_and_saved_record_update_stop_before_any_effect(runtime):
    record = insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    before = rows(runtime)
    with pytest.raises(Rejected, match='only its exact work'):
        runtime.start({'work_id': 'FOREIGN'})
    with pytest.raises(Rejected, match='only its exact work'):
        runtime._save(record)
    assert rows(runtime) == before


def test_foreign_admission_denial_cannot_be_adopted(runtime):
    record = {'assignment': {'work_id': 'FOREIGN'}, 'observation': {'state': 'failed'}}
    with runtime._db() as db:
        db.execute('INSERT INTO admission_denial VALUES(?,?)', ('foreign-denial', canonical(record)))
    before = rows(runtime)
    with pytest.raises(Rejected, match='only its exact work'):
        runtime.lookup('foreign-denial')
    assert rows(runtime) == before


def test_native_operation_requires_an_owned_product_attempt(runtime, tmp_path):
    insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    insert_native(runtime, 'foreign-native', 'foreign-attempt')
    insert_native(runtime, 'unbound-native', None)
    before = rows(runtime)
    for check_id in ('foreign-native', 'unbound-native'):
        with pytest.raises(Rejected, match='runtime_scope'):
            runtime._native_stop(check_id, cancel=True)
    for recipe in ({}, {'_attempt_id': 'missing'}, {'_attempt_id': 'foreign-attempt'}):
        with pytest.raises(Rejected, match='runtime_scope'):
            runtime._check(recipe, {}, tmp_path / 'never-created', 'writer', {})
    assert not (tmp_path / 'never-created').exists()
    assert rows(runtime) == before


def test_scoped_shutdown_only_reconciles_owned_unfinished_native_checks(runtime, monkeypatch):
    insert_attempt(runtime, 'owned-attempt', 'OWNED')
    insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    for check_id, aid, phase in (
        ('owned-running', 'owned-attempt', 'running'),
        ('owned-clean', 'owned-attempt', 'clean'),
        ('foreign-running', 'foreign-attempt', 'running'),
        ('historical-unbound', None, 'cleanup_unknown'),
        ('orphan', 'absent', 'cleanup_unknown'),
    ):
        insert_native(runtime, check_id, aid, phase)
    before = rows(runtime)
    observed = []
    monkeypatch.setattr(runtime, '_native_stop', lambda check_id, *, cancel: observed.append((check_id, cancel)))
    runtime.close()
    assert observed == [('owned-running', True)]
    assert rows(runtime) == before


def test_legacy_shutdown_retains_existing_unscoped_behavior(tmp_path, monkeypatch):
    runtime = CodexRuntime(lifecycle(tmp_path), {})
    insert_native(runtime, 'legacy-unbound', None)
    observed = []
    monkeypatch.setattr(runtime, '_native_stop', lambda check_id, *, cancel: observed.append((check_id, cancel)))
    runtime.close()
    assert observed == [('legacy-unbound', True)]


def test_persistence_cannot_adopt_foreign_attempt_with_owned_incoming_identity(runtime):
    insert_attempt(runtime, 'owned-attempt', 'OWNED')
    foreign = insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    owned_native = insert_native(runtime, 'owned-native', 'owned-attempt')
    foreign_native = insert_native(runtime, 'foreign-native', 'foreign-attempt')
    before = rows(runtime)
    adopted = {**foreign, 'assignment': {'work_id': 'OWNED'}}
    with pytest.raises(Rejected, match='runtime_scope'):
        runtime._save(adopted)
    with pytest.raises(Rejected, match='runtime_scope'):
        runtime._startup_patch(adopted, {'error': 'must not write'}, match_frontier=False)
    for change in (foreign_native, {**foreign_native, 'attempt_id': 'owned-attempt'},
                   {**owned_native, 'attempt_id': 'foreign-attempt'}):
        with pytest.raises(Rejected, match='runtime_scope'):
            runtime._native_save(change)
    assert rows(runtime) == before


def test_owned_persistence_keeps_assignment_identity_and_accepts_factual_updates(runtime):
    owned = insert_attempt(runtime, 'owned-attempt', 'OWNED')
    owned_native = insert_native(runtime, 'owned-native', 'owned-attempt')
    with pytest.raises(Rejected, match='assignment identity'):
        runtime._save({**owned, 'assignment': {**owned['assignment'], 'executor': 'changed'}})
    runtime._save({**owned, 'error': 'retained fact'})
    runtime._native_save({**owned_native, 'phase': 'cleanup_unknown'})
    assert runtime._get('owned-attempt')['error'] == 'retained fact'
    assert runtime._native_get('owned-native')['phase'] == 'cleanup_unknown'


@pytest.mark.parametrize('corruption', ['indexed-attempt', 'record-check-id'])
def test_native_record_disagreement_blocks_all_actions_without_repair(runtime, corruption):
    insert_attempt(runtime, 'owned-attempt', 'OWNED')
    insert_attempt(runtime, 'foreign-attempt', 'FOREIGN')
    value = insert_native(runtime, 'owned-native', 'owned-attempt')
    with runtime._db() as db:
        if corruption == 'indexed-attempt':
            db.execute("UPDATE native_check SET attempt_id='foreign-attempt' WHERE id='owned-native'")
        else:
            db.execute("UPDATE native_check SET record=? WHERE id='owned-native'",
                       (canonical({**value, 'check_id': 'different-id'}),))
    before = rows(runtime)
    with pytest.raises(Rejected, match='runtime_scope'):
        runtime._native_stop('owned-native', cancel=True)
    with pytest.raises(Rejected, match='runtime_scope'):
        runtime._native_save(value)
    assert rows(runtime) == before
