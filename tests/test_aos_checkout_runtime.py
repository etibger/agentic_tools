"""Checkout endpoints and probe failures use fake process/kernel observations."""
import io
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from agentic_tools.aos import checkout_runtime as runtime
from agentic_tools.aos.store import Rejected


@pytest.fixture
def endpoint(tmp_path):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir()
    outputs.mkdir()
    return {'source': str(source), 'outputs': str(outputs),
            'checkout_policy': {'endpoint_limits': {'minimum_free_bytes': 100,
                'max_owned_growth_bytes': 20, 'max_output_bytes': 30}}}


def test_endpoint_counts_owned_generated_files_without_shared_cache(endpoint, tmp_path, monkeypatch):
    source, outputs = Path(endpoint['source']), Path(endpoint['outputs'])
    (source / 'main').write_bytes(b'abc')
    target = source / 'target'
    target.mkdir()
    (target / 'artifact').write_bytes(b'data')
    os.link(target / 'artifact', target / 'artifact-alias')
    (outputs / 'report').write_bytes(b'out')
    (tmp_path / 'shared-cache').write_bytes(b'x' * 1000)
    monkeypatch.setattr(runtime.shutil, 'disk_usage', lambda p: SimpleNamespace(
        free=800 if str(p) == str(source) else 900))
    observed = runtime.observe_resources(endpoint)
    assert observed['owned_bytes'] == 10
    assert observed['output_bytes'] == 3
    assert observed['free_bytes'] == 800


def test_endpoint_rejects_external_symlink_without_following_it(endpoint, tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('preserve')
    (Path(endpoint['outputs']) / 'alias').symlink_to(outside)
    with pytest.raises(Rejected, match='link or special'):
        runtime.observe_resources(endpoint)
    assert outside.read_text() == 'preserve'


def test_expired_endpoint_deadline_rejects_even_empty_roots(endpoint, monkeypatch):
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 10.0)
    with pytest.raises(Rejected, match='deadline'):
        runtime.observe_resources(endpoint, deadline=9.0)


@pytest.mark.parametrize('after,reason', [
    ({'owned_bytes': 121, 'output_bytes': 20, 'free_bytes': 100}, 'growth'),
    ({'owned_bytes': 100, 'output_bytes': 31, 'free_bytes': 100}, 'output'),
    ({'owned_bytes': 100, 'output_bytes': 20, 'free_bytes': 99}, 'disk space'),
])
def test_endpoint_limit_failures_retain_measurements(endpoint, after, reason):
    before = {'owned_bytes': 100}
    with pytest.raises(Rejected, match=reason) as failure:
        runtime.enforce_endpoints(endpoint, before, after)
    assert failure.value.receipt['before'] == before
    assert failure.value.receipt['after'] == after
    assert failure.value.receipt['passed'] is False


def test_endpoint_exact_limits_pass_and_deleted_bytes_do_not_create_credit(endpoint):
    before = {'owned_bytes': 100}
    after = {'owned_bytes': 120, 'output_bytes': 30, 'free_bytes': 100}
    assert runtime.enforce_endpoints(endpoint, before, after)['observed_growth_bytes'] == 20
    after['owned_bytes'] = 80
    assert runtime.enforce_endpoints(endpoint, before, after)['observed_growth_bytes'] == 0


def test_endpoint_boolean_is_not_a_byte_measurement(endpoint):
    with pytest.raises(Rejected, match='incomplete'):
        runtime.enforce_endpoints(endpoint, {'owned_bytes': 0},
            {'owned_bytes': True, 'output_bytes': 0, 'free_bytes': 100})


def identity(pid, *, parent=1, group=100, birth=1):
    return {'pid': pid, 'uid': 123, 'birth': [7, birth],
            'parent': parent, 'group': group, 'status': 2}


def install_kernel(monkeypatch, live):
    monkeypatch.setattr(runtime.native_processes, 'identity',
                        lambda pid: dict(live[pid]) if pid in live else None)
    monkeypatch.setattr(runtime, '_rows', lambda *args, **kwargs: {
        pid: {key: row[key] for key in ('parent', 'group', 'uid')}
        for pid, row in live.items()})


def test_reused_retained_pid_cannot_seed_ownership(monkeypatch):
    owner = identity(100)
    previous = identity(101, parent=100, birth=2)
    live = {100: owner, 101: identity(101, group=900, birth=3),
            102: identity(102, parent=101, group=900),
            103: identity(103, parent=100)}
    install_kernel(monkeypatch, live)
    assert set(runtime._owned(owner, {101: previous})) == {103}


def test_discovery_rechecks_live_parent_identity(monkeypatch):
    owner = identity(100)
    reads = 0
    def lookup(pid):
        nonlocal reads
        if pid == 100:
            reads += 1
            return owner if reads == 1 else identity(100, birth=9)
        return identity(101, parent=100)
    monkeypatch.setattr(runtime.native_processes, 'identity', lookup)
    monkeypatch.setattr(runtime, '_rows', lambda *args, **kwargs: {
        101: {'parent': 100, 'group': 100, 'uid': 123}})
    assert runtime._owned(owner, {}) == {}


def test_prelaunch_deadline_keeps_known_no_launch_cleanup(tmp_path, monkeypatch):
    control = tmp_path / 'control'
    control.mkdir()
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 10.0)
    monkeypatch.setattr(runtime.subprocess, 'Popen',
                        lambda *args, **kwargs: pytest.fail('Expired probe must not launch'))
    with pytest.raises(Rejected):
        runtime._run_probe(['unused'], cwd=tmp_path, environment={}, control=control, deadline=12.0)
    record = json.loads((control / 'process.json').read_text())
    assert record['cleanup_verified'] is True
    assert record.get('owner') is None
    assert record['phase'] != 'cleanup_unknown'


def test_supervisor_loss_retains_known_children_and_does_not_claim_old_group(tmp_path, monkeypatch):
    control = tmp_path / 'control'
    control.mkdir()
    owner, known = identity(100), identity(101, parent=100, birth=2)
    live = {100: owner, 101: known}
    install_kernel(monkeypatch, live)
    monkeypatch.setattr(runtime.os, 'getuid', lambda: 123)
    clock = SimpleNamespace(now=0.0, lost=False)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock.now)
    def sleep(seconds):
        clock.now += seconds
        if not clock.lost:
            clock.lost = True
            live.pop(100)
            live[101] = {**known, 'parent': 1, 'group': 101}
            live[102] = identity(102, parent=1, group=100, birth=8)
    monkeypatch.setattr(runtime.time, 'sleep', sleep)
    sent = []
    def interrupt(pid, sig):
        sent.append((pid, sig))
        assert pid == 101 and sig == signal.SIGINT
        live.pop(pid)
    monkeypatch.setattr(runtime.os, 'kill', interrupt)
    class Supervisor:
        pid = 100
        stdin = io.BytesIO()
        returncode = None
        def poll(self):
            self.returncode = None if 100 in live else 1
            return self.returncode
        def wait(self, timeout):
            return self.poll()
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs: Supervisor())
    with pytest.raises(Rejected):
        runtime._run_probe(['unused'], cwd=tmp_path, environment={}, control=control, deadline=20.0)
    record = json.loads((control / 'process.json').read_text())
    assert sent == [(101, signal.SIGINT)]
    assert 102 in live
    assert any(row['pid'] == 101 and row['birth'] == known['birth']
               for row in record['observed'])
    assert record['phase'] == 'cleanup_unknown'
    assert record['cleanup_verified'] is False


def test_direct_probe_dispatch_prepares_scoped_outputs_before_runner(tmp_path, monkeypatch):
    import copy
    import sys
    from agentic_tools.aos import checkout_boundary, codex_runtime, instructions

    root = tmp_path.resolve()
    for name in ('source', 'outputs', 'state', 'home', 'native-tmp'):
        (root / name).mkdir()
    source, output, state, home = (root / name for name in ('source', 'outputs', 'state', 'home'))
    (source / 'code.py').write_text('unchanged source\n')
    (output / 'run').mkdir()
    selected = output / 'probe'
    selected.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setenv('TMPDIR', str(root / 'native-tmp'))
    recipe = {'execution_policy': checkout_boundary.POLICY, 'source': str(source),
        'outputs': str(output), 'read_roots': [str(Path(sys.base_prefix).resolve())],
        'probe_interpreter': str(Path(sys.executable).resolve()), 'instruction_manifest': {},
        'checkout_policy': {'change_roots': ['code.py'], 'protected_roots': [],
            'native_environment': {'XDG_RUNTIME_DIR': str(output / 'run')},
            'endpoint_limits': {'minimum_free_bytes': 100, 'max_owned_growth_bytes': 100,
                                'max_output_bytes': 100}}}
    original = copy.deepcopy(recipe)
    monkeypatch.setattr(instructions, 'validate', lambda *args, **kwargs: [])
    monkeypatch.setattr(instructions, 'runtime_overrides', lambda *args: [])
    monkeypatch.setattr(codex_runtime, 'bundled_codex_path', lambda: Path('/SIMULATED/codex'))
    profiles = []
    real_profile = codex_runtime.CodexRuntime._profile
    def profile(self, scoped, *args, **kwargs):
        profiles.append(copy.deepcopy(scoped))
        return real_profile(self, scoped, *args, **kwargs)
    monkeypatch.setattr(codex_runtime.CodexRuntime, '_profile', profile)
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        payload = json.loads(argv[-1])
        assert payload['state'] == str(state)
        assert Path(payload['output_canary']).parent == selected
        assert Path(payload['control']).is_relative_to(state / 'proof')
        assert (selected / 'run').is_dir()
        checks = {name: True for name in ('state_read_denied', 'state_write_denied',
            'control_read_denied', 'credentials_denied', 'credential_config_denied',
            'output_write', 'source_access', 'network_denied')}
        (kwargs['control'] / 'stdout.txt').write_text(json.dumps(checks))
        return {'child': {'exit_code': 0}, 'cleanup_verified': True, 'simulated': True}
    monkeypatch.setattr(runtime, '_run_probe', runner)
    monkeypatch.setattr(runtime.subprocess, 'Popen',
                        lambda *args, **kwargs: pytest.fail('The direct dispatch test must not launch'))
    result = runtime.probe(recipe, state_dir=state, output=selected, control=state / 'proof',
                           role='writer', deadline=runtime.time.monotonic() + 30)
    assert len(calls) == len(profiles) == 1
    assert profiles[0]['outputs'] == str(selected)
    assert profiles[0]['checkout_policy']['native_environment']['XDG_RUNTIME_DIR'] == str(selected / 'run')
    assert {str(state), str(state / 'proof')}.issubset(profiles[0]['checkout_policy']['protected_roots'])
    assert result['cleanup']['simulated'] is True and result['model_calls'] == 0
    assert recipe == original
    assert (source / 'code.py').read_text() == 'unchanged source\n'


def test_endpoint_counts_internal_link_bytes_and_its_target_only_once(endpoint, monkeypatch):
    from agentic_tools.aos import checkout_boundary
    endpoint['execution_policy'] = checkout_boundary.POLICY
    endpoint['checkout_policy']['change_roots'] = ['.']
    source, output = Path(endpoint['source']), Path(endpoint['outputs'])
    target = source / 'AGENTS.md'
    target.write_bytes(b'policy')
    link = source / 'CLAUDE.md'
    link.symlink_to('AGENTS.md')
    (output / 'report').write_bytes(b'out')
    monkeypatch.setattr(runtime.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1000))
    observed = runtime.observe_resources(endpoint)
    assert observed['owned_bytes'] == len(b'policy') + link.lstat().st_size + len(b'out')
    assert observed['output_bytes'] == len(b'out')
    assert target.read_bytes() == b'policy' and os.readlink(link) == 'AGENTS.md'


def test_endpoint_counts_generated_alias_chains_without_following_targets(endpoint, tmp_path, monkeypatch):
    from agentic_tools.aos import checkout_boundary, native_paths
    endpoint['execution_policy'] = checkout_boundary.POLICY
    endpoint['checkout_policy']['change_roots'] = ['code.py']
    source, output = Path(endpoint['source']), Path(endpoint['outputs'])
    generated = source / 'target'
    generated.mkdir()
    endpoint['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    (source / 'code.py').write_bytes(b'code')
    artifact = generated / 'libexample.1.2.dylib'
    artifact.write_bytes(b'generated artifact')
    outside = tmp_path / 'shared-installation'
    outside.mkdir()
    (outside / 'large-library').write_bytes(b'x' * 4096)
    targets = {'libexample.1.dylib': artifact.name,
               'libexample.dylib': 'libexample.1.dylib',
               'dangling-cache-entry': 'not-created-yet',
               'external-directory': str(outside),
               'external-file': str(outside / 'large-library')}
    links = []
    for name, target in targets.items():
        path = generated / name
        path.symlink_to(target)
        links.append(path)
    (output / 'report').write_bytes(b'out')
    original_iterdir = Path.iterdir
    def no_external_scan(path):
        if path == outside or path == generated / 'external-directory':
            pytest.fail('Generated link accounting followed an external directory')
        return original_iterdir(path)
    monkeypatch.setattr(Path, 'iterdir', no_external_scan)
    monkeypatch.setattr(runtime.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10000))
    observed = runtime.observe_resources(endpoint)
    assert observed['owned_bytes'] == (len(b'code') + len(b'generated artifact')
                                       + sum(path.lstat().st_size for path in links) + len(b'out'))
    assert observed['output_bytes'] == len(b'out')
    native = native_paths.snapshot(endpoint)[str(generated)]['entries']
    assert all(native[name]['kind'] == 'symlink' and native[name]['target'] == target
               for name, target in targets.items())
    assert (outside / 'large-library').read_bytes() == b'x' * 4096
