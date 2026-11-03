"""Herdr validation must retain scope, exact prerequisites and owned cleanup."""
import errno
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
from types import SimpleNamespace

import pytest

from agentic_tools import check_adapters
from agentic_tools.projects import herdr as validation


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'rust-toolchain.toml').write_text(
        '[toolchain]\nchannel = "1.96.1"\ncomponents = ["clippy", "rustfmt"]\n')
    (source / 'src').mkdir()
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    toolchain = tmp_path / 'rustup/toolchains/1.96.1-aarch64-apple-darwin'
    for name in ('cargo', 'rustc', 'rustfmt', 'clippy-driver'):
        executable(toolchain / 'bin' / name)
    rustlib = toolchain / 'lib/rustlib'
    rustlib.mkdir(parents=True)
    (rustlib / 'multirust-channel-manifest.toml').write_text(
        '[pkg.rustc]\nversion = "1.96.1 (31fca3adb 2026-06-26)"\n')
    windows = rustlib / 'x86_64-pc-windows-msvc/lib'
    windows.mkdir(parents=True)
    (windows / 'libstd-fixture.rlib').write_bytes(b'fixture')
    tools = {name: executable(tmp_path / 'tools' / name)
             for name in ('just', 'cargo-nextest', 'python3', 'bun', 'node', 'zig', 'rustup')}
    home, temporary, sdk = (tmp_path / name for name in ('home', 'temporary', 'sdk'))
    for path in (home / '.cargo', temporary, sdk):
        path.mkdir(parents=True)
    entries = []
    for key, marker in (('include_dir', 'stdlib.h'), ('sys_include_dir', 'vcruntime.h'),
                        ('crt_dir', 'ucrt.lib'), ('msvc_lib_dir', 'vcruntime.lib'),
                        ('kernel32_lib_dir', 'kernel32.lib')):
        folder = tmp_path / 'windows-sdk' / key
        folder.mkdir(parents=True)
        (folder / marker).write_bytes(b'fixture')
        entries.append(f'{key}={folder}\n')
    libc = tmp_path / 'windows-sdk/libc.txt'
    libc.write_text(''.join(entries))
    env = {'HOME': str(home), 'TMPDIR': str(temporary), 'SDKROOT': str(sdk),
           'LIBGHOSTTY_VT_WINDOWS_LIBC': str(libc), 'PATH': '/untrusted/bin',
           'HERDR_SOCKET_PATH': '/existing/session.sock', 'CARGO_TARGET_DIR': '/other/target',
           'RUSTC_WRAPPER': '/untrusted/wrapper', 'TOKEN': 'private'}
    return SimpleNamespace(source=source, outputs=outputs, toolchain=toolchain, tools=tools, env=env)


def prepared(host):
    return validation.prepare_herdr_environment(
        host.source, toolchain_root=host.toolchain, tools=host.tools,
        runtime_read_roots=[host.tools['just'].parent], inherited_env=host.env)


def probe_results(environment):
    results = {name: {'argv': argv, 'exit_code': 0, 'stdout': name + ' version'}
               for name, argv in environment['version_probes'].items()}
    results['rustc']['stdout'] = 'rustc 1.96.1 (31fca3adb 2026-06-26)'
    results['cargo']['stdout'] = 'cargo 1.96.1 (fixture)'
    results['zig']['stdout'] = '0.16.0\n'
    for name, output in {'rustfmt': 'rustfmt 1.9.0-stable', 'clippy-driver': 'clippy 0.1.96',
                         'just': 'just 1.59.0', 'cargo-nextest': 'cargo-nextest 0.9.150',
                         'python3': 'Python 3.12.9', 'bun': '1.3.14',
                         'node': 'v26.13.0', 'rustup': 'rustup 1.29.2'}.items():
        results[name]['stdout'] = output
    return results


def test_environment_uses_supplied_pin_and_normal_cache_without_inheriting_selectors(host):
    result = prepared(host)
    values = result['env']
    assert values['RUSTC'] == str(host.toolchain / 'bin/rustc')
    assert values['RUSTUP_HOME'] == str(host.toolchain.parent.parent)
    assert values['CARGO_HOME'] == str(Path(host.env['HOME']) / '.cargo')
    assert values['TMPDIR'] == host.env['TMPDIR']
    assert values['RUSTUP_AUTO_INSTALL'] == '0'
    assert values['CARGO_NET_OFFLINE'] == 'true'
    assert not {'TOKEN', 'HERDR_SOCKET_PATH', 'CARGO_TARGET_DIR', 'RUSTC_WRAPPER'} & values.keys()
    assert '/untrusted' not in values['PATH']
    assert {'path': str(host.source / 'target'), 'kind': 'generated'} in result['native_write_roots']
    cargo = Path(host.env['HOME']) / '.cargo'
    cargo_writes = [row for row in result['native_write_roots'] if Path(row['path']).is_relative_to(cargo)]
    assert len(cargo_writes) == 6 and all(row['kind'] == 'cache-file' for row in cargo_writes)
    assert all(Path(row['path']).name not in ('registry', 'git') for row in cargo_writes)
    assert not (host.source / 'target').exists()


def test_missing_compiler_is_not_hidden_by_cargo_and_clippy(host):
    (host.toolchain / 'bin/rustc').unlink()
    with pytest.raises(FileNotFoundError):
        prepared(host)


def test_missing_windows_target_is_reported_without_installing(host):
    (host.toolchain / 'lib/rustlib/x86_64-pc-windows-msvc/lib/libstd-fixture.rlib').unlink()
    with pytest.raises(ValueError, match='already be installed'):
        prepared(host)


def test_partial_windows_sdk_is_not_ready(host):
    Path(host.env['LIBGHOSTTY_VT_WINDOWS_LIBC']).write_text('include_dir=/missing\n')
    with pytest.raises(ValueError, match='incomplete'):
        prepared(host)


def test_unpinned_project_or_wrong_install_metadata_is_rejected(host):
    (host.source / 'rust-toolchain.toml').write_text(
        '[toolchain]\nchannel = "stable"\ncomponents = ["clippy", "rustfmt"]\n')
    with pytest.raises(ValueError, match='exact project Rust'):
        prepared(host)


def test_versions_require_exact_probe_commands_and_success(host):
    environment = prepared(host)
    results = probe_results(environment)
    assert validation.validate_herdr_versions(environment, results)['verified']
    results['rustc']['stdout'] = 'rustc 1.99.1 (other)'
    with pytest.raises(ValueError, match='project Rust'):
        validation.validate_herdr_versions(environment, results)
    results = probe_results(environment)
    results['cargo']['argv'] = ['/other/cargo', '--version']
    with pytest.raises(ValueError, match='did not succeed'):
        validation.validate_herdr_versions(environment, results)
    results = probe_results(environment)
    results['bun']['exit_code'] = True
    with pytest.raises(ValueError, match='did not succeed'):
        validation.validate_herdr_versions(environment, results)


def recipe(host, **overrides):
    arguments = {'environment': prepared(host), 'author': 'implementer', 'reviewer': 'reviewer',
                 'source_scope': ['src'], 'focused_tests': ['client::metrics::tests::freshness'],
                 'selected_checks': ['native-source', 'render-scaling'],
                 'impact_reason': 'Provider and grid change; native counters and render scaling are affected.'}
    arguments.update(overrides)
    return validation.build_herdr_recipe(host.source, host.outputs, **arguments)


def test_recipe_orders_focused_checks_before_explicit_final_validation(host):
    result = recipe(host)
    assert [c['name'] for c in result['checks']] == [
        'focused-1', 'native-source', 'render-scaling', 'full-native-check']
    assert all(check_adapters.select(c) for c in result['checks'])
    assert result['checks'][-1]['argv'] == [str(host.tools['just']), 'check']
    assert result['checks'][-1]['suite_counts'] == {'nextest': 1, 'unittest': 2, 'bun': 5}
    assert '--exact' in result['checks'][0]['argv']
    assert '--ignored' in result['checks'][1]['argv']
    assert result['env']['XDG_CONFIG_HOME'] == str(host.outputs / 'config')
    assert result['automated_ipc_is_human_acceptance'] is False
    assert not {'HOME', 'TMPDIR', 'CARGO_HOME', 'GIT_CONFIG_COUNT'} & result['native_environment'].keys()
    assert result['native_environment']['NEXTEST_USER_CONFIG_FILE'] == 'none'
    assert not any(row['path'] == str(host.outputs) for row in result['native_write_roots'])


@pytest.mark.parametrize('override', [
    {'reviewer': 'implementer'}, {'impact_reason': ''}, {'source_scope': ['../elsewhere']},
    {'source_scope': ['.git/config']}, {'selected_checks': ['arbitrary-command']},
    {'focused_tests': ['name; touch other']}, {'selected_checks': ['native-source', 'native-source']},
])
def test_recipe_rejects_unreviewed_or_unbounded_selection(host, override):
    with pytest.raises(ValueError):
        recipe(host, **override)


def test_source_scope_cannot_follow_symlink_outside_checkout(host):
    (host.source / 'escape').symlink_to(host.outputs, target_is_directory=True)
    with pytest.raises(ValueError, match='escapes'):
        recipe(host, source_scope=['escape'])


def test_empty_test_selection_still_retains_final_suite(host):
    result = recipe(host, selected_checks=[], focused_tests=[])
    assert [c['name'] for c in result['checks']] == ['full-native-check']



def test_local_native_tests_are_explicit_host_configuration_not_model_authority(host):
    assert 'native_local_tests' not in recipe(host)
    contract = {'ephemeral_tcp': {'first': 49152, 'last': 65535},
                'denied_tcp_ports': [8443, 8444]}
    result = recipe(host, native_local_tests=contract)
    assert result['native_local_tests'] == contract
    assert 'native_local_tests' not in result['native_environment']
    contract['denied_tcp_ports'].append(9000)
    assert result['native_local_tests']['denied_tcp_ports'] == [8443, 8444]
    assert 'native_local_tests' not in recipe(host)


@pytest.mark.parametrize('contract', [
    {'ephemeral_tcp': {'first': 1024, 'last': 65535}, 'denied_tcp_ports': [8443]},
    {'ephemeral_tcp': {'first': True, 'last': 65535}, 'denied_tcp_ports': []},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8444, 8443]},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8443, 8443]},
    {'ephemeral_tcp': {'first': 49152, 'last': 65535}, 'denied_tcp_ports': [8443], 'allow_remote': True},
])
def test_local_native_tests_reject_broadened_or_ambiguous_host_contract(host, contract):
    with pytest.raises(ValueError, match='Native local tests'):
        recipe(host, native_local_tests=contract)


def test_log_capture_stays_bounded_while_drain_reaches_eof(monkeypatch):
    chunks = iter([b'x' * (validation._MAX_LOG + 300), b''])
    monkeypatch.setattr(validation.select, 'select', lambda *args: ([99], [], []))
    monkeypatch.setattr(validation.os, 'read', lambda *args: next(chunks))
    stream = {'eof': False, 'bytes': 0, 'retained_bytes': 0, 'error': None}
    destination = io.BytesIO()
    validation._drain_fd(99, stream, destination, threading.Event())
    assert stream['eof'] and stream['error'] is None
    assert stream['bytes'] == validation._MAX_LOG + 300
    assert len(destination.getvalue()) == stream['retained_bytes'] == validation._MAX_LOG


def test_trickling_ping_cannot_extend_absolute_deadline(monkeypatch):
    class Trickle:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def sendall(self, message): pass
        def recv(self, size): return b'x'
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr(validation.socket, 'socket', lambda *args: Trickle())
    monkeypatch.setattr(validation.time, 'monotonic', lambda: next(clock))
    with pytest.raises(TimeoutError, match='Absolute'):
        validation._ping(0.35)


class StopProcess:
    def __init__(self, *, timeout=False, stubborn=False):
        self.pid = 54321
        self.returncode = None
        self.timeout, self.stubborn = timeout, stubborn
        self.signals = []
        self.stdout, self.stderr = self.pipe(b'stop output\n'), self.pipe(b'')
    @staticmethod
    def pipe(contents):
        read, write = os.pipe()
        os.write(write, contents)
        os.close(write)
        return os.fdopen(read, 'rb', buffering=0)
    def wait(self, timeout):
        if self.stubborn or self.timeout and not self.signals:
            raise subprocess.TimeoutExpired('owned stop', timeout)
        self.returncode = -signal.SIGINT if self.signals else 0
        return self.returncode
    def poll(self):
        return self.returncode
    def send_signal(self, value):
        self.signals.append(value)


@pytest.mark.parametrize('timeout,stubborn,normal,reaped', [
    (False, False, True, True), (True, False, False, True), (True, True, False, False)])
def test_stop_command_retains_timeouts_and_never_forces_kill(
        tmp_path, monkeypatch, timeout, stubborn, normal, reaped):
    process = StopProcess(timeout=timeout, stubborn=stubborn)
    monkeypatch.setattr(validation.subprocess, 'Popen', lambda *args, **kwargs: process)
    result = validation._stop_command(Path('/owned/herdr'), tmp_path, {}, 1)
    assert result['pid'] == process.pid
    assert result['normal'] is normal and result['reaped'] is reaped
    assert result['timed_out'] is timeout
    assert process.signals == ([signal.SIGINT] if timeout else [])
    assert result['readers_joined']
    assert all(s['eof'] for s in result['streams'])


def test_stop_reader_setup_failure_still_interrupts_and_reaps_owned_child(tmp_path, monkeypatch):
    process = StopProcess()
    monkeypatch.setattr(validation.subprocess, 'Popen', lambda *args, **kwargs: process)
    original = Path.open
    def fail_stderr(path, *args, **kwargs):
        if path.name == 'stop.stderr':
            raise OSError('retained log unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_stderr)
    result = validation._stop_command(Path('/owned/herdr'), tmp_path, {}, 1)
    assert process.signals == [signal.SIGINT]
    assert result['reaped'] and not result['normal']
    assert any('retained log unavailable' in error for error in result['errors'])


def test_inline_multithreaded_session_rejected_before_any_output(tmp_path, monkeypatch):
    monkeypatch.setattr(validation.threading, 'active_count', lambda: 2)
    with pytest.raises(ValueError, match='dedicated'):
        validation.run_owned_herdr_session(Path('/missing'), tmp_path, environment={})
    assert not (tmp_path / 'herdr-ipc').exists()


def test_existing_runtime_is_preserved_without_launch(tmp_path, monkeypatch):
    binary = executable(tmp_path / 'herdr')
    runtime = tmp_path / 'herdr-ipc'
    runtime.mkdir()
    marker = runtime / 'retained-evidence'
    marker.write_text('keep')
    monkeypatch.setattr(validation.threading, 'active_count', lambda: 1)
    with pytest.raises(FileExistsError):
        validation.run_owned_herdr_session(binary, tmp_path, environment={})
    assert marker.read_text() == 'keep'


def test_fork_failure_restores_cwd_and_retains_failed_receipt(tmp_path, monkeypatch):
    import pty
    binary = executable(tmp_path / 'herdr')
    previous = Path.cwd()
    monkeypatch.setattr(validation.threading, 'active_count', lambda: 1)
    monkeypatch.setattr(pty, 'fork', lambda: (_ for _ in ()).throw(OSError('fork denied')))
    result = validation.run_owned_herdr_session(binary, tmp_path, environment={})
    assert Path.cwd() == previous
    assert not result['ipc_passed'] and not result['server_reaped']
    assert any('fork denied' in error for error in result['errors'])
    assert json.loads((tmp_path / 'herdr-ipc/result.json').read_text()) == result


def test_ownership_write_failure_after_fork_still_stops_only_owned_session(tmp_path, monkeypatch):
    import pty
    binary = executable(tmp_path / 'herdr')
    previous = Path.cwd()
    read, write = os.pipe()
    os.close(write)
    finished = []
    calls = []
    monkeypatch.setattr(validation.threading, 'active_count', lambda: 1)
    monkeypatch.setattr(pty, 'fork', lambda: (12345, read))
    monkeypatch.setattr(validation.os, 'waitpid', lambda pid, flags: (pid, 0) if finished else (0, 0))
    original = Path.write_text
    def fail_ownership(path, *args, **kwargs):
        if path.name == 'ownership.json':
            raise OSError('ownership receipt failed')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_ownership)
    def stop(binary, runtime, environment, seconds):
        calls.append((runtime, environment))
        finished.append(True)
        return {'normal': True}
    monkeypatch.setattr(validation, '_stop_command', stop)
    result = validation.run_owned_herdr_session(binary, tmp_path,
        environment={'PATH': '/usr/bin', 'HERDR_SOCKET_PATH': '/existing.sock', 'TOKEN': 'secret'})
    assert Path.cwd() == previous
    assert result['server_pid'] == 12345 and result['server_reaped']
    assert not result['ipc_passed'] and not result['human_ui_acceptance']
    assert len(calls) == 1 and calls[0][0] == tmp_path / 'herdr-ipc'
    assert calls[0][1]['HERDR_SOCKET_PATH'] == './h.sock'
    assert 'TOKEN' not in calls[0][1]
    assert calls[0][1]['GIT_CONFIG_COUNT'] == '1'
    assert calls[0][1]['GIT_CONFIG_KEY_0'] == 'core.fsmonitor'
    assert calls[0][1]['GIT_CONFIG_VALUE_0'] == 'false'
    assert 'descendants require caller proof' in result['cleanup_scope']


@pytest.mark.parametrize('is_pty,expected_eof', [(False, False), (True, True)])
def test_only_pty_eio_is_end_of_stream(monkeypatch, is_pty, expected_eof):
    monkeypatch.setattr(validation.select, 'select', lambda *args: ([99], [], []))
    def fail_read(*args):
        raise OSError(errno.EIO, 'input/output failure')
    monkeypatch.setattr(validation.os, 'read', fail_read)
    stream = {'eof': False, 'bytes': 0, 'retained_bytes': 0, 'error': None}
    validation._drain_fd(99, stream, io.BytesIO(), threading.Event(), is_pty)
    assert stream['eof'] is expected_eof
    assert (stream['error'] is None) is is_pty


class CloseFailure(io.BytesIO):
    def close(self):
        raise OSError('log close failure')


def test_stop_log_close_failure_remains_failed_after_reaping(tmp_path, monkeypatch):
    process = StopProcess()
    monkeypatch.setattr(validation.subprocess, 'Popen', lambda *args, **kwargs: process)
    original = Path.open
    def failing_log(path, *args, **kwargs):
        return CloseFailure() if path.name == 'stop.stdout' else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', failing_log)
    result = validation._stop_command(Path('/owned/herdr'), tmp_path, {}, 1)
    assert result['reaped'] and not result['normal']
    assert any('log close failure' in error for error in result['errors'])


def test_server_log_close_failure_still_restores_cwd(tmp_path, monkeypatch):
    import pty
    binary = executable(tmp_path / 'herdr')
    previous = Path.cwd()
    monkeypatch.setattr(validation.threading, 'active_count', lambda: 1)
    monkeypatch.setattr(pty, 'fork', lambda: (_ for _ in ()).throw(OSError('fork denied')))
    original = Path.open
    def failing_log(path, *args, **kwargs):
        return CloseFailure() if path.name == 'server.pty' else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', failing_log)
    result = validation.run_owned_herdr_session(binary, tmp_path, environment={})
    assert Path.cwd() == previous and not result['ipc_passed']
    assert any('log close failure' in error for error in result['errors'])


def test_tools_require_explicit_installed_runtime_prefix(host):
    unrelated = host.outputs / 'unrelated-runtime'
    unrelated.mkdir()
    with pytest.raises(ValueError, match='explicit installed runtime'):
        validation.prepare_herdr_environment(host.source, toolchain_root=host.toolchain,
            tools=host.tools, runtime_read_roots=[unrelated], inherited_env=host.env)


def test_recipe_protects_default_and_supplied_live_sessions(host):
    custom = host.outputs.parent / 'existing-herdr-session'
    result = recipe(host, live_session_roots=[custom])
    home = Path(host.env['HOME'])
    assert result['protected_roots'] == [
        str(home / '.config/herdr'), str(home / '.config/herdr-dev'), str(custom)]
    with pytest.raises(ValueError, match='overlap'):
        recipe(host, live_session_roots=[host.outputs])
    with pytest.raises(ValueError, match='absolute'):
        recipe(host, live_session_roots=[Path('relative-session')])


def test_every_supported_check_has_a_bounded_scoped_adapter_descriptor(host):
    result = recipe(host, selected_checks=['native-source', 'metrics-profile', 'render-scaling'])
    assert {check['name'] for check in result['checks']} == {
        'focused-1', 'native-source', 'metrics-profile', 'render-scaling', 'full-native-check'}
    for check in result['checks']:
        assert {'name', 'argv', 'scope', 'timeout_seconds', 'adapter_id', 'result_kind'} <= check.keys()
        assert isinstance(check['scope'], str) and len(check['scope'].strip()) >= 20
        assert isinstance(check['argv'], list) and all(isinstance(arg, str) and arg for arg in check['argv'])
        assert type(check['timeout_seconds']) is int and 1 <= check['timeout_seconds'] <= 1800
        assert check_adapters.select(check).ADAPTER_ID == check['adapter_id']


def test_explicit_retained_zig_compatibility_tool_is_used_for_probes_and_builds(host):
    wrapper = executable(host.tools['zig'].parent / 'retained-zig-compat.sh')
    host.tools['zig'] = wrapper
    environment = prepared(host)
    assert environment['tools']['zig'] == str(wrapper)
    assert environment['env']['ZIG'] == str(wrapper)
    assert environment['version_probes']['zig'] == [str(wrapper), 'version']
    result = recipe(host)
    assert result['native_environment']['ZIG'] == str(wrapper)
    assert all(check['argv'][0] != str(wrapper) for check in result['checks'])


def test_full_suite_descriptor_does_not_recognize_build_only_output(host, tmp_path):
    check = recipe(host, selected_checks=[], focused_tests=[])['checks'][0]
    adapter = check_adapters.select(check)
    result = adapter.interpret(tmp_path, '', 'Finished dev profile successfully\n', 0)
    assert not result['recognized']
    assert result['selected'] == result['passed'] == 0
