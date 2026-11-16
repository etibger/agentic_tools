"""Evidence-backed milestones for the existing writer/assessor adapter.

Events are append-only and bound to the concept, candidate and assignment. They
are not coordinator assertions of task success. The lifecycle produces them only
after its normal candidate/effect and independent-assessment checks succeed.
"""
from __future__ import annotations

import json
from copy import deepcopy

from .planning import validate_execution_plan, execution_tasks
from .store import Rejected, digest
from .planning import Estimate
from pydantic import ValidationError


def _serial(package):
    return package['di_plan'].get('execution_profile') == 'serial-milestones-v1'


def _scope(assignment):
    try:
        value = json.loads(assignment['scope']) if isinstance(assignment['scope'], str) else assignment['scope']
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def task_in(db, assignment):
    package = package_in(db, assignment['work_id'])
    tasks = execution_tasks(package['di_plan'])
    kind = 'assessment' if assignment['kind'] == 'assessment' else 'implementation'
    if not _serial(package):
        return next(t for t in tasks if t['kind'] == kind)
    scope = _scope(assignment)
    task = next((t for t in tasks if t['id'] == scope.get('di_task_id')), None)
    if (not task or task['kind'] != kind or task['owner'] != assignment['executor']
            or scope.get('di_milestone_id') != task['milestone']
            or scope.get('di_plan_digest') != digest(package['di_plan'])):
        raise Rejected('di_task_binding', 'Assignment must name its exact approved DI task and milestone')
    return task


def remaining_jobs_in(db, assignment):
    if not _scope(assignment).get('di_task_id'):
        return int(assignment['kind'] != 'assessment')
    package = package_in(db, assignment['work_id'])
    tasks = execution_tasks(package['di_plan'])
    task = task_in(db, assignment)
    return len(tasks) - next(i for i, t in enumerate(tasks) if t['id'] == task['id']) - 1


def implementation_baseline_in(db, assignment, contract):
    if not _scope(assignment).get('di_task_id'):
        return contract['baseline']
    task = task_in(db, assignment)
    if not task['depends_on']:
        return contract['baseline']
    candidate = json.loads(assignment['candidate']) if isinstance(assignment['candidate'], str) else assignment['candidate']
    if not isinstance(candidate, dict) or not candidate.get('source_identity'):
        raise Rejected('di_task_binding', 'Later writer requires its exact accepted checkpoint baseline')
    return candidate['source_identity']


def _proofs_in(db, work_id, concept_digest):
    proofs = {}
    for row in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_milestone_passed' ORDER BY event_id", (work_id,)):
        proof = json.loads(row[0])
        if proof.get('concept_digest') == concept_digest:
            proofs[proof['milestone_id']] = proof
    return proofs


def _verified_proof_in(db, work_id, package, proof, _seen=frozenset()):
    """A projection/event alone is insufficient: retain the qualified receipt."""
    try:
        identity = digest(proof)
        if identity in _seen or len(_seen) >= 32:
            return False
        seen = _seen | {identity}
        task = next(t for t in package['di_plan']['tasks'] if t['milestone'] == proof['milestone_id'])
        row = db.execute('SELECT a.*,c.result AS receipt_result,c.fingerprint,c.disposition FROM assignment a '
            'JOIN completion c ON c.assignment_id=a.assignment_id WHERE c.receipt_id=? AND a.work_id=?',
            (proof['receipt_id'], work_id)).fetchone()
        if not row or row['assignment_id'] != proof['assignment_id'] or row['state'] != 'completed':
            return False
        result = json.loads(row['receipt_result'])
        if (row['result'] != row['receipt_result'] or row['fingerprint'] != digest([row['assignment_id'], result])
                or result.get('candidate') != proof['candidate'] or result.get('executor') != task['owner']
                or result.get('status') != 'completed' or proof['task_ids'] != [task['id']]
                or _scope(row).get('di_task_id') != task['id']):
            return False
        origin = db.execute('SELECT package FROM concept WHERE work_id=? AND digest=?', (work_id, row['concept_digest'])).fetchone()
        if not origin or json.loads(origin[0]).get('di_plan') != package['di_plan']:
            return False
        if proof['concept_digest'] != row['concept_digest']:
            # Every inheritance hop refers to one retained, byte-exact proof.
            parent = next((json.loads(event[0]) for event in db.execute(
                "SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_milestone_passed'", (work_id,))
                if digest(json.loads(event[0])) == proof.get('inherited_proof_digest')), None)
            if (not parent or parent['concept_digest'] != proof.get('inherited_from_digest')
                    or any(parent[key] != proof[key] for key in
                           ('assignment_id', 'receipt_id', 'milestone_id', 'task_ids', 'candidate', 'evidence'))
                    or not _verified_proof_in(db, work_id, package, parent, seen)):
                return False
        origin_proofs = _proofs_in(db, work_id, row['concept_digest'])
        origin_milestone = next(m for m in package['di_plan']['milestones'] if m['id'] == task['milestone'])
        dependencies = _scope(row).get('di_dependencies', {})
        if set(dependencies) != set(origin_milestone['depends_on']):
            return False
        for dep in origin_milestone['depends_on']:
            prerequisite = origin_proofs.get(dep, {})
            if digest(prerequisite) != dependencies[dep] or not _verified_proof_in(db, work_id, package, prerequisite, seen):
                return False
        # A repair/retry of this task invalidates its earlier candidate freeze.
        for later in db.execute('SELECT * FROM assignment WHERE work_id=? AND concept_digest=? AND rowid>'
                '(SELECT rowid FROM assignment WHERE assignment_id=?)', (work_id, row['concept_digest'], row['assignment_id'])):
            if _scope(later).get('di_task_id') == task['id']:
                return False
        evidence = proof['evidence']
        milestone = next(m for m in package['di_plan']['milestones'] if m['id'] == task['milestone'])
        if set(evidence) != set(milestone['verification']):
            return False
        if task['kind'] == 'implementation':
            return (row['disposition'] == 'assessment_required' and evidence['candidate-frozen'] ==
                    {'candidate': result['candidate'], 'reference': result['output']})
        checks = {c['name']: {'reference': c['output'], 'candidate': c['candidate']} for c in result['checks']
                  if c.get('result') == 'pass' and c.get('candidate') == result['candidate'] and c.get('output')}
        checks['independent-assessment'] = result['initial_reports']
        return row['disposition'] in ('milestone_passed', 'delivered') and evidence == checks
    except (KeyError, TypeError, ValueError, StopIteration):
        return False


def bind_scope_in(db, work_id, kind, base_scope, *, task_id=None):
    package = package_in(db, work_id)
    if not _serial(package):
        return base_scope
    tasks = execution_tasks(package['di_plan'])
    if task_id is None:
        latest = db.execute('SELECT * FROM assignment WHERE work_id=? AND concept_digest=? ORDER BY rowid DESC LIMIT 1',
                            (work_id, digest(package))).fetchone()
        if latest:
            prior = task_in(db, latest)
            index = next(i for i, t in enumerate(tasks) if t['id'] == prior['id'])
            if kind == 'repair':
                index -= int(prior['kind'] == 'assessment')
            elif kind == 'assessment' and prior['kind'] == 'implementation':
                index += 1
            task_id = tasks[index]['id']
        else:
            # Technical continuation inherits the exact applicable freeze.
            proofs = _proofs_in(db, work_id, digest(package))
            frozen = [i for i, t in enumerate(tasks) if t['kind'] == 'implementation' and t['milestone'] in proofs]
            task_id = tasks[max(frozen) + 1]['id'] if kind == 'assessment' and frozen else tasks[0]['id']
    task = next((t for t in tasks if t['id'] == task_id), None)
    phase = 'assessment' if kind == 'assessment' else 'implementation'
    if not task or task['kind'] != phase:
        raise Rejected('di_task_binding', 'Successor must match its exact approved task phase')
    milestone = next(m for m in package['di_plan']['milestones'] if m['id'] == task['milestone'])
    proofs = _proofs_in(db, work_id, digest(package))
    if any(dep not in proofs for dep in milestone['depends_on']):
        raise Rejected('di_milestone_failed', 'Successor requires the preceding verified milestone')
    return dict(base_scope, di_task_id=task['id'], di_milestone_id=task['milestone'],
        di_plan_digest=digest(package['di_plan']), di_dependencies={dep: digest(proofs[dep]) for dep in milestone['depends_on']})


def expected_scope_in(db, assignment, base_scope):
    package = package_in(db, assignment['work_id'])
    if not _serial(package):
        return base_scope
    task_in(db, assignment)
    scope = _scope(assignment)
    expected = dict(base_scope, **{k: scope.get(k) for k in
        ('di_task_id', 'di_milestone_id', 'di_plan_digest', 'di_dependencies')})
    event = db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_assignment_planned' "
                       "AND json_extract(detail,'$.assignment_id')=? ORDER BY event_id DESC LIMIT 1",
                       (assignment['work_id'], assignment['assignment_id'])).fetchone()
    if not event or json.loads(event[0])['scope_digest'] != digest(expected):
        raise Rejected('di_task_binding', 'Saved task scope differs from its immutable dispatch binding')
    return expected


def package_in(db, work_id):
    row = db.execute('SELECT c.package,c.digest FROM concept c JOIN work w ON w.work_id=c.work_id '
                     'AND w.current_concept=c.revision WHERE w.work_id=?', (work_id,)).fetchone()
    if not row:
        raise Rejected('invalid_di_plan', 'Current approved plan is unavailable')
    package = json.loads(row[0])
    if digest(package) != row[1]:
        raise Rejected('invalid_di_plan', 'Current plan/package bytes differ from their approved digest')
    validate_execution_plan(package.get('di_plan'), package['team_envelope'], package.get('execution_envelope', {}))
    return package


def progress_in(db, work_id, package):
    plan = package['di_plan']
    progress = {'tasks': {}, 'milestones': {}}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_event'").fetchone():
        return progress
    for row in db.execute('SELECT kind,detail,created_at FROM lifecycle_event WHERE work_id=? ORDER BY event_id', (work_id,)):
        detail = json.loads(row['detail'])
        if row['kind'] not in {'di_milestone_passed', 'di_forecast_recorded'} or detail.get('concept_digest') != digest(package):
            continue
        if row['kind'] == 'di_forecast_recorded':
            record = progress['tasks'].setdefault(detail['task_id'], {})
            record.setdefault('revised_forecasts', []).append(deepcopy(detail))
            if detail.get('actuals') is not None:
                record['actuals'] = detail['actuals']
            continue
        if _serial(package) and not _verified_proof_in(db, work_id, package, detail):
            continue
        for task_id in detail['task_ids']:
            progress['tasks'].setdefault(task_id, {}).update(status='completed', observed_at=row['created_at'],
                evidence=detail['evidence'], candidate=detail['candidate'])
        progress['milestones'][detail['milestone_id']] = {'status': 'passed', 'observed_at': row['created_at'],
            'candidate': detail['candidate'], 'evidence': detail['evidence']}
    latest = {}
    for row in db.execute('SELECT * FROM assignment WHERE work_id=? AND concept_digest=? ORDER BY created_at,assignment_id',
                          (work_id, digest(package))):
        key = _scope(row).get('di_task_id') if _serial(package) else ('assessment' if row['kind'] == 'assessment' else 'implementation')
        latest[key] = row
    for kind, assignment in latest.items():
        if assignment['state'] == 'completed':
            continue
        status = {'failed': 'failed', 'cancelled': 'held', 'observed_active': 'active',
                  'sent': 'waiting', 'acknowledged': 'waiting', 'proposed': 'ready'}.get(assignment['state'], 'held')
        for task in plan['tasks']:
            if (task['id'] if _serial(package) else task['kind']) == kind:
                progress['tasks'].setdefault(task['id'], {}).update(status=status,
                    observed_at=assignment['terminal_at'] or assignment['observed_active_at'] or assignment['created_at'],
                    wait_reason='Assignment ' + assignment['state'])
                progress['milestones'][task['milestone']] = {'status': 'failed' if status == 'failed' else 'held',
                    'wait_reason': 'Awaiting passing evidence from ' + assignment['assignment_id']}
    return progress


def phase_errors_in(db, assignment):
    try:
        package = package_in(db, assignment['work_id'])
        if _serial(package):
            task = task_in(db, assignment)
            event = db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_assignment_planned' "
                "AND json_extract(detail,'$.assignment_id')=? ORDER BY event_id DESC LIMIT 1",
                (assignment['work_id'], assignment['assignment_id'])).fetchone()
            if not event or json.loads(event[0]).get('scope_digest') != digest(_scope(assignment)):
                return ['DI assignment differs from its immutable task dispatch binding']
            milestone = next(m for m in package['di_plan']['milestones'] if m['id'] == task['milestone'])
            scope = _scope(assignment)
            proofs = _proofs_in(db, assignment['work_id'], digest(package))
            dependencies = scope.get('di_dependencies')
            if not isinstance(dependencies, dict) or set(dependencies) != set(milestone['depends_on']):
                return ['DI assignment lacks its exact prerequisite milestone bindings']
            for dep in milestone['depends_on']:
                proof = proofs.get(dep, {})
                if digest(proof) != dependencies[dep] or not _verified_proof_in(db, assignment['work_id'], package, proof):
                    return ['DI milestone ' + dep + ' requires its qualified completion receipt and exact candidate evidence']
                if task['kind'] == 'assessment' or assignment['kind'] == 'implementation':
                    candidate = json.loads(assignment['candidate']) if isinstance(assignment['candidate'], str) else assignment['candidate']
                    if proof['candidate'] != candidate:
                        return ['DI milestone ' + dep + ' does not match the assigned candidate baseline']
            return []
        if assignment['kind'] != 'assessment':
            return []
        progress = progress_in(db, assignment['work_id'], package)
        milestone = next(m for m in package['di_plan']['milestones']
                         if any(t['kind'] == 'assessment' and t['milestone'] == m['id'] for t in package['di_plan']['tasks']))
        candidate = json.loads(assignment['candidate']) if isinstance(assignment['candidate'], str) else assignment['candidate']
        for dep in milestone['depends_on']:
            proof = progress['milestones'].get(dep, {})
            if proof.get('status') != 'passed' or proof.get('candidate') != candidate:
                return ['DI milestone ' + dep + ' requires verified evidence for the current frozen candidate']
        return []
    except (Rejected, KeyError, StopIteration, TypeError, ValueError) as error:
        return ['DI plan holds dispatch: ' + str(error)]


def advance_in(lifecycle, db, assignment, result, receipt_id=None):
    """Called only at qualified completion; verify each declared milestone check."""
    package = package_in(db, assignment['work_id'])
    plan = package['di_plan']
    kind = 'assessment' if assignment['kind'] == 'assessment' else 'implementation'
    tasks = [task_in(db, assignment)] if _serial(package) else [t for t in plan['tasks'] if t['kind'] == kind]
    milestone = next(m for m in plan['milestones'] if m['id'] == tasks[0]['milestone'])
    evidence = {}
    if kind == 'implementation':
        candidate = result.get('candidate')
        if (not isinstance(candidate, dict) or not all(candidate.get(k) for k in ('identity', 'source_identity', 'inventory'))
                or result.get('owned_processes') != [] or result.get('effects_reconciled') is not True
                or not result.get('output')):
            raise Rejected('di_milestone_failed', 'candidate-frozen requires exact candidate, factual output and reconciled effects')
        evidence['candidate-frozen'] = {'candidate': candidate, 'reference': result['output']}
    else:
        errors = phase_errors_in(db, assignment)
        if errors:
            raise Rejected('di_milestone_failed', '; '.join(errors))
        candidate = result.get('candidate')
        error = lifecycle._assessment_error(json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?',
                                            (assignment['work_id'],)).fetchone()[0]), assignment, result)
        if error:
            raise Rejected('di_milestone_failed', error)
        for check in result.get('checks', []):
            if check.get('result') == 'pass' and check.get('output') and check.get('candidate') == candidate:
                evidence[check['name']] = {'reference': check['output'], 'candidate': candidate}
        evidence['independent-assessment'] = deepcopy(result['initial_reports'])
    missing = set(milestone['verification']) - evidence.keys()
    if missing:
        raise Rejected('di_milestone_failed', 'Missing passing milestone evidence: ' + ', '.join(sorted(missing)))
    lifecycle._touch(db, assignment['work_id'], 'di_milestone_passed', {
        'concept_digest': digest(package), 'milestone_id': milestone['id'], 'task_ids': [t['id'] for t in tasks],
        'assignment_id': assignment['assignment_id'], 'candidate': candidate, 'evidence': evidence,
        **({'receipt_id': receipt_id} if _serial(package) else {})})


def next_task_in(db, assignment):
    package = package_in(db, assignment['work_id'])
    if not _serial(package):
        return None
    tasks = execution_tasks(package['di_plan'])
    index = next(i for i, t in enumerate(tasks) if t['id'] == task_in(db, assignment)['id'])
    return tasks[index + 1] if index + 1 < len(tasks) else None


def inherit_frozen_candidate_in(lifecycle, db, work_id, previous_digest, candidate):
    """Called inside a validated technical-continuation publication transaction.

    A new approval identity cannot erase the exact prior candidate-freeze proof.
    Missing proof, changed plans or changed candidates remain held; this creates
    no approval and never reconstructs evidence from a completed status alone.
    """
    current = package_in(db, work_id)
    row = db.execute('SELECT package FROM concept WHERE work_id=? AND digest=?',
                     (work_id, previous_digest)).fetchone()
    if not row:
        raise Rejected('di_milestone_failed', 'Prior candidate-freeze package is missing')
    previous = json.loads(row[0])
    if digest(previous) != previous_digest or previous.get('di_plan') != current['di_plan']:
        raise Rejected('di_milestone_failed', 'Technical continuation must preserve the exact DI plan')
    if _serial(current):
        proofs = _proofs_in(db, work_id, previous_digest)
        tasks = execution_tasks(current['di_plan'])
        frozen = [i for i, task in enumerate(tasks) if task['kind'] == 'implementation'
                  and proofs.get(task['milestone'], {}).get('candidate') == candidate]
        if not frozen:
            raise Rejected('di_milestone_failed', 'Continuation requires the exact staged candidate-freeze proof')
        for task in tasks[:max(frozen) + 1]:
            proof = proofs.get(task['milestone'], {})
            if not _verified_proof_in(db, work_id, previous, proof):
                raise Rejected('di_milestone_failed', 'Continuation requires every qualified upstream milestone receipt')
            detail = deepcopy(proof)
            detail.update(concept_digest=digest(current), inherited_from_digest=previous_digest,
                          inherited_proof_digest=digest(proof),
                          inheritance_reason='Validated technical continuation preserves the exact plan and candidate')
            lifecycle._touch(db, work_id, 'di_milestone_passed', detail)
        for row in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_forecast_recorded' ORDER BY event_id",
                              (work_id,)).fetchall():
            observation = json.loads(row[0])
            if observation.get('concept_digest') == previous_digest:
                detail = deepcopy(observation)
                detail.update(concept_digest=digest(current), inherited_from_digest=previous_digest,
                              inherited_observation_digest=digest(observation))
                lifecycle._touch(db, work_id, 'di_forecast_recorded', detail)
        return
    milestone = next(t['milestone'] for t in current['di_plan']['tasks'] if t['kind'] == 'implementation')
    for row in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='di_milestone_passed' ORDER BY event_id DESC", (work_id,)):
        proof = json.loads(row[0])
        if (proof.get('concept_digest') == previous_digest and proof.get('milestone_id') == milestone
                and proof.get('candidate') == candidate
                and proof.get('evidence', {}).get('candidate-frozen', {}).get('candidate') == candidate):
            detail = deepcopy(proof)
            detail.update(concept_digest=digest(current), inherited_from_digest=previous_digest,
                          inheritance_reason='Validated technical continuation preserves the exact plan and candidate')
            lifecycle._touch(db, work_id, 'di_milestone_passed', detail)
            return
    raise Rejected('di_milestone_failed', 'Technical continuation needs prior verified candidate-freeze evidence')


def record_forecast(lifecycle, operation_id, work_id, expected_work_revision, digest, task_id,
                    reason, forecast, actuals):
    """Trusted host observations never rewrite originals or enlarge hard limits."""
    import math
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
        raise Rejected('di_forecast', 'A reason for the revised forecast is required')
    try:
        Estimate.model_validate(forecast, strict=True)
    except ValidationError as error:
        raise Rejected('di_forecast', str(error)) from error
    keys = {'active_hours', 'human_wait_hours', 'technical_wait_hours', 'monetary'}
    if (not isinstance(actuals, dict) or set(actuals) != keys
            or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
                   for k, v in actuals.items() if k != 'monetary') or actuals.get('monetary') is not None):
        raise Rejected('di_forecast', 'Actuals require nonnegative hours or null and monetary:null (billing uncollected)')
    payload = ['di-forecast', work_id, expected_work_revision, digest, task_id, reason, forecast, actuals]
    with lifecycle.store._transaction() as db:
        if prior := lifecycle.store._prior(db, operation_id, payload):
            return prior
        lifecycle._command_guard(db, work_id, expected_work_revision, digest)
        package = package_in(db, work_id)
        task = next((t for t in package['di_plan']['tasks'] if t['id'] == task_id), None)
        if task is None:
            raise Rejected('di_forecast', 'Unknown task identifier: ' + task_id)
        growth = any(forecast[key]['high'] > task['original_estimate'][key]['high']
                     for key in ('effort_hours', 'elapsed_hours'))
        detail = {'concept_digest': digest, 'task_id': task_id, 'reason': reason,
                  'forecast': forecast, 'actuals': actuals, 'outside_original_range': growth}
        lifecycle._touch(db, work_id, 'di_forecast_recorded', detail)
        return lifecycle.store._record(db, operation_id, payload, {'recorded': True, 'outside_original_range': growth,
            'limits_changed': False})
