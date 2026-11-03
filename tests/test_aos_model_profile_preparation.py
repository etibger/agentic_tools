"""Startup scheduling and same-invocation model profiles; no OS qualification."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import codex_runtime, instructions, native_paths, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, digest
from test_aos_check_adapters import prepared_checks, claim
from test_aos_model_exact_mutable_files import fixture, expected_checks


@pytest.mark.parametrize('assessment', [False, True])
@pytest.mark.parametrize('after_startup', [False, True])
def test_prepared_profile_keeps_exact_role_phase_and_objects(fixture, assessment, after_startup):
    p = fixture
    profile = p.runtime._profile(p.recipe, assessment, after_startup=after_startup)
    prepared = (digest(p.recipe), assessment, after_startup, profile)
    assert CodexRuntime._checked_model_profile(p.recipe, assessment, after_startup, prepared) is profile


@pytest.mark.parametrize('defect', ['recipe', 'role', 'phase', 'integer-role', 'integer-phase',
                                  'legacy', 'outer-list', 'short', 'inner-list', 'overrides', 'profile'])
def test_prepared_profile_rejects_other_authority_or_shape(fixture, defect):
    p = fixture
    recipe = copy.deepcopy(p.recipe)
    profile = p.runtime._profile(recipe)
    prepared = (digest(recipe), False, False, profile)
    if defect == 'recipe':
        recipe['outputs'] += '-other-attempt'
    elif defect == 'role':
        prepared = (prepared[0], True, False, profile)
    elif defect == 'phase':
        prepared = (prepared[0], False, True, profile)
    elif defect == 'integer-role':
        prepared = (prepared[0], 0, False, profile)
    elif defect == 'integer-phase':
        prepared = (prepared[0], False, 0, profile)
    elif defect == 'legacy':
        recipe.pop('model_write_scope')
        prepared = (digest(recipe), False, False, profile)
    elif defect == 'outer-list':
        prepared = list(prepared)
    elif defect == 'short':
        prepared = prepared[:3]
    elif defect == 'inner-list':
        prepared = (*prepared[:3], list(profile))
    elif defect == 'overrides':
        prepared = (*prepared[:3], ({}, profile[1]))
    else:
        prepared = (*prepared[:3], (profile[0], []))
    with pytest.raises(Rejected, match='recipe, role and phase'):
        CodexRuntime._checked_model_profile(recipe, False, False, prepared)


@pytest.mark.parametrize('assessment', [False, True])
@pytest.mark.parametrize('missing', [None, 'instruction_read_0', 'instruction_write_denied_0',
                                   'native_path_access_0', 'native_file_read_denied_0',
                                   'mutable_access_14', 'mutable_unchanged_14'])
def test_prepared_probe_reuses_profile_and_keeps_all_controls(fixture, monkeypatch, assessment, missing):
    p = fixture
    immutable = p.source / 'immutable.txt'
    p.recipe['instruction_manifest'] = {'chain': [{'path': str(immutable),
        'sha256': hashlib.sha256(immutable.read_bytes()).hexdigest()}], 'support': [], 'configs': []}
    monkeypatch.setattr(instructions, 'validate', lambda *args, **kwargs: [str(immutable)])
    cache = p.root / 'cache'; cache.mkdir()
    p.recipe['native_write_roots'] = [{'path': str(cache), 'kind': 'cache'}]
    native_file = p.root / 'native-input'; native_file.write_bytes(b'fixture native input')
    p.recipe['native_sandbox'] = {'version': native_sandbox.VERSION, 'file_read_paths': [
        {'path': str(native_file), 'sha256': hashlib.sha256(native_file.read_bytes()).hexdigest()}]}
    profile = p.runtime._profile(p.recipe, assessment)
    prepared = (digest(p.recipe), assessment, False, profile)
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs:
                        pytest.fail('Prepared probe unexpectedly rebuilt its model profile'))
    validations, calls = [], []
    validate = native_paths.validate
    def checked_native_paths(recipe, **kwargs):
        validations.append((recipe, kwargs))
        return validate(recipe, **kwargs)
    monkeypatch.setattr(native_paths, 'validate', checked_native_paths)
    answer = {**expected_checks(), 'instruction_read_0': True, 'instruction_write_denied_0': True,
              'native_path_access_0': True, 'native_file_read_denied_0': True}
    if missing:
        answer.pop(missing)
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr='')
    monkeypatch.setattr(codex_runtime.subprocess, 'run', run)
    record = {'attempt_id': 'prepared-probe', 'assignment': {
        'kind': 'assessment' if assessment else 'implementation'}}
    if missing:
        with pytest.raises(Rejected, match='incomplete'):
            p.runtime._probe_sandbox(record, p.recipe, prepared_model=prepared)
    else:
        assert p.runtime._probe_sandbox(record, p.recipe, prepared_model=prepared)['checks'] == answer
    assert len(calls) == 1 and 0 < calls[0][1]['timeout'] <= 15
    assert len(validations) == 1
    contract = json.loads(calls[0][0][-1])
    assert len(contract) == 15 and all(item['write'] is not assessment for item in contract)


@pytest.mark.parametrize('mode', ['native', 'legacy'])
def test_prepared_model_cannot_replace_native_or_legacy_probe(fixture, monkeypatch, mode):
    p = fixture
    prepared = (digest(p.recipe), False, False, p.runtime._profile(p.recipe))
    if mode == 'legacy':
        p.recipe.pop('model_write_scope')
    monkeypatch.setattr(p.runtime, '_profile', lambda *args, **kwargs:
                        pytest.fail('Invalid prepared scope must reject before profile work'))
    monkeypatch.setattr(codex_runtime.subprocess, 'run', lambda *args, **kwargs:
                        pytest.fail('Invalid prepared scope must reject before execution'))
    with pytest.raises(Rejected, match='native or legacy'):
        p.runtime._probe_sandbox({'attempt_id': 'wrong-mode', 'assignment': {'kind': 'implementation'}},
                                p.recipe, native=mode == 'native', prepared_model=prepared)


def startup_fixture(tmp_path, monkeypatch, *, exact=True):
    """Real fixture admission/start with scripted RPC and no subprocess/thread."""
    update = (lambda recipe: recipe.update(model_write_scope='existing-mutable-files')) if exact else None
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=update)
    p.events, p.profiles, p.probes, p.clock_fields = [], [], [], []
    p.guard_active = False
    p.client_kwargs = []
    monkeypatch.setattr(codex_runtime, 'processes', lambda **kwargs: {})
    monkeypatch.setattr(codex_runtime, 'process_groups', lambda **kwargs: {})
    monkeypatch.setattr(codex_runtime, 'version', lambda name: codex_runtime.PIN)
    monkeypatch.setattr(codex_runtime, 'bundled_codex_path', lambda: Path('/synthetic/codex'))
    def executable_version(argv, **kwargs):
        assert p.guard_active
        assert argv == ['/synthetic/codex', '--version']
        p.events.append('cli-version')
        return SimpleNamespace(stdout='codex-cli ' + codex_runtime.PIN)
    monkeypatch.setattr(codex_runtime.subprocess, 'run', executable_version)

    class Timer:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            p.events.append('timer')
        def cancel(self):
            pass
    monkeypatch.setattr(codex_runtime.threading, 'Timer', Timer)

    original_profile = p.runtime._profile
    def profile(recipe, assessment=False, **kwargs):
        scoped = recipe['outputs'] != p.recipe['outputs']
        phase = bool(kwargs.get('after_startup'))
        if scoped:
            p.events.append('prepare-post-thread' if phase else 'prepare-initial')
        value = original_profile(recipe, assessment, **kwargs)
        if scoped:
            p.profiles.append((digest(recipe), assessment, phase, value))
        return value
    monkeypatch.setattr(p.runtime, '_profile', profile)
    original_remaining = p.runtime._startup_remaining
    def remaining(record, **kwargs):
        p.clock_fields.append(tuple(record[key] for key in (
            'startup_monotonic', 'startup_deadline_monotonic', 'startup_deadline')))
        return original_remaining(record, **kwargs)
    monkeypatch.setattr(p.runtime, '_startup_remaining', remaining)
    def activate(record, recipe):
        p.events.append('activate')
        assert not p.guard_active
        p.guard_active = True
    monkeypatch.setattr(p.runtime, '_resource_watch', activate)
    monkeypatch.setattr(p.runtime, '_resource_sample', lambda *args, **kwargs: None)
    original_factory = p.runtime.factory
    def factory(*args):
        assert p.guard_active
        p.events.append('app-server')
        client = original_factory(*args)
        initialize, request = client.initialize, client.request
        def initialize_after_guard():
            assert p.guard_active
            p.events.append('initialize')
            return initialize()
        def request_after_guard(method, params=None):
            assert p.guard_active
            p.events.append(method)
            return request(method, params)
        client.initialize, client.request = initialize_after_guard, request_after_guard
        return client
    p.runtime.factory = factory
    original_client = p.runtime._client
    def client(record, recipe, **kwargs):
        p.client_kwargs.append(kwargs)
        return original_client(record, recipe, **kwargs)
    monkeypatch.setattr(p.runtime, '_client', client)
    verify = p.runtime._verify_config
    def verify_config(config, profile):
        p.events.append('verify-config')
        p.verified_profile = profile
        return verify(config, profile)
    monkeypatch.setattr(p.runtime, '_verify_config', verify_config)
    def probe(record, recipe, **kwargs):
        assert p.guard_active
        phase = bool(record.get('thread_id'))
        p.events.append('probe-post-thread' if phase else 'probe-initial')
        p.probes.append(kwargs)
        if exact:
            prepared = kwargs['prepared_model']
            pair = CodexRuntime._checked_model_profile(recipe,
                record['assignment']['kind'] == 'assessment', phase, prepared)
            assert pair[1] is p.verified_profile
            assert any(pair[0] is item[3][0] and pair[1] is item[3][1] and item[:3] == prepared[:3]
                       for item in p.profiles)
        else:
            assert kwargs == {}
        return {'SIMULATED': True}
    monkeypatch.setattr(p.runtime, '_probe_sandbox', probe)
    return p


def test_exact_start_prepares_before_activation_and_reuses_fresh_post_thread_profile(tmp_path, monkeypatch):
    p = startup_fixture(tmp_path, monkeypatch)
    try:
        assignment = claim(p)
        result = p.runtime.start(assignment)
        assert result['state'] == 'active'
        assert p.events.index('prepare-initial') < p.events.index('activate')
        for event in ('cli-version', 'app-server', 'initialize', 'config/read', 'probe-initial',
                      'thread/start', 'prepare-post-thread', 'probe-post-thread', 'turn/start'):
            assert p.events.index('activate') < p.events.index(event)
        assert p.events.index('probe-initial') < p.events.index('thread/start')
        assert p.events.index('thread/start') < p.events.index('prepare-post-thread')
        assert p.events.index('prepare-post-thread') < p.events.index('probe-post-thread')
        assert p.events.index('probe-post-thread') < p.events.index('turn/start')
        assert len(p.profiles) == 2 and [item[2] for item in p.profiles] == [False, True]
        assert p.profiles[0][3][1] is not p.profiles[1][3][1]
        assert p.client_kwargs[0]['prepared_model'][3] is p.profiles[0][3]
        assert [item['prepared_model'][2] for item in p.probes] == [False, True]
        assert len(set(p.clock_fields)) == 1
        assert p.runtime.budget_receipt()['attempts_reserved'] == 1
    finally:
        p.runtime.close()


def test_expired_preparation_cannot_activate_or_launch_or_reset_clock(tmp_path, monkeypatch):
    p = startup_fixture(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(codex_runtime.time, 'monotonic', lambda: clock[0])
    profile = p.runtime._profile
    def expired_profile(recipe, assessment=False, **kwargs):
        result = profile(recipe, assessment, **kwargs)
        if recipe['outputs'] != p.recipe['outputs']:
            clock[0] += p.recipe['max_seconds'] + 1
        return result
    monkeypatch.setattr(p.runtime, '_profile', expired_profile)
    try:
        assignment = claim(p)
        result = p.runtime.start(assignment)
        assert result['state'] != 'active'
        assert p.events == ['prepare-initial']
        assert not p.guard_active and not p.client_kwargs and not p.backend.calls
        assert len(set(p.clock_fields)) == 1 and p.clock_fields[0][0] == 100.0
        record = p.runtime._get(assignment['attempt_id'])
        assert record['server_launch'] == 'not_started' and record['model_calls_attempted'] == 0
        assert 'original startup/assignment deadline' in record['error']
        assert p.runtime.budget_receipt()['attempts_reserved'] == 1
    finally:
        p.runtime.close()


def test_legacy_start_does_not_pass_new_keyword_or_precompute_before_activation(tmp_path, monkeypatch):
    p = startup_fixture(tmp_path, monkeypatch, exact=False)
    try:
        assert p.runtime.start(claim(p))['state'] == 'active'
        assert p.events[0] == 'activate'
        assert p.client_kwargs == [{}] and p.probes == [{}, {}]
        assert p.events.index('activate') < p.events.index('prepare-initial')
        assert len(set(p.clock_fields)) == 1
    finally:
        p.runtime.close()
