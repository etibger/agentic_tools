"""Resource observations retain limits across restarts and independent attempts."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agentic_tools.aos import resource_limits as limits
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical


@pytest.fixture
def guarded(tmp_path):
    source, outputs, state = (tmp_path / name for name in ('source', 'outputs', 'state'))
    for path in (source, outputs, state):
        path.mkdir()
    target = source / 'target'
    target.mkdir()
    (source / 'existing.txt').write_bytes(b'0123456789')
    recipe = {'source': str(source), 'outputs': str(outputs),
              'native_write_roots': [{'path': str(target), 'kind': 'generated'}],
              'resource_limits': {'output_roots': [str(target)], 'source_evidence_roots': [str(source), str(outputs)],
                                  'max_output_growth_bytes': 10, 'max_source_evidence_growth_bytes': 10,
                                  'min_free_bytes': 1, 'sample_interval_seconds': 1}}
    path = state / 'runtime.sqlite3'
    db = sqlite3.connect(path)
    limits.initialize(db)
    with db:
        baseline = limits.prepare(db, 'WORK', recipe)
    yield SimpleNamespace(source=source, outputs=outputs, target=target, state=state, path=path,
                          db=db, recipe=recipe, bound=limits.scope(recipe), baseline=baseline)
    db.close()


def sample(g, attempt='a1', **kwargs):
    with g.db:
        return limits.sample(g.db, 'WORK', g.bound, attempt, **kwargs)


def test_baseline_and_breach_survive_restart_second_assignment_and_cleanup(guarded):
    g = guarded
    sample(g, activate=True)
    (g.target / 'first').write_bytes(b'a' * 8)
    first = sample(g, force=True, finish=True)
    assert first['high_water_growth_bytes']['output_roots'] == 8
    g.db.close()
    g.db = sqlite3.connect(g.path)
    with g.db:
        assert limits.prepare(g.db, 'WORK', g.recipe)['baseline'] == g.baseline['baseline']
    sample(g, 'a2', activate=True)
    (g.target / 'second').write_bytes(b'b' * 4)
    failed = sample(g, 'a2', force=True)
    assert failed['breach']['reason'] == 'max_output_growth_bytes'
    assert failed['high_water_growth_bytes']['output_roots'] == 12
    (g.target / 'first').unlink()
    (g.target / 'second').unlink()
    cleaned = sample(g, 'a2', force=True, finish=True)
    assert cleaned['breach'] == failed['breach']
    assert cleaned['high_water_growth_bytes']['output_roots'] == 12
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        limits.enforce(cleaned)
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        limits.prepare(g.db, 'WORK', g.recipe)


def test_generated_bytes_separate_from_source_and_evidence(guarded):
    g = guarded
    (g.target / 'object').write_bytes(b'x' * 9)
    report = sample(g, activate=True)
    assert report['high_water_growth_bytes'] == {'output_roots': 9, 'source_evidence_roots': 0}
    (g.outputs / 'log').write_bytes(b'z' * 11)
    report = sample(g, force=True)
    assert report['breach']['reason'] == 'max_source_evidence_growth_bytes'


def test_source_deletion_never_credits_new_evidence_growth(guarded):
    g = guarded
    (g.source / 'existing.txt').unlink()
    (g.outputs / 'log').write_bytes(b'x' * 12)
    report = sample(g, activate=True)
    assert report['high_water_growth_bytes']['source_evidence_roots'] == 12
    assert report['breach']


def test_free_disk_breach_is_sticky(guarded, monkeypatch):
    g = guarded
    sample(g, activate=True)
    monkeypatch.setattr(limits.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096))
    report = sample(g, force=True)
    assert report['breach']['reason'] == 'min_free_bytes'
    monkeypatch.setattr(limits.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=100, f_frsize=4096))
    assert sample(g, force=True)['breach'] == report['breach']


def test_initial_low_disk_prevents_baseline_and_admission(tmp_path, guarded, monkeypatch):
    g = guarded
    monkeypatch.setattr(limits.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096))
    with pytest.raises(Rejected, match='before execution'):
        limits.prepare(g.db, 'NEW', g.recipe)
    assert g.db.execute("SELECT 1 FROM resource_limit WHERE work_id='NEW'").fetchone() is None


def test_active_observation_gap_holds_but_idle_wait_does_not(guarded, monkeypatch):
    g = guarded
    moment = g.baseline['baseline']['observed_at']
    monkeypatch.setattr(limits.time, 'time', lambda: moment)
    sample(g, activate=True, finish=True)
    moment += 1000
    assert sample(g, 'a2', activate=True)['breach'] is None
    moment += g.bound['max_observation_gap_seconds'] + 1
    report = sample(g, 'a2', force=True)
    assert report['breach']['reason'] == 'active_observation_gap'


def test_observation_failure_and_root_replacement_are_retained(guarded, monkeypatch):
    g = guarded
    g.source.rename(g.source.with_name('old-source'))
    g.source.mkdir()
    g.target.mkdir()
    report = sample(g, activate=True)
    assert report['breach']['reason'] == 'observation_failed'
    with pytest.raises(Rejected, match='resource_limit_exceeded'):
        limits.enforce(report)
    assert limits._read(g.db, 'WORK')['breach'] == report['breach']


def test_filesystem_read_failure_cannot_erase_successful_observation(guarded, monkeypatch):
    g = guarded
    before = sample(g, activate=True)
    monkeypatch.setattr(limits, 'observe', lambda scope: (_ for _ in ()).throw(OSError('inspection unavailable')))
    failed = sample(g, force=True)
    assert failed['last_observation'] == before['last_observation']
    assert failed['breach']['reason'] == 'observation_failed'


def test_symlink_target_is_never_traversed(guarded, tmp_path):
    g = guarded
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'large').write_bytes(b'x' * 1000)
    (g.target / 'link').symlink_to(outside, target_is_directory=True)
    report = sample(g, activate=True)
    assert report['high_water_growth_bytes']['output_roots'] == (g.target / 'link').lstat().st_size


@pytest.mark.parametrize('key,value', [
    ('max_output_growth_bytes', True), ('max_source_evidence_growth_bytes', 0),
    ('min_free_bytes', -1), ('sample_interval_seconds', 0), ('sample_interval_seconds', 11),
    ('source_evidence_roots', []), ('output_roots', []),
])
def test_invalid_or_incomplete_cap_scope_rejected(guarded, key, value):
    recipe = copy.deepcopy(guarded.recipe)
    recipe['resource_limits'][key] = value
    with pytest.raises(Rejected, match='resource_limits'):
        limits.scope(recipe)


def test_scope_alias_overlap_and_protected_control_rejected(guarded):
    g = guarded
    with pytest.raises(Rejected, match='protected'):
        limits.scope(g.recipe, protected=(g.source / 'private-control',))
    bad = copy.deepcopy(g.recipe)
    bad['resource_limits']['source_evidence_roots'].append(str(g.source))
    with pytest.raises(Rejected, match='overlap'):
        limits.scope(bad)
    alias = g.source.with_name('alias')
    alias.symlink_to(g.source, target_is_directory=True)
    bad['resource_limits']['source_evidence_roots'] = [str(alias), str(g.outputs)]
    with pytest.raises(Rejected, match='canonical'):
        limits.scope(bad)


def test_changed_limit_or_unfinished_other_attempt_cannot_reset_baseline(guarded):
    g = guarded
    sample(g, activate=True)
    with pytest.raises(Rejected, match='Another unresolved'):
        sample(g, 'a2', activate=True)
    recipe = copy.deepcopy(g.recipe)
    recipe['resource_limits']['max_output_growth_bytes'] += 1
    with pytest.raises(Rejected, match='cannot be reset'):
        limits.prepare(g.db, 'WORK', recipe)


def test_external_supervisor_samples_same_private_durable_baseline(guarded):
    g = guarded
    sample(g, activate=True)
    (g.target / 'large').write_bytes(b'x' * 11)
    receipt = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    assert receipt['breach']['reason'] == 'max_output_growth_bytes'
    assert limits._read(g.db, 'WORK')['high_water_growth_bytes']['output_roots'] == 11


def test_runtime_model_monitor_cancels_exact_attempt_on_breach(guarded, monkeypatch):
    g = guarded
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=g.state)), {'WORK': g.recipe})
    record = {'attempt_id': 'guarded-model', 'assignment': {'work_id': 'WORK'}}
    with runtime._db() as db:
        limits.prepare(db, 'WORK', g.recipe)
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    cancelled, ids = threading.Event(), []
    def cancel(attempt):
        ids.append(attempt)
        cancelled.set()
    monkeypatch.setattr(runtime, 'cancel', cancel)
    try:
        runtime._resource_watch(record, g.recipe)
        (g.outputs / 'oversized-log').write_bytes(b'x' * 11)
        assert cancelled.wait(5)
        assert ids == ['guarded-model']
        result = runtime._resource_finish(record, g.recipe)
        assert result['breach']['reason'] == 'max_source_evidence_growth_bytes'
        assert result['active_attempt'] is None
    finally:
        runtime.close()


def test_absent_optional_caps_keep_historical_behavior(guarded):
    recipe = {k: v for k, v in guarded.recipe.items() if k != 'resource_limits'}
    assert limits.scope(recipe) is None
    assert limits.prepare(guarded.db, 'NO-LIMITS', recipe) is None


def test_dynamic_native_temporary_growth_is_counted_without_touching_existing_entries(guarded, tmp_path):
    g = guarded
    temporary = tmp_path / 'native-temporary'
    temporary.mkdir()
    existing = temporary / 'owned-123'
    existing.mkdir()
    (existing / 'unrelated').write_bytes(b'x' * 1000)
    recipe = copy.deepcopy(g.recipe)
    recipe['native_sandbox'] = {'version': 'macos-seatbelt-v1', 'temporary_paths': [
        {'parent': str(temporary), 'name': 'owned-{pid}', 'kind': 'tree'}]}
    with g.db:
        limits.prepare(g.db, 'TEMP', recipe)
        bound = limits.scope(recipe)
        first = limits.sample(g.db, 'TEMP', bound, 'native-1', activate=True)
    assert first['high_water_growth_bytes']['output_roots'] == 0
    fresh = temporary / 'owned-456'
    fresh.mkdir()
    (fresh / 'generated').write_bytes(b'a' * 11)
    with g.db:
        failed = limits.sample(g.db, 'TEMP', bound, 'native-1', force=True)
    assert failed['breach']['reason'] == 'max_output_growth_bytes'
    assert failed['high_water_growth_bytes']['output_roots'] == 11
    assert (existing / 'unrelated').read_bytes() == b'x' * 1000


def test_recreated_temporary_name_is_not_mistaken_for_denied_existing_inode(guarded, tmp_path):
    g = guarded
    temporary = tmp_path / 'native-temporary'
    temporary.mkdir()
    existing = temporary / 'owned-123'
    existing.mkdir()
    recipe = copy.deepcopy(g.recipe)
    recipe['native_sandbox'] = {'version': 'macos-seatbelt-v1', 'temporary_paths': [
        {'parent': str(temporary), 'name': 'owned-{pid}', 'kind': 'tree'}]}
    with g.db:
        limits.prepare(g.db, 'TEMP', recipe)
    existing.rename(temporary / 'preserved-old')
    existing.mkdir()
    (existing / 'new-data').write_bytes(b'a' * 11)
    with g.db:
        failed = limits.sample(g.db, 'TEMP', limits.scope(recipe), 'native-1', activate=True)
    assert failed['high_water_growth_bytes']['output_roots'] == 11
    assert failed['breach']['reason'] == 'max_output_growth_bytes'


def test_incremental_scan_rejects_entry_exhaustion(guarded, monkeypatch):
    g = guarded
    for index in range(4):
        (g.target / str(index)).write_bytes(b'a')
    monkeypatch.setattr(limits, 'MAX_ENTRIES', 2)
    result = sample(g, activate=True)
    assert result['breach']['reason'] == 'observation_failed'
