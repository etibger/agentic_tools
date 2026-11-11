"""Explicit, append-only recovery authority for an already-amended held feature.

Preparation performs static profile checks without launching processes. A normal saved decision grants one supplemental
metadata attempt and finite named coordinator slots. Applying that exact packet
never admits work, changes its original clock, refunds charges or resets a journal.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import predispatch_feature_amendment as previous
from . import readiness, instructions
from . import startup_failure_recovery as fencing
from .coordinator import ConceptRequest
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-predispatch-recovery-v1'
TABLE = 'predispatch_recovery'
INTENT = TABLE + '_intent'
SUCCESSOR = TABLE + '_successor_intent'
SCHEMA = {}
for name in (TABLE, INTENT):
    SCHEMA[name] = f'CREATE TABLE {name} (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,record TEXT NOT NULL)'
    for action in ('update', 'delete'):
        SCHEMA[name + '_' + action] = (f'CREATE TRIGGER {name}_{action} BEFORE {action.upper()} ON {name} '
            "BEGIN SELECT RAISE(ABORT,'Supplemental recovery history is immutable'); END")
SUCCESSOR_SCHEMA = {
    SUCCESSOR: f'CREATE TABLE {SUCCESSOR} (operation_id TEXT PRIMARY KEY,work_id TEXT UNIQUE NOT NULL,predecessor_operation_id TEXT UNIQUE NOT NULL,record TEXT NOT NULL)',
}
for action in ('update', 'delete'):
    SUCCESSOR_SCHEMA[SUCCESSOR + '_' + action] = (f'CREATE TRIGGER {SUCCESSOR}_{action} BEFORE {action.upper()} ON {SUCCESSOR} '
        "BEGIN SELECT RAISE(ABORT,'Supplemental recovery successor history is immutable'); END")


class Exact(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Reference(Exact):
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ReceiptReference(Exact):
    operation_id: str
    work_id: str
    receipt_digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')


class SourceChange(Exact):
    before: Reference
    after: Reference


class Allowances(Exact):
    metadata_attempts: int = Field(ge=1, le=1)
    metadata_max_seconds: int = Field(ge=30, le=600)
    runtime_slots: dict[str, int]
    effective_limit_seconds: int = Field(ge=1, le=172800)


class ConfigurationScalar(Exact):
    key: Literal['model', 'model_reasoning_effort']
    before: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    after: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


class ConfigurationRefresh(Exact):
    path: str
    before_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    after_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    replacements: list[ConfigurationScalar] = Field(min_length=1, max_length=2)


class FailedPredecessor(Exact):
    packet: Reference
    qualification: Reference
    engine_directory: str


class PrepareRequest(Exact):
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    decision_id: str
    prior_amendment: ReceiptReference
    prior_engine_directory: str
    original_controller_contract_path: str
    controller_contract_path: str
    controller_resource: str
    source_changes: list[SourceChange] = Field(min_length=1, max_length=128)
    additional_inputs: list[Reference] = Field(max_length=32)
    readiness_directory: str
    destination_directory: str
    concept_update_path: str
    recipe_path: str | None = None
    configuration_refresh: list[ConfigurationRefresh] = Field(default_factory=list, max_length=16)
    predecessor: FailedPredecessor | None = None
    allowances: Allowances


class ApplyRequest(Exact):
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    decision_id: str
    packet_path: str
    packet_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


QualifyRequest = ApplyRequest
VerifyRequest = ReceiptReference


def require(value, message):
    if not value:
        raise Rejected('predispatch_recovery', message)


def _schema(db, create=False, successor=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE name GLOB 'predispatch_recovery*' OR "
        "(tbl_name IN (?,?,?) AND (type='trigger' OR (type='index' AND sql IS NOT NULL)))", (TABLE, INTENT, SUCCESSOR)).fetchall())
    if not found and create:
        for sql in SCHEMA.values():
            db.execute(sql)
        found = dict(SCHEMA)
    require(not found or found in (SCHEMA, {**SCHEMA, **SUCCESSOR_SCHEMA}), 'Supplemental recovery audit schema differs')
    if create and successor and SUCCESSOR not in found:
        for sql in SUCCESSOR_SCHEMA.values():
            db.execute(sql)
        found.update(SUCCESSOR_SCHEMA)
    return bool(found)


def _intent_table(request):
    return SUCCESSOR if request.get('predecessor') is not None else INTENT


def _has_table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone())


def source_manifest(directory):
    """Reconstruct the engine fingerprint from exact preserved ordinary sources."""
    directory = Path(directory)
    require(directory.is_absolute() and directory.resolve() == directory and directory.is_dir() and
        not any(p.is_symlink() for p in (directory, *directory.parents)), 'Preserved engine root must be exact')
    result = {}
    for package in ('aos', 'check_adapters', 'projects'):
        root = directory / package
        require(root.is_dir() and not root.is_symlink(), 'Preserved engine package missing')
        for path in sorted(root.rglob('*.py')):
            _, ref = previous._file(path)
            result[path.relative_to(directory).as_posix()] = ref['sha256']
    require(1 <= len(result) <= 2048, 'Preserved engine manifest is empty or excessive')
    return result


def _historical(store, reference, engine_directory):
    """Verify original applied authority without pretending it is current authority."""
    ReceiptReference.model_validate(reference, strict=True)
    with store._connection() as db:
        require(previous._schema(db), 'Original amendment is absent')
        row = db.execute(f'SELECT record FROM {previous.TABLE} WHERE operation_id=? AND work_id=?',
            (reference['operation_id'], reference['work_id'])).fetchone()
        require(row is not None, 'Original amendment receipt missing')
        receipt = json.loads(row[0])
        require(digest(receipt) == reference['receipt_digest'], 'Original amendment receipt changed')
        operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (reference['operation_id'],)).fetchone()
        require(operation and json.loads(operation[0]) == {'applied': True, 'receipt_digest': digest(receipt),
            'work_id': reference['work_id']}, 'Original amendment operation differs')
        package_row = db.execute('SELECT package,digest FROM concept WHERE work_id=? AND digest=?',
            (reference['work_id'], receipt['to_concept_digest'])).fetchone()
        require(package_row is not None, 'Original amendment concept missing')
        package = json.loads(package_row['package'])
        require(receipt['registered_contract'] == {**package['execution_envelope'],
            'concept_revision': package['revision'], 'concept_digest': receipt['to_concept_digest']},
            'Original registered amendment differs from its saved concept')
    manifest = source_manifest(engine_directory)
    require(digest(manifest) == receipt['engine_digest'], 'Preserved engine does not match historical approval')
    packet, ref = previous._json(receipt['packet']['path'], immutable=True)
    require(ref == receipt['packet'] and digest(packet) == receipt['packet_digest'], 'Original immutable packet changed')
    # The old validator still verifies every old source, recipe, decision and
    # journal. Only the running-engine comparison uses its independently read
    # exact historical source manifest instead of today's recovery engine.
    previous._validate_packet(store, packet, live_accounting=False, historical_engine_manifest=manifest)
    previous._approval(store, receipt['to_decision_id'], reference['work_id'], receipt['to_concept_digest'])
    return receipt, packet


def _substitute(value, changes):
    if isinstance(value, dict):
        return {k: _substitute(v, changes) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, changes) for v in value]
    if isinstance(value, str):
        return changes.get(value, value)
    return value


def _closure(store, path, *, seen=None):
    """Validate a bounded reference-only source closure, including every byte pin."""
    seen = set() if seen is None else seen
    path = Path(path)
    require(str(path) not in seen and len(seen) < 512, 'Cyclic or excessive source closure')
    seen.add(str(path))
    value, _ = previous._json(path)
    require(set(value) == {'format', 'files'} and isinstance(value['format'], str) and
        isinstance(value['files'], list) and 1 <= len(value['files']) <= 128,
        'Integrity source manifests must contain only format and exact file references')
    paths = set()
    for entry in value['files']:
        require(isinstance(entry, dict) and set(entry) in ({'path', 'sha256'}, {'path', 'sha256', 'size'}) and
            entry['path'] not in paths, 'Invalid or duplicate closure source reference')
        paths.add(entry['path']); previous._protected(store, entry['path'])
        raw, ref = previous._file(entry['path'])
        require(ref == {k: entry[k] for k in ('path', 'sha256')} and
            ('size' not in entry or type(entry['size']) is int and entry['size'] == len(raw)), 'Closure source bytes changed')
        if Path(entry['path']).suffix == '.json':
            child, _ = previous._json(entry['path'])
            if set(child) == {'format', 'files'}:
                _closure(store, entry['path'], seen=seen)
    return value


def _controller(store, old, request):
    changes, after = {}, []
    before_refs = old['input_files']
    for change in request['source_changes']:
        a, b = change['before'], change['after']
        require(a in before_refs and a != b and a['path'] != b['path'],
            'Replacement must preserve a distinct original approved source')
        for ref in (a, b):
            previous._protected(store, ref['path'])
            require(previous._file(ref['path'])[1] == ref, 'Source delta bytes changed')
        left = previous._file(a['path'])[0]; right = previous._file(b['path'])[0]
        if left != right and not (Path(a['path']).suffix == Path(b['path']).suffix == '.py'):
            require(Path(a['path']).suffix == Path(b['path']).suffix == '.json', 'Only executable code or integrity manifests may change')
            before_closure = _closure(store, a['path']); after_closure = _closure(store, b['path'])
            require(before_closure['format'] == after_closure['format'], 'Integrity manifest kind changed')
        elif Path(a['path']).suffix == '.json':
            value, _ = previous._json(a['path'])
            if set(value) == {'format', 'files'}:
                _closure(store, a['path']); _closure(store, b['path'])
        for key in ('path', 'sha256'):
            require(a[key] not in changes or changes[a[key]] == b[key], 'Ambiguous source replacement')
            changes[a[key]] = b[key]
        after.append(b)
    require(len({r['path'] for r in after}) == len(after), 'Duplicate replacement path')
    result = _substitute(old, changes)
    # Active references are replaced without editing any historical source.
    # The full prior input closure remains bound in the immutable old packet.
    for ref in request['additional_inputs']:
        previous._protected(store, ref['path'])
        require(previous._file(ref['path'])[1] == ref, 'Additional source input changed')
        same_path = [r for r in result['input_files'] if r['path'] == ref['path']]
        require(not same_path or same_path == [ref], 'Conflicting input at retained path')
        if ref not in result['input_files']:
            result['input_files'].append(ref)
    result['supplemental_recovery'] = {'operation_id': request['operation_id']}
    # No substitution may change accounting, product source or runtime authority.
    protected = ('format', 'work_id', 'state_dir', 'worker_roots', 'control_root', 'repository',
        'ack_root', 'kickoff_sha', 'baseline_inventory', 'stage_order', 'wall_seconds',
        'active_seconds', 'coordinator_operations', 'accounting_origin',
        'coordinator_allowance_amendment', 'coordinator_reservation_plan', 'checkpoint_wait_seconds',
        'assessment_evidence_gate', 'approved_di', 'approved_task_plan', 'native_catalog', 'native_catalog_sha256')
    require(all(result.get(k) == old.get(k) for k in protected), 'Source replacements alter protected authority')
    if 'native_handoff' in old:
        require({k: v for k, v in result['native_handoff'].items() if k != 'module_path'} ==
            {k: v for k, v in old['native_handoff'].items() if k != 'module_path'}, 'Native schema and custody roots cannot change')
    actual, ref = previous._json(request['controller_contract_path'])
    require(actual == result, 'Controller differs beyond exact source replacements and recovery marker')
    previous._inputs(store, result)
    return result, ref


def _allowances(old, allowances, accounting):
    slots = allowances['runtime_slots']
    require(type(slots) is dict and 1 <= len(slots) <= 4 and
        all(isinstance(k, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', k) and
            type(v) is int and 1 <= v <= 10800 for k, v in slots.items()), 'Finite named recovery slots required')
    plan = old['coordinator_reservation_plan']
    require(not set(slots) & set(plan['slots']) and
        not set(slots) & set(accounting['operations']) and not any(k.startswith('qualification-') for k in slots),
        'Recovery cannot reuse an original operation or reserved product slot')
    ceiling = allowances['effective_limit_seconds']
    require(old['active_seconds'] <= ceiling <= old['active_seconds'] + sum(slots.values()),
        'Effective ceiling must be explicit and bounded by the additional reservations')
    planned = plan['planned_lifetime_seconds'] + sum(slots.values())
    require(planned <= ceiling and accounting['clock']['spent'] + sum(slots.values()) <= ceiling,
        'Full no-refund reservation forecast exceeds proposed effective ceiling')
    return {**copy.deepcopy(plan), 'slots': {**plan['slots'], **slots},
        'planned_lifetime_seconds': planned, 'effective_limit_seconds': ceiling,
        'unallocated_seconds': ceiling - planned}


def _configuration_refresh(old, new, proofs):
    """Prove only explicitly overridden model defaults changed, byte for byte.

    Old configuration contents are reconstructed only in memory. Neither whole
    configuration nor any unrelated secret value is retained or returned.
    """
    changed = {a['path']: (a, b) for a, b in zip(old['configs'], new['configs']) if a != b}
    require(len({p['path'] for p in proofs}) == len(proofs) and set(changed) == {p['path'] for p in proofs},
        'Every changed configuration requires one exact scalar reconstruction proof')
    for proof in proofs:
        before, after = changed[proof['path']]
        require((proof['before_sha256'], proof['after_sha256']) == (before['sha256'], after['sha256']),
            'Configuration proof differs from its original and proposed pins')
        raw, ref = previous._file(after['resolved'])
        require(ref['sha256'] == after['sha256'] and len(raw) == after['bytes'], 'Current configuration bytes differ')
        current = tomllib.loads(raw.decode())
        lines = raw.decode().splitlines(keepends=True)
        keys = [r['key'] for r in proof['replacements']]
        require(len(keys) == len(set(keys)), 'Duplicate configuration scalar replacement')
        for replacement in proof['replacements']:
            key = replacement['key']; matched = []
            require(replacement['before'] != replacement['after'] and current.get(key) == replacement['after'],
                'Configuration scalar is not the declared current top-level value')
            for index, line in enumerate(lines):
                if line.lstrip().startswith('['):
                    break
                pattern = r'([ \t]*' + key + r"[ \t]*=[ \t]*)([\"'])([^\"'\r\n]*)([\"'])([ \t]*(?:#[^\r\n]*)?)(\r?\n|$)"
                found = re.fullmatch(pattern, line)
                if found:
                    require(found[2] == found[4] and found[3] == replacement['after'], 'Configuration quote or value is not exact')
                    matched.append(index)
                    lines[index] = found[1] + found[2] + replacement['before'] + found[4] + found[5] + found[6]
            require(len(matched) == 1, 'Only one plain quoted top-level scalar can be reconstructed')
        reconstructed = ''.join(lines).encode()
        require(hashlib.sha256(reconstructed).hexdigest() == before['sha256'] and len(reconstructed) == before['bytes'],
            'Configuration changed beyond the allowed model defaults')
        original = tomllib.loads(reconstructed.decode())
        require({k: v for k, v in original.items() if k not in keys} ==
            {k: v for k, v in current.items() if k not in keys} and
            all(original.get(r['key']) == r['before'] for r in proof['replacements']),
            'Reconstructed configuration changed other settings')


def _recipe(store, original, request):
    recipe = copy.deepcopy(original)
    reference = None
    if request.get('recipe_path') is not None:
        recipe, reference = previous._json(previous._protected(store, request['recipe_path']))
        require(set(recipe) == set(original) and
            all(recipe[k] == original[k] for k in original if k != 'instruction_manifest'),
            'Recovery cannot change recipe execution, source, permissions or acceptance')
        if recipe['instruction_manifest'] != original['instruction_manifest']:
            previous._manifest(original['instruction_manifest'], recipe['instruction_manifest'])
    _configuration_refresh(original['instruction_manifest'], recipe['instruction_manifest'], request.get('configuration_refresh', []))
    actual = instructions.readback(recipe['instruction_manifest'], Path(recipe['source']))
    require(actual['manifest'] == recipe['instruction_manifest'], 'Current recovery instructions differ')
    return recipe, reference


def _concept(m, request):
    old = m['original_contract']
    envelope = {k: copy.deepcopy(v) for k, v in old.items() if k not in ('concept_revision', 'concept_digest')}
    resource = request['controller_resource']
    require(resource in old['resources'] and resource.endswith(digest(m['original_controller'])),
        'Exact original controller resource is not registered')
    updated_resource = resource[:-len(digest(m['original_controller']))] + digest(m['controller_contract'])
    envelope.update(revision=old['revision'] + 1,
        runtime_recipe=m['recipe'], runtime_recipe_digest=digest(m['recipe']),
        resources=[updated_resource if r == resource else r for r in old['resources']],
        prerequisites={k: False for k in old['prerequisites']},
        predispatch_recovery={'operation_id': request['operation_id'], 'metadata_digest': digest(m)})
    allowances = request['allowances']
    if request.get('predecessor') is not None:
        envelope['authority'] += (' Supplemental recovery successor ' + request['operation_id'] +
            ' grants one additional metadata attempt of at most ' + str(allowances['metadata_max_seconds']) +
            ' seconds in its separate namespace. It carries forward unchanged these previously approved but unapplied '
            'coordinator-only reservations (seconds): ' + canonical(allowances['runtime_slots']) +
            ', and the same prospective effective coordinator lifetime ceiling of ' + str(allowances['effective_limit_seconds']) +
            ' seconds. It adds no runtime reservation or lifetime seconds to that grant. The failed metadata attempt remains consumed; '
            'original approval start, deadline, spent charges, product envelope and every historical decision remain unchanged. '
            'No refund, slot reuse, automatic retry, branch or further successor is authorized.')
    else:
        envelope['authority'] += (' Supplemental recovery ' + request['operation_id'] +
            ' prospectively grants one metadata attempt of at most ' + str(allowances['metadata_max_seconds']) +
            ' seconds and these new coordinator-only reservations (seconds): ' + canonical(allowances['runtime_slots']) +
            '. Its effective coordinator lifetime ceiling is ' + str(allowances['effective_limit_seconds']) +
            ' seconds, superseding only the prior coordinator ceiling. Original approval start, deadline, spent charges, '
            'product envelope and every historical decision and attempt remain unchanged; no refund, slot reuse or automatic retry.')
    update = m['concept_update']
    require(set(update) == {'body', 'decision_context', 'recommendation', 'alternative', 'consequence'},
        'Only the technical decision brief may change')
    # The exact source delta and allowance are inside the approved envelope, not
    # merely a mutable file referenced by prose.
    envelope['predispatch_recovery']['allowances'] = copy.deepcopy(request['allowances'])
    concept = {k: copy.deepcopy(v) for k, v in m['original_package'].items() if k not in ('format', 'execution_envelope')}
    concept.update(update)
    concept.update(operation_id=request['operation_id'] + '-present', expected_work_revision=request['expected_work_revision'],
        revision=m['original_package']['revision'] + 1, execution_envelope=envelope, authority=envelope['authority'])
    return concept


def _snapshot_unchanged(m):
    require(previous._json(m['accounting']['clock_reference']['path'])[1] == m['accounting']['clock_reference'],
        'Accounting changed after recovery preparation')
    root = Path(m['accounting_origin']['clock_path']).parent
    current = {str(p) for p in root.iterdir() if p.name.startswith(('operation-', 'completed-'))}
    refs = [ref for operation in m['accounting']['operations'].values() for ref in (operation['start'], operation['completion']) if ref]
    require(current == {r['path'] for r in refs}, 'Operation history changed after recovery preparation')
    for ref in refs:
        require(previous._file(ref['path'])[1] == ref, 'Historical operation receipt changed')


def _metadata_preflight(store, request, recipe, contract):
    return readiness.preflight_metadata(recipe, state_dir=store.directory,
        readiness_dir=Path(request['destination_directory']) / 'readiness',
        model=contract['model'], effort=contract['effort'],
        max_seconds=request['allowances']['metadata_max_seconds'], max_attempts=1, native_checks=[])


def _validate_packet(store, packet, *, snapshot=True, historical_runtime_identity=None):
    require(packet.get('format') == FORMAT, 'Unsupported supplemental recovery packet')
    request = packet['request']; PrepareRequest.model_validate(request, strict=True)
    m = packet['metadata']
    require(m['state_dir'] == str(store.directory) and
        m['worker_roots'] == list(map(str, store.worker_writable_roots)), 'Recovery ledger or worker scope differs')
    prior, prior_packet = _historical(store, request['prior_amendment'], request['prior_engine_directory'])
    require(prior == m['original_amendment_receipt'] and
        prior_packet['metadata']['controller_contract'] == m['original_controller'], 'Original recovery basis changed')
    require((readiness._runtime_identity() if historical_runtime_identity is None else historical_runtime_identity) == m['runtime_identity'],
        'Exact reviewed recovery engine changed')
    if request.get('predecessor') is not None:
        require(_failed_predecessor(store, request, snapshot=snapshot) == m['failed_predecessor'], 'Failed predecessor lineage changed')
    new, new_ref = _controller(store, m['original_controller'], request)
    require(new == m['controller_contract'] and new_ref == m['controller_reference'], 'Recovery source template changed')
    require(previous._json(request['original_controller_contract_path'])[0] == m['original_controller'] and
        previous._json(request['concept_update_path']) == (m['concept_update'], m['concept_update_reference']),
        'Recovery decision or original controller changed')
    require(_allowances(m['original_controller'], request['allowances'], m['accounting']) == m['coordinator_reservation_plan'],
        'Recovery allowances changed')
    require(packet['concept'] == _concept(m, request), 'Recovery concept exceeds its exact allowed delta')
    require(previous._json(m['readiness_journal']['path']) == (m['readiness_before'], m['readiness_journal']),
        'Original readiness history changed')
    recipe, recipe_ref = _recipe(store, m['original_contract']['runtime_recipe'], request)
    require(recipe == m['recipe'] and recipe_ref == m['recipe_reference'], 'Exact recovery recipe changed')
    if historical_runtime_identity is None:
        require(m.get('metadata_preflight') == _metadata_preflight(store, request, recipe, m['original_contract']),
            'Exact metadata profile preflight changed')
    require(time_valid(m['accounting_origin']['deadline']), 'Original feature deadline expired')
    if snapshot:
        require(previous._baseline(m['recipe'], m['original_controller']) == m['source_candidate'], 'Product source changed')
        _snapshot_unchanged(m)


def time_valid(deadline):
    import time
    return type(deadline) in (int, float) and math.isfinite(deadline) and time.time() < deadline


def _failed_predecessor(store, request, *, snapshot=True):
    """One successor to a positively settled, pre-role failure; never a reset."""
    predecessor = request['predecessor']
    require(Path(predecessor['packet']['path']).is_file() and Path(predecessor['qualification']['path']).is_file(),
        'Interrupted or missing predecessor qualification cannot acquire a successor')
    packet, packet_ref = previous._json(previous._protected(store, predecessor['packet']['path']), immutable=True)
    qualification, qualified_ref = previous._json(previous._protected(store, predecessor['qualification']['path']), immutable=True)
    require(packet_ref == predecessor['packet'] and qualified_ref == predecessor['qualification'] and
        packet['request'].get('predecessor') is None, 'Only the exact original recovery may have one successor')
    old_request = packet['request']; qualified_request = qualification['request']
    ApplyRequest.model_validate(qualified_request, strict=True)
    require(old_request['work_id'] == request['work_id'] and old_request['operation_id'] != request['operation_id'] and
        qualified_request['operation_id'] == old_request['operation_id'] and qualified_request['work_id'] == request['work_id'] and
        qualified_request['packet_path'] == packet_ref['path'] and qualified_request['packet_sha256'] == packet_ref['sha256'] and
        qualified_request['digest'] == digest(previous._package(packet['concept'])) and
        all(qualified_request[k] == request[k] for k in ('decision_id', 'digest', 'expected_work_revision')),
        'Successor must bind the exact failed current decision and work revision')
    destination = Path(request['destination_directory']); original_destination = Path(old_request['destination_directory'])
    require(not destination.is_relative_to(original_destination) and not original_destination.is_relative_to(destination) and
        qualified_ref['path'] == str(original_destination / 'qualification-result.json'), 'Successor must use a separate output namespace')
    require(request['prior_amendment'] == old_request['prior_amendment'] and
        request['controller_resource'] == old_request['controller_resource'] and
        request['readiness_directory'] == old_request['readiness_directory'] and
        all(request['allowances'][k] == old_request['allowances'][k] for k in ('runtime_slots', 'effective_limit_seconds')),
        'Successor must carry forward the same unapplied runtime grant and original accounting basis')
    with store._connection() as db:
        require(_schema(db), 'Original recovery intent absent')
        intent = db.execute(f'SELECT record FROM {INTENT} WHERE operation_id=? AND work_id=?',
            (old_request['operation_id'], request['work_id'])).fetchone()
        require(intent and json.loads(intent[0]) == {'packet': packet_ref}, 'Original recovery intent differs')
        require(not db.execute(f'SELECT 1 FROM {TABLE} WHERE operation_id=?', (old_request['operation_id'],)).fetchone(),
            'An applied recovery cannot acquire a failed-metadata successor')
    decision = previous._approval(store, request['decision_id'], request['work_id'], request['digest'])
    require(json.loads(decision['package_snapshot']) == previous._package(packet['concept']), 'Failed current proposal differs from saved approval')
    identity = packet['metadata']['runtime_identity']
    require(digest(source_manifest(predecessor['engine_directory'])) == identity['runtime_source_digest'] and
        {k: v for k, v in readiness._runtime_identity().items() if k != 'runtime_source_digest'} ==
        {k: v for k, v in identity.items() if k != 'runtime_source_digest'}, 'Exact failed recovery engine or runtime provenance differs')
    _validate_packet(store, packet, snapshot=snapshot, historical_runtime_identity=identity)
    _qualification_result(store, packet, packet_ref, qualified_request, qualification,
        failed=True, historical_runtime_identity=identity)
    return {'packet': packet_ref, 'qualification': qualified_ref, 'operation_id': old_request['operation_id'],
        'decision_id': request['decision_id'], 'concept_digest': request['digest'],
        'engine_directory': predecessor['engine_directory'], 'engine_digest': identity['runtime_source_digest'],
        'reservation': qualification['reservation'], 'journal': qualification['journal'], 'receipt': qualification['receipt']}


def prepare(lifecycle, request):
    PrepareRequest.model_validate(request, strict=True)
    identifier(request['operation_id']); identifier(request['work_id'])
    store = lifecycle.store; work = request['work_id']
    destination = previous._protected(store, request['destination_directory'], directory=True)
    packet_path = destination / 'packet.json'
    intent_table = _intent_table(request)
    with store._connection() as db:
        if _schema(db) and _has_table(db, intent_table):
            row = db.execute(f'SELECT record FROM {intent_table} WHERE work_id=?', (work,)).fetchone()
            if row:
                intent = json.loads(row[0]); packet, ref = previous._json(intent['packet']['path'], immutable=True)
                require(packet['request'] == request and ref == intent['packet'], 'Another recovery already owns this work')
                _validate_packet(store, packet)
                return {'packet_path': ref['path'], 'packet_sha256': ref['sha256'], 'packet': packet}
    prior, old_packet = _historical(store, request['prior_amendment'], request['prior_engine_directory'])
    predecessor = _failed_predecessor(store, request) if request.get('predecessor') is not None else None
    require(prior['work_id'] == work and (predecessor is not None or
        prior['to_decision_id'] == request['decision_id'] and prior['to_concept_digest'] == request['digest']),
        'Recovery must bind the exact current decision and original registered amendment')
    decision = previous._approval(store, request['decision_id'], work, request['digest'])
    accounting = previous.current_accounting(store, prior, settled=True)
    old, _ = previous._json(request['original_controller_contract_path'])
    require(old == old_packet['metadata']['controller_contract'], 'Current controller is not the approved amendment controller')
    new, new_ref = _controller(store, old, request)
    plan = _allowances(old, request['allowances'], accounting)
    directory = previous._protected(store, request['readiness_directory'], directory=True)
    require(str(directory / 'control/readiness.json') == old_packet['metadata']['readiness_journal']['path'],
        'Recovery must retain the original readiness journal')
    journal, journal_ref = previous._journal(directory, old_packet['metadata']['readiness_receipt'])
    update, update_ref = previous._json(previous._protected(store, request['concept_update_path']))
    with fencing._worker_fence(store) as fence, previous._accounting_lock(store, prior['accounting_origin']['clock_path']) as clock_fence:
        with fencing._connection(store, write=True) as (db, _):
            db.execute('BEGIN IMMEDIATE')
            lifecycle._command_guard(db, work, request['expected_work_revision'], request['digest'])
            contract = previous._passive(lifecycle, db, work); previous._empty_product(db, contract)
            package = json.loads(decision['package_snapshot'])
            ceiling = db.execute('SELECT coalesce(max(event_id),0) FROM lifecycle_event WHERE work_id=?', (work,)).fetchone()[0]
            base_package = previous._package(old_packet['concept'])
            require(contract == previous._effective_contract(db, base_package, prior['to_concept_digest'], floor=prior['prerequisite_event_floor']),
                'Prior registered authority differs')
            if _schema(db):
                require(not db.execute(f'SELECT 1 FROM {TABLE} WHERE work_id=?', (work,)).fetchone(), 'A recovery was already applied')
            recipe, recipe_ref = _recipe(store, contract['runtime_recipe'], request)
            m = {'work_id': work, 'operation_id': request['operation_id'], 'state_dir': str(store.directory),
                'worker_roots': list(map(str, store.worker_writable_roots)), 'original_contract': contract,
                'original_package': package, 'original_controller': old, 'controller_contract': new,
                'controller_reference': new_ref, 'original_amendment_receipt': prior,
                'accounting_origin': prior['accounting_origin'], 'accounting': accounting,
                'original_budget': previous._budget_snapshot(db), 'original_event_ceiling': ceiling,
                'coordinator_reservation_plan': plan, 'concept_update': update, 'concept_update_reference': update_ref,
                'readiness_before': journal, 'readiness_journal': journal_ref,
                'source_candidate': previous._baseline(recipe, old), 'recipe': recipe, 'recipe_reference': recipe_ref,
                'runtime_identity': readiness._runtime_identity(),
                'metadata_preflight': _metadata_preflight(store, request, recipe, contract)}
            if predecessor is not None:
                m['failed_predecessor'] = predecessor
            packet = {'format': FORMAT, 'request': request, 'metadata': m, 'concept': _concept(m, request)}
            ConceptRequest.model_validate(packet['concept'], strict=True)
            _validate_packet(store, packet)
            _schema(db, create=True, successor=predecessor is not None)
            require(not db.execute(f'SELECT 1 FROM {intent_table} WHERE work_id=?', (work,)).fetchone(), 'Concurrent recovery owns this work')
            ref = previous._retain(packet_path, packet)
            if predecessor is not None:
                db.execute(f'INSERT INTO {SUCCESSOR} VALUES(?,?,?,?)',
                    (request['operation_id'], work, predecessor['operation_id'], canonical({'packet': ref})))
            else:
                db.execute(f'INSERT INTO {INTENT} VALUES(?,?,?)', (request['operation_id'], work, canonical({'packet': ref})))
            fence(); clock_fence(); db.commit()
    return {'packet_path': ref['path'], 'packet_sha256': ref['sha256'], 'packet': packet}


def _load(store, request):
    ApplyRequest.model_validate(request, strict=True)
    packet, ref = previous._json(previous._protected(store, request['packet_path']), immutable=True)
    require(ref['sha256'] == request['packet_sha256'] and packet['request']['operation_id'] == request['operation_id'] and
        packet['request']['work_id'] == request['work_id'] and digest(previous._package(packet['concept'])) == request['digest'],
        'Recovery request does not identify its exact approved packet')
    with store._connection() as db:
        require(_schema(db), 'Recovery intent is absent')
        table = _intent_table(packet['request'])
        require(_has_table(db, table), 'Recovery intent lineage is absent')
        intent = db.execute(f'SELECT record FROM {table} WHERE operation_id=? AND work_id=?',
            (request['operation_id'], request['work_id'])).fetchone()
        require(intent and json.loads(intent[0]) == {'packet': ref}, 'Recovery intent differs')
    decision = previous._approval(store, request['decision_id'], request['work_id'], request['digest'])
    return packet, ref, decision


def _guard(lifecycle, db, request, packet, decision):
    lifecycle._command_guard(db, request['work_id'], request['expected_work_revision'], request['digest'])
    m = packet['metadata']
    require(previous._passive(lifecycle, db, request['work_id']) == m['original_contract'] and
        previous._budget_snapshot(db) == m['original_budget'], 'Registered authority or product accounting changed')
    previous._empty_product(db, m['original_contract'])
    package = previous._package(packet['concept'])
    require(json.loads(decision['package_snapshot']) == package, 'Saved decision approves a different recovery')


def qualify(lifecycle, request):
    store = lifecycle.store
    packet, ref, decision = _load(store, request)
    m = packet['metadata']; destination = Path(packet['request']['destination_directory'])
    with fencing._worker_fence(store) as fence, previous._accounting_lock(store, m['accounting_origin']['clock_path']) as clock_fence:
        _validate_packet(store, packet)
        with fencing._connection(store) as (db, _):
            _guard(lifecycle, db, request, packet, decision)
        reservation = {'format': FORMAT, 'packet': ref, 'request': request, 'decision_id': decision['decision_id'],
            'metadata_attempts': 1, 'max_seconds': packet['request']['allowances']['metadata_max_seconds'],
            'original_readiness_journal': m['readiness_journal']}
        reserved = destination / 'qualification-reservation.json'; completed = destination / 'qualification-result.json'
        if reserved.exists():
            require(previous._json(reserved, immutable=True)[0] == reservation, 'Consumed recovery metadata reservation differs')
            require(completed.exists(), 'Supplemental metadata attempt interrupted; allowance remains consumed')
            result = previous._json(completed, immutable=True)[0]
            _qualification_result(store, packet, ref, request, result)
            return result
        require(not completed.exists(), 'Unowned qualification result')
        previous._retain(reserved, reservation)
        # The original journal is never copied, rewritten or reset. This child
        # journal has its own one-attempt allowance, bound above to saved approval.
        directory = destination / 'readiness'
        require(not directory.exists(), 'Unowned supplemental metadata directory')
        directory.mkdir(mode=0o700)
        recipe = m['recipe']
        result = readiness.run_readiness(recipe, state_dir=store.directory, readiness_dir=directory,
            model=m['original_contract']['model'], effort=m['original_contract']['effort'],
            max_seconds=reservation['max_seconds'], max_attempts=1, native_checks=[])
        journal, journal_ref = previous._json(directory / 'control/readiness.json')
        require(len(journal.get('runs', [])) == 1, 'Supplemental metadata exceeded its one attempt')
        receipt, receipt_ref = previous._json(journal['runs'][0]['receipt'])
        require(receipt == result and receipt_ref['sha256'] == journal['runs'][0]['sha256'], 'Metadata result differs from journal')
        value = {'format': FORMAT, 'packet': ref, 'request': request,
            'reservation': previous._file(reserved)[1], 'journal': journal_ref, 'receipt': receipt_ref,
            'result': result, 'completed_at': now()}
        previous._retain(completed, value)
        fence(); clock_fence()
        _qualification_result(store, packet, ref, request, value)
        with fencing._connection(store) as (db, _):
            _guard(lifecycle, db, request, packet, decision)
        _validate_packet(store, packet)
        return value


def _qualification_result(store, packet, packet_ref, request, value, *, failed=False, historical_runtime_identity=None):
    destination = Path(packet['request']['destination_directory'])
    expected_reservation = {'format': FORMAT, 'packet': packet_ref, 'request': request,
        'decision_id': request['decision_id'], 'metadata_attempts': 1,
        'max_seconds': packet['request']['allowances']['metadata_max_seconds'],
        'original_readiness_journal': packet['metadata']['readiness_journal']}
    require(previous._json(destination / 'qualification-reservation.json', immutable=True)[0] == expected_reservation,
        'Qualification reservation does not match saved approval')
    require(value['packet'] == packet_ref and value['request'] == request and
        previous._file(value['reservation']['path'], immutable=True)[1] == value['reservation'] and
        value['reservation']['path'] == str(destination / 'qualification-reservation.json'), 'Qualification authority differs')
    journal, journal_ref = previous._journal(destination / 'readiness', value['receipt'])
    require(journal_ref == value['journal'] and len(journal['runs']) == 1 and journal['max_attempts'] == 1 and
        journal['max_seconds'] == packet['request']['allowances']['metadata_max_seconds'] and
        journal['state_dir'] == str(store.directory) and journal['source'] == packet['metadata']['recipe']['source'],
        'Supplemental readiness allowance or journal differs')
    result, result_ref = previous._json(value['receipt']['path'])
    require(result_ref == value['receipt'] and result == value['result'] and journal['runs'][0]['receipt'] == result_ref['path'] and
        journal['runs'][0]['sha256'] == result_ref['sha256'] and
        result_ref['path'] == str(destination / 'readiness/control/readiness-1.json') and
        result['resource_output'] == str(destination / 'readiness/outputs/readiness-1') and
        result['limits']['max_attempts'] == 1 and result['limits']['max_seconds'] == journal['max_seconds'],
        'Supplemental readiness receipt identity differs')
    if failed:
        identity = historical_runtime_identity or readiness._runtime_identity()
        require(result.get('status') == 'failed' and result.get('ready') is False and result.get('simulated') is False and
            result.get('roles') == [] and result.get('native') is None and result.get('phase') is None and
            result.get('cleanup_verified') is True and result.get('requested_native_checks') == [] and
            result.get('readiness_scope') == 'model-metadata-only' and
            all(type(result.get(k)) is int and result[k] == 0 for k in ('model_turns', 'product_attempts', 'product_reservations')) and
            result.get('prepared_recipe') == packet['metadata']['recipe'] and result.get('runtime_identity') == identity and
            isinstance(result.get('blockers'), list) and bool(result['blockers']),
            'Only a positively settled pre-role metadata failure can have a successor')
    else:
        previous._ready_result(result, packet['metadata']['recipe'])
    readiness._verify_cached_cleanup(result, destination / 'readiness/runtime')


def apply(lifecycle, request):
    store = lifecycle.store; packet, ref, decision = _load(store, request)
    m = packet['metadata']; work = request['work_id']
    with store._connection() as db:
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=?', (request['operation_id'],)).fetchone()
        if row:
            receipt = json.loads(row[0]); require(receipt['apply_request'] == request, 'Applied recovery replay differs')
            return verify(store, {'operation_id': request['operation_id'], 'work_id': work, 'receipt_digest': digest(receipt)})
    with fencing._worker_fence(store) as fence, previous._accounting_lock(store, m['accounting_origin']['clock_path']) as clock_fence:
        with fencing._connection(store, write=True) as (db, _):
            db.execute('BEGIN IMMEDIATE'); _guard(lifecycle, db, request, packet, decision)
            _validate_packet(store, packet)
            qualification_path = Path(packet['request']['destination_directory']) / 'qualification-result.json'
            require(qualification_path.is_file(), 'Approved supplemental metadata has no completed result')
            qualification, qualification_ref = previous._json(qualification_path, immutable=True)
            _qualification_result(store, packet, ref, request, qualification)
            package = previous._package(packet['concept'])
            expected = {**package['execution_envelope'], 'concept_revision': package['revision'], 'concept_digest': request['digest']}
            targets = ('lifecycle', 'work', 'lifecycle_event', 'operation')
            require(not db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN (" + ','.join('?' for _ in targets) + ')', targets).fetchone(), 'Unsupported mutation triggers')
            db.execute("UPDATE lifecycle SET contract=? WHERE work_id=? AND contract=? AND admitted=0 AND control='none'",
                (canonical(expected), work, canonical(m['original_contract'])))
            require(db.execute('SELECT changes()').fetchone()[0] == 1, 'Recovery contract compare-and-swap failed')
            lifecycle._touch(db, work, 'predispatch_recovered', {'operation_id': request['operation_id'], 'packet_digest': digest(packet)})
            floor = db.execute('SELECT max(event_id) FROM lifecycle_event WHERE work_id=?', (work,)).fetchone()[0]
            old = m['original_amendment_receipt']
            receipt = {'format': FORMAT, 'operation_id': request['operation_id'], 'work_id': work,
                'state_dir': str(store.directory), 'apply_request': request, 'packet': ref, 'packet_digest': digest(packet),
                'qualification': qualification_ref, 'from_decision_id': old['to_decision_id'],
                'to_decision_id': decision['decision_id'], 'from_concept_digest': m['original_contract']['concept_digest'],
                'to_concept_digest': request['digest'], 'from_recipe_digest': m['original_contract']['runtime_recipe_digest'],
                'to_recipe_digest': expected['runtime_recipe_digest'], 'engine_digest': m['runtime_identity']['runtime_source_digest'],
                'accounting_origin': m['accounting_origin'], 'original_clock': old['original_clock'],
                'recovery_clock': m['accounting']['clock'], 'original_amendment_receipt': old,
                'accounting_snapshot': m['accounting'], 'runtime_slots': packet['request']['allowances']['runtime_slots'],
                'effective_limit_seconds': packet['request']['allowances']['effective_limit_seconds'],
                'coordinator_reservation_plan': m['coordinator_reservation_plan'],
                'registered_contract': expected, 'prerequisite_event_floor': floor, 'created_at': now()}
            if 'failed_predecessor' in m:
                receipt['failed_predecessor'] = m['failed_predecessor']
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)', (request['operation_id'], work, canonical(receipt)))
            store._record(db, request['operation_id'], ['predispatch_recovery', request],
                {'applied': True, 'receipt_digest': digest(receipt), 'work_id': work})
            fence(); clock_fence(); db.commit()
    return receipt


def verify(store, reference):
    VerifyRequest.model_validate(reference, strict=True)
    with store._connection() as db:
        require(_schema(db), 'Supplemental recovery authority absent')
        row = db.execute(f'SELECT record FROM {TABLE} WHERE operation_id=? AND work_id=?',
            (reference['operation_id'], reference['work_id'])).fetchone()
        require(row is not None, 'Supplemental recovery receipt absent')
        receipt = json.loads(row[0])
        require(digest(receipt) == reference['receipt_digest'], 'Supplemental receipt digest differs')
        operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (reference['operation_id'],)).fetchone()
        require(operation and json.loads(operation[0]) == {'applied': True, 'receipt_digest': digest(receipt),
            'work_id': reference['work_id']}, 'Supplemental operation receipt differs')
        work = store._work(db, reference['work_id'])
        row = db.execute('SELECT package,digest FROM concept WHERE work_id=? AND revision=?',
            (reference['work_id'], work['current_concept'])).fetchone()
        require(row and row['digest'] == receipt['to_concept_digest'], 'Supplemental decision is no longer current')
        package = json.loads(row['package'])
        contract = json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (reference['work_id'],)).fetchone()[0])
        require(contract == previous._effective_contract(db, package, receipt['to_concept_digest'], floor=receipt['prerequisite_event_floor']),
            'Supplemental registered contract differs')
    packet, ref = previous._json(receipt['packet']['path'], immutable=True)
    require(ref == receipt['packet'] and digest(packet) == receipt['packet_digest'], 'Supplemental preparation changed')
    _validate_packet(store, packet, snapshot=False)
    previous._approval(store, receipt['to_decision_id'], receipt['work_id'], receipt['to_concept_digest'])
    qualification, qualification_ref = previous._json(receipt['qualification']['path'], immutable=True)
    require(qualification_ref == receipt['qualification'], 'Qualification result changed')
    _qualification_result(store, packet, ref, receipt['apply_request'], qualification)
    current_accounting(store, receipt)
    return receipt


def current_accounting(store, receipt, *, settled=False, at=None):
    """Acquire the existing accounting fence before the read-only reconciliation."""
    with previous._accounting_lock(store, receipt['accounting_origin']['clock_path'], shared=True):
        return _current_accounting_locked(store, receipt, settled=settled, at=at)


def _current_accounting_locked(store, receipt, *, settled=False, at=None):
    """Reconcile preserved historical authority and prospective recovery charges."""
    import time
    at = time.time() if at is None else at
    require(type(at) in (int, float) and math.isfinite(at), 'Invalid accounting observation time')
    snapshot = receipt['accounting_snapshot']; before = snapshot['clock']; origin = receipt['accounting_origin']
    clock, clock_ref = previous._json(origin['clock_path'])
    require(set(clock) == set(before) and all(clock[k] == before[k] for k in ('identity', 'approved_at', 'deadline', 'active_limit')) and
        clock['held'] is False and type(clock['spent']) is int and before['spent'] <= clock['spent'] <= receipt['effective_limit_seconds'] and
        type(clock['last']) in (int, float) and math.isfinite(clock['last']) and before['last'] <= clock['last'] <= at < clock['deadline'],
        'Original accounting identity, deadline or monotonic spending differs')
    root = Path(origin['clock_path']).parent
    require(not any(root.glob('clock.json.next*')), 'Uncertain partial clock publication')
    operations = copy.deepcopy(snapshot['operations']); refs = {}; used = set(); timeline = []; total = 0
    for name, operation in operations.items():
        for key in ('start', 'completion'):
            ref = operation[key]; require(ref and previous._file(ref['path'])[1] == ref, 'Historical operation changed')
            refs[Path(ref['path']).name] = ref
        start = operation['reservation']; end = operation['result']['completed_at']
        used.add(start.get('slot', start['operation'])); total += start['seconds']; timeline.append((start['started_at'], end))
    paths = sorted(p for p in root.iterdir() if p.name.startswith(('operation-', 'completed-')))
    require(len(paths) <= 256 and all(p.suffix == '.json' for p in paths), 'Partial or excessive operation history')
    new = {}
    for path in paths:
        if path.name not in refs:
            new[path.name], refs[path.name] = previous._json(path, immutable=True)
    opened = []; paired = set(); limits = receipt['coordinator_reservation_plan']['slots']
    for name, start in new.items():
        if not name.startswith('operation-'):
            continue
        slot = start.get('slot'); operation = start.get('operation'); context = start.get('context')
        require(set(start) == {'identity', 'operation', 'seconds', 'started_at', 'slot', 'context', 'authority_receipt_digest'} and
            isinstance(operation, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', operation) and name == 'operation-' + operation + '.json' and
            start['identity'] == origin['clock_identity'] and start['authority_receipt_digest'] == digest(receipt) and
            slot in limits and slot not in used and type(start['seconds']) is int and 1 <= start['seconds'] <= limits[slot] and
            type(start['started_at']) in (int, float) and math.isfinite(start['started_at']) and
            datetime.fromisoformat(receipt['created_at']).timestamp() <= start['started_at'], 'New charge exceeds supplemental authority')
        used.add(slot)
        base = {'work_id': receipt['work_id'], 'concept_digest': receipt['to_concept_digest'], 'recipe_digest': receipt['to_recipe_digest'], 'slot': slot}
        require(isinstance(context, dict) and all(context.get(k) == v for k, v in base.items()), 'Foreign operation context')
        product_slot = slot in ('stage1-storage', 'stage1-runtime', 'stage2-import', 'repair') or slot.startswith('qualification-')
        if not product_slot:
            require(context == {**base, 'kind': 'coordinator'} and operation == slot, 'Coordinator context differs')
        else:
            require(slot not in receipt['runtime_slots'], 'Supplemental runtime slots are coordinator-only')
            fields = set(base) | {'kind', 'assignment_id', 'attempt_id', 'request_digest', 'ordinal', 'increment', 'requested_at', 'assignment_deadline', 'feature_deadline'}
            require(set(context) == fields and context['kind'] in ('checkpoint', 'qualification') and
                isinstance(context['request_digest'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', context['request_digest']) and
                type(context['ordinal']) is int and all(type(context[k]) in (int, float) and math.isfinite(context[k]) for k in ('requested_at','assignment_deadline','feature_deadline')) and
                context['feature_deadline'] == origin['deadline'] and clock['approved_at'] <= context['requested_at'] <= start['started_at'] < min(context['assignment_deadline'], origin['deadline']), 'Invalid checkpoint context')
            with store._connection() as db:
                assignment = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND attempt_id=? AND work_id=?',
                    (context['assignment_id'], context['attempt_id'], receipt['work_id'])).fetchone()
            require(assignment and assignment['kind'] in ('implementation', 'repair') and assignment['sent_at'] and assignment['concept_digest'] == receipt['to_concept_digest'] and
                datetime.fromisoformat(assignment['sent_at']).timestamp() + receipt['registered_contract']['max_seconds'] == context['assignment_deadline'], 'Checkpoint lacks actual writer authority')
            if context['kind'] == 'qualification':
                require(slot == 'qualification-' + str(context['ordinal']) and context['increment'] == 'qualification' and operation == 'first-writer-' + slot, 'Qualification slot differs')
            else:
                require(slot == context['increment'] and slot in ('stage1-storage','stage1-runtime','stage2-import','repair') and
                    context['ordinal'] == ['stage1-storage','stage1-runtime','stage2-import','repair'].index(slot) + 1 and
                    operation == f"checkpoint-{context['ordinal']:04d}", 'Checkpoint slot differs')
        done_name = 'completed-' + operation + '.json'; done = new.get(done_name)
        if done is None:
            opened.append(start); end = clock['last']
        else:
            require(set(done) == {'start_digest','completed_at'} and done['start_digest'] == digest(start) and
                type(done['completed_at']) in (int,float) and math.isfinite(done['completed_at']), 'Completion differs')
            paired.add(done_name); end = done['completed_at']
        require(clock['approved_at'] <= start['started_at'] <= end <= clock['last'] and
            end <= start['started_at'] + start['seconds'] <= clock['deadline'], 'Operation exceeded original reservation')
        if context['kind'] != 'coordinator':
            require(end < min(context['requested_at'] + limits[slot], context['assignment_deadline'], origin['deadline']), 'Checkpoint window exceeded')
        total += start['seconds']; timeline.append((start['started_at'],end))
        operations[operation] = {'start': refs[name], 'completion': refs.get(done_name), 'reservation': start, 'result': done}
    require(paired == {k for k in new if k.startswith('completed-')} and total == clock['spent'], 'All charges must reconcile without refunds')
    require(len(opened) <= 1 and ((not opened and clock['open'] is None) or len(opened) == 1 and
        clock['open'] == {'id': opened[0]['operation'], 'reserved': opened[0]['seconds'], 'started_at': opened[0]['started_at']} and
        at <= opened[0]['started_at'] + opened[0]['seconds']), 'Unknown or overdue operation owner')
    ordered = sorted(timeline); require(all(a[1] <= b[0] for a,b in zip(ordered,ordered[1:])), 'Overlapping operations')
    require(not settled or clock['open'] is None, 'Settled accounting required')
    return {'clock': clock, 'spent': clock['spent'], 'used_slots': sorted(used), 'receipt_count': len(refs),
        'clock_reference': clock_ref, 'operations': operations, 'receipt_digest': digest(refs)}
