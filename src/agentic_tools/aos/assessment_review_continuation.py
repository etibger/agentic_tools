"""One final independent assessment with explicitly bound source-review evidence.

This changes no product allowance, permission or check. The blocked review and
its charges remain historical; a new exact human decision permits the remaining
assessment slot under the same product limits.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from . import assessment_continuation as retained
from . import assessment_review_readiness as technical
from . import budgets, project, readiness
from . import source_review_evidence as source_evidence
from . import startup_failure_recovery as fencing
from .store import Rejected, canonical, digest, identifier, now

KEY = 'assessment_review_continuation'
FORMAT = 'aos-assessment-review-continuation-v1'
TABLE = 'assessment_review_continuation'
SCHEMA = {
    TABLE: 'CREATE TABLE assessment_review_continuation (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,packet_digest TEXT NOT NULL,record TEXT NOT NULL)',
    TABLE + '_update': "CREATE TRIGGER assessment_review_continuation_update BEFORE UPDATE ON assessment_review_continuation BEGIN SELECT RAISE(ABORT,'Source-review continuation history is immutable'); END",
    TABLE + '_delete': "CREATE TRIGGER assessment_review_continuation_delete BEFORE DELETE ON assessment_review_continuation BEGIN SELECT RAISE(ABORT,'Source-review continuation history is immutable'); END",
}


def _reject(message):
    raise Rejected('assessment_review_continuation', message)


def _schema(db, *, create=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'assessment_review_continuation*' OR (tbl_name='assessment_review_continuation' AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))").fetchall())
    if not found and create:
        for sql in SCHEMA.values():
            db.execute(sql)
    elif found != SCHEMA:
        _reject('The immutable source-review continuation audit changed')


def _previous(reference):
    packet, checksum = retained._file(reference['path'])
    if (checksum != reference['sha256'] or not packet.get('assessment_continuation') or packet.get(KEY)):
        _reject('Retain the exact previously approved assessment continuation')
    return packet


def _report(reference, outputs):
    path = Path(reference['path'])
    if (path.resolve() != path or not path.is_relative_to(Path(outputs)) or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents) or path.stat().st_size > 1024 * 1024):
        _reject('Blocked source review must remain an exact bounded output artifact')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference['sha256']:
        _reject('The exact blocked review artifact changed')
    return json.loads(raw)


def _incident(lifecycle, db, work_id, failed_assignment_id, old_contract):
    row = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND work_id=?', (failed_assignment_id, work_id)).fetchone()
    if not row:
        _reject('The exact terminal source-review assessment is required')
    assignment = dict(row)
    result = json.loads(assignment['result'] or 'null')
    frozen = json.loads(assignment['candidate'] or 'null')
    phase = (result or {}).get('reviewed_host_validation', {})
    reference = phase.get('review', {})
    if (assignment['kind'] != 'assessment' or assignment['state'] != 'failed' or not assignment['terminal_at']
            or assignment['concept_digest'] != old_contract['concept_digest']
            or assignment['contract_revision'] != old_contract['revision']
            or not result or result.get('stopped') is not True or result.get('effects_reconciled') is not True
            or result.get('owned_processes') != [] or result.get('partial_mutations') != {'status': 'none', 'changes': []}
            or result.get('checks') != [] or result.get('native_checks') != [] or result.get('review_findings') != []
            or result.get('candidate') != frozen or phase.get('phase') != 'review_rejected'
            or phase.get('mode') != 'reviewed-host-v1' or phase.get('review_verdict') != 'blocked'):
        _reject('Only a clean terminal blocked source review with zero native execution qualifies')
    report = _report(reference, old_contract['runtime_recipe']['outputs'])
    if (report.get('candidate_identity') != frozen['identity'] or report.get('verdict') != 'blocked'
            or report.get('findings') != [] or not isinstance(report.get('limits'), str) or not report['limits'].strip()):
        _reject('The retained review does not establish this technical block')
    completion = db.execute('SELECT * FROM completion WHERE assignment_id=?', (failed_assignment_id,)).fetchone()
    if not completion or completion['result'] != assignment['result'] or completion['disposition'] != 'held':
        _reject('Blocked assessment completion is inconsistent')
    path = lifecycle.store.directory / 'codex-runtime.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as runtime:
        raw = runtime.execute('SELECT record FROM attempt WHERE id=?', (assignment['attempt_id'],)).fetchone()
        if not raw:
            _reject('The exact completed runtime evidence is missing')
        attempt = json.loads(raw[0])
        stop = attempt.get('stop_receipt', {})
        if (attempt.get('phase') != 'terminal' or attempt.get('terminal_observation', {}).get('result') != result
                or stop.get('stopped') is not True or stop.get('effects_reconciled') is not True
                or stop.get('owned_processes') != [] or stop.get('native_checks') != []
                or runtime.execute('SELECT 1 FROM native_check WHERE attempt_id=?', (assignment['attempt_id'],)).fetchone()):
            _reject('The failed review runtime lacks positive terminal cleanup or has native effects')
    return {'failed_assignment_id': failed_assignment_id, 'assignment_digest': digest(assignment),
            'runtime_record_sha256': hashlib.sha256(raw[0].encode()).hexdigest(),
            'completion_digest': digest(dict(completion)), 'review': reference, 'candidate': frozen}


def _capacity(lifecycle, db, work_id, contract):
    from . import plan_runtime
    latest = db.execute('SELECT * FROM assignment WHERE work_id=? ORDER BY rowid DESC LIMIT 1', (work_id,)).fetchone()
    if latest is not None and plan_runtime.remaining_jobs_in(db, latest):
        _reject('Final-assessment continuation cannot skip remaining internal DI tasks; use a continuation with full finish capacity')
    run, _ = lifecycle._run(db, work_id)
    count = db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (work_id,)).fetchone()[0]
    if (contract['max_assignments'] - count != 1 or run['cycle'] != contract['max_repairs']
            or lifecycle._startup_retries_used_in(db, work_id) != contract.get('max_startup_retries', 0)):
        _reject('This route is restricted to the one remaining assessment slot, without correction or startup expansion')
    allowance = budgets.approved_budget(contract, lifecycle.store.directory)
    path = lifecycle.store.directory / 'codex-runtime.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as runtime:
        saved = runtime.execute('SELECT record FROM runtime_budget WHERE id=?', (allowance['budget_id'],)).fetchone()
        if not saved or json.loads(saved[0]) != budgets._budget_identity(allowance, contract['runtime_recipe']['source']):
            _reject('Original product allowance changed')
        for scope in ('product:' + allowance['product_id'], 'source:' + contract['runtime_recipe']['source']):
            found = runtime.execute('SELECT budget_id FROM product_budget_scope WHERE scope=?', (scope,)).fetchone()
            if not found or found[0] != allowance['budget_id']:
                _reject('Original product/source budget binding changed')
        attempts = budgets.attempts_for(runtime, allowance['budget_id'])
    if any(type(a.get('budget_seconds')) is not int or a['budget_seconds'] < 1 for a in attempts):
        _reject('Original runtime charges are incomplete')
    charged = sum(a['budget_seconds'] for a in attempts)
    seconds = contract['runtime_recipe']['max_seconds']
    if (len(attempts) >= allowance['max_assignments'] or allowance['max_seconds'] - charged < seconds
            or seconds > allowance['max_job_seconds']):
        _reject('The unchanged product allowance cannot cover one remaining assessment')
    return {'phase': 'final-assessment', 'ready': True, 'required_assignments': 1, 'required_seconds': seconds,
            'lifecycle_assignments': count, 'lifecycle_remaining_assignments': 1,
            'attempts_reserved': len(attempts), 'seconds_reserved': charged,
            'remaining_seconds': allowance['max_seconds'] - charged, 'reservation_performed': False}


def _resources(db, work_id, contract, failed_assignment_id):
    rows = [dict(row) for row in db.execute('SELECT * FROM resource WHERE work_id=? ORDER BY resource_id', (work_id,))]
    if ({row['resource_id'] for row in rows} != set(contract['resources'])
            or any(row['state'] != 'frozen' or row['assignment_id'] not in (None, failed_assignment_id) for row in rows)):
        _reject('The exact approved resources must remain frozen under the failed assessment')
    return rows


def _held(lifecycle, db, meta, *, published=False):
    run, contract = lifecycle._run(db, meta['work_id'])
    blockers = {r[0] for r in db.execute('SELECT code FROM technical_blocker WHERE work_id=?', (meta['work_id'],))}
    if (run['control'] != 'none' or run['execution'] != 'held'
            or json.loads(run['candidate'] or 'null') != meta['incident']['candidate']
            or blockers != (set() if published else {'assessment_failed'})
            or db.execute("SELECT 1 FROM assignment WHERE work_id=? AND state NOT IN ('completed','failed','cancelled')", (meta['work_id'],)).fetchone()
            or db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (meta['work_id'],)).fetchone()
            or db.execute('SELECT 1 FROM lifecycle WHERE work_id<>? AND admitted=1', (meta['work_id'],)).fetchone()):
        _reject('The exact frozen source-review hold and reconciled ownership are required')
    if not published and contract != meta['original_contract']:
        _reject('Original approved contract changed before review continuation')
    if _resources(db, meta['work_id'], contract, meta['incident']['failed_assignment_id']) != meta['frozen_resources']:
        _reject('Retained frozen resource ownership changed')
    if _incident(lifecycle, db, meta['work_id'], meta['incident']['failed_assignment_id'], meta['original_contract']) != meta['incident']:
        _reject('Retained failure evidence changed')
    return run, contract


def _concept(previous, meta, recipe):
    original_feature = retained._original(previous['assessment_continuation']['original_prepared_packet'])
    concept = copy.deepcopy(previous['concept'])
    concept.update(operation_id=meta['operation_id'] + '-present', expected_work_revision=meta['expected_work_revision'],
                   revision=previous['concept']['revision'] + 1)
    envelope = concept['execution_envelope']
    envelope.update(revision=envelope['revision'] + 1, runtime_recipe=recipe, runtime_recipe_digest=digest(recipe))
    evidence = recipe['source_review_evidence']
    concept['body'] = original_feature['concept']['body'] + '\n\nTechnical continuation: supply verified baseline-to-candidate source evidence to the independent reviewer. Candidate ' + meta['incident']['candidate']['identity'] + '. Preserve all feature behavior, permissions, commands, original baseline, previous attempts and charges. Use the single remaining assessment slot; no additional correction, startup retry or product budget is granted.'
    concept.update(recommendation='Approve the exact source-review evidence handoff and final assessment.',
                   alternative='Retain the candidate and evidence in the current held state.',
                   consequence='One remaining independent assessment may run; tests still require its passing review.')
    concept['decision_context'] = {
        'problem': 'The previous reviewer could read candidate files but could not reconstruct their exact Git baseline within its approved read scope.',
        'example': 'The coordinator supplies verified baseline and candidate bytes plus their patch in the assessor output. No Git permission expansion is granted.',
        'effort': 'One remaining product assessment within the original assignment/time limits. ' + meta['readiness_description'],
        'dependencies': 'Runtime ' + meta['runtime_identity']['runtime_source_digest'] + '; unchanged model, permissions, checks, source and product allowance.',
        'evidence': 'Blocked review ' + canonical(meta['incident']['review']) + '; baseline/candidate artifact ' + canonical(evidence) + '; technical readiness ' + canonical(meta['readiness_extension']),
        'stopping_point': 'Deliver only after independent review and every required native check pass, with known cleanup. Hold on any remaining finding, evidence drift, ownership uncertainty or limit; no commit or publication.'}
    return concept


def _validate_descriptor(descriptor, contract, frozen, work_id, roots):
    source_evidence.validate(descriptor, source=Path(contract['runtime_recipe']['source']),
        baseline_identity=contract['baseline'], candidate=frozen, work_id=work_id,
        recipe=contract['runtime_recipe'], writable_roots=tuple(roots))


def prepare(lifecycle, *, operation_id, work_id, failed_assignment_id, source_review_evidence, readiness_extra_attempt=False,
            readiness_runner=None):
    for value in (operation_id, work_id, failed_assignment_id):
        identifier(value)
    if type(readiness_extra_attempt) is not bool:
        _reject('Readiness extension selection must be explicit')
    with fencing._worker_fence(lifecycle.store) as verify:
        with lifecycle.store._connection() as db:
            run, contract = lifecycle._run(db, work_id)
            prior = db.execute('SELECT operation_id,packet_digest FROM assessment_continuation WHERE work_id=?', (work_id,)).fetchone()
            if not prior:
                _reject('The exact previous assessment continuation is required')
            previous_path = lifecycle.store.directory / ('prepared-assessment-' + work_id + '-' + prior['operation_id'] + '.json')
            previous, checksum = retained._file(previous_path)
            if digest(previous) != prior['packet_digest']:
                _reject('Previous preparation differs from its immutable registered audit')
            destination = lifecycle.store.directory / ('prepared-assessment-review-' + work_id + '-' + operation_id + '.json')
            if destination.exists() or destination.is_symlink():
                request, _ = project._load(Path(previous['input_bindings']['request']['path']), project.WorkRequest)
                packet = load(destination, previous['input_bindings'], previous['project'], request)
                if (packet[KEY]['incident']['failed_assignment_id'] != failed_assignment_id
                        or packet['concept']['execution_envelope']['runtime_recipe']['source_review_evidence'] != source_review_evidence
                        or bool(packet[KEY]['readiness_extension']) != readiness_extra_attempt):
                    _reject('Retained review preparation belongs to another exact request')
                return packet
            worker = db.execute('SELECT * FROM worker_status WHERE singleton=1').fetchone()
            if not worker or worker['state'] != 'stopped':
                _reject('The previous foreground owner must be positively stopped')
            meta = {'format': FORMAT, 'operation_id': operation_id, 'work_id': work_id,
                    'original_contract': contract, 'previous_prepared_packet': {'path': str(previous_path), 'sha256': checksum},
                    'incident': _incident(lifecycle, db, work_id, failed_assignment_id, contract),
                    'frozen_resources': _resources(db, work_id, contract, failed_assignment_id),
                    'worker_stop': dict(worker), 'expected_work_revision': lifecycle.store._work(db, work_id)['revision']}
            _held(lifecycle, db, meta)
            capacity = _capacity(lifecycle, db, work_id, contract)
        if 'source_review_evidence' in contract['runtime_recipe']:
            _reject('This narrow route adds source evidence only once')
        _validate_descriptor(source_review_evidence, contract, meta['incident']['candidate'], work_id, lifecycle.store.worker_writable_roots)
        recipe = copy.deepcopy(contract['runtime_recipe'])
        recipe['source_review_evidence'] = copy.deepcopy(source_review_evidence)
        recipe = readiness._normalise(recipe, lifecycle.store.directory)
        config = previous['project']
        extension = technical.extend(config, previous, operation_id, work_id) if readiness_extra_attempt else None
        limit = technical.validate(extension, config, previous)['max_attempts'] if extension else previous['readiness'].get('limits', {}).get('max_attempts', config['readiness']['max_attempts'])
        receipt = (readiness_runner or readiness.run_readiness)(copy.deepcopy(recipe), state_dir=lifecycle.store.directory,
            readiness_dir=Path(config['readiness_dir']), model=contract['model'], effort=contract['effort'],
            max_seconds=config['readiness']['max_seconds'], max_attempts=limit, native_checks=[])
        prepared, identity = project._successful_readiness(receipt, recipe)
        if identity != meta['incident']['candidate']['identity']:
            _reject('Fresh readiness did not preserve the exact frozen candidate')
        if extension:
            technical.validate_receipt(receipt, extension, config, previous)
        meta.update(runtime_identity=readiness._runtime_identity(), readiness_extension=extension,
                    readiness_description=(f'Cumulative metadata-only readiness limit {limit - 1} to {limit}; unchanged {config["readiness"]["max_seconds"]}-second ceiling and all prior records retained.' if extension else 'Existing technical readiness allowance retained.'))
        packet = copy.deepcopy(previous)
        packet.update({KEY: meta, 'readiness': receipt, 'finish_capacity': capacity,
                       'concept': _concept(previous, meta, prepared)})
        project._unchanged(packet['input_bindings'])
        with lifecycle.store._connection() as db:
            _held(lifecycle, db, meta)
            if lifecycle.store._work(db, work_id)['revision'] != meta['expected_work_revision']:
                _reject('Work changed during technical preparation')
        verify()
        return packet


def validate(packet, bindings, config, request):
    meta = packet.get(KEY, {})
    if meta.get('format') != FORMAT:
        _reject('A source-review continuation packet is required')
    previous = _previous(meta['previous_prepared_packet'])
    if (packet['input_bindings'] != bindings or previous['input_bindings'] != bindings
            or packet['project'] != config or previous['project'] != config
            or packet['introduction'] != request['introduction'] or previous['introduction'] != request['introduction']
            or any(packet.get(k) != previous.get(k) for k in set(previous) - {'concept', 'readiness', 'finish_capacity'})):
        _reject('Continuation changed original inputs or non-technical preparation')
    project._unchanged(bindings)
    old = meta['original_contract']
    previous_bounds = previous['concept']['execution_envelope']
    if previous_bounds != {k: v for k, v in old.items() if k not in ('concept_revision', 'concept_digest')}:
        _reject('Previous preparation and approved contract differ')
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    if ({k: v for k, v in recipe.items() if k != 'source_review_evidence'} != old['runtime_recipe']
            or 'source_review_evidence' not in recipe or packet['concept'] != _concept(previous, meta, recipe)):
        _reject('Only the exact source-review evidence descriptor may change execution')
    extension = meta['readiness_extension']
    if extension:
        checked = technical.validate(extension, config, previous)
        technical.validate_receipt(packet['readiness'], extension, config, previous)
        description = f'Cumulative metadata-only readiness limit {checked["max_attempts"] - 1} to {checked["max_attempts"]}; unchanged {config["readiness"]["max_seconds"]}-second ceiling and all prior records retained.'
        if checked['operation_id'] != meta['operation_id'] or checked['work_id'] != meta['work_id']:
            _reject('Technical readiness extension belongs to another continuation')
    else:
        description = 'Existing technical readiness allowance retained.'
    if meta['readiness_description'] != description or meta['runtime_identity'] != readiness._runtime_identity():
        _reject('Readiness limits or current runtime identity changed')
    prepared, identity = project._successful_readiness(packet['readiness'], recipe)
    if (prepared != recipe or identity != meta['incident']['candidate']['identity']
            or packet['readiness']['runtime_identity'] != meta['runtime_identity']):
        _reject('Fresh readiness does not bind the current candidate/runtime')
    _validate_descriptor(recipe['source_review_evidence'], old, meta['incident']['candidate'], meta['work_id'], config['worker_writable_roots'])
    return packet


def load(path, bindings, config, request):
    return validate(retained._file(path)[0], bindings, config, request)


def _validated(packet):
    bindings = packet['input_bindings']
    config, first = project._load(Path(bindings['project']['path']), project.ProjectConfiguration)
    request, second = project._load(Path(bindings['request']['path']), project.WorkRequest)
    return validate(packet, {'project': first, 'request': second}, config, request)


def publish(lifecycle, packet):
    _validated(packet)
    meta = packet[KEY]
    work_id, operation = meta['work_id'], meta['operation_id']
    package = retained._package(packet['concept'])
    target = digest(package)
    expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': target}
    with lifecycle.store._connection() as db:
        _, current = lifecycle._run(db, work_id)
        if current == expected:
            _schema(db)
            row = db.execute(f'SELECT packet_digest FROM {TABLE} WHERE operation_id=?', (operation,)).fetchone()
            if not row or row[0] != digest(packet):
                _reject('Current contract lost its immutable continuation audit')
            return target
        _held(lifecycle, db, meta)
        if _capacity(lifecycle, db, work_id, current) != packet['finish_capacity']:
            _reject('Retained remaining capacity or historical charges changed')
        work = lifecycle.store._work(db, work_id)
        if work['current_concept'] == package['revision']:
            saved = db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?', (work_id, package['revision'])).fetchone()
            if not saved or json.loads(saved[0]) != package:
                _reject('Another concept superseded this continuation')
        elif work['revision'] != meta['expected_work_revision']:
            _reject('Work changed before technical presentation')
    lifecycle.store.present(**packet['concept'])
    with lifecycle.store._transaction() as db:
        _held(lifecycle, db, meta)
        if lifecycle._approved_envelope_errors(db, work_id, expected):
            _reject('New concept and execution contract differ')
        from . import plan_runtime
        _, prior_contract = lifecycle._run(db, work_id)
        plan_runtime.inherit_frozen_candidate_in(lifecycle, db, work_id, prior_contract['concept_digest'], meta['incident']['candidate'])
        _schema(db, create=True)
        record = {'format': FORMAT, 'metadata': meta, 'registered_contract': expected, 'packet_digest': digest(packet)}
        db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?)', (operation, work_id, digest(packet), canonical(record)))
        db.execute("UPDATE lifecycle SET contract=?,admitted=0,execution='held' WHERE work_id=?", (canonical(expected), work_id))
        db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='assessment_failed'", (work_id,))
        lifecycle._touch(db, work_id, 'assessment_review_continuation_presented', {'operation_id': operation, 'digest': target,
                                                                               'failed_assignment_id': meta['incident']['failed_assignment_id']})
    return target


def admit(lifecycle, packet):
    _validated(packet)
    meta = packet[KEY]
    work_id, operation = meta['work_id'], meta['operation_id']
    lifecycle.reconcile(work_id)
    with lifecycle.store._transaction() as db:
        _schema(db)
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (operation,)).fetchone()
        _, contract = lifecycle._run(db, work_id)
        if not row or json.loads(row[0])['registered_contract'] != contract or json.loads(row[0])['packet_digest'] != digest(packet):
            _reject('Exact current source-review continuation audit is missing')
        prior = db.execute('SELECT * FROM assignment WHERE predecessor=?', ('reassessment:' + operation,)).fetchone()
        if prior:
            if prior['concept_digest'] != contract['concept_digest'] or prior['kind'] != 'assessment':
                _reject('The reassessment predecessor belongs to another binding')
            return {'work_id': work_id, 'assignment_id': prior['assignment_id'], 'already_admitted': True}
        _held(lifecycle, db, meta, published=True)
        errors = lifecycle._admission_errors(db, work_id)
        if errors:
            _reject('; '.join(errors))
        capacity = _capacity(lifecycle, db, work_id, contract)
        if capacity != packet['finish_capacity']:
            _reject('Retained remaining capacity or historical charges changed')
        aid = lifecycle._enqueue(db, work_id, 'assessment', 'reassessment:' + operation)
        if aid is None:
            _reject('The original cumulative assignment limit prevents the final assessment')
        db.execute("UPDATE resource SET state='frozen',assignment_id=NULL,observed_at=? WHERE work_id=?", (now(), work_id))
        db.execute("UPDATE lifecycle SET admitted=1,execution='eligible' WHERE work_id=?", (work_id,))
        lifecycle._touch(db, work_id, 'reassessment_requested', {'assignment_id': aid})
        lifecycle._touch(db, work_id, 'assessment_review_continuation_admitted', {'operation_id': operation,
            'assignment_id': aid, 'capacity': capacity, 'charges_preserved': True})
        return {'work_id': work_id, 'assignment_id': aid, 'execution': 'eligible'}
