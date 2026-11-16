"""Prepare one existing AOS concept from maintained project configuration.

Preparation owns no product lifecycle or approval. Readiness facts and retained
allowance are checked before the exact, unchanged human request is presented.
"""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_tools import check_adapters
from . import budgets, checkout_boundary, instructions, native_network, native_paths, native_processes
from .checks import source_digest, validate_descriptors
from .coordinator import ConceptRequest, ExecutionEnvelope, IntroductionRequest
from .readiness import run_readiness
from .lifecycle import validate_contract
from .store import Rejected, concept_package, digest, identifier, validate_introduction

FORMAT = 'aos-prepared-project-v1'
_RECIPE_KEYS = {'execution_policy', 'source', 'outputs', 'read_roots', 'checks', 'max_seconds',
                'requires_detached_processes', 'implementation_prompt', 'assessment_prompt',
                'instruction_manifest', 'checkout_policy', 'probe_interpreter',
                'native_write_roots', 'native_protected_paths', 'native_process_ownership',
                'source_review_evidence'}
_ENVELOPE_KEYS = set(ExecutionEnvelope.model_fields) | {
    'execution_policy', 'runtime_recipe', 'runtime_recipe_digest', 'check_adapters',
    'check_kinds', 'adapter_architecture', 'product_budget', 'change_roots'}
_BUDGET_KEYS = {'budget_id', 'product_id', 'ledger_directory', 'max_assignments', 'max_seconds', 'max_job_seconds'}


class ReadinessConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_seconds: int = Field(ge=30, le=3600)
    max_attempts: int = Field(ge=1, le=10)
    checks: list[dict]
    prerequisite: str = 'host_readiness'


class DecisionServiceConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    certificate: str
    key: str
    port: int = Field(ge=1, le=65535)
    qualification_reference: str = Field(min_length=1)


class ProjectConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    format: Literal['aos-project-v1']
    state_dir: str
    readiness_dir: str
    worker_writable_roots: list[str] = Field(min_length=1, max_length=16)
    recipe: dict
    readiness: ReadinessConfiguration
    decision_service: DecisionServiceConfiguration
    project_doc_max_bytes: int | None = Field(default=None, ge=1, le=instructions.MAX_RUNTIME_DOC_BYTES)


class WorkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    introduction: IntroductionRequest
    concept: ConceptRequest


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected('project_json', 'Duplicate JSON object field')
        result[key] = value
    return result


def _nonfinite(value):
    raise Rejected('project_json', 'Non-finite JSON values are not supported')


def _load(path: Path, schema):
    # CLI input filenames may be relative; keep symlink/alias detection intact.
    path = native_paths._canonical(str(Path(path).absolute()), regular_file=True)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise Rejected('project_json', 'Project and work requests must each fit within 2 MiB')
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    schema.model_validate(value, strict=True)
    return value, {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def _private(path: Path, *, file=False):
    """Never adopt foreign or broadly accessible coordinator controls."""
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISREG(info.st_mode) if file else stat.S_ISDIR(info.st_mode))
            or file and info.st_nlink != 1):
        raise Rejected('project_controls', 'Coordinator controls must be private, user-owned and unlinked')


def _checks(checks: list, ceiling: int) -> dict:
    return validate_descriptors(checks, ceiling)


def _lifecycle_count(state: Path, work_id: str) -> int:
    path = state / 'aos.sqlite3'
    if not path.exists() and not path.is_symlink():
        return 0
    native_paths._canonical(str(path), regular_file=True)
    _private(path, file=True)
    # No Store construction, schema migration, journal creation or write mode.
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'work' not in tables:
                raise Rejected('project_accounting', 'Existing coordinator state is not a recognized AOS ledger')
            exists = db.execute('SELECT 1 FROM work WHERE work_id=?', (work_id,)).fetchone()
            if 'assignment' not in tables:
                if exists and 'lifecycle' in tables and db.execute(
                        'SELECT 1 FROM lifecycle WHERE work_id=?', (work_id,)).fetchone():
                    raise Rejected('project_accounting', 'Existing lifecycle assignment accounting is unavailable')
                return 0
            return db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (work_id,)).fetchone()[0]
    except sqlite3.Error as error:
        raise Rejected('project_accounting', 'Cannot read retained lifecycle accounting; no capacity assumed') from error


def _discover_instructions(source: Path, *, project_doc_max_bytes: int | None = None) -> dict:
    options = {} if project_doc_max_bytes is None else {'project_doc_max_bytes': project_doc_max_bytes}
    manifest = instructions.discover(source, **options)
    config_path = str(Path(manifest['codex_home']) / 'config.toml')
    record = next((item for item in manifest['configs'] if item['path'] == config_path), None)
    if record is not None:
        config = instructions.read_configuration(Path(config_path), record)
        projects = config.get('projects', {})
        if not isinstance(projects, dict):
            raise Rejected('instruction_registration', 'Configured project trust entries must be an object')
        if manifest['project_root'] not in projects:
            return instructions.discover(source, allow_project_registration=True, **options)
    return manifest


def _directory_plan(recipe: dict, state: Path, required: list[Path]) -> list[str]:
    """Validate bounded empty-directory creation; full runtime checks follow it."""
    checkout_boundary._config(recipe)
    source, output = Path(recipe['source']), Path(recipe['outputs'])
    if native_paths.overlaps(source, output):
        raise Rejected('project_scope', 'Source and outputs must be disjoint')
    trusted = checkout_boundary._trusted(recipe)
    denied = checkout_boundary._denied(recipe, state) | checkout_boundary._instruction_paths(recipe)
    if source == Path('/') or source == Path.home().resolve() or any(
            native_paths.overlaps(source, guard) for guard in trusted):
        raise Rejected('project_scope', 'Isolated source cannot overlap trusted runtime roots')
    for entry in recipe.get('native_write_roots', []):
        path = native_paths._canonical(entry['path'])
        if entry['kind'] == 'generated':
            if path == source or not path.is_relative_to(source):
                raise Rejected('project_scope', 'Generated directories must be strict source children')
            if checkout_boundary._control(path.relative_to(source)):
                raise Rejected('project_scope', 'Generated directories cannot alter repository or instruction controls')
        elif entry['kind'] == 'cache':
            native_paths._canonical(entry['path'], directory=True)
        elif entry['kind'] == 'cache-file':
            native_paths._canonical(entry['path'], regular_file=True)
        else:
            raise Rejected('project_scope', 'Unknown native directory kind')
    for key, value in recipe['checkout_policy'].get('native_environment', {}).items():
        if key in {'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'}:
            path = Path(value)
            if path == output or not path.is_relative_to(output):
                raise Rejected('project_scope', 'Owned runtime directories must be strict output children')
    proposed = sorted(set(required), key=lambda path: (len(path.parts), str(path)))
    missing = []
    for path in proposed:
        native_paths._canonical(str(path))
        # State may contain its own configured control files, but never trusted
        # code or an enclosing credential/control namespace.
        guards = trusted | {guard for guard in denied
                            if not (path == state and (guard == state or guard.is_relative_to(state)))}
        if path in {Path('/'), Path.home().resolve()} or any(
                native_paths.overlaps(path, guard) for guard in guards):
            raise Rejected('project_scope', 'Proposed directory overlaps a trusted or protected path')
        if path.exists():
            if not path.is_dir() or path.stat().st_uid != os.getuid():
                raise Rejected('project_directories', 'Existing configured directories must be real and user-owned')
            continue
        if not path.parent.is_dir() and str(path.parent) not in missing:
            raise Rejected('project_directories', 'Every missing parent must be explicitly configured in the directory plan')
        if path.parent.exists() and path.parent.stat().st_uid != os.getuid():
            raise Rejected('project_directories', 'New directory parents must belong to the current user')
        missing.append(str(path))
    return missing


def _paths(config: dict, bindings: dict, packet: dict) -> dict:
    recipe = copy.deepcopy(config['recipe'])
    if set(recipe) - _RECIPE_KEYS or recipe.get('execution_policy') != checkout_boundary.POLICY:
        raise Rejected('project_recipe', 'Unsupported recipe fields or execution policy')
    state = native_paths._canonical(config['state_dir'])
    readiness = native_paths._canonical(config['readiness_dir'])
    source = native_paths._canonical(recipe['source'], directory=True)
    limit = config.get('project_doc_max_bytes')
    if 'instruction_manifest' not in recipe:
        recipe['instruction_manifest'] = _discover_instructions(source, project_doc_max_bytes=limit)
    elif limit is not None and instructions._runtime_limit(recipe['instruction_manifest']) != limit:
        raise Rejected('project_instruction_limit', 'Configured instruction limit differs from the explicit manifest override')
    output = native_paths._canonical(recipe['outputs'])
    workers = [native_paths._canonical(p) for p in config['worker_writable_roots']]
    if len(set(workers)) != len(workers) or set(workers) != {source, output}:
        raise Rejected('project_scope', 'Worker roots must be the exact isolated source and output directories')
    if native_paths.overlaps(state, readiness):
        raise Rejected('project_controls', 'Product state and readiness must have separate private roots')
    service = config['decision_service']
    certificate = native_paths._canonical(service['certificate'], regular_file=True)
    key = native_paths._canonical(service['key'], regular_file=True)
    _private(key, file=True)
    if not certificate.is_file():
        raise Rejected('project_controls', 'Decision certificate must be an existing regular file')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(str(certificate), str(key), password=lambda: '')
    except (OSError, ValueError) as error:
        raise Rejected('project_controls', 'Decision certificate and unencrypted private key do not form a usable pair') from error
    if not service['qualification_reference'].strip():
        raise Rejected('project_controls', 'Decision service needs its existing qualification reference')
    if certificate == key:
        raise Rejected('project_controls', 'Certificate and private key must be separate files')
    required = [state, output]
    for entry in recipe.get('native_write_roots', []):
        if not isinstance(entry, dict) or set(entry) != {'path', 'kind'}:
            raise Rejected('project_scope', 'Native writes must use existing exact path/kind declarations')
        path = native_paths._canonical(entry['path'])
        if entry['kind'] == 'generated':
            required.append(path)
    policy = recipe.get('checkout_policy')
    if not isinstance(policy, dict) or not isinstance(policy.get('native_environment', {}), dict):
        raise Rejected('project_recipe', 'Checkout policy and native environment must be objects')
    if checkout_boundary.reviewed_host(recipe) and config['readiness']['checks']:
        raise Rejected('project_readiness', 'Reviewed host validation requires metadata-only readiness with no native checks')
    if 'native_local_tests' in policy:
        native_network.contract(recipe)  # Validate exact settings; never observe or open host ports here.
        if service['port'] not in policy['native_local_tests']['denied_tcp_ports']:
            raise Rejected('project_decision_port',
                           f"Decision service port {service['port']} must be explicitly listed in native_local_tests.denied_tcp_ports")
    for value in policy.get('native_environment', {}).items():
        if value[0] in {'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'}:
            required.append(native_paths._canonical(value[1]))
    declared_writes = [native_paths._canonical(entry['path']) for entry in recipe.get('native_write_roots', [])]
    controls = [state, readiness / 'control', readiness / 'runtime', certificate, key,
                *(Path(value['path']) for value in bindings.values())]
    reads = [native_paths._canonical(p) for p in recipe.get('read_roots', [])]
    for control in [*controls, readiness]:
        if any(native_paths.overlaps(control, root) for root in [*workers, *declared_writes, *reads]):
            raise Rejected('project_controls', 'Project read/write grants overlap coordinator controls or decision files')
    # These negative roots also cover the native backend's implicit temporary
    # writes. Only readiness control/runtime are denied: its owned check outputs
    # must remain usable by the readiness runner.
    existing = policy.get('protected_roots', [])
    if (not isinstance(existing, list) or not all(isinstance(p, str) for p in existing)
            or len(set(existing)) != len(existing)):
        raise Rejected('project_recipe', 'Protected roots must be an exact path list')
    policy['protected_roots'] = sorted(set(existing) | {str(path) for path in controls})
    # The retained raw readiness output must not remain writable through the
    # native backend's implicit temporary-directory grants on later product jobs.
    if any(native_paths.overlaps(readiness, root) for root in checkout_boundary._temporary_roots()):
        raise Rejected('project_controls', 'Retained readiness must be outside native temporary directories')
    for path in (state, readiness, readiness / 'control', readiness / 'runtime'):
        if path.exists():
            _private(path)
    packet['required_directories'] = _directory_plan(recipe, state, required)
    instructions.validate(recipe.get('instruction_manifest'), source, state)
    if not packet['required_directories']:
        checkout_boundary.validate(recipe, state)
        native_paths.validate(recipe, protected=(state, readiness, certificate, key))
        native_processes.validate(recipe)
    return recipe


def _request(config: dict, request: dict, recipe: dict):
    intro, concept = request['introduction'], request['concept']
    bounds = concept['execution_envelope']
    # Reuse the approval/dispatch barrier before directory preparation or any
    # scarce readiness attempt. Schema validity alone cannot qualify a plan.
    validate_introduction(intro['title'], intro['structure'], intro['team'])
    package = concept_package(**{key: value for key, value in concept.items()
                                 if key not in ('operation_id', 'expected_work_revision')})
    validate_contract({**bounds, 'concept_revision': concept['revision'], 'concept_digest': digest(package)})
    if set(bounds) - _ENVELOPE_KEYS:
        raise Rejected('project_request', 'Unsupported execution envelope field for this profile')
    if (intro['work_id'] != concept['work_id'] or intro['team'] != concept['team_envelope']
            or concept['authority'] != bounds['authority']):
        raise Rejected('project_request', 'Work identity, team and authority must agree exactly')
    for name in ('operation_id', 'work_id'):
        identifier(intro[name])
        identifier(concept[name])
    identifier(concept['package_id'])
    if not bounds['model'].strip() or not bounds['effort'].strip():
        raise Rejected('project_request', 'Model and effort must be explicit')
    names = {person['name']: person for person in intro['team']}
    if len(names) != len(intro['team']) or bounds['writer'] == bounds['assessor']:
        raise Rejected('project_request', 'Unique team members and an independent assessor are required')
    for role in ('writer', 'assessor'):
        member = names.get(bounds[role])
        if not member or (member['model'], member['effort']) != (bounds['model'], bounds['effort']):
            raise Rejected('project_request', 'Execution model/effort must match the declared writer and assessor')
    if bounds['workflow'] not in ('feature-change', 'bug-fix') or (
            bounds['workflow'] == 'feature-change' and not bounds['compatibility_checks']):
        raise Rejected('project_request', 'Use the existing feature or bug-fix workflow with its compatibility checks')
    native_owner = native_processes.HOST_BACKEND if checkout_boundary.reviewed_host(recipe) else native_processes.BACKEND
    if (recipe.get('requires_detached_processes') is not False
            or recipe.get('native_process_ownership') != native_owner
            or type(recipe.get('max_seconds')) is not int
            or not 1 <= recipe['max_seconds'] <= bounds['max_seconds']):
        raise Rejected('project_recipe', 'Recipe requires a bounded non-detached native owner')
    for name in ('implementation_prompt', 'assessment_prompt'):
        if not isinstance(recipe.get(name), str) or not recipe[name].strip():
            raise Rejected('project_recipe', 'Implementation and assessment prompts must be explicit')
    product = bounds.get('product_budget')
    if not isinstance(product, dict) or set(product) != _BUDGET_KEYS:
        raise Rejected('project_budget', 'An exact stable product allowance is required')
    allowance = budgets.approved_budget(bounds, Path(config['state_dir']))
    if recipe['max_seconds'] > allowance['max_job_seconds']:
        raise Rejected('project_budget', 'Recipe duration exceeds the approved per-job allowance')
    selected = _checks(recipe.get('checks'), recipe['max_seconds'])
    if any('result_kind' in c or 'adapter_id' in c for c in recipe['checks']):
        interpreter = recipe.get('probe_interpreter')
        if (not isinstance(interpreter, str) or not Path(interpreter).is_absolute()
                or not Path(interpreter).is_file()):
            raise Rejected('project_recipe', 'Typed checks require an explicit existing probe interpreter')
    expected = bounds['required_checks'] + bounds['compatibility_checks']
    if not selected or len(set(expected)) != len(expected) or set(selected) != set(expected):
        raise Rejected('project_checks', 'Recipe must contain exactly the declared feature and compatibility checks')
    _checks(config['readiness']['checks'], config['readiness']['max_seconds'])
    prerequisite = config['readiness'].get('prerequisite', 'host_readiness')
    if prerequisite not in bounds['prerequisites'] or any(
            not value for name, value in bounds['prerequisites'].items() if name != prerequisite):
        raise Rejected('project_prerequisites', 'Declare the readiness prerequisite; resolve other prerequisites before preparation')
    identities = check_adapters.identities()
    adapters = {name: identities[name] for name in sorted({adapter.ADAPTER_ID for adapter in selected.values()})}
    technical = {'execution_policy': checkout_boundary.POLICY, 'change_roots': recipe['checkout_policy']['change_roots'],
                 'check_adapters': adapters}
    for name, value in technical.items():
        if name in bounds and bounds[name] != value:
            raise Rejected('project_scope', 'Supplied execution scope or adapter identity differs from the project')
    if 'runtime_recipe' in bounds and bounds['runtime_recipe'] != recipe:
        raise Rejected('project_scope', 'Supplied exact runtime recipe differs from the prepared project controls')
    if 'runtime_recipe_digest' in bounds and bounds['runtime_recipe_digest'] != digest(recipe):
        raise Rejected('project_scope', 'Supplied runtime recipe digest differs from the project')
    return allowance, technical, prerequisite


def _successful_readiness(receipt: dict, recipe: dict) -> tuple[dict, str]:
    if isinstance(receipt, dict) and receipt.get('ready') is not True:
        blockers = receipt.get('blockers', [])
        if (isinstance(blockers, list) and blockers and isinstance(blockers[0], dict)
                and isinstance(blockers[0].get('detail'), str)):
            raise Rejected('project_readiness', blockers[0]['detail'])
    if (not isinstance(receipt, dict) or receipt.get('ready') is not True
            or receipt.get('status') != 'passed' or receipt.get('cleanup_verified') is not True
            or receipt.get('simulated') is not False
            or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in
                   ('model_turns', 'product_attempts', 'product_reservations'))):
        raise Rejected('project_readiness', 'Host readiness is incomplete, simulated, or lacks verified cleanup')
    prepared = receipt.get('prepared_recipe')
    if not isinstance(prepared, dict) or receipt.get('recipe_digest') != digest(prepared):
        raise Rejected('project_readiness', 'Readiness did not bind its exact prepared recipe')
    before, after = copy.deepcopy(recipe), copy.deepcopy(prepared)
    # Readiness already verifies the only allowed registration transition.
    before.pop('instruction_manifest', None)
    after.pop('instruction_manifest', None)
    if before != after:
        raise Rejected('project_scope', 'Readiness changed execution scope, checks or limits')
    manifest = instructions.readback(recipe['instruction_manifest'], Path(recipe['source']), after_startup=True)
    if prepared['instruction_manifest'] != manifest['manifest']:
        raise Rejected('project_readiness', 'Prepared instructions do not match current verified configuration')
    source = receipt.get('source_after')
    runtime_identity = receipt.get('runtime_identity')
    if (not isinstance(source, dict) or source != receipt.get('source_before')
            or not isinstance(source.get('inventory'), dict)
            or source.get('identity') != digest(source['inventory'])
            or source.get('source_identity') != source['identity']
            or not isinstance(runtime_identity, dict)
            or runtime_identity.get('runtime_source_digest') != source_digest()):
        raise Rejected('project_readiness', 'Readiness baseline or current runtime identity is inconsistent')
    return prepared, source['identity']


def _packet() -> dict:
    return {'format': FORMAT, 'ready': False, 'blocker': None, 'input_bindings': {},
            'finish_capacity': None, 'readiness': None, 'required_directories': [],
            'approval_recorded': False, 'reservation_performed': False}


def _failure(error: Exception) -> dict:
    detail = str(error) if isinstance(error, Rejected) else (
        'Invalid or unreadable project/work configuration (' + type(error).__name__ + ')')
    if isinstance(error, ValidationError):
        first = error.errors(include_input=False, include_url=False)[0]
        detail = 'Invalid configuration at ' + '.'.join(map(str, first['loc'])) + ': ' + first['msg']
    elif isinstance(error, json.JSONDecodeError):
        detail = f'Invalid JSON at line {error.lineno}, column {error.colno}'
    return {'code': getattr(error, 'code', 'project_configuration'), 'detail': detail}


def _plan(project_path: Path, request_path: Path, packet: dict):
    config, packet['input_bindings']['project'] = _load(project_path, ProjectConfiguration)
    request, packet['input_bindings']['request'] = _load(request_path, WorkRequest)
    packet.update(project=copy.deepcopy(config), introduction=copy.deepcopy(request['introduction']),
                  concept=copy.deepcopy(request['concept']))
    recipe = _paths(config, packet['input_bindings'], packet)
    allowance, technical, prerequisite = _request(config, request, recipe)
    bounds = request['concept']['execution_envelope']
    packet['finish_capacity'] = budgets.read_finish_capacity(
        Path(config['state_dir']) / 'codex-runtime.sqlite3', allowance, source=recipe['source'],
        max_repairs=bounds['max_repairs'], max_seconds=recipe['max_seconds'],
        max_startup_retries=bounds.get('max_startup_retries', 0),
        reviewed_host=checkout_boundary.reviewed_host(recipe),
        max_assignments=bounds['max_assignments'],
        lifecycle_assignments=_lifecycle_count(Path(config['state_dir']), request['concept']['work_id']))
    if not packet['finish_capacity']['ready']:
        raise Rejected('project_finish_capacity', packet['finish_capacity']['blockers'][0])
    return config, request, recipe, technical, prerequisite


def _unchanged(bindings: dict):
    if (not isinstance(bindings, dict) or set(bindings) != {'project', 'request'}
            or any(not isinstance(value, dict) or set(value) != {'path', 'sha256'} for value in bindings.values())):
        raise Rejected('project_input_drift', 'Exact original project/request bindings are required')
    for binding in bindings.values():
        path = native_paths._canonical(binding['path'], regular_file=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256']:
            raise Rejected('project_input_drift', 'Project or work request changed during preparation')


def static_plan(project_path: Path, request_path: Path) -> dict:
    """Inspect the existing setup plan without readiness or product effects.

    A complete static plan is never host readiness or approval: ready stays
    false, with no blocker only when all configured directories already exist.
    """
    packet = _packet()
    try:
        _plan(project_path, request_path, packet)
        _unchanged(packet['input_bindings'])
        if packet['required_directories']:
            raise Rejected('project_directories', 'Host preparation must create the listed private output/control directories once')
    except (Rejected, ValidationError, OSError, ValueError, TypeError, KeyError) as error:
        packet['blocker'] = _failure(error)
    return packet


def prepare_directories(project_path: Path, request_path: Path, *, plan: dict, state_only: bool = False) -> dict:
    """Create only the revalidated, explicit empty-directory plan once.

    This does not run readiness or adopt/repair existing controls. Full ordinary
    preparation must follow; any created directories are retained on failure.
    state_only establishes just the declared missing state root so a supervisor
    can acquire its existing worker lock before preparing other directories.
    """
    result = {'prepared': False, 'blocker': None, 'created_directories': []}
    if state_only is True:
        result['scope'] = 'state-only'
    try:
        if type(state_only) is not bool:
            raise Rejected('project_directories', 'Directory preparation scope must be an explicit boolean')
        if (not isinstance(plan, dict) or plan.get('format') != FORMAT or plan.get('ready') is not False
                or not isinstance(plan.get('blocker'), dict)
                or plan['blocker'].get('code') != 'project_directories'
                or not isinstance(plan.get('required_directories'), list) or not plan['required_directories']):
            raise Rejected('project_directories', 'A complete current directory plan is required')
        _unchanged(plan['input_bindings'])
        fresh = _packet()
        _plan(project_path, request_path, fresh)
        if (fresh['input_bindings'] != plan['input_bindings']
                or fresh['required_directories'] != plan['required_directories']):
            raise Rejected('project_input_drift', 'Project directory plan changed before initialization')
        _unchanged(fresh['input_bindings'])
        targets = fresh['required_directories']
        if state_only:
            state = fresh['project']['state_dir']
            if state not in targets:
                raise Rejected('project_directories', 'State-only initialization requires the declared state directory to be missing')
            targets = [state]
        for name in targets:
            path = native_paths._canonical(name)
            native_paths._canonical(str(path.parent), directory=True)
            if path.parent.stat().st_uid != os.getuid():
                raise Rejected('project_directories', 'Directory parent ownership changed')
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                # A concurrent creator is not authority to repair permissions.
                native_paths._canonical(name, directory=True)
                _private(path)
            else:
                result['created_directories'].append(name)
                native_paths._canonical(name, directory=True)
                _private(path)
        result['prepared'] = True
    except (Rejected, ValidationError, OSError, ValueError, TypeError, KeyError) as error:
        result['blocker'] = _failure(error)
    return result


def prepare(project_path: Path, request_path: Path, *, readiness_runner=run_readiness) -> dict:
    """Validate, preview capacity, then prepare one complete existing concept.

    No product Store/runtime, registration, decision or reservation is created.
    Missing project/output directories are reported, never created here. The
    callable seam is for trusted Python tests only, never JSON configuration.
    """
    packet = _packet()
    try:
        config, request, recipe, technical, prerequisite = _plan(project_path, request_path, packet)
        if packet['required_directories']:
            raise Rejected('project_directories', 'Host preparation must create the listed private output/control directories once')
        bounds = request['concept']['execution_envelope']
        packet['readiness'] = readiness_runner(
            copy.deepcopy(recipe), state_dir=Path(config['state_dir']), readiness_dir=Path(config['readiness_dir']),
            model=bounds['model'], effort=bounds['effort'], max_seconds=config['readiness']['max_seconds'],
            max_attempts=config['readiness']['max_attempts'], native_checks=copy.deepcopy(config['readiness']['checks']))
        prepared, baseline = _successful_readiness(packet['readiness'], recipe)
        if bounds['baseline'] not in ('auto', baseline):
            raise Rejected('project_baseline', 'Observed source differs from the requested exact baseline')
        _unchanged(packet['input_bindings'])
        current_adapters = check_adapters.identities()
        if any(current_adapters.get(name) != identity for name, identity in technical['check_adapters'].items()):
            raise Rejected('project_adapter_drift', 'Native check adapters changed during preparation')
        final = packet['concept']['execution_envelope']
        final.update(technical, baseline=baseline, runtime_recipe=prepared, runtime_recipe_digest=digest(prepared))
        final['prerequisites'][prerequisite] = True
        packet['ready'] = True
    except (Rejected, ValidationError, OSError, ValueError, TypeError, KeyError) as error:
        packet['blocker'] = _failure(error)
    return packet
