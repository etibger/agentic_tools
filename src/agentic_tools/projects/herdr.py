"""Maintained Herdr checks and one owned IPC session.

This module selects existing project commands; it is not a scheduler or an
approval mechanism. Readiness owns version probes and the native process
boundary. Automated IPC evidence never substitutes for human UI acceptance.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import threading
import time
import tomllib
from typing import Mapping, Sequence

from agentic_tools import check_adapters

_REQUIRED_TOOLS = ('just', 'cargo-nextest', 'python3', 'bun', 'node', 'zig', 'rustup')
_RUST_TOOLS = ('cargo', 'rustc', 'rustfmt', 'clippy-driver')
_HOST_ENV = ('HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SDKROOT', 'CARGO_HOME',
             'LIBGHOSTTY_VT_WINDOWS_LIBC')
_SESSION_ENV = (*_HOST_ENV, 'PATH', 'RUSTUP_HOME', 'RUSTUP_TOOLCHAIN',
                'RUSTUP_AUTO_INSTALL', 'CARGO_NET_OFFLINE')
_NATIVE_SOURCE = 'platform::macos::metrics::tests::herdr_metrics_per_core_native_source'
_METRICS_PROFILE = 'client::shell::tests::herdr_metrics::herdr_metrics_per_core_actual_source_profile'
_MAX_LOG = 1024 * 1024
_CARGO_CACHE_FILES = ('.package-cache', '.package-cache-mutate', '.global-cache',
                      '.global-cache-journal', '.global-cache-wal', '.global-cache-shm')
_NATIVE_ENV_KEYS = {'PATH', 'ZIG', 'SDKROOT', 'RUSTC', 'RUSTUP_HOME', 'RUSTUP_TOOLCHAIN',
                    'RUSTUP_AUTO_INSTALL', 'CARGO_NET_OFFLINE', 'CARGO_BUILD_JOBS',
                    'NEXTEST_TEST_THREADS', 'NEXTEST_USER_CONFIG_FILE', 'LIBGHOSTTY_VT_WINDOWS_LIBC'}



def _directory(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f'Directory required: {path}')
    return path


def _executable(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f'Existing executable required: {path}')
    return path


def prepare_herdr_environment(source: Path, *, toolchain_root: Path,
                              tools: Mapping[str, Path], runtime_read_roots: Sequence[Path],
                              inherited_env: Mapping[str, str]) -> dict:
    """Resolve a supplied installation without executing, installing or copying it.

    The caller supplies trusted host configuration, not model-authored paths.
    Cargo keeps its ordinary cache; build output remains the project's target/.
    """
    source, toolchain = _directory(source), _directory(toolchain_root)
    spec = tomllib.loads((source / 'rust-toolchain.toml').read_text())['toolchain']
    pin = spec.get('channel', '')
    if not isinstance(pin, str) or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pin) is None:
        raise ValueError('Herdr requires an exact project Rust version')
    if not {'clippy', 'rustfmt'}.issubset(spec.get('components', [])):
        raise ValueError('Herdr toolchain must declare clippy and rustfmt')
    if toolchain.parent.name != 'toolchains' or not toolchain.name.startswith(pin + '-'):
        raise ValueError('Supply the matching installed rustup toolchain directory')
    runtime_roots = list(dict.fromkeys(_directory(path) for path in runtime_read_roots))
    if not runtime_roots or len(runtime_roots) > 24:
        raise ValueError('Configure one to 24 existing installed runtime roots')
    if any(path == Path('/') or path == Path(inherited_env.get('HOME', '/')).resolve()
           or source == path or source.is_relative_to(path) for path in runtime_roots):
        raise ValueError('Installed runtime roots must not cover the checkout or whole home')
    resolved = {name: _executable(toolchain / 'bin' / name) for name in _RUST_TOOLS}
    for name in _REQUIRED_TOOLS:
        if name not in tools:
            raise ValueError(f'Missing configured Herdr tool: {name}')
        resolved[name] = _executable(tools[name])
    available_roots = [toolchain.parent.parent, *runtime_roots]
    if any(not any(path.is_relative_to(root) for root in available_roots) for path in resolved.values()):
        raise ValueError('Every configured tool needs an explicit installed runtime read root')
    manifest = toolchain / 'lib/rustlib/multirust-channel-manifest.toml'
    recorded = tomllib.loads(manifest.read_text())['pkg']['rustc']['version']
    if recorded.split()[0] != pin:
        raise ValueError('Installed toolchain metadata differs from the project Rust pin')
    windows = toolchain / 'lib/rustlib/x86_64-pc-windows-msvc/lib'
    if not windows.is_dir() or not any(windows.glob('libstd-*.rlib')):
        raise ValueError('Windows Rust standard library must already be installed')
    values = {key: str(inherited_env[key]) for key in _HOST_ENV if key in inherited_env}
    for key in ('HOME', 'TMPDIR', 'SDKROOT'):
        if key not in values:
            raise ValueError(f'Missing configured host environment: {key}')
        _directory(values[key])
    cargo_home = Path(values.get('CARGO_HOME', str(Path(values['HOME']) / '.cargo')))
    cargo_home = _directory(cargo_home)
    if cargo_home != (Path(values['HOME']) / '.cargo').resolve():
        raise ValueError('Use the ordinary host Cargo cache for this checkout profile')
    libc = Path(values.get('LIBGHOSTTY_VT_WINDOWS_LIBC',
                           str(Path(values['HOME']) / '.local/share/herdr/windows-cross/libc.txt')))
    if not libc.is_file():
        raise ValueError('Windows SDK libc configuration must already exist')
    sdk_roots = []
    libc_values = dict(line.split('=', 1) for line in libc.read_text().splitlines() if '=' in line)
    for key, marker in (('include_dir', 'stdlib.h'), ('sys_include_dir', 'vcruntime.h'),
                        ('crt_dir', 'ucrt.lib'), ('msvc_lib_dir', 'vcruntime.lib'),
                        ('kernel32_lib_dir', 'kernel32.lib')):
        path = Path(libc_values.get(key, ''))
        if not path.is_absolute() or not (path / marker).is_file():
            raise ValueError(f'Windows SDK configuration is incomplete: {key}')
        sdk_roots.append(str(path.resolve()))
    path_dirs = list(dict.fromkeys([str(toolchain / 'bin'),
        *(str(path.parent) for path in resolved.values()), '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
    values.update(PATH=os.pathsep.join(path_dirs), CARGO_HOME=str(cargo_home),
                  RUSTUP_HOME=str(toolchain.parent.parent), RUSTUP_TOOLCHAIN=toolchain.name,
                  RUSTUP_AUTO_INSTALL='0', RUSTC=str(resolved['rustc']),
                  CARGO_NET_OFFLINE='true', CARGO_BUILD_JOBS='4',
                  NEXTEST_TEST_THREADS='1', NEXTEST_USER_CONFIG_FILE='none',
                  PYTHONDONTWRITEBYTECODE='1', ZIG=str(resolved['zig']),
                  LIBGHOSTTY_VT_WINDOWS_LIBC=str(libc.resolve()),
                  GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.fsmonitor',
                  GIT_CONFIG_VALUE_0='false')
    probes = {name: [str(path), 'version' if name == 'zig' else '--version']
              for name, path in resolved.items()}
    writes = [{'path': str((source / name).resolve()), 'kind': 'generated'}
              for name in ('target', 'vendor/libghostty-vt/.zig-cache', 'vendor/libghostty-vt/zig-pkg')]
    writes += [{'path': str(cargo_home / name), 'kind': 'cache-file'} for name in _CARGO_CACHE_FILES]
    writes.append({'path': str((Path(values['HOME']) / '.cache/zig').resolve()), 'kind': 'cache'})
    read_roots = sorted(set([str(toolchain.parent.parent), str(cargo_home), str(libc.resolve()),
                             str(Path(values['SDKROOT']).resolve()), *sdk_roots,
                             *(str(path) for path in runtime_roots)]))
    if len(read_roots) > 24:
        raise ValueError('Herdr configuration exceeds the 24 runtime read-root limit')
    return {
        'format': 'herdr-native-environment-v1', 'source': str(source), 'rust_version': pin,
        'tools': {name: str(path) for name, path in resolved.items()}, 'env': values,
        'version_probes': probes,
        'read_roots': read_roots,
        'native_write_roots': writes,
        'required_directories': [row['path'] for row in writes if row['kind'] != 'cache-file'],
    }


def validate_herdr_versions(environment: dict, probe_results: Mapping[str, dict]) -> dict:
    """Validate readiness-owned probe observations; this function starts no process."""
    probes = environment['version_probes']
    if set(probe_results) != set(probes):
        raise ValueError('Every configured version probe needs one result')
    versions = {}
    for name, argv in probes.items():
        result = probe_results[name]
        output = str(result.get('stdout', '')).strip()
        if (result.get('argv') != argv or type(result.get('exit_code')) is not int
                or result['exit_code'] != 0 or not output):
            raise ValueError(f'Version probe did not succeed: {name}')
        versions[name] = output.splitlines()[0]
    pin = environment['rust_version']
    for name in ('rustc', 'cargo'):
        if re.match(rf'^{name} {re.escape(pin)}(?:\s|$)', versions[name]) is None:
            raise ValueError(f'{name} does not match project Rust {pin}')
    prefixes = {'rustfmt': r'rustfmt [0-9]', 'clippy-driver': r'clippy [0-9]',
                'just': r'just [0-9]', 'cargo-nextest': r'cargo-nextest [0-9]',
                'python3': r'Python 3\.', 'bun': r'[0-9]+\.[0-9]+\.[0-9]+',
                'node': r'v[0-9]+\.[0-9]+\.[0-9]+', 'rustup': r'rustup [0-9]'}
    for name, prefix in prefixes.items():
        if re.match(prefix, versions[name]) is None:
            raise ValueError(f'Unexpected tool version response: {name}')
    if versions['zig'] != '0.16.0':
        raise ValueError('The Herdr Windows SDK workflow requires Zig 0.16.0')
    return {'verified': True, 'rust_version': pin, 'versions': versions}


def build_herdr_recipe(source: Path, outputs: Path, *, environment: dict,
                       author: str, reviewer: str, source_scope: Sequence[str],
                       focused_tests: Sequence[str] = (),
                       selected_checks: Sequence[str] = (), impact_reason: str,
                       live_session_roots: Sequence[Path] = (),
                       native_local_tests: Mapping | None = None) -> dict:
    """Build an explicit, reviewed check list. No dependency inference or execution.

    The optional host local-test contract applies to the resulting recipe's
    native checks. It does not change model permissions or imply per-check
    network enforcement; a trusted direct caller can select separate recipes.
    """
    source, outputs = _directory(source), _directory(outputs)
    if source == outputs or source.is_relative_to(outputs) or outputs.is_relative_to(source):
        raise ValueError('Source and validation output directories must be disjoint')
    if environment.get('source') != str(source):
        raise ValueError('Environment was prepared for a different checkout')
    if not author.strip() or not reviewer.strip() or author.strip() == reviewer.strip():
        raise ValueError('An independent reviewer must confirm check selection')
    if not impact_reason.strip() or not source_scope:
        raise ValueError('Explicit source scope and impact reasoning are required')
    for name in source_scope:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not name or '.git' in path.parts:
            raise ValueError('Source scope must name checkout-relative paths')
        if not (source / path).resolve().is_relative_to(source):
            raise ValueError('Source scope escapes the checkout')
    if len(focused_tests) > 16 or len(set(focused_tests)) != len(focused_tests):
        raise ValueError('Choose at most 16 distinct focused tests')
    if any(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_:]*', name) is None for name in focused_tests):
        raise ValueError('Focused tests must be exact Rust test names')
    allowed = {'native-source', 'metrics-profile', 'render-scaling'}
    if not set(selected_checks).issubset(allowed) or len(set(selected_checks)) != len(selected_checks):
        raise ValueError('Unknown or duplicate Herdr check selection')
    local_tests = None
    if native_local_tests is not None:
        if not isinstance(native_local_tests, Mapping) or set(native_local_tests) != {'ephemeral_tcp', 'denied_tcp_ports'}:
            raise ValueError('Native local tests require an explicit host TCP range and protected ports')
        bounds, denied_ports = native_local_tests['ephemeral_tcp'], native_local_tests['denied_tcp_ports']
        if (not isinstance(bounds, Mapping) or set(bounds) != {'first', 'last'}
                or any(type(bounds[key]) is not int for key in bounds)
                or not 1024 <= bounds['first'] <= bounds['last'] <= 65535
                or bounds['last'] - bounds['first'] + 1 > 16384
                or not isinstance(denied_ports, list) or len(denied_ports) > 128
                or any(type(port) is not int or not 1 <= port <= 65535 for port in denied_ports)
                or denied_ports != sorted(set(denied_ports))):
            raise ValueError('Native local tests require bounded, canonical host configuration')
        local_tests = {'ephemeral_tcp': dict(bounds), 'denied_tcp_ports': list(denied_ports)}
    cargo, just = environment['tools']['cargo'], environment['tools']['just']
    checks = []
    def rust_check(name, test, *, release=False, ignored=False, timeout=300):
        argv = [cargo, 'test', '--locked']
        if release:
            argv.append('--release')
        argv += ['--bin', 'herdr', test, '--', '--exact', '--nocapture', '--test-threads=1']
        if ignored:
            argv.append('--ignored')
        return {'name': name, 'argv': argv, 'timeout_seconds': timeout,
                'scope': f'Run only the exact candidate Rust test {test}; include its compilation and owned process cleanup.',
                'adapter_id': 'cargo-test-v1', 'result_kind': 'cargo-test'}
    for index, test in enumerate(focused_tests, 1):
        checks.append(rust_check(f'focused-{index}', test))
    if 'native-source' in selected_checks:
        checks.append(rust_check('native-source', _NATIVE_SOURCE, release=True, ignored=True))
    if 'metrics-profile' in selected_checks:
        checks.append(rust_check('metrics-profile', _METRICS_PROFILE,
                                 release=True, ignored=True, timeout=900))
    if 'render-scaling' in selected_checks:
        checks.append({'name': 'render-scaling', 'argv': [just, 'bench-render-scale'],
                       'timeout_seconds': 600, 'adapter_id': 'cargo-test-v1',
                       'result_kind': 'cargo-test',
                       'scope': 'Run the candidate full-render scaling benchmarks across the project pane-count cases.'})
    checks.append({'name': 'full-native-check', 'argv': [just, 'check'],
                   'timeout_seconds': 1800, 'adapter_id': 'native-tests-v1',
                   'result_kind': 'native-tests',
                   'scope': 'Validate candidate formatting, all-target Clippy, complete Nextest, maintenance, UI architecture, integration assets, Windows lint and documentation contracts.',
                   'suite_counts': {'nextest': 1, 'unittest': 2, 'bun': 5}})
    for check in checks:
        check_adapters.select(check)
    values = dict(environment['env'])
    values.update({f'XDG_{kind}_HOME': str(outputs / kind.lower())
                   for kind in ('CONFIG', 'STATE', 'DATA')})
    values['XDG_RUNTIME_DIR'] = str(outputs / 'run')
    home = Path(environment['env']['HOME'])
    protected = list(dict.fromkeys(str(Path(path).resolve()) for path in
        [home / '.config/herdr', home / '.config/herdr-dev', *live_session_roots]))
    if len(protected) > 16:
        raise ValueError('At most 16 known Herdr session roots may be protected')
    if any(not Path(path).is_absolute() for path in live_session_roots):
        raise ValueError('Known live session roots must be absolute paths')
    if any(source.is_relative_to(Path(path)) or outputs.is_relative_to(Path(path))
           or Path(path).is_relative_to(source) or Path(path).is_relative_to(outputs)
           for path in protected):
        raise ValueError('Checkout or outputs overlap a known live Herdr session')
    result = {'format': 'herdr-validation-v1', 'source': str(source), 'outputs': str(outputs),
            'env': values, 'checks': checks, 'read_roots': list(environment['read_roots']),
            'protected_roots': protected,
            'native_write_roots': [dict(row) for row in environment['native_write_roots']],
            'native_environment': {key: value for key, value in values.items()
                                   if key in _NATIVE_ENV_KEYS or key.startswith('XDG_')},
            'required_directories': [*environment['required_directories'],
                                     *(str(outputs / name) for name in ('config', 'state', 'data', 'run'))],
            'selection': {'author': author.strip(), 'reviewer': reviewer.strip(),
                          'source_scope': list(source_scope), 'reason': impact_reason.strip(),
                          'focused_tests': list(focused_tests), 'selected_checks': list(selected_checks)},
            'automated_ipc_is_human_acceptance': False}
    if local_tests is not None:
        result['native_local_tests'] = local_tests
    return result


def _drain_fd(fd, stream, destination, stop, pty_eof=False):
    try:
        while not stop.is_set():
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                block = os.read(fd, 65536)
            except OSError as error:
                if pty_eof and error.errno == errno.EIO:
                    stream['eof'] = True
                    break
                raise
            if not block:
                stream['eof'] = True
                break
            stream['bytes'] += len(block)
            retained = block[:max(0, _MAX_LOG - stream['retained_bytes'])]
            destination.write(retained)
            destination.flush()
            stream['retained_bytes'] += len(retained)
    except BaseException as error:
        stream['error'] = repr(error)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Absolute startup deadline expired')
    return min(0.5, remaining)


def _ping(deadline):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_remaining(deadline))
        client.connect('./h.sock')
        client.settimeout(_remaining(deadline))
        client.sendall(b'{"id":"aos-ipc-readiness","method":"ping","params":{}}\n')
        response = bytearray()
        while b'\n' not in response:
            client.settimeout(_remaining(deadline))
            block = client.recv(4096)
            if not block:
                raise RuntimeError('API closed before complete ping response')
            response.extend(block)
            if len(response) > 65536:
                raise RuntimeError('Ping response exceeded bounded size')
        value = json.loads(response.split(b'\n', 1)[0])
        result = value.get('result', {})
        if (value.get('id') != 'aos-ipc-readiness' or result.get('type') != 'pong'
                or not isinstance(result.get('version'), str) or type(result.get('protocol')) is not int):
            raise RuntimeError('Unexpected ping response')
        return value


def _stop_command(binary, runtime, environment, seconds):
    """Reap the stop child explicitly; a timeout can never become a normal stop."""
    result = {'pid': None, 'exit_code': None, 'reaped': False, 'timed_out': False,
              'interrupted': False, 'errors': []}
    streams, readers = [], []
    stop = threading.Event()
    process = None
    try:
        process = subprocess.Popen([str(binary), 'server', 'stop'], cwd=runtime, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result['pid'] = process.pid
        for name, pipe in (('stdout', process.stdout), ('stderr', process.stderr)):
            stream = {'eof': False, 'bytes': 0, 'retained_bytes': 0, 'error': None}
            destination = (runtime / f'stop.{name}').open('xb')
            streams.append((stream, destination, pipe))
            reader = threading.Thread(target=_drain_fd,
                args=(pipe.fileno(), stream, destination, stop), name=f'herdr-stop-{name}', daemon=True)
            readers.append(reader)
            reader.start()
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
            if process.poll() is None:
                result['interrupted'] = True
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                result['errors'].append('Owned stop command remains unreaped after SIGINT')
    except BaseException as error:
        result['errors'].append(repr(error))
    finally:
        # This also handles failure while constructing a reader after Popen.
        if process is not None and process.poll() is None and not result['interrupted']:
            result['interrupted'] = True
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
            except BaseException as error:
                result['errors'].append('stop-child cleanup: ' + repr(error))
        if process is not None:
            result['exit_code'] = process.poll()
            result['reaped'] = process.returncode is not None
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=1)
        stop.set()
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=1)
        result['streams'] = [item[0] for item in streams]
        result['readers_joined'] = all(not reader.is_alive() for reader in readers)
        for _stream, destination, pipe in streams:
            try:
                pipe.close()
            except BaseException as error:
                result['errors'].append('stop pipe close: ' + repr(error))
            if result['readers_joined']:
                try:
                    destination.close()
                except BaseException as error:
                    result['errors'].append('stop log close: ' + repr(error))
        # A stream not yet attached to a reader still belongs to this child.
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                try:
                    if pipe is not None and not pipe.closed:
                        pipe.close()
                except BaseException as error:
                    result['errors'].append('unattached stop pipe close: ' + repr(error))
    result['normal'] = bool(result['reaped'] and result['exit_code'] == 0
        and not result['timed_out'] and not result['interrupted'] and not result['errors']
        and len(streams) == 2 and result['readers_joined']
        and all(s['eof'] and s['error'] is None for s, _destination, _pipe in streams))
    return result


def run_owned_herdr_session(binary: Path, output: Path, *, environment: Mapping[str, str],
                            startup_seconds: int = 20, stop_seconds: int = 20) -> dict:
    """Run only in a dedicated native-check process, never the threaded coordinator.

    Relative sockets avoid macOS path-length limits. The caller must provide the
    native boundary and independently observe descendants. This helper proves
    direct-child reaping, stream closure and its exact socket cleanup only.
    """
    if threading.active_count() != 1:
        raise ValueError('Owned Herdr session requires a dedicated single-threaded check process')
    if any(type(n) is not int or not 1 <= n <= 30 for n in (startup_seconds, stop_seconds)):
        raise ValueError('Startup and stop bounds must each be 1–30 seconds')
    binary, output = _executable(binary), _directory(output)
    runtime = output / 'herdr-ipc'
    runtime.mkdir(mode=0o700, exist_ok=False)
    for name in ('config', 'state', 'data', 'run'):
        (runtime / name).mkdir(mode=0o700)
    config = runtime / 'config.toml'
    config.write_text('onboarding = false\n[ui.sound]\nenabled = false\n')
    values = {key: environment[key] for key in _SESSION_ENV if key in environment}
    values.update(HERDR_SOCKET_PATH='./h.sock', HERDR_CONFIG_PATH=str(config),
                  XDG_CONFIG_HOME=str(runtime / 'config'), XDG_STATE_HOME=str(runtime / 'state'),
                  XDG_DATA_HOME=str(runtime / 'data'), XDG_RUNTIME_DIR=str(runtime / 'run'),
                  TERM='xterm-256color', SHELL='/bin/sh', HERDR_DISABLE_SOUND='1',
                  GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.fsmonitor', GIT_CONFIG_VALUE_0='false')
    report = {'ipc_passed': False, 'human_ui_acceptance': False, 'binary': str(binary),
              'runtime': str(runtime), 'errors': [], 'server_reaped': False,
              'cleanup_scope': 'direct server and stop children, PTY EOF and exact sockets; descendants require caller proof'}
    previous = os.open('.', os.O_RDONLY)
    pid = master = status = None
    reader = log = None
    stop = threading.Event()
    stream = {'eof': False, 'bytes': 0, 'retained_bytes': 0, 'error': None}
    started = time.monotonic()
    def reaped():
        nonlocal status
        if pid is not None and status is None:
            found, value = os.waitpid(pid, os.WNOHANG)
            if found == pid:
                status = value
        return status is not None
    def wait_server(seconds):
        deadline = time.monotonic() + seconds
        while not reaped() and time.monotonic() < deadline:
            time.sleep(0.05)
        return reaped()
    try:
        os.chdir(runtime)
        log = (runtime / 'server.pty').open('xb')
        import pty
        pid, master = pty.fork()
        if pid == 0:
            try:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
                os.execve(binary, [str(binary), 'server'], values)
            except BaseException:
                os._exit(127)
        report['server_pid'] = pid
        (runtime / 'ownership.json').write_text(json.dumps(
            {'server_pid': pid, 'controller_pid': os.getpid(), 'binary': str(binary),
             'cwd': str(runtime), 'socket_paths': ['./h.sock', './h-client.sock']}) + '\n')
        reader = threading.Thread(target=_drain_fd, args=(master, stream, log, stop, True),
                                  name='owned-herdr-pty', daemon=True)
        reader.start()
        deadline = started + startup_seconds
        last_error = None
        while time.monotonic() < deadline:
            if reaped():
                raise RuntimeError('Owned server exited before readiness')
            try:
                for name in ('h.sock', 'h-client.sock'):
                    info = Path(name).lstat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                        raise RuntimeError('Unexpected owned socket identity')
                report['ping'] = _ping(deadline)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(_remaining(deadline))
                    client.connect('./h-client.sock')
                report['client_socket_connected'] = True
                break
            except (OSError, RuntimeError, ValueError) as error:
                last_error = repr(error)
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        else:
            raise RuntimeError('Startup deadline: ' + str(last_error))
    except BaseException as error:
        report['errors'].append(repr(error))
    finally:
        try:
            if pid is not None and not reaped():
                try:
                    report['normal_stop'] = _stop_command(binary, runtime, values, stop_seconds)
                except BaseException as error:
                    report['errors'].append('normal stop cleanup: ' + repr(error))
                if not wait_server(5):
                    report['fallback_sigint'] = True
                    try:
                        os.kill(pid, signal.SIGINT)
                        wait_server(5)
                    except BaseException as error:
                        report['errors'].append('owned interruption: ' + repr(error))
            report['server_reaped'] = reaped()
            report['server_exit'] = os.waitstatus_to_exitcode(status) if status is not None else None
        except BaseException as error:
            report['errors'].append('server cleanup: ' + repr(error))
        finally:
            try:
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=2)
                stop.set()
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=1)
            except BaseException as error:
                report['errors'].append('PTY reader cleanup: ' + repr(error))
            finally:
                stop.set()
                try:
                    if master is not None:
                        try:
                            os.close(master)
                        except BaseException as error:
                            report['errors'].append('PTY close: ' + repr(error))
                    if log is not None and (reader is None or not reader.is_alive()):
                        try:
                            log.close()
                        except BaseException as error:
                            report['errors'].append('PTY log close: ' + repr(error))
                finally:
                    try:
                        os.fchdir(previous)
                    except BaseException as error:
                        report['errors'].append('cwd restoration: ' + repr(error))
                    finally:
                        try:
                            os.close(previous)
                        except BaseException as error:
                            report['errors'].append('cwd descriptor close: ' + repr(error))
        report['pty'] = {**stream, 'reader_joined': reader is None or not reader.is_alive()}
        report['sockets_absent'] = {name: not os.path.lexists(runtime / name)
                                   for name in ('h.sock', 'h-client.sock')}
        report['ipc_passed'] = bool(report.get('ping') and report.get('client_socket_connected')
            and not report['errors'] and report.get('normal_stop', {}).get('normal')
            and report['server_reaped'] and report.get('server_exit') == 0
            and not report.get('fallback_sigint') and stream['eof'] and stream['error'] is None
            and report['pty']['reader_joined'] and all(report['sockets_absent'].values()))
        report['elapsed_seconds'] = time.monotonic() - started
        try:
            (runtime / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        except BaseException as error:
            report['errors'].append('result receipt: ' + repr(error))
            report['ipc_passed'] = False
    return report
