"""Reviewed ordinary-user checks, reusing the product native-check journal.

This tier is cooperative host execution, not a filesystem/network boundary.
Receipts establish observed-owner cleanup and declared source/output effects;
they cannot exclude unobserved detached processes or all shared-host effects.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from agentic_tools.check_adapters import identities as adapter_identities
from . import checkout_boundary, native_journal, native_paths, owned_host_command
from .checks import adapter_for, passed as evidence_passed, source_manifest, validate_normalized
from .store import Rejected, canonical, digest, identifier, now


BACKEND = 'host-owned-v1'
LIMITS = ('Reviewed commands run with ordinary user permissions. Cleanup covers observed owners '
          'and declared source/output effects, not unobserved detached processes, transient peaks '
          'or an exhaustive reconciliation of shared host effects.')


def _helpers():
    # The runtime dispatches here; defer its small inventory helpers to avoid a
    # circular import and keep candidate identity identical to the model path.
    from .codex_runtime import inventory, changed
    return inventory, changed


def _ordinary(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise Rejected('runtime_output', 'Reviewed check paths must be exact and contain no symlinks')


def _clock(end: float):
    def check_deadline():
        if time.monotonic() >= end:
            raise Rejected('assignment_deadline', 'The original reviewed-check deadline expired')
    return check_deadline


def _candidate(recipe: dict, deadline=None) -> dict:
    inventory, _ = _helpers()
    files = inventory(Path(recipe['source']), recipe=recipe, check_deadline=deadline)
    return {'identity': digest(files), 'source_identity': digest(files), 'inventory': files}


def _gate(runtime, recipe, descriptor, output, executor, frozen, *, deadline=None, observe_source=True):
    if not checkout_boundary.reviewed_host(recipe):
        raise Rejected('reviewed_host', 'Reviewed host checks require explicit approved selection')
    aid = recipe.get('_attempt_id')
    if not isinstance(aid, str) or not (attempt := runtime._get(aid)):
        raise Rejected('runtime_scope', 'Reviewed checks require an exact real product attempt')
    assignment = attempt.get('assignment', {})
    runtime._require_work(assignment)
    runtime._coordinator_evidence(assignment, native=True, check=descriptor, output=output)
    if (attempt.get('attempt_id') != aid or assignment.get('attempt_id') != aid
            or assignment.get('kind') != 'assessment' or assignment.get('executor') != executor
            or attempt.get('startup_qualification_only') or attempt.get('startup_outcome')
            or attempt.get('model_calls_attempted') != 1
            or attempt.get('terminal_turn', {}).get('status') != 'completed'):
        raise Rejected('reviewed_host', 'Only a completed product assessment may admit reviewed checks')
    if attempt.get('native_cancel_requested'):
        raise Rejected('check_cancelled', 'Native admission was durably cancelled')
    errors = runtime.lifecycle.assignment_authority_errors(assignment)
    if errors:
        raise Rejected('runtime_authority', '; '.join(errors))
    model = attempt.get('model_stop_receipt', {})
    if (model.get('attempt_id') != aid or model.get('stopped') is not True
            or model.get('effects_reconciled') is not True or model.get('owned_processes') != []):
        raise Rejected('check_cleanup_unknown', 'Positive model cleanup must precede reviewed host checks')
    base = runtime.recipes.get(assignment.get('work_id'))
    gate = attempt.get('reviewed_host_gate')
    if (not isinstance(base, dict) or not checkout_boundary.reviewed_host(base)
            or not isinstance(gate, dict) or gate.get('mode') != 'reviewed-host-v1'
            or digest(base) != attempt.get('recipe_digest')
            or gate.get('recipe_digest') != attempt.get('recipe_digest')
            or gate.get('candidate') != frozen or gate.get('checks') != base.get('checks')
            or descriptor not in gate['checks']):
        raise Rejected('reviewed_host', 'Saved review gate differs from the approved candidate or recipe')
    if json.loads(assignment.get('candidate', 'null')) != frozen:
        raise Rejected('source_drift', 'Assessment candidate differs from the saved review gate')
    bindings = runtime._bindings(assignment, base)
    if gate.get('adapters') != bindings:
        raise Rejected('check_adapter', 'Saved review adapter bindings changed')
    expected = copy.deepcopy(base)
    guards = expected['checkout_policy'].get('protected_roots', [])
    expected['checkout_policy']['protected_roots'] = sorted(set(guards) | {str(runtime.directory.resolve())})
    expected = checkout_boundary.scoped_outputs(expected, Path(attempt['run_outputs']))
    expected.update(_attempt_id=aid, _adapter_bindings=bindings)
    identifier(descriptor['name'])
    if recipe != expected or output != Path(attempt['run_outputs']) / descriptor['name']:
        raise Rejected('runtime_scope', 'Reviewed check declarations or output differ from the exact attempt')
    reference = gate.get('review', {})
    report = Path(attempt['run_outputs']) / ('review-' + aid + '.txt')
    _ordinary(report)
    if (reference.get('path') != str(report) or not report.is_file()
            or not 0 < report.stat().st_size <= 1024 * 1024):
        raise Rejected('invalid_review', 'Exact saved source review is unavailable')
    raw = report.read_bytes()
    review = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != reference.get('sha256')
            or review.get('candidate_identity') != frozen['identity']
            or review.get('verdict') != 'pass' or review.get('findings') != []
            or not isinstance(review.get('limits'), str) or not review['limits'].strip()):
        raise Rejected('invalid_review', 'Saved source review is changed or does not pass')
    if source_manifest() != runtime._native_source_manifest:
        raise Rejected('check_adapter', 'Trusted runtime or adapter source changed before reviewed execution')
    adapter = adapter_for(descriptor)
    identity = adapter_identities()[adapter.ADAPTER_ID]
    binding = bindings.get(adapter.ADAPTER_ID)
    if not binding or any(binding.get(key) != value for key, value in identity.items()):
        raise Rejected('check_adapter', 'Reviewed check adapter identity changed')
    if observe_source and _candidate(recipe, deadline) != frozen:
        raise Rejected('source_drift', 'Reviewed source changed before native execution')
    if deadline:
        deadline()
    command = adapter.command([part.replace('{output}', recipe['outputs']) for part in descriptor['argv']], output)
    return attempt, gate, adapter, binding, command


def _persist(runtime, check_id, snapshot):
    with runtime._native_lock:
        row = runtime._native_get(check_id)
        if not row or row.get('check_id') != check_id or row.get('ownership_backend') != BACKEND:
            raise Rejected('check_cleanup_unknown', 'Reviewed host ownership journal changed')
        previous = row.get('host_owner_receipt', {})
        if row.get('phase') == 'clean':
            if snapshot != previous:
                raise Rejected('check_cleanup_unknown', 'Late ownership evidence differs from completed cleanup')
            return
        for key in ('owner', 'controller'):
            if previous.get(key) and snapshot.get(key) != previous[key]:
                raise Rejected('check_cleanup_unknown', 'Reviewed host process identity changed')
        # Reconciliation can race a live runner callback. A later partial view
        # cannot erase a completed receipt or its immutable launch binding.
        if previous.get('cleanup_verified') is True and snapshot.get('cleanup_verified') is not True:
            return
        if (snapshot.get('backend') != BACKEND or snapshot.get('control') != row['host_control']
                or snapshot.get('argv') != row['command'] or snapshot.get('cwd') != row['source']
                or snapshot.get('environment_sha256') != row['environment_sha256']
                or snapshot.get('deadline_monotonic') != row['deadline_monotonic']
                or snapshot.get('cleanup_deadline_monotonic') != row['cleanup_deadline_monotonic']):
            raise Rejected('check_cleanup_unknown', 'Runner snapshot differs from the durable launch binding')
        row['host_owner_receipt'] = copy.deepcopy(snapshot)
        row['owner'] = copy.deepcopy(snapshot.get('owner'))
        if snapshot.get('owner'):
            row['phase'] = 'running'
        try:
            runtime._native_save(row)
        except Rejected as error:
            # Another runtime may finish cleanup between our read and atomic
            # save. Only the same final receipt (or an older partial callback)
            # is an idempotent no-op; conflicting final evidence remains held.
            current = runtime._native_get(check_id)
            completed = current.get('host_owner_receipt', {}) if current else {}
            if (error.code == 'native_state' and completed.get('cleanup_verified') is True
                    and (snapshot.get('cleanup_verified') is not True or completed == snapshot)):
                return
            raise


def _finish(runtime, row):
    if row.get('phase') == 'clean':
        return verify_completed(row)
    inventory, changed = _helpers()
    snapshot = row.get('host_owner_receipt')
    if not isinstance(snapshot, dict) or snapshot.get('cleanup_verified') is not True:
        raise Rejected('check_cleanup_unknown', 'Reviewed host cleanup remains unresolved')
    verified = owned_host_command.verify_completed(snapshot)
    if verified != snapshot:
        raise Rejected('check_cleanup_unknown', 'Newer host ownership evidence differs from the saved completion')
    deadline = _clock(snapshot['cleanup_deadline_monotonic'])
    deadline()
    source = inventory(Path(row['source']), recipe=row['native_path_recipe'], check_deadline=deadline)
    outputs = inventory(Path(row['outputs_root']), allow_empty=True, check_deadline=deadline)
    row['cleanup'] = {
        'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
        'partial_mutations': changed(row['before_source'], source),
        'output_mutations': changed(row['before_outputs'], outputs),
        'source_after': source, 'outputs_after': outputs, 'observed_at': now(),
        'process_ownership': copy.deepcopy(snapshot), 'limits': LIMITS}
    row['phase'] = 'clean'
    runtime._native_save(row)
    return row


def stop(runtime, check_id, cancel=False):
    """Reconcile retained ownership; never launch or infer success from absence."""
    with runtime._native_lock:
        row = runtime._native_get(check_id)
        if not row or row.get('check_id') != check_id or row.get('ownership_backend') != BACKEND:
            raise Rejected('check_cleanup_unknown', 'Exact reviewed host check is missing')
        if row.get('phase') == 'clean':
            return verify_completed(row)
        if cancel:
            row['cancelled'] = True
            runtime._native_save(row)
        snapshot = copy.deepcopy(row.get('host_owner_receipt'))
    try:
        if not snapshot:
            raise Rejected('check_cleanup_unknown', 'Launch intent has no retained ownership proof; never relaunch')
        if snapshot.get('cleanup_verified') is not True:
            snapshot = owned_host_command.reconcile_owned(
                snapshot, deadline=snapshot['cleanup_deadline_monotonic'],
                persist=lambda value: _persist(runtime, check_id, value), cancel=cancel)
            _persist(runtime, check_id, snapshot)
        with runtime._native_lock:
            return _finish(runtime, runtime._native_get(check_id))
    except Exception:
        with runtime._native_lock:
            latest = runtime._native_get(check_id)
            if latest and latest.get('phase') != 'clean':
                latest.pop('cleanup', None)
                latest['phase'] = 'cleanup_unknown'
                runtime._native_save(latest)
        raise


def verify_completed(row):
    """Read-only retained receipt verification; no journal writes or signals."""
    if not isinstance(row, dict):
        raise Rejected('check_cleanup_unknown', 'Exact completed reviewed host cleanup is required')
    checked = copy.deepcopy(row)
    cleanup = checked.get('cleanup', {})
    snapshot = checked.get('host_owner_receipt')
    if (checked.get('ownership_backend') != BACKEND or checked.get('phase') != 'clean'
            or cleanup.get('stopped') is not True or cleanup.get('effects_reconciled') is not True
            or cleanup.get('owned_processes') != [] or cleanup.get('process_ownership') != snapshot):
        raise Rejected('check_cleanup_unknown', 'Exact completed reviewed host cleanup is required')
    if (not isinstance(snapshot, dict) or snapshot.get('control') != checked.get('host_control')
            or snapshot.get('argv') != checked.get('command') or snapshot.get('cwd') != checked.get('source')
            or snapshot.get('environment_sha256') != checked.get('environment_sha256')
            or snapshot.get('deadline_monotonic') != checked.get('deadline_monotonic')
            or snapshot.get('cleanup_deadline_monotonic') != checked.get('cleanup_deadline_monotonic')):
        raise Rejected('check_cleanup_unknown', 'Completed ownership differs from the saved command binding')
    verified = owned_host_command.verify_completed(snapshot)
    if verified != snapshot:
        raise Rejected('check_cleanup_unknown', 'Newer host ownership evidence differs from the saved completion')
    if checked.get('receipt'):
        from .codex_runtime import CodexRuntime
        if (checked['receipt'].get('cleanup') != cleanup
                or checked['receipt'].get('candidate') != checked.get('candidate')
                or checked['receipt'].get('native_check_id') != checked.get('check_id')):
            raise Rejected('check_cleanup_unknown', 'Completed check evidence differs from its cleanup')
        CodexRuntime._verify_check_receipt(checked['receipt'])
    return checked


def check(runtime, recipe, check, output, executor, frozen):
    """Admit one reviewed command; saved intent is never a retry allowance."""
    descriptor = check
    output = Path(output)
    _ordinary(output)
    attempt, gate, adapter, binding, command = _gate(
        runtime, recipe, descriptor, output, executor, frozen, observe_source=False)
    check_id = 'nc-' + digest([str(output.resolve()), command, frozen])[7:39]
    environment = checkout_boundary.environment(recipe, native=True)
    environment_sha = hashlib.sha256(json.dumps(environment, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    binding_identity = {'gate': digest(gate), 'command': command, 'adapter': binding,
                        'candidate': frozen, 'contract': native_paths.contract(recipe),
                        'environment_sha256': environment_sha}
    inventory, _ = _helpers()
    with runtime._native_lock:
        row = runtime._native_get(check_id)
        fresh = row is None
        if row is not None:
            if (row.get('check_id') != check_id or row.get('ownership_backend') != BACKEND
                    or row.get('attempt_id') != attempt['attempt_id']
                    or row.get('reviewed_binding') != binding_identity):
                raise Rejected('reviewed_host', 'Saved check belongs to a different reviewed launch')
            _gate(runtime, recipe, descriptor, output, executor, frozen)
        else:
            execution_deadline = runtime._native_deadline(recipe, descriptor)
            end = time.monotonic() + (execution_deadline - datetime.now(timezone.utc)).total_seconds()
            deadline = _clock(end)
            deadline()
            outer_end = attempt.get('startup_deadline_monotonic')
            if (type(outer_end) not in (int, float) or not math.isfinite(outer_end)
                    or outer_end < end):
                raise Rejected('assignment_deadline', 'The original assignment cleanup deadline is unavailable')
            # The journal revision fences a concurrent command while baselines
            # are read without a long SQLite write transaction.
            with runtime._db() as db:
                revision = native_journal.revision(db)
            before_outputs = inventory(Path(recipe['outputs']), allow_empty=True, check_deadline=deadline)
            _gate(runtime, recipe, descriptor, output, executor, frozen, deadline=deadline)
            control = runtime.directory / 'native-status' / (check_id + '-host')
            _ordinary(control)
            row = {'check_id': check_id, 'attempt_id': attempt['attempt_id'], 'phase': 'intent',
                   'ownership_backend': BACKEND, 'source': recipe['source'], 'outputs_root': recipe['outputs'],
                   'output': str(output), 'command': command, 'candidate': frozen, 'adapter': binding,
                   'before_source': frozen['inventory'], 'before_outputs': before_outputs,
                   'native_path_contract': native_paths.contract(recipe), 'native_path_recipe': copy.deepcopy(recipe),
                   'reviewed_binding': binding_identity, 'environment_sha256': environment_sha,
                   'host_control': str(control), 'status': str(control / 'status.json'),
                   'execution_deadline': execution_deadline.isoformat(), 'deadline_monotonic': end,
                   'cleanup_deadline_monotonic': min(end + 26, outer_end),
                   'created_at': now(), 'limits': LIMITS}
            with runtime._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if native_journal.revision(db) != revision or native_journal.active_rows(db):
                    raise Rejected('native_ownership', 'An unresolved native check owns the serial host test lane')
                deadline()
                output.mkdir(mode=0o700, parents=True, exist_ok=False)
                control.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, attempt['attempt_id'], canonical(row)))
    if fresh:
        def cancelled():
            saved = runtime._get(attempt['attempt_id'])
            current = runtime._native_get(check_id)
            return (not saved or not current or saved.get('native_cancel_requested') is True
                    or current.get('cancelled') is True)
        def before_go(deadline):
            with runtime._native_lock:
                if cancelled():
                    raise Rejected('check_cancelled', 'Native check was cancelled before GO')
                current = runtime._native_get(check_id)
                _, latest_gate, _, latest_binding, latest_command = _gate(
                    runtime, recipe, descriptor, output, executor, frozen, deadline=_clock(deadline))
                if (digest(latest_gate) != binding_identity['gate'] or latest_binding != binding
                        or latest_command != command or current.get('reviewed_binding') != binding_identity
                        or checkout_boundary.environment(recipe, native=True) != environment):
                    raise Rejected('reviewed_host', 'Reviewed launch binding changed before GO')
        try:
            snapshot = owned_host_command.run_owned(
                command, recipe['source'], environment, Path(row['host_control']), deadline=row['deadline_monotonic'],
                cleanup_deadline=row['cleanup_deadline_monotonic'],
                before_go=before_go, cancelled=cancelled,
                persist=lambda value: _persist(runtime, check_id, value))
            _persist(runtime, check_id, snapshot)
        except BaseException:
            # A missing first callback is not proof that Popen had no effects.
            # stop() reconciles only exact retained ownership, never relaunches.
            try:
                stop(runtime, check_id, cancel=True)
            except Exception:
                pass
            raise
    row = stop(runtime, check_id)
    if row.get('receipt'):
        return row['receipt']
    snapshot = row['host_owner_receipt']
    control = Path(row['host_control'])
    stdout_path, stderr_path = control / 'stdout.log', control / 'stderr.log'
    raw_paths = []
    for path in (stdout_path, stderr_path):
        _ordinary(path)
        if path.is_file():
            raw_paths.append(path)
        elif not snapshot.get('no_launch') or path.exists():
            raise Rejected('check_evidence', 'Admitted command output is missing or is not an ordinary file')
    stdout, stderr = (path.read_text() if path in raw_paths else '' for path in (stdout_path, stderr_path))
    transcript = output / 'output.txt'
    _ordinary(transcript)
    transcript_bytes = (stdout + '\n' + stderr).encode()
    try:
        with transcript.open('xb') as stream:
            stream.write(transcript_bytes)
    except FileExistsError:
        # A coordinator interruption after writing evidence does not authorize
        # another command. Resume only the identical ordinary transcript.
        _ordinary(transcript)
        if not transcript.is_file() or transcript.read_bytes() != transcript_bytes:
            raise Rejected('check_evidence', 'Existing reviewed transcript differs from retained command output')
    if source_manifest() != runtime._native_source_manifest:
        raise Rejected('check_adapter', 'Trusted runtime or adapter source changed during reviewed execution')
    exit_code = (snapshot.get('child') or {}).get('exit_code')
    evidence = adapter.interpret(output, stdout, stderr, exit_code)
    validate_normalized(evidence)
    # Include coordinator-created transcript/adapter artifacts in the final
    # endpoint observation; no command is rerun during evidence finalization.
    _, changed = _helpers()
    after = inventory(Path(recipe['outputs']), allow_empty=True,
                      check_deadline=_clock(row['cleanup_deadline_monotonic']))
    row['cleanup']['outputs_after'] = after
    row['cleanup']['output_mutations'] = changed(row['before_outputs'], after)
    row['cleanup']['observed_at'] = now()
    outcome = ('cancelled' if row.get('cancelled') or snapshot.get('cancel_requested') else
               'timeout' if snapshot.get('timed_out') else
               'blocked' if snapshot.get('errors') or not snapshot.get('launch_authorized')
               or row['cleanup']['partial_mutations']['changes'] else
               'pass' if evidence_passed(evidence, exit_code) else
               'fail' if evidence['recognized'] and (evidence['selected'] or evidence['kind'] == 'build') else 'blocked')
    result = {'name': descriptor['name'], 'command': command,
              'environment': 'reviewed ordinary-user host execution; sanitized normal project environment',
              'executor': executor, **evidence, 'result': outcome, 'adapter': binding,
              'output': str(transcript), 'output_sha256': hashlib.sha256(transcript.read_bytes()).hexdigest(),
              'candidate': frozen, 'scope': descriptor['scope'], 'limits': LIMITS, 'exit_code': exit_code,
              'native_check_id': check_id, 'cleanup': row['cleanup'], 'review': gate['review'],
              'raw_outputs': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in raw_paths}}
    for filename in evidence.get('artifacts', []):
        path = Path(filename)
        _ordinary(path)
        if not path.is_file() or not path.resolve().is_relative_to(output.resolve()):
            raise Rejected('check_evidence', 'Adapter artifact is outside the exact reviewed check output')
        result['raw_outputs'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    with runtime._native_lock:
        latest = runtime._native_get(check_id)
        if latest.get('phase') != 'clean' or latest.get('host_owner_receipt') != snapshot:
            raise Rejected('check_cleanup_unknown', 'Ownership changed during evidence finalization')
        latest['cleanup'] = row['cleanup']
        latest['receipt'] = result
        runtime._native_save(latest)
    return result
