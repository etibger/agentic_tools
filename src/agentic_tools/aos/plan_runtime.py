"""Evidence-backed milestones for the existing writer/assessor adapter.

Events are append-only and bound to the concept, candidate and assignment. They
are not coordinator assertions of task success. The lifecycle produces them only
after its normal candidate/effect and independent-assessment checks succeed.
"""
from __future__ import annotations

import json
from copy import deepcopy

from .planning import validate_execution_plan
from .store import Rejected, digest
from .planning import Estimate
from pydantic import ValidationError


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
        for task_id in detail['task_ids']:
            progress['tasks'].setdefault(task_id, {}).update(status='completed', observed_at=row['created_at'],
                evidence=detail['evidence'], candidate=detail['candidate'])
        progress['milestones'][detail['milestone_id']] = {'status': 'passed', 'observed_at': row['created_at'],
            'candidate': detail['candidate'], 'evidence': detail['evidence']}
    latest = {}
    for row in db.execute('SELECT * FROM assignment WHERE work_id=? AND concept_digest=? ORDER BY created_at,assignment_id',
                          (work_id, digest(package))):
        latest['assessment' if row['kind'] == 'assessment' else 'implementation'] = row
    for kind, assignment in latest.items():
        if assignment['state'] == 'completed':
            continue
        status = {'failed': 'failed', 'cancelled': 'held', 'observed_active': 'active',
                  'sent': 'waiting', 'acknowledged': 'waiting', 'proposed': 'ready'}.get(assignment['state'], 'held')
        for task in plan['tasks']:
            if task['kind'] == kind:
                progress['tasks'].setdefault(task['id'], {}).update(status=status,
                    observed_at=assignment['terminal_at'] or assignment['observed_active_at'] or assignment['created_at'],
                    wait_reason='Assignment ' + assignment['state'])
                progress['milestones'][task['milestone']] = {'status': 'failed' if status == 'failed' else 'held',
                    'wait_reason': 'Awaiting passing evidence from ' + assignment['assignment_id']}
    return progress


def phase_errors_in(db, assignment):
    try:
        package = package_in(db, assignment['work_id'])
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


def advance_in(lifecycle, db, assignment, result):
    """Called only at qualified completion; verify each declared milestone check."""
    package = package_in(db, assignment['work_id'])
    plan = package['di_plan']
    kind = 'assessment' if assignment['kind'] == 'assessment' else 'implementation'
    tasks = [t for t in plan['tasks'] if t['kind'] == kind]
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
        'assignment_id': assignment['assignment_id'], 'candidate': candidate, 'evidence': evidence})


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
