"""Declarative macOS boundary for trusted native checks, never model turns.

The recipe is part of the approved execution digest. It contains no Seatbelt
source or arbitrary regular expressions. Temporary names remain normal native
locations; preexisting matching paths are denied and retained for reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .store import Rejected
from .native_paths import _ancestors, _canonical, _lexical_relative_parts, overlaps
from . import native_network

VERSION = 'macos-seatbelt-v1'
_TOKENS = {'pid': '[1-9][0-9]*', 'counter': '[0-9]+', 'hex32': '[0-9a-f]' * 32,
           'hex16': '[0-9a-f]' * 16, 'token8': '[a-z0-9_]' * 8,
           'alnum6': '[A-Za-z0-9]' * 6, 'alnum10': '[A-Za-z0-9]' * 10,
           'crockford32': '[0-9A-HJKMNP-TV-Z]' + '[0-9A-HJKMNP-TV-Z]?' * 12}
_MINIMAL = ('/System', '/usr', '/bin', '/sbin', '/Library/Apple', '/private/var/db/dyld',
            '/private/var/db/timezone', '/private/etc/localtime')
_SOCKET_GROUP_LITERAL_BYTES = 768


def _read_file(recipe, entry, *, verify=False):
    if (not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}
            or not isinstance(entry.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
        _reject('Native file reads require exact path and SHA-256 bindings')
    path = _path(entry['path'])
    home = Path.home()
    confidential = [home / name for name in ('.codex', '.ssh', '.aws', '.gnupg', '.azure', '.kube',
                                             '.docker', '.config/gcloud', 'Library/Keychains')]
    confidential.extend(map(Path, ('/private/etc/ssh', '/private/var/db/dslocal', '/private/var/root')))
    if (path.name.lower() in {'.git-credentials', '.netrc', '_netrc', '.npmrc', '.pypirc', 'credentials',
                             'credentials.json', 'credentials.db', 'auth.json', 'access_tokens.db',
                             'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'kubeconfig'}
            or any(overlaps(path, root) for root in confidential)):
        _reject('Native file reads cannot expose credential or private-state paths')
    mutable = [Path(recipe[key]) for key in ('source', 'outputs') if key in recipe]
    mutable.extend(Path(root['path']) for root in recipe.get('native_write_roots', []))
    if any(overlaps(path, root) for root in mutable):
        _reject('Native read files must be outside model and native writable scopes')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _reject('Native read files must be existing regular files without inode aliases')
        if info.st_size > 1024 * 1024:
            _reject('Native read files must not exceed one MiB')
    except OSError as error:
        raise Rejected('native_sandbox', 'Native read file is unavailable') from error
    if not verify:
        return path
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_size > 1024 * 1024):
                _reject('Native read-file identity changed')
            data = stream.read(1024 * 1024 + 1)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                 info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(opened) != identity(finished) or identity(opened) != identity(current):
            _reject('Native read-file identity changed')
    except OSError as error:
        raise Rejected('native_sandbox', 'Native read file is unavailable') from error
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        _reject('Native read-file binding changed')
    if (re.search(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----', data)
            or re.search(rb'(?im)^\s*\[\s*credential(?:[\s"\]])', data)
            or re.search(rb'(?im)^\s*(?:password|passwd|token|access_token|refresh_token|secret|secret_key|client_secret|authorization|extraheader)\s*=', data)):
        _reject('Native file reads cannot expose private keys or credential settings')
    return path, opened


def _reject(message):
    raise Rejected('native_sandbox', message)


def _path(value, *, directory=False):
    if not isinstance(value, str) or not value or '\x00' in value or '\n' in value or '\r' in value:
        _reject('Native paths must be explicit absolute paths')
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != value
            or any(p.is_symlink() for p in (path, *path.parents)) or path.resolve() != path):
        _reject('Native paths must be canonical and contain no aliases')
    return _canonical(value, directory=directory)


def _name_regex(value):
    if not isinstance(value, str) or not value or len(value) > 180:
        _reject('Temporary name template is invalid')
    parts = re.split(r'(\{[a-z0-9]+\})', value)
    if value != '{token8}' and not re.fullmatch(r'(?:[A-Za-z]|\.[A-Za-z])[A-Za-z0-9_ .-]*', parts[0]):
        _reject('Temporary name requires a literal native prefix')
    result, tokens = '', 0
    for part in parts:
        if part.startswith('{'):
            if part[1:-1] not in _TOKENS:
                _reject('Unknown temporary name token')
            result += _TOKENS[part[1:-1]]
            tokens += 1
        else:
            if not re.fullmatch(r'[A-Za-z0-9_ .-]*', part):
                _reject('Temporary names accept safe literals and bounded tokens only')
            result += part.replace('.', '[.]')
    if value in ('.', '..'):
        _reject('Temporary name must identify a native path')
    return result


def config(recipe):
    value = recipe.get('native_sandbox')
    if value is None:
        return None
    allowed = {'version', 'localhost_tcp', 'ephemeral_tcp', 'unix_socket_roots', 'allow_pty', 'temporary_paths',
               'temporary_hardlink_paths', 'owned_temporary_roots', 'file_read_paths',
               'directory_create_paths', 'directory_read_paths', 'directory_write_access_paths',
               'denied_tcp_ports', 'denied_unix_sockets', 'iokit_user_clients'}
    if not isinstance(value, dict) or set(value) - allowed or value.get('version') != VERSION:
        _reject('Unsupported native sandbox contract')
    for key in ('localhost_tcp', 'allow_pty'):
        if type(value.get(key, False)) is not bool:
            _reject('Native capability switches must be boolean')
    read_files = value.get('file_read_paths', [])
    if not isinstance(read_files, list) or len(read_files) > 64:
        _reject('Native file reads require a bounded list of exact files')
    seen_reads = set()
    for entry in read_files:
        path = _read_file(recipe, entry)
        if path in seen_reads:
            _reject('Duplicate native read file')
        seen_reads.add(path)
    if value.get('localhost_tcp'):
        _reject('Host-local TCP is not qualified; protected-port exclusions must not be bypassed')
    native_network.contract(recipe)
    roots = value.get('unix_socket_roots', [])
    entries = value.get('temporary_paths', [])
    if not isinstance(roots, list) or not isinstance(entries, list) or len(roots) > 32 or len(entries) > 1024:
        _reject('Native socket and temporary scopes must be bounded lists')
    if not all(isinstance(p, str) for p in roots) or len(roots) != len(set(roots)):
        _reject('Duplicate native socket scope')
    for root in roots:
        _path(root, directory=True)
    directories = value.get('directory_create_paths', [])
    if (not isinstance(directories, list) or len(directories) > 32
            or not all(isinstance(p, str) for p in directories)
            or len(directories) != len(set(directories))):
        _reject('Native directory creation requires exact bounded paths')
    for directory in directories:
        path = _path(directory)
        if not path.parent.is_dir() or (path.exists() and not path.is_dir()):
            _reject('Native directory creation requires a real parent and absent or real directory')
    read_directories = value.get('directory_read_paths', [])
    if (not isinstance(read_directories, list) or len(read_directories) > 32
            or not all(isinstance(p, str) for p in read_directories)
            or len(read_directories) != len(set(read_directories))):
        _reject('Native directory reads require exact bounded paths')
    for directory in read_directories:
        _path(directory, directory=True)
    ports = value.get('denied_tcp_ports', [])
    denied_sockets = value.get('denied_unix_sockets', [])
    if (not isinstance(ports, list) or len(ports) > 128
            or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
            or len(set(ports)) != len(ports) or not isinstance(denied_sockets, list) or len(denied_sockets) > 128):
        _reject('Protected native network endpoints must be exact bounded lists')
    for path in denied_sockets:
        _path(path)
    clients = value.get('iokit_user_clients', [])
    if not isinstance(clients, list) or clients not in ([], ['AppleSMCClient']):
        _reject('Only the separately qualified AppleSMCClient native capability is supported')
    seen, parents = set(), set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {'parent', 'name', 'kind'}
                or entry['kind'] not in ('tree', 'file', 'socket')):
            _reject('Native temporary scope requires parent, name and kind')
        if not isinstance(entry['parent'], str):
            _reject('Native temporary parent must be an exact absolute directory')
        if entry['parent'] not in parents:
            _path(entry['parent'], directory=True)
            parents.add(entry['parent'])
        _name_regex(entry['name'])
        if entry['name'] == '{token8}' and entry['kind'] != 'file':
            _reject('Temporary unprefixed random names must be exact files')
        key = entry['parent'], entry['name']
        if key in seen:
            _reject('Duplicate native temporary scope')
        seen.add(key)
    hardlinks = value.get('temporary_hardlink_paths', [])
    tree_scopes = {(entry['parent'], entry['name']) for entry in entries if entry['kind'] == 'tree'}
    if not isinstance(hardlinks, list) or len(hardlinks) > 32:
        _reject('Native temporary hardlinks require a bounded list of tree references')
    linked = set()
    for reference in hardlinks:
        if (not isinstance(reference, dict) or set(reference) != {'parent', 'name'}
                or not all(isinstance(reference[key], str) for key in ('parent', 'name'))):
            _reject('Native temporary hardlinks require exact tree references')
        key = reference['parent'], reference['name']
        if key not in tree_scopes or key in linked:
            _reject('Native temporary hardlinks must reference unique declared tree scopes')
        linked.add(key)
    owned = value.get('owned_temporary_roots', [])
    literal_trees = {str(Path(entry['parent']) / entry['name']) for entry in entries
                     if entry['kind'] == 'tree' and '{' not in entry['name']}
    if (not isinstance(owned, list) or len(owned) > 32
            or not all(isinstance(path, str) for path in owned) or len(owned) != len(set(owned))):
        _reject('Owned temporary roots require a bounded unique list of exact paths')
    for path in owned:
        _path(path)
        if path not in literal_trees:
            _reject('Owned temporary roots must reference declared literal tree scopes')
    write_access_directories = value.get('directory_write_access_paths', [])
    if (not isinstance(write_access_directories, list) or len(write_access_directories) > 32
            or not all(isinstance(p, str) for p in write_access_directories)
            or len(write_access_directories) != len(set(write_access_directories))):
        _reject('Native directory write-access checks require exact bounded paths')
    for directory in write_access_directories:
        _path(directory, directory=True)
        if directory not in parents:
            _reject('Native directory write-access checks require an existing temporary template parent')
    return value


def _compiled_entries(value):
    result = {}
    for entry in value.get('temporary_paths', []):
        result.setdefault(Path(entry['parent']), []).append(
            (re.compile(_name_regex(entry['name']), re.IGNORECASE), entry['kind']))
    for directory in value.get('directory_create_paths', []):
        path = Path(directory)
        result.setdefault(path.parent, []).append((re.compile(re.escape(path.name), re.IGNORECASE), 'directory'))
    return result


def _relative_parts(path, parent, cache):
    # A render revisits the same paths for many temporary templates. Ancestor
    # identities are already frozen in this invocation's cache; retain their
    # ordered relation too, including non-overlap and the empty root relation.
    key = ('relative_parts', path, parent)
    if key not in cache:
        cache[key] = _relative_parts_uncached(path, parent, cache)
    return cache[key]


def _relative_parts_uncached(path, parent, cache):
    lexical = _lexical_relative_parts(path, parent)
    if lexical is not None:
        return lexical
    key = ('relative_parent_ancestors', parent)
    if key not in cache:
        parent_ancestors = {}
        for identity, suffix in _ancestors(parent, cache):
            parent_ancestors.setdefault(identity, []).append(suffix)
        # The underlying ancestry is already frozen in this invocation. Reuse
        # its ordered index when many distinct paths share the same parent.
        cache[key] = {identity: tuple(suffixes) for identity, suffixes in parent_ancestors.items()}
    parent_ancestors = cache[key]
    for identity, suffix in _ancestors(path, cache):
        for parent_suffix in parent_ancestors.get(identity, ()):
            if suffix[:len(parent_suffix)] == parent_suffix:
                return suffix[len(parent_suffix):]
    return None


def _writable(path, entries, *, cache=None):
    path = Path(path)
    cache = {} if cache is None else cache
    for parent, patterns in entries.items():
        relative = _relative_parts(path, parent, cache)
        if relative:
            # Conservatively protect case aliases even on a case-sensitive
            # volume; the host's usual APFS volumes are case-insensitive.
            for pattern, kind in patterns:
                if pattern.fullmatch(relative[0]) and (len(relative) == 1 or kind == 'tree'):
                    return True
    return False


def writable(path, recipe):
    """Whether a temporary template grants this path, without following links."""
    value = config(recipe)
    return value is not None and _writable(path, _compiled_entries(value))


def protect(recipe, paths):
    """Keep protected code and evidence immutable, without denying their reads.

    Read confidentiality is enforced by the filesystem's explicit deny entries
    in render(); protected paths also include intentionally readable toolchains.
    """
    value = config(recipe)
    if value is None:
        return
    entries, cache = _compiled_entries(value), {}
    for candidate in paths:
        path = Path(candidate)
        if _writable(path, entries, cache=cache) or any(
                _relative_parts(parent, path, cache) is not None for parent in entries):
            _reject('Native temporary scope overlaps protected code, source or evidence')


def temporary_snapshot(recipe):
    """Observe direct matching roots only; never infer ownership from their names."""
    value, result = config(recipe), {}
    if value is None:
        return result
    for parent, patterns in _compiled_entries(value).items():
        for path in parent.iterdir():
            if not any(pattern.fullmatch(path.name) for pattern, _kind in patterns):
                continue
            info = path.lstat()
            result[str(path)] = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
                                 'mode': info.st_mode, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
                                 'link': os.readlink(path) if stat.S_ISLNK(info.st_mode) else None}
    return result


def _quoted(value):
    return json.dumps(value, ensure_ascii=True)


def _regex_path(value):
    # SBPL regex literals do not use ordinary JSON/Python backslash escaping.
    # Keep recipe paths declarative and use character classes for literal dots.
    if not re.fullmatch(r'[A-Za-z0-9_@ ./-]+', value):
        _reject('Native socket and temporary paths use unsupported regex-literal characters')
    return value.replace('.', '[.]')


def _socket_canonical(path, cache):
    key = ('socket_canonical', path)
    if key not in cache:
        try:
            parsed = Path(path)
            if (path == '/' or parsed.anchor != '/' or str(parsed) != path
                    or any(part in ('.', '..') for part in parsed.parts)):
                raise ValueError('Nonordinary absolute spelling')
            _canonical(path)
            cache[key] = True
        except Exception:
            # Uncertain spellings keep their original, individual denial.
            cache[key] = False
    return cache[key]


def _socket_suffix_matches(entry, suffix, *, insensitive=False):
    if entry['kind'] == 'root':
        return True
    if not suffix or (entry['kind'] == 'socket' and len(suffix) != 1):
        return False
    return re.fullmatch(_name_regex(entry['name']), suffix[0],
                        flags=re.IGNORECASE if insensitive else 0) is not None


def _socket_denial_relevant(entry, denial, cache):
    """Discard only prefix languages proved disjoint from one positive scope."""
    if not _socket_canonical(entry['parent'], cache) or not _socket_canonical(denial, cache):
        return True
    parent_parts, denial_parts = Path(entry['parent']).parts, Path(denial).parts
    for insensitive in (False, True):
        parent = tuple(part.casefold() for part in parent_parts) if insensitive else parent_parts
        denied = tuple(part.casefold() for part in denial_parts) if insensitive else denial_parts
        if parent[:len(denied)] == denied:
            return True
        if (denied[:len(parent)] == parent
                and _socket_suffix_matches(entry, denial_parts[len(parent):], insensitive=insensitive)):
            return True
    try:
        forward = _relative_parts(Path(denial), Path(entry['parent']), cache)
        reverse = _relative_parts(Path(entry['parent']), Path(denial), cache)
    except Exception:
        return True
    return reverse is not None or (forward is not None and _socket_suffix_matches(
        entry, forward, insensitive=True))


def _socket_denial_pattern(members):
    if len(members) == 1:
        return '^' + _regex_path(members[0]) + '(/.*)?$'
    parent = str(Path(members[0]).parent)
    prefix = _regex_path(parent) + ('' if parent == '/' else '/')
    names = [_regex_path(Path(path).name) for path in members]
    return '^' + prefix + '(' + '|'.join(names) + ')(/.*)?$'


def _socket_factored_pattern(members):
    """Compact literal affixes only after the raw grouping decision is fixed."""
    if len(members) == 1:
        return _socket_denial_pattern(members)
    names = [Path(path).name for path in members]
    prefix = os.path.commonprefix(names)[:min(map(len, names)) - 1]
    remaining = [name[len(prefix):] for name in names]
    reversed_suffix = os.path.commonprefix([name[::-1] for name in remaining])
    suffix = reversed_suffix[:min(map(len, remaining)) - 1][::-1]
    middles = [name[:-len(suffix)] if suffix else name for name in remaining]
    parent = str(Path(members[0]).parent)
    base = _regex_path(parent) + ('' if parent == '/' else '/')
    literal_prefix = _regex_path(prefix) if prefix else ''
    literal_suffix = _regex_path(suffix) if suffix else ''
    return ('^' + base + literal_prefix + '(' + '|'.join(_regex_path(name) for name in middles)
            + ')' + literal_suffix + '(/.*)?$')


def _socket_denial_groups(paths, cache):
    """Represent every forbidden prefix once, with at most 16 exact siblings.

    Reuse these same predicate leaves in positive guards and explicit denials.
    The encoded-literal budget bounds grouping, not an asserted OS limit.
    Oversized singletons retain their own group. Literal validation and the
    original regex fallback for uncertain spellings remain mandatory.
    """
    by_parent, member_groups = {}, []
    for index, path in enumerate(paths):
        if _socket_canonical(path, cache):
            by_parent.setdefault(str(Path(path).parent), []).append(index)
        else:
            member_groups.append([index])
    for indices in by_parent.values():
        pending = []
        for index in indices:
            candidate = pending + [index]
            pattern = _socket_denial_pattern([paths[item] for item in candidate])
            literal_bytes = len(('#' + _quoted(pattern)).encode())
            if pending and (len(candidate) > 16 or literal_bytes > _SOCKET_GROUP_LITERAL_BYTES):
                member_groups.append(pending)
                pending = [index]
            else:
                pending = candidate
        if pending:
            member_groups.append(pending)
    member_groups.sort(key=lambda indices: indices[0])
    groups = []
    for indices in member_groups:
        members = [paths[index] for index in indices]
        # Keep the existing literal validation even when the final predicate
        # uses the kernel's exact path-prefix primitive instead of a regex.
        pattern = _socket_factored_pattern(members)
        if all(_socket_canonical(path, cache) for path in members):
            leaf = '(require-any ' + ' '.join('(subpath ' + _quoted(path) + ')' for path in members) + ')'
        else:
            # Nonordinary or uncertain spellings retain their original denial.
            leaf = '(path-regex #' + _quoted(pattern) + ')'
        groups.append((members, leaf))
    return groups


def _filter(path, files=()):
    # A literal file grant cannot authorize siblings. A directory subtree is
    # canonicalized by Seatbelt before matching, so symlinks do not widen it.
    return '(' + ('literal' if path in files or Path(path).is_file() else 'subpath') + ' ' + _quoted(path) + ')'


def _grant(operation, path, excludes, files=()):
    match = _filter(path, files)
    if excludes:
        match = '(require-all ' + match + ' ' + ' '.join('(require-not ' + _filter(p, files) + ')' for p in excludes) + ')'
    return '(allow ' + operation + ' ' + match + ')'


def _prune_grants(grants, files):
    """Drop identical descendants while preserving every permission transition."""
    result = {}
    for name in sorted(grants, key=lambda p: (len(Path(p).parts), p)):
        ancestor = next((str(p) for p in Path(name).parents if str(p) in result
                         and str(p) not in files and not p.is_file()), None)
        if ancestor is None or result[ancestor] != grants[name]:
            result[name] = grants[name]
    return result


def _temporary_filter(entry, grants, literal_paths, cache, *, write=False):
    full = '^' + _regex_path(entry['parent']) + '/' + _name_regex(entry['name'])
    suffix = '(/.*)?$' if entry['kind'] == 'tree' else '$'
    pattern = '(regex #' + _quoted(full + suffix) + ')'
    template = _compiled_entries({'temporary_paths': [entry]})
    exclusions = []
    parent = Path(entry['parent'])
    for path, access in grants.items():
        if access != 'deny' and not (write and access == 'read'):
            continue
        relative = _relative_parts(Path(path), parent, cache)
        if _writable(path, template, cache=cache):
            # SBPL negative path filters do not canonicalize every macOS
            # firmlink alias. Express restrictions in the granted namespace.
            resolved = str(parent.joinpath(*relative))
            match = '(' + ('literal' if path in literal_paths or Path(path).is_file() else 'subpath') + ' ' + _quoted(resolved) + ')'
            exclusions.append(match)
        elif (access == 'deny' and path not in literal_paths
              and _relative_parts(parent, Path(path), cache) is not None):
            # A denied ancestor blocks the entire template, including aliases.
            return None
    if exclusions:
        pattern = '(require-all ' + pattern + ' ' + ' '.join(
            '(require-not ' + match + ')' for match in sorted(set(exclusions))) + ')'
    return pattern


def verify_file_read_paths(recipe, filesystem):
    """Verify native-only immutable reads before launch or cached-check reuse.

    The caller supplies its complete native filesystem map, so explicit state
    denials are checked before any bound file contents are read.
    """
    value = config(recipe)
    if value is None:
        return []
    templates, cache, verified = _compiled_entries(value), {}, []
    for entry in value.get('file_read_paths', []):
        path = Path(entry['path'])
        for grant, access in filesystem.items():
            if grant.startswith(':'):
                continue
            if access == 'deny' and _relative_parts(path, Path(grant), cache) is not None:
                _reject('Native file reads cannot override an explicit denied namespace')
            if access == 'write' and overlaps(path, Path(grant), _cache=cache):
                _reject('Native read files cannot overlap writable scopes')
        if _writable(path, templates, cache=cache) or any(
                _relative_parts(parent, path, cache) is not None for parent in templates):
            _reject('Native read files cannot overlap temporary scopes')
        path, info = _read_file(recipe, entry, verify=True)
        verified.append({**entry, 'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size})
    return verified


def render(recipe, filesystem, *, preexisting=None, protected=(), network_observation=None):
    """Compile exact native filesystem grants and local IPC into one profile.

    Call with the native-only filesystem map, never the model writer map.
    `preexisting` must be the retained snapshot taken before launch. New matching
    resources are merely reported later; this module never removes them.
    """
    value = config(recipe)
    if value is None:
        _reject('An explicit native sandbox contract is required')
    templates, path_cache = _compiled_entries(value), {}
    if filesystem.get(':root') != 'deny' or set(k for k in filesystem if k.startswith(':')) - {':root', ':minimal'}:
        _reject('Native sandbox requires a default-deny filesystem')
    grants = {}
    literal_paths = {r['path'] for r in recipe.get('native_write_roots', []) if r.get('kind') == 'cache-file'}
    # A create-only directory declaration owns its literal mkdir operation, not
    # its descendants. Preserve separate preexisting-child denials when pruning.
    literal_paths.update(path for path in value.get('directory_create_paths', [])
                         if filesystem.get(path) != 'deny')
    if filesystem.get(':minimal') == 'read':
        grants.update({p: 'read' for p in _MINIMAL})
    elif ':minimal' in filesystem:
        _reject('Native minimal system scope must be read-only')
    for name, access in filesystem.items():
        if name.startswith(':'):
            continue
        _path(name)
        if access not in ('read', 'write', 'deny'):
            _reject('Invalid native filesystem access')
        grants[name] = access
    read_files = verify_file_read_paths(recipe, filesystem)
    read_file_paths = {entry['path'] for entry in read_files}
    for entry in read_files:
        literal_paths.add(entry['path'])
        grants[entry['path']] = 'read'
    protect(recipe, protected)
    for root in value.get('unix_socket_roots', []):
        ancestors = [p for p in grants if Path(root).is_relative_to(p)]
        if not ancestors or grants[max(ancestors, key=lambda p: len(Path(p).parts))] != 'write':
            _reject('Unix socket roots must be inside an explicit native writable scope')
        if any(overlaps(Path(root), Path(p)) for p in protected):
            _reject('Unix socket scope overlaps protected code or evidence')
    before = temporary_snapshot(recipe) if preexisting is None else preexisting
    for path in before:
        if not _writable(path, templates, cache=path_cache):
            _reject('Preexisting temporary snapshot is outside declared templates')
        grants[str(_path(path))] = 'deny'
    # Filesystem pruning cannot discard restrictions subsequently crossed by a
    # temporary template (for example a read-only child beneath a read parent).
    temporary_grants = dict(grants)
    grants = _prune_grants(grants, literal_paths)
    for directory in value.get('directory_create_paths', []):
        path = Path(directory)
        if any(access == 'deny' and not grant.startswith(':')
               and _relative_parts(path, Path(grant), path_cache) is not None
               for grant, access in filesystem.items()):
            _reject('Native directory creation cannot override an explicit denied namespace')
        if any(access == 'write' and overlaps(path, Path(grant), _cache=path_cache)
               for grant, access in grants.items()) or _writable(
                   path, _compiled_entries({'temporary_paths': value.get('temporary_paths', [])}), cache=path_cache):
            _reject('Directory-create-only scope overlaps a broader native write grant')
    for directory in value.get('directory_read_paths', []):
        if any(access == 'deny' and _relative_parts(Path(directory), Path(grant), path_cache) is not None
               for grant, access in grants.items()):
            _reject('Native directory reads cannot override an explicit denied namespace')
    for directory in value.get('directory_write_access_paths', []):
        if any(access == 'deny' and _relative_parts(Path(directory), Path(grant), path_cache) is not None
               for grant, access in grants.items()):
            _reject('Native directory write-access checks cannot override an explicit denied namespace')
    lines = ['(version 1)', '(deny default)', '(allow process-exec process-fork)',
             '(allow signal (target same-sandbox))', '(deny process-info*)',
             '(allow process-info* (target same-sandbox))', '(allow process-info-listpids)',
             '(allow sysctl-read (require-not (sysctl-name-prefix "kern.procargs")))',
             '(allow file-read-metadata)', '(allow file-read-data (literal "/"))',
             '(deny file-link)',
             '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") '
             '(global-name "com.apple.system.opendirectoryd.membership") '
             '(global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.cfprefsd.agent") '
             '(global-name "com.apple.system.logger"))',
             '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             '(allow file-read* (subpath "/dev/fd"))']
    for path, access in sorted(grants.items()):
        descendants = [p for p in grants if p != path and Path(p).is_relative_to(path)]
        if access != 'deny':
            operations = 'file-read*' if path in read_file_paths else 'file-read* file-map-executable'
            lines.append(_grant(operations, path,
                                sorted(p for p in descendants if grants[p] == 'deny'), literal_paths))
        if access == 'write':
            lines.append(_grant('file-write*', path, sorted(p for p in descendants if grants[p] != 'write'), literal_paths))
    for declaration in recipe.get('native_write_roots', []):
        if declaration.get('kind') not in ('generated', 'cache'):
            continue
        root = declaration['path']
        if grants.get(root) != 'write' or not Path(root).is_dir():
            _reject('Native hardlink roots require exact writable directory grants')
        excludes = sorted(p for p in grants if p != root and Path(p).is_relative_to(root)
                          and grants[p] != 'write')
        # On the qualified kernel file-link checks both endpoints separately.
        # Do not grant it to outputs, temporary templates, devices, exact cache
        # files or metadata-only directories merely because they allow writes.
        lines.append(_grant('file-link', root, excludes, literal_paths))
    linked = {(reference['parent'], reference['name']) for reference in value.get('temporary_hardlink_paths', [])}
    for entry in value.get('temporary_paths', []):
        read_pattern = _temporary_filter(entry, temporary_grants, literal_paths, path_cache)
        write_pattern = _temporary_filter(entry, temporary_grants, literal_paths, path_cache, write=True)
        if read_pattern == write_pattern and read_pattern is not None:
            lines.append('(allow file-read* file-write* ' + read_pattern + ')')
        else:
            if read_pattern is not None:
                lines.append('(allow file-read* ' + read_pattern + ')')
            if write_pattern is not None:
                lines.append('(allow file-write* ' + write_pattern + ')')
        if (entry['parent'], entry['name']) in linked and write_pattern is not None:
            # The qualified kernel checks both endpoints. Reuse the exact
            # new-tree filter, including preexisting and denied-ancestor guards.
            lines.append('(allow file-link ' + write_pattern + ')')
    for directory in value.get('directory_create_paths', []):
        # Existing mkdir must still return EEXIST. Creation cannot replace a
        # preexisting directory, and this grants no data, child, chmod or delete.
        lines.append('(allow file-write-create (require-all (literal ' + _quoted(directory)
                     + ') (vnode-type DIRECTORY)))')
    for directory in value.get('directory_read_paths', []):
        lines.append('(allow file-read-data (require-all (literal ' + _quoted(directory)
                     + ') (vnode-type DIRECTORY)))')
    for directory in value.get('directory_write_access_paths', []):
        # Bash checks W_OK before choosing its normal here-document directory.
        # This literal directory operation grants no child namespace, listing,
        # metadata change, creation, deletion or cleanup authority.
        lines.append('(allow file-write-data (require-all (literal ' + _quoted(directory)
                     + ') (vnode-type DIRECTORY)))')
    if value.get('allow_pty'):
        lines.extend(['(allow pseudo-tty)',
                      '(allow file-read* file-write* file-ioctl (regex #"^/dev/(ptmx|ttys[0-9]+)$"))'])
    for client in value.get('iokit_user_clients', []):
        # A class grant is not a payload-level read/write filter. Qualification
        # exercises the pinned read-only provider; no SMC write method is run.
        lines.append('(allow iokit-open (iokit-user-client-class ' + _quoted(client) + '))')
    # TCP is opt-in. Positive finite destinations retain protected service
    # exclusions that broad wildcard-plus-deny rules cannot enforce here.
    lines.extend(native_network.rules(recipe, network_observation))
    for port in value.get('denied_tcp_ports', []):
        endpoint = _quoted('*:' + str(port))
        lines.append('(deny network-outbound (remote ip ' + endpoint + '))')
        lines.append('(deny network-bind network-inbound (local ip ' + endpoint + '))')
    socket_entries = [{'kind': 'root', 'parent': root} for root in value.get('unix_socket_roots', [])]
    socket_entries.extend(entry for entry in value.get('temporary_paths', []) if entry['kind'] in ('tree', 'socket'))
    socket_denials = sorted(set(before) | set(map(str, protected)) | set(value.get('denied_unix_sockets', [])))
    socket_cache = {}
    socket_groups = _socket_denial_groups(socket_denials, socket_cache)
    socket_filters = []
    for entry in socket_entries:
        pattern = '^' + _regex_path(entry['parent']) + '/'
        pattern += ('.*$' if entry['kind'] == 'root' else _name_regex(entry['name'])
                    + ('/.*$' if entry['kind'] == 'tree' else '$'))
        path_filter = '(path-regex #' + _quoted(pattern) + ')'
        excluded = ['(require-not ' + leaf + ')' for members, leaf in socket_groups
                    if any(_socket_denial_relevant(entry, path, socket_cache) for path in members)]
        if excluded:
            # Keep the whole group if any member can overlap. Every additional
            # member is disjoint from this positive; uncertainty always keeps it.
            path_filter = '(require-all ' + path_filter + ' ' + ' '.join(excluded) + ')'
        socket_filters.append(path_filter)
    for operation, direction in (('network-bind network-inbound', 'local'), ('network-outbound', 'remote')):
        for path_filter in socket_filters:
            lines.append('(allow ' + operation + ' (' + direction + ' unix-socket ' + path_filter + '))')
    # Explicit socket denial is separate from file access; a preexisting socket
    # must never become reachable through a broader declared native prefix.
    for _members, leaf in socket_groups:
        for operation, direction in (('network-bind network-inbound', 'local'), ('network-outbound', 'remote')):
            lines.append('(deny ' + operation + ' (' + direction + ' unix-socket ' + leaf + '))')
    return '\n'.join(lines) + '\n'
