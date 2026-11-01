"""Pin the pinned runtime's instruction discovery; never grant a directory tree.

Only instruction files are permission exceptions. Config files are hashed for
freshness, never exposed to a worker. Explicit supporting policy files remain
part of the recipe that must be approved before dispatch.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from pathlib import Path

from .store import Rejected

DEFAULTS = {'project_root_markers': ['.git'], 'project_doc_fallback_filenames': [],
            'project_doc_max_bytes': 32768}


def file_record(path: Path) -> dict:
    path = Path(os.path.abspath(path))
    try:
        pending, current, aliases = list(path.parts[1:]), Path(path.anchor), []
        while pending:
            current /= pending.pop(0)
            if current.is_symlink():
                target = os.readlink(current)
                aliases.append({'path': str(current), 'target': target})
                if len(aliases) > 40:
                    raise OSError('Instruction alias loop')
                replacement = Path(target) if Path(target).is_absolute() else current.parent / target
                replacement = Path(os.path.abspath(replacement))
                pending = list(replacement.parts[1:]) + pending
                current = Path(replacement.anchor)
        if not stat.S_ISREG(current.stat().st_mode):
            raise OSError('Instruction is not a regular file')
        data = current.read_bytes()
        data.decode('utf-8')
    except (OSError, UnicodeError, RuntimeError) as error:
        raise Rejected('instruction_unavailable', f'Required instruction cannot be read: {path}') from error
    return {'path': str(path), 'resolved': str(current), 'aliases': aliases,
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def discover(source: Path, *, support_files=(), codex_home: Path | None = None) -> dict:
    """Inspection only. Global, then closest repository root through cwd.

    codex_home is injectable for discovery tests; dispatch always rediscovers
    using the actual account home used by the sanitized runtime environment.
    """
    source = source.resolve(strict=True)
    home = codex_home or Path.home() / '.codex'
    settings = {key: list(value) if isinstance(value, list) else value for key, value in DEFAULTS.items()}
    configs = []
    ancestors = list(reversed([source, *source.parents]))
    for path in dict.fromkeys([home / 'config.toml', *[p / '.codex/config.toml' for p in ancestors]]):
        if path.exists() or path.is_symlink():
            record = file_record(path)
            config = tomllib.loads(path.read_text())
            # Instructions supplied by config are not replaced or silently lost.
            # This narrow route requires their explicit treatment first.
            if any(config.get(k) for k in ('instructions', 'developer_instructions', 'model_instructions_file')):
                raise Rejected('instruction_config', 'Additional configured instructions require an explicit qualified route')
            settings.update({k: config[k] for k in DEFAULTS if k in config})
            configs.append(record)
    for key in ('project_root_markers', 'project_doc_fallback_filenames'):
        names = settings[key]
        if not isinstance(names, list) or any(not isinstance(n, str) or not n or Path(n).name != n or n in ('.', '..') for n in names):
            raise Rejected('instruction_config', 'Instruction discovery requires simple ordered filenames')
    if type(settings['project_doc_max_bytes']) is not int or settings['project_doc_max_bytes'] <= 0:
        raise Rejected('instruction_config', 'Instruction byte limit must be positive')
    root = next((p for p in [source, *source.parents]
                 if any((p / marker).exists() for marker in settings['project_root_markers'])), source)
    directories = [home, *[p for p in ancestors if p == root or p.is_relative_to(root)]]
    chain = []
    for index, directory in enumerate(directories):
        names = ['AGENTS.override.md', 'AGENTS.md'] + (settings['project_doc_fallback_filenames'] if index else [])
        for name in dict.fromkeys(names):
            path = directory / name
            if path.exists() or path.is_symlink():
                record = file_record(path)
                if record['bytes']:
                    chain.append(record)
                    break
    if sum(entry['bytes'] for entry in chain) > settings['project_doc_max_bytes']:
        raise Rejected('instruction_truncation', 'Applicable guidance exceeds the configured byte limit; do not silently truncate')
    return {'version': 1, 'source': str(source), 'codex_home': str(home), 'project_root': str(root),
            'settings': settings, 'configs': configs, 'chain': chain,
            'support': [file_record(Path(p)) for p in sorted(set(map(str, support_files)))]}


def validate(manifest: dict, source: Path, state: Path) -> list[str]:
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise Rejected('instruction_manifest', 'A current instruction manifest must be bound into the approved recipe')
    actual = discover(source, support_files=[r['path'] for r in manifest.get('support', [])])
    if actual != manifest:
        raise Rejected('instruction_drift', 'Instruction chain, aliases, configuration or content changed')
    credentials = Path.home() / '.codex'
    global_names = {str(credentials / name) for name in ('AGENTS.md', 'AGENTS.override.md')}
    paths = []
    for record in actual['chain'] + actual['support']:
        checked = [record['path'], record['resolved']]
        for alias in record['aliases']:
            target = Path(alias['target'])
            checked.extend([alias['path'], os.path.abspath(target if target.is_absolute() else Path(alias['path']).parent / target)])
        for text in checked:
            path = Path(text)
            if (path == state or path.is_relative_to(state)
                    or path.is_relative_to(credentials) and text not in global_names):
                raise Rejected('instruction_scope', 'Instruction alias or support file overlaps protected state or credentials')
        paths.extend([record['path'], record['resolved']])
    return sorted(set(paths))


def verify_settings(manifest: dict, config: dict):
    if any((config.get(key) if config.get(key) is not None else default) != manifest['settings'][key]
           for key, default in DEFAULTS.items()):
        raise Rejected('instruction_config', 'Effective runtime discovery differs from the pinned instruction manifest')
    if any(config.get(k) for k in ('instructions', 'developer_instructions', 'model_instructions_file')):
        raise Rejected('instruction_config', 'Unqualified effective instruction override')
