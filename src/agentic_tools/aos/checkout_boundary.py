"""Small, explicit isolated-checkout permissions; legacy recipes are untouched.

This module grants no execution or recovery authority. The caller retains the
ordinary assignment deadline, owned-process cleanup and before/after evidence.
Endpoint limits are validated here, not represented as continuous monitoring.
"""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time

from . import native_network, native_paths, native_sandbox
from .store import Rejected

POLICY = 'isolated-checkout-v1'
_LIMITS = {'minimum_free_bytes', 'max_owned_growth_bytes', 'max_output_bytes'}
_SETTINGS = {'change_roots', 'protected_roots', 'native_environment', 'endpoint_limits',
             'native_local_tests', 'native_validation'}
_LEGACY = {'resource_limits', 'resource_limits_carry_forward', 'resource_observation_continuation',
           'model_write_scope', 'native_sandbox'}
_CARGO_FILES = {'.package-cache', '.package-cache-mutate', '.global-cache',
                '.global-cache-journal', '.global-cache-wal', '.global-cache-shm'}
_ENVIRONMENT = {'PATH', 'ZIG', 'SDKROOT', 'CC', 'CXX', 'AR', 'RUSTC', 'RUSTDOC',
                'RUSTUP_TOOLCHAIN', 'RUSTUP_HOME', 'RUSTUP_AUTO_INSTALL', 'CARGO_NET_OFFLINE', 'CARGO_BUILD_JOBS',
                'NEXTEST_TEST_THREADS', 'NEXTEST_USER_CONFIG_FILE', 'LIBGHOSTTY_VT_WINDOWS_LIBC',
                'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'}
_EXECUTABLE_ENV = {'ZIG', 'CC', 'CXX', 'AR', 'RUSTC', 'RUSTDOC'}
_READ_DIRECTORY_ENV = {'SDKROOT', 'RUSTUP_HOME'}
_READ_FILE_ENV = {'NEXTEST_USER_CONFIG_FILE', 'LIBGHOSTTY_VT_WINDOWS_LIBC'}
_OWNED_DIRECTORY_ENV = {'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'}
_CONTROL_NAMES = {'.git', '.codex', 'AGENTS.md', 'AGENTS.override.md'}
# OpenSSL initialization used by native tools reads this standard system file.
# This exact read was exercised by G0; models receive no implicit grant for it.
_NATIVE_SYSTEM_READ_FILES = {'/private/etc/ssl/openssl.cnf'}
_TEMPORARY_FORMAT = 'checkout-native-temporary-v1'
_TEMPORARY_SCOPE = 'Observed existing temporary names are excluded; not a private namespace.'
_MAX_TEMPORARY_CHILDREN = 4096


def _reject(message: str):
    raise Rejected('checkout_scope', message)


def enabled(recipe: dict) -> bool:
    if 'execution_policy' not in recipe:
        return False
    if recipe['execution_policy'] != POLICY:
        _reject('Unsupported explicit execution policy')
    return True


def reviewed_host(recipe: dict) -> bool:
    """Select only the explicit reviewed host tier; model permissions stay unchanged."""
    policy = recipe.get('checkout_policy')
    configured = isinstance(policy, dict) and 'native_validation' in policy
    if not enabled(recipe):
        if configured:
            _reject('Reviewed host validation requires isolated-checkout-v1')
        return False
    if not configured:
        return False
    if type(policy['native_validation']) is not str or policy['native_validation'] != 'reviewed-host-v1':
        _reject('Unsupported native validation tier')
    if 'native_local_tests' in policy:
        _reject('Reviewed host validation cannot mix native_local_tests permissions')
    return True


def _relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        _reject('Change scopes must be ordinary relative paths')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        _reject('Change scopes must be canonical relative paths')
    return path


def _control(path: PurePosixPath) -> bool:
    return any(part in _CONTROL_NAMES or part.startswith('AGENTS.') for part in path.parts)


def _config(recipe: dict) -> dict:
    if not enabled(recipe):
        _reject('The checkout boundary requires an explicit execution policy')
    value = recipe.get('checkout_policy')
    if not isinstance(value, dict) or set(value) - _SETTINGS:
        _reject('Unsupported isolated-checkout settings')
    reviewed_host(recipe)
    scopes = value.get('change_roots')
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 32:
        _reject('One to 32 explicit change roots are required')
    if any(_control(_relative(name)) for name in scopes) or len(set(scopes)) != len(scopes):
        _reject('Change roots cannot duplicate or name repository controls')
    limits = value.get('endpoint_limits')
    if (not isinstance(limits, dict) or set(limits) != _LIMITS
            or any(type(n) is not int or not 0 < n <= 2**63 - 1 for n in limits.values())):
        _reject('Endpoint limits require three positive bounded byte counts')
    extra = value.get('protected_roots', [])
    if not isinstance(extra, list) or len(extra) > 16 or not all(isinstance(p, str) for p in extra):
        _reject('Protected roots require at most 16 exact paths')
    if len(set(extra)) != len(extra):
        _reject('Duplicate protected roots')
    env = value.get('native_environment', {})
    if (not isinstance(env, dict) or set(env) - _ENVIRONMENT
            or any(not isinstance(v, str) or not v or len(v) > 4096 or '\x00' in v or '\n' in v
                   for v in env.values())):
        _reject('Native environment contains an unsupported key or value')
    if env.get('RUSTUP_AUTO_INSTALL', '0') != '0' or env.get('CARGO_NET_OFFLINE', 'true') != 'true':
        _reject('Native tool installation and dependency fetching are disabled')
    for key in ('CARGO_BUILD_JOBS', 'NEXTEST_TEST_THREADS'):
        jobs = env.get(key, '1')
        if not jobs.isascii() or not jobs.isdigit() or not 1 <= int(jobs) <= 64:
            _reject('Native build/test parallelism must be bounded from 1 to 64')
    if 'RUSTUP_TOOLCHAIN' in env and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', env['RUSTUP_TOOLCHAIN']):
        _reject('Toolchain selection must name an existing toolchain')
    if 'native_local_tests' in value:
        native_network.contract(recipe)
    return value


def _instruction_paths(recipe: dict, *, kinds=('chain', 'support', 'configs')) -> set[Path]:
    """Protect declared paths; the caller separately verifies manifest content."""
    result = set()
    manifest = recipe.get('instruction_manifest') or {}
    if not isinstance(manifest, dict):
        _reject('Invalid instruction manifest')
    for kind in kinds:
        for entry in manifest.get(kind, []):
            if not isinstance(entry, dict):
                _reject('Invalid instruction path record')
            for key in ('path', 'resolved'):
                if key in entry:
                    path = Path(entry[key])
                    if not path.is_absolute():
                        _reject('Instruction paths must be absolute')
                    result.update((path, path.resolve()))
    return result


def _denied(recipe: dict, state_dir: Path | None = None) -> set[Path]:
    home = Path.home().resolve()
    names = ('.codex', '.ssh', '.aws', '.gnupg', '.azure', '.kube', '.docker', '.netrc',
             '.npmrc', '.pypirc', '.git-credentials', '.config/gcloud', '.config/gh',
             'Library/Keychains', '.cargo/credentials', '.cargo/credentials.toml')
    paths = {home / name for name in names}
    paths.update(Path('/private/tmp') / name for name in
                 ('codex-app-tools', 'codex-browser-use', 'com.openai.sky.CUAService'))
    if state_dir is not None:
        paths.add(Path(state_dir))
    paths.update(native_paths._canonical(p) for p in _config(recipe).get('protected_roots', []))
    return {p.resolve() for p in paths}


def _trusted(recipe: dict) -> set[Path]:
    paths = {Path(__file__).resolve().parent.parent, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    paths.update(Path(p) for p in _NATIVE_SYSTEM_READ_FILES)
    paths.update(native_paths._canonical(p) for p in recipe.get('native_protected_paths', []))
    if recipe.get('probe_interpreter'):
        interpreter = Path(recipe['probe_interpreter']).resolve()
        paths.add(interpreter.parent.parent if interpreter.parent.name in ('bin', 'Scripts') else interpreter.parent)
    return paths


def _temporary_roots() -> tuple[Path, ...]:
    # Preserve the host's ordinary native temporary location, including its
    # lexical TMPDIR spelling in environment(). IPC needs the explicit local-test
    # mode and a retained observation excluding every pre-existing child.
    text = os.environ.get('TMPDIR') or tempfile.gettempdir()
    temporary = Path(text).resolve()
    if not temporary.is_dir() or temporary == Path('/') or temporary == Path.home().resolve():
        _reject('The normal native temporary directory is unavailable or too broad')
    return tuple(dict.fromkeys((temporary, Path('/private/tmp'))))


def _path_aliases(path: Path) -> set[Path]:
    """Retain names under the ordinary macOS lexical temporary aliases.

    Do not resolve child symlinks into unrelated global exclusions. Their old
    names remain closed; targets retain their existing filesystem protection.
    """
    result = {path}
    for canonical, lexical in ((Path('/private/tmp'), Path('/tmp')),
                               (Path('/private/var'), Path('/var'))):
        if lexical.resolve() != canonical:
            continue
        for origin, alias in ((canonical, lexical), (lexical, canonical)):
            if path.is_relative_to(origin):
                result.add(alias / path.relative_to(origin))
    return result


def _temporary_aliases(root: Path) -> list[str]:
    aliases = _path_aliases(root)
    lexical = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
    if lexical.is_absolute() and '..' not in lexical.parts and lexical.resolve() == root:
        aliases.add(lexical)
    return sorted(map(str, aliases))


def _temporary_clock(deadline: float | None) -> float:
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise Rejected('checkout_temporary', 'A finite monotonic temporary observation deadline is required')
    return min(time.monotonic() + 10, deadline if deadline is not None else math.inf)


def _temporary_check(end: float) -> None:
    if time.monotonic() >= end:
        raise Rejected('checkout_temporary', 'Temporary endpoint observation exceeded its bounded deadline')


def observe_temporary(recipe: dict, *, deadline: float | None = None) -> dict | None:
    """Bounded direct-child census, never recursive or continuous.

    Every entry kind is retained using lstat. The deadline is cooperative; it
    cannot interrupt an individual filesystem call inside the kernel.
    """
    if not _config(recipe).get('native_local_tests'):
        return None
    end, roots, count = _temporary_clock(deadline), [], 0
    try:
        for root in _temporary_roots():
            _temporary_check(end)
            identity = native_paths.identity(root)
            entries = []
            with os.scandir(root) as children:
                for child in children:
                    _temporary_check(end)
                    count += 1
                    if count > _MAX_TEMPORARY_CHILDREN:
                        raise Rejected('checkout_temporary', 'Temporary endpoint census exceeds its bounded entry limit')
                    info = child.stat(follow_symlinks=False)
                    entries.append({'name': child.name, 'device': info.st_dev, 'inode': info.st_ino,
                                    'uid': info.st_uid, 'mode': info.st_mode})
            _temporary_check(end)
            if native_paths.identity(root) != identity:
                raise Rejected('checkout_temporary', 'Temporary root identity changed during observation')
            roots.append({'path': str(root), 'identity': identity, 'aliases': _temporary_aliases(root),
                          'entries': sorted(entries, key=lambda entry: entry['name'])})
    except OSError as error:
        raise Rejected('checkout_temporary', 'A complete temporary endpoint census is required') from error
    _temporary_check(end)
    return {'format': _TEMPORARY_FORMAT, 'scope': _TEMPORARY_SCOPE, 'roots': roots}


def _temporary_rows(recipe: dict, previous: dict | None) -> list[dict]:
    if not _config(recipe).get('native_local_tests'):
        if previous is not None:
            raise Rejected('checkout_temporary', 'Unexpected temporary IPC authority in a disabled recipe')
        return []
    if (not isinstance(previous, dict) or set(previous) != {'format', 'scope', 'roots'}
            or previous['format'] != _TEMPORARY_FORMAT or previous['scope'] != _TEMPORARY_SCOPE
            or not isinstance(previous['roots'], list)):
        raise Rejected('checkout_temporary', 'An exact retained temporary endpoint observation is required')
    expected = list(map(str, _temporary_roots()))
    rows, count = previous['roots'], 0
    if len(rows) != len(expected):
        raise Rejected('checkout_temporary', 'Temporary observation roots differ from the normal host roots')
    for row, path in zip(rows, expected):
        if (not isinstance(row, dict) or set(row) != {'path', 'identity', 'aliases', 'entries'}
                or row['path'] != path or row['aliases'] != _temporary_aliases(Path(path))
                or not isinstance(row['identity'], dict) or set(row['identity']) != {'device', 'inode', 'uid'}
                or any(type(n) is not int or n < 0 for n in row['identity'].values())
                or not isinstance(row['entries'], list)):
            raise Rejected('checkout_temporary', 'Retained temporary root or alias identity is invalid')
        names = []
        for entry in row['entries']:
            count += 1
            if (count > _MAX_TEMPORARY_CHILDREN or not isinstance(entry, dict)
                    or set(entry) != {'name', 'device', 'inode', 'uid', 'mode'}
                    or not isinstance(entry['name'], str) or not entry['name']
                    or entry['name'] in ('.', '..') or '/' in entry['name'] or '\x00' in entry['name']
                    or any(type(entry[key]) is not int or entry[key] < 0 for key in ('device', 'inode', 'uid', 'mode'))):
                raise Rejected('checkout_temporary', 'Retained temporary child observation is invalid')
            names.append(entry['name'])
        if names != sorted(set(names)):
            raise Rejected('checkout_temporary', 'Retained temporary children must be exact unique names')
    return rows


def verify_temporary_observation(recipe: dict, previous: dict | None, *, deadline: float | None = None) -> None:
    """Before GO, reject new names; old exclusions remain sticky after removal."""
    rows = _temporary_rows(recipe, previous)
    if not rows:
        return
    current = observe_temporary(recipe, deadline=deadline)
    for old, new in zip(rows, current['roots']):
        if old['identity'] != new['identity'] or old['aliases'] != new['aliases']:
            raise Rejected('checkout_temporary', 'Temporary root identity or aliases changed before launch')
        if {entry['name'] for entry in new['entries']} - {entry['name'] for entry in old['entries']}:
            raise Rejected('checkout_temporary', 'A new temporary child is not excluded; prepare a fresh boundary')


def _below(path: Path, parent: Path, cache: dict) -> bool:
    return native_sandbox._relative_parts(path, parent, cache) is not None


def validate(recipe: dict, state_dir: Path) -> None:
    """Cheap recipe/path validation; source hardlink scanning is separate."""
    if not enabled(recipe):
        return
    value = _config(recipe)
    if any(key in recipe for key in _LEGACY) or recipe.get('mutable') or recipe.get('immutable'):
        _reject('Isolated checkout cannot mix exact-file or continuous-monitoring contracts')
    source = native_paths._canonical(recipe['source'], directory=True)
    outputs = native_paths._canonical(recipe['outputs'], directory=True)
    state = native_paths._canonical(str(state_dir), directory=True)
    reads = recipe.get('read_roots', [])
    if not isinstance(reads, list) or len(reads) > 24 or not all(isinstance(p, str) for p in reads):
        _reject('At most 24 explicit read roots are supported')
    if len(set(reads)) != len(reads):
        _reject('Duplicate read roots')
    roots = [native_paths._canonical(p) for p in reads]
    if any(not path.exists() for path in roots):
        _reject('Read roots must already exist')
    denied = _denied(recipe, state)
    cache = {}
    if native_paths.overlaps(source, outputs, _cache=cache):
        _reject('Checkout and outputs must be disjoint')
    home = Path.home().resolve()
    if any(path == Path('/') or path == home for path in [source, outputs, *roots]):
        _reject('Root and whole-home grants are unsupported')
    cargo = home / '.cargo'
    cargo_credentials = {cargo / 'credentials', cargo / 'credentials.toml'}
    for path in [source, outputs, *roots]:
        for guard in denied:
            if (native_paths.overlaps(path, guard, _cache=cache)
                    and not (path in roots and path == cargo and guard in cargo_credentials)):
                _reject('Checkout, outputs or read roots overlap authoritative state or credentials')
    instructions = _instruction_paths(recipe)
    trusted = _trusted(recipe)
    if any(native_paths.overlaps(path, guard, _cache=cache) for path in (source, outputs) for guard in trusted):
        _reject('Checkout or outputs overlap trusted runtime code')
    if any(native_paths.overlaps(outputs, path, _cache=cache) for path in instructions):
        _reject('Outputs overlap protected instruction files')
    declarations = recipe.get('native_write_roots', [])
    if not isinstance(declarations, list) or len(declarations) > 16:
        _reject('At most 16 native generated/cache declarations are supported')
    writes = native_paths.validate(recipe, protected=(*denied, *trusted, *instructions))
    cargo = home / '.cargo'
    for entry in writes:
        path = Path(entry['path'])
        cargo_leaf = entry['kind'] == 'cache-file' and path.parent == cargo and path.name in _CARGO_FILES
        if native_paths.overlaps(path, cargo, _cache=cache) and not cargo_leaf:
            _reject('Cargo writes are limited to the six fixed lock/database files')
        if entry['kind'] != 'generated' and any(
                native_paths.overlaps(path, read, _cache=cache) and not (cargo_leaf and read == cargo)
                for read in [*roots, *(Path(p) for p in native_sandbox._MINIMAL)]):
            _reject('Native cache writes cannot overlap trusted read/tool roots')
    available = [*roots, *(Path(p) for p in native_sandbox._MINIMAL)]
    generated = [Path(entry['path']) for entry in writes if entry['kind'] == 'generated']
    for temporary in _temporary_roots():
        if (_below(temporary, source, cache) and not any(_below(temporary, root, cache) for root in generated)):
            _reject('Native temporary writes cannot open undeclared checkout paths')
        if any(_below(temporary, root, cache) for root in [*trusted, *denied, *available]):
            _reject('Native temporary writes cannot open trusted or protected roots')
    mutable = [source, outputs, *(Path(entry['path']) for entry in writes)]
    env = value.get('native_environment', {})
    candidates = [(p, True, False) for p in env.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin').split(':')]
    for key, name in env.items():
        if key == 'NEXTEST_USER_CONFIG_FILE' and name == 'none':
            continue
        if key in _EXECUTABLE_ENV | _READ_DIRECTORY_ENV | _READ_FILE_ENV:
            candidates.append((name, key in _READ_DIRECTORY_ENV, key in _EXECUTABLE_ENV))
    for name, directory, executable in candidates:
        path = Path(name)
        if not path.is_absolute() or not path.exists() or (directory and not path.is_dir()) or (
                not directory and (not path.is_file() or executable and not os.access(path, os.X_OK))):
            _reject('Native environment tools/configs must already exist at absolute paths')
        path = path.resolve()
        if (not any(_below(path, root, cache) for root in available)
                or any(native_paths.overlaps(path, root, _cache=cache) for root in mutable)
                or any(native_paths.overlaps(path, guard, _cache=cache) for guard in denied)):
            _reject('Native environment tools require a read-only trusted root')
    for key in _OWNED_DIRECTORY_ENV & env.keys():
        path = native_paths._canonical(env[key], directory=True)
        if path == outputs or not _below(path, outputs, cache):
            _reject('Native runtime directories must be prepared strict output children')
        if any(native_paths.overlaps(path, guard, _cache=cache) for guard in denied | instructions | trusted):
            _reject('Native runtime directory overlaps a protected namespace')



def scoped_outputs(recipe: dict, output: Path, *, prepare: bool = False) -> dict:
    """Rebind only owned XDG directories for a trusted technical output scope.

    Callers add every authoritative state/private-control root to protected_roots
    before this call. Runtime callers derive an attempt child; readiness may use
    a separately prepared private root. Retain the original recipe for identity
    and authority checks. Rendering profiles never creates these directories.
    """
    if not enabled(recipe):
        return {**recipe, 'outputs': str(output)}
    policy = copy.deepcopy(_config(recipe))
    original = native_paths._canonical(recipe['outputs'], directory=True)
    destination = native_paths._canonical(str(output), directory=True)
    source = native_paths._canonical(recipe['source'], directory=True)
    guards = (_denied(recipe) | _trusted(recipe) | _instruction_paths(recipe)
              | {source, *(native_paths._canonical(p) for p in recipe.get('read_roots', []))}
              | {Path(p) for p in native_sandbox._MINIMAL}
              | {Path(entry['path']) for entry in native_paths.validate(recipe)})
    cache = {}
    if any(native_paths.overlaps(destination, guard, _cache=cache) for guard in guards):
        _reject('Scoped output overlaps source, protected state or trusted read roots')
    env = policy.get('native_environment', {})
    planned = []
    for key in sorted(_OWNED_DIRECTORY_ENV & env.keys()):
        previous = native_paths._canonical(env[key])
        if previous == original or not previous.is_relative_to(original):
            _reject('Original native runtime directories must be strict owned output children')
        suffix = previous.relative_to(original)
        target = native_paths._canonical(str(destination / suffix))
        for path, base in ((previous, original), (target, destination)):
            for parent in (path, *path.parents):
                if parent == base:
                    break
                if parent.exists() and not parent.is_dir():
                    _reject('Owned runtime directory path contains a non-directory')
        if any(native_paths.overlaps(target, guard, _cache=cache) for guard in guards):
            _reject('Remapped runtime directory overlaps a protected namespace')
        env[key] = str(target)
        planned.append(suffix)
    result = {**recipe, 'outputs': str(destination), 'checkout_policy': policy}
    if prepare:
        # Validate the complete plan first. Walk only the existing owned root
        # with no-follow directory handles; never follow a substituted symlink.
        identity = native_paths.identity(destination)
        root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != (identity['device'], identity['inode']):
                _reject('Scoped output identity changed before directory preparation')
            for suffix in planned:
                directory = os.dup(root_fd)
                try:
                    for name in suffix.parts:
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=directory)
                        except FileExistsError:
                            pass
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                        os.close(directory)
                        directory = child
                finally:
                    os.close(directory)
            if native_paths.identity(destination) != identity:
                _reject('Scoped output identity changed during directory preparation')
            for suffix in planned:
                native_paths._canonical(str(destination / suffix), directory=True)
        finally:
            os.close(root_fd)
    return result



def validate_source_link(recipe: dict, path: Path) -> str:
    """Return exact text of a direct relative link to an owned regular file.

    Do not follow directory links or read the target's contents. Source inventory
    hashes this text; the ordinary target is observed independently. Outputs and
    legacy recipes never acquire link support through this helper.
    """
    if not enabled(recipe):
        _reject('Source links require the explicit isolated-checkout policy')
    source = native_paths._canonical(recipe['source'], directory=True)
    path = Path(path)
    if not path.is_absolute() or path == source or not path.is_relative_to(source):
        _reject('Source link must be a strict checkout child')
    try:
        native_paths._canonical(str(path.parent), directory=True)
        if path.name not in os.listdir(path.parent):
            _reject('Source link must use canonical directory-entry spelling')
        before = path.lstat()
        if not stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
            _reject('Source link must be one ordinary symbolic-link entry')
        text = os.readlink(path)
        target = Path(text)
        if not text or target.is_absolute():
            _reject('Source links must use a relative target')
        current = path.parent
        parts = target.parts
        for index, part in enumerate(parts):
            current = current.parent if part == '..' else current / part
            if current != source and not current.is_relative_to(source):
                _reject('Source link leaves the checkout')
            info = current.lstat()
            final = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) if final else stat.S_ISDIR(info.st_mode)):
                _reject('Source links cannot traverse links or target directories')
            native_paths._canonical(str(current), regular_file=final, directory=not final)
        if not parts or current == source or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _reject('Source link target must be one regular file inside the checkout')
        generated = native_paths.generated_roots(recipe)
        if any(native_paths.overlaps(current, root) for root in generated):
            _reject('Source link cannot expose generated build output')
        signature = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                                   value.st_mtime_ns, value.st_ctime_ns)
        if (signature(path.lstat()) != signature(before) or os.readlink(path) != text
                or signature(current.lstat()) != signature(info)
                or signature(path.stat()) != signature(info)):
            _reject('Source link or target changed during observation')
        return text
    except (OSError, RuntimeError) as error:
        raise Rejected('checkout_scope', 'Source link requires an existing ordinary file inside the checkout') from error


def validate_tree(recipe: dict, *, deadline: float | None = None) -> dict[str, int]:
    """One endpoint scan, never continuous. Native roots use their own snapshot.

    Ordinary source and output files must have one link. Direct internal source
    file symlinks use validate_source_link; output aliases remain forbidden. This
    prevents an assessor's writable output alias from changing its checkout.
    The monotonic deadline is cooperative: it bounds further observations,
    but cannot interrupt an individual filesystem call inside the kernel.
    """
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise Rejected('checkout_deadline', 'A finite monotonic endpoint deadline is required')

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise Rejected('checkout_deadline', 'Checkout endpoint observation exceeded its shared deadline')

    check_deadline()
    _config(recipe)
    excluded = {Path(entry['path']) for entry in native_paths.validate(recipe) if entry['kind'] == 'generated'}
    counts = {}
    for key in ('source', 'outputs'):
        check_deadline()
        root = native_paths._canonical(recipe[key], directory=True)
        identity = native_paths.identity(root)
        pending = [root]
        count = 0
        while pending:
            check_deadline()
            directory = pending.pop()
            if directory.is_symlink() or directory.resolve() != directory:
                _reject('Checkout tree changed to a directory alias')
            for path in directory.iterdir():
                check_deadline()
                info = path.lstat()
                if path in excluded:
                    if not stat.S_ISDIR(info.st_mode):
                        _reject('Declared generated root changed type')
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        _reject('Checkout/output hardlinks require an isolated copy before execution')
                    count += 1
                elif key == 'source' and stat.S_ISLNK(info.st_mode):
                    validate_source_link(recipe, path)
                    count += 1
                else:
                    _reject('Checkout/output contains an unsupported symlink or special file')
        check_deadline()
        if native_paths.identity(root) != identity:
            _reject('Checkout/output identity changed during endpoint observation')
        counts[key] = count
    check_deadline()
    return counts


def profile(recipe: dict, state_dir: Path, assessment: bool = False, native: bool = False) -> dict[str, str]:
    validate(recipe, state_dir)
    _config(recipe)
    source, outputs = Path(recipe['source']), Path(recipe['outputs'])
    fs = {':root': 'deny', ':minimal': 'read', **{p: 'read' for p in recipe.get('read_roots', [])},
          str(source): 'read' if assessment or native else 'write', str(outputs): 'write'}
    for entry in native_paths.validate(recipe):
        if native or entry['kind'] == 'generated':
            fs[entry['path']] = 'write' if native else 'read'
    for name in _CONTROL_NAMES:
        fs[str(source / name)] = 'read'
    # Configurations are checked by the trusted coordinator for freshness,
    # never exposed as worker inputs. Add their denies after ordinary scope
    # validation so a project config may remain protected inside the checkout.
    denied = _denied(recipe, state_dir) | _instruction_paths(recipe, kinds=('configs',))
    cache = {}
    for path in _instruction_paths(recipe, kinds=('chain', 'support')):
        if not any(_below(path, guard, cache) for guard in denied):
            fs[str(path)] = 'read'
    if native:
        for path in _temporary_roots():
            fs[str(path)] = 'write'
        for path in _trusted(recipe):
            if not any(_below(path, guard, cache) for guard in denied):
                fs[str(path)] = 'read'
    fs.update({str(path): 'deny' for path in denied})
    return fs


def environment(recipe: dict, native: bool = False) -> dict[str, str]:
    value = _config(recipe)
    env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
           'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    if native:
        env.update(RUSTUP_AUTO_INSTALL='0', CARGO_NET_OFFLINE='true', GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0='core.fsmonitor', GIT_CONFIG_VALUE_0='false')
        env.update(value.get('native_environment', {}))
        _temporary_roots()
        env['TMPDIR'] = os.environ.get('TMPDIR') or tempfile.gettempdir()
    return env


def changes_allowed(recipe: dict, paths) -> bool:
    value = _config(recipe)
    if isinstance(paths, (str, bytes)):
        return False
    roots = [_relative(name) for name in value['change_roots']]
    source = Path(recipe['source'])
    instructions = _instruction_paths(recipe)
    cache = {}
    for name in paths:
        try:
            path = _relative(name)
        except Rejected:
            return False
        if (path == PurePosixPath('.') or _control(path)
                or not any(root == PurePosixPath('.') or path.is_relative_to(root) for root in roots)
                or any(native_paths.overlaps(source / str(path), control, _cache=cache) for control in instructions)):
            return False
    return True


def _shared_exclusions(paths, *, name_offset: int = 0) -> tuple[list[str], str | None]:
    """Name the same conjunction once, with at most 16 terms per definition.

    Every literal negative occurs exactly once. Definitions only factor the
    predicate; they introduce no operation, path merge or independent denial.
    """
    if not paths:
        return [], None
    terms = ['(require-not (subpath ' + native_sandbox._quoted(path) + '))' for path in paths]
    definitions = []
    while True:
        parents = []
        for start in range(0, len(terms), 16):
            group = terms[start:start + 16]
            name = 'aos-temporary-guard-' + str(name_offset + len(definitions))
            expression = group[0] if len(group) == 1 else '(require-all ' + ' '.join(group) + ')'
            definitions.append('(define ' + name + ' ' + expression + ')')
            parents.append(name)
        if len(parents) == 1:
            return definitions, parents[0]
        terms = parents


def _occupied_for_positive(root: str | None, occupied, temporary, cache: dict) -> tuple[str, ...]:
    """Discard only occupied prefixes proved disjoint from a known path root.

    A literal is conservatively treated as a subtree. Only our constructed Unix
    root regex uses this path proof; arbitrary regexes pass None and keep all.
    Known temporary root aliases are mapped without resolving child symlinks.
    All memoization belongs to the caller's one-render filesystem proof cache.
    """
    original = tuple(occupied)
    if root is None or root == '/' or not original:
        return original
    alias_spec = tuple((row['path'], tuple(row['aliases'])) for row in temporary)
    key = ('checkout_occupied_plan', original, alias_spec)
    if key not in cache:
        aliases = sorted(((Path(alias), Path(canonical)) for canonical, values in alias_spec for alias in values),
                         key=lambda pair: len(pair[0].parts), reverse=True)
        cache[key] = {'aliases': aliases, 'ordinary': {}, 'pairs': None, 'selections': {}}
    plan = cache[key]
    def ordinary(path):
        if path not in plan['ordinary']:
            parsed, result = Path(path), path
            for alias, canonical in plan['aliases']:
                if parsed.is_relative_to(alias):
                    result = str(canonical / parsed.relative_to(alias))
                    break
            plan['ordinary'][path] = result
        return plan['ordinary'][path]
    parent = ordinary(root)
    if parent not in plan['selections']:
        # This is exactly the first branch of the existing relation. An
        # uncertain positive retains every original spelling without N calls.
        if not native_sandbox._socket_canonical(parent, cache):
            plan['selections'][parent] = original
        else:
            if plan['pairs'] is None:
                plan['pairs'] = tuple((path, ordinary(path)) for path in original)
            related, retained = {}, []
            for path, normal in plan['pairs']:
                # Alias spellings share a decision, never a rewritten emitted
                # path. Cache each normalized pair only for this selection;
                # the completed selection then serves repeated positive roots.
                if normal not in related:
                    related[normal] = native_sandbox._socket_denial_relevant(
                        {'parent': parent, 'kind': 'root'}, normal, cache)
                if related[normal]:
                    retained.append(path)
            plan['selections'][parent] = tuple(retained)
    return plan['selections'][parent]


def _grant(operation: str, path: str, excludes, files=(), *, shared_guard: str | None = None) -> str:
    # Canonical negative prefixes stay closed if an external owner replaces a
    # protected file by a directory after compilation. Positive cache-file
    # grants remain literal, so replacement cannot grant child writes.
    match = native_sandbox._filter(path, files)
    if excludes or shared_guard:
        denied = ['(require-not (subpath ' + native_sandbox._quoted(p) + '))' for p in excludes]
        if shared_guard:
            denied.append(shared_guard)
        match = '(require-all ' + match + ' ' + ' '.join(denied) + ')'
    return '(allow ' + operation + ' ' + match + ')'


def render_native(recipe: dict, state_dir: Path, *, temporary_observation: dict | None = None,
                  network_observation: dict | None = None) -> str:
    """One native policy; local tests require retained host endpoint observations.

    Default IPC remains owned-output-only. Optional local tests use fresh temp
    names and observed finite TCP destinations, never a private namespace.
    """
    fs = profile(recipe, state_dir, native=True)
    grants = {p: 'read' for p in native_sandbox._MINIMAL}
    grants.update({p: mode for p, mode in fs.items() if not p.startswith(':')})
    temporary = _temporary_rows(recipe, temporary_observation)
    protected = {p for p, access in grants.items() if access == 'deny'}
    occupied = set()
    if temporary:
        # Exclude old names from file reads, writes, links/renames and IPC. A
        # forbidden socket cannot be renamed or linked to a fresh allowed name.
        protected = {str(alias) for path in protected for alias in _path_aliases(Path(path))}
        for row in temporary:
            for alias in row['aliases']:
                occupied.update(str(Path(alias) / entry['name']) for entry in row['entries'])
                for path in tuple(protected):
                    if Path(path).is_relative_to(Path(row['path'])):
                        protected.add(str(Path(alias) / Path(path).relative_to(row['path'])))
    denied = sorted(protected)
    occupied = tuple(sorted(occupied))
    literals = (_NATIVE_SYSTEM_READ_FILES
                | {entry['path'] for entry in recipe.get('native_write_roots', []) if entry['kind'] == 'cache-file'})
    cache = {}
    grant_paths = {path: Path(path) for path in grants}
    protected_paths, occupied_paths = tuple(map(Path, protected)), tuple(map(Path, occupied))
    for path, access in grants.items():
        if access != 'deny':
            if any(_below(grant_paths[path], guard, cache) for guard in protected_paths):
                _reject('A native grant cannot reopen protected state or credentials')
            # A pre-existing symlink's name is protected, not ownership of its
            # target. Keep this comparison lexical, including retained aliases.
            if any(grant_paths[path].is_relative_to(guard) for guard in occupied_paths):
                _reject('Native source, output or tools overlap a pre-existing temporary namespace')
    definitions, selected_guards, root_guards = [], {(): None}, {}
    roots = list(dict.fromkeys([recipe['outputs'], *(alias for row in temporary for alias in row['aliases'])]))
    for root in dict.fromkeys(['/', *(path for path, mode in sorted(grants.items()) if mode != 'deny'), *roots]):
        selected = _occupied_for_positive(root, occupied, temporary, cache)
        if selected not in selected_guards:
            parts, guard = _shared_exclusions(selected, name_offset=len(definitions))
            definitions.extend(parts)
            selected_guards[selected] = guard
        root_guards[root] = selected_guards[selected]
    lines = ['(version 1)', '(deny default)', *definitions, '(allow process-exec process-fork)',
             '(allow signal (target same-sandbox))', '(allow process-info* (target same-sandbox))',
             '(allow process-info-listpids)', '(allow sysctl-read (require-not (sysctl-name-prefix "kern.procargs")))',
             _grant('file-read-metadata', '/', denied, shared_guard=root_guards['/']), '(allow file-read-data (literal "/"))',
             '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") '
             '(global-name "com.apple.system.opendirectoryd.membership") (global-name "com.apple.cfprefsd.daemon") '
             '(global-name "com.apple.cfprefsd.agent") (global-name "com.apple.system.logger"))',
             '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow file-read* (subpath "/dev/fd"))', '(allow pseudo-tty)',
             '(allow file-read* file-write* file-ioctl (regex #"^/dev/(ptmx|ttys[0-9]+)$"))',
             '(allow iokit-open (iokit-user-client-class "AppleSMCClient"))']
    for path, access in sorted(grants.items()):
        if access == 'deny':
            continue
        shared_guard = root_guards[path]
        lines.append(_grant('file-read* file-map-executable', path, denied, literals, shared_guard=shared_guard))
        if access == 'write':
            exclusions = sorted(set(denied) | {p for p, mode in grants.items()
                if p != path and mode != 'write' and _below(grant_paths[p], grant_paths[path], cache)})
            lines.append(_grant('file-write*', path, exclusions, literals, shared_guard=shared_guard))
            if path not in literals and Path(path).is_dir():
                lines.append(_grant('file-link', path, exclusions, literals, shared_guard=shared_guard))
    for root in roots:
        shared_guard = root_guards[root]
        match = '(path-regex #' + native_sandbox._quoted('^' + native_sandbox._regex_path(root) + '/.*$') + ')'
        exclusions = ['(require-not (subpath ' + native_sandbox._quoted(p) + '))' for p in denied]
        if shared_guard:
            exclusions.append(shared_guard)
        match = '(require-all ' + match + ' ' + ' '.join(exclusions) + ')'
        lines.extend(('(allow network-bind network-inbound (local unix-socket ' + match + '))',
                      '(allow network-outbound (remote unix-socket ' + match + '))'))
    lines.extend(native_network.rules(recipe, network_observation))
    return '\n'.join(lines) + '\n'
