"""Versioned DI planning inputs and their read-only dashboard projection.

The approval package owns original estimates and dependency semantics. Runtime
observations, revised forecasts and actuals belong to the separate progress
record; rendering never creates evidence or changes an approved forecast.
"""
from __future__ import annotations

from copy import deepcopy
from html import escape
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator


Text = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r'[\s\S]*\S[\s\S]*')]
Identity = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
NodeIdentity = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9-]{0,127}$')]
Texts = Annotated[list[Text], Field(min_length=1, max_length=128)]
Identities = Annotated[list[Identity], Field(max_length=128)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class PlanModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class EstimateRange(PlanModel):
    low: Nonnegative
    base: Nonnegative
    high: Nonnegative

    @model_validator(mode='after')
    def ordered(self):
        if not self.low <= self.base <= self.high:
            raise ValueError('Estimate range must satisfy 0 <= low <= base <= high')
        return self


class MonetaryEstimate(PlanModel):
    value: EstimateRange | None
    currency: Annotated[str, StringConstraints(pattern=r'^[A-Z]{3}$')] | None
    basis: Text

    @model_validator(mode='after')
    def known_currency(self):
        if (self.value is None) != (self.currency is None):
            raise ValueError('Unknown money needs explicit null value and currency; known money needs both')
        return self


class MonetaryEstimates(PlanModel):
    agent_api: MonetaryEstimate
    infrastructure: MonetaryEstimate


class Estimate(PlanModel):
    effort_hours: EstimateRange
    elapsed_hours: EstimateRange
    assumptions: Texts
    confidence: Literal['low', 'medium', 'high']
    monetary: MonetaryEstimates
    exclusions: Texts
    risks: Texts


class PlanTask(PlanModel):
    id: NodeIdentity
    title: Text
    owner: Text
    kind: Literal['implementation', 'assessment']
    outputs: Texts
    depends_on: Identities
    definition_of_done: Texts
    milestone: Identity
    original_estimate: Estimate


class PlanMilestone(PlanModel):
    id: NodeIdentity
    title: Text
    depends_on: Identities
    task_ids: Annotated[list[Identity], Field(min_length=1, max_length=128)]
    verification: Annotated[list[Identity], Field(min_length=1, max_length=128)]


def _unique(values, description):
    if len(values) != len(set(values)):
        raise ValueError(description + ' must be unique')


def _graph(tasks, milestones):
    """Include implicit milestone-entry edges so cross-stage cycles reject too."""
    graph = {m.id: list(m.depends_on) + list(m.task_ids) for m in milestones.values()}
    for task in tasks.values():
        graph[task.id] = list(dict.fromkeys(task.depends_on + milestones[task.milestone].depends_on))
    return graph


def _depths(graph):
    # Iterative traversal remains bounded even at the largest accepted plan.
    depths = {}
    remaining = set(graph)
    while remaining:
        ready = sorted(key for key in remaining if all(dep in depths for dep in graph[key]))
        if not ready:
            raise ValueError('Dependency cycle includes: ' + ', '.join(sorted(remaining)))
        for key in ready:
            depths[key] = max((depths[dep] + 1 for dep in graph[key]), default=0)
        remaining.difference_update(ready)
    return depths


class DIPlan(PlanModel):
    format: Literal['aos-di-plan-v1']
    execution_profile: Literal['serial-milestones-v1'] | None = None
    tasks: Annotated[list[PlanTask], Field(min_length=1, max_length=128)]
    milestones: Annotated[list[PlanMilestone], Field(min_length=1, max_length=128)]
    replan_triggers: Texts

    @model_validator(mode='after')
    def dependencies(self):
        _unique([task.id for task in self.tasks] + [m.id for m in self.milestones],
                'Task and milestone identifiers')
        tasks = {task.id: task for task in self.tasks}
        milestones = {milestone.id: milestone for milestone in self.milestones}
        if {task.kind for task in self.tasks} != {'implementation', 'assessment'}:
            raise ValueError('Plan needs implementation and independent assessment tasks')
        coverage = []
        for task in self.tasks:
            _unique(task.depends_on, 'Task ' + task.id + ' dependencies')
            unknown = set(task.depends_on) - tasks.keys()
            if unknown:
                raise ValueError('Task ' + task.id + ' has unknown task dependencies: ' + ', '.join(sorted(unknown)))
            if task.milestone not in milestones:
                raise ValueError('Task ' + task.id + ' names unknown milestone ' + task.milestone)
        for milestone in self.milestones:
            _unique(milestone.depends_on, 'Milestone ' + milestone.id + ' dependencies')
            _unique(milestone.task_ids, 'Milestone ' + milestone.id + ' tasks')
            _unique(milestone.verification, 'Milestone ' + milestone.id + ' verification checks')
            unknown = set(milestone.depends_on) - milestones.keys()
            if unknown:
                raise ValueError('Milestone ' + milestone.id + ' has unknown milestone dependencies: ' + ', '.join(sorted(unknown)))
            for key in milestone.task_ids:
                if key not in tasks or tasks[key].milestone != milestone.id:
                    raise ValueError('Milestone ' + milestone.id + ' task membership disagrees with task ' + key)
            coverage.extend(milestone.task_ids)
        if sorted(coverage) != sorted(tasks):
            raise ValueError('Milestones must cover every task exactly once')
        _depths(_graph(tasks, milestones))
        for task in self.tasks:
            ancestors = set()
            pending = list(milestones[task.milestone].depends_on)
            while pending:
                prior = pending.pop()
                if prior not in ancestors:
                    ancestors.add(prior)
                    pending.extend(milestones[prior].depends_on)
            for key in task.depends_on:
                predecessor = tasks[key]
                if predecessor.milestone != task.milestone and predecessor.milestone not in ancestors:
                    raise ValueError('Task ' + task.id + ' dependency ' + key
                                     + ' requires its milestone in the transitive milestone prerequisites')
                if (self.execution_profile != 'serial-milestones-v1'
                        and task.kind == 'implementation' and predecessor.kind == 'assessment'):
                    raise ValueError('Implementation task ' + task.id + ' cannot depend on assessment task ' + key)
            if self.execution_profile != 'serial-milestones-v1' and task.kind == 'implementation' and any(
                    other.kind == 'assessment' and other.milestone in ancestors for other in self.tasks):
                raise ValueError('Implementation task ' + task.id + ' cannot follow an assessment milestone')
        if self.execution_profile == 'serial-milestones-v1':
            if len(self.tasks) not in (2, 4, 6) or len(self.milestones) != len(self.tasks):
                raise ValueError('serial-milestones-v1 requires exactly 2, 4 or 6 tasks and '
                                 'the same number of milestones, one task per milestone')
            for index, (task, milestone) in enumerate(zip(self.tasks, self.milestones)):
                kind = 'implementation' if index % 2 == 0 else 'assessment'
                if task.kind != kind:
                    raise ValueError('serial-milestones-v1 tasks must alternate implementation and '
                                     'assessment in list order; task ' + task.id + ' must be ' + kind)
                if task.milestone != milestone.id or milestone.task_ids != [task.id]:
                    raise ValueError('serial-milestones-v1 requires exactly one task per milestone '
                                     'in matching list order; task ' + task.id + ' and milestone ' + milestone.id)
                previous_task = [self.tasks[index - 1].id] if index else []
                previous_milestone = [self.milestones[index - 1].id] if index else []
                if task.depends_on != previous_task or milestone.depends_on != previous_milestone:
                    raise ValueError('serial-milestones-v1 supports a serial chain only: task ' + task.id
                                     + ' dependencies must be ' + str(previous_task) + ' and milestone '
                                     + milestone.id + ' dependencies must be ' + str(previous_milestone)
                                     + '; parallel graphs and skipped checkpoints are unsupported')
        return self


def validate_plan(value, team=None) -> dict:
    """Validate without normalization; the exact caller input stays digest-bound."""
    from .store import Rejected

    try:
        plan = DIPlan.model_validate(value, strict=True)
        if team is not None:
            members = {member.get('name') for member in team if isinstance(member, dict)}
            unknown = sorted({task.owner for task in plan.tasks} - members)
            if unknown:
                raise ValueError('Task owners absent from the approved team: ' + ', '.join(unknown))
    except ValidationError as error:
        details = ['.'.join(map(str, item['loc'])) + ': ' + item['msg'] for item in error.errors()]
        raise Rejected('invalid_di_plan', '; '.join(details)) from error
    except ValueError as error:
        raise Rejected('invalid_di_plan', str(error)) from error
    return deepcopy(value)


def execution_tasks(plan: dict) -> list[dict]:
    """Return the validated execution order without changing digest-bound input."""
    validated = validate_plan(plan)
    if validated.get('execution_profile') == 'serial-milestones-v1':
        return validated['tasks']
    return sorted(validated['tasks'], key=lambda task: task['kind'] == 'assessment')


def validate_execution_plan(plan, team, envelope) -> dict:
    """Bind an aggregate or explicit serial plan to its approved lifecycle."""
    from .store import Rejected

    validated = validate_plan(plan, team)
    parsed = DIPlan.model_validate(plan, strict=True)
    serial = parsed.execution_profile == 'serial-milestones-v1'
    if not serial and len(parsed.tasks) != 2:
        raise Rejected('invalid_di_plan', 'This adapter requires one aggregate task per execution phase. '
                       'Split additional task gates into separately approved work; bulk completion is not task evidence')
    if not isinstance(envelope, dict):
        raise Rejected('invalid_di_plan', 'Plan needs the exact approved execution envelope')
    milestones = {milestone.id: milestone for milestone in parsed.milestones}
    if serial:
        assignments = envelope.get('max_assignments')
        if type(assignments) is not int or assignments < len(parsed.tasks):
            raise Rejected('invalid_di_plan', 'serial-milestones-v1 needs max_assignments of at least '
                           + str(len(parsed.tasks)) + ' to execute every planned task; approved limits are not widened')
    else:
        milestone_ids = {kind: {task.milestone for task in parsed.tasks if task.kind == kind}
                         for kind in ('implementation', 'assessment')}
        if (len(parsed.milestones) != 2 or any(len(ids) != 1 for ids in milestone_ids.values())
                or milestone_ids['implementation'] == milestone_ids['assessment']):
            raise Rejected('invalid_di_plan', 'This execution adapter requires exactly two milestones: '
                           'implementation and independent assessment. Split extra milestones into '
                           'separately approved work; decorative gates cannot enforce progression')
        implementation_id = next(iter(milestone_ids['implementation']))
        assessment_id = next(iter(milestone_ids['assessment']))
        implementation = milestones[implementation_id]
        assessment = milestones[assessment_id]
        if implementation.depends_on or assessment.depends_on != [implementation_id]:
            raise Rejected('invalid_di_plan', 'Implementation must have no milestone prerequisites; '
                           'independent assessment must depend on the implementation milestone')
    writer, assessor = envelope.get('writer'), envelope.get('assessor')
    if not writer or not assessor or writer == assessor:
        raise Rejected('invalid_di_plan', 'Distinct execution-envelope writer and assessor required')
    for task in parsed.tasks:
        owner = writer if task.kind == 'implementation' else assessor
        if task.owner != owner:
            raise Rejected('invalid_di_plan', 'Task ' + task.id + ' owner must be the execution-envelope '
                           + ('writer' if task.kind == 'implementation' else 'assessor') + ': ' + str(owner))
    required = envelope.get('required_checks')
    compatibility = envelope.get('compatibility_checks')
    if (not isinstance(required, list) or not required or not isinstance(compatibility, list)
            or any(not isinstance(check, str) or not check.strip() for check in required + compatibility)):
        raise Rejected('invalid_di_plan', 'Execution-envelope required and compatibility checks must be explicit')
    checks = set(required + compatibility + ['independent-assessment'])
    for task in parsed.tasks:
        milestone = milestones[task.milestone]
        if task.kind == 'implementation' and milestone.verification != ['candidate-frozen']:
            raise Rejected('invalid_di_plan', 'Implementation verification must be exactly candidate-frozen')
        if task.kind == 'assessment' and set(milestone.verification) != checks:
            raise Rejected('invalid_di_plan', 'Assessment verification must contain exactly the required checks, '
                           'compatibility checks and independent-assessment: ' + ', '.join(sorted(checks)))
    return validated


def approval_summary_html(plan: dict, envelope: dict) -> str:
    """Render readable original forecasts beside, and separate from, hard limits."""
    validate_plan(plan)
    parsed = DIPlan.model_validate(plan, strict=True)

    def text(value):
        return escape(str(value))

    def items(values):
        return '<ul>' + ''.join('<li>' + text(value) + '</li>' for value in values) + '</ul>'

    def estimate_range(value):
        return f'{value.low:g} / {value.base:g} / {value.high:g}'

    parts = ['<section aria-label="DI plan and estimates"><h2>DI plan and original estimates</h2>',
             '<p>Ranges show low / base / high. Active agent effort and elapsed time are separate. '
             'Elapsed time includes the dependencies and waiting assumptions stated for each task. '
             'These forecasts do not grant additional execution capacity.</p>']
    if parsed.execution_profile == 'serial-milestones-v1':
        parts.append('<p>Serial checkpoints run in the listed task order. Each assessment runs all approved '
                     'checks before the next implementation task; only the final assessment accepts delivery.</p>')
    for task in parsed.tasks:
        estimate = task.original_estimate
        parts.extend([
            '<article><h3>' + text(task.id) + ' — ' + text(task.title) + '</h3>',
            '<p><strong>Owner:</strong> ' + text(task.owner) + '; <strong>kind:</strong> ' + text(task.kind)
            + '; <strong>milestone:</strong> ' + text(task.milestone) + '</p>',
            '<p><strong>Task dependencies:</strong> ' + text(', '.join(task.depends_on) or 'None') + '</p>',
            '<p><strong>Concrete outputs:</strong></p>' + items(task.outputs),
            '<p><strong>Definition of done:</strong></p>' + items(task.definition_of_done),
            '<p><strong>Original active agent effort (hours):</strong> ' + estimate_range(estimate.effort_hours)
            + '; <strong>original elapsed time (hours):</strong> ' + estimate_range(estimate.elapsed_hours)
            + '; <strong>confidence:</strong> ' + text(estimate.confidence) + '</p>',
        ])
        for title, money in [('Agent/API cost', estimate.monetary.agent_api),
                             ('Infrastructure cost', estimate.monetary.infrastructure)]:
            value = ('Unknown' if money.value is None else
                     estimate_range(money.value) + ' ' + text(money.currency))
            parts.append('<p><strong>' + title + ':</strong> ' + value
                         + '. <strong>Basis / unavailable figures:</strong> ' + text(money.basis) + '</p>')
        for title, values in [('Assumptions and waits', estimate.assumptions),
                              ('Delivery and cost risks', estimate.risks), ('Exclusions', estimate.exclusions)]:
            parts.append('<p><strong>' + title + ':</strong></p>' + items(values))
        parts.append('</article>')
    parts.append('<h3>Milestone evidence required before progression</h3>')
    for milestone in parsed.milestones:
        parts.append('<p><strong>' + text(milestone.id) + ' — ' + text(milestone.title) + ':</strong> tasks '
                     + text(', '.join(milestone.task_ids)) + '; prerequisite milestones '
                     + text(', '.join(milestone.depends_on) or 'None') + '; required verification '
                     + text(', '.join(milestone.verification)) + '.</p>')
    parts.extend([
        '<h3>Approved execution limits, separate from forecasts</h3>',
        '<p>Maximum assignments: ' + text(envelope.get('max_assignments', 'Unknown'))
        + '; maximum seconds per assignment: ' + text(envelope.get('max_seconds', 'Unknown'))
        + '; maximum repairs: ' + text(envelope.get('max_repairs', 'Unknown'))
        + '. Existing execution checks enforce these limits. Monetary forecasts do not establish a currency cap.</p>',
        '<h3>Stop and replan triggers</h3>' + items(parsed.replan_triggers),
        '<p>The exact package preserves these original estimates. Revised forecasts require a recorded reason; '
        'actual observations remain separate. A material scope or authority change needs a new decision.</p></section>',
    ])
    return ''.join(parts)


def project_plan(plan: dict, progress: dict, observed_at: str) -> list[dict]:
    """Project the authoritative graph, keeping original and observed facts apart."""
    validate_plan(plan)
    parsed = DIPlan.model_validate(plan, strict=True)
    tasks = {task.id: task for task in parsed.tasks}
    milestones = {m.id: m for m in parsed.milestones}
    graph = _graph(tasks, milestones)
    depths = _depths(graph)
    records = {**progress.get('tasks', {}), **progress.get('milestones', {})}
    status_map = {'completed': 'accepted', 'passed': 'accepted'}
    accepted = {key for key, record in records.items()
                if record.get('status') in {'completed', 'passed', 'accepted'}}
    statuses = {'accepted', 'active', 'ready', 'review', 'correction', 'waiting', 'held', 'failed', 'pending'}
    nodes = []
    rows = {}
    for item in [*parsed.tasks, *parsed.milestones]:
        is_task = item.id in tasks
        record = records.get(item.id, {})
        deps = graph[item.id]
        missing = [key for key in deps if key not in accepted]
        status = record.get('status', 'waiting' if missing else 'ready')
        status = status_map.get(status, status)
        if status not in statuses:
            status = 'held'
        column = depths[item.id]
        row = rows.get(column, 0)
        rows[column] = row + 1
        summary = ('Outputs: ' + '; '.join(item.outputs) + '. Done when: ' + '; '.join(item.definition_of_done)
                   if is_task else 'Verification required: ' + ', '.join(item.verification))
        node = {'id': item.id, 'title': item.title, 'owner': item.owner if is_task else 'coordinator',
                'kind': 'action' if is_task else 'decision', 'status': status,
                'summary': summary, 'depends_on': deps, 'column': column, 'row': row,
                'wait_reason': record.get('wait_reason') or ('Waiting for: ' + ', '.join(missing) if missing
                                                            else 'Evidence recorded' if status == 'accepted'
                                                            else 'Awaiting assignment or stated verification'),
                'observed_at': record.get('observed_at') or observed_at}
        if is_task:
            original = next(task for task in plan['tasks'] if task['id'] == item.id)
            node['original_estimate'] = deepcopy(original['original_estimate'])
        for name in ('revised_forecasts', 'actuals'):
            if name in record:
                node[name] = deepcopy(record[name])
        if record.get('evidence'):
            node['verification_evidence'] = deepcopy(record['evidence'])
        nodes.append(node)
    return nodes
