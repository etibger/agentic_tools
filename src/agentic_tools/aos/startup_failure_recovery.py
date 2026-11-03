"""Audited recovery of one independently evidenced, pre-thread startup failure.

The original runtime row, false cleanup result, observation and reservations are
immutable history. This host-only route derives a *new* terminal receipt from a
qualified producer and fresh observations. It never acknowledges a resource
breach, admits a successor, or supplies new execution authority.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import native_paths, resource_limits
from .codex_runtime import directory_identity, inventory, process_groups
from .lifecycle import stop_conflicts_in
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-pre-thread-failure-recovery-v1'
TABLE = 'startup_failure_recovery'
SCHEMA = ('CREATE TABLE startup_failure_recovery '
          '(operation_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
PRODUCER_MANIFEST = '02666499b5395df0c76c1e16e3df709ba5a47051b867bead400e325e3cf11b09'
PRODUCER_FILES = {
    'codex_runtime.py': '1f9e84f9b1d36ef3a5ab504c48bbe41fabea169e43432c3978b51c2a20e09b77',
    'worker.py': '09fc147a2ed4d6b5cea03a14131867b0f141b26c0f7f3ad7cfcc7b47fd03e4c4',
    'lifecycle.py': '4b72f84ca237e230ac0ee0f9edd90332d9b2e03fcdb3d3cde9af9519cc489b28',
}
FAILURE = 'Rejected: runtime_sandbox: Exact mutable probe exhausted its original fifteen-second window'
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SQL_BYTES = 512 * 1024 * 1024
MAX_ROWS = 10000
OBSERVATION_SECONDS = 120


class StartupFailureRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    attempt_id: str
    runtime_record_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    evidence_path: str
    evidence_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def _reject(message):
    raise Rejected('startup_failure_recovery', message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject('Duplicate evidence keys are not an exact recovery record')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda value: _reject('Nonfinite evidence value'))


def _path(value):
    return native_paths._canonical(value)


def _identity(path, *, private=True):
    path = _path(str(path))
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)):
        _reject('Recovery files must be protected, owned, single-link regular files')
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
            'mode': stat.S_IMODE(info.st_mode), 'links': info.st_nlink}


def _file(store, reference, *, immutable=False):
    if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or not isinstance(reference['path'], str)
            or not isinstance(reference['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', reference['sha256'])):
        _reject('Exact protected file path and SHA-256 are required')
    path = _path(reference['path'])
    if any(native_paths.overlaps(path, Path(root)) for root in store.worker_writable_roots):
        _reject('Evidence overlaps a worker-writable namespace')
    identity = _identity(path, private=False)
    if immutable and identity['mode'] & 0o222:
        _reject('Original runtime record backup must be immutable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE_BYTES:
            _reject('Recovery file exceeds its bounded size')
        raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')
    if (len(raw) > MAX_FILE_BYTES or any(getattr(before, k) != getattr(after, k) for k in fields)
            or _identity(path, private=False) != identity or _sha(raw) != reference['sha256']):
        _reject('Protected evidence changed while reading')
    return raw


def _sidecar(database, work_id):
    path = resource_limits._failure_path(database, work_id)
    if not path.exists() and not path.is_symlink():
        return {'path': str(path), 'absent': True}, None
    identity = _identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(4097)
        after = os.fstat(stream.fileno())
    if (len(raw) > 4096 or _identity(path) != identity
            or any(getattr(before, key) != getattr(after, key)
                   for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
        _reject('Resource failure sidecar changed or exceeded its bound')
    return {'path': str(path), 'identity': identity, 'sha256': _sha(raw)}, _json(raw)


@contextmanager
def _worker_fence(store):
    path = store.directory / 'worker.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        identity = _identity(path)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (identity['device'], identity['inode']):
            _reject('Worker lock changed while opening')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('worker_owned', 'Stop the worker before startup failure recovery') from error
        def verify():
            if _identity(path) != identity:
                _reject('Worker lock identity changed')
        yield verify
        verify()
    finally:
        os.close(fd)


@contextmanager
def _connection(store, *, write=False):
    paths = (store.path, store.directory / 'codex-runtime.sqlite3')
    identities = [_identity(path) for path in paths]
    if identities[0]['device'] != identities[1]['device']:
        _reject('Atomic recovery requires both rollback journals on the same filesystem')
    for path in paths:
        if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
               for suffix in ('-wal', '-shm', '-journal')):
            _reject('Pending SQLite auxiliaries are not qualified for this atomic recovery')
    mode = 'rw' if write else 'ro'
    db = sqlite3.connect(paths[0].as_uri() + '?mode=' + mode, uri=True, timeout=0)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('ATTACH DATABASE ? AS runtime', (paths[1].as_uri() + '?mode=' + mode,))
        if any(db.execute(f'PRAGMA {name}.journal_mode').fetchone()[0] != 'delete'
               for name in ('main', 'runtime')):
            _reject('Only unchanged DELETE rollback journals have qualified atomicity')
        if [_identity(path) for path in paths] != identities:
            _reject('Journal identity changed while opening')
        yield db, identities
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def _rows(db, sql, args=()):
    rows, size = [], 0
    for row in db.execute(sql, args):
        value = dict(row)
        size += len(canonical(value).encode())
        if len(rows) >= MAX_ROWS or size > MAX_SQL_BYTES:
            _reject('Recovery SQL snapshot exceeded its bound')
        rows.append(value)
    return rows


def _audit_schema(db):
    objects = [tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name=? OR (type='trigger' AND tbl_name=?) ORDER BY name",
        (TABLE, TABLE))]
    if objects and objects != [('table', TABLE, SCHEMA)]:
        _reject('Recovery audit schema changed')
    # The only recovery writes are these tables. Unknown triggers could turn a
    # guarded update into a change to immutable history in either journal.
    targets = ('assignment', 'outbox', 'lifecycle', 'resource', 'completion',
               'technical_blocker', 'work', 'lifecycle_event', 'operation')
    if db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ("
                  + ','.join('?' for _ in targets) + ')', targets).fetchone():
        _reject('Recovery write tables have unsupported triggers')
    if db.execute("SELECT 1 FROM runtime.sqlite_schema WHERE type='trigger' AND tbl_name='resource_limit'").fetchone():
        _reject('Resource guard has unsupported mutation triggers')
    return bool(objects)


def _state(lifecycle, db, request, identities):
    work, aid = request['work_id'], request['attempt_id']
    lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
    _audit_schema(db)
    selected = {
        'work': _rows(db, 'SELECT * FROM work WHERE work_id=?', (work,)),
        'lifecycle': _rows(db, 'SELECT * FROM lifecycle WHERE work_id=?', (work,)),
        'assignments': _rows(db, 'SELECT * FROM assignment WHERE work_id=? ORDER BY assignment_id', (work,)),
        'observations': _rows(db, 'SELECT * FROM runtime_observation WHERE attempt_id=?', (aid,)),
        'worker': _rows(db, 'SELECT * FROM worker_status ORDER BY singleton'),
        'resources': _rows(db, 'SELECT * FROM resource ORDER BY resource_id'),
        'outbox': _rows(db, 'SELECT * FROM outbox WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?) ORDER BY assignment_id', (work,)),
        'stops': _rows(db, 'SELECT * FROM owned_stop WHERE work_id=? ORDER BY attempt_id', (work,)),
        'blockers': _rows(db, 'SELECT * FROM technical_blocker WHERE work_id=? ORDER BY code', (work,)),
        'completions': _rows(db, 'SELECT * FROM completion WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?) ORDER BY receipt_id', (work,)),
        'other_admitted': _rows(db, 'SELECT * FROM lifecycle WHERE admitted=1 AND work_id<>? ORDER BY work_id', (work,)),
        'other_live_assignments': _rows(db, "SELECT * FROM assignment WHERE work_id<>? AND state NOT IN ('completed','failed','cancelled') ORDER BY assignment_id", (work,)),
        'attempts': _rows(db, 'SELECT id,record FROM runtime.attempt ORDER BY id'),
        'budgets': _rows(db, 'SELECT * FROM runtime.runtime_budget ORDER BY id'),
        'budget_scopes': _rows(db, 'SELECT * FROM runtime.product_budget_scope ORDER BY scope'),
        'processes': _rows(db, 'SELECT * FROM runtime.observed_process WHERE attempt_id=? ORDER BY pid,started', (aid,)),
        'native_checks': _rows(db, 'SELECT id,attempt_id,record FROM runtime.native_check ORDER BY id'),
        'guards': _rows(db, 'SELECT * FROM runtime.resource_limit ORDER BY work_id'),
    }
    if sum(len(canonical(rows).encode()) for rows in selected.values()) > MAX_SQL_BYTES:
        _reject('Combined recovery snapshot exceeded its bound')
    raw = next((row['record'] for row in selected['attempts'] if row['id'] == aid), None)
    if raw is None or _sha(raw.encode()) != request['runtime_record_sha256']:
        _reject('Exact original runtime row is missing or changed')
    sidecars = [_sidecar(path, work) for path in (lifecycle.store.path, lifecycle.store.directory / 'codex-runtime.sqlite3')]
    snapshot = {'journals': identities, 'tables': {key: {'rows': len(rows), 'digest': digest(rows)}
                                                for key, rows in selected.items()},
                'failure_sidecars': [item[0] for item in sidecars],
                'stop_conflicts': stop_conflicts_in(db, work)}
    return selected, snapshot, [item[1] for item in sidecars]


def capture(lifecycle, request):
    """Capture exact SQL/sidecar identities for review; no runtime is constructed."""
    for key in ('work_id', 'attempt_id'):
        identifier(request[key])
    with _worker_fence(lifecycle.store) as verify_lock, _connection(lifecycle.store) as (db, identities):
        db.execute('BEGIN')
        snapshot = _state(lifecycle, db, request, identities)[1]
        verify_lock()
        return snapshot


def _evidence(store, request):
    reference = {'path': request['evidence_path'], 'sha256': request['evidence_sha256']}
    raw = _file(store, reference)
    evidence = _json(raw)
    keys = {'format', 'work_id', 'attempt_id', 'producer', 'failure_readback', 'runtime_record',
            'snapshot', 'worker_completion', 'protected_files', 'authority', 'review'}
    if (not isinstance(evidence, dict) or set(evidence) != keys or evidence['format'] != FORMAT
            or any(evidence[k] != request[k] for k in ('work_id', 'attempt_id'))):
        _reject('Exact modern pre-thread recovery evidence is required')
    producer = evidence['producer']
    if (not isinstance(producer, dict) or set(producer) != {'manifest', *PRODUCER_FILES}
            or producer['manifest'].get('sha256') != PRODUCER_MANIFEST):
        _reject('Failure producer is not the qualified R21 source')
    _file(store, producer['manifest'])
    for name, expected in PRODUCER_FILES.items():
        if producer[name].get('sha256') != expected:
            _reject('The producer branch proof names different source bytes')
        _file(store, producer[name])
    original = _file(store, evidence['runtime_record'], immutable=True)
    if _sha(original) != request['runtime_record_sha256']:
        _reject('Immutable original runtime backup does not match the exact row')
    failure = _json(_file(store, evidence['failure_readback']))
    _worker_files(store, evidence)
    if not _file(store, evidence['authority']).strip():
        _reject('Explicit host recovery authority must be retained')
    protected = evidence['protected_files']
    if (not isinstance(protected, list) or len(protected) > 64
            or len({ref['path'] for ref in protected}) != len(protected)):
        _reject('Protected budget and sidecar references must be exact and unique')
    for ref in protected:
        _file(store, ref)
    review = evidence['review']
    if (not isinstance(review, dict) or set(review) != {'author', 'reviewer', 'report'}
            or not all(isinstance(review[k], str) and review[k].strip() for k in ('author', 'reviewer'))
            or review['author'] == review['reviewer']):
        _reject('Independent review is required for separately derived recovery evidence')
    report = _json(_file(store, review['report']))
    expected = {'format': FORMAT + '-review', 'work_id': request['work_id'], 'attempt_id': request['attempt_id'],
                'snapshot_digest': digest(evidence['snapshot']), 'runtime_record_sha256': request['runtime_record_sha256'],
                'producer_manifest_sha256': PRODUCER_MANIFEST, 'verdict': 'pass',
                'worker_completion_sha256': evidence['worker_completion']['sha256'],
                'author': review['author'], 'reviewer': review['reviewer']}
    if (not isinstance(report, dict) or set(report) != {*expected, 'reason'}
            or any(report[k] != value for k, value in expected.items())
            or not isinstance(report['reason'], str) or not report['reason'].strip()):
        _reject('Independent review does not bind the exact failure, producer and snapshot')
    return evidence, original.decode(), failure


def _worker_files(store, evidence):
    """Read retained command provenance; never inspect or run its shell text."""
    completion = _json(_file(store, evidence['worker_completion'], immutable=True))
    configuration = _json(_file(store, completion['worker_configuration']))
    mapping_ref = {key: configuration['runtime_recipes'][key] for key in ('path', 'sha256')}
    mapping = _json(_file(store, mapping_ref))
    return completion, configuration, mapping


def _worker_completion(store, evidence, request, worker, recipe, failure):
    completion, configuration, mapping = _worker_files(store, evidence)
    if (completion.get('format') != 'aos-owned-worker-command-evidence-v1'
            or any(completion.get(key) != request[key] for key in ('work_id', 'attempt_id'))
            or completion.get('worker_epoch') != worker['epoch']
            or completion.get('worker_owner') != worker['owner']
            or worker['state'] != 'stopped' or failure.get('worker') != [worker]
            or type(completion.get('session_id')) is not int or completion['session_id'] <= 0
            or type(completion.get('exit_code')) is not int or completion['exit_code'] != 1
            or configuration.get('shared_state', {}).get('path') != str(store.directory)
            or configuration.get('qualified_source', {}).get('candidate', {}).get('sha256') != PRODUCER_MANIFEST
            or configuration.get('runtime_recipes', {}).get('recipe_digest') != digest(recipe)
            or mapping != {request['work_id']: recipe}):
        _reject('Owned command completion does not bind this stopped worker, state and recipe')
    events = completion.get('events')
    if not isinstance(events, list) or len(events) != 4:
        _reject('Retain the exact paired launch and foreground completion tool events')
    lines, payloads, stamps = [], [], []
    for item in events:
        if (type(item.get('line')) is not int or item['line'] < 1
                or not isinstance(item.get('line_sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', item['line_sha256'])):
            _reject('Retained command event identity is malformed')
        lines.append(item['line'])
        event = item['event']
        if event.get('type') != 'response_item':
            _reject('Only original tool call/result events establish owned command completion')
        payloads.append(event['payload'])
        stamps.append(datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')))
    if (lines != sorted(set(lines)) or stamps != sorted(stamps)
            or any(stamp.tzinfo is None for stamp in stamps)):
        _reject('Command launch and completion ordering is not retained')
    launch, launched, stop, stopped = payloads
    for call, result in ((launch, launched), (stop, stopped)):
        if (call.get('type') != 'custom_tool_call' or call.get('name') != 'exec'
                or result.get('type') != 'custom_tool_call_output'
                or not isinstance(call.get('call_id'), str) or not call['call_id']
                or result.get('call_id') != call['call_id']):
            _reject('Owned command tool calls and results do not match')
    if launch['call_id'] == stop['call_id']:
        _reject('Launch and completion require distinct retained calls')
    def objects(payload):
        result = []
        for item in payload['output']:
            if item.get('type') == 'input_text' and item.get('text', '').startswith('{'):
                value = _json(item['text'])
                if isinstance(value, dict):
                    result.append(value)
        return result
    launched_results = [value for value in objects(launched) if value.get('session_id') == completion['session_id']]
    stop_results = objects(stopped)
    ended_results = [value for value in stop_results if value.get('exit_code') == 1
                     and 'KeyboardInterrupt' in value.get('output', '')]
    if (len(launched_results) != 1 or launched_results[0].get('exit_code') is not None
            or len(ended_results) != 1 or ended_results[0].get('session_id') is not None
            or any(value.get('session_id') is not None for value in stop_results)
            or 'worker.run(' not in ended_results[0]['output']
            or 'time.sleep(interval)' not in ended_results[0]['output']):
        _reject('The owned foreground worker command has no exact completed exit receipt')
    # This is a deliberately narrow decoder for the retained original call, not
    # an evaluator or a general shell/tool transcript interpreter.
    command = re.findall(r'tools\.exec_command\(\{cmd:("(?:[^"\\]|\\.)*")', launch['input'])
    if len(command) != 1:
        _reject('The retained launch must name one explicit worker command')
    argv = shlex.split(_json(command[0]))
    expected_argv = configuration.get('worker_argv')
    if (not argv or not isinstance(expected_argv, list) or not expected_argv
            or str(Path(configuration['cwd']) / argv[0]) != expected_argv[0]
            or argv[1:] != expected_argv[1:]
            or argv[1:5] != ['-I', '-m', 'agentic_tools.aos.cli', 'worker']
            or '--state-dir' not in argv or argv[argv.index('--state-dir') + 1] != str(store.directory)
            or '--runtime-recipes' not in argv
            or argv[argv.index('--runtime-recipes') + 1] != configuration['runtime_recipes']['path']):
        _reject('Retained launch differs from the exact reviewed worker configuration')
    stopped_session = re.findall(r'tools\.write_stdin\(\{session_id:(\d+),chars:("(?:[^"\\]|\\.)*")', stop['input'])
    if (len(stopped_session) != 1 or int(stopped_session[0][0]) != completion['session_id']
            or _json(stopped_session[0][1]) != '\x03'):
        _reject('Foreground stop belongs to a different owned command session')
    return {'completion_sha256': evidence['worker_completion']['sha256'],
            'session_id': completion['session_id'], 'exit_code': 1,
            'epoch': worker['epoch'], 'owner': worker['owner'],
            'method': 'retained owned foreground command exit plus stopped worker row and exclusive unchanged worker lock',
            'worker_pid_or_detached_census_claimed': False}


def _evidence_identities(request, evidence, worker_references):
    references = [{'path': request['evidence_path'], 'sha256': request['evidence_sha256']},
                  *evidence['producer'].values(), evidence['runtime_record'], evidence['failure_readback'],
                  evidence['authority'], evidence['review']['report'], evidence['worker_completion'],
                  *evidence['protected_files'], *worker_references]
    result = {}
    for reference in references:
        path = _path(reference['path'])
        info = path.lstat()
        result[str(path)] = (_identity(path, private=False), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return result


def _classify(state, request, evidence, original, failure):
    aid, work = request['attempt_id'], request['work_id']
    if (len(state['lifecycle']) != 1 or len(state['assignments']) != 1 or len(state['observations']) != 1
            or len(state['worker']) != 1 or state['worker'][0]['state'] != 'stopped'
            or state['other_admitted'] or state['other_live_assignments'] or state['completions']):
        _reject('Only one unresolved assignment with a stopped worker and no other live work is supported')
    run, assignment = state['lifecycle'][0], state['assignments'][0]
    contract, record = _json(run['contract']), _json(original)
    observation_row = state['observations'][0]
    observation = _json(observation_row['observation'])
    if (run['admitted'] != 1 or run['execution'] != 'held' or run['control'] != 'none'
            or assignment['attempt_id'] != aid or assignment['state'] != 'blocked_unknown_effects'
            or assignment['acknowledged_at'] is not None or assignment['terminal_at'] is not None
            or assignment['result'] is not None or assignment['kind'] != 'implementation'
            or assignment['contract_revision'] != contract['revision']
            or assignment['concept_revision'] != contract['concept_revision']
            or assignment['concept_revision'] != state['work'][0]['current_concept']
            or assignment['concept_digest'] != request['digest'] or contract['concept_digest'] != request['digest']):
        _reject('Assignment and approved contract are outside the supported failed startup class')
    unknown = [row for row in state['blockers'] if row['code'] == 'unknown_effects']
    if (len(unknown) != 1 or unknown[0]['work_id'] != work
            or unknown[0]['owner'] != 'coordinator' or unknown[0]['dependency'] != 'dispatch'
            or state['stops']):
        _reject('The ordinary unknown-effects hold must have no cancellation or owned-stop operation')
    expected_observation = {'attempt_id': aid, 'state': 'unknown', 'model': None, 'effort': None,
                            'thread_id': None, 'turn_id': None, 'usage': None,
                            'model_calls_attempted': 0, 'detail': FAILURE}
    if (observation != expected_observation or record.get('attempt_id') != aid
            or record.get('phase') != 'intent' or record.get('startup_version') != 1
            or record.get('startup_qualification_only') is not False
            or type(record.get('model_calls_attempted')) is not int or record['model_calls_attempted'] != 0
            or any(record.get(k) is not None for k in ('thread_id', 'turn_id', 'startup_outcome', 'error', 'terminal_observation', 'usage'))
            or record.get('usage_raw') != [] or record.get('native_cancel_requested') is not True
            or record.get('runtime_version') != 'codex-cli 0.163.0'):
        _reject('The retained main-thread observation does not prove the qualified pre-thread failure')
    if (failure.get('attempt') != record or failure.get('assignment') != assignment
            or failure.get('runtime_observations') != state['observations']
            or not isinstance(record.get('assignment'), dict)
            or any(record['assignment'].get(k) != assignment[k] for k in (
                'assignment_id', 'attempt_id', 'work_id', 'kind', 'concept_revision', 'concept_digest',
                'contract_revision', 'scope', 'executor', 'requested_model', 'requested_effort'))):
        _reject('Original runtime, independent observation and sealed failure readback differ')
    recipe = contract.get('runtime_recipe', {})
    if (record.get('recipe_digest') != digest(recipe) or recipe.get('model_write_scope') != 'existing-mutable-files'
            or record.get('run_outputs') != str(Path(recipe['outputs']) / aid)
            or record.get('budget_seconds') != recipe.get('max_seconds')
            or record.get('budget_seconds') != contract['max_seconds']):
        _reject('The original recipe, output identity or charged allowance changed')
    capsule = recipe.get('resource_limits_carry_forward')
    if (not isinstance(capsule, dict) or set(capsule) != {'path', 'sha256'}
            or evidence['protected_files'] != [capsule]):
        _reject('The exact recipe carry-forward capsule must be the sole external accounting reference')
    if (set(record.get('startup_paths', {})) != {recipe['source'], record['run_outputs']}
            or not isinstance(record.get('baseline'), dict) or not record['baseline']
            or not isinstance(record.get('startup_outputs'), dict)
            or not isinstance(record.get('startup_native_paths'), dict)):
        _reject('Original complete startup source, output and native inventories are required')
    stop = record.get('startup_stop_receipt', {})
    if (stop.get('attempt_id') != aid or stop.get('stopped') is not True or stop.get('owned_processes') != []
            or stop.get('effects_reconciled') is not False or stop.get('startup_outcome') is not None
            or any(stop.get(k) != {'status': 'none', 'changes': []} for k in ('partial_mutations', 'output_mutations'))
            or stop.get('native_path_effects', {}).get('observed') is not True
            or any(stop['native_path_effects']['changes'].values())
            or stop['native_path_effects']['after'] != record.get('startup_native_paths')):
        _reject('The original false cleanup must retain positive stopped and unchanged-effect observations')
    owners, exited = record.get('server_history', []), record.get('startup_owner_exit', {})
    if (len(owners) != 1 or owners[0].get('pid') != owners[0].get('group')
            or type(owners[0]['pid']) is not int or owners[0]['pid'] <= 0
            or any(exited.get(k) != owners[0][k] for k in ('pid', 'started'))):
        _reject('Exact startup owner and its independent exit observation are required')
    stamps = [record['created_at'], record['configuration_verified_at'], exited['observed_at'],
              observation_row['observed_at'], stop['observed_at']]
    parsed = [datetime.fromisoformat(value) for value in stamps]
    if any(value.tzinfo is None for value in parsed) or parsed != sorted(parsed):
        _reject('Failure, owner exit and later stale cleanup ordering is not established')
    if any(row['attempt_id'] == aid or _json(row['record']).get('phase') != 'clean' for row in state['native_checks']):
        _reject('Native checks or unresolved native ownership are outside pre-thread recovery')
    resources = [row for row in state['resources'] if row['work_id'] == work]
    if ({row['resource_id'] for row in resources} != set(contract['resources'])
            or len(resources) != len(contract['resources'])
            or any(row['assignment_id'] not in (None, assignment['assignment_id']) for row in resources)):
        _reject('All and only the assignment resource claims must be retained')
    for other in (row for row in state['resources'] if row['work_id'] != work):
        if other['resource_id'].startswith('/') and any(
                row['resource_id'].startswith('/') and native_paths.overlaps(Path(other['resource_id']), Path(row['resource_id']))
                for row in resources):
            _reject('Another work retains overlapping resource ownership')
    budget = next((row for row in state['budgets'] if row['id'] == record.get('budget_id')), None)
    if budget is None:
        _reject('Original durable budget record is missing')
    limits = _json(budget['record'])
    reservations = [_json(row['record']) for row in state['attempts']
                    if _json(row['record']).get('budget_id') == record['budget_id']]
    if (limits.get('kind') != 'product' or limits.get('source') != recipe['source']
            or limits.get('budget_id') != record['budget_id']
            or {row['scope'] for row in state['budget_scopes'] if row['budget_id'] == record['budget_id']}
               != {'source:' + recipe['source'], 'product:' + limits.get('product_id', '')}
            or any(type(row.get('budget_seconds')) is not int or row['budget_seconds'] <= 0 for row in reservations)
            or len(reservations) > limits['max_assignments']
            or sum(row['budget_seconds'] for row in reservations) > limits['max_seconds']):
        _reject('Original failed reservations and budget limits must remain consistent')
    guard_row = next((row for row in state['guards'] if row['work_id'] == work), None)
    if guard_row is None:
        _reject('Original resource guard is required')
    guard = _json(guard_row['record'])
    if (guard.get('active_attempt') != aid or guard.get('scope_digest') != digest(guard['scope'])
            or guard.get('breach', {}).get('reason') != 'observation_failed'
            or guard['breach'].get('error_code') != 'resource_observation'
            or guard['breach'].get('error_message') != 'Resource observation exceeded its bounded scan'
            or any(row['work_id'] != work and _json(row['record']).get('active_attempt') is not None for row in state['guards'])):
        _reject('Only this stopped attempt and its retained observation failure may be deactivated')
    if (set(guard.get('high_water_growth_bytes', {})) != {'output_roots', 'source_evidence_roots'}
            or any(type(value) is not int or value < 0 for value in guard['high_water_growth_bytes'].values())):
        _reject('Retained resource high-water floors must be nonnegative integers')
    resource_limits._owner_generation(guard)
    expected_scope = {key: value for key, value in guard['scope'].items() if key not in (
        'source', 'generated_roots', 'temporary_paths', 'scan_seconds', 'max_observation_gap_seconds')}
    configured = dict(recipe['resource_limits'])
    for key in ('output_roots', 'source_evidence_roots'):
        configured[key] = sorted(configured[key])
    if (configured != expected_scope or guard['scope']['source'] != recipe['source']
            or guard['scope']['generated_roots'] != sorted(entry['path'] for entry in recipe.get('native_write_roots', []) if entry['kind'] == 'generated')
            or guard['scope']['temporary_paths'] != recipe.get('native_sandbox', {}).get('temporary_paths', [])
            or guard['scope']['scan_seconds'] != resource_limits.SCAN_SECONDS
            or guard['scope']['max_observation_gap_seconds'] != guard['scope']['sample_interval_seconds'] + resource_limits.SCAN_SECONDS + 2
            or guard.get('carry_forward_reference') != capsule
            or guard.get('carry_forward', {}).get('capsule_digest') != capsule['sha256']
            or failure.get('resource_guard_summary') != {key: guard.get(key) for key in (
                'active_attempt', 'breach', 'carry_forward', 'high_water_growth_bytes', 'owner_generation', 'scope_digest')}):
        _reject('Original resource scope, capsule, caps or retained high-water floors differ')
    return record, recipe, assignment, guard, resources


def _observe_processes(record, observed, remaining):
    remaining()
    rows = process_groups(timeout=min(5, remaining()))
    owners = record['server_history']
    known = dict(record.get('startup_observed_processes', {})) | dict(record.get('observed_processes', {}))
    for row in observed:
        known[row['pid']] = row['started']
    if 'unavailable' in known:
        _reject('Historical process observation was unavailable')
    pids = {int(pid) for pid in known} | {owner['pid'] for owner in owners}
    groups = {owner['group'] for owner in owners}
    if any(pid in rows for pid in pids) or any(row['group'] in groups for row in rows.values()):
        _reject('An original owner, observed PID or process group is still present or reused')
    remaining()
    return {'observed_pids': sorted(pids), 'observed_groups': sorted(groups), 'present': [],
            'method': 'bounded appserver/observed-process group observation; not a worker PID or detached-process census'}


def _observe_effects(record, recipe, remaining):
    for name, identity in record['startup_paths'].items():
        if directory_identity(Path(name)) != identity:
            _reject('Original startup source/output identity changed')
    source = inventory(Path(recipe['source']), recipe=recipe, check_deadline=remaining)
    outputs = inventory(Path(record['run_outputs']), allow_empty=True, check_deadline=remaining)
    native = native_paths.reconcile(recipe, record['startup_native_paths'], check_deadline=remaining)
    if source != record['baseline'] or outputs != record['startup_outputs'] or any(native['changes'].values()):
        _reject('Current source, outputs or native paths differ from the original stopped observations')
    for name, identity in record['startup_paths'].items():
        if directory_identity(Path(name)) != identity:
            _reject('Startup directory identity changed during recovery observation')
    remaining()
    return {'source_inventory_digest': digest(source), 'output_inventory_digest': digest(outputs),
            'native_snapshot_digest': digest(native['after']), 'source_changes': [], 'output_changes': [],
            'native_changes': native['changes'], 'native_attribution': native['attribution']}


def _finish_guard(guard, retained, attempt_id, work_id):
    """Pure reduction with one fresh scan; sticky failures are never cleared."""
    original = copy.deepcopy(guard)
    for failure in retained:
        if failure is None:
            continue
        if (set(failure) != {'work_id', 'scope_digest', 'breach', 'high_water_growth_bytes'}
                or failure['work_id'] != work_id or failure['scope_digest'] != guard['scope_digest']
                or failure['breach'] != guard['breach']
                or set(failure['high_water_growth_bytes']) != set(guard['high_water_growth_bytes'])
                or any(type(v) is not int or v < 0 for v in failure['high_water_growth_bytes'].values())):
            _reject('Failure sidecar is contradictory or malformed')
        for key, value in failure['high_water_growth_bytes'].items():
            guard['high_water_growth_bytes'][key] = max(guard['high_water_growth_bytes'][key], value)
    observed = resource_limits.observe({**guard['scope'], '_temporary_baseline': guard['baseline'].get('temporary_baseline', {})})
    if observed['identities'] != guard['baseline']['identities']:
        _reject('Resource root identity changed')
    for group, cap in (('output_roots', 'max_output_growth_bytes'), ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
        growth = sum(max(0, size - guard['baseline']['files'][group].get(name, 0))
                     for name, size in observed['files'][group].items())
        guard['high_water_growth_bytes'][group] = max(guard['high_water_growth_bytes'][group], growth)
        if guard['high_water_growth_bytes'][group] > guard['scope'][cap]:
            _reject('Observed retained growth exceeds the unchanged cap')
    if min(observed['available_bytes'].values(), default=0) < guard['scope']['min_free_bytes']:
        _reject('Current free space is below the original resource limit')
    if guard['active_attempt'] != attempt_id or guard['breach'] != original['breach']:
        _reject('Original failure ownership changed')
    guard.update(active_attempt=None, owner_generation=resource_limits._owner_generation(guard) + 1,
                 last_observation=observed)
    return guard


def _recover(lifecycle, request, *, apply=False):
    """Trusted preview/apply; no constructors, workers, signals or model calls."""
    StartupFailureRecoveryRequest.model_validate(request, strict=True)
    if type(apply) is not bool:
        _reject('Apply must be explicit boolean')
    store = lifecycle.store
    if store.recovery_hold() or (apply and store.read_only):
        _reject('A held recovery copy or read-only store cannot release original ownership')
    for key in ('operation_id', 'work_id', 'attempt_id'):
        identifier(request[key])
    payload = [FORMAT, request]
    with store._connection() as prior_db:
        if prior := store._prior(prior_db, request['operation_id'], payload):
            return prior
    with _worker_fence(store) as verify_lock:
        evidence, original, failure = _evidence(store, request)
        completion, configuration, _ = _worker_files(store, evidence)
        worker_references = [completion['worker_configuration'],
                             {key: configuration['runtime_recipes'][key] for key in ('path', 'sha256')}]
        evidence_identities = _evidence_identities(request, evidence, worker_references)
        with _connection(store) as (db, identities):
            db.execute('BEGIN')
            state, snapshot, sidecars = _state(lifecycle, db, request, identities)
        if snapshot != evidence['snapshot'] or snapshot['stop_conflicts']:
            _reject('Exact reviewed current state changed or has stop conflicts')
        record, recipe, assignment, guard, resources = _classify(state, request, evidence, original, failure)
        worker_completion = _worker_completion(store, evidence, request, state['worker'][0], recipe, failure)
        carried = resource_limits._carry_forward(recipe, guard['scope'])
        if (not carried or carried.get('target_work_id') != request['work_id']
                or carried['baseline'] != guard['baseline']
                or any(carried[key] != guard['carry_forward'][key] for key in (
                    'qualification_id', 'charged_growth_bytes', 'original_ceilings', 'capsule_digest'))):
            _reject('Original carried baseline, ceilings or charges changed')
        started = time.monotonic()
        def remaining():
            left = OBSERVATION_SECONDS - (time.monotonic() - started)
            if left <= 0:
                _reject('Recovery observation exceeded its bounded read-only window')
            return left
        processes = _observe_processes(record, state['processes'], remaining)
        effects = _observe_effects(record, recipe, remaining)
        # Re-read all pinned evidence before the final scan, keeping large
        # hashing/decoding work out of the short freshness and writer windows.
        if _evidence(store, request) != (evidence, original, failure):
            _reject('Recovery evidence changed during observation')
        if _evidence_identities(request, evidence, worker_references) != evidence_identities:
            _reject('Recovery evidence identity changed during observation')
        finished = _finish_guard(copy.deepcopy(guard), sidecars, request['attempt_id'], request['work_id'])
        remaining()
        process_recheck = _observe_processes(record, state['processes'], remaining)
        if process_recheck != processes:
            _reject('Process proof changed during recovery')
        terminal = {'format': FORMAT + '-terminal', 'attempt_id': request['attempt_id'],
                    'executor': assignment['executor'], 'status': 'failed', 'stopped': True,
                    'owned_processes': [], 'effects_reconciled': True,
                    'partial_mutations': {'status': 'none', 'changes': []},
                    'provenance': 'separately-derived-after-failure', 'original_row_modified': False,
                    'original_cleanup_effects_reconciled': False, 'failure': FAILURE,
                    'producer_manifest_sha256': PRODUCER_MANIFEST, 'model_calls_attempted': 0,
                    'thread_id': None, 'turn_id': None, 'processes': processes, 'effects': effects,
                    'worker_completion': worker_completion,
                    'resource_limits': resource_limits.receipt(finished),
                    'limits': 'Current observed-owner and filesystem equality; no unobserved detached-process or historical transient-cache proof'}
        detail = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': request['work_id'],
                  'attempt_id': request['attempt_id'], 'evidence': evidence,
                  'evidence_sha256': request['evidence_sha256'], 'snapshot_digest': digest(snapshot),
                  'original_runtime_sha256': request['runtime_record_sha256'], 'terminal_receipt': terminal,
                  'original_guard_sha256': _sha(next(row['record'] for row in state['guards']
                                                    if row['work_id'] == request['work_id']).encode()),
                  'finished_guard_digest': digest(finished),
                  'original_deadline': record['startup_deadline'], 'original_budget_seconds': record['budget_seconds'],
                  'new_execution_authorized': False, 'resource_breach_acknowledged': False}
        encoded_guard = resource_limits._bounded_record(canonical(finished))
        encoded_detail, encoded_terminal = canonical(detail), canonical(terminal)
        if len(encoded_detail.encode()) > MAX_FILE_BYTES:
            _reject('Recovery audit exceeds its bounded size')
        def fresh():
            age = time.time() - finished['last_observation']['observed_at']
            if not 0 <= age <= guard['scope']['max_observation_gap_seconds']:
                _reject('Final recovery observation is no longer fresh; ownership remains held')
        fresh()
        if not apply:
            with _connection(store) as (db, current_identities):
                db.execute('BEGIN')
                if _state(lifecycle, db, request, current_identities)[1] != snapshot:
                    _reject('Recovery preview facts changed before completion')
            if _evidence_identities(request, evidence, worker_references) != evidence_identities:
                _reject('Recovery evidence identity changed before preview completion')
            verify_lock()
            fresh()
            return {'ready': True, 'applied': False, 'work_id': request['work_id'],
                    'snapshot_digest': digest(snapshot), 'terminal_receipt': terminal,
                    'runtime_and_budget_modified': False, 'resource_breach_acknowledged': False,
                    'new_execution_authorized': False, 'evidence_sha256': request['evidence_sha256']}
        lifecycle._fault('startup_recovery_before_commit')
        with _connection(store, write=True) as (db, current_identities):
            db.execute('BEGIN IMMEDIATE')
            if prior := store._prior(db, request['operation_id'], payload):
                return prior
            if _state(lifecycle, db, request, current_identities)[1] != snapshot:
                _reject('Recovery compare-and-set rejected changed authoritative records')
            if [_identity(path) for path in (store.path, store.directory / 'codex-runtime.sqlite3')] != identities:
                _reject('Journal identity changed before recovery commit')
            if _evidence_identities(request, evidence, worker_references) != evidence_identities:
                _reject('Recovery evidence identity changed before application')
            verify_lock()
            fresh()
            if not _audit_schema(db):
                db.execute(SCHEMA)
            stamp, aid, work = now(), request['attempt_id'], request['work_id']
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)', (request['operation_id'], aid, encoded_detail))
            db.execute("UPDATE assignment SET state='failed',terminal_at=?,result=? WHERE assignment_id=? AND state='blocked_unknown_effects'",
                       (stamp, encoded_terminal, assignment['assignment_id']))
            db.execute("UPDATE outbox SET state='blocked',updated_at=? WHERE assignment_id=?", (stamp, assignment['assignment_id']))
            db.execute("UPDATE lifecycle SET execution='held',control='none',admitted=0 WHERE work_id=?", (work,))
            for resource in resources:
                if db.execute('DELETE FROM resource WHERE resource_id=? AND work_id=? AND assignment_id IS ?',
                              (resource['resource_id'], work, resource['assignment_id'])).rowcount != 1:
                    _reject('Exact resource ownership changed')
            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='unknown_effects'", (work,))
            lifecycle._block(db, work, 'startup_failure_recovered',
                             'Failed startup was separately reconciled; original resource breach and charged reservations remain.',
                             'Rebind and approve any future execution; this recovery admits no successor')
            receipt_id = 'startup-recovery:' + _sha(request['operation_id'].encode())[:32]
            db.execute('INSERT INTO completion VALUES(?,?,?,?,?,?)',
                       (receipt_id, assignment['assignment_id'], digest([assignment['assignment_id'], terminal]),
                        encoded_terminal, 'startup_failure_recovered_held', stamp))
            raw_guard = next(row['record'] for row in state['guards'] if row['work_id'] == work)
            if db.execute('UPDATE runtime.resource_limit SET record=? WHERE work_id=? AND record=?',
                          (encoded_guard, work, raw_guard)).rowcount != 1:
                _reject('Original resource guard changed before deactivation')
            lifecycle._touch(db, work, 'startup_failure_recovered',
                             {'operation_id': request['operation_id'], 'attempt_id': aid,
                              'audit_digest': digest(detail), 'evidence_sha256': request['evidence_sha256']})
            lifecycle._fault('startup_recovery_after_updates')
            if ([_identity(path) for path in (store.path, store.directory / 'codex-runtime.sqlite3')] != identities
                    or _evidence_identities(request, evidence, worker_references) != evidence_identities
                    or [_sidecar(path, work)[0] for path in (store.path, store.directory / 'codex-runtime.sqlite3')]
                       != snapshot['failure_sidecars']):
                _reject('Protected identities or failure sidecars changed before commit')
            verify_lock()
            fresh()
            result = {'applied': True, 'work_id': work, 'work_revision': store._work(db, work)['revision'],
                      'assignment_id': assignment['assignment_id'], 'execution': 'held',
                      'terminal_receipt': terminal, 'original_runtime_and_budget_modified': False,
                      'resource_breach_acknowledged': False, 'new_execution_authorized': False,
                      'audit_digest': digest(detail), 'evidence_sha256': request['evidence_sha256']}
            store._record(db, request['operation_id'], payload, result)
            db.commit()
            return result


def recover(lifecycle, request, *, apply=False):
    """Reject malformed/unavailable evidence without changing the original failure."""
    try:
        return _recover(lifecycle, request, apply=apply)
    except Rejected:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        raise Rejected('startup_failure_recovery', 'Exact recovery evidence or atomic journal access is unavailable') from error
