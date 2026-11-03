"""Readiness must fail before product authority/accounting, never manufacture it."""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import readiness
from agentic_tools.aos.store import Rejected, canonical


class FakeClient:
    def __init__(self, fixture, binary, overrides, cwd, timeout):
        self.fixture = fixture
        self.cwd = str(cwd)
        self.timeout = timeout
        fixture.clients.append(self)

    def initialize(self):
        self.fixture.calls.append('initialize')

    def request(self, method, params):
        self.fixture.calls.append(method)
        assert method != 'turn/start', 'Readiness must never request model work'
        if method == 'config/read':
            return {'config': {'verified': True}}
        assert method == 'thread/start'
        return {'thread': {'id': 'metadata-' + str(len(self.fixture.clients))},
                'model': params['model'], 'reasoningEffort': params['config']['model_reasoning_effort'],
                'cwd': self.cwd, 'approvalPolicy': 'never'}


class FakeOwner:
    def __init__(self, fixture, client):
        self.fixture = fixture
        self.owner = {'pid': 123, 'uid': 1, 'birth': [1, 2], 'group': 123}

    def close(self, deadline):
        self.fixture.closes += 1
        return {'verified': self.fixture.clean, 'owner': self.owner, 'members': [],
                'server_reaped': True, 'readers_complete': True}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source, outputs, state = [tmp_path / name for name in ('source', 'outputs', 'state')]
    for path in (source, outputs, state):
        path.mkdir()
    (source / 'app.py').write_text('answer = 1\n')
    # An invalid SQLite payload makes any accidental product opening observable.
    (state / 'codex-runtime.sqlite3').write_bytes(b'existing historical ledger; never open or mutate')
    recipe = {'execution_policy': 'isolated-checkout-v1', 'source': str(source), 'outputs': str(outputs),
              'read_roots': [], 'checks': [], 'max_seconds': 180,
              'probe_interpreter': str(Path(sys.executable).resolve()),
              'instruction_manifest': {'version': 1, 'setting': 'first'},
              'checkout_policy': {'change_roots': ['app.py'], 'protected_roots': [],
                  'native_environment': {key: str(outputs / name) for key, name in (
                      ('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
                      ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'run'))},
                  'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 10000,
                                      'max_output_bytes': 10000}}}
    p = SimpleNamespace(recipe=recipe, source=source, outputs=outputs, state=state,
                        directory=tmp_path / 'readiness', calls=[], clients=[], probes=[], closes=0,
                        clean=True, fail_probe=False, resource_error=False, free=10000)
    monkeypatch.setattr(readiness.checkout_boundary, 'enabled', lambda recipe: recipe['execution_policy'] == 'isolated-checkout-v1')
    monkeypatch.setattr(readiness.checkout_boundary, 'validate', lambda *args: None)
    monkeypatch.setattr(readiness.checkout_boundary, 'validate_tree', lambda *args, **kwargs: {})
    monkeypatch.setattr(readiness.native_paths, 'writable_roots', lambda recipe: ())
    monkeypatch.setattr(readiness.instructions, 'readback', lambda manifest, *args, **kwargs:
                        {'manifest': copy.deepcopy(manifest), 'configuration_transition': None})
    monkeypatch.setattr(readiness.instructions, 'validate', lambda *args, **kwargs: [])
    monkeypatch.setattr(readiness.instructions, 'verify_settings', lambda *args: None)
    monkeypatch.setattr(readiness, '_runtime_identity', lambda: {'binary': '/unused/codex', 'version': 'pinned'})
    monkeypatch.setattr(readiness, 'adapter_for', lambda check: object())
    monkeypatch.setattr(readiness.CodexRuntime, '_profile', lambda self, *args, **kwargs: ([], {'verified': True}))
    monkeypatch.setattr(readiness.CodexRuntime, '_verify_config', staticmethod(
        lambda config, expected: None if config == expected else pytest.fail('Unexpected configuration')))
    def resources(recipe, deadline):
        if p.resource_error:
            raise Rejected('resource_observation', 'Injected unavailable resource observation')
        return {'owned_bytes': 1, 'output_bytes': 0, 'free_bytes': p.free}
    def enforce(recipe, before, after):
        if after['free_bytes'] < 1:
            raise Rejected('checkout_resource_limit', 'Insufficient current free space')
        return {'passed': True, 'peak_usage': 'not established'}
    monkeypatch.setattr(readiness, '_resources', resources)
    monkeypatch.setattr(readiness, '_enforce_resources', enforce)
    def probe(recipe, **kwargs):
        p.probes.append({'recipe': recipe, **kwargs})
        if p.fail_probe:
            error = Rejected('checkout_probe', 'Injected denied capability')
            error.cleanup_verified = True  # Explicit simulated clean-denial receipt.
            raise error
        return {'checks': {'passed': True}, 'cleanup': {'verified': True}}
    p.probe = probe
    p.transport = lambda *args, **kwargs: FakeClient(p, *args, **kwargs)
    p.owner = lambda client: FakeOwner(p, client)
    p.run = lambda **kwargs: readiness.run_readiness(
        p.recipe, state_dir=state, readiness_dir=p.directory, model='fixture-model', effort='high',
        max_seconds=180, transport_factory=p.transport, owner_factory=p.owner, role_probe=p.probe, **kwargs)
    return p


def test_both_metadata_roles_have_no_product_runtime_turn_or_reservation(setup, monkeypatch):
    p = setup
    before = {path.name: path.read_bytes() for path in p.state.iterdir()}
    monkeypatch.setattr(readiness.CodexRuntime, '__init__', lambda *args, **kwargs:
                        pytest.fail('Empty native readiness must not construct any runtime'))
    result = p.run()
    assert result['ready'] and result['simulated'] and result['cleanup_verified']
    assert result['product_attempts'] == result['product_reservations'] == result['model_turns'] == 0
    assert p.calls.count('thread/start') == 2 and 'turn/start' not in p.calls
    assert [item['role'] for item in p.probes] == ['writer', 'writer', 'assessor', 'assessor']
    assert p.closes == 2
    assert {path.name: path.read_bytes() for path in p.state.iterdir()} == before
    assert not (p.directory / 'runtime/readiness-1/codex-runtime.sqlite3').exists()
    for item in p.probes:
        assert str(p.directory / 'control') in item['recipe']['checkout_policy']['protected_roots']
        assert str(p.directory / 'runtime') in item['recipe']['checkout_policy']['protected_roots']
        environment = item['recipe']['checkout_policy']['native_environment']
        for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
            path = Path(environment[key])
            assert path.is_relative_to(item['output']) and path.is_dir()
            assert not path.is_relative_to(p.outputs)
    assert list(p.outputs.iterdir()) == []  # Remapping never creates stable product state.


def test_clean_readiness_failure_is_retained_and_not_retried_by_polling(setup):
    p = setup
    p.fail_probe = True
    failed = p.run()
    assert not failed['ready'] and failed['status'] == 'failed' and failed['cleanup_verified']
    assert failed['blockers'][0]['code'] == 'checkout_probe'
    assert len(p.clients) == 1
    again = p.run()
    assert again['cached'] and not again['ready'] and len(p.clients) == 1
    assert (p.state / 'codex-runtime.sqlite3').read_bytes().startswith(b'existing historical')


@pytest.mark.parametrize('metadata_only', [False, True])
def test_changed_configuration_uses_only_remaining_technical_attempt(setup, metadata_only):
    p = setup
    if metadata_only:
        p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    first = p.run()
    retained = (p.directory / 'control/readiness-1.json').read_bytes()
    p.recipe['instruction_manifest']['setting'] = 'second'
    second = p.run()
    assert first['ready'] and second['ready'] and len(p.clients) == 4
    assert (p.directory / 'control/readiness-1.json').read_bytes() == retained
    p.recipe['instruction_manifest']['setting'] = 'third'
    with pytest.raises(Rejected, match='attempts exhausted'):
        p.run()
    assert len(p.clients) == 4


def test_earlier_cached_success_cannot_bypass_later_unknown_cleanup(setup):
    p = setup
    assert p.run()['ready']
    p.recipe['instruction_manifest']['setting'] = 'second'
    p.clean = False
    assert p.run()['status'] == 'cleanup_unknown'
    p.recipe['instruction_manifest']['setting'] = 'first'
    with pytest.raises(Rejected, match='ownership is unresolved'):
        p.run()
    assert len(p.clients) == 3


def test_resource_observation_failure_without_baseline_replays_saved_blocker(setup):
    p = setup
    p.resource_error = True
    failed = p.run()
    assert failed['status'] == 'failed' and not failed['ready']
    assert 'resources_before' not in failed
    again = p.run()
    assert again['cached'] and again['blockers'] == failed['blockers']
    assert not p.clients


@pytest.mark.parametrize('metadata_only', [False, True])
def test_cached_success_rechecks_current_resource_capacity(setup, metadata_only):
    p = setup
    if metadata_only:
        p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    assert p.run()['ready']
    p.free = 0
    cached = p.run()
    assert cached['cached'] and not cached['ready'] and cached['status'] == 'held'
    assert cached['blockers'][0]['code'] == 'checkout_resource_limit'
    assert len(p.clients) == 2


def test_changed_selected_checks_cannot_reuse_metadata_only_success(setup):
    p = setup
    assert p.run()['ready']
    called = []
    def native(recipe, checks, **kwargs):
        called.extend(checks)
        return {'passed': True, 'checks': [], 'cleanup': {'verified': True}}
    check = {'name': 'build', 'argv': ['/unused/build'], 'timeout_seconds': 30, 'scope': 'build'}
    result = p.run(native_checks=[check], native_runner=native)
    assert result['ready'] and called == [check] and len(p.clients) == 4
    assert result['requested_native_checks'] == [check]


def test_simulated_success_is_not_a_real_cache_hit(setup, monkeypatch):
    p = setup
    assert p.run()['ready']
    monkeypatch.setattr(readiness, '_role', lambda *args, **kwargs:
                        (_ for _ in ()).throw(Rejected('new_real_check', 'Real readiness reached; no fake cache')))
    result = readiness.run_readiness(p.recipe, state_dir=p.state, readiness_dir=p.directory,
                                    model='fixture-model', effort='high', max_seconds=180)
    assert not result.get('cached') and not result['simulated'] and not result['ready']
    assert result['blockers'][0]['code'] == 'new_real_check'


@pytest.mark.parametrize('name', ['../outside', '/outside', '.', '', 'has/slash'])
def test_malformed_check_name_rejected_before_any_controller_output(setup, name):
    p = setup
    check = {'name': name, 'argv': ['/unused/build'], 'timeout_seconds': 30, 'scope': 'build'}
    with pytest.raises(Rejected):
        p.run(native_checks=[check])
    assert not p.directory.exists() and not p.clients


def test_product_internal_identity_is_not_accepted_as_readiness_input(setup):
    p = setup
    p.recipe['_attempt_id'] = 'existing-product-attempt'
    with pytest.raises(Rejected, match='internal execution fields'):
        p.run()
    assert not p.directory.exists() and not p.clients


def test_readiness_rejects_aliased_private_output_parent(setup, tmp_path):
    p = setup
    p.directory.mkdir()
    (p.directory / 'outputs').symlink_to(p.outputs)
    with pytest.raises(Rejected, match='canonical directories'):
        p.run()
    assert not p.clients and list(p.outputs.iterdir()) == []


@pytest.mark.parametrize('metadata_only', [False, True])
def test_source_mutation_during_metadata_check_does_not_become_ready(setup, metadata_only):
    p = setup
    if metadata_only:
        p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    original = p.probe
    def changed(recipe, **kwargs):
        if kwargs['after_startup']:
            (p.source / 'app.py').write_text('changed during a technical probe\n')
        return original(recipe, **kwargs)
    p.probe = changed
    result = p.run()
    assert not result['ready'] and result['cleanup_verified']
    assert result['blockers'][0]['code'] == 'readiness_source_drift'


def test_native_failure_reconciles_null_assignment_rows_in_private_journal(setup, monkeypatch):
    p = setup
    stopped = []
    class NativeFixture:
        def __init__(self, lifecycle, recipes):
            self.path = lifecycle.store.directory / 'codex-runtime.sqlite3'
            assert self.path.parent != p.state
            with self._db() as db:
                db.execute('CREATE TABLE native_check (id TEXT, attempt_id TEXT, record TEXT)')
                db.execute('CREATE TABLE attempt (id TEXT)')
        @contextmanager
        def _db(self):
            with sqlite3.connect(self.path) as db:
                yield db
        def _check(self, recipe, check, output, executor, frozen):
            assert recipe['_readiness_deadline_monotonic'] > readiness.time.monotonic()
            assert recipe['max_seconds'] <= 150
            with self._db() as db:
                db.execute('INSERT INTO native_check VALUES(?,NULL,?)', ('native-one', '{}'))
            return {'name': check['name'], 'result': 'fail', 'native_check_id': 'native-one'}
        def _native_stop(self, check_id, *, cancel):
            stopped.append((check_id, cancel))
            return {'check_id': check_id, 'phase': 'clean'}
        def close(self):
            pass
    monkeypatch.setattr(readiness, 'CodexRuntime', NativeFixture)
    monkeypatch.setattr(readiness, '_verify_native', lambda row: row)
    control = p.directory / 'control/run'
    control.mkdir(parents=True)
    output = p.directory / 'outputs/native'
    output.mkdir(parents=True)
    private = p.directory / 'runtime/run'
    private.parent.mkdir()
    result = readiness._native(p.recipe, [{'name': 'failing', 'timeout_seconds': 30}],
        state=p.state, private_runtime=private, output=output, control=control,
        deadline=readiness.time.monotonic() + 180, save_phase=lambda phase: None)
    assert not result['passed'] and result['cleanup']['verified']
    assert result['cleanup']['private_model_attempts'] == 0
    assert stopped == [('native-one', True)]
    assert (p.state / 'codex-runtime.sqlite3').read_bytes().startswith(b'existing historical')


def test_permission_probe_unknown_cleanup_cannot_be_hidden_by_stopped_metadata_server(setup):
    p = setup
    def unknown(recipe, **kwargs):
        error = Rejected('checkout_probe', 'Probe owner cannot be reconciled')
        error.cleanup_verified = False
        raise error
    p.probe = unknown
    result = p.run()
    assert not result['ready'] and result['status'] == 'cleanup_unknown'
    assert result['roles'][0]['cleanup']['metadata_verified']
    assert not result['roles'][0]['cleanup']['verified']
    assert p.closes == 1


def test_one_deadline_prevents_late_thread_request_and_second_role(setup, monkeypatch):
    p = setup
    elapsed = [0]
    monkeypatch.setattr(readiness.time, 'monotonic', lambda: 1000 + elapsed[0])
    original = p.probe
    def slow(recipe, **kwargs):
        answer = original(recipe, **kwargs)
        elapsed[0] += 181
        return answer
    p.probe = slow
    result = p.run()
    assert not result['ready'] and result['cleanup_verified']
    assert result['blockers'][0]['code'] == 'readiness_deadline'
    assert len(p.clients) == 1 and 'thread/start' not in p.calls


def test_observer_error_still_closes_known_metadata_stdin(monkeypatch):
    import io
    import threading
    class Process:
        pid = 123
        stdin = io.StringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()
        def poll(self):
            return 0 if self.stdin.closed else None
        def send_signal(self, sig):
            pytest.fail('EOF already stopped the direct child')
    owner = object.__new__(readiness._MetadataOwner)
    owner.proc = Process()
    owner.client = SimpleNamespace(readers_complete=lambda timeout: True)
    owner.owner = {'pid': 123, 'uid': 1, 'birth': [1, 2], 'group': 123}
    owner.members = {}
    owner.errors = []
    owner.stop = threading.Event()
    owner.lock = threading.Lock()
    owner.thread = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False)
    owner._observe = lambda: (_ for _ in ()).throw(OSError('Observation unavailable'))
    monkeypatch.setattr(readiness.native_processes, 'identity', lambda pid: None)
    monkeypatch.setattr(readiness, 'process_groups', lambda **kwargs: {})
    result = owner.close(readiness.time.monotonic() + 2)
    assert owner.proc.stdin.closed and result['server_reaped']
    assert not result['verified'] and result['errors']


def test_actual_private_readiness_outputs_are_charged_to_endpoint_limits(setup, monkeypatch):
    p = setup
    seen = []
    def observed(recipe, deadline):
        output = Path(recipe['outputs'])
        seen.append(output)
        size = sum(path.stat().st_size for path in output.rglob('*') if path.is_file())
        return {'owned_bytes': size + 1, 'output_bytes': size, 'free_bytes': 10000}
    def enforce(recipe, before, after):
        if after['output_bytes'] > 10:
            raise Rejected('checkout_resource_limit', 'Private readiness output grew too large')
        return {'passed': True}
    def native(recipe, checks, *, output, **kwargs):
        (output / 'retained.log').write_text('x' * 11)
        return {'passed': True, 'checks': [], 'cleanup': {'verified': True}}
    monkeypatch.setattr(readiness, '_resources', observed)
    monkeypatch.setattr(readiness, '_enforce_resources', enforce)
    result = p.run(native_runner=native)
    assert not result['ready'] and result['cleanup_verified']
    assert result['blockers'][0]['code'] == 'checkout_resource_limit'
    assert result['resources_after']['output_bytes'] == 11
    assert result['product_resources_after']['output_bytes'] == 0
    assert p.directory / 'outputs/readiness-1' in seen


def test_reviewed_host_rejects_project_checks_before_any_preparation_or_attempt(setup, monkeypatch):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    before = {path.name: path.read_bytes() for path in p.state.iterdir()}
    monkeypatch.setattr(readiness, '_normalise', lambda *args: pytest.fail('No preparation before rejected checks'))
    monkeypatch.setattr(readiness, '_runtime_identity', lambda: pytest.fail('No runtime preparation'))
    def native(*args, **kwargs):
        pytest.fail('Readiness cannot execute reviewed project checks')
    with pytest.raises(Rejected, match='metadata-only'):
        p.run(native_checks=[{'name': 'project', 'argv': ['/untrusted/project-build'],
                              'timeout_seconds': 30, 'scope': 'project build'}], native_runner=native)
    assert not p.directory.exists() and not p.clients and not p.calls
    assert {path.name: path.read_bytes() for path in p.state.iterdir()} == before


def test_reviewed_host_preserves_both_metadata_roles_and_never_calls_native_runner(setup, monkeypatch):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    before = {path.name: path.read_bytes() for path in p.state.iterdir()}
    monkeypatch.setattr(readiness.CodexRuntime, '__init__', lambda *args, **kwargs:
                        pytest.fail('Metadata-only readiness cannot construct a runtime'))
    def native(*args, **kwargs):
        pytest.fail('Even an injected native runner must remain unused')
    result = p.run(native_runner=native)
    assert result['ready'] and result['cleanup_verified']
    assert result['readiness_scope'] == 'model-metadata-only'
    assert result['native']['execution'] == 'not_requested' and result['native']['checks'] == []
    assert result['requested_native_checks'] == []
    assert p.calls.count('thread/start') == 2 and 'turn/start' not in p.calls and p.closes == 2
    assert [item['role'] for item in p.probes] == ['writer', 'writer', 'assessor', 'assessor']
    assert result['source_before'] == result['source_after'] and result['runtime_identity']['version'] == 'pinned'
    assert result['product_attempts'] == result['product_reservations'] == result['model_turns'] == 0
    assert not (p.directory / 'runtime/readiness-1').exists()
    assert not (p.directory / 'outputs/readiness-1/native').exists()
    assert {path.name: path.read_bytes() for path in p.state.iterdir()} == before


def test_reviewed_host_helper_cannot_execute_a_native_check_directly(setup, monkeypatch):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    monkeypatch.setattr(readiness.CodexRuntime, '__init__', lambda *args, **kwargs:
                        pytest.fail('No private runtime before native rejection'))
    with pytest.raises(Rejected, match='cannot execute project checks'):
        readiness._native(p.recipe, [{'name': 'project'}], state=p.state,
                          private_runtime=p.directory / 'runtime', output=p.directory / 'outputs',
                          control=p.directory / 'control', deadline=readiness.time.monotonic() + 180,
                          save_phase=lambda phase: pytest.fail('No native phase'))
    assert not p.directory.exists()


@pytest.mark.parametrize('legacy_checks', [False, True])
def test_reviewed_host_cannot_reuse_previous_native_or_default_readiness_cache(setup, legacy_checks):
    p = setup
    called = []
    def native(recipe, checks, **kwargs):
        called.append(list(checks))
        return {'passed': True, 'checks': [], 'cleanup': {'verified': True}}
    check = {'name': 'build', 'argv': ['/unused/build'], 'timeout_seconds': 30, 'scope': 'build'}
    first = p.run(native_checks=[check] if legacy_checks else [], native_runner=native)
    assert first['ready'] and len(called) == 1
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    second = p.run(native_runner=native)
    assert second['ready'] and not second.get('cached') and second['input_key'] != first['input_key']
    assert second['readiness_scope'] == 'model-metadata-only' and len(called) == 1
    assert len(p.clients) == 4
    cached = p.run(native_runner=native)
    assert cached['ready'] and cached['cached'] and cached['input_key'] == second['input_key']
    assert len(called) == 1 and len(p.clients) == 4


def test_reviewed_host_clean_metadata_failure_replays_without_native_stage(setup):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    p.fail_probe = True
    failed = p.run()
    assert not failed['ready'] and failed['cleanup_verified'] and failed['native'] is None
    again = p.run()
    assert again['cached'] and again['blockers'] == failed['blockers'] and len(p.clients) == 1


def test_reviewed_host_mode_change_cannot_bypass_unknown_prior_owner(setup):
    p = setup
    p.clean = False
    assert p.run()['status'] == 'cleanup_unknown'
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    with pytest.raises(Rejected, match='ownership is unresolved'):
        p.run()
    assert len(p.clients) == 1


def test_reviewed_host_still_rejects_changed_runtime_or_pinned_tool_identity(setup, monkeypatch):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    calls = []
    def identity():
        calls.append(True)
        return {'binary': '/unused/codex', 'version': 'pinned' if len(calls) == 1 else 'changed'}
    monkeypatch.setattr(readiness, '_runtime_identity', identity)
    result = p.run()
    assert not result['ready'] and result['cleanup_verified']
    assert result['blockers'][0]['code'] == 'readiness_runtime_drift'
    assert len(p.clients) == 2 and p.closes == 2
    assert result['native']['execution'] == 'not_requested'


@pytest.mark.parametrize('interpreter', ['/bin/sh', 'python3', '/missing/python', None, False])
def test_reviewed_host_rejects_foreign_probe_interpreter_before_any_attempt(setup, monkeypatch, interpreter):
    p = setup
    p.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    p.recipe['probe_interpreter'] = interpreter
    monkeypatch.setattr(readiness, '_normalise', lambda *args: pytest.fail('No preparation for a foreign interpreter'))
    with pytest.raises(Rejected, match='exact worker Python interpreter'):
        p.run()
    assert not p.directory.exists() and not p.clients and not p.calls
    assert (p.state / 'codex-runtime.sqlite3').read_bytes().startswith(b'existing historical')
