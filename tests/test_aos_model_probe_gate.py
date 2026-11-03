"""Late model canary admission reads durable state; no process is executed."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import codex_runtime, native_paths, resource_limits
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical, digest


class Clock:
    elapsed = 0.0

    def monotonic(self):
        return 1000.0 + self.elapsed

    def now(self, tz=timezone.utc):
        return (datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed)).astimezone(tz)


def _save_attempt(p, record=None):
    with p.runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                   (p.record['attempt_id'], canonical(record if record is not None else p.record)))


def _save_guard(p, guard=None):
    with p.runtime._db() as db:
        db.execute('INSERT INTO resource_limit VALUES(?,?) ON CONFLICT(work_id) DO UPDATE SET record=excluded.record',
                   (p.record['assignment']['work_id'], canonical(guard if guard is not None else p.guard)))


def _database_bytes(p):
    return {str(path): path.read_bytes() if path.exists() else None
            for path in (p.runtime.path, Path(str(p.runtime.path) + '-wal'), Path(str(p.runtime.path) + '-shm'))}


@pytest.fixture
def gate(tmp_path, monkeypatch):
    source, outputs, state = [tmp_path / name for name in ('source', 'outputs', 'state')]
    for path in (source, outputs, state):
        path.mkdir()
    mutable = source / 'mutable.py'
    immutable = source / 'immutable.py'
    mutable.write_text('original mutable contents\n')
    immutable.write_text('original immutable contents\n')
    recipe = {'source': str(source), 'outputs': str(outputs), 'mutable': [mutable.name],
              'immutable': {immutable.name: hashlib.sha256(immutable.read_bytes()).hexdigest()},
              'read_roots': [], 'model_write_scope': 'existing-mutable-files',
              'checks': [{'name': 'not-executed', 'argv': [sys.executable], 'timeout_seconds': 30}],
              'max_seconds': 120,
              'resource_limits': {'output_roots': [], 'source_evidence_roots': [str(source), str(outputs)],
                                  'max_output_growth_bytes': 10000, 'max_source_evidence_growth_bytes': 10000,
                                  'min_free_bytes': 1, 'sample_interval_seconds': 2}}
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=state)), {})
    clock = Clock()
    clock.elapsed = 0.0
    record = {'attempt_id': 'probe-attempt', 'assignment': {'work_id': 'PROBE', 'kind': 'implementation'},
              'recipe_digest': digest(recipe), 'created_at': clock.now().isoformat(), 'run_outputs': str(outputs),
              'startup_version': 1, 'startup_monotonic': clock.monotonic(),
              'startup_deadline': (clock.now() + timedelta(seconds=120)).isoformat(),
              'startup_deadline_monotonic': clock.monotonic() + 120, 'startup_cleanup_reserve': 5,
              'phase': 'intent', 'thread_id': None, 'turn_id': None, 'model_calls_attempted': 0,
              'server_launch': 'not_started', 'server_history': [], 'native_cancel_requested': False}
    bound = resource_limits.scope(recipe)
    guard = {'scope_digest': digest(bound), 'active_attempt': record['attempt_id'], 'breach': None}
    p = SimpleNamespace(runtime=runtime, record=record, recipe=recipe, bound=bound, guard=guard,
                        source=source, outputs=outputs, mutable=mutable, immutable=immutable, clock=clock,
                        calls=[])
    _save_attempt(p)
    _save_guard(p)
    monkeypatch.setattr(codex_runtime.subprocess, 'Popen', lambda *args, **kwargs:
                        pytest.fail('A model probe gate test must never launch a process'))
    monkeypatch.setattr(codex_runtime.subprocess, 'run', lambda *args, **kwargs:
                        pytest.fail('A model probe gate test must install a controlled runner'))
    yield p
    runtime.close()


def _controlled_probe(p, monkeypatch, *, exact=True):
    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return p.clock.now(tz or timezone.utc)

    monkeypatch.setattr(codex_runtime, 'time', SimpleNamespace(monotonic=p.clock.monotonic))
    monkeypatch.setattr(codex_runtime, 'datetime', ControlledDatetime)
    monkeypatch.setattr(codex_runtime, 'bundled_codex_path', lambda: Path('/unused-fixture-codex'))
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs: ([], {'filesystem': {
        ':root': 'deny', str(p.source): 'read', str(p.mutable): 'write', str(p.outputs): 'write'}}))
    answer = {name: True for name in ('state_read_denied', 'state_write_denied', 'credentials_denied',
                                     'credential_config_denied', 'immutable_write_denied', 'output_write',
                                     'source_access', 'network_denied', 'mutable_access_0', 'mutable_unchanged_0')}

    def runner(argv, **kwargs):
        p.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer if exact else {'simulated': True}), stderr='')

    monkeypatch.setattr(codex_runtime.subprocess, 'run', runner)


@pytest.mark.parametrize('resources', [True, False])
def test_healthy_gate_is_read_only_and_does_not_sample_files(gate, monkeypatch, resources):
    p = gate
    if not resources:
        p.recipe.pop('resource_limits')
        p.record['recipe_digest'] = digest(p.recipe)
        _save_attempt(p)
    before = _database_bytes(p)
    source_before = {str(path): path.read_bytes() for path in p.source.iterdir()}
    statements = []

    @contextmanager
    def read_only_database():
        db = sqlite3.connect(f'file:{p.runtime.path}?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            db.set_trace_callback(statements.append)
            yield db
        finally:
            db.close()

    monkeypatch.setattr(p.runtime, '_db', read_only_database)
    for name in ('observe', 'sample', 'sample_file'):
        monkeypatch.setattr(resource_limits, name, lambda *args, **kwargs:
                            pytest.fail('Admission gate must not scan or update resource observations'))
    assert p.runtime._model_probe_gate(p.record, p.recipe) is None
    assert _database_bytes(p) == before
    assert {str(path): path.read_bytes() for path in p.source.iterdir()} == source_before
    assert not any(sql.upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN IMMEDIATE')) for sql in statements)


def test_missing_durable_attempt_blocks_gate(gate):
    p = gate
    with p.runtime._db() as db:
        db.execute('DELETE FROM attempt')
    with pytest.raises(Rejected, match='disappeared'):
        p.runtime._model_probe_gate(p.record, p.recipe)


@pytest.mark.parametrize(('key', 'changed'), [
    ('attempt_id', 'other-attempt'), ('assignment', {'work_id': 'OTHER', 'kind': 'implementation'}),
    ('recipe_digest', 'sha256:different'), ('created_at', '2000-01-01T00:00:00+00:00'),
    ('run_outputs', '/different-output'), ('startup_version', 2), ('startup_monotonic', 999),
    ('startup_deadline', '2000-01-01T00:00:00+00:00'), ('startup_deadline_monotonic', 999),
    ('startup_cleanup_reserve', 9), ('phase', 'thread_request'), ('thread_id', 'different-thread'),
    ('turn_id', 'different-turn'), ('model_calls_attempted', 1), ('server_launch', 'intent'),
    ('server_history', [{'pid': 1234, 'started': 'changed'}]),
])
def test_changed_identity_clock_or_execution_frontier_blocks_gate(gate, key, changed):
    p = gate
    current = {**p.record, key: changed}
    _save_attempt(p, current)
    before = _database_bytes(p)
    with pytest.raises(Rejected, match='identity or execution changed'):
        p.runtime._model_probe_gate(p.record, p.recipe)
    assert _database_bytes(p) == before


@pytest.mark.parametrize('field', ['assignment', 'recipe_digest', 'startup_deadline', 'startup_deadline_monotonic'])
def test_missing_original_identity_or_clock_field_blocks_gate(gate, field):
    p = gate
    current = copy.deepcopy(p.record)
    current.pop(field)
    _save_attempt(p, current)
    with pytest.raises(Rejected, match='identity or execution changed'):
        p.runtime._model_probe_gate(p.record, p.recipe)


@pytest.mark.parametrize('stop', [
    {'native_cancel_requested': True}, {'startup_stop_receipt': {'stopped': True}},
    {'terminal_observation': {'status': 'startup_failed'}}, {'terminal_observation': {}},
])
def test_durable_stop_blocks_even_an_unchanged_frontier(gate, stop):
    p = gate
    _save_attempt(p, {**p.record, **stop})
    with pytest.raises(Rejected, match='Startup was stopped'):
        p.runtime._model_probe_gate(p.record, p.recipe)


@pytest.mark.parametrize('damage', ['missing', 'different-scope', 'inactive', 'other-attempt', 'breached'])
def test_resource_guard_must_remain_exact_active_and_healthy(gate, damage):
    p = gate
    guard = copy.deepcopy(p.guard)
    if damage == 'missing':
        with p.runtime._db() as db:
            db.execute('DELETE FROM resource_limit')
    else:
        if damage == 'different-scope':
            guard['scope_digest'] = 'sha256:changed'
        elif damage == 'inactive':
            guard['active_attempt'] = None
        elif damage == 'other-attempt':
            guard['active_attempt'] = 'another-attempt'
        else:
            guard['breach'] = {'reason': 'max_output_growth_bytes', 'observed_at': 1}
        _save_guard(p, guard)
    before = _database_bytes(p)
    with pytest.raises(Rejected, match='active unchanged resource guard'):
        p.runtime._model_probe_gate(p.record, p.recipe)
    assert _database_bytes(p) == before


@pytest.mark.parametrize('damage', ['valid-failure', 'corrupt-json', 'wrong-scope'])
def test_retained_failure_sidecar_blocks_before_cancellation_is_saved(gate, damage):
    p = gate
    failure = {'work_id': p.record['assignment']['work_id'], 'scope_digest': digest(p.bound),
               'breach': {'reason': 'observation_failed', 'observed_at': 1},
               'high_water_growth_bytes': {'output_roots': 0, 'source_evidence_roots': 0}}
    if damage == 'wrong-scope':
        failure['scope_digest'] = 'sha256:another-scope'
    path = resource_limits._failure_path(p.runtime.path, p.record['assignment']['work_id'])
    path.write_text('{invalid-json' if damage == 'corrupt-json' else canonical(failure))
    path.chmod(0o600)
    before = _database_bytes(p), path.read_bytes()
    with pytest.raises((Rejected, ValueError)):
        p.runtime._model_probe_gate(p.record, p.recipe)
    assert (_database_bytes(p), path.read_bytes()) == before
    with p.runtime._db() as db:
        assert not json.loads(db.execute('SELECT record FROM attempt').fetchone()[0])['native_cancel_requested']


def test_cancellation_during_probe_preparation_prevents_runner(gate, monkeypatch):
    p = gate
    _controlled_probe(p, monkeypatch)
    original = native_paths.validate

    def cancel_during_arguments(recipe, **kwargs):
        _save_attempt(p, {**p.record, 'native_cancel_requested': True})
        return original(recipe, **kwargs)

    monkeypatch.setattr(native_paths, 'validate', cancel_during_arguments)
    with pytest.raises(Rejected, match='Startup was stopped'):
        p.runtime._probe_sandbox(p.record, p.recipe)
    assert not p.calls


@pytest.mark.parametrize(('total', 'reserve', 'gate_seconds', 'timeout'), [(120, 5, 4, 11), (9, 2, 4, 3)])
def test_gate_time_consumes_both_original_deadline_and_local_probe_window(gate, monkeypatch,
                                                                       total, reserve, gate_seconds, timeout):
    p = gate
    _controlled_probe(p, monkeypatch)
    p.record.update(startup_deadline=(p.clock.now() + timedelta(seconds=total)).isoformat(),
                    startup_deadline_monotonic=p.clock.monotonic() + total, startup_cleanup_reserve=reserve)
    _save_attempt(p)
    original = p.runtime._model_probe_gate

    def delayed_gate(record, recipe):
        p.clock.elapsed += gate_seconds
        return original(record, recipe)

    monkeypatch.setattr(p.runtime, '_model_probe_gate', delayed_gate)
    assert p.runtime._probe_sandbox(p.record, p.recipe)['exit_code'] == 0
    assert len(p.calls) == 1
    assert p.calls[0][1]['timeout'] == pytest.approx(timeout)
    assert p.mutable.read_text() == 'original mutable contents\n'


@pytest.mark.parametrize(('elapsed', 'message'), [(15, 'fifteen-second'), (120, 'original startup/assignment'),
                                                  (-1, 'original startup/assignment')])
def test_gate_cannot_refresh_expired_or_rolled_back_time(gate, monkeypatch, elapsed, message):
    p = gate
    _controlled_probe(p, monkeypatch)
    original = p.runtime._model_probe_gate

    def delayed_gate(record, recipe):
        p.clock.elapsed += elapsed
        return original(record, recipe)

    monkeypatch.setattr(p.runtime, '_model_probe_gate', delayed_gate)
    with pytest.raises(Rejected, match=message):
        p.runtime._probe_sandbox(p.record, p.recipe)
    assert not p.calls


@pytest.mark.parametrize('mode', ['native', 'legacy-no-startup', 'already-started-model'])
def test_gate_does_not_change_native_or_nonstartup_probe_routes(gate, monkeypatch, mode):
    p = gate
    exact = mode == 'already-started-model'
    _controlled_probe(p, monkeypatch, exact=exact)
    if not exact:
        p.recipe.pop('model_write_scope')
    if mode == 'legacy-no-startup':
        p.record.pop('startup_version')
    if mode == 'already-started-model':
        p.record['model_calls_attempted'] = 1
    monkeypatch.setattr(p.runtime, '_model_probe_gate', lambda *args, **kwargs:
                        pytest.fail('This route is outside the startup-only model gate'))
    assert p.runtime._probe_sandbox(p.record, p.recipe, native=mode == 'native')['exit_code'] == 0
    assert len(p.calls) == 1
