"""Pin the pinned runtime's instruction discovery; never grant a directory tree.

Only instruction files are permission exceptions. Config files are hashed for
freshness, never exposed to a worker. Explicit supporting policy files remain
part of the recipe that must be approved before dispatch.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import stat
import tomllib
from pathlib import Path

from .store import Rejected

DEFAULTS = {'project_root_markers': ['.git'], 'project_doc_fallback_filenames': [],
            'project_doc_max_bytes': 32768}
MAX_RUNTIME_DOC_BYTES = 1024 * 1024


def _runtime_limit(manifest: dict) -> int | None:
    if 'runtime_overrides' not in manifest:
        return None
    overrides = manifest['runtime_overrides']
    settings = manifest.get('settings')
    if (not isinstance(overrides, dict) or set(overrides) != {'project_doc_max_bytes'}
            or type(overrides['project_doc_max_bytes']) is not int
            or not 0 < overrides['project_doc_max_bytes'] <= MAX_RUNTIME_DOC_BYTES
            or not isinstance(settings, dict) or type(settings.get('project_doc_max_bytes')) is not int
            or settings['project_doc_max_bytes'] != overrides['project_doc_max_bytes']):
        raise Rejected('instruction_limit', 'Only an exact bounded instruction byte-limit override is supported')
    return overrides['project_doc_max_bytes']


def runtime_overrides(manifest: dict | None) -> list[str]:
    """Emit only the recipe-bound byte limit; never inherit arbitrary overrides."""
    value = _runtime_limit(manifest) if isinstance(manifest, dict) else None
    return [f'project_doc_max_bytes={value}'] if value is not None else []


def configuration_digest(config: dict) -> str:
    """Pin all parsed settings, including unknown fields; never just instructions."""
    try:
        encoded = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise Rejected('instruction_config', 'Configuration contains unsupported values for a registration allowance') from error
    return hashlib.sha256(encoded).hexdigest()


def read_configuration(path: Path, record: dict) -> dict:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise Rejected('instruction_drift', 'Configuration changed while being inspected')
    return tomllib.loads(data.decode())


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


def discover(source: Path, *, support_files=(), codex_home: Path | None = None,
             allow_project_registration: bool = False, project_doc_max_bytes: int | None = None) -> dict:
    """Inspection only. Global, then closest repository root through cwd.

    codex_home is injectable for discovery tests; dispatch always rediscovers
    using the actual account home used by the sanitized runtime environment.
    """
    source = source.resolve(strict=True)
    home = codex_home or Path.home() / '.codex'
    settings = {key: list(value) if isinstance(value, list) else value for key, value in DEFAULTS.items()}
    configs = []
    global_config = None
    ancestors = list(reversed([source, *source.parents]))
    for path in dict.fromkeys([home / 'config.toml', *[p / '.codex/config.toml' for p in ancestors]]):
        if path.exists() or path.is_symlink():
            record = file_record(path)
            config = read_configuration(path, record)
            if path == home / 'config.toml':
                global_config = config
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
    if project_doc_max_bytes is not None:
        if (type(project_doc_max_bytes) is not int
                or not settings['project_doc_max_bytes'] <= project_doc_max_bytes <= MAX_RUNTIME_DOC_BYTES):
            raise Rejected('instruction_limit', 'The explicit instruction byte limit may only increase the configured limit, up to 1 MiB')
        settings['project_doc_max_bytes'] = project_doc_max_bytes
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
    result = {'version': 1, 'source': str(source), 'codex_home': str(home), 'project_root': str(root),
              'settings': settings, 'configs': configs, 'chain': chain,
              'support': [file_record(Path(p)) for p in sorted(set(map(str, support_files)))]}
    if project_doc_max_bytes is not None:
        result['runtime_overrides'] = {'project_doc_max_bytes': project_doc_max_bytes}
    if allow_project_registration:
        if global_config is None or str(root) in global_config.get('projects', {}):
            raise Rejected('instruction_registration', 'Registration allowance requires an existing global config and an absent project entry')
        result['startup_project_registration'] = {
            'path': str(home / 'config.toml'), 'project_root': str(root),
            'projects_table_present': 'projects' in global_config,
            'configuration_sha256': configuration_digest(global_config)}
    return result


def readback(manifest: dict, source: Path, *, after_startup: bool = False) -> dict:
    """Allow one explicitly bound trust registration only after session ACK.

    Earlier manifests and pre-dispatch validation remain byte-exact. The
    allowance never authorizes changing existing trust or ignoring other config.
    """
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        raise Rejected('instruction_manifest', 'A current instruction manifest must be bound into the approved recipe')
    approved = copy.deepcopy(manifest)
    registration = approved.pop('startup_project_registration', None)
    limit = _runtime_limit(manifest)
    actual = discover(source, support_files=[r['path'] for r in manifest.get('support', [])],
                      project_doc_max_bytes=limit)
    if actual == approved:
        if registration is not None:
            expected = discover(source, support_files=[r['path'] for r in manifest.get('support', [])],
                                allow_project_registration=True,
                                project_doc_max_bytes=limit)['startup_project_registration']
            if registration != expected:
                raise Rejected('instruction_registration', 'Registration allowance differs from the pinned project/configuration')
        return {'manifest': actual, 'configuration_transition': None}
    if after_startup and isinstance(registration, dict):
        path = str(Path(manifest['codex_home']) / 'config.toml')
        root = manifest['project_root']
        if (set(registration) == {'path', 'project_root', 'projects_table_present', 'configuration_sha256'}
                and registration['path'] == path and registration['project_root'] == root
                and type(registration['projects_table_present']) is bool):
            before = next((r for r in approved['configs'] if r['path'] == path), None)
            after = next((r for r in actual['configs'] if r['path'] == path), None)
            if before and after and {k: v for k, v in before.items() if k not in ('sha256', 'bytes')} == {
                    k: v for k, v in after.items() if k not in ('sha256', 'bytes')}:
                config = read_configuration(Path(path), after)
                projects = config.get('projects', {})
                if isinstance(projects, dict) and projects.get(root) == {'trust_level': 'trusted'}:
                    del projects[root]
                    if not registration['projects_table_present'] and not projects:
                        del config['projects']
                    comparable = copy.deepcopy(actual)
                    comparable['configs'][comparable['configs'].index(after)] = before
                    if configuration_digest(config) == registration['configuration_sha256'] and comparable == approved:
                        return {'manifest': actual, 'configuration_transition': {
                            'kind': 'declared_project_registration', 'path': path, 'project_root': root,
                            'before_sha256': before['sha256'], 'after_sha256': after['sha256'],
                            'writer': 'not established'}}
    raise Rejected('instruction_drift', 'Instruction chain, aliases, configuration or content changed')


def validate(manifest: dict, source: Path, state: Path, *, after_startup: bool = False) -> list[str]:
    actual = readback(manifest, source, after_startup=after_startup)['manifest']
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
    _runtime_limit(manifest)
    effective_limit = config.get('project_doc_max_bytes')
    if effective_limit is not None and type(effective_limit) is not int:
        raise Rejected('instruction_config', 'Effective runtime instruction byte limit must be an integer')
    if any((config.get(key) if config.get(key) is not None else default) != manifest['settings'][key]
           for key, default in DEFAULTS.items()):
        raise Rejected('instruction_config', 'Effective runtime discovery differs from the pinned instruction manifest')
    if any(config.get(k) for k in ('instructions', 'developer_instructions', 'model_instructions_file')):
        raise Rejected('instruction_config', 'Unqualified effective instruction override')
