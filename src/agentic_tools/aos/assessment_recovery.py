"""Guarded, retrospective recovery of the pinned pre-admission assessor failure.

Recovery preserves original receipts, approval, candidate and charges. It does
not claim a contemporaneous no-launch receipt existed or authorize a successor.
"""
from __future__ import annotations
import json
import shlex
import sqlite3
import time
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from . import startup_failure_recovery as protected
from .codex_runtime import inventory, directory_identity, process_groups
from .lifecycle import stop_conflicts_in, stopped_with_effects
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-pre-admission-assessment-recovery-v1'
TABLE = 'assessment_recovery'
SCHEMA = ('CREATE TABLE assessment_recovery '
          '(operation_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
PRODUCER = 'sha256:77b74a0a6d31669f34f1e2c2657aa41353e2f2504bd635c955c7deee68d18536'
PRODUCER_FILES = {
    'codex_runtime.py': '312db9a2c799ea39144fc350d74b45ac65f500a3d0e098ac0e7020160811e20a',
    'instructions.py': '19188c84dc7ad75d820a94c07bbdb183b623404c58ea28a3b89a9ae9fa577448',
    'worker.py': '6ebf346b358a16569909d4e254dceaf31adbc778359098ad992ef0818c3b37d9',
    'supervisor.py': '9e6772def5a136953b86b636aa7e212ac8fb4a82464153cc4cf83c2cfe8b1596',
}
FAILURE = 'instruction_drift: Instruction chain, aliases, configuration or content changed'
OBSERVATION_SECONDS = 120

class AssessmentRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    attempt_id: str
    evidence_path: str
    evidence_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

def _reject(message):
    raise Rejected('assessment_recovery', message)

def verify_schema(db, *, allow_absent=False):
    objects = [tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name=? OR (type='trigger' AND tbl_name=?) ORDER BY name", (TABLE, TABLE))]
    if not objects and allow_absent:
        return False
    if objects != [('table', TABLE, SCHEMA)]:
        _reject('Assessment recovery audit schema is missing or changed')
    return True

def read_audit(db, operation_id, work_id):
    """Verify the applied audit against its operation receipt; no authority grant."""
    verify_schema(db)
    row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (operation_id,)).fetchone()
    operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (operation_id,)).fetchone()
    if not row or not operation:
        _reject('An applied assessment recovery is required')
    audit, result = protected._json(row[0]), protected._json(operation[0])
    if (audit.get('format') != FORMAT or audit.get('work_id') != work_id or audit.get('operation_id') != operation_id
            or audit.get('new_execution_authorized') is not False or result.get('applied') is not True
            or result.get('audit_digest') != digest(audit)):
        _reject('Recovery audit differs from its immutable operation receipt')
    return audit

def _schema(db):
    exists = verify_schema(db, allow_absent=True)
    targets = ('assignment', 'outbox', 'lifecycle', 'resource', 'completion', 'owned_stop',
               'technical_blocker', 'work', 'lifecycle_event', 'operation')
    if db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ("
                  + ','.join('?' for _ in targets) + ')', targets).fetchone():
        _reject('Recovery write tables have unsupported triggers')
    return exists

def _state(lifecycle, db, request, identities):
    work, aid = request['work_id'], request['attempt_id']
    lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
    _schema(db)
    queries = {
        'work': ('SELECT * FROM work WHERE work_id=?', (work,)),
        'lifecycle': ('SELECT * FROM lifecycle WHERE work_id=?', (work,)),
        'assignments': ('SELECT * FROM assignment WHERE work_id=? ORDER BY assignment_id', (work,)),
        'worker': ('SELECT * FROM worker_status ORDER BY singleton', ()),
        'resources': ('SELECT * FROM resource ORDER BY resource_id', ()),
        'stops': ('SELECT * FROM owned_stop WHERE work_id=? ORDER BY attempt_id', (work,)),
        'blockers': ('SELECT * FROM technical_blocker WHERE work_id=? ORDER BY code', (work,)),
        'events': ('SELECT * FROM lifecycle_event WHERE work_id=? ORDER BY event_id', (work,)),
        'observations': ('SELECT * FROM runtime_observation WHERE attempt_id=?', (aid,)),
        'outbox': ('SELECT * FROM outbox WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?) ORDER BY assignment_id', (work,)),
        'completions': ('SELECT * FROM completion WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?) ORDER BY receipt_id', (work,)),
        'concepts': ('SELECT * FROM concept WHERE work_id=? ORDER BY revision', (work,)),
        'decisions': ('SELECT * FROM decision WHERE work_id=? ORDER BY created_at', (work,)),
        'attempts': ('SELECT * FROM runtime.attempt ORDER BY id', ()),
        'denials': ('SELECT * FROM runtime.admission_denial ORDER BY id', ()),
        'native_checks': ('SELECT * FROM runtime.native_check ORDER BY id', ()),
        'processes': ('SELECT * FROM runtime.observed_process ORDER BY attempt_id,pid,started', ()),
        'budgets': ('SELECT * FROM runtime.runtime_budget ORDER BY id', ()),
        'budget_scopes': ('SELECT * FROM runtime.product_budget_scope ORDER BY scope', ()),
        'guards': ('SELECT * FROM runtime.resource_limit ORDER BY work_id', ()),
        'other_admitted': ('SELECT * FROM lifecycle WHERE admitted=1 AND work_id<>? ORDER BY work_id', (work,)),
        'other_live': ("SELECT * FROM assignment WHERE work_id<>? AND state NOT IN ('completed','failed','cancelled') ORDER BY assignment_id", (work,)),
    }
    state = {key: protected._rows(db, sql, args) for key, (sql, args) in queries.items()}
    if sum(len(canonical(rows).encode()) for rows in state.values()) > protected.MAX_SQL_BYTES:
        _reject('Recovery snapshot exceeds its bound')
    sidecars = [protected._sidecar(path, work)[0] for path in (lifecycle.store.path, lifecycle.store.directory / 'codex-runtime.sqlite3')]
    snapshot = {'journals': identities, 'tables': {key: {'rows': len(rows), 'digest': digest(rows)} for key, rows in state.items()},
                'failure_sidecars': sidecars, 'stop_conflicts': stop_conflicts_in(db, work)}
    return state, snapshot

def capture(lifecycle, request):
    """Pin stopped authoritative state for independent review, without a runtime."""
    if lifecycle.store.recovery_hold():
        _reject('A restored recovery copy cannot release original ownership')
    for key in ('work_id', 'attempt_id'):
        identifier(request[key])
    with protected._worker_fence(lifecycle.store) as verify, protected._connection(lifecycle.store) as (db, ids):
        db.execute('BEGIN')
        result = _state(lifecycle, db, request, ids)[1]
        verify()
        return result

def _json_file(store, reference, *, immutable=False):
    return protected._json(protected._file(store, reference, immutable=immutable))

def _evidence(store, request):
    value = _json_file(store, {'path': request['evidence_path'], 'sha256': request['evidence_sha256']})
    keys = {'format', 'work_id', 'attempt_id', 'snapshot', 'producer', 'prepared_packet', 'owner_exit', 'review'}
    if (not isinstance(value, dict) or set(value) != keys or value['format'] != FORMAT + '-evidence'
            or value['work_id'] != request['work_id'] or value['attempt_id'] != request['attempt_id']):
        _reject('Exact assessment recovery evidence is required')
    producer = value['producer']
    if not isinstance(producer, dict) or set(producer) != {'manifest', *PRODUCER_FILES}:
        _reject('The complete qualified producer proof is required')
    manifest = _json_file(store, producer['manifest'], immutable=True)
    if digest(manifest) != PRODUCER:
        _reject('The producing source manifest is not qualified')
    for name, expected in PRODUCER_FILES.items():
        protected._file(store, producer[name], immutable=True)
        if producer[name]['sha256'] != expected or manifest.get('aos/' + name) != expected:
            _reject('Producer bytes differ from the qualified no-launch branch')
    prepared = _json_file(store, value['prepared_packet'], immutable=True)
    packet = prepared.get('packet', {})
    if (prepared.get('format') != 'aos-supervisor-prepared-v1' or prepared.get('packet_digest') != digest(packet)
            or packet.get('ready') is not True or packet.get('approval_recorded') is not False
            or packet.get('reservation_performed') is not False
            or packet.get('readiness', {}).get('runtime_identity', {}).get('runtime_source_digest') != PRODUCER
            or packet.get('project', {}).get('state_dir') != str(store.directory)):
        _reject('Original immutable preparation or producing runtime binding changed')
    for ref in packet['input_bindings'].values():
        protected._file(store, ref)
    owner = _json_file(store, value['owner_exit'], immutable=True)
    execution, bindings = owner.get('command_execution', {}), packet['input_bindings']
    argv = ['uv', 'run', 'aos', 'start', '--project', bindings['project']['path'], '--request', bindings['request']['path']]
    command = shlex.split(execution.get('command', ''))
    if len(command) == 3 and command[1] == '-lc':
        command = shlex.split(command[2])
    output = execution.get('output', {})
    if (owner.get('source') != 'codex_app.read_thread' or not owner.get('thread_id') or not owner.get('turn_id')
            or execution.get('type') != 'commandExecution' or not execution.get('id') or command != argv
            or execution.get('status') != 'failed' or execution.get('exitCode') != 3
            or output.get('truncated') is not False or not isinstance(output.get('text'), str)):
        _reject('Exact original supervisor exit transcript is required')
    decoder, text, messages = json.JSONDecoder(object_pairs_hook=protected._unique), output['text'], []
    while text.strip():
        item, end = decoder.raw_decode(text.lstrip()); messages.append(item); text = text.lstrip()[end:]
    final = messages[-1]
    cleanup = final.get('cleanup', {})
    if (final.get('format') != 'aos-supervisor-v1' or final.get('work_id') != request['work_id']
            or final.get('state') != 'held' or final.get('blocker') != 'unknown_effects'
            or cleanup.get('runtime_closed') is not True
            or cleanup.get('decision_service') != {'listener_stopped': True, 'requests_stopped': True}
            or cleanup.get('owned_stop') != {'known_stopped': False, 'unresolved_attempts': [request['attempt_id']],
                                            'unlaunched_intents': [], 'stop_conflicts': []}):
        _reject('Supervisor exit does not isolate exactly this failed assessor')
    review = _json_file(store, value['review'], immutable=True)
    core = {k: v for k, v in value.items() if k != 'review'}
    if (review.get('format') != FORMAT + '-review' or review.get('work_id') != request['work_id']
            or review.get('attempt_id') != request['attempt_id'] or review.get('evidence_digest') != digest(core)
            or review.get('verdict') != 'pass' or not review.get('author') or not review.get('reviewer')
            or review['author'] == review['reviewer'] or not review.get('reason')):
        _reject('Independent review must bind the exact producer, failure, state and owner evidence')
    return value, packet, final

def _classify(lifecycle, db, state, snapshot, request, packet):
    work, aid = request['work_id'], request['attempt_id']
    run, assignments = state['lifecycle'][0], state['assignments']
    contract = protected._json(run['contract'])
    failed = next((a for a in assignments if a['attempt_id'] == aid), None)
    writers = [a for a in assignments if a['kind'] == 'implementation' and a['state'] == 'completed']
    if (len(assignments) != 2 or len(writers) != 1 or not failed or failed['kind'] != 'assessment'
            or failed['state'] != 'blocked_unknown_effects' or failed['sent_at'] is None
            or any(failed[k] is not None for k in ('acknowledged_at', 'observed_active_at', 'runtime_model', 'runtime_effort', 'terminal_at', 'result'))
            or run['execution'] != 'held' or run['control'] != 'owned_stop_requested' or run['admitted'] != 1
            or run['cycle'] != 0 or run['delivery'] is not None or contract.get('execution_policy') != 'isolated-checkout-v1'
            or state['other_admitted'] or state['other_live'] or state['observations'] or snapshot['stop_conflicts']
            or any(not s.get('absent') for s in snapshot['failure_sidecars'])):
        _reject('Only the exact unacknowledged post-implementation assessment is eligible')
    writer, frozen, recipe = writers[0], protected._json(run['candidate']), contract.get('runtime_recipe')
    envelope = packet['concept']['execution_envelope']
    if (not isinstance(recipe, dict) or recipe != envelope.get('runtime_recipe') or envelope.get('runtime_recipe_digest') != digest(recipe)
            or lifecycle._approved_envelope_errors(db, work, contract) or failed['candidate'] != run['candidate']
            or failed['concept_digest'] != request['digest']
            or any(failed[k] != writer[k] for k in ('contract_revision', 'concept_revision', 'concept_digest'))
            or contract['revision'] != failed['contract_revision'] or contract['concept_revision'] != failed['concept_revision']
            or contract['concept_digest'] != failed['concept_digest']):
        _reject('Original approved contract or frozen candidate changed')
    approved = [d for d in state['decisions'] if d['digest'] == request['digest'] and d['outcome'] == 'approved']
    if len(approved) != 1:
        _reject('The original saved approval must remain present')
    readback = db.execute('SELECT status FROM decision_readback WHERE decision_id=?', (approved[0]['decision_id'],)).fetchone()
    if not readback or readback[0] != 'verified':
        _reject('Original approval readback is not verified')
    completions = [c for c in state['completions'] if c['assignment_id'] == writer['assignment_id']]
    if len(completions) != 1 or failed['predecessor'] != 'completion:' + completions[0]['receipt_id']:
        _reject('Assessment must directly follow the retained completed writer')
    result = protected._json(writer['result'])
    if (protected._json(completions[0]['result']) != result or result.get('candidate') != frozen
            or result.get('status') != 'completed' or not stopped_with_effects(result)):
        _reject('Writer candidate and positive cleanup evidence must agree')
    if len(state['attempts']) != 1 or state['attempts'][0]['id'] != writer['attempt_id']:
        _reject('Runtime journal must contain exactly the preceding writer and no assessment attempt')
    original = protected._json(state['attempts'][0]['record'])
    if (original.get('attempt_id') != writer['attempt_id'] or original.get('phase') != 'terminal'
            or original.get('recipe_digest') != digest(recipe) or original.get('terminal_observation', {}).get('result') != result
            or not stopped_with_effects(original.get('stop_receipt', {}))
            or state['denials'] or state['native_checks'] or state['guards']
            or any(p['attempt_id'] != writer['attempt_id'] for p in state['processes'])):
        _reject('Runtime evidence conflicts with the exact pre-admission failure')
    intents = [e for e in state['events'] if e['kind'] == 'launch_intent'
               and protected._json(e['detail']).get('assignment_id') == failed['assignment_id']]
    if len(intents) != 1:
        _reject('One durable launch intent is required')
    tail = [e for e in state['events'] if e['event_id'] >= intents[0]['event_id']]
    if ([e['kind'] for e in tail] != ['launch_intent', 'unknown_effects', 'owned_stop_requested', 'owned_stop_reconciled']
            or protected._json(tail[1]['detail']) != FAILURE
            or protected._json(tail[2]['detail']) != {'assignment_id': failed['assignment_id'], 'reason': 'supervisor_stopped',
                'detail': 'Foreground owner stopped; exact owned cleanup required'}):
        _reject('The original instruction-drift sequence is missing or contradictory')
    worker = state['worker']
    if (len(worker) != 1 or worker[0]['state'] != 'stopped' or worker[0]['epoch'] != protected._json(intents[0]['detail'])['epoch']
            or worker[0]['heartbeat'] < tail[-1]['created_at']):
        _reject('Exact original worker epoch has not positively stopped')
    stops = state['stops']
    if (len(stops) != 1 or stops[0]['attempt_id'] != aid or stops[0]['state'] != 'blocked_unknown_effects'
            or stops[0]['reason'] != 'supervisor_stopped' or protected._json(stops[0]['observation']) != protected._json(tail[-1]['detail'])
            or set(b['code'] for b in state['blockers']) != {'supervisor_stopped', 'unknown_effects'}):
        _reject('Recovery may clear only this exact failed stop and its two blockers')
    resources = [r for r in state['resources'] if r['work_id'] == work]
    if (set(r['resource_id'] for r in resources) != set(contract['resources'])
            or any(r['state'] != 'frozen' or r['assignment_id'] != failed['assignment_id'] for r in resources)):
        _reject('Exact frozen assessment resource ownership is required')
    return contract, recipe, frozen, failed, original, resources

def _observe(store, recipe, frozen, original, aid):
    start = time.monotonic()
    def remaining():
        left = OBSERVATION_SECONDS - (time.monotonic() - start)
        if left <= 0:
            _reject('Recovery observation exceeded its bounded window')
        return left
    source = Path(recipe['source'])
    if directory_identity(source) != original['startup_paths'][str(source)]:
        _reject('Source directory identity differs from original writer ownership')
    files = inventory(source, recipe=recipe, check_deadline=remaining)
    if {'identity': digest(files), 'source_identity': digest(files), 'inventory': files} != frozen:
        _reject('The frozen candidate changed after implementation')
    absent = [Path(recipe['outputs']) / aid, store.directory / 'checkout-probes' / aid]
    if any(p.exists() or p.is_symlink() for p in absent):
        _reject('Failed assessor has an unexplained output or probe directory')
    rows = process_groups(timeout=min(5, remaining()))
    known = dict(original.get('observed_processes', {}))
    if 'unavailable' in known or not original.get('server_history'):
        _reject('Original writer process ownership is incomplete')
    for server in original['server_history']:
        known[str(server['pid'])] = server['started']
    if any(int(pid) in rows and rows[int(pid)]['started'].strip() == birth.strip() for pid, birth in known.items()):
        _reject('An exact original writer process is still present')
    if directory_identity(source) != original['startup_paths'][str(source)]:
        _reject('Source identity changed during recovery observation')
    remaining()
    return {'candidate_identity': frozen['identity'], 'source_identity': directory_identity(source),
            'absent_assessor_paths': [str(p) for p in absent], 'known_writer_process_identities': len(known),
            'live_original_processes': [], 'observed_at': now(),
            'limits': 'Current equality and known process identities corroborate the retained producer/failure proof; no historical transient-effects claim.'}

def _recover(lifecycle, request, *, apply=False):
    AssessmentRecoveryRequest.model_validate(request, strict=True)
    if type(apply) is not bool:
        _reject('Apply must be explicit boolean')
    store = lifecycle.store
    if store.recovery_hold() or apply and store.read_only:
        _reject('A restored recovery copy or read-only store cannot release ownership')
    for key in ('operation_id', 'work_id', 'attempt_id'):
        identifier(request[key])
    payload = [FORMAT, request]
    with store._connection() as db:
        if prior := store._prior(db, request['operation_id'], payload):
            return prior
    with protected._worker_fence(store) as verify:
        evidence, packet, owner = _evidence(store, request)
        with protected._connection(store) as (db, ids):
            db.execute('BEGIN')
            state, snapshot = _state(lifecycle, db, request, ids)
            if snapshot != evidence['snapshot']:
                _reject('Reviewed state changed before recovery')
            contract, recipe, frozen, failed, original, resources = _classify(lifecycle, db, state, snapshot, request, packet)
        effects = _observe(store, recipe, frozen, original, request['attempt_id'])
        terminal = {'format': FORMAT + '-terminal', 'attempt_id': request['attempt_id'], 'executor': failed['executor'],
                    'status': 'failed', 'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                    'partial_mutations': {'status': 'none', 'changes': []}, 'model_calls_attempted': 0, 'thread_id': None, 'turn_id': None,
                    'failure': FAILURE, 'provenance': 'separately-derived-after-failure', 'original_admission_denial_existed': False,
                    'producer_source_digest': PRODUCER, 'observations': effects,
                    'proof': 'Pinned instruction rejection precedes mandatory reservation/attempt barrier; retained writer cleanup and supervisor exit agree.'}
        audit = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': request['work_id'],
                 'attempt_id': request['attempt_id'], 'candidate': frozen, 'original_contract': contract,
                 'original_prepared_packet': evidence['prepared_packet'], 'evidence_sha256': request['evidence_sha256'],
                 'evidence': evidence, 'snapshot_digest': digest(snapshot), 'snapshot': snapshot,
                 'original_records': {k: state[k] for k in ('assignments', 'stops', 'blockers', 'outbox', 'worker', 'lifecycle')},
                 'terminal_receipt': terminal, 'new_execution_authorized': False, 'charges_preserved': True}
        if len(canonical(audit).encode()) > protected.MAX_FILE_BYTES:
            _reject('Recovery audit exceeds its bounded size')
        if _evidence(store, request) != (evidence, packet, owner):
            _reject('Evidence changed during observation')
        verify()
        with protected._connection(store, write=apply) as (db, current_ids):
            db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            if prior := store._prior(db, request['operation_id'], payload):
                return prior
            if _state(lifecycle, db, request, current_ids)[1] != snapshot:
                _reject('Recovery compare-and-set rejected changed authoritative facts')
            if not apply:
                return {'ready': True, 'applied': False, 'work_id': request['work_id'], 'snapshot_digest': digest(snapshot),
                        'terminal_receipt': terminal, 'runtime_and_budget_modified': False, 'new_execution_authorized': False}
            lifecycle._fault('assessment_recovery_before_commit')
            # Repeat filesystem/process and protected evidence observations at the
            # final commit boundary; a preview does not lease mutable inputs.
            current = _observe(store, recipe, frozen, original, request['attempt_id'])
            if ({k: v for k, v in current.items() if k != 'observed_at'}
                    != {k: v for k, v in effects.items() if k != 'observed_at'}
                    or _evidence(store, request) != (evidence, packet, owner)):
                _reject('Evidence or ownership changed before recovery commit')
            if not _schema(db):
                db.execute(SCHEMA)
            stamp, aid, work = now(), request['attempt_id'], request['work_id']
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)', (request['operation_id'], aid, canonical(audit)))
            db.execute("UPDATE assignment SET state='failed',terminal_at=?,result=? WHERE assignment_id=?", (stamp, canonical(terminal), failed['assignment_id']))
            db.execute("UPDATE outbox SET state='blocked',updated_at=? WHERE assignment_id=?", (stamp, failed['assignment_id']))
            db.execute("UPDATE owned_stop SET state='stopped',observation=?,observed_at=? WHERE attempt_id=?", (canonical(terminal), stamp, aid))
            db.execute("UPDATE lifecycle SET execution='held',control='none',admitted=0 WHERE work_id=?", (work,))
            for resource in resources:
                if db.execute('DELETE FROM resource WHERE resource_id=? AND work_id=? AND assignment_id=?',
                              (resource['resource_id'], work, failed['assignment_id'])).rowcount != 1:
                    _reject('Frozen resource ownership changed')
            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code IN ('unknown_effects','supervisor_stopped')", (work,))
            lifecycle._block(db, work, 'assessment_recovered',
                             'Pre-admission assessment failure reconciled; original candidate, history and charges retained.',
                             'Prepare and approve an exact technical continuation; recovery grants no execution')
            receipt_id = 'assessment-recovery:' + protected._sha(request['operation_id'].encode())[:32]
            db.execute('INSERT INTO completion VALUES(?,?,?,?,?,?)', (receipt_id, failed['assignment_id'],
                       digest([failed['assignment_id'], terminal]), canonical(terminal), 'assessment_recovered_held', stamp))
            lifecycle._touch(db, work, 'assessment_recovered', {'operation_id': request['operation_id'],
                             'attempt_id': aid, 'audit_digest': digest(audit), 'charges_preserved': True})
            lifecycle._fault('assessment_recovery_after_updates')
            verify()
            result = {'applied': True, 'work_id': work, 'work_revision': store._work(db, work)['revision'],
                      'assignment_id': failed['assignment_id'], 'execution': 'held', 'audit_digest': digest(audit),
                      'terminal_receipt': terminal, 'runtime_and_budget_modified': False, 'new_execution_authorized': False}
            store._record(db, request['operation_id'], payload, result)
            db.commit()
            return result

def recover(lifecycle, request, *, apply=False):
    try:
        return _recover(lifecycle, request, apply=apply)
    except Rejected:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        raise Rejected('assessment_recovery', 'Exact recovery evidence or atomic journal access is unavailable') from error
