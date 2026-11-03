"""Host-only retirement of a pre-startup-journal attempt after a local reboot.

This records prospective adoption of observed contents. It never reconstructs a
historical stop receipt, changes the runtime/budget journal, or admits new work.
Evidence authorship and authority are trusted-operator obligations, not inferred
from a model answer or from the existence of an arbitrary evidence filename.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .codex_runtime import inventory
from .lifecycle import Lifecycle, stop_conflicts_in
from .store import Rejected, digest, identifier, now


class LegacyRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    attempt_id: str
    runtime_record_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    evidence_path: str
    evidence_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def observe_host() -> dict:
    """Read native boot and stable machine identity; never accept caller boot data."""
    if platform.system() != 'Darwin':
        raise Rejected('legacy_host_unqualified', 'Only the qualified local macOS recovery route is supported')
    try:
        def command(*args):
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        boot = command('/usr/sbin/sysctl', '-n', 'kern.boottime')
        match = re.search(r'sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)', boot)
        uuid = re.search(r'"IOPlatformUUID"\s*=\s*"([A-Fa-f0-9-]+)"',
                         command('/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'))
        session = command('/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid')
        if not match or not uuid or not re.fullmatch(r'[A-Fa-f0-9-]{36}', session):
            raise ValueError('Missing native boot or machine identity')
        return {'host_id': sha256(uuid[1].upper().encode()), 'boot_seconds': int(match[1]),
                'boot_microseconds': int(match[2]), 'boot_session': session.upper()}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Rejected('legacy_host_unavailable', 'Native local reboot evidence is unavailable') from error


def _real_file(path: Path, *, private=False) -> bytes:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise Rejected('legacy_evidence_path', 'Use an absolute real path without aliases')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or (private and stat.S_IMODE(info.st_mode) & 0o077)):
            raise Rejected('legacy_evidence_permissions', 'Evidence must be a private user-owned regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise Rejected('legacy_evidence_changed', 'File changed while reading')
        return raw
    finally:
        os.close(fd)


def _protected(store, reference: dict) -> bytes:
    if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or not isinstance(reference['path'], str) or not isinstance(reference['sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', reference['sha256'])):
        raise Rejected('legacy_evidence_shape', 'Evidence references require exact path and SHA-256')
    path = Path(reference['path'])
    if any(path.resolve().is_relative_to(root.resolve()) for root in store.worker_writable_roots):
        raise Rejected('legacy_evidence_unprotected', 'Recovery evidence must be outside worker-writable roots')
    raw = _real_file(path, private=True)
    if sha256(raw) != reference['sha256']:
        raise Rejected('legacy_evidence_changed', 'Retained evidence bytes differ')
    return raw


@contextmanager
def _worker_fence(store):
    fd = os.open(store.directory / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Rejected('legacy_worker_lock', 'Unsafe worker lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('worker_owned', 'Stop the existing worker before legacy retirement') from error
        yield
    finally:
        os.close(fd)


def _runtime(store, attempt_id: str, expected_sha256: str) -> tuple[str, dict, dict]:
    path = store.directory / 'codex-runtime.sqlite3'
    _real_file(path, private=True)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT record FROM attempt WHERE id=?', (attempt_id,)).fetchone()
        if not row:
            raise Rejected('legacy_attempt_missing', 'Exact retained runtime attempt required')
        if sha256(row[0].encode()) != expected_sha256:
            raise Rejected('legacy_runtime_changed', 'Runtime record changed since review')
        record = json.loads(row[0])
        budget_id = record.get('budget_id')
        if not isinstance(budget_id, str) or not budget_id or type(record.get('budget_seconds')) is not int or record['budget_seconds'] < 1:
            raise Rejected('legacy_budget_missing', 'Exact original charged allowance must be retained')
        budget = db.execute('SELECT record FROM runtime_budget WHERE id=?', (budget_id,)).fetchone()
        if not budget:
            raise Rejected('legacy_budget_missing', 'Original budget record must be retained')
        scopes = [{'scope': r[0], 'budget_id': r[1]} for r in db.execute(
            'SELECT scope,budget_id FROM product_budget_scope WHERE budget_id=? ORDER BY scope', (budget_id,))]
        attempts = [{'attempt_id': r[0], 'record_sha256': sha256(r[1].encode()),
                     'budget_seconds': json.loads(r[1]).get('budget_seconds')} for r in db.execute(
            'SELECT id,record FROM attempt ORDER BY id') if json.loads(r[1]).get('budget_id') == budget_id]
        accounting = {'budget_id': budget_id, 'budget_record': budget[0],
                      'budget_record_sha256': sha256(budget[0].encode()), 'scopes': scopes, 'reservations': attempts}
    return row[0], record, accounting


def observe_quiescence(resources: list[dict], attempt_id: str, assignment_id: str) -> dict:
    """Bounded ordinary-user inspection; no signals, recursive walks or privilege."""
    roots = sorted(r['resource_id'] for r in resources if r['resource_id'].startswith('/'))
    uid, own_pid = os.getuid(), os.getpid()
    try:
        def command(*args, empty_ok=False):
            result = subprocess.run(args, capture_output=True, timeout=20)
            if (result.returncode not in ((0, 1) if empty_ok else (0,)) or result.stderr
                    or len(result.stdout) > 16 * 1024 * 1024
                    or (result.returncode == 1 and result.stdout)):
                raise ValueError('Incomplete process inspection')
            return result.stdout.decode('utf-8', errors='strict')
        opened = command('/usr/sbin/lsof', '-nP', '-a', '-u', str(uid), '-F0pn', empty_ok=True)
        pid = None
        for field in opened.split('\0'):
            field = field.lstrip('\n')
            if field.startswith('p'):
                pid = int(field[1:])
            elif field.startswith('n') and pid != own_pid:
                path = field[1:].removesuffix(' (deleted)')
                if any(path == root or path.startswith(root + '/') for root in roots):
                    raise Rejected('legacy_resource_busy', f'Process {pid} still has a held resource open')
        processes = command('/bin/ps', '-axww', '-o', 'pid=,uid=,comm=')
        for line in processes.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3:
                raise ValueError('Incomplete process row')
            if int(fields[1]) != uid or int(fields[0]) == own_pid:
                continue
            executable = fields[2]
            if any(executable == root or executable.startswith(root + '/') for root in roots):
                raise Rejected('legacy_resource_busy', f'Process {fields[0]} still references the held attempt/resources')
    except Rejected:
        raise
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        raise Rejected('legacy_quiescence_unavailable', 'Complete native current-user resource inspection is required') from error
    return {'uid': uid, 'methods': ['lsof-user-open-files', 'ps-user-executable-paths'],
            'resource_roots': roots, 'matching_processes': [], 'open_resources': []}


def _metadata(resource: dict) -> dict:
    name = resource['resource_id']
    if not name.startswith('/'):
        return {'kind': 'logical'}
    path = Path(name)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Rejected('legacy_resource_alias', 'Resource roots must not be aliases')
    try:
        info = path.stat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise Rejected('legacy_resource_kind', 'A special resource needs separate ownership reconciliation')
    return {'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file', 'device': info.st_dev,
            'inode': info.st_ino, 'uid': info.st_uid, 'mode': stat.S_IMODE(info.st_mode)}


def _snapshot(lifecycle, db, request: dict, host: dict) -> dict:
    store, work_id, aid = lifecycle.store, request['work_id'], request['attempt_id']
    lifecycle._command_guard(db, work_id, request['expected_work_revision'], request['digest'])
    run, contract = lifecycle._run(db, work_id)
    worker = db.execute('SELECT * FROM worker_status WHERE singleton=1').fetchone()
    if worker and worker['state'] != 'stopped':
        raise Rejected('legacy_worker_active', 'Worker must be stopped, not merely stale')
    assignments = [dict(a) for a in db.execute('SELECT * FROM assignment WHERE work_id=?', (work_id,))]
    if (not run['admitted'] or run['execution'] != 'held' or run['control'] != 'blocked_unknown_effects'
            or len(assignments) != 1 or assignments[0]['attempt_id'] != aid
            or assignments[0]['state'] != 'blocked_unknown_effects'
            or db.execute('SELECT 1 FROM lifecycle WHERE admitted=1 AND work_id<>?', (work_id,)).fetchone()):
        raise Rejected('legacy_scope', 'Only one fenced, unresolved legacy assignment can be retired')
    if (stop_conflicts_in(db, work_id) or db.execute(
            "SELECT 1 FROM technical_blocker WHERE work_id=? AND code='stop_receipt_conflict'", (work_id,)).fetchone()
            or db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state='stopped'", (work_id,)).fetchone()):
        raise Rejected('legacy_stop_conflict', 'Existing stop claims/conflicts require ordinary reconciliation')
    raw, record, accounting = _runtime(store, aid, request['runtime_record_sha256'])
    assignment = assignments[0]
    if (record.get('attempt_id') != aid or record.get('phase') != 'thread_request'
            or type(record.get('model_calls_attempted')) is not int or record['model_calls_attempted'] != 0
            or any(k.startswith('startup_') for k in record)
            or any(record.get(k) is not None for k in ('thread_id', 'turn_id', 'server_launch', 'terminal_observation'))
            or not isinstance(record.get('assignment'), dict)
            or any(record['assignment'].get(k) != assignment[k] for k in (
                'assignment_id', 'attempt_id', 'work_id', 'kind', 'concept_revision', 'concept_digest',
                'contract_revision', 'scope', 'executor', 'requested_model', 'requested_effort'))):
        raise Rejected('legacy_record_class', 'Record is outside the no-turn legacy startup class')
    boot = host['boot_seconds'] + host['boot_microseconds'] / 1_000_000
    try:
        stamps = [record['created_at'], assignment['created_at'], assignment['sent_at']]
        dates = [datetime.fromisoformat(s) for s in stamps]
        if any(d.tzinfo is None or d.timestamp() >= boot for d in dates) or boot > datetime.now(timezone.utc).timestamp():
            raise ValueError('Attempt is not from an earlier boot')
    except (TypeError, ValueError, KeyError) as error:
        raise Rejected('legacy_reboot_required', 'Positive local reboot after the attempt is required') from error
    recipe = contract.get('runtime_recipe', {})
    if (not isinstance(recipe, dict) or not all(isinstance(recipe.get(k), str) for k in ('source', 'outputs'))
            or not isinstance(record.get('run_outputs'), str)):
        raise Rejected('legacy_resource_scope', 'Retained source/output recipe required')
    source, outputs = Path(recipe['source']), Path(record['run_outputs'])
    if record.get('recipe_digest') != digest(recipe):
        raise Rejected('legacy_recipe_changed', 'Current resource recipe differs from the retained attempted recipe')
    if (not source.is_absolute() or source.resolve() != source or not outputs.is_absolute()
            or outputs.resolve() != outputs or outputs != Path(recipe['outputs']) / aid):
        raise Rejected('legacy_resource_scope', 'Exact canonical source and attempt outputs required')
    resources = [dict(r) for r in db.execute('SELECT * FROM resource WHERE work_id=? ORDER BY resource_id', (work_id,))]
    if ({r['resource_id'] for r in resources} != set(contract['resources'])
            or len(resources) != len(contract['resources'])
            or any(r['assignment_id'] not in (None, assignment['assignment_id']) for r in resources)
            or str(source) not in contract['resources'] or str(Path(recipe['outputs'])) not in contract['resources']):
        raise Rejected('legacy_resource_scope', 'All and only the exact owned resources must be accounted for')
    roots = [Path(r['resource_id']) for r in resources if r['resource_id'].startswith('/')]
    for other in db.execute('SELECT resource_id FROM resource WHERE work_id<>?', (work_id,)):
        if other[0].startswith('/') and any(Path(other[0]).is_relative_to(root) or root.is_relative_to(Path(other[0])) for root in roots):
            raise Rejected('legacy_resource_conflict', 'Another work retains overlapping resource ownership')
    budget = json.loads(accounting['budget_record'])
    if (not isinstance(budget, dict) or budget.get('budget_id') != accounting['budget_id']
            or budget.get('kind') != 'product' or budget.get('source') != str(source)
            or not isinstance(budget.get('product_id'), str) or not budget['product_id']
            or {s['scope'] for s in accounting['scopes']} != {'source:' + str(source), 'product:' + budget['product_id']}
            or record['budget_seconds'] != recipe.get('max_seconds')):
        raise Rejected('legacy_budget_missing', 'Exact product/source must remain bound to the original charged allowance')
    baseline = record.get('baseline')
    if not isinstance(baseline, dict) or not baseline:
        raise Rejected('legacy_baseline', 'Retained original source inventory required')
    current = inventory(source)
    result = {'runtime_record_sha256': request['runtime_record_sha256'], 'assignment': assignment,
              'contract_digest': digest(contract), 'outbox': [dict(r) for r in db.execute(
                  'SELECT * FROM outbox WHERE assignment_id=?', (assignment['assignment_id'],))], 'budget': accounting,
              'quiescence': observe_quiescence(resources, aid, assignment['assignment_id']), 'source': str(source),
              'source_inventory': current, 'baseline_inventory': baseline,
              'source_changes': {p: {'before': baseline.get(p), 'after': current.get(p)}
                                 for p in sorted(baseline.keys() | current.keys()) if baseline.get(p) != current.get(p)},
              'outputs': str(outputs), 'outputs_inventory': inventory(outputs, allow_empty=True),
              'resources': [{'record': r, 'current': _metadata(r)} for r in resources]}
    return result


def inspect_legacy(lifecycle: Lifecycle, request: dict) -> dict:
    """Read current facts for a separately reviewed packet; never author adoption."""
    with _worker_fence(lifecycle.store), lifecycle.store._connection() as db:
        db.execute('BEGIN')
        host = observe_host()
        return {'host': host, 'snapshot': _snapshot(lifecycle, db, request, host)}


def recover(lifecycle: Lifecycle, request: dict, *, apply: bool = False) -> dict:
    """Preview by default; atomic retirement requires exact independently reviewed evidence."""
    LegacyRecoveryRequest.model_validate(request, strict=True)
    store = lifecycle.store
    if store.recovery_hold():
        raise Rejected('recovery_copy_held', 'A restore copy cannot release original resources')
    for key in ('operation_id', 'work_id', 'attempt_id'):
        identifier(request[key])
    payload = ['recover-legacy-cancellation', request]
    # An immutable completed operation is readable even after another worker
    # owns the lane. First application still fences and checks again below.
    with store._connection() as db:
        if prior := store._prior(db, request['operation_id'], payload):
            return prior
    with _worker_fence(store), store._transaction() as db:
        if prior := store._prior(db, request['operation_id'], payload):
            return prior
        evidence_raw = _protected(store, {'path': request['evidence_path'], 'sha256': request['evidence_sha256']})
        evidence = json.loads(evidence_raw)
        if (not isinstance(evidence, dict) or set(evidence) != {'format', 'work_id', 'attempt_id', 'host', 'host_provenance', 'snapshot',
                              'authority', 'adoption', 'review'}
                or evidence['format'] != 'aos-legacy-recovery-v1' or evidence['work_id'] != request['work_id']
                or evidence['attempt_id'] != request['attempt_id']):
            raise Rejected('legacy_evidence_shape', 'Exact legacy recovery evidence identity required')
        host = observe_host()
        if evidence['host'] != host:
            raise Rejected('legacy_host_changed', 'Evidence belongs to another machine or boot')
        for key in ('host_provenance', 'authority'):
            if not _protected(store, evidence[key]).strip():
                raise Rejected('legacy_evidence_shape', 'Authority and local execution provenance must be retained')
        adoption, review = evidence['adoption'], evidence['review']
        if (not isinstance(adoption, dict) or set(adoption) != {'summary', 'delivery_inventory'}
                or not isinstance(adoption['summary'], str) or not adoption['summary'].strip()
                or not isinstance(review, dict) or set(review) != {'author', 'reviewer', 'report'}
                or not all(isinstance(review[k], str) and review[k].strip() for k in ('author', 'reviewer'))
                or review['author'] == review['reviewer']):
            raise Rejected('legacy_review_required', 'Explicit adoption and independent review are required')
        assessment = json.loads(_protected(store, review['report']))
        required_assessment = {'format': 'aos-legacy-current-safety-review-v1', 'work_id': request['work_id'],
                               'attempt_id': request['attempt_id'], 'host': host,
                               'snapshot_digest': digest(evidence['snapshot']), 'author': review['author'],
                               'reviewer': review['reviewer'], 'verdict': 'pass',
                               'historical_startup_outcome': 'unknown', 'current_content_adoption': True,
                               'local_execution_provenance_verified': True}
        if (not isinstance(assessment, dict) or set(assessment) != {*required_assessment, 'reason'}
                or any(assessment[k] != value for k, value in required_assessment.items())
                or assessment['current_content_adoption'] is not True
                or assessment['local_execution_provenance_verified'] is not True
                or not isinstance(assessment['reason'], str) or not assessment['reason'].strip()):
            raise Rejected('legacy_review_required', 'Passing independent current-safety review must bind this exact host, attempt and snapshot')
        delivery = json.loads(_protected(store, adoption['delivery_inventory']))
        snapshot = _snapshot(lifecycle, db, request, host)
        if snapshot != evidence['snapshot'] or delivery != snapshot['source_inventory']:
            raise Rejected('legacy_inventory_changed', 'Current source, outputs or resources differ from the reviewed adoption')
        if not apply:
            return {'work_id': request['work_id'], 'ready': True, 'applied': False,
                    'disposition': 'Legacy retirement preview; historical startup outcome remains unknown',
                    'snapshot_digest': digest(snapshot), 'evidence_sha256': request['evidence_sha256']}
        lifecycle._fault('legacy_before_recheck')
        for ref in (evidence['host_provenance'], evidence['authority'], review['report'], adoption['delivery_inventory']):
            _protected(store, ref)
        if (_protected(store, {'path': request['evidence_path'], 'sha256': request['evidence_sha256']}) != evidence_raw
                or observe_host() != host or _snapshot(lifecycle, db, request, host) != snapshot):
            raise Rejected('legacy_evidence_changed', 'Recovery facts changed before application')
        detail = {'operation_id': request['operation_id'], 'attempt_id': request['attempt_id'],
                  'evidence_sha256': request['evidence_sha256'], 'evidence_path': request['evidence_path'],
                  'evidence': evidence, 'historical_startup_outcome': 'unknown',
                  'disposition': 'Retired from current reboot and adopted-content evidence; no historical stop receipt',
                  'runtime_and_budget_modified': False, 'new_work_admitted': False}
        stamp, work_id = now(), request['work_id']
        db.execute("UPDATE assignment SET state='cancelled',terminal_at=? WHERE work_id=?", (stamp, work_id))
        db.execute("UPDATE outbox SET state='cancelled',updated_at=? WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?)", (stamp, work_id))
        db.execute("UPDATE lifecycle SET execution='retired',control='retired',admitted=0 WHERE work_id=?", (work_id,))
        db.execute('DELETE FROM resource WHERE work_id=?', (work_id,))
        db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='unknown_effects'", (work_id,))
        lifecycle._touch(db, work_id, 'legacy_retired', detail)
        lifecycle._fault('legacy_before_commit')
        result = {'work_id': work_id, 'work_revision': store._work(db, work_id)['revision'], 'applied': True,
                  'execution': 'retired', 'historical_startup_outcome': 'unknown',
                  'runtime_and_budget_modified': False, 'new_work_admitted': False,
                  'evidence_sha256': request['evidence_sha256']}
        return store._record(db, request['operation_id'], payload, result)
