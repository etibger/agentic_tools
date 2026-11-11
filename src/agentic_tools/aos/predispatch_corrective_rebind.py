"""One explicit correction of an applied recovery after a settled runtime failure.

Historical effects remain evidence, never a zero-effects claim. Preparation and
preview grant nothing; only an exact saved decision can consume the new finite
diagnostic and metadata allowances. Application remains held and unadmitted.
"""
from __future__ import annotations

import copy
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

from pydantic import Field

from . import predispatch_recovery as recovery
from . import predispatch_feature_amendment as previous
from . import readiness, native_processes, owned_host_command
from . import startup_failure_recovery as fencing
from .coordinator import ConceptRequest
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-predispatch-corrective-rebind-v1'
TABLE = 'predispatch_corrective_rebind'
INTENT = TABLE + '_intent'
SCHEMA = {}
for name in (TABLE, INTENT):
    SCHEMA[name] = (f'CREATE TABLE {name} (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,'
        'parent_receipt_digest TEXT UNIQUE NOT NULL,record TEXT NOT NULL)')
    for action in ('update', 'delete'):
        SCHEMA[name + '_' + action] = (f'CREATE TRIGGER {name}_{action} BEFORE {action.upper()} ON {name} '
            "BEGIN SELECT RAISE(ABORT,'Corrective rebind history is immutable'); END")

Exact = recovery.Exact
Reference = recovery.Reference
ReceiptReference = recovery.ReceiptReference
ApplyRequest = recovery.ApplyRequest
QualifyRequest = ApplyRequest
VerifyRequest = ReceiptReference


class LockReference(Exact):
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)


class ProcessIdentity(Exact):
    pid: int = Field(ge=2)
    uid: int = Field(ge=0)
    birth: list[int] = Field(min_length=2, max_length=2)
    parent: int = Field(ge=0)
    group: int = Field(ge=1)
    status: int = Field(ge=1)


class CompletedCommand(Exact):
    name: str
    receipt: Reference
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str
    environment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class SettledFailure(Exact):
    operation_id: str
    observation: Reference
    failure: Reference
    effects: Reference
    settlement: Reference
    closed_owner: Reference
    controller_lock: LockReference
    commands: list[CompletedCommand] = Field(min_length=1, max_length=64)
    retained_outputs: list[Reference] = Field(min_length=1, max_length=256)
    preserved_files: list[Reference] = Field(min_length=1, max_length=64)
    absent_paths: list[str] = Field(min_length=1, max_length=64)
    preserved_process: ProcessIdentity
    failure_phase: str


class Diagnostic(Exact):
    argv: list[str] = Field(min_length=4, max_length=128)
    cwd: str
    environment: dict[str, str]
    sources: list[Reference] = Field(min_length=2, max_length=128)
    source_closure: Reference


class Allowances(recovery.Allowances):
    diagnostic_attempts: int = Field(ge=1, le=1)
    diagnostic_max_seconds: int = Field(ge=27, le=60)


class PrepareRequest(Exact):
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    decision_id: str
    applied_recovery: ReceiptReference
    applied_engine_directory: str
    original_controller_contract_path: str
    controller_contract_path: str
    controller_resource: str
    source_changes: list[recovery.SourceChange] = Field(min_length=1, max_length=128)
    additional_inputs: list[Reference] = Field(max_length=32)
    destination_directory: str
    concept_update_path: str
    recipe_path: str | None = None
    configuration_refresh: list[recovery.ConfigurationRefresh] = Field(default_factory=list, max_length=16)
    settled_failure: SettledFailure
    diagnostic: Diagnostic
    allowances: Allowances


def require(value, message):
    if not value:
        raise Rejected('predispatch_corrective_rebind', message)


def _schema(db, create=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'predispatch_corrective_rebind*' OR "
        "(tbl_name IN (?,?) AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))", (TABLE, INTENT)).fetchall())
    require(not found or found == SCHEMA, 'Corrective history schema differs')
    if not found and create:
        for sql in SCHEMA.values():
            db.execute(sql)
        found = SCHEMA
    return bool(found)


def _read(store, ref, *, immutable=True, raw=False):
    previous._protected(store, ref['path'])
    value, actual = (previous._file if raw else previous._json)(ref['path'], immutable=immutable)
    require(actual == ref, 'Retained correction evidence changed')
    return value


def _parent(store, reference, engine_directory, *, live=True):
    """Prove historical applied recovery without requiring it remain current."""
    ReceiptReference.model_validate(reference, strict=True)
    with store._connection() as db:
        require(recovery._schema(db), 'Applied recovery history is absent')
        row = db.execute(f'SELECT record FROM {recovery.TABLE} WHERE operation_id=? AND work_id=?',
            (reference['operation_id'], reference['work_id'])).fetchone()
        require(row is not None, 'Only an applied supplemental recovery can be corrected')
        receipt = json.loads(row[0])
        require(receipt.get('format') == recovery.FORMAT and digest(receipt) == reference['receipt_digest'] and
            'predispatch_corrective_rebind' not in receipt['registered_contract'], 'Correction cannot branch or correct a correction')
        operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (reference['operation_id'],)).fetchone()
        require(operation and json.loads(operation[0]) == {'applied': True, 'receipt_digest': digest(receipt),
            'work_id': reference['work_id']}, 'Applied parent operation differs')
        row = db.execute('SELECT package FROM concept WHERE work_id=? AND digest=?',
            (reference['work_id'], receipt['to_concept_digest'])).fetchone()
        require(row is not None, 'Applied parent concept missing')
        package = json.loads(row[0])
        require(receipt['registered_contract'] == {**package['execution_envelope'], 'concept_revision': package['revision'],
            'concept_digest': receipt['to_concept_digest']}, 'Applied parent contract differs')
    manifest = recovery.source_manifest(engine_directory)
    require(digest(manifest) == receipt['engine_digest'], 'Exact historical applied engine changed')
    packet = _read(store, receipt['packet'])
    require(digest(packet) == receipt['packet_digest'], 'Historical applied packet changed')
    identity = packet['metadata']['runtime_identity']
    require(identity['runtime_source_digest'] == digest(manifest) and
        {k: v for k, v in readiness._runtime_identity().items() if k != 'runtime_source_digest'} ==
        {k: v for k, v in identity.items() if k != 'runtime_source_digest'}, 'Historical interpreter or runtime provenance changed')
    recovery._validate_packet(store, packet, snapshot=False, historical_runtime_identity=identity, observe_cleanup=live)
    qualification = _read(store, receipt['qualification'])
    recovery._qualification_result(store, packet, receipt['packet'], receipt['apply_request'], qualification,
        historical_runtime_identity=identity, historical_engine_manifest=manifest, observe_cleanup=live)
    previous._approval(store, receipt['to_decision_id'], receipt['work_id'], receipt['to_concept_digest'])
    return receipt, packet


@contextmanager
def _controller_fence(store, reference):
    path = previous._protected(store, reference['path'])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1 and
            not stat.S_IMODE(before.st_mode) & 0o077 and (before.st_dev, before.st_ino) ==
            (reference['device'], reference['inode']), 'Original controller fence identity differs')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('predispatch_corrective_rebind', 'Original controller remains owned') from error
        def check():
            after = path.stat()
            require((after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), 'Controller fence was replaced')
        check()
        yield check
        check()
    finally:
        os.close(fd)


def _settled_failure(store, request, parent, accounting, *, live=True, snapshot=True, preserved_snapshots=None):
    proof = request['settled_failure']
    observation = _read(store, proof['observation'])
    failure = _read(store, proof['failure']); effects = _read(store, proof['effects'])
    settlement = _read(store, proof['settlement']); closed = _read(store, proof['closed_owner'])
    supporting = [r for key, r in observation.get('references', {}).items() if key != 'clock']
    supporting.extend(observation.get('effects', {}).get('retained_reference_checks', []))
    supporting.extend(effects[k] for k in ('identity_diagnostic','unrelated_before') if k in effects)
    seen = set()
    for entry in supporting:
        ref = {k: entry[k] for k in ('path','sha256')}
        if (ref['path'], ref['sha256']) not in seen:
            _read(store, ref, immutable=False, raw=True)
            seen.add((ref['path'], ref['sha256']))
    if 'clock' in observation.get('references', {}):
        require(observation['references']['clock'] == accounting['clock_reference'], 'Settled observation clock pin differs')
    require(all(observation.get('references', {}).get(k) == proof[k]
        for k in ('failure','effects','settlement','closed_owner')), 'Independent settled observation references differ')
    parent_copy = _read(store, observation['references']['applied_receipt'], immutable=False)
    require(parent_copy == parent or parent_copy.get('receipt') == parent,
        'Independent settled observation identifies another applied recovery')
    old_vm = observation['old_vm']; preserved = proof['preserved_process']
    require(old_vm['observation']['identity'] == effects.get('old_vm', {}).get('identity') == preserved and
        old_vm['config']['sha256'] == effects['old_vm'].get('config_sha256') and old_vm['config'] in proof['preserved_files'] and
        sorted(proof['absent_paths']) == sorted(effects.get('absent_paths', [])) == sorted(old_vm['absence_checks']) and
        all(v is True for v in old_vm['absence_checks'].values()), 'Preserved runtime or lifecycle boundaries differ from retained observations')
    refs = {r['path']: r for r in proof['preserved_files']}
    require(len(refs) == len(proof['preserved_files']) and all(str(Path(old_vm['config']['path']).parent / name) in refs
        and str(pid) == str(preserved['pid']) for name, pid in old_vm['pidfiles'].items()), 'Preserved PID files differ')
    lock = proof['controller_lock']
    require(any(r['path'] == lock['path'] and all(r['before'][k] == lock[k] for k in ('device','inode'))
        for r in observation['locks']), 'Controller fence differs from positive settlement observation')
    operation = accounting['operations'].get(proof['operation_id'])
    require(operation and operation['result'] is not None and
        operation['reservation'].get('authority_receipt_digest') == digest(parent) and
        operation['reservation']['slot'] in parent['runtime_slots'], 'Failure lacks a settled parent-owned runtime charge')
    start = operation['reservation']; end = operation['result']['completed_at']
    require(start['context']['kind'] == 'coordinator' and start['operation'] == proof['operation_id'] and
        start['seconds'] == parent['runtime_slots'][start['slot']], 'Failed runtime allowance must remain fully charged')
    owner_pid = effects.get('owner_pid')
    require(type(owner_pid) is int and owner_pid > 1 and
        failure.get('owner_pid') == settlement.get('owner_pid') == closed.get('owner_pid') == owner_pid,
        'Failed owner identities disagree')
    require(failure.get('lifecycle_command_started') is False and failure.get('automatic_retry') is False and
        failure.get('phase') == proof['failure_phase'] and failure.get('clock', {}).get('open') ==
        {'id': start['operation'], 'reserved': start['seconds'], 'started_at': start['started_at']} and
        start['started_at'] <= failure['observed_at'] <= end, 'Failure is not the exact pre-lifecycle runtime observation')
    require(effects.get('outcome') == 'held-before-lifecycle' and effects.get('runtime_qualified') is False and
        effects.get('all_launched_commands_ended') is True and effects.get('failure') == proof['failure'] and
        settlement.get('outcome') == 'failed-held' and settlement.get('runtime_qualified') is False and
        settlement.get('new_attempt_authorized') is False and settlement.get('full_charge_seconds') == start['seconds'] and
        settlement.get('completion') == operation['completion'] and settlement.get('evidence') == proof['effects'],
        'Known effects and terminal settlement do not prove the failed operation')
    require(type(closed.get('foreground_session_exit_code')) is int and closed['foreground_session_exit_code'] == 0 and
        type(closed.get('assignments')) is int and closed['assignments'] == 0 and
        closed.get('lifecycle', {}).get('execution') == 'held' and closed['lifecycle'].get('control') == 'none' and
        type(closed['lifecycle'].get('admitted')) is int and closed['lifecycle']['admitted'] == 0 and closed.get('clock') == settlement.get('clock') and
        settlement['clock'].get('open') is None and settlement['clock']['spent'] >= start['seconds'],
        'Original foreground owner has no positive terminal held readback')
    if snapshot:
        require(settlement['clock'] == accounting['clock'], 'Settled failure accounting changed')
    commands = proof['commands']; names = [c['name'] for c in commands]
    require(len(set(names)) == len(names) and set(effects.get('commands', {})) == set(names) and
        sorted(effects.get('attempted_roles', [])) == sorted(names), 'Partial or unknown command effects remain')
    roots = {Path(c['receipt']['path']).parent for c in commands}
    require(len(roots) == 1, 'All failed-operation command evidence must share its retained namespace')
    output_root = roots.pop(); previous._protected(store, output_root, directory=True)
    paths = list(output_root.rglob('*'))
    require(len(paths) <= 1024 and not any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths),
        'Retained output namespace contains links or special files')
    output_paths = {str(p) for p in paths if p.is_file()}
    require(len(proof['retained_outputs']) == len({r['path'] for r in proof['retained_outputs']}) and
        output_paths == {r['path'] for r in proof['retained_outputs']}, 'Complete retained output inventory is required')
    controller = None
    for command in commands:
        declared = effects['commands'][command['name']]
        actual = _read(store, command['receipt'], immutable=False)
        require(Path(actual['control']).is_relative_to(output_root), 'Command executor evidence escaped its retained namespace')
        require(declared == {'reference': command['receipt'], 'passed': True, 'cleanup_verified': True,
            'child': {'exit_code': 0}}, 'Effect summary contradicts its completed command')
        require(any(all(r.get(k) == command['receipt'][k] for k in ('path','sha256'))
            for r in observation['effects']['retained_reference_checks']), 'Command is absent from independent retained effect inventory')
        require(actual.get('format') == 'owned-host-command-v1' and actual.get('controller_finished') is True and
            actual.get('launch_authorized') is True and actual.get('boundary_rechecked') is True and
            actual.get('passed') is True and actual.get('timed_out') is False and actual.get('errors') == [] and
            type(actual.get('child', {}).get('exit_code')) is int and actual['child']['exit_code'] == 0 and
            all(actual.get(k) == command[k] for k in ('argv', 'cwd', 'environment_sha256')) and
            start['started_at'] <= actual['started_at'] <= actual['checkpoint_at'] <= end,
            'Retained command profile or reservation chronology differs')
        require(actual.get('controller', {}).get('pid') == owner_pid, 'Command belongs to another foreground owner')
        ProcessIdentity.model_validate(actual['controller'], strict=True)
        require(actual['controller']['birth'][0] > 0 and 0 <= actual['controller']['birth'][1] < 1000000,
            'Historical foreground birth identity is invalid')
        if controller is None:
            controller = actual['controller']
        require(actual['controller'] == controller, 'Command foreground birth identity changed')
        # The maintained verifier checks its independently retained executor
        # receipt, positive reap/EOF/group evidence, and exact absent identities.
        if live:
            try:
                require(owned_host_command.verify_completed(actual) == actual, 'Completed command evidence differs')
            except (OSError, RuntimeError, ValueError) as error:
                raise Rejected('predispatch_corrective_rebind', 'Known command cleanup is not independently verified') from error
        else:
            require(previous._json(Path(actual['control']) / 'receipt.json')[0] == actual,
                'Retained executor receipt differs')
    for ref in proof['retained_outputs']:
        _read(store, ref, immutable=False, raw=True)
    retained = []
    for ref in proof['preserved_files']:
        if preserved_snapshots is None:
            _ordinary_pin(store, ref)
            item = {'reference': ref, 'bytes_base64': base64.b64encode(previous._file(ref['path'])[0]).decode()}
        else:
            matches = [r for r in preserved_snapshots if r['reference'] == ref]
            require(len(matches) == 1, 'Historical runtime file snapshot missing')
            item = matches[0]
        raw = base64.b64decode(item['bytes_base64'], validate=True)
        require(hashlib.sha256(raw).hexdigest() == ref['sha256'], 'Historical runtime byte snapshot changed')
        if snapshot:
            _ordinary_pin(store, ref)
        if Path(ref['path']).name in old_vm['pidfiles']:
            require(raw.decode().strip() == str(preserved['pid']), 'Historical PID-file bytes differ')
        retained.append(item)
    require(len(set(proof['absent_paths'])) == len(proof['absent_paths']), 'Duplicate absence boundary')
    if snapshot:
        for name in proof['absent_paths']:
            path = Path(name)
            require(path.is_absolute() and not path.exists() and not path.is_symlink(), 'A forbidden lifecycle output appeared')
    if live:
        try:
            current_owner = native_processes.identity(owner_pid)
            if current_owner is not None:
                ProcessIdentity.model_validate(current_owner, strict=True)
                require(current_owner['pid'] == owner_pid and current_owner['birth'][0] > 0 and
                    0 <= current_owner['birth'][1] < 1000000 and current_owner['birth'] != controller['birth'],
                    'Original foreground owner has not ended or its identity is contradictory')
            if snapshot:
                current = native_processes.identity(proof['preserved_process']['pid'])
                require(current is not None and current.get('status') != 5 and
                    all(current[k] == proof['preserved_process'][k] for k in ('pid','uid','birth','group','parent')),
                    'Preserved runtime identity changed before correction')
        except (OSError, RuntimeError, ValueError) as error:
            raise Rejected('predispatch_corrective_rebind', 'Exact owner or preserved runtime identity is unreadable') from error
    return {'proof': proof, 'operation': operation, 'controller_identity': controller,
        'known_effect_count': len(commands), 'zero_effects': False, 'preserved_file_snapshots': retained}


def _ordinary_pin(store, ref):
    path = Path(ref['path'])
    require(all(not path.is_relative_to(root) and not Path(root).is_relative_to(path)
        for root in store.worker_writable_roots), 'Pinned trusted input overlaps worker scope')
    require(previous._file(path)[1] == ref, 'Pinned ordinary input changed')


def _diagnostic_plan(store, request):
    plan = request['diagnostic']; env = plan['environment']; argv = plan['argv']
    require(set(env) <= {'PATH','LANG','LC_ALL','TMPDIR','PYTHONPATH','PYTHONDONTWRITEBYTECODE'} and
        isinstance(env.get('TMPDIR'), str) and env['TMPDIR'] and Path(env['TMPDIR']).is_dir() and
        all(isinstance(v, str) and '\x00' not in v for v in env.values()), 'Diagnostic needs an exact narrow environment and TMPDIR')
    require(Path(plan['cwd']).is_absolute() and Path(plan['cwd']).resolve() == Path(plan['cwd']) and Path(plan['cwd']).is_dir(),
        'Diagnostic cwd must be exact')
    refs = {r['path']: r for r in plan['sources']}
    require(len(refs) == len(plan['sources']) and argv[1:3] == ['-I','-B'] and argv[0] in refs and
        argv[3] in refs and Path(argv[3]).suffix == '.py' and
        all(isinstance(a, str) and a and '\x00' not in a for a in argv), 'Diagnostic requires pinned Python and script sources')
    closure_ref = plan['source_closure']; _ordinary_pin(store, closure_ref)
    closure = recovery._closure(store, closure_ref['path'])
    expected = {r['path']: {k: r[k] for k in ('path','sha256')} for r in closure['files']}
    require(argv[3] in expected and argv[0] not in expected and closure_ref['path'] not in expected,
        'Diagnostic closure must include its script without interpreter or self-reference')
    expected.update({argv[0]: refs[argv[0]], closure_ref['path']: closure_ref})
    require(refs == expected, 'Diagnostic source inventory must bind the complete exact closure')
    require(argv[0] == sys.executable and os.access(argv[0], os.X_OK),
        'Diagnostic must preserve the approved lexical coordinator interpreter')
    executable = Path(argv[0]); resolved = executable.resolve(strict=True)
    require(all(not p.is_relative_to(root) and not Path(root).is_relative_to(p)
        for p in (executable, resolved, Path(sys.prefix), Path(sys.base_prefix)) for root in store.worker_writable_roots),
        'Diagnostic interpreter authority overlaps worker scope')
    before = resolved.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.getuid()) and
        before.st_size <= 128 * 1024 * 1024 and not before.st_mode & 0o022, 'Diagnostic executable is not trusted')
    binary = resolved.read_bytes(); after = resolved.stat()
    require(all(getattr(before, k) == getattr(after, k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) and
        hashlib.sha256(binary).hexdigest() == refs[argv[0]]['sha256'], 'Diagnostic executable changed')
    for ref in refs.values():
        if ref['path'] != argv[0]:
            _ordinary_pin(store, ref)
    identity = {'path': argv[0], 'resolved': str(resolved), 'sha256': refs[argv[0]]['sha256'],
        'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
        'links': [{'path': str(p), 'target': os.readlink(p)} for p in (executable, *executable.parents) if p.is_symlink()]}
    config = Path(sys.prefix) / 'pyvenv.cfg'
    identity['venv_config'] = None if not config.exists() else hashlib.sha256(config.read_bytes()).hexdigest()
    return {**copy.deepcopy(plan), 'interpreter_identity': identity}


def _allowances(parent, allowances, accounting):
    slots = allowances['runtime_slots']
    require(type(slots) is dict and len(slots) == 1 and all(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', k) and
        not k.startswith('qualification-') and type(v) is int and v == 3600 for k, v in slots.items()),
        'Correction permits exactly one distinct 3600-second coordinator slot')
    plan = parent['coordinator_reservation_plan']
    require(not set(slots) & (set(plan['slots']) | set(accounting['operations'])) and
        allowances['effective_limit_seconds'] == parent['effective_limit_seconds'] + 3600,
        'Correction cannot reuse a slot or change the exact additional ceiling')
    planned = plan['planned_lifetime_seconds'] + 3600
    require(planned <= allowances['effective_limit_seconds'], 'Full no-refund plan exceeds correction ceiling')
    return {**copy.deepcopy(plan), 'slots': {**plan['slots'], **slots}, 'planned_lifetime_seconds': planned,
        'effective_limit_seconds': allowances['effective_limit_seconds'],
        'unallocated_seconds': allowances['effective_limit_seconds'] - planned}


def _concept(m, request):
    old = m['original_contract']; envelope = {k: copy.deepcopy(v) for k,v in old.items() if k not in ('concept_revision','concept_digest')}
    resource = request['controller_resource']; original_digest = digest(m['original_controller'])
    require(resource in old['resources'] and resource.endswith(original_digest), 'Exact applied controller resource required')
    envelope.update(revision=old['revision'] + 1, runtime_recipe=m['recipe'], runtime_recipe_digest=digest(m['recipe']),
        resources=[resource[:-len(original_digest)] + digest(m['controller_contract']) if r == resource else r for r in old['resources']],
        prerequisites={k: False for k in old['prerequisites']},
        predispatch_corrective_rebind={'operation_id': request['operation_id'], 'metadata_digest': digest(m),
            'allowances': copy.deepcopy(request['allowances'])})
    a = request['allowances']
    envelope['authority'] += (' Corrective rebind ' + request['operation_id'] + ' grants one new metadata attempt of at most ' +
        str(a['metadata_max_seconds']) + ' seconds, one separately reserved read-only diagnostic of at most ' +
        str(a['diagnostic_max_seconds']) + ' seconds and one new coordinator-only reservation (seconds): ' + canonical(a['runtime_slots']) +
        '. The prospective effective coordinator lifetime ceiling is ' + str(a['effective_limit_seconds']) +
        ' seconds. All known retained effects, prior decisions, consumed attempts and charges remain preserved, including the failed runtime slot. '
        'Original approval start, deadline, product envelope and unused original slots remain unchanged. '
        'No refund, reuse, automatic retry, admission, further correction or runtime qualification is authorized by applying this rebind.')
    update = m['concept_update']
    require(set(update) == {'body','decision_context','recommendation','alternative','consequence'}, 'Only the technical decision brief may change')
    concept = {k: copy.deepcopy(v) for k,v in m['original_package'].items() if k not in ('format','execution_envelope')}
    concept.update(update)
    concept.update(operation_id=request['operation_id'] + '-present', expected_work_revision=request['expected_work_revision'],
        revision=m['original_package']['revision'] + 1, execution_envelope=envelope, authority=envelope['authority'])
    return concept


def _metadata(lifecycle, request, *, live=True):
    store = lifecycle.store
    parent, parent_packet = _parent(store, request['applied_recovery'], request['applied_engine_directory'], live=live)
    destination = previous._protected(store, request['destination_directory'], directory=True)
    historical_roots = {Path(parent_packet['request']['destination_directory']),
        Path(parent['original_amendment_receipt']['packet']['path']).parent,
        *(Path(c['receipt']['path']).parent for c in request['settled_failure']['commands'])}
    if parent_packet['request'].get('predecessor') is not None:
        historical_roots.add(Path(parent_packet['request']['predecessor']['packet']['path']).parent)
    require(all(not destination.is_relative_to(root) and not root.is_relative_to(destination) for root in historical_roots),
        'Correction must preserve every historical namespace and use a distinct destination')
    require(parent['work_id'] == request['work_id'] and parent['to_decision_id'] == request['decision_id'] and
        parent['to_concept_digest'] == request['digest'], 'Correction must bind the exact applied current decision')
    decision = previous._approval(store, request['decision_id'], request['work_id'], request['digest'])
    accounting = (recovery.current_accounting if live else recovery._current_accounting_locked)(store, parent, settled=True)
    old, _ = previous._json(request['original_controller_contract_path'])
    require(old == parent_packet['metadata']['controller_contract'], 'Corrective baseline is not the applied controller')
    require(request['settled_failure']['controller_lock']['path'] == str(Path(old['control_root']) / 'controller.lock'),
        'Correction must fence the actual applied controller lock')
    new, new_ref = recovery._controller(store, old, request, marker='corrective_rebind')
    update, update_ref = previous._json(previous._protected(store, request['concept_update_path']))
    with fencing._connection(store) as (db, _):
        lifecycle._command_guard(db, request['work_id'], request['expected_work_revision'], request['digest'])
        contract = previous._passive(lifecycle, db, request['work_id']); previous._empty_product(db, contract)
        package = json.loads(decision['package_snapshot'])
        require(contract == previous._effective_contract(db, package, parent['to_concept_digest'], floor=parent['prerequisite_event_floor']),
            'Current registered applied authority differs')
        require('predispatch_corrective_rebind' not in contract, 'A correction cannot receive another correction')
        budget = previous._budget_snapshot(db)
    recipe, recipe_ref = recovery._recipe(store, contract['runtime_recipe'], request)
    require(not request.get('configuration_refresh') and recipe == contract['runtime_recipe'],
        'Corrective rebind must preserve the exact applied recipe and configuration')
    return {'work_id': request['work_id'], 'operation_id': request['operation_id'], 'state_dir': str(store.directory),
        'worker_roots': list(map(str, store.worker_writable_roots)), 'original_contract': contract,
        'original_package': package, 'original_controller': old, 'controller_contract': new, 'controller_reference': new_ref,
        'parent_recovery_receipt': parent, 'accounting_origin': parent['accounting_origin'], 'accounting': accounting,
        'original_budget': budget, 'coordinator_reservation_plan': _allowances(parent, request['allowances'], accounting),
        'concept_update': update, 'concept_update_reference': update_ref,
        'source_candidate': previous._baseline(recipe, old), 'recipe': recipe, 'recipe_reference': recipe_ref,
        'runtime_identity': readiness._runtime_identity(),
        'metadata_preflight': recovery._metadata_preflight(store, request, recipe, contract),
        'diagnostic_plan': _diagnostic_plan(store, request),
        'settled_failure': _settled_failure(store, request, parent, accounting, live=live)}


def _validate_packet(store, packet, *, snapshot=True, live=True):
    require(packet.get('format') == FORMAT, 'Unsupported corrective packet')
    request = packet['request']; PrepareRequest.model_validate(request, strict=True); m = packet['metadata']
    parent, _ = _parent(store, request['applied_recovery'], request['applied_engine_directory'], live=live)
    require(parent == m['parent_recovery_receipt'] and readiness._runtime_identity() == m['runtime_identity'] and
        m['state_dir'] == str(store.directory) and m['worker_roots'] == list(map(str, store.worker_writable_roots)),
        'Applied history, current engine or ledger differs')
    require(recovery._controller(store, m['original_controller'], request, marker='corrective_rebind') ==
        (m['controller_contract'], m['controller_reference']) and
        previous._json(request['original_controller_contract_path'])[0] == m['original_controller'] and
        previous._json(request['concept_update_path']) == (m['concept_update'], m['concept_update_reference']), 'Exact source or decision delta changed')
    require(recovery._recipe(store, m['original_contract']['runtime_recipe'], request) == (m['recipe'], m['recipe_reference']) and
        recovery._metadata_preflight(store, request, m['recipe'], m['original_contract']) == m['metadata_preflight'] and
        _diagnostic_plan(store, request) == m['diagnostic_plan'], 'Exact prospective profile changed')
    require(not request.get('configuration_refresh') and m['recipe'] == m['original_contract']['runtime_recipe'],
        'Corrective rebind cannot change recipe or configuration')
    require(_allowances(parent, request['allowances'], m['accounting']) == m['coordinator_reservation_plan'] and
        packet['concept'] == _concept(m, request), 'Corrective authority changed')
    require(request['settled_failure']['controller_lock']['path'] == str(Path(m['original_controller']['control_root']) / 'controller.lock'),
        'Correction controller fence differs from applied authority')
    require(_settled_failure(store, request, parent, m['accounting'], live=live, snapshot=snapshot,
        preserved_snapshots=m['settled_failure']['preserved_file_snapshots']) == m['settled_failure'],
        'Settled failure lineage changed')
    require(recovery.time_valid(m['accounting_origin']['deadline']), 'Original feature deadline expired')
    if snapshot:
        recovery._snapshot_unchanged(m)
        require(previous._baseline(m['recipe'], m['original_controller']) == m['source_candidate'], 'Product source changed')


def preview(lifecycle, request):
    """Static implementation review; never an intent, approval or live proof."""
    PrepareRequest.model_validate(request, strict=True)
    m = _metadata(lifecycle, request, live=False)
    packet = {'format': FORMAT, 'request': request, 'metadata': m, 'concept': _concept(m, request)}
    ConceptRequest.model_validate(packet['concept'], strict=True)
    _validate_packet(lifecycle.store, packet, live=False)
    return {'packet': packet, 'approval_recorded': False, 'intent_created': False,
        'live_gates_deferred': ['controller-worker-accounting-fences', 'exact-owner-absence', 'maintained-command-cleanup', 'preserved-runtime-identity']}


@contextmanager
def _fences(store, request, origin):
    with _controller_fence(store, request['settled_failure']['controller_lock']) as controller:
        with fencing._worker_fence(store) as worker:
            with previous._accounting_lock(store, origin['clock_path']) as clock:
                def check():
                    controller(); worker(); clock()
                yield check


def prepare(lifecycle, request):
    PrepareRequest.model_validate(request, strict=True)
    identifier(request['operation_id']); identifier(request['work_id']); store = lifecycle.store
    destination = previous._protected(store, request['destination_directory'], directory=True)
    with store._connection() as db:
        if _schema(db):
            row = db.execute(f'SELECT record FROM {INTENT} WHERE work_id=?', (request['work_id'],)).fetchone()
            if row:
                ref = json.loads(row[0])['packet']; packet = _read(store, ref)
                require(packet['request'] == request, 'Another correction already owns this work')
                _validate_packet(store, packet)
                return {'packet_path': ref['path'], 'packet_sha256': ref['sha256'], 'packet': packet}
    m = _metadata(lifecycle, request)
    packet = {'format': FORMAT, 'request': request, 'metadata': m, 'concept': _concept(m, request)}
    ConceptRequest.model_validate(packet['concept'], strict=True)
    with _fences(store, request, m['accounting_origin']) as fence:
        with fencing._connection(store, write=True) as (db, _):
            db.execute('BEGIN IMMEDIATE')
            lifecycle._command_guard(db, request['work_id'], request['expected_work_revision'], request['digest'])
            require(previous._passive(lifecycle, db, request['work_id']) == m['original_contract'] and
                previous._budget_snapshot(db) == m['original_budget'], 'Current registered authority changed')
            previous._empty_product(db, m['original_contract'])
            _validate_packet(store, packet)
            _schema(db, create=True)
            require(not db.execute(f'SELECT 1 FROM {INTENT} WHERE work_id=? OR parent_receipt_digest=?',
                (request['work_id'], request['applied_recovery']['receipt_digest'])).fetchone(), 'Concurrent correction owns this parent')
            ref = previous._retain(destination / 'packet.json', packet)
            db.execute(f'INSERT INTO {INTENT} VALUES(?,?,?,?)', (request['operation_id'], request['work_id'],
                request['applied_recovery']['receipt_digest'], canonical({'packet': ref})))
            fence(); db.commit()
    return {'packet_path': ref['path'], 'packet_sha256': ref['sha256'], 'packet': packet}


def _load(store, request):
    ApplyRequest.model_validate(request, strict=True)
    packet, ref = previous._json(previous._protected(store, request['packet_path']), immutable=True)
    require(ref['sha256'] == request['packet_sha256'] and packet['request']['operation_id'] == request['operation_id'] and
        packet['request']['work_id'] == request['work_id'] and digest(previous._package(packet['concept'])) == request['digest'],
        'Request does not identify the exact corrective packet')
    with store._connection() as db:
        require(_schema(db), 'Corrective intent missing')
        row = db.execute(f'SELECT record FROM {INTENT} WHERE operation_id=? AND work_id=?',
            (request['operation_id'], request['work_id'])).fetchone()
        require(row and json.loads(row[0]) == {'packet': ref}, 'Corrective intent differs')
    return packet, ref, previous._approval(store, request['decision_id'], request['work_id'], request['digest'])


def _guard(lifecycle, db, request, packet, decision):
    recovery._guard(lifecycle, db, request, packet, decision)


def _reservation(packet, ref, request):
    return {'format': FORMAT, 'packet': ref, 'request': request, 'decision_id': request['decision_id'],
        'allowances': {k: v for k,v in packet['request']['allowances'].items() if k.startswith(('metadata_', 'diagnostic_'))}}


def _diagnostic_environment(packet, guard_ref):
    return {**packet['metadata']['diagnostic_plan']['environment'], 'AOS_CORRECTIVE_PACKET_DIGEST': digest(packet),
        'AOS_CORRECTIVE_GUARD_PATH': guard_ref['path'], 'AOS_CORRECTIVE_GUARD_SHA256': 'read-retained-guard'}


def _diagnostic_guard(guard, packet, packet_ref, request, reservation_ref):
    fields = {'format','packet','packet_digest','request','reservation','started_monotonic','deadline_monotonic',
        'cleanup_deadline_monotonic','feature_deadline','supervisor','coordinator'}
    require(set(guard) == fields and guard['format'] == 'aos-corrective-diagnostic-guard-v1' and
        guard['packet'] == packet_ref and guard['packet_digest'] == digest(packet) and guard['request'] == request and
        guard['reservation'] == reservation_ref and guard['feature_deadline'] == packet['metadata']['accounting_origin']['deadline'],
        'Diagnostic guard differs from exact qualification authority')
    for key in ('started_monotonic','deadline_monotonic','cleanup_deadline_monotonic','feature_deadline'):
        require(type(guard[key]) in (int,float) and math.isfinite(guard[key]), 'Diagnostic guard has an invalid deadline')
    duration = packet['request']['allowances']['diagnostic_max_seconds']
    require(guard['cleanup_deadline_monotonic'] == guard['started_monotonic'] + duration and
        guard['deadline_monotonic'] == guard['cleanup_deadline_monotonic'] - owned_host_command.CLEANUP_RESERVE_SECONDS,
        'Diagnostic guard changed its original total or cleanup reserve')
    for key in ('supervisor','coordinator'):
        ProcessIdentity.model_validate(guard[key], strict=True)


def diagnostic_authority(path, sha256):
    """Read the exact reserved diagnostic authority inside its owned child only.

    The coordinator holds all fences while this helper runs. This read does not
    reserve, approve, reconcile, scan processes or qualify runtime ownership.
    """
    from .store import Store
    from .lifecycle import Lifecycle
    guard, ref = previous._json(path, immutable=True)
    require(ref['sha256'] == sha256 and guard.get('format') == 'aos-corrective-diagnostic-guard-v1', 'Exact diagnostic guard required')
    packet, packet_ref = previous._json(guard['packet']['path'], immutable=True)
    require(packet_ref == guard['packet'] and digest(packet) == guard['packet_digest'], 'Diagnostic packet differs')
    m = packet['metadata']; store = Store(Path(m['state_dir']), tuple(map(Path, m['worker_roots'])), read_only=True)
    lifecycle = Lifecycle.for_reading(store); loaded, loaded_ref, decision = _load(store, guard['request'])
    _diagnostic_guard(guard, packet, packet_ref, guard['request'], guard['reservation'])
    require(loaded == packet and loaded_ref == packet_ref and _read(store, guard['reservation']) ==
        _reservation(packet, packet_ref, guard['request']), 'Diagnostic lacks its exact consumed saved approval')
    destination = Path(packet['request']['destination_directory'])
    require(ref['path'] == str(destination / 'diagnostic-guard.json') and guard['reservation']['path'] ==
        str(destination / 'qualification-reservation.json') and not (destination / 'diagnostic-result.json').exists() and
        not (destination / 'qualification-result.json').exists(), 'Diagnostic allowance has ended')
    require(type(guard['deadline_monotonic']) in (int,float) and type(guard['cleanup_deadline_monotonic']) in (int,float) and
        time.monotonic() < guard['deadline_monotonic'] < guard['cleanup_deadline_monotonic'] and
        guard['cleanup_deadline_monotonic'] - guard['started_monotonic'] <= packet['request']['allowances']['diagnostic_max_seconds'] and
        time.time() < guard['feature_deadline'] == m['accounting_origin']['deadline'], 'Original diagnostic deadline ended')
    require(readiness._runtime_identity() == m['runtime_identity'] and
        _diagnostic_plan(store, packet['request']) == m['diagnostic_plan'], 'Diagnostic source or launch identity changed')
    with fencing._connection(store) as (db, _):
        _guard(lifecycle, db, guard['request'], packet, decision)
    parent = native_processes.identity(os.getppid())
    coordinator = native_processes.identity(guard['coordinator']['pid'])
    require(native_processes.same(guard['supervisor'], parent) and parent['status'] != 5 and
        parent['parent'] == guard['coordinator']['pid'] and parent['group'] == guard['supervisor']['group'] and
        native_processes.same(guard['coordinator'], coordinator) and coordinator['status'] != 5,
        'Diagnostic is outside its original owned process chain')
    return {'packet_digest': digest(packet), 'deadline_monotonic': guard['deadline_monotonic'],
        'cleanup_deadline_monotonic': guard['cleanup_deadline_monotonic'], 'uid': os.getuid(),
        'diagnostic_plan': m['diagnostic_plan'], 'runtime_qualified': False}


def _diagnostic_result(store, packet, value, *, packet_ref=None, request=None, reservation_ref=None):
    plan = packet['metadata']['diagnostic_plan']; destination = Path(packet['request']['destination_directory'])
    owner = _read(store, value['owned_execution'])
    guard = _read(store, value['guard'])
    require(packet_ref is not None and request is not None and reservation_ref is not None, 'Expected diagnostic authority is missing')
    _diagnostic_guard(guard, packet, packet_ref, request, reservation_ref)
    require(value['guard']['path'] == str(destination / 'diagnostic-guard.json') and guard['packet_digest'] == digest(packet),
        'Diagnostic guard changed')
    require(owned_host_command.verify_completed(owner) == owner and owner.get('passed') is True and owner.get('timed_out') is False and
        owner.get('launch_authorized') is True and owner.get('boundary_rechecked') is True and
        owner.get('child', {}).get('exit_code') == 0 and owner['argv'] == plan['argv'] and owner['cwd'] == plan['cwd'] and
        type(owner.get('child', {}).get('exit_code')) is int and owner['control'] == str(destination / 'diagnostic') and
        owner['deadline_monotonic'] == guard['deadline_monotonic'] and owner['cleanup_deadline_monotonic'] == guard['cleanup_deadline_monotonic'] and
        owner['owner'] == guard['supervisor'] and owner['controller'] == guard['coordinator'] and
        owner['environment_sha256'] == hashlib.sha256(canonical(_diagnostic_environment(packet, value['guard'])).encode()).hexdigest() and
        0 <= owner['elapsed_seconds'] <= packet['request']['allowances']['diagnostic_max_seconds'], 'Diagnostic execution differs or did not pass')
    raw = _read(store, value['output'], immutable=False, raw=True)
    require(value['output']['path'] == str(destination / 'diagnostic/stdout.log') and len(raw) <= 65536,
        'Diagnostic output is unbound or excessive')
    result = json.loads(raw)
    require(set(result) <= {'format','packet_digest','passed','runtime_qualified','model_turns','product_attempts','observations'} and
        {k: v for k,v in result.items() if k != 'observations'} == {'format': 'aos-corrective-diagnostic-v1',
        'packet_digest': digest(packet), 'passed': True, 'runtime_qualified': False, 'model_turns': 0, 'product_attempts': 0} and
        result['passed'] is True and result['runtime_qualified'] is False and
        all(type(result[k]) is int for k in ('model_turns','product_attempts')) and
        isinstance(result.get('observations', {}), dict), 'Diagnostic does not prove its exact limited scope')


def _qualification_result(store, packet, ref, request, value):
    destination = Path(packet['request']['destination_directory'])
    require(set(value) == {'format','packet','request','reservation','diagnostic','journal','receipt','result','completed_at'} and
        value['format'] == FORMAT and set(value['diagnostic']) == {'owned_execution','guard','output'},
        'Unsupported corrective qualification result')
    require(_read(store, value['reservation']) == _reservation(packet, ref, request) and
        value['reservation']['path'] == str(destination / 'qualification-reservation.json') and
        value['packet'] == ref and value['request'] == request, 'Qualification reservation differs from saved approval')
    _diagnostic_result(store, packet, value['diagnostic'], packet_ref=ref, request=request, reservation_ref=value['reservation'])
    journal, journal_ref = previous._journal(destination / 'readiness', value['receipt'])
    result = _read(store, value['receipt'], immutable=False)
    require(journal_ref == value['journal'] and journal['state_dir'] == str(store.directory) and
        journal['source'] == packet['metadata']['recipe']['source'] and len(journal['runs']) == 1 and
        journal['max_attempts'] == result['limits']['max_attempts'] == 1 and
        journal['max_seconds'] == result['limits']['max_seconds'] == packet['request']['allowances']['metadata_max_seconds'] and
        value['receipt']['path'] == str(destination / 'readiness/control/readiness-1.json') and
        result['resource_output'] == str(destination / 'readiness/outputs/readiness-1') and result == value['result'],
        'Corrective metadata allowance or exact producer journal differs')
    previous._ready_result(result, packet['metadata']['recipe'])
    readiness._verify_cached_cleanup(result, destination / 'readiness/runtime')


def qualify(lifecycle, request):
    store = lifecycle.store; packet, ref, decision = _load(store, request); m = packet['metadata']
    destination = Path(packet['request']['destination_directory'])
    with _fences(store, packet['request'], m['accounting_origin']) as fence:
        _validate_packet(store, packet)
        with fencing._connection(store) as (db, _):
            _guard(lifecycle, db, request, packet, decision)
        reservation = _reservation(packet, ref, request)
        reserved = destination / 'qualification-reservation.json'; completed = destination / 'qualification-result.json'
        if reserved.exists():
            require(previous._json(reserved, immutable=True)[0] == reservation, 'Consumed qualification reservation differs')
            require(completed.exists(), 'Corrective qualification interrupted or failed; allowances remain consumed')
            result = previous._json(completed, immutable=True)[0]
            _qualification_result(store, packet, ref, request, result)
            return result
        require(not completed.exists() and not (destination / 'diagnostic').exists() and not (destination / 'readiness').exists(),
            'Unowned corrective qualification namespace')
        allowances = packet['request']['allowances']
        require(time.time() + allowances['diagnostic_max_seconds'] + allowances['metadata_max_seconds'] <=
            m['accounting_origin']['deadline'], 'Original feature deadline cannot fund both complete qualification allowances')
        reservation_ref = previous._retain(reserved, reservation)
        plan = m['diagnostic_plan']; started = time.monotonic()
        total = packet['request']['allowances']['diagnostic_max_seconds']
        require(time.time() + total <= m['accounting_origin']['deadline'], 'Original feature deadline cannot fund the complete diagnostic allowance')
        cleanup_until = started + total; until = cleanup_until - owned_host_command.CLEANUP_RESERVE_SECONDS
        # The guard is published only after the maintained supervisor has a
        # retained birth identity, while its payload is still held behind G.
        guard_path = destination / 'diagnostic-guard.json'; latest = {}; guard_ref = None
        def persist(value):
            latest.clear(); latest.update(value)
        def before_go(deadline):
            nonlocal guard_ref
            require(type(deadline) in (int,float) and deadline == until, 'Diagnostic command deadline changed')
            _validate_packet(store, packet); fence()
            with fencing._connection(store) as (db, _):
                _guard(lifecycle, db, request, packet, decision)
            guard_ref = previous._retain(guard_path, {'format': 'aos-corrective-diagnostic-guard-v1',
                'packet': ref, 'packet_digest': digest(packet), 'request': request, 'reservation': reservation_ref,
                'started_monotonic': started, 'deadline_monotonic': until, 'cleanup_deadline_monotonic': cleanup_until,
                'feature_deadline': m['accounting_origin']['deadline'], 'supervisor': latest['owner'], 'coordinator': latest['controller']})
        # The guard hash cannot precede the supervisor identity. The child reads
        # its exact private path and hashes it; this expected path is bound in
        # the immutable packet and saved reservation, not caller input.
        environment = {**plan['environment'], 'AOS_CORRECTIVE_PACKET_DIGEST': digest(packet),
            'AOS_CORRECTIVE_GUARD_PATH': str(guard_path), 'AOS_CORRECTIVE_GUARD_SHA256': 'read-retained-guard'}
        owner = owned_host_command.run_owned(plan['argv'], plan['cwd'], environment, destination / 'diagnostic', deadline=until,
            cleanup_deadline=cleanup_until, before_go=before_go, persist=persist)
        owner_ref = previous._retain(destination / 'diagnostic-execution.json', owner)
        require(guard_ref is not None, 'Diagnostic failed before exact owned admission; allowance remains consumed')
        diagnostic = {'owned_execution': owner_ref, 'guard': guard_ref, 'output': previous._file(destination / 'diagnostic/stdout.log')[1]}
        previous._retain(destination / 'diagnostic-result.json', diagnostic)
        _diagnostic_result(store, packet, diagnostic, packet_ref=ref, request=request, reservation_ref=reservation_ref)
        fence(); _validate_packet(store, packet)
        require(time.time() + allowances['metadata_max_seconds'] <= m['accounting_origin']['deadline'],
            'Original feature deadline cannot fund the complete metadata allowance')
        with fencing._connection(store) as (db, _):
            _guard(lifecycle, db, request, packet, decision)
        directory = destination / 'readiness'; directory.mkdir(mode=0o700)
        require(time.time() + allowances['metadata_max_seconds'] <= m['accounting_origin']['deadline'],
            'Original feature deadline no longer funds the complete metadata allowance')
        result = readiness.run_readiness(m['recipe'], state_dir=store.directory, readiness_dir=directory,
            model=m['original_contract']['model'], effort=m['original_contract']['effort'],
            max_seconds=packet['request']['allowances']['metadata_max_seconds'], max_attempts=1, native_checks=[])
        journal, journal_ref = previous._json(directory / 'control/readiness.json')
        require(len(journal.get('runs', [])) == 1, 'Corrective metadata exceeded one attempt')
        actual, receipt_ref = previous._json(journal['runs'][0]['receipt'])
        require(actual == result and receipt_ref['sha256'] == journal['runs'][0]['sha256'], 'Metadata return differs from retained producer history')
        value = {'format': FORMAT, 'packet': ref, 'request': request, 'reservation': reservation_ref,
            'diagnostic': diagnostic, 'journal': journal_ref, 'receipt': receipt_ref, 'result': result, 'completed_at': now()}
        previous._retain(completed, value)
        _qualification_result(store, packet, ref, request, value)
        _validate_packet(store, packet)
        with fencing._connection(store) as (db, _):
            _guard(lifecycle, db, request, packet, decision)
        fence()
        return value


def apply(lifecycle, request):
    store = lifecycle.store; packet, ref, decision = _load(store, request); m = packet['metadata']; work = request['work_id']
    with store._connection() as db:
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (request['operation_id'],)).fetchone()
        if row:
            receipt = json.loads(row[0]); require(receipt['apply_request'] == request, 'Applied corrective replay differs')
            return verify(store, {'operation_id': request['operation_id'], 'work_id': work, 'receipt_digest': digest(receipt)})
    with _fences(store, packet['request'], m['accounting_origin']) as fence:
        with fencing._connection(store, write=True) as (db, _):
            db.execute('BEGIN IMMEDIATE'); _guard(lifecycle, db, request, packet, decision)
            _validate_packet(store, packet)
            qualification_path = Path(packet['request']['destination_directory']) / 'qualification-result.json'
            require(qualification_path.is_file(), 'Corrective qualification has no completed result')
            qualification, qualification_ref = previous._json(qualification_path, immutable=True)
            _qualification_result(store, packet, ref, request, qualification)
            package = previous._package(packet['concept'])
            expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': request['digest']}
            targets = ('lifecycle','work','lifecycle_event','operation')
            require(not db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN (" + ','.join('?' for _ in targets) + ')', targets).fetchone(), 'Unsupported mutation triggers')
            db.execute("UPDATE lifecycle SET contract=? WHERE work_id=? AND contract=? AND admitted=0 AND control='none'",
                (canonical(expected), work, canonical(m['original_contract'])))
            require(db.execute('SELECT changes()').fetchone()[0] == 1, 'Corrective contract compare-and-swap failed')
            lifecycle._touch(db, work, 'predispatch_corrective_rebound', {'operation_id': request['operation_id'], 'packet_digest': digest(packet)})
            floor = db.execute('SELECT max(event_id) FROM lifecycle_event WHERE work_id=?', (work,)).fetchone()[0]
            parent = m['parent_recovery_receipt']
            receipt = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': work, 'state_dir': str(store.directory),
                'apply_request': request, 'packet': ref, 'packet_digest': digest(packet), 'qualification': qualification_ref,
                'from_decision_id': parent['to_decision_id'], 'to_decision_id': decision['decision_id'],
                'from_concept_digest': m['original_contract']['concept_digest'], 'to_concept_digest': request['digest'],
                'from_recipe_digest': m['original_contract']['runtime_recipe_digest'], 'to_recipe_digest': expected['runtime_recipe_digest'],
                'engine_digest': m['runtime_identity']['runtime_source_digest'], 'accounting_origin': m['accounting_origin'],
                'original_clock': parent['original_clock'], 'recovery_clock': m['accounting']['clock'],
                'parent_recovery_receipt': parent, 'original_amendment_receipt': parent['original_amendment_receipt'],
                'accounting_snapshot': m['accounting'], 'runtime_slots': packet['request']['allowances']['runtime_slots'],
                'effective_limit_seconds': packet['request']['allowances']['effective_limit_seconds'],
                'coordinator_reservation_plan': m['coordinator_reservation_plan'], 'settled_failure': m['settled_failure'],
                'registered_contract': expected, 'prerequisite_event_floor': floor, 'created_at': now()}
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?)', (request['operation_id'], work, digest(parent), canonical(receipt)))
            store._record(db, request['operation_id'], ['predispatch_corrective_rebind', request],
                {'applied': True, 'receipt_digest': digest(receipt), 'work_id': work})
            fence(); db.commit()
    return receipt


def verify(store, reference):
    VerifyRequest.model_validate(reference, strict=True)
    with store._connection() as db:
        require(_schema(db), 'Corrective authority missing')
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=? AND work_id=?',
            (reference['operation_id'], reference['work_id'])).fetchone()
        require(row is not None, 'Corrective applied receipt missing')
        receipt = json.loads(row[0]); require(digest(receipt) == reference['receipt_digest'], 'Corrective receipt digest differs')
        operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (reference['operation_id'],)).fetchone()
        require(operation and json.loads(operation[0]) == {'applied': True, 'receipt_digest': digest(receipt), 'work_id': reference['work_id']},
            'Corrective applied operation differs')
        work = store._work(db, reference['work_id'])
        row = db.execute('SELECT package,digest FROM concept WHERE work_id=? AND revision=?', (reference['work_id'], work['current_concept'])).fetchone()
        require(row and row['digest'] == receipt['to_concept_digest'], 'Corrective decision is no longer current')
        package = json.loads(row['package'])
        contract = json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (reference['work_id'],)).fetchone()[0])
        require(contract == previous._effective_contract(db, package, receipt['to_concept_digest'], floor=receipt['prerequisite_event_floor']),
            'Corrective registered authority differs')
    packet = _read(store, receipt['packet'])
    require(digest(packet) == receipt['packet_digest'], 'Corrective immutable packet changed')
    _validate_packet(store, packet, snapshot=False)
    previous._approval(store, receipt['to_decision_id'], receipt['work_id'], receipt['to_concept_digest'])
    _qualification_result(store, packet, receipt['packet'], receipt['apply_request'], _read(store, receipt['qualification']))
    current_accounting(store, receipt)
    return receipt


current_accounting = recovery.current_accounting
_current_accounting_locked = recovery._current_accounting_locked
