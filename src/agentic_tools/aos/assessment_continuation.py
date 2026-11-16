"""Exact continuation of a recovered, unlaunched independent assessment.

Original preparation, feature approval, implementation and accounting remain
historical facts. A new saved human decision binds changed execution metadata.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import budgets, checkout_boundary, instructions, project, readiness
from . import assessment_recovery as recovery
from . import startup_failure_recovery as fencing
from .codex_runtime import candidate
from .readiness import _runtime_identity, run_readiness
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-assessment-continuation-v1'
TABLE = 'assessment_continuation'
PREFIX = 'startup-retry:assessment-continuation:'
SCHEMA = {
    TABLE: 'CREATE TABLE assessment_continuation (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,packet_digest TEXT NOT NULL,record TEXT NOT NULL)',
    TABLE + '_update': "CREATE TRIGGER assessment_continuation_update BEFORE UPDATE ON assessment_continuation BEGIN SELECT RAISE(ABORT,'Assessment continuation history is immutable'); END",
    TABLE + '_delete': "CREATE TRIGGER assessment_continuation_delete BEFORE DELETE ON assessment_continuation BEGIN SELECT RAISE(ABORT,'Assessment continuation history is immutable'); END",
}


def _reject(message):
    raise Rejected('assessment_continuation', message)


def _schema(db, *, create=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'assessment_continuation*' OR (tbl_name='assessment_continuation' AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))").fetchall())
    if not found and create:
        for sql in SCHEMA.values():
            db.execute(sql)
        return
    if found != SCHEMA:
        _reject('The exact immutable continuation audit schema is missing or changed')


def _immutable_value(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        _reject('Preparation must name its exact canonical file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1
                or before.st_size > 64 * 1024 * 1024):
            _reject('Preparation must remain a private immutable regular file')
        raw = stream.read()
        after = path.stat()
    if any(getattr(before, k) != getattr(after, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        _reject('Preparation changed during readback')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _file(path):
    value, checksum = _immutable_value(path)
    if (value.get('format') != 'aos-supervisor-prepared-v1' or not isinstance(value.get('packet'), dict)
            or value.get('packet_digest') != digest(value['packet'])):
        _reject('Preparation wrapper identity is inconsistent')
    return value['packet'], checksum


def _original(reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        _reject('Exact original immutable preparation is required')
    packet, checksum = _file(reference['path'])
    if checksum != reference['sha256'] or packet.get('assessment_continuation'):
        _reject('Original preparation identity changed')
    return packet


def _package(concept):
    from .supervisor import _package as package
    return package(concept)


def _passive(lifecycle, db, audit, *, published=False):
    run, contract = lifecycle._run(db, audit['work_id'])
    if (run['admitted'] or run['control'] != 'none' or run['execution'] != 'held'
            or json.loads(run['candidate'] or 'null') != audit['candidate']
            or db.execute("SELECT 1 FROM assignment WHERE work_id=? AND state NOT IN ('completed','failed','cancelled')", (audit['work_id'],)).fetchone()
            or db.execute('SELECT 1 FROM resource WHERE work_id=?', (audit['work_id'],)).fetchone()
            or db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (audit['work_id'],)).fetchone()
            or db.execute('SELECT 1 FROM lifecycle WHERE work_id<>? AND admitted=1', (audit['work_id'],)).fetchone()):
        _reject('Continuation requires the recovered frozen candidate and no active ownership')
    blockers = {r[0] for r in db.execute('SELECT code FROM technical_blocker WHERE work_id=?', (audit['work_id'],))}
    if blockers != (set() if published else {'assessment_recovered'}):
        _reject('An unrelated hold prevents this exact continuation')
    if not published and contract != audit['original_contract']:
        _reject('The recovered original contract changed')
    return run, contract


def _capacity(lifecycle, db, contract):
    from . import plan_runtime
    recipe = contract['runtime_recipe']
    run, _ = lifecycle._run(db, contract['_work_id'])
    used = lifecycle._startup_retries_used_in(db, contract['_work_id'])
    retries = contract.get('max_startup_retries', 0) - used - 1
    repairs = contract['max_repairs'] - run['cycle']
    if retries < 0 or repairs < 0:
        _reject('Original startup-retry or correction allowance is exhausted')
    if retries != 0:
        _reject('This bounded continuation requires exactly one remaining startup retry')
    allowance = budgets.approved_budget(contract, lifecycle.store.directory)
    path = lifecycle.store.directory / 'codex-runtime.sqlite3'
    if path.is_symlink() or path.resolve() != Path(allowance['ledger_directory']) / path.name:
        _reject('Retain the exact runtime accounting identity')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1) as runtime:
        runtime.execute('BEGIN')
        saved = runtime.execute('SELECT record FROM runtime_budget WHERE id=?', (allowance['budget_id'],)).fetchone()
        if not saved or json.loads(saved[0]) != budgets._budget_identity(allowance, recipe['source']):
            _reject('Original product allowance changed or disappeared')
        for scope in ('product:' + allowance['product_id'], 'source:' + recipe['source']):
            row = runtime.execute('SELECT budget_id FROM product_budget_scope WHERE scope=?', (scope,)).fetchone()
            if not row or row[0] != allowance['budget_id']:
                _reject('Original product/source budget binding changed')
        attempts = budgets.attempts_for(runtime, allowance['budget_id'])
    if any(type(a.get('budget_seconds')) is not int or a['budget_seconds'] < 1 for a in attempts):
        _reject('Original reserved-time accounting is incomplete')
    count = db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (contract['_work_id'],)).fetchone()[0]
    latest = db.execute('SELECT * FROM assignment WHERE work_id=? ORDER BY rowid DESC LIMIT 1',
                        (contract['_work_id'],)).fetchone()
    following = plan_runtime.remaining_jobs_in(db, latest) if latest is not None else 0
    required = 1 + following + 3 * repairs + retries
    receipt = {'ready': True, 'phase': 'assessment', 'required_assignments': required,
               'required_seconds': required * recipe['max_seconds'], 'attempts_reserved': len(attempts),
               'seconds_reserved': sum(a['budget_seconds'] for a in attempts), 'lifecycle_assignments': count,
               'startup_retries_used': used, 'remaining_assignments': allowance['max_assignments'] - len(attempts),
               'lifecycle_remaining_assignments': contract['max_assignments'] - count,
               'remaining_seconds': allowance['max_seconds'] - sum(a['budget_seconds'] for a in attempts),
               'reservation_performed': False}
    if (min(receipt['remaining_assignments'], receipt['lifecycle_remaining_assignments']) < required
            or receipt['remaining_seconds'] < receipt['required_seconds']
            or recipe['max_seconds'] > min(allowance['max_job_seconds'], contract['max_seconds'])):
        _reject('Remaining original allowance cannot finish assessment and permitted correction')
    return receipt


def _prune_unavailable_path_entries(original):
    """Drop only absent, unaliased PATH entries; preserve every remaining byte."""
    recipe = copy.deepcopy(original)
    environment = recipe.get('checkout_policy', {}).get('native_environment', {})
    if 'PATH' not in environment:
        return recipe, []
    value = environment['PATH']
    if not isinstance(value, str):
        _reject('The original PATH must remain an exact string')
    entries, removed = value.split(':'), []
    for name in entries:
        path = Path(name)
        if not path.is_absolute():
            _reject('Only absolute unavailable PATH directories may be removed')
        try:
            path.lstat()
        except FileNotFoundError:
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                _reject('An unavailable PATH entry with a linked ancestor cannot be removed')
            removed.append(name)
    if removed:
        retained = [name for name in entries if name not in removed]
        if not retained:
            _reject('Continuation cannot remove the entire PATH')
        environment['PATH'] = ':'.join(retained)
    return recipe, removed


def _prior_readiness_receipts(journal, directory):
    references = []
    for index, run in enumerate(journal['runs'], 1):
        path = directory / 'control' / f'readiness-{index}.json'
        if (run.get('id') != f'readiness-{index}' or run.get('receipt') != str(path)
                or run.get('state') not in ('passed', 'failed') or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != run.get('sha256')):
            _reject('Prior readiness records must remain complete and exact')
        receipt = readiness._load(path)
        native = receipt.get('native')
        if (receipt.get('simulated') is not False or receipt.get('readiness_scope') != 'model-metadata-only'
                or receipt.get('status') != run['state'] or receipt.get('requested_native_checks') != []
                or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in
                       ('model_turns', 'product_attempts', 'product_reservations'))
                or native is not None and (native.get('checks') != [] or native.get('execution') != 'not_requested')):
            _reject('The extra technical allowance requires prior zero-product metadata-only runs')
        readiness._verify_cached_cleanup(receipt, directory / 'runtime')
        references.append({'path': str(path), 'sha256': run['sha256']})
    return references


def _check_extension(reference, config, *, audit=None, allow_pending=False):
    directory = readiness._canonical_directory(Path(config['readiness_dir']))
    extension, checksum = _immutable_value(reference['path'])
    if (checksum != reference['sha256'] or extension.get('format') != FORMAT + '-readiness-extension'
            or Path(reference['path']).parent != directory / 'control'
            or extension.get('readiness_dir') != str(directory)
            or extension.get('state_dir') != config['state_dir']
            or extension.get('source') != config['recipe']['source']
            or extension.get('original_max_attempts') != config['readiness']['max_attempts']
            or extension.get('max_attempts') != extension['original_max_attempts'] + 1
            or extension.get('max_attempts', 11) > 10
            or extension.get('max_seconds') != config['readiness']['max_seconds']
            or extension.get('product_budget_changed') is not False
            or Path(reference['path']).name != 'assessment-readiness-extension-' + extension.get('operation_id', '') + '.json'):
        _reject('The exact one-attempt technical extension is missing or changed')
    if audit is not None and (extension.get('work_id') != audit['work_id']
                             or extension.get('recovery_operation_id') != audit['operation_id']
                             or extension.get('recovery_audit_digest') != digest(audit)):
        _reject('Readiness extension belongs to a different recovery')
    original = extension['original_journal']
    if (original.get('format') != readiness.FORMAT or original.get('state_dir') != config['state_dir']
            or original.get('source') != config['recipe']['source'] or original.get('max_seconds') != config['readiness']['max_seconds']
            or hashlib.sha256(extension.get('original_journal_raw', '').encode()).hexdigest() != extension.get('original_journal_sha256')
            or json.loads(extension['original_journal_raw']) != original
            or original.get('max_attempts') != extension['original_max_attempts']
            or len(original.get('runs', [])) != extension['original_max_attempts']
            or _prior_readiness_receipts(original, directory) != extension['prior_receipts']):
        _reject('Original readiness history differs from the preserved extension')
    current = readiness._load(directory / 'control' / 'readiness.json')
    if allow_pending and current == original:
        return extension
    expected = {**original, 'max_attempts': extension['max_attempts'], 'allowance_extension': reference}
    if (any(current.get(k) != v for k, v in expected.items() if k != 'runs')
            or set(current) != set(expected)
            or current.get('runs', [])[:len(original['runs'])] != original['runs']
            or not len(original['runs']) <= len(current.get('runs', [])) <= extension['max_attempts']):
        _reject('Cumulative readiness history or extension limits changed')
    return extension


def _extension_receipt(receipt, reference, config):
    extension = _check_extension(reference, config)
    directory = Path(config['readiness_dir'])
    current = readiness._load(directory / 'control' / 'readiness.json')
    if len(current['runs']) != extension['max_attempts']:
        _reject('The additional readiness attempt has not produced its retained receipt')
    _prior_readiness_receipts(current, directory)
    saved = readiness._load(Path(current['runs'][-1]['receipt']))
    cache_fields = {'cached', 'revalidated_at', 'current_resources', 'current_product_resources'}
    if (saved != {k: v for k, v in receipt.items() if k not in cache_fields}
            or saved.get('limits', {}).get('max_attempts') != extension['max_attempts']
            or saved.get('limits', {}).get('max_seconds') != extension['max_seconds']):
        _reject('The additional readiness receipt differs from the retained exact run')


def _extend_readiness(config, audit, operation_id):
    """Explicit one-time technical allowance; never resets runs or product limits."""
    directory = readiness._canonical_directory(Path(config['readiness_dir']))
    control = readiness._canonical_directory(directory / 'control')
    journal_path = control / 'readiness.json'
    extension_path = control / ('assessment-readiness-extension-' + operation_id + '.json')
    fd = os.open(control / 'readiness.lock', os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('readiness_owned', 'Another readiness owner prevents allowance extension') from error
        journal = readiness._load(journal_path)
        if extension_path.exists() or extension_path.is_symlink():
            extension, checksum = _immutable_value(extension_path)
            if extension.get('operation_id') != operation_id:
                _reject('Retained readiness extension belongs to a different operation')
            reference = {'path': str(extension_path), 'sha256': checksum}
            # Finish a retained pre-update claim after a crash, without another increment.
            _check_extension(reference, config, audit=audit, allow_pending=True)
            if journal == extension.get('original_journal'):
                readiness._save(journal_path, {**journal, 'max_attempts': extension['max_attempts'],
                                               'allowance_extension': reference})
            _check_extension(reference, config, audit=audit)
            return reference
        limit = config['readiness']['max_attempts']
        if (not 1 <= limit < 10 or journal.get('format') != readiness.FORMAT
                or journal.get('state_dir') != config['state_dir'] or journal.get('source') != config['recipe']['source']
                or journal.get('max_attempts') != limit or journal.get('max_seconds') != config['readiness']['max_seconds']
                or len(journal.get('runs', [])) != limit or 'allowance_extension' in journal):
            _reject('Extend only the exhausted original technical allowance once')
        references = _prior_readiness_receipts(journal, directory)
        extension = {'format': FORMAT + '-readiness-extension', 'operation_id': operation_id,
                     'work_id': audit['work_id'], 'recovery_operation_id': audit['operation_id'],
                     'recovery_audit_digest': digest(audit), 'state_dir': config['state_dir'],
                     'readiness_dir': str(directory), 'source': config['recipe']['source'],
                     'original_max_attempts': limit, 'max_attempts': limit + 1,
                     'max_seconds': config['readiness']['max_seconds'], 'original_journal': journal,
                     'original_journal_raw': journal_path.read_text(),
                     'original_journal_sha256': hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                     'prior_receipts': references, 'product_budget_changed': False, 'created_at': now()}
        encoded = (canonical(extension) + '\n').encode()
        out = os.open(extension_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(out, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(control, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        reference = {'path': str(extension_path), 'sha256': hashlib.sha256(encoded).hexdigest()}
        readiness._save(journal_path, {**journal, 'max_attempts': limit + 1, 'allowance_extension': reference})
        _check_extension(reference, config, audit=audit)
        return reference


def _concept(original, metadata, prepared):
    result = copy.deepcopy(original['concept'])
    result.update(operation_id=metadata['operation_id'] + '-present',
                  expected_work_revision=metadata['expected_work_revision'], revision=original['concept']['revision'] + 1)
    envelope = result['execution_envelope']
    envelope.update(revision=envelope['revision'] + 1, runtime_recipe=prepared, runtime_recipe_digest=digest(prepared))
    summary = ('Technical continuation of the preserved implementation candidate ' + metadata['candidate']['identity']
               + '. Resume independent assessment after a separately verified pre-launch instruction-drift failure. '
               'Preserve feature behavior, acceptance, original source baseline, checks, roles, write/network permissions, '
               'allocated slots and charges. Consume one existing startup retry; do not rerun implementation. '
               'Bind current instruction/configuration hashes, the listed read-only policy support files, and freshly '
               'qualified runtime identity. Remove only these absent PATH directories without adding or substituting tools: '
               + (', '.join(metadata['removed_unavailable_path_entries']) or 'none')
               + '. Technical readiness allowance: ' + metadata['readiness_allowance_description']
               + '. The original feature decision remains retained history.')
    result['body'] = original['concept']['body'] + '\n\n' + summary
    result.update(recommendation='Approve this exact technical continuation and resume independent assessment.',
                  alternative='Keep the candidate held with its existing evidence and charges.',
                  consequence='Approval permits one bounded successor assessment under the remaining original limits.')
    result['decision_context'] = {
        'problem': 'Instructions/configuration changed between implementation and independent assessment; the assessor was rejected before launch.',
        'example': 'The completed gradient implementation is assessed as saved; implementation is not restarted.',
        'effort': 'Existing product assignment, correction and reserved-time limits remain unchanged. This consumes the existing startup-retry allowance. ' + metadata['readiness_allowance_description'],
        'dependencies': 'Derived recovery evidence ' + metadata['recovery_operation_id'] + '; current metadata-only readiness; exact runtime ' + metadata['runtime_identity']['runtime_source_digest'] + '. Read-only policy support: ' + (', '.join(metadata['support_files']) or 'none') + '. Removed unavailable PATH entries: ' + (', '.join(metadata['removed_unavailable_path_entries']) or 'none'),
        'evidence': 'Original preparation ' + metadata['original_prepared_packet']['sha256'] + '; recovery audit ' + metadata['recovery_audit_digest'] + '; candidate ' + metadata['candidate']['identity'] + '; new exact instruction manifest ' + digest(prepared['instruction_manifest']) + ('; technical readiness extension ' + canonical(metadata['readiness_extension']) if metadata['readiness_extension'] else ''),
        'stopping_point': 'Keep held if evidence, ownership, candidate, instructions or capacity changes. Native project checks require passing independent review of the exact candidate. No commit or publication.'}
    return result


def prepare(lifecycle, *, operation_id, recovery_operation_id, support_files=(), readiness_extra_attempt=False, readiness_runner=None):
    """Prepare technical evidence only; no decision, contract, or product launch."""
    identifier(operation_id)
    identifier(recovery_operation_id)
    if type(readiness_extra_attempt) is not bool:
        _reject('The technical allowance extension must be explicitly selected')
    if len(support_files) > 16 or any(not isinstance(p, str) for p in support_files):
        _reject('At most sixteen exact read-only policy support files are supported')
    with fencing._worker_fence(lifecycle.store) as verify:
        with lifecycle.store._connection() as db:
            row = db.execute(f'SELECT record FROM {recovery.TABLE} WHERE operation_id=?', (recovery_operation_id,)).fetchone()
            if row is None:
                _reject('The exact successful assessment recovery is required')
            work_id = json.loads(row[0])['work_id']
            audit = recovery.read_audit(db, recovery_operation_id, work_id)
            retained = lifecycle.store.directory / ('prepared-assessment-' + work_id + '-' + operation_id + '.json')
            if retained.exists() or retained.is_symlink():
                original = _original(audit['original_prepared_packet'])
                request, _ = project._load(Path(original['input_bindings']['request']['path']), project.WorkRequest)
                packet = load(retained, original['input_bindings'], original['project'], request)
                meta = packet['assessment_continuation']
                expected_support = sorted(set(support_files) | {item['path'] for item in audit['original_contract']['runtime_recipe']['instruction_manifest'].get('support', [])})
                if meta['operation_id'] != operation_id or meta['recovery_operation_id'] != recovery_operation_id or meta['support_files'] != expected_support or bool(meta.get('readiness_extension')) != readiness_extra_attempt:
                    _reject('Retained preparation belongs to a different exact continuation request')
                _audit(lifecycle, db, packet)
                verify()
                return packet
            run, contract = _passive(lifecycle, db, audit)
            work = lifecycle.store._work(db, work_id)
            capacity = _capacity(lifecycle, db, {**contract, '_work_id': work_id})
            expected_revision = work['revision']
        original = _original(audit['original_prepared_packet'])
        if original['concept']['execution_envelope'] != {k: v for k, v in contract.items() if k not in ('concept_revision', 'concept_digest')}:
            _reject('Original preparation differs from recovered contract')
        recipe, removed_path_entries = _prune_unavailable_path_entries(contract['runtime_recipe'])
        if not checkout_boundary.reviewed_host(recipe):
            _reject('This continuation requires the existing reviewed-host policy')
        existing_support = [item['path'] for item in recipe['instruction_manifest'].get('support', [])]
        support = sorted(set(existing_support) | set(support_files))
        limit = recipe['instruction_manifest'].get('runtime_overrides', {}).get('project_doc_max_bytes')
        recipe['instruction_manifest'] = instructions.discover(Path(recipe['source']), support_files=support,
                                                              project_doc_max_bytes=limit)
        instructions.validate(recipe['instruction_manifest'], Path(recipe['source']), lifecycle.store.directory)
        if candidate(Path(recipe['source']), recipe=recipe) != audit['candidate']:
            _reject('Frozen candidate changed before continuation readiness')
        config = original['project']
        if config['readiness']['checks']:
            _reject('Continuation readiness must remain metadata-only')
        # Validate the full environment before spending the one extra technical attempt.
        recipe = readiness._normalise(recipe, lifecycle.store.directory)
        extension = (_extend_readiness(config, audit, operation_id) if readiness_extra_attempt else None)
        max_attempts = config['readiness']['max_attempts'] + int(readiness_extra_attempt)
        receipt = (readiness_runner or run_readiness)(copy.deepcopy(recipe), state_dir=lifecycle.store.directory,
            readiness_dir=Path(config['readiness_dir']), model=contract['model'], effort=contract['effort'],
            max_seconds=config['readiness']['max_seconds'], max_attempts=max_attempts, native_checks=[])
        prepared, observed = project._successful_readiness(receipt, recipe)
        if extension:
            _extension_receipt(receipt, extension, config)
        if observed != audit['candidate']['identity'] or candidate(Path(recipe['source']), recipe=prepared) != audit['candidate']:
            _reject('Readiness changed or did not observe the frozen implementation candidate')
        metadata = {'format': FORMAT, 'operation_id': operation_id, 'work_id': work_id,
                    'recovery_operation_id': recovery_operation_id, 'recovery_audit_digest': digest(audit),
                    'original_prepared_packet': audit['original_prepared_packet'], 'original_contract': contract,
                    'candidate': audit['candidate'], 'expected_work_revision': expected_revision,
                    'runtime_identity': _runtime_identity(), 'support_files': support,
                    'removed_unavailable_path_entries': removed_path_entries, 'readiness_extension': extension,
                    'readiness_allowance_description': (f"Cumulative metadata-only readiness attempts {config['readiness']['max_attempts']} to {max_attempts}, unchanged {config['readiness']['max_seconds']}-second ceiling per attempt; all prior runs retained" if extension else 'Original technical readiness allowance retained')}
        result = copy.deepcopy(original)
        result.update(assessment_continuation=metadata, readiness=receipt, finish_capacity=capacity,
                      concept=_concept(original, metadata, prepared))
        project._unchanged(result['input_bindings'])
        with lifecycle.store._connection() as db:
            fresh = recovery.read_audit(db, recovery_operation_id, work_id)
            if fresh != audit or lifecycle.store._work(db, work_id)['revision'] != expected_revision:
                _reject('Recovered state changed during readiness')
            _passive(lifecycle, db, audit)
        verify()
        return result


def validate(packet, bindings, config, request):
    """Validate the append-only packet, original inputs, and permitted delta."""
    metadata = packet.get('assessment_continuation', {})
    if metadata.get('format') != FORMAT:
        _reject('A qualified assessment-continuation packet is required')
    original = _original(metadata['original_prepared_packet'])
    if (packet.get('input_bindings') != bindings or original.get('input_bindings') != bindings
            or packet.get('project') != config or original.get('project') != config
            or packet.get('introduction') != request['introduction']
            or original.get('introduction') != request['introduction']
            or {k: v for k, v in original['concept'].items() if k != 'execution_envelope'}
            != {k: v for k, v in request['concept'].items() if k != 'execution_envelope'}):
        _reject('Continuation inputs differ from the original immutable preparation')
    project._unchanged(bindings)
    if any(packet.get(k) != original.get(k) for k in set(original) - {'concept', 'readiness', 'finish_capacity'}):
        _reject('Continuation changed a non-technical preparation field')
    old = metadata['original_contract']
    if original['concept']['execution_envelope'] != {k: v for k, v in old.items() if k not in ('concept_revision', 'concept_digest')}:
        _reject('Continuation did not retain the exact original contract')
    extension = metadata.get('readiness_extension')
    if extension:
        checked = _check_extension(extension, config)
        if (checked['operation_id'] != metadata['operation_id']
                or checked['recovery_audit_digest'] != metadata['recovery_audit_digest']):
            _reject('Technical extension does not bind this continuation')
        _extension_receipt(packet['readiness'], extension, config)
        description = f"Cumulative metadata-only readiness attempts {checked['original_max_attempts']} to {checked['max_attempts']}, unchanged {checked['max_seconds']}-second ceiling per attempt; all prior runs retained"
    else:
        description = 'Original technical readiness allowance retained'
    if metadata.get('readiness_allowance_description') != description:
        _reject('The technical readiness allowance disclosure changed')
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    expected_recipe, removed_path_entries = _prune_unavailable_path_entries(old['runtime_recipe'])
    if metadata.get('removed_unavailable_path_entries') != removed_path_entries:
        _reject('Previously unavailable PATH entries changed; retain the exact removal decision')
    if ({k: v for k, v in recipe.items() if k != 'instruction_manifest'}
            != {k: v for k, v in expected_recipe.items() if k != 'instruction_manifest'}):
        _reject('Continuation changed execution scope, permissions, commands, models or limits')
    if packet['concept'] != _concept(original, metadata, recipe):
        _reject('Continuation concept differs from the permitted technical delta')
    if (metadata['runtime_identity'] != _runtime_identity()
            or packet['readiness']['runtime_identity'] != metadata['runtime_identity']
            or sorted(item['path'] for item in recipe['instruction_manifest'].get('support', [])) != metadata['support_files']):
        _reject('Fresh runtime or exact support-file binding changed')
    prepared, observed = project._successful_readiness(packet['readiness'], recipe)
    if prepared != recipe or observed != metadata['candidate']['identity']:
        _reject('Continuation readiness did not bind its frozen candidate')
    return packet


def load(path, bindings, config, request):
    return validate(_file(path)[0], bindings, config, request)


def _validate_saved(packet):
    bindings = packet['input_bindings']
    config, project_binding = project._load(Path(bindings['project']['path']), project.ProjectConfiguration)
    request, request_binding = project._load(Path(bindings['request']['path']), project.WorkRequest)
    return validate(packet, {'project': project_binding, 'request': request_binding}, config, request)


def _audit(lifecycle, db, packet):
    meta = packet['assessment_continuation']
    audit = recovery.read_audit(db, meta['recovery_operation_id'], meta['work_id'])
    if (digest(audit) != meta['recovery_audit_digest'] or audit['original_contract'] != meta['original_contract']
            or audit['candidate'] != meta['candidate'] or audit['original_prepared_packet'] != meta['original_prepared_packet']):
        _reject('Continuation differs from its exact recovery audit')
    return audit


def publish(lifecycle, packet):
    """Present/register the exact successor while held; no approval is supplied."""
    _validate_saved(packet)
    meta = packet['assessment_continuation']
    work_id, operation = meta['work_id'], meta['operation_id']
    package = _package(packet['concept'])
    target = digest(package)
    expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': target}
    with lifecycle.store._connection() as db:
        audit = _audit(lifecycle, db, packet)
        run, contract = lifecycle._run(db, work_id)
        if contract == expected:
            _schema(db)
            row = db.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?', (operation,)).fetchone()
            if not row or row['packet_digest'] != digest(packet):
                _reject('Registered continuation lost its immutable audit')
            return target
        _passive(lifecycle, db, audit)
        current = lifecycle.store._work(db, work_id)
        if current['current_concept'] == package['revision']:
            saved = db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?', (work_id, package['revision'])).fetchone()
            if not saved or json.loads(saved[0]) != package:
                _reject('A different concept superseded this continuation')
        elif current['current_concept'] != contract['concept_revision'] or current['revision'] != meta['expected_work_revision']:
            _reject('Recovered concept changed before continuation presentation')
    lifecycle.store.present(**packet['concept'])
    with lifecycle.store._transaction() as db:
        audit = _audit(lifecycle, db, packet)
        _passive(lifecycle, db, audit)
        current = lifecycle.store._work(db, work_id)
        if current['current_concept'] != package['revision'] or lifecycle._approved_envelope_errors(db, work_id, expected):
            _reject('Successor concept and execution contract differ')
        from . import plan_runtime
        plan_runtime.inherit_frozen_candidate_in(lifecycle, db, work_id, audit['original_contract']['concept_digest'], meta['candidate'])
        _schema(db, create=True)
        record = {'format': FORMAT, 'operation_id': operation, 'work_id': work_id, 'metadata': meta,
                  'original_contract': audit['original_contract'], 'registered_contract': expected,
                  'packet_digest': digest(packet), 'created_at': now()}
        db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?)', (operation, work_id, digest(packet), canonical(record)))
        db.execute("UPDATE lifecycle SET contract=?,execution='held',admitted=0 WHERE work_id=?", (canonical(expected), work_id))
        db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='assessment_recovered'", (work_id,))
        lifecycle._touch(db, work_id, 'assessment_continuation_presented', {'operation_id': operation, 'digest': target,
                                                                       'recovery_operation_id': meta['recovery_operation_id']})
    return target


def admit(lifecycle, packet):
    """After the real decision, enqueue one bounded assessment and consume S."""
    _validate_saved(packet)
    meta = packet['assessment_continuation']
    work_id, operation = meta['work_id'], meta['operation_id']
    lifecycle.reconcile(work_id)
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    instructions.validate(recipe['instruction_manifest'], Path(recipe['source']), lifecycle.store.directory)
    if candidate(Path(recipe['source']), recipe=recipe) != meta['candidate']:
        _reject('Frozen candidate changed before continuation admission')
    with lifecycle.store._transaction() as db:
        _schema(db)
        audit = _audit(lifecycle, db, packet)
        row = db.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?', (operation,)).fetchone()
        run, contract = lifecycle._run(db, work_id)
        if (not row or row['packet_digest'] != digest(packet)
                or json.loads(row['record'])['registered_contract'] != contract):
            _reject('Current contract does not match the immutable continuation')
        prior = db.execute('SELECT * FROM assignment WHERE predecessor=?', (PREFIX + operation,)).fetchone()
        if prior:
            assessment_context(db, prior, contract)
            return {'work_id': work_id, 'assignment_id': prior['assignment_id'], 'already_admitted': True}
        _passive(lifecycle, db, audit, published=True)
        errors = lifecycle._admission_errors(db, work_id)
        if errors:
            _reject('; '.join(errors))
        capacity = _capacity(lifecycle, db, {**contract, '_work_id': work_id})
        aid = lifecycle._enqueue(db, work_id, 'assessment', PREFIX + operation)
        if aid is None:
            _reject('Original assignment bound prevents the successor assessment')
        for resource in contract['resources']:
            db.execute('INSERT INTO resource VALUES(?,?,NULL,?,?)', (resource, work_id, 'frozen', now()))
        db.execute("UPDATE lifecycle SET admitted=1,execution='eligible' WHERE work_id=?", (work_id,))
        lifecycle._touch(db, work_id, 'assessment_continuation_admitted', {'operation_id': operation,
            'assignment_id': aid, 'recovery_operation_id': meta['recovery_operation_id'], 'capacity': capacity,
            'charges_preserved': True, 'startup_retry_consumed': True})
        return {'work_id': work_id, 'assignment_id': aid, 'execution': 'eligible'}


def assessment_context(db, assignment, contract):
    """Recognize only the exact audited assessment across the new binding."""
    if not assignment['predecessor'].startswith(PREFIX):
        return None
    _schema(db)
    operation = assignment['predecessor'][len(PREFIX):]
    row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (operation,)).fetchone()
    if not row:
        _reject('Assessment has no immutable continuation history')
    record = json.loads(row[0])
    meta = record['metadata']
    audit = recovery.read_audit(db, meta['recovery_operation_id'], assignment['work_id'])
    if (record['registered_contract'] != contract or digest(audit) != meta['recovery_audit_digest']
            or assignment['kind'] != 'assessment' or assignment['work_id'] != record['work_id']
            or assignment['contract_revision'] != contract['revision']
            or assignment['concept_revision'] != contract['concept_revision']
            or assignment['concept_digest'] != contract['concept_digest']
            or json.loads(assignment['candidate']) != meta['candidate'] or assignment['executor'] != contract['assessor']):
        _reject('Assessment continuation lineage or candidate changed')
    return {'purpose': 'assessment'}
