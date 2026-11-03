"""Observed resource ceilings, with durable work baselines and no cleanup powers.

This is a bounded sampling guard, not a filesystem quota. An observed breach or
unobserved active interval is retained even when later cleanup frees space.
"""
from __future__ import annotations

import os
import hashlib
import json
import math
import re
import sqlite3
import stat
import time
from pathlib import Path

from .store import Rejected, canonical, digest
from . import native_sandbox, native_paths

SCAN_SECONDS = 10
MAX_ENTRIES = 1_000_000
MAX_RECORD_BYTES = 128 * 1024 * 1024
CONTINUATION_FORMAT = 'aos-startup-resource-continuation-v1'
CONTINUATION_LIMITS = ('Historical interval remains unobserved; endpoint observations do not establish '
                       'peak usage or absence of unobserved detached processes.')


def _path(value: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise Rejected('resource_limits', 'Resource roots must be canonical existing directories')
    path = Path(value)
    if (str(path) != value or path.resolve() != path or not path.is_dir()
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise Rejected('resource_limits', 'Resource roots must be canonical existing directories')
    return path


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def scope(recipe: dict, *, protected=()) -> dict | None:
    limits = recipe.get('resource_limits')
    if limits is None:
        return None
    keys = {'output_roots', 'source_evidence_roots', 'max_output_growth_bytes',
            'max_source_evidence_growth_bytes', 'min_free_bytes', 'sample_interval_seconds'}
    if not isinstance(limits, dict) or set(limits) != keys:
        raise Rejected('resource_limits', 'Exact resource ceiling fields required')
    for key in keys - {'output_roots', 'source_evidence_roots'}:
        if type(limits[key]) is not int or limits[key] < 1:
            raise Rejected('resource_limits', 'Resource ceilings must be positive integers')
    if not 1 <= limits['sample_interval_seconds'] <= 10:
        raise Rejected('resource_limits', 'Resource sampling interval must be 1–10 seconds')
    groups = {}
    for key in ('output_roots', 'source_evidence_roots'):
        roots = limits[key]
        if not isinstance(roots, list) or len(roots) > 32:
            raise Rejected('resource_limits', 'Resource groups need bounded explicit root lists')
        paths = [_path(root) for root in roots]
        if any(_overlap(a, b) for i, a in enumerate(paths) for b in paths[i + 1:]):
            raise Rejected('resource_limits', 'Resource roots within a group must not overlap')
        if any(_overlap(path, Path(guard).resolve()) for path in paths for guard in protected):
            raise Rejected('resource_limits', 'Resource observation overlaps protected state or credentials')
        groups[key] = sorted(str(path) for path in paths)
    source, outputs = _path(recipe['source']), _path(recipe['outputs'])
    evidence = [Path(root) for root in groups['source_evidence_roots']]
    if not all(any(path == root or path.is_relative_to(root) for root in evidence) for path in (source, outputs)):
        raise Rejected('resource_limits', 'The source and all attempt evidence must be covered')
    generated = sorted(entry['path'] for entry in recipe.get('native_write_roots', []) if entry['kind'] == 'generated')
    if any(root not in groups['output_roots'] for root in generated):
        raise Rejected('resource_limits', 'Every generated source root must have its own output allowance')
    for out in map(Path, groups['output_roots']):
        for root in evidence:
            if _overlap(out, root) and not (str(out) in generated and out.is_relative_to(root)):
                raise Rejected('resource_limits', 'Resource groups overlap outside declared generated source')
    native = native_sandbox.config(recipe) or {}
    return {**limits, **groups, 'generated_roots': generated, 'source': str(source),
            'temporary_paths': native.get('temporary_paths', []),
            'scan_seconds': SCAN_SECONDS, 'max_observation_gap_seconds': limits['sample_interval_seconds'] + SCAN_SECONDS + 2}


def _identity(path: Path) -> dict:
    _path(str(path))
    info = path.stat()
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}


def _children(path: Path, *, missing=False):
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                yield Path(entry.path)
    except FileNotFoundError:
        if not missing:
            raise


def _temporary_roots(bound: dict) -> dict[str, dict]:
    result, patterns = {}, {}
    for entry in bound.get('temporary_paths', []):
        patterns.setdefault(entry['parent'], []).append(re.compile(native_sandbox._name_regex(entry['name']), re.IGNORECASE))
    started, count = time.monotonic(), 0
    for parent, candidates in patterns.items():
        for path in _children(Path(parent)):
            count += 1
            if count > MAX_ENTRIES or time.monotonic() - started > SCAN_SECONDS:
                raise Rejected('resource_observation', 'Temporary namespace observation exceeded its bounded scan')
            if not any(pattern.fullmatch(path.name) for pattern in candidates):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            result[str(path)] = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}
    return result


def observe(bound: dict) -> dict:
    """No link traversal, deletion credits or claims about global-cache writers."""
    started = time.monotonic()
    count = 0
    identities, available, groups = {}, {}, {}
    ignored_temporary = bound.get('_temporary_baseline', {})
    for group in ('output_roots', 'source_evidence_roots'):
        files = {}
        excluded = set(bound['generated_roots']) if group == 'source_evidence_roots' else set()
        for name in bound[group]:
            root = Path(name)
            before = _identity(root)
            identities[name] = before
            usage = os.statvfs(root)
            device = str(before['device'])
            free = usage.f_bavail * usage.f_frsize
            available[device] = min(available.get(device, free), free)
            pending = [root]
            while pending:
                directory = pending.pop()
                if directory.is_symlink() or directory.resolve() != directory:
                    raise Rejected('resource_observation', 'Resource directory changed to an alias')
                for entry in _children(directory, missing=directory != root):
                    count += 1
                    if count > MAX_ENTRIES or time.monotonic() - started > SCAN_SECONDS:
                        raise Rejected('resource_observation', 'Resource observation exceeded its bounded scan')
                    if str(entry) in excluded:
                        continue
                    try:
                        info = entry.lstat()
                    except FileNotFoundError:
                        continue  # A transient removal contributes no current growth.
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry)
                    else:
                        # Link bytes are counted, but their targets are not visited.
                        files[str(entry)] = max(0, info.st_size)
            if _identity(root) != before:
                raise Rejected('resource_observation', 'Resource root identity changed during observation')
            usage = os.statvfs(root)
            available[device] = min(available[device], usage.f_bavail * usage.f_frsize)
        groups[group] = files
    # Normal native temporary namespaces are part of output growth too. Existing
    # denied entries retain their exact identity and are not traversed. New
    # matches are counted conservatively, without attributing their writer.
    temporary = _temporary_roots(bound)
    for parent in sorted({entry['parent'] for entry in bound.get('temporary_paths', [])}):
        path = Path(parent)
        identities[parent] = _identity(path)
        usage = os.statvfs(path)
        device, free = str(identities[parent]['device']), usage.f_bavail * usage.f_frsize
        available[device] = min(available.get(device, free), free)
    pending = [Path(name) for name, identity in temporary.items() if ignored_temporary.get(name) != identity]
    while pending:
        path = pending.pop()
        count += 1
        if count > MAX_ENTRIES or time.monotonic() - started > SCAN_SECONDS:
            raise Rejected('resource_observation', 'Resource observation exceeded its bounded scan')
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if path.resolve() != path:
                    raise Rejected('resource_observation', 'Temporary resource directory changed to an alias')
                for child in _children(path, missing=True):
                    if count + len(pending) >= MAX_ENTRIES or time.monotonic() - started > SCAN_SECONDS:
                        raise Rejected('resource_observation', 'Temporary resource observation exceeded its bounded scan')
                    pending.append(child)
            else:
                groups['output_roots'][str(path)] = max(0, info.st_size)
        except FileNotFoundError:
            continue
    for parent in {entry['parent'] for entry in bound.get('temporary_paths', [])}:
        if _identity(Path(parent)) != identities[parent]:
            raise Rejected('resource_observation', 'Native temporary parent identity changed')
    for name, identity in identities.items():
        usage = os.statvfs(name)
        device = str(identity['device'])
        available[device] = min(available[device], usage.f_bavail * usage.f_frsize)
    if time.monotonic() - started > SCAN_SECONDS:
        raise Rejected('resource_observation', 'Resource observation exceeded its bounded scan')
    return {'identities': identities, 'files': groups, 'available_bytes': available, 'observed_at': time.time()}


def initialize(db) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS resource_limit (work_id TEXT PRIMARY KEY, record TEXT NOT NULL)')


def _read(db, work_id: str) -> dict | None:
    return _snapshot(db, work_id)[1]


def _bounded_record(raw):
    if not isinstance(raw, str) or len(raw) > MAX_RECORD_BYTES or len(raw.encode('utf-8')) > MAX_RECORD_BYTES:
        raise Rejected('resource_observation', 'Resource guard record exceeded its bounded size')
    return raw


def _snapshot(db, work_id):
    row = db.execute('SELECT record FROM resource_limit WHERE work_id=?', (work_id,)).fetchone()
    raw = _bounded_record(row[0]) if row else None
    return raw, json.loads(raw) if raw is not None else None


def _save(db, work_id: str, record: dict) -> None:
    db.execute('INSERT INTO resource_limit VALUES(?,?) ON CONFLICT(work_id) DO UPDATE SET record=excluded.record',
               (work_id, _bounded_record(canonical(record))))


def _cas_save(db, work_id, expected, encoded):
    return db.execute('UPDATE resource_limit SET record=? WHERE work_id=? AND record=?',
                      (encoded, work_id, expected)).rowcount == 1


def _failure_path(database, work_id):
    return Path(database).parent / ('.resource-observation-failure-' + digest(work_id)[7:39] + '.json')


def _read_failure(db, work_id, bound):
    database = db.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        return None
    path = _failure_path(database, work_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise Rejected('resource_observation', 'Private observation-failure evidence changed')
        failure = json.loads(stream.read(4097))
    if not isinstance(failure, dict) or set(failure) != {'work_id', 'scope_digest', 'breach', 'high_water_growth_bytes'}:
        raise Rejected('resource_observation', 'Private observation-failure evidence is malformed')
    if failure['work_id'] != work_id or failure['scope_digest'] != digest(bound):
        raise Rejected('resource_observation', 'Observation-failure evidence names another resource scope')
    breach, growth = failure.get('breach'), failure.get('high_water_growth_bytes')
    if (not isinstance(breach, dict) or not isinstance(breach.get('reason'), str) or not breach['reason']
            or type(breach.get('observed_at')) not in (int, float) or not math.isfinite(breach['observed_at'])
            or not isinstance(growth, dict) or set(growth) != {'output_roots', 'source_evidence_roots'}
            or any(type(value) is not int or value < 0 for value in growth.values())
            or ('error_code' in breach and (not isinstance(breach['error_code'], str)
                                           or not 1 <= len(breach['error_code']) <= 64))
            or ('error_message' in breach and (not isinstance(breach['error_message'], str)
                                              or len(breach['error_message']) > 500))
            or ('errno' in breach and type(breach['errno']) is not int)):
        raise Rejected('resource_observation', 'Private observation-failure evidence is malformed')
    return failure


def _retain_failure(database, work_id, bound, pending):
    """Retain the first stop and its observed lower bounds while SQLite is locked.

    Later observers cannot clear this permanent failure. Their larger concurrent
    snapshots are not claimed by this first-failure fallback receipt.
    """
    failure = {'work_id': work_id, 'scope_digest': digest(bound),
               'breach': (pending or {}).get('breach') or {'reason': 'resource_observation_unavailable', 'observed_at': time.time()},
               'high_water_growth_bytes': (pending or {}).get('high_water_growth_bytes', {group: 0 for group in
                                                                                       ('output_roots', 'source_evidence_roots')})}
    try:
        fd = os.open(_failure_path(database, work_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(failure))
        stream.flush()
        os.fsync(stream.fileno())


def recorded_scope(db, work_id: str, recipe: dict) -> dict | None:
    """Bind the unchanged contract without inspecting possibly damaged roots.

    Ongoing filesystem errors must reach sample(), where they become durable
    failures; validating root existence here would lose that observation.
    """
    if recipe.get('resource_limits') is None:
        _without_limits(db, work_id, recipe)
        return None
    record = _read(db, work_id)
    if record is None:
        raise Rejected('resource_limits', 'Original work resource baseline is missing')
    _continuation_binding(record, recipe)
    bound = record['scope']
    current = dict(recipe['resource_limits'])
    for key in ('output_roots', 'source_evidence_roots'):
        current[key] = sorted(current[key])
    expected = {key: value for key, value in bound.items()
                if key not in ('source', 'generated_roots', 'temporary_paths', 'scan_seconds', 'max_observation_gap_seconds')}
    generated = sorted(entry['path'] for entry in recipe.get('native_write_roots', []) if entry['kind'] == 'generated')
    if (current != expected or generated != bound['generated_roots'] or recipe['source'] != bound['source']
            or recipe.get('native_sandbox', {}).get('temporary_paths', []) != bound['temporary_paths']):
        raise Rejected('resource_limits', 'The original resource contract changed')
    return bound


def prepare(db, work_id: str, recipe: dict) -> dict | None:
    bound = scope(recipe)
    if bound is None:
        _without_limits(db, work_id, recipe)
        return None
    prior = _read(db, work_id)
    if prior is not None:
        _continuation_binding(prior, recipe)
        if (prior['scope'] != bound
                or prior.get('carry_forward_reference') != recipe.get('resource_limits_carry_forward')):
            raise Rejected('resource_limits', 'The original work resource baseline and limits cannot be reset')
        enforce(prior)
        return prior
    if recipe.get('resource_observation_continuation') is not None:
        raise Rejected('resource_limits', 'A continuation requires its already installed historical guard')
    carried = _carry_forward(recipe, bound)
    if carried and carried.get('target_work_id') != work_id:
        raise Rejected('resource_limits', 'Carry-forward evidence belongs to another exact work item')
    if carried:
        baseline = carried['baseline']
    else:
        temporary = _temporary_roots(bound)
        baseline = observe({**bound, '_temporary_baseline': temporary})
        baseline['temporary_baseline'] = temporary
    record = _initial_record(bound, baseline)
    if carried:
        record['carry_forward_reference'] = recipe['resource_limits_carry_forward']
        record['carry_forward'] = {key: carried[key] for key in ('qualification_id', 'charged_growth_bytes',
                                                                'original_ceilings', 'capsule_digest')}
    _save(db, work_id, record)
    # Import the saved export baseline, never today's files. This first sample
    # accounts for growth during a later formal decision or process restart.
    return sample(db, work_id, bound, 'carry-forward', force=True) if carried else record


def _without_limits(db, work_id, recipe):
    if recipe.get('resource_observation_continuation') is not None:
        raise Rejected('resource_limits', 'A continuation cannot remove the original resource limits')
    if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='resource_limit'").fetchone():
        saved = _read(db, work_id)
        if saved and saved.get('continuation') is not None:
            raise Rejected('resource_limits', 'An installed continuation cannot omit its resource contract')


def _continuation_binding(record, recipe):
    """The old recipe cannot silently use a separately approved continuation."""
    marker, reference = record.get('continuation'), recipe.get('resource_observation_continuation')
    if marker is None and reference is None:
        return
    keys = {'operation_id', 'original_guard_sha256', 'evidence_sha256', 'unobserved_interval', 'acknowledged_limits'}
    if (not isinstance(marker, dict) or set(marker) != {'format', 'reference', 'decision_id', 'concept_digest', 'historical_breach'}
            or marker['format'] != CONTINUATION_FORMAT or not isinstance(reference, dict) or set(reference) != keys
            or marker['reference'] != reference or reference['acknowledged_limits'] != CONTINUATION_LIMITS
            or not isinstance(reference['operation_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', reference['operation_id'])
            or any(not isinstance(reference[key], str) or not re.fullmatch('[a-f0-9]{64}', reference[key])
                   for key in ('original_guard_sha256', 'evidence_sha256'))
            or not isinstance(marker['decision_id'], str) or not marker['decision_id']
            or not isinstance(marker['concept_digest'], str) or not re.fullmatch('sha256:[a-f0-9]{64}', marker['concept_digest'])
            or not isinstance(marker['historical_breach'], dict) or marker['historical_breach'].get('reason') != 'observation_failed'):
        raise Rejected('resource_limits', 'The exact newly approved continuation reference is required')
    interval = reference['unobserved_interval']
    if (not isinstance(interval, dict) or set(interval) != {'start', 'end'}
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in interval.values())
            or interval['start'] > interval['end']):
        raise Rejected('resource_limits', 'The acknowledged historical interval is malformed')


def _initial_record(bound, baseline):
    if min(baseline['available_bytes'].values(), default=0) < bound['min_free_bytes']:
        raise Rejected('resource_limits', 'Free disk space is below the approved reserve before execution')
    return {'scope': bound, 'scope_digest': digest(bound), 'baseline': baseline, 'last_observation': baseline,
            'high_water_growth_bytes': {'output_roots': 0, 'source_evidence_roots': 0},
            'active_attempt': None, 'owner_generation': 0, 'breach': None}


def _carry_forward(recipe: dict, bound: dict) -> dict | None:
    reference = recipe.get('resource_limits_carry_forward')
    if reference is None:
        return None
    if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or not isinstance(reference['path'], str)):
        raise Rejected('resource_limits', 'An exact carry-forward capsule path and hash are required')
    path = Path(reference['path'])
    writable = [Path(root) for key in ('output_roots', 'source_evidence_roots') for root in bound[key]]
    writable += [Path(entry['path']) for entry in recipe.get('native_write_roots', [])]
    writable += [Path(entry['parent']) for entry in bound.get('temporary_paths', [])]
    if (not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file() or str(path) not in recipe.get('native_protected_paths', [])
            or any(native_paths.overlaps(path, root) for root in writable)):
        raise Rejected('resource_limits', 'The carry-forward capsule must be outside and protected from writable roots')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 128 * 1024 * 1024:
        raise Rejected('resource_limits', 'The carry-forward capsule must be one private owned file')
    raw = path.read_bytes()
    if len(raw) > 128 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise Rejected('resource_limits', 'The exact carry-forward capsule changed')
    try:
        capsule = json.loads(raw)
        if (capsule['format'] != 'aos-technical-resource-carry-forward-v1' or capsule['accepted'] is not False
                or capsule['scope'] != bound or not capsule['all_phases_stopped']
                or capsule['breach'] is not None):
            raise ValueError('Invalid technical capsule')
        for group, key in (('output_roots', 'max_output_growth_bytes'),
                           ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
            charge, ceiling = capsule['charged_growth_bytes'][group], capsule['original_ceilings'][key]
            if type(charge) is not int or charge < 0 or type(ceiling) is not int or ceiling - charge != bound[key]:
                raise ValueError('Invalid remaining allowance')
        baseline = capsule['baseline']
        if (set(baseline['files']) != {'output_roots', 'source_evidence_roots'}
                or any(type(size) is not int or size < 0 for files in baseline['files'].values() for size in files.values())):
            raise ValueError('Invalid carried baseline')
    except (KeyError, TypeError, ValueError) as error:
        raise Rejected('resource_limits', 'Malformed carry-forward resource evidence') from error
    return {**capsule, 'capsule_digest': reference['sha256']}


def _owner_generation(record):
    generation = record.get('owner_generation', 0)
    if type(generation) is not int or generation < 0:
        raise Rejected('resource_observation', 'Resource observer ownership generation is malformed')
    return generation


def _validate_sample(record, bound, attempt_id):
    if record is None or record['scope'] != bound:
        raise Rejected('resource_limits', 'Original work resource baseline is missing or different')
    if record['active_attempt'] not in (None, attempt_id):
        raise Rejected('resource_limits', 'Another unresolved attempt owns resource observation')
    _owner_generation(record)


def _sample_record(record, bound, attempt_id, retained, *, force=False, activate=False, finish=False):
    """Observe and reduce a detached guard value; no database work occurs here."""
    _validate_sample(record, bound, attempt_id)
    record['owner_generation'] = _owner_generation(record)
    if retained:
        record['breach'] = record['breach'] or retained['breach']
        for group in record['high_water_growth_bytes']:
            record['high_water_growth_bytes'][group] = max(record['high_water_growth_bytes'][group],
                                                          retained['high_water_growth_bytes'][group])
    active = record['active_attempt'] is not None
    elapsed = time.time() - record['last_observation']['observed_at']
    if active and (elapsed < 0 or elapsed > bound['max_observation_gap_seconds']):
        record['breach'] = record['breach'] or {'reason': 'active_observation_gap', 'observed_at': time.time()}
    if activate and record['active_attempt'] != attempt_id:
        record['active_attempt'] = attempt_id
        record['owner_generation'] += 1
    if force or activate or finish or elapsed >= bound['sample_interval_seconds']:
        try:
            observed = observe({**bound, '_temporary_baseline': record['baseline'].get('temporary_baseline', {})})
            completed_gap = observed['observed_at'] - record['last_observation']['observed_at']
            if active and (completed_gap < 0 or completed_gap > bound['max_observation_gap_seconds']):
                record['breach'] = record['breach'] or {'reason': 'active_observation_gap', 'observed_at': observed['observed_at']}
            if observed['identities'] != record['baseline']['identities']:
                raise Rejected('resource_observation', 'Resource root identity differs from the work baseline')
            for group, cap in (('output_roots', 'max_output_growth_bytes'),
                               ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
                before = record['baseline']['files'][group]
                growth = sum(max(0, size - before.get(name, 0)) for name, size in observed['files'][group].items())
                record['high_water_growth_bytes'][group] = max(record['high_water_growth_bytes'][group], growth)
                if growth > bound[cap]:
                    record['breach'] = record['breach'] or {'reason': cap, 'observed_bytes': growth,
                                                           'limit_bytes': bound[cap], 'observed_at': observed['observed_at']}
            free = min(observed['available_bytes'].values(), default=0)
            if free < bound['min_free_bytes']:
                record['breach'] = record['breach'] or {'reason': 'min_free_bytes', 'observed_bytes': free,
                                                       'limit_bytes': bound['min_free_bytes'],
                                                       'available_bytes': observed['available_bytes'],
                                                       'observed_at': observed['observed_at']}
            record['last_observation'] = observed
        except (OSError, ValueError) as error:
            failure = {'reason': 'observation_failed', 'error': type(error).__name__, 'observed_at': time.time()}
            if isinstance(error, Rejected) and isinstance(error.code, str) and error.code:
                message = str(error)
                prefix = error.code + ': '
                if message.startswith(prefix):
                    message = message[len(prefix):]
                failure.update(error_code=error.code[:64], error_message=message[:500])
            elif isinstance(error, OSError) and type(error.errno) is int:
                failure['errno'] = error.errno
            record['breach'] = record['breach'] or failure
    if finish and record['active_attempt'] is not None:
        record['active_attempt'] = None
        record['owner_generation'] += 1
    changed = bool(force or activate or finish or retained
                   or elapsed >= bound['sample_interval_seconds'] or elapsed < 0)
    return record, changed


def _origin(record):
    return {key: record.get(key) for key in ('scope', 'scope_digest', 'baseline', 'active_attempt',
                                            'carry_forward_reference', 'carry_forward')} | {
        'owner_generation': _owner_generation(record), 'continuation': record.get('continuation')}


def _stage_sample(db, work_id, bound, attempt_id, *, force=False, activate=False, finish=False):
    if db.in_transaction:
        raise Rejected('resource_observation', 'Detached sampling requires an autocommit connection')
    raw, record = _snapshot(db, work_id)
    _validate_sample(record, bound, attempt_id)
    origin, known_at = _origin(record), record['last_observation']['observed_at']
    retained = _read_failure(db, work_id, bound)
    pending, changed = _sample_record(record, bound, attempt_id, retained,
                                      force=force, activate=activate, finish=finish)
    try:
        encoded = _bounded_record(canonical(pending)) if changed else None
    except Exception as error:
        error.resource_pending = pending
        raise
    return {'origin': origin, 'known_at': known_at, 'expected': raw, 'pending': pending,
            'changed': changed, 'encoded': encoded}


def _rebase_sample(db, work_id, bound, attempt_id, staged):
    """Merge a completed observation after a peer CAS, without observing again."""
    if db.in_transaction:
        raise Rejected('resource_observation', 'Resource observation rebase cannot hold a writer transaction')
    raw, current = _snapshot(db, work_id)
    _validate_sample(current, bound, attempt_id)
    if _origin(current) != staged['origin']:
        raise Rejected('resource_observation', 'Original resource baseline, scope or observer ownership changed')
    # Only the peer's already committed observation refreshes coverage. The
    # caller's original monotonic and execution deadlines remain unchanged.
    committed_at = current['last_observation']['observed_at']
    pending = staged['pending']
    current['breach'] = current['breach'] or pending['breach']
    for group, growth in pending['high_water_growth_bytes'].items():
        current['high_water_growth_bytes'][group] = max(current['high_water_growth_bytes'][group], growth)
    if pending['last_observation']['observed_at'] > current['last_observation']['observed_at']:
        current['last_observation'] = pending['last_observation']
    retained = _read_failure(db, work_id, bound)
    if retained:
        current['breach'] = current['breach'] or retained['breach']
        for group, growth in retained['high_water_growth_bytes'].items():
            current['high_water_growth_bytes'][group] = max(current['high_water_growth_bytes'][group], growth)
    current['active_attempt'] = pending['active_attempt']
    current['owner_generation'] = pending['owner_generation']
    staged.update(known_at=max(staged['known_at'], committed_at), expected=raw,
                  pending=current, encoded=_bounded_record(canonical(current)))


def _persist_sample(db, work_id, bound, staged):
    if not staged['changed']:
        return True
    if db.in_transaction:
        raise Rejected('resource_observation', 'Detached resource persistence must own its short transaction')
    # Encoding and any late gap diagnosis happen before taking the writer lock.
    age = time.time() - staged['known_at']
    pending = staged['pending']
    if (staged['origin']['active_attempt'] is not None
            and (age < 0 or age > bound['max_observation_gap_seconds']) and not pending['breach']):
        pending['breach'] = {'reason': 'active_observation_gap', 'observed_at': time.time()}
        staged['encoded'] = _bounded_record(canonical(pending))
    try:
        db.execute('BEGIN IMMEDIATE')
        matched = _cas_save(db, work_id, staged['expected'], staged['encoded'])
    except sqlite3.OperationalError as error:
        error.resource_pending = pending
        raise
    if not matched:
        db.rollback()
    return matched


def _retry_remaining(started, bound, known_at, *, deadline=None):
    wall, monotonic = time.time(), time.monotonic()
    if type(known_at) not in (int, float) or not math.isfinite(known_at) or wall < known_at:
        return 0
    remaining = min(started + bound['max_observation_gap_seconds'] - monotonic,
                    bound['max_observation_gap_seconds'] - (wall - known_at))
    return min(remaining, deadline - wall) if deadline is not None else remaining


def _retain_staged_failure(db, work_id, bound, staged):
    database = db.execute('PRAGMA database_list').fetchone()[2]
    if database:
        _retain_failure(database, work_id, bound, staged['pending'] if staged else None)


def sample(db, work_id: str, bound: dict, attempt_id: str, *, force=False, activate=False, finish=False) -> dict:
    """Leave the returned update uncommitted; never release a caller transaction.

    Normal autocommit callers observe and encode before a short compare-and-swap
    writer transaction. Admission/accounting callers already inside a transaction
    retain their existing atomic semantics and must keep active scans outside it.
    """
    if db.in_transaction:
        record = _read(db, work_id)
        pending, changed = _sample_record(record, bound, attempt_id, _read_failure(db, work_id, bound),
                                          force=force, activate=activate, finish=finish)
        if changed:
            try:
                _save(db, work_id, pending)
            except Exception as error:
                error.resource_pending = pending
                _retain_staged_failure(db, work_id, bound, {'pending': pending})
                raise
        return pending
    started = time.monotonic()
    staged = None
    try:
        staged = _stage_sample(db, work_id, bound, attempt_id, force=force, activate=activate, finish=finish)
        while True:
            if _persist_sample(db, work_id, bound, staged):
                return staged['pending']
            if _retry_remaining(started, bound, staged['known_at']) <= 0:
                raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window')
            _rebase_sample(db, work_id, bound, attempt_id, staged)
            if _retry_remaining(started, bound, staged['known_at']) <= 0:
                raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window')
    except Exception as error:
        pending = getattr(error, 'resource_pending', None)
        if staged is not None or pending is not None:
            _retain_staged_failure(db, work_id, bound, staged or {'pending': pending})
        raise


def enforce(record: dict | None) -> None:
    if record and record['breach']:
        raise Rejected('resource_limit_exceeded', 'Retained resource guard violation: ' + record['breach']['reason'])


def receipt(record: dict) -> dict:
    result = {key: record[key] for key in ('scope_digest', 'high_water_growth_bytes', 'active_attempt', 'breach')} | {
        'available_bytes': record['last_observation']['available_bytes'],
        'observed_at': record['last_observation']['observed_at'],
        'sample_interval_seconds': record['scope']['sample_interval_seconds'],
        'limits': 'Bounded stop-on-observed-breach sampling; no hard quota or exclusive global-cache attribution'}
    if record.get('continuation') is not None:
        result['historical_observation_continuation'] = record['continuation']
    return result


def sample_file(database: str, work_id: str, bound: dict, attempt_id: str, *, force=False,
                activate=False, finish=False, deadline=None, last_observed_at=None) -> dict:
    """Observe without a writer lock, then durably merge through one short CAS."""
    started = time.monotonic()
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise Rejected('resource_observation', 'Invalid original execution deadline')
    known_at, staged, persisted, rebase = last_observed_at, None, False, False
    unavailable, retrying = False, False
    db = sqlite3.connect(database, timeout=0)
    try:
        while True:
            try:
                if retrying and _retry_remaining(started, bound, known_at, deadline=deadline) <= 0:
                    unavailable = True
                    raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window')
                if staged is None:
                    staged = _stage_sample(db, work_id, bound, attempt_id,
                                           force=force, activate=activate, finish=finish)
                    known_at = staged['known_at']
                elif rebase:
                    _rebase_sample(db, work_id, bound, attempt_id, staged)
                    known_at = staged['known_at']
                    rebase = False
                if retrying and _retry_remaining(started, bound, known_at, deadline=deadline) <= 0:
                    raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window')
                if not persisted:
                    if not _persist_sample(db, work_id, bound, staged):
                        rebase = retrying = True
                        if _retry_remaining(started, bound, known_at, deadline=deadline) <= 0:
                            raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window')
                        continue
                    persisted = True
                db.commit()
                return receipt(staged['pending'])
            except sqlite3.OperationalError as error:
                code = getattr(error, 'sqlite_errorcode', None)
                if code is None or code & 0xff not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                retrying = True
                # An unsuccessful UPDATE can safely roll back: its detached
                # observed value survives. A busy COMMIT retries the same SQL
                # transaction, retaining the completed observation and breach.
                if not persisted:
                    db.rollback()
                elif staged['changed'] and not db.in_transaction:
                    unavailable = True
                    raise Rejected('resource_observation', 'An observed resource result lost its transaction') from error
                remaining = _retry_remaining(started, bound, known_at, deadline=deadline)
                if remaining <= 0:
                    unavailable = True
                    raise Rejected('resource_observation', 'Resource journal remained unavailable within the original observation window') from error
                time.sleep(min(.025, remaining))
    except Exception as error:
        pending = staged['pending'] if staged else getattr(error, 'resource_pending', None)
        if pending is not None or unavailable:
            _retain_failure(database, work_id, bound, pending)
        raise
    finally:
        db.close()
