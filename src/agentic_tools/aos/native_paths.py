"""Exact native build paths, separate from frozen product source.

These declarations are part of the approved recipe. They do not establish
external cache exclusivity, grant model permissions, or authorize cleanup.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path, PosixPath

from .store import Rejected


def _lexical_relative_parts(path: Path, parent: Path) -> tuple | None:
    if type(path) is PosixPath and type(parent) is PosixPath:
        parts, parent_parts = path.parts, parent.parts
        # An empty relative parent ('.') must not match an absolute path.
        # Keep lexical '..', spelling and distinct '/'/'//' anchors unchanged.
        if path.anchor == parent.anchor and parts[:len(parent_parts)] == parent_parts:
            return parts[len(parent_parts):]
        return None
    if path.is_relative_to(parent):
        return path.relative_to(parent).parts
    return None


def _ancestors(path: Path, cache: dict) -> tuple:
    key = ('ancestors', path)
    if key not in cache:
        values = []
        parts = path.parts if type(path) is PosixPath else None
        for ancestor in (path, *path.parents):
            identity_key = ('identity', ancestor)
            if identity_key not in cache:
                try:
                    info = ancestor.stat()
                    cache[identity_key] = (info.st_dev, info.st_ino)
                except (FileNotFoundError, NotADirectoryError):
                    cache[identity_key] = None
                except OSError as error:
                    raise Rejected('native_scope', 'Cannot verify physical native path ancestry') from error
            if cache[identity_key] is not None:
                suffix = parts[len(ancestor.parts):] if parts is not None else path.relative_to(ancestor).parts
                values.append((cache[identity_key], suffix))
        cache[key] = tuple(values)
    return cache[key]


def overlaps(left: Path, right: Path, *, _cache: dict | None = None) -> bool:
    """Compare names and physical ancestors, including macOS firmlink aliases."""
    if type(left) is PosixPath and type(right) is PosixPath:
        lexical = (left == right or _lexical_relative_parts(left, right) is not None
                   or _lexical_relative_parts(right, left) is not None)
    else:
        lexical = left == right or left.is_relative_to(right) or right.is_relative_to(left)
    if lexical:
        return True
    cache = {} if _cache is None else _cache
    right_ancestors = {}
    for identity_value, suffix in _ancestors(right, cache):
        right_ancestors.setdefault(identity_value, []).append(suffix)
    for identity_value, suffix in _ancestors(left, cache):
        for other in right_ancestors.get(identity_value, ()):
            if suffix[:len(other)] == other or other[:len(suffix)] == suffix:
                return True
    return False


def _canonical(value: str, *, directory: bool = False, regular_file: bool = False) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise Rejected('native_scope', 'Native paths must be exact canonical absolute paths')
    path = Path(value)
    if (str(path) != value or path.resolve() != path or
            any(p.is_symlink() for p in (path, *path.parents))):
        raise Rejected('native_scope', 'Native paths must not contain aliases or symlinks')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        child = current / part
        if not child.exists():
            break
        # Path.resolve() preserves case aliases on the usual macOS filesystem.
        # Require the actual directory-entry spelling before comparing scopes.
        if part not in os.listdir(current):
            raise Rejected('native_scope', 'Native paths must use canonical directory-entry spelling')
        current = child
    if directory and not path.is_dir():
        raise Rejected('native_scope', 'Native write roots must be existing real directories')
    if regular_file and ((path.exists() and not path.is_file()) or not path.parent.is_dir()):
        raise Rejected('native_scope', 'Native cache files need a real parent and must be absent or regular files')
    return path


def validate(recipe: dict, *, protected=()) -> list[dict]:
    """Reject broad roots, source exclusions and protected namespace overlap."""
    declarations = recipe.get('native_write_roots', [])
    if not isinstance(declarations, list):
        raise Rejected('native_scope', 'Native write roots must be an exact list')
    source, outputs = Path(recipe['source']), Path(recipe['outputs'])
    declared_protected = recipe.get('native_protected_paths', [])
    if not isinstance(declared_protected, list):
        raise Rejected('native_scope', 'Native protected paths must be an exact list')
    guards = [Path(p).resolve() for p in protected]
    # The probe and coordinator import trusted code from their runtimes, not
    # merely their executable files. Cache grants must not make that code mutable.
    guards.extend((Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()))
    if recipe.get('probe_interpreter'):
        for interpreter in (Path(recipe['probe_interpreter']), Path(recipe['probe_interpreter']).resolve()):
            guards.extend((interpreter, interpreter.parent.parent
                           if interpreter.parent.name in ('bin', 'Scripts') else interpreter.parent))
    guards.extend(_canonical(p) for p in declared_protected)
    guards.extend(source / p for p in [*recipe.get('mutable', []), *recipe.get('immutable', {})])
    # Repository control files are never generated output namespaces, even when
    # absent in an exported tree. Applicable external instructions stay protected.
    guards.extend(source / p for p in ('.git', '.codex', 'AGENTS.md', 'AGENTS.override.md'))
    manifest = recipe.get('instruction_manifest', {})
    for key in ('chain', 'support', 'configs'):
        for entry in manifest.get(key, []):
            guards.extend(Path(entry[p]).resolve() for p in ('path', 'resolved') if p in entry)
    result = []
    overlap_cache = {}
    for declaration in declarations:
        if (not isinstance(declaration, dict) or set(declaration) != {'path', 'kind'} or
                declaration['kind'] not in ('generated', 'cache', 'cache-file')):
            raise Rejected('native_scope', 'Each native root needs only an exact path and generated/cache/cache-file kind')
        root = _canonical(declaration['path'], directory=declaration['kind'] != 'cache-file',
                          regular_file=declaration['kind'] == 'cache-file')
        if declaration['kind'] == 'generated':
            if root == source or not root.is_relative_to(source):
                raise Rejected('native_scope', 'Generated roots must be strict source children')
        elif overlaps(root, source, _cache=overlap_cache):
            raise Rejected('native_scope', 'Native cache roots must be disjoint from source')
        if overlaps(root, outputs, _cache=overlap_cache) or any(overlaps(root, p, _cache=overlap_cache) for p in guards):
            raise Rejected('native_scope', 'Native writes overlap output, protected or mutable scope')
        if any(overlaps(root, Path(entry['path']), _cache=overlap_cache) for entry in result):
            raise Rejected('native_scope', 'Native roots must be distinct and non-overlapping')
        result.append(dict(declaration))
    return result


def writable_roots(recipe: dict, *, protected=()) -> tuple[Path, ...]:
    return tuple(Path(entry['path']) for entry in validate(recipe, protected=protected))


def generated_roots(recipe: dict) -> tuple[Path, ...]:
    return tuple(Path(entry['path']) for entry in validate(recipe) if entry['kind'] == 'generated')


def identity(path: Path) -> dict:
    _canonical(str(path), directory=True)
    info = path.lstat()
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}


def _entry(path: Path) -> dict:
    info = path.lstat()
    item = {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode,
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
            'nlink': info.st_nlink}
    if stat.S_ISLNK(info.st_mode):
        item.update(kind='symlink', target=os.readlink(path))
    elif stat.S_ISDIR(info.st_mode):
        item['kind'] = 'directory'
    elif stat.S_ISREG(info.st_mode):
        item['kind'] = 'file'
    else:
        raise Rejected('native_effects', 'Native root contains a live socket or special file')
    return item


def snapshot(recipe: dict, *, check_deadline=None) -> dict:
    """Observe owned namespaces without following links or claiming attribution.

    Native artifacts are not source and are not execution evidence. Metadata
    records their filesystem effects without hashing multi-gigabyte build caches;
    raw check outputs retain their independent cryptographic evidence hashes.
    Sockets and other special files must be gone before reconciliation succeeds.
    """
    result = {}
    aliases = {}
    for entry in validate(recipe):
        root = Path(entry['path'])
        boundary = root.parent if entry['kind'] == 'cache-file' else root
        before = identity(boundary)
        if entry['kind'] == 'cache-file':
            observed = _entry(root) if root.exists() else {'kind': 'absent'}
            if observed['kind'] not in ('file', 'absent') or identity(boundary) != before:
                raise Rejected('native_effects', 'Native cache file boundary changed during observation')
            if observed['kind'] == 'file':
                aliases.setdefault((observed['device'], observed['inode']), []).append((root, observed))
            result[str(root)] = {'kind': entry['kind'], 'identity': before, 'entries': {root.name: observed}}
            continue
        files = {}
        pending = [root]
        while pending:
            directory = pending.pop()
            if check_deadline:
                check_deadline()
            if directory.is_symlink() or directory.resolve() != directory:
                raise Rejected('native_effects', 'Native directory changed to an alias during observation')
            for path in sorted(directory.iterdir()):
                if check_deadline:
                    check_deadline()
                item = _entry(path)
                if item['kind'] == 'directory':
                    pending.append(path)
                elif item['kind'] == 'file':
                    aliases.setdefault((item['device'], item['inode']), []).append((path, item))
                files[str(path.relative_to(root))] = item
        if identity(root) != before:
            raise Rejected('native_effects', 'Native root identity changed during observation')
        result[str(root)] = {'kind': entry['kind'], 'identity': before, 'entries': files}
    for entries in aliases.values():
        # Cargo may hardlink its own artifacts. Every inode alias must remain
        # within the exact declared writable namespaces, including across roots.
        if any(item['nlink'] != len(entries) for _, item in entries):
            raise Rejected('native_alias', 'Native writable inode has an alias outside the approved writable paths')
        if len(entries) > 1:
            for path, observed in entries:
                current = path.lstat()
                if (current.st_dev, current.st_ino, current.st_nlink) != (observed['device'], observed['inode'], len(entries)):
                    raise Rejected('native_alias', 'Native hardlink identity changed during observation')
    return result


def contract(recipe: dict) -> dict:
    """Stable permission inputs that a cached native receipt must match exactly."""
    keys = ('source', 'outputs', 'read_roots', 'mutable', 'immutable', 'checks', 'max_seconds', 'native_write_roots',
            'native_protected_paths', 'probe_interpreter', 'instruction_manifest',
            'native_sandbox', 'native_process_ownership', 'execution_policy', 'checkout_policy')
    return {key: recipe[key] for key in keys if key in recipe}


def reconcile(recipe: dict, before: dict, *, check_deadline=None) -> dict:
    after = snapshot(recipe, check_deadline=check_deadline)
    if set(before) != set(after) or any(before[p]['identity'] != after[p]['identity'] for p in before):
        raise Rejected('native_effects', 'Native write root identity changed; ownership remains held')
    changes = {p: sorted(k for k in before[p]['entries'].keys() | after[p]['entries'].keys()
                        if before[p]['entries'].get(k) != after[p]['entries'].get(k)) for p in before}
    return {'observed': True, 'attribution': 'filesystem observations, not exclusive external-writer attribution',
            'changes': changes, 'after': after}


def verify_identities(recipe: dict, observed: dict) -> None:
    current = {entry['path']: identity(Path(entry['path']).parent if entry['kind'] == 'cache-file' else Path(entry['path']))
               for entry in validate(recipe)}
    if set(current) != set(observed) or any(current[p] != observed[p]['identity'] for p in current):
        raise Rejected('native_effects', 'Native write root identity changed after cleanup')


def reject_conflicts(recipe: dict, active_rows: list[dict]) -> None:
    """Call in the journal's admission transaction; an unknown owner still holds."""
    writes = writable_roots(recipe)
    source_outputs = (Path(recipe['source']), Path(recipe['outputs']))
    for row in active_rows:
        other = row.get('native_path_recipe')
        other_writes = writable_roots(other) if other else ()
        if not writes and not other_writes:
            continue
        if not row.get('source') or not row.get('outputs_root'):
            raise Rejected('native_ownership', 'An unresolved native check lacks complete path ownership')
        other_roots = (Path(row['source']), Path(row['outputs_root']), *other_writes)
        if (any(overlaps(root, occupied) for root in writes for occupied in other_roots) or
                any(overlaps(root, occupied) for root in other_writes for occupied in source_outputs)):
            raise Rejected('native_ownership', 'A live or unresolved native check owns an overlapping path')
