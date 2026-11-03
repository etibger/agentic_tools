"""One explicit product continuation after a recovered pre-thread observation failure.

This does not repair historical observation coverage. The new human binding
acknowledges that uncertainty, while an immutable raw guard and a durable,
non-retryable claim preserve the original failure and every observed floor.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import budgets, resource_limits
from . import startup_failure_recovery as recovery
from .lifecycle import stop_conflicts_in
from .store import Rejected, canonical, digest, identifier

FORMAT = resource_limits.CONTINUATION_FORMAT
TABLE = 'startup_resource_continuation'
LIMITS = resource_limits.CONTINUATION_LIMITS
SCHEMA = {
    TABLE: "CREATE TABLE startup_resource_continuation (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,request_fingerprint TEXT NOT NULL,original_guard_raw TEXT NOT NULL,original_contract_raw TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('reserved','success','failed')),outcome TEXT)",
    TABLE + '_insert': """CREATE TRIGGER startup_resource_continuation_insert BEFORE INSERT ON startup_resource_continuation BEGIN
        SELECT CASE WHEN NEW.state!='reserved' OR NEW.outcome IS NOT NULL
          OR EXISTS(SELECT 1 FROM startup_resource_continuation WHERE operation_id=NEW.operation_id OR work_id=NEW.work_id)
          THEN RAISE(ABORT,'Continuation must begin with one immutable reservation') END;
    END""",
    TABLE + '_update': """CREATE TRIGGER startup_resource_continuation_update BEFORE UPDATE ON startup_resource_continuation BEGIN
        SELECT CASE WHEN OLD.state!='reserved' OR NEW.state NOT IN ('success','failed') OR NEW.outcome IS NULL
          OR NEW.operation_id IS NOT OLD.operation_id OR NEW.work_id IS NOT OLD.work_id
          OR NEW.request_fingerprint IS NOT OLD.request_fingerprint OR NEW.original_guard_raw IS NOT OLD.original_guard_raw
          OR NEW.original_contract_raw IS NOT OLD.original_contract_raw
          THEN RAISE(ABORT,'Continuation history and terminal outcomes are immutable') END;
    END""",
    TABLE + '_delete': """CREATE TRIGGER startup_resource_continuation_delete BEFORE DELETE ON startup_resource_continuation BEGIN
        SELECT RAISE(ABORT,'Continuation history cannot be removed');
    END""",
}


class StartupResourceContinuationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    recovery_operation_id: str
    decision_id: str
    original_guard_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    evidence_path: str
    evidence_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def _reject(message):
    raise Rejected('startup_resource_continuation', message)


def _sha(raw):
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()


def verify_schema(db, *, allow_absent=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'startup_resource_continuation*' "
                            "OR (tbl_name='startup_resource_continuation' AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))").fetchall())
    if not found and allow_absent:
        return False
    if found != SCHEMA:
        _reject('The exact continuation audit schema is missing or changed')
    return True


def _initialize(db):
    if verify_schema(db, allow_absent=True):
        return
    for sql in SCHEMA.values():
        db.execute(sql)
    verify_schema(db)


def _claim(db, request, fingerprint):
    if not verify_schema(db, allow_absent=True):
        return None
    row = db.execute(f'SELECT * FROM {TABLE} WHERE work_id=? OR operation_id=?',
                     (request['work_id'], request['operation_id'])).fetchone()
    if row is None:
        return None
    if (row['work_id'] != request['work_id'] or row['operation_id'] != request['operation_id']
            or row['request_fingerprint'] != fingerprint):
        _reject('This work already owns its one exact continuation claim')
    if row['state'] == 'reserved':
        return {'accepted': False, 'applied': False, 'status': 'held', 'operation_id': request['operation_id'],
                'grants_work': False, 'grants_time': False, 'detail': 'Original observation claim is unresolved; no retry or new scan'}
    return recovery._json(row['outcome'])


def admission_predecessor(db, work_id, contract):
    """Read the one completed disposition; assignment.predecessor consumes it."""
    reference = contract.get('runtime_recipe', {}).get('resource_observation_continuation')
    if reference is None:
        return None
    verify_schema(db)
    row = db.execute(f'SELECT * FROM {TABLE} WHERE work_id=?', (work_id,)).fetchone()
    if not row or row['state'] != 'success':
        _reject('The exact continuation has not completed successfully')
    outcome = recovery._json(row['outcome'])
    audit = db.execute(f'SELECT record FROM {recovery.TABLE} WHERE operation_id=?',
                       (outcome.get('recovery_operation_id'),)).fetchone()
    predecessor = 'startup-continuation:' + row['operation_id']
    if (outcome.get('reference') != reference or row['operation_id'] != reference.get('operation_id')
            or outcome.get('registered_contract_digest') != digest(contract)
            or outcome.get('admission_predecessor') != predecessor or outcome.get('work_id') != work_id
            or _sha(row['original_guard_raw']) != reference.get('original_guard_sha256')
            or not audit or digest(recovery._json(audit[0])) != outcome.get('recovery_audit_digest')):
        _reject('The successful disposition does not bind the current exact contract')
    return predecessor


def _absent_sidecars(store, work):
    result = []
    for path in (store.path, store.directory / 'codex-runtime.sqlite3'):
        saved, value = recovery._sidecar(path, work)
        if value is not None or saved.get('absent') is not True:
            _reject('A failure sidecar prevents this narrow continuation; never remove or ignore it')
        result.append(saved)
    return result


def _evidence(store, request, audit, guard):
    packet = recovery._json(recovery._file(store, {'path': request['evidence_path'], 'sha256': request['evidence_sha256']}))
    keys = {'format', 'work_id', 'recovery_operation_id', 'recovery_audit_digest', 'original_guard_sha256', 'failed_guard',
            'historical_breach', 'unobserved_interval', 'acknowledged_limits'}
    if (not isinstance(packet, dict) or set(packet) != keys or packet['format'] != FORMAT
            or any(packet[key] != request[key] for key in ('work_id', 'recovery_operation_id', 'original_guard_sha256'))
            or packet['recovery_audit_digest'] != digest(audit)
            or packet['historical_breach'] != guard['breach'] or packet['acknowledged_limits'] != LIMITS):
        _reject('Exact evidence must explicitly retain the historical observation uncertainty')
    failed_raw = recovery._file(store, packet['failed_guard'], immutable=True)
    if _sha(failed_raw) != audit['original_guard_sha256']:
        _reject('The immutable failed guard is not the recovered historical guard')
    failed = recovery._json(failed_raw)
    interval = {'start': failed['last_observation']['observed_at'],
                'end': audit['terminal_receipt']['resource_limits']['observed_at']}
    if (packet['unobserved_interval'] != interval
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in interval.values())
            or not interval['start'] <= guard['breach']['observed_at'] <= interval['end']
            or failed['breach'] != guard['breach']
            or any(failed.get(k) != guard.get(k) for k in ('baseline', 'scope', 'scope_digest', 'carry_forward_reference', 'carry_forward'))):
        _reject('Historical interval, baseline or accounting differs from the recovered evidence')
    for key, cap in (('output_roots', 'max_output_growth_bytes'), ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
        if not 0 <= failed['high_water_growth_bytes'][key] <= guard['high_water_growth_bytes'][key] <= guard['scope'][cap]:
            _reject('A historical byte violation or reduced observed floor cannot be acknowledged')
    if min(failed['last_observation']['available_bytes'].values(), default=0) < guard['scope']['min_free_bytes']:
        _reject('A recorded free-space violation cannot be acknowledged')
    return packet


def _fields_compatible(old, new, replacements):
    """Refresh named evidence fields without admitting unknown contract keys."""
    return (isinstance(old, dict) and isinstance(new, dict) and set(old) == set(new)
            and all(value == new[key] or (key in replacements and replacements[key](value, new[key]))
                    for key, value in old.items()))


def _text_replacement(old, new):
    return isinstance(new, str) and bool(new.strip())


def _digest_replacement(old, new):
    return isinstance(new, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', new) is not None


def _file_replacement(old, new):
    # Current proof summaries may become sealed references; their contents are
    # evidence, never a replacement for the separately frozen execution controls.
    return (isinstance(new, dict) and set(new) == {'path', 'sha256', 'bytes'}
            and isinstance(new['path'], str) and Path(new['path']).is_absolute()
            and isinstance(new['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', new['sha256']) is not None
            and type(new['bytes']) is int and new['bytes'] > 0)


def _accounting_compatible(old, new):
    replacements = {key: _file_replacement for key in ('latest_readonly_preflight',)}
    replacements.update({key: lambda a, b: True for key in
                         ('minimum_available_bytes_at_preflight', 'post_export_growth_bytes_at_preflight',
                          'remaining_after_observed_post_export_growth_bytes')})
    replacements['normal_import'] = _text_replacement
    if not _fields_compatible(old, new, replacements):
        return False
    available = new.get('minimum_available_bytes_at_preflight')
    if type(available) is not int or type(new.get('min_free_bytes')) is not int or available < new['min_free_bytes']:
        return False
    growth, remaining = new.get('post_export_growth_bytes_at_preflight'), new.get('remaining_after_observed_post_export_growth_bytes')
    previous, caps = old.get('post_export_growth_bytes_at_preflight'), new.get('exact_remaining_limits')
    groups = {'output_roots': 'max_output_growth_bytes', 'source_evidence_roots': 'max_source_evidence_growth_bytes'}
    if any(not isinstance(value, dict) for value in (growth, remaining, previous, caps)):
        return False
    if set(growth) != set(groups) or set(remaining) != set(groups) or set(previous) != set(groups):
        return False
    return all(type(growth[key]) is int and type(previous[key]) is int and type(caps.get(cap)) is int
               and 0 <= previous[key] <= growth[key] <= caps[cap]
               and type(remaining[key]) is int and remaining[key] == caps[cap] - growth[key]
               for key, cap in groups.items())


def _launch_compatible(old, new):
    def recipe_file(a, b):
        return (_fields_compatible(a, b, {'path': _text_replacement, 'sha256': lambda x, y:
                                         isinstance(y, str) and re.fullmatch(r'[a-f0-9]{64}', y) is not None})
                and set(b) == {'path', 'sha256'} and Path(b['path']).is_absolute())

    def recipe_map(a, b):
        return _fields_compatible(a, b, {
            'path': lambda x, y: isinstance(y, str) and Path(y).is_absolute(),
            'sha256': lambda x, y: isinstance(y, str) and re.fullmatch(r'[a-f0-9]{64}', y) is not None,
            'bytes': lambda x, y: type(y) is int and y > 0,
            'recipe': recipe_file, 'recipe_digest': _digest_replacement})

    if not _fields_compatible(old, new, {'launch_condition': _text_replacement, 'runtime_recipes': recipe_map,
                                         'argv': lambda a, b: isinstance(a, list) and isinstance(b, list)}):
        return False
    before, after = old.get('argv'), new.get('argv')
    if (not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after)
            or before.count('--runtime-recipes') != 1 or after.count('--runtime-recipes') != 1):
        return False
    index = before.index('--runtime-recipes') + 1
    if index >= len(before) or before[:index] != after[:index] or before[index + 1:] != after[index + 1:]:
        return False
    return (isinstance(old.get('runtime_recipes'), dict) and isinstance(new.get('runtime_recipes'), dict)
            and before[index] == old['runtime_recipes'].get('path')
            and after[index] == new['runtime_recipes'].get('path'))


def _current_evidence_compatible(old, new):
    replacements = {key: _file_replacement for key in (
        'actual_folder_coordinator', 'actual_product_role_readiness', 'affected_verification',
        'feature_binding_preparation', 'final_readiness_preflight', 'frozen_aos_candidate',
        'native_metadata_equivalence', 'runtime_recipe_file', 'worker_configuration')}
    replacements.update(frozen_aos_source_digest=_digest_replacement, limitation=_text_replacement,
                        model_permission_mode=lambda a, b: _fields_compatible(
                            a, b, {'actual_product_roles_status': _text_replacement}))
    return _fields_compatible(old, new, replacements)


def _contract_compatible(old, new):
    """Keep settled controls exact while replacing explicitly approved proof references."""
    before = dict(old)
    # Older registration omitted this derived field. No other new key is implicit.
    if 'runtime_recipe_digest' not in before and 'runtime_recipe_digest' in new:
        before['runtime_recipe_digest'] = digest(old.get('runtime_recipe'))
    replacements = {key: lambda a, b: True for key in
                    ('revision', 'concept_revision', 'concept_digest', 'runtime_recipe_digest',
                     'prerequisites', 'qualified_route', 'max_assignments')}
    replacements.update(
        runtime_recipe=lambda a, b: isinstance(a, dict) and isinstance(b, dict)
        and 'resource_observation_continuation' not in a
        and {key: value for key, value in b.items() if key != 'resource_observation_continuation'} == a,
        baseline_provenance=lambda a, b: _fields_compatible(a, b, {'current_revalidation': _file_replacement}),
        resource_accounting_binding=_accounting_compatible, runtime_recipe_status=_text_replacement,
        worker_configuration=_file_replacement, worker_launch_binding=_launch_compatible,
        evidence=_current_evidence_compatible,
        prerequisite_evidence_binding=lambda a, b: _fields_compatible(a, b, {
            key: _file_replacement for key in ('actual_folder_coordinator', 'current_source_budget_resource_ownership',
                                               'role_qualification', 'worker_configuration')}))
    if not _fields_compatible(before, new, replacements):
        return False
    evidence = new.get('evidence', {})
    prerequisites = new.get('prerequisite_evidence_binding', {})
    launch = new.get('worker_launch_binding', {})
    if any(not isinstance(value, dict) for value in (evidence, prerequisites, launch)):
        return False
    # Repeated current bindings must identify the same concrete files.
    for values in (
        [new.get('worker_configuration'), evidence.get('worker_configuration'), prerequisites.get('worker_configuration')],
        [evidence.get('actual_folder_coordinator'), prerequisites.get('actual_folder_coordinator')],
        [new.get('baseline_provenance', {}).get('current_revalidation'),
         new.get('resource_accounting_binding', {}).get('latest_readonly_preflight'),
         evidence.get('final_readiness_preflight'), prerequisites.get('current_source_budget_resource_ownership')],
    ):
        present = [value for value in values if value is not None]
        if present and any(value != present[0] for value in present):
            return False
    if launch:
        recipe_map = launch.get('runtime_recipes', {})
        recipe_file = evidence.get('runtime_recipe_file')
        if (recipe_map.get('recipe_digest') != digest(new.get('runtime_recipe'))
                or (recipe_file is not None and recipe_map.get('recipe') != {
                    key: recipe_file.get(key) for key in ('path', 'sha256')})):
            return False
    return True


def _approval(lifecycle, db, request, historical, packet):
    work = lifecycle.store._work(db, request['work_id'])
    lifecycle._command_guard(db, request['work_id'], request['expected_work_revision'], request['digest'])
    run, old_contract = lifecycle._run(db, request['work_id'])
    if (any(old_contract.get(key) != historical[key] for key in ('concept_revision', 'concept_digest'))
            or old_contract.get('revision') != historical['contract_revision']):
        _reject('The original registered contract no longer matches the recovered assignment')
    concept = db.execute('SELECT * FROM concept WHERE work_id=? AND revision=?',
                         (request['work_id'], work['current_concept'])).fetchone()
    decision = db.execute('SELECT * FROM decision WHERE decision_id=?', (request['decision_id'],)).fetchone()
    if not concept or not decision:
        _reject('A new saved exact human decision is required')
    decision = dict(decision)
    challenge = db.execute('SELECT * FROM challenge WHERE challenge_id=?', (decision['challenge_id'],)).fetchone()
    readback = db.execute('SELECT status FROM decision_readback WHERE decision_id=?', (request['decision_id'],)).fetchone()
    package = recovery._json(concept['package'])
    if (decision['outcome'] != 'approved' or decision['provenance_kind'] != 'qualified-host-response'
            or decision['work_id'] != request['work_id'] or decision['concept_revision'] != work['current_concept']
            or decision['concept_revision'] <= historical['concept_revision']
            or decision['digest'] != request['digest'] or concept['digest'] != request['digest']
            or digest(package) != request['digest'] or decision['package_snapshot'] != concept['package']
            or decision['package_id'] != package.get('package_id')
            or recovery._json(decision['response']) != {'action': 'accept', 'approved': True}
            or any(not decision.get(k) for k in ('exact_answer', 'source_channel', 'interaction_reference', 'requested_at', 'created_at'))
            or not challenge or challenge['status'] != 'approved'
            or any(challenge[k] != decision[k] for k in ('work_id', 'concept_revision', 'digest'))
            or challenge['created_at'] != decision['requested_at'] or not readback or readback[0] != 'verified'):
        _reject('The new exact qualified human approval and verified provenance are required')
    operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (challenge['operation_id'],)).fetchone()
    expected = {**decision, 'response': recovery._json(decision['response'])}
    if not operation or recovery._json(operation[0]).get('receipt') != expected:
        _reject('The saved human decision operation does not match its immutable receipt')
    envelope = package.get('execution_envelope', {})
    contract = {**envelope, 'concept_revision': work['current_concept'], 'concept_digest': request['digest']}
    # The stored contract need not repeat the recipe hash; it is checked against
    # the exact human package below and remains part of the archived new binding.
    if lifecycle._approved_envelope_errors(db, request['work_id'], contract):
        _reject('The new execution contract must exactly match the human-approved package')
    if (contract.get('revision') != old_contract['revision'] + 1
            or not _contract_compatible(old_contract, contract)
            or not contract.get('qualified_route') or not isinstance(contract.get('prerequisites'), dict)
            or set(contract['prerequisites']) != set(old_contract['prerequisites'])
            or not all(value is True for value in contract['prerequisites'].values())):
        _reject('Continuation preserves the settled contract and requires every existing prerequisite qualified')
    recipe = contract.get('runtime_recipe')
    reference = {key: request[key] for key in ('operation_id', 'original_guard_sha256', 'evidence_sha256')}
    reference.update(unobserved_interval=packet['unobserved_interval'], acknowledged_limits=LIMITS)
    if (not isinstance(recipe, dict) or recipe.get('resource_observation_continuation') != reference
            or package['execution_envelope'].get('runtime_recipe_digest') != digest(recipe)
            or contract['concept_revision'] != work['current_concept'] or contract['concept_digest'] != request['digest']):
        _reject('The approved and registered recipe must name this exact continuation and uncertainty')
    return run, contract, recipe, decision, reference


def _state(lifecycle, db, request, identities):
    store, work = lifecycle.store, request['work_id']
    recovery._audit_schema(db)
    verify_schema(db, allow_absent=True)
    row = db.execute(f'SELECT record FROM {recovery.TABLE} WHERE operation_id=?', (request['recovery_operation_id'],)).fetchone()
    if row is None:
        _reject('Apply the qualified startup recovery before considering continuation')
    audit = recovery._json(row[0])
    aid = audit.get('attempt_id')
    historical = db.execute('SELECT * FROM assignment WHERE attempt_id=? AND work_id=?', (aid, work)).fetchone()
    raw_guard = db.execute('SELECT record FROM runtime.resource_limit WHERE work_id=?', (work,)).fetchone()
    raw_runtime = db.execute('SELECT record FROM runtime.attempt WHERE id=?', (aid,)).fetchone()
    if not historical or not raw_guard or not raw_runtime:
        _reject('Original recovered assignment, runtime and guard must remain present')
    guard, original = recovery._json(raw_guard[0]), recovery._json(raw_runtime[0])
    terminal = audit.get('terminal_receipt', {})
    historical_sidecars = audit.get('evidence', {}).get('snapshot', {}).get('failure_sidecars')
    if (audit.get('format') != recovery.FORMAT or audit.get('work_id') != work
            or audit.get('resource_breach_acknowledged') is not False or audit.get('new_execution_authorized') is not False
            or _sha(raw_runtime[0]) != audit.get('original_runtime_sha256')
            or _sha(raw_guard[0]) != request['original_guard_sha256'] or digest(guard) != audit.get('finished_guard_digest')
            or historical['state'] != 'failed' or recovery._json(historical['result']) != terminal
            or terminal.get('provenance') != 'separately-derived-after-failure'
            or terminal.get('format') != recovery.FORMAT + '-terminal'
            or terminal.get('producer_manifest_sha256') != recovery.PRODUCER_MANIFEST
            or terminal.get('failure') != recovery.FAILURE
            or terminal.get('stopped') is not True or terminal.get('effects_reconciled') is not True
            or terminal.get('owned_processes') != [] or terminal.get('model_calls_attempted') != 0
            or terminal.get('partial_mutations') != {'status': 'none', 'changes': []}
            or any(original.get(k) is not None for k in ('thread_id', 'turn_id'))
            or original.get('model_calls_attempted') != 0 or original.get('phase') != 'intent'
            or guard.get('active_attempt') is not None or guard.get('continuation') is not None
            or guard.get('scope_digest') != digest(guard['scope'])
            or guard.get('breach', {}).get('reason') != 'observation_failed'
            or guard['breach'].get('error_code') != 'resource_observation'
            or guard['breach'].get('error_message') != 'Resource observation exceeded its bounded scan'):
        _reject('Only the exact separately recovered pre-thread observation failure is eligible')
    if (not isinstance(historical_sidecars, list) or len(historical_sidecars) != 2
            or any(item.get('absent') is not True for item in historical_sidecars)):
        _reject('Historical failure sidecars must also have been positively absent')
    packet = _evidence(store, request, audit, guard)
    run, contract, recipe, decision, reference = _approval(lifecycle, db, request, historical, packet)
    old_recipe = recovery._json(run['contract']).get('runtime_recipe')
    if not old_recipe or digest(old_recipe) != original['recipe_digest']:
        _reject('The recovered original recipe changed before continuation')
    if (run['admitted'] or run['control'] != 'none' or run['execution'] not in ('held', 'paused')
            or db.execute('SELECT 1 FROM resource WHERE work_id=?', (work,)).fetchone()
            or db.execute("SELECT 1 FROM assignment WHERE work_id=? AND state NOT IN ('completed','failed','cancelled')", (work,)).fetchone()
            or db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (work,)).fetchone()
            or stop_conflicts_in(db, work)
            or db.execute("SELECT 1 FROM worker_status WHERE state<>'stopped'").fetchone()
            or db.execute('SELECT 1 FROM lifecycle WHERE admitted=1').fetchone()
            or db.execute("SELECT 1 FROM runtime.native_check WHERE attempt_id=? OR json_extract(record,'$.phase') IS NOT 'clean'", (aid,)).fetchone()
            or db.execute("SELECT 1 FROM runtime.resource_limit WHERE json_extract(record,'$.active_attempt') IS NOT NULL").fetchone()):
        _reject('Passive continuation requires stopped ownership and no unresolved model/native work')
    if (resource_limits.scope(recipe) != guard['scope']
            or recipe.get('resource_limits_carry_forward') != guard.get('carry_forward_reference')):
        _reject('Resource roots, baseline capsule and all original caps must remain unchanged')
    carried = resource_limits._carry_forward(recipe, guard['scope'])
    if (not carried or carried['target_work_id'] != work or carried['baseline'] != guard['baseline']
            or any(carried[key] != guard['carry_forward'][key] for key in
                   ('qualification_id', 'charged_growth_bytes', 'original_ceilings', 'capsule_digest'))):
        _reject('The original sealed carry-forward accounting must remain exact')
    approved = budgets.approved_budget(recovery._json(decision['package_snapshot'])['execution_envelope'], store.directory)
    saved_budget = db.execute('SELECT record FROM runtime.runtime_budget WHERE id=?', (original['budget_id'],)).fetchone()
    if not saved_budget or approved.get('kind') != 'product' or approved['budget_id'] != original['budget_id']:
        _reject('The same durable product budget is required')
    budget = recovery._json(saved_budget[0])
    if (any(approved.get(k) != budget.get(k) for k in ('kind', 'budget_id', 'product_id', 'max_assignments', 'max_seconds', 'max_job_seconds'))
            or budget.get('source') != recipe['source']):
        _reject('Product reservations and limits cannot be reset')
    for scope in ('source:' + recipe['source'], 'product:' + approved['product_id']):
        saved = db.execute('SELECT budget_id FROM runtime.product_budget_scope WHERE scope=?', (scope,)).fetchone()
        if not saved or saved[0] != original['budget_id']:
            _reject('The durable product and source reservation identity changed')
    reservations = [recovery._json(row[0]) for row in db.execute('SELECT record FROM runtime.attempt')
                    if recovery._json(row[0]).get('budget_id') == original['budget_id']]
    count = db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (work,)).fetchone()[0]
    if (type(contract.get('max_assignments')) is not int or contract['max_assignments'] != count + 2
            or contract['max_assignments'] > budget['max_assignments']
            or recipe.get('max_seconds') != contract['max_seconds'] or recipe['max_seconds'] > budget['max_job_seconds']
            or len(reservations) + 2 > budget['max_assignments']
            or any(type(r.get('budget_seconds')) is not int or r['budget_seconds'] <= 0 for r in reservations)
            or sum(r['budget_seconds'] for r in reservations) + 2 * recipe['max_seconds'] > budget['max_seconds']):
        _reject('Retained failed reservations must leave room for the bounded writer and assessor')
    for group, cap in (('output_roots', 'max_output_growth_bytes'), ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
        if type(guard['high_water_growth_bytes'][group]) is not int or not 0 <= guard['high_water_growth_bytes'][group] <= guard['scope'][cap]:
            _reject('A retained byte violation cannot be acknowledged')
    if min(guard['last_observation']['available_bytes'].values(), default=0) < guard['scope']['min_free_bytes']:
        _reject('A retained free-space violation cannot be acknowledged')
    tables = {}
    for schema, names in (('main', ('work', 'concept', 'decision', 'decision_readback', 'challenge', 'operation',
                                   'lifecycle', 'assignment', 'outbox', 'resource', 'completion', 'owned_stop',
                                   'technical_blocker', 'worker_status', recovery.TABLE)),
                          ('runtime', ('attempt', 'runtime_budget', 'product_budget_scope', 'observed_process', 'native_check', 'resource_limit'))):
        for name in names:
            rows = recovery._rows(db, f'SELECT * FROM {schema}.{name} ORDER BY 1')
            tables[schema + '.' + name] = digest(rows)
    snapshot = {'journals': identities, 'tables': tables, 'sidecars': _absent_sidecars(store, work)}
    return {'guard': guard, 'raw_guard': raw_guard[0], 'audit': audit, 'recipe': recipe, 'reference': reference,
            'contract': contract, 'raw_contract': run['contract'], 'packet': packet,
            'decision_id': decision['decision_id'], 'snapshot': snapshot}


def _failed(store, request, fingerprint, error, candidate):
    # The reserved row survives even if this best-effort terminal write cannot.
    outcome = {'accepted': False, 'applied': False, 'status': 'failed', 'operation_id': request['operation_id'],
               'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0,
               'error': {'code': str(getattr(error, 'code', type(error).__name__))[:64], 'message': str(error)[:2000]},
               'observed_resource_limits': resource_limits.receipt(candidate) if candidate is not None else None}
    encoded = json.dumps(outcome, sort_keys=True, separators=(',', ':'), allow_nan=False)
    with recovery._connection(store, write=True) as (db, _):
        db.execute('BEGIN IMMEDIATE')
        verify_schema(db)
        db.execute(f"UPDATE {TABLE} SET state='failed',outcome=? WHERE operation_id=? AND request_fingerprint=? AND state='reserved'",
                   (encoded, request['operation_id'], fingerprint))
        db.commit()
    return outcome


def acknowledge(lifecycle, request, *, apply=False):
    """Preview static eligibility or apply once; never admit or create work."""
    if type(apply) is not bool:
        _reject('Apply must be an explicit boolean')
    request = StartupResourceContinuationRequest.model_validate(request).model_dump()
    for key in ('operation_id', 'work_id', 'recovery_operation_id'):
        identifier(request[key])
    store = lifecycle.store
    if store.recovery_hold() or (apply and store.read_only):
        _reject('A held recovery copy or read-only store cannot apply continuation')
    fingerprint = digest([FORMAT, request])
    with recovery._worker_fence(store) as verify_lock:
        with recovery._connection(store) as (db, identities):
            db.execute('BEGIN')
            if prior := _claim(db, request, fingerprint):
                return prior
            checked = _state(lifecycle, db, request, identities)
        verify_lock()
        if not apply:
            return {'accepted': False, 'ready': True, 'applied': False, 'operation_id': request['operation_id'],
                    'grants_work': False, 'grants_time': False, 'snapshot_digest': digest(checked['snapshot']),
                    'unobserved_interval': checked['packet']['unobserved_interval'], 'limits': LIMITS}
        with recovery._connection(store, write=True) as (db, identities):
            db.execute('BEGIN IMMEDIATE')
            if _state(lifecycle, db, request, identities)['snapshot'] != checked['snapshot']:
                _reject('Authority or retained resource evidence changed before the claim')
            _initialize(db)
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,NULL)',
                       (request['operation_id'], request['work_id'], fingerprint, checked['raw_guard'], checked['raw_contract'], 'reserved'))
            verify_lock()
            db.commit()
        candidate = None
        try:
            lifecycle._fault('continuation_after_claim')
            candidate = copy.deepcopy(checked['guard'])
            candidate.update(breach=None, active_attempt=None,
                             owner_generation=resource_limits._owner_generation(candidate) + 1,
                             continuation={'format': FORMAT, 'reference': checked['reference'],
                                           'decision_id': checked['decision_id'], 'concept_digest': request['digest'],
                                           'historical_breach': checked['guard']['breach']})
            lifecycle._fault('continuation_before_scan')
            candidate, _ = resource_limits._sample_record(candidate, candidate['scope'], request['operation_id'], None, force=True)
            sampled_monotonic = time.monotonic()
            lifecycle._fault('continuation_after_scan')
            resource_limits.enforce(candidate)
            encoded = resource_limits._bounded_record(canonical(candidate))
            outcome = {'accepted': False, 'applied': True, 'status': 'success', 'operation_id': request['operation_id'],
                       'work_id': request['work_id'], 'grants_work': False, 'grants_time': False, 'model_budget_reservations': 0,
                       'work_revision': request['expected_work_revision'] + 1,
                       'original_guard_sha256': request['original_guard_sha256'], 'decision_id': request['decision_id'],
                       'evidence_sha256': request['evidence_sha256'], 'continuation_guard_digest': digest(candidate),
                       'reference': checked['reference'], 'recovery_operation_id': request['recovery_operation_id'],
                       'recovery_audit_digest': digest(checked['audit']),
                       'registered_contract_digest': digest(checked['contract']),
                       'admission_predecessor': 'startup-continuation:' + request['operation_id'],
                       'resource_limits': resource_limits.receipt(candidate), 'limits': LIMITS}
            encoded_outcome = canonical(outcome)
            def fresh():
                age = time.time() - candidate['last_observation']['observed_at']
                elapsed = time.monotonic() - sampled_monotonic
                bound = candidate['scope']['max_observation_gap_seconds']
                if not 0 <= age <= bound or not 0 <= elapsed <= bound:
                    _reject('The real continuation observation is no longer fresh')
            fresh()
            lifecycle._fault('continuation_before_commit')
            with recovery._connection(store, write=True) as (db, identities):
                db.execute('BEGIN IMMEDIATE')
                if _state(lifecycle, db, request, identities)['snapshot'] != checked['snapshot']:
                    _reject('Authority or historical records changed before continuation commit')
                fresh()
                if db.execute('UPDATE runtime.resource_limit SET record=? WHERE work_id=? AND record=?',
                              (encoded, request['work_id'], checked['raw_guard'])).rowcount != 1:
                    _reject('Original resource guard changed before continuation commit')
                if db.execute("UPDATE lifecycle SET contract=?,execution='held',admitted=0 WHERE work_id=? AND contract=? AND admitted=0 AND control='none'",
                              (canonical(checked['contract']), request['work_id'], checked['raw_contract'])).rowcount != 1:
                    _reject('Original contract changed before the atomic continuation')
                db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='startup_failure_recovered'", (request['work_id'],))
                lifecycle._touch(db, request['work_id'], 'startup_resource_continuation',
                                 {'operation_id': request['operation_id'], 'decision_id': request['decision_id'],
                                  'original_guard_sha256': request['original_guard_sha256'],
                                  'evidence_sha256': request['evidence_sha256']})
                if db.execute(f"UPDATE {TABLE} SET state='success',outcome=? WHERE operation_id=? AND request_fingerprint=? AND state='reserved'",
                              (encoded_outcome, request['operation_id'], fingerprint)).rowcount != 1:
                    _reject('The exclusive continuation claim changed')
                lifecycle._fault('continuation_after_updates')
                # Recheck exact protected bytes after the last mutation boundary.
                # These are bounded reads only; no sample or historical repair.
                for reference in ({'path': request['evidence_path'], 'sha256': request['evidence_sha256']},
                                  checked['packet']['failed_guard'], checked['guard']['carry_forward_reference']):
                    recovery._file(store, reference, immutable=reference == checked['packet']['failed_guard'])
                _absent_sidecars(store, request['work_id'])
                verify_lock()
                fresh()
                db.commit()
            return outcome
        except Exception as error:
            try:
                return _failed(store, request, fingerprint, error, candidate)
            except Exception as persistence_error:
                raise Rejected('startup_resource_continuation',
                               'Continuation remains durably reserved and held; failed outcome could not be saved') from persistence_error
