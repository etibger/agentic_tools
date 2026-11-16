"""Disposable serial checkpoints enforce each planned task before delivery."""
from copy import deepcopy
import json

import pytest

from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos import plan_runtime
from agentic_tools.aos.planning import execution_tasks, validate_execution_plan
from agentic_tools.aos.projection import collect, dashboard
from agentic_tools.aos.store import Rejected, Store, digest
from agentic_tools.aos.worker import Worker
from di_plan_fixture import synthetic_plan
from test_aos_di_plan import present, proposal
from test_aos_lifecycle import (CANDIDATE, approve, assessment, contract, implementation,
                                run_to_end, runtime)
from test_aos_store import TEAM


def serial_plan(bounds, count=4):
    plan = synthetic_plan(bounds)
    templates = deepcopy(plan)
    plan.update(execution_profile='serial-milestones-v1', tasks=[], milestones=[])
    for index in range(count):
        task = deepcopy(templates['tasks'][index % 2])
        milestone = deepcopy(templates['milestones'][index % 2])
        task.update(id=f'task-{index}', title=f'Synthetic serial task {index}',
                    milestone=f'gate-{index}', depends_on=[f'task-{index - 1}'] if index else [])
        milestone.update(id=f'gate-{index}', task_ids=[task['id']],
                         depends_on=[f'gate-{index - 1}'] if index else [])
        plan['tasks'].append(task)
        plan['milestones'].append(milestone)
    return plan


def serial_setup(tmp_path, *, count=4):
    store = Store(tmp_path / 'state', (tmp_path / 'fixture',))
    store.introduce('intro', 'Q1', 'Disposable serial milestone work', ['fixture/'], TEAM)
    bounds = contract({'digest': 'not-self-referential'})
    bounds.pop('concept_digest')
    bounds.pop('concept_revision')
    team = [{**TEAM[0], 'name': name} for name in ('writer', 'assessor')]
    current = store.present('present', 'Q1', 1, 'Q1-DI', 1, 'Synthetic serial work',
                            ['Every checkpoint and final assessment pass'], team, bounds['authority'],
                            execution_envelope=bounds, di_plan=serial_plan(bounds, count))
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'Q1', contract(current))
    approve(store, current)
    lifecycle.admit('admit', 'Q1')
    return store, lifecycle, current


def scripted_four(*, second_candidate=CANDIDATE, final_polls=0):
    return [{'kind': 'implementation', 'result': implementation()},
            {'kind': 'assessment', 'result': assessment()},
            {'kind': 'implementation', 'result': implementation(second_candidate)},
            {'kind': 'assessment', 'result': assessment(candidate=second_candidate), 'polls': final_polls}]


def milestone_events(store):
    with store._connection() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT detail FROM lifecycle_event WHERE kind='di_milestone_passed' ORDER BY event_id")]


@pytest.mark.parametrize('count', [2, 4, 6])
def test_explicit_serial_profile_preserves_exact_approved_plan_and_order(tmp_path, count):
    store, bounds, team = proposal(tmp_path)
    plan = serial_plan(bounds, count)
    original = deepcopy(plan)
    assert validate_execution_plan(plan, team, bounds) == original
    assert execution_tasks(plan) == original['tasks']
    execution_tasks(plan)[0]['outputs'].append('Caller-local change')
    assert plan == original
    current = present(store, bounds, team, plan)
    assert store.read('PLAN')['concepts'][-1]['package']['di_plan'] == original
    assert current['digest']


@pytest.mark.parametrize('mutate,match', [
    (lambda p, b: p.update(execution_profile='parallel-v1'), 'execution_profile'),
    (lambda p, b: p['tasks'][2].update(depends_on=[]), 'serial chain'),
    (lambda p, b: p['tasks'][2].update(depends_on=['task-0']), 'serial chain'),
    (lambda p, b: p['milestones'][2].update(depends_on=['gate-0']), 'prerequisites|serial chain'),
    (lambda p, b: p['tasks'][2].update(depends_on=['missing']), 'unknown task'),
    (lambda p, b: p['tasks'][0].update(depends_on=['task-3']), 'cycle'),
    (lambda p, b: p['tasks'][2].update(kind='assessment'), 'alternate'),
    (lambda p, b: p['tasks'][2].update(owner='assessor'), 'writer'),
    (lambda p, b: p['milestones'][2].update(verification=['candidate-frozen', 'extra']), 'candidate-frozen'),
    (lambda p, b: p['milestones'][1].update(verification=['independent-assessment']), 'required checks'),
    (lambda p, b: p['milestones'][1].update(verification=['fixture', 'independent-assessment', 'extra']), 'required checks'),
    (lambda p, b: b.update(max_assignments=3), 'max_assignments'),
])
def test_serial_profile_rejects_unsupported_or_unenforceable_plans(tmp_path, mutate, match):
    store, bounds, team = proposal(tmp_path)
    plan = serial_plan(bounds)
    mutate(plan, bounds)
    with pytest.raises(Rejected, match=match):
        present(store, bounds, team, plan)
    assert store.read('PLAN')['concepts'] == []


@pytest.mark.parametrize('count', [1, 3, 5, 8])
def test_serial_profile_rejects_unbounded_or_incomplete_task_pairs(tmp_path, count):
    _, bounds, team = proposal(tmp_path)
    with pytest.raises(Rejected, match='implementation and independent|2, 4 or 6'):
        validate_execution_plan(serial_plan(bounds, count), team, bounds)


def test_multiple_tasks_cannot_share_or_reorder_a_serial_checkpoint(tmp_path):
    _, bounds, team = proposal(tmp_path)
    plan = serial_plan(bounds)
    plan['milestones'][2], plan['milestones'][3] = plan['milestones'][3], plan['milestones'][2]
    with pytest.raises(Rejected, match='matching list order'):
        validate_execution_plan(plan, team, bounds)
    plan = serial_plan(bounds)
    plan['tasks'][1]['milestone'] = 'gate-0'
    plan['milestones'][0]['task_ids'].append('task-1')
    plan['milestones'].pop(1)
    plan['milestones'][1]['depends_on'] = ['gate-0']
    with pytest.raises(Rejected, match='same number of milestones'):
        validate_execution_plan(plan, team, bounds)


def test_serial_profile_is_opt_in_and_all_assessments_run_all_approved_checks(tmp_path):
    _, bounds, team = proposal(tmp_path)
    plan = serial_plan(bounds)
    plan.pop('execution_profile')
    with pytest.raises(Rejected, match='cannot depend on assessment|cannot follow an assessment'):
        validate_execution_plan(plan, team, bounds)
    bounds['compatibility_checks'] = ['consumer']
    plan = serial_plan(bounds)
    plan['milestones'][1]['verification'].remove('consumer')
    with pytest.raises(Rejected, match='compatibility checks'):
        validate_execution_plan(plan, team, bounds)


def test_four_tasks_deliver_only_after_each_scoped_checkpoint_passes(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    final_candidate = {**CANDIDATE, 'identity': 'sha256:final-scoped-patch',
                       'source_identity': 'sha256:final-base-plus-patch'}
    run_to_end(lifecycle, runtime(tmp_path, scripted_four(second_candidate=final_candidate)))
    snap = lifecycle.snapshot('Q1')
    assert snap['execution'] == 'delivered'
    assert snap['delivery']['candidate'] == final_candidate
    assignments = lifecycle.assignments()
    assert [a['kind'] for a in assignments] == ['implementation', 'assessment', 'implementation', 'assessment']
    plan = store.read('Q1')['concepts'][-1]['package']['di_plan']
    scopes = [json.loads(a['scope']) for a in assignments]
    assert [scope['di_task_id'] for scope in scopes] == [f'task-{i}' for i in range(4)]
    assert [scope['di_milestone_id'] for scope in scopes] == [f'gate-{i}' for i in range(4)]
    assert {scope['di_plan_digest'] for scope in scopes} == {digest(plan)}
    assert scopes[0]['di_dependencies'] == {}
    assert all(list(scope['di_dependencies']) == [f'gate-{i - 1}'] for i, scope in enumerate(scopes[1:], 1))
    assert json.loads(assignments[2]['candidate']) == CANDIDATE
    events = milestone_events(store)
    assert [event['task_ids'] for event in events] == [[f'task-{i}'] for i in range(4)]
    assert len({event['receipt_id'] for event in events}) == 4
    view = dashboard(collect(store, 'Q1'))
    assert {node['status'] for node in view['phase_flow']['nodes']} == {'accepted'}


def test_checkpoint_completion_is_not_delivery_and_duplicate_receipts_do_not_advance(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    script = scripted_four()
    script[1]['polls'] = 1000
    fake = runtime(tmp_path, script)
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        checkpoint = lifecycle.assignments()[-1]
        assert checkpoint['kind'] == 'assessment'
        result = lifecycle.complete('checkpoint-receipt', checkpoint['assignment_id'], assessment())
        assert result['disposition'] == 'milestone_passed'
        assert lifecycle.complete('checkpoint-receipt', checkpoint['assignment_id'], assessment())['duplicate']
        assert lifecycle.complete('second-receipt-same-task', checkpoint['assignment_id'], assessment())['disposition'] == 'historical'
        before = lifecycle.snapshot('Q1')
        assert before['delivery'] is None and before['execution'] != 'delivered'
        assert len(lifecycle.assignments()) == 3
        progress = store.read('Q1')['plan_progress']
        assert progress['tasks']['task-0']['status'] == progress['tasks']['task-1']['status'] == 'completed'
        assert progress['tasks'].get('task-2', {}).get('status') != 'completed'
        assert progress['tasks'].get('task-3', {}).get('status') != 'completed'
        worker.tick()
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assert len(lifecycle.assignments()) == 4
    assert len(milestone_events(store)) == 4


def test_failed_checkpoint_blocks_later_tasks_and_dashboard_progress(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    bad = assessment()
    bad['checks'][0]['result'] = 'fail'
    script = scripted_four()
    script[1]['result'] = bad
    run_to_end(lifecycle, runtime(tmp_path, script))
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert len(lifecycle.assignments()) == 2
    assert [event['milestone_id'] for event in milestone_events(store)] == ['gate-0']
    progress = dashboard(collect(store, 'Q1'))['plan_progress']
    assert progress['milestones']['gate-1']['status'] == 'failed'
    assert progress['tasks'].get('task-2', {}).get('status') != 'completed'


def test_checkpoint_repair_stays_on_its_tasks_then_continues_remaining_plan(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'Synthetic defect at checkpoint'})
    bad['checks'][0]['result'] = 'fail'
    repaired = {**CANDIDATE, 'identity': 'sha256:checkpoint-repair', 'source_identity': 'sha256:checkpoint-repair-source'}
    script = [{'kind': 'implementation', 'result': implementation()},
              {'kind': 'assessment', 'result': bad},
              {'kind': 'repair', 'result': implementation(repaired)},
              {'kind': 'assessment', 'result': assessment(candidate=repaired)},
              {'kind': 'implementation', 'result': implementation(repaired)},
              {'kind': 'assessment', 'result': assessment(candidate=repaired)}]
    run_to_end(lifecycle, runtime(tmp_path, script))
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assignments = lifecycle.assignments()
    assert [a['kind'] for a in assignments] == ['implementation', 'assessment', 'repair', 'assessment', 'implementation', 'assessment']
    assert [json.loads(a['scope'])['di_task_id'] for a in assignments] == ['task-0', 'task-1', 'task-0', 'task-1', 'task-2', 'task-3']
    assert {event['milestone_id'] for event in milestone_events(store)} == {f'gate-{i}' for i in range(4)}


def test_missing_predecessor_proof_prevents_next_implementation_dispatch(tmp_path, monkeypatch):
    store, lifecycle, _ = serial_setup(tmp_path)
    enqueue = lifecycle._enqueue

    def remove_checkpoint_proof(db, work_id, kind, predecessor, **kwargs):
        result = enqueue(db, work_id, kind, predecessor, **kwargs)
        if kind == 'implementation':
            for row in db.execute("SELECT event_id,detail FROM lifecycle_event WHERE kind='di_milestone_passed'"):
                if json.loads(row['detail'])['milestone_id'] == 'gate-1':
                    db.execute('DELETE FROM lifecycle_event WHERE event_id=?', (row['event_id'],))
        return result

    monkeypatch.setattr(lifecycle, '_enqueue', remove_checkpoint_proof)
    run_to_end(lifecycle, runtime(tmp_path, scripted_four()))
    assignments = lifecycle.assignments()
    assert len(assignments) == 3 and assignments[-1]['state'] == 'proposed'
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert lifecycle.snapshot('Q1')['delivery'] is None


def test_earlier_assessment_scope_cannot_authorize_a_later_checkpoint(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    fake = runtime(tmp_path, scripted_four(final_polls=1000))
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        assignments = lifecycle.assignments()
        assert len(assignments) == 4 and assignments[-1]['state'] == 'observed_active'
        # Deliberate persistence corruption: identical candidate bytes do not make
        # an earlier task's assessment scope valid for the final checkpoint.
        with store._transaction() as db:
            db.execute('UPDATE assignment SET scope=? WHERE assignment_id=?',
                       (assignments[1]['scope'], assignments[3]['assignment_id']))
        worker.tick()
    snap = lifecycle.snapshot('Q1')
    assert snap['execution'] == 'held' and snap['delivery'] is None
    assert snap['owned_stops'][-1]['reason'] == 'approved_envelope'
    assert 'gate-3' not in {event['milestone_id'] for event in milestone_events(store)}


def test_six_serial_tasks_finish_with_six_distinct_qualified_receipts(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path, count=6)
    script = []
    for _ in range(3):
        script.extend([{'kind': 'implementation', 'result': implementation()},
                       {'kind': 'assessment', 'result': assessment()}])
    run_to_end(lifecycle, runtime(tmp_path, script))
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assignments = lifecycle.assignments()
    assert [json.loads(a['scope'])['di_task_id'] for a in assignments] == [f'task-{i}' for i in range(6)]
    events = milestone_events(store)
    assert [event['milestone_id'] for event in events] == [f'gate-{i}' for i in range(6)]
    assert len({event['receipt_id'] for event in events}) == 6
    assert {node['status'] for node in dashboard(collect(store, 'Q1'))['phase_flow']['nodes']} == {'accepted'}


def test_second_checkpoint_repair_preserves_first_checkpoint_receipts(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    bad = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'Synthetic second-checkpoint defect'})
    bad['checks'][0]['result'] = 'fail'
    repaired = {**CANDIDATE, 'identity': 'sha256:second-checkpoint-repair',
                'source_identity': 'sha256:second-checkpoint-repair-source'}
    script = scripted_four()
    script[3]['result'] = bad
    script.extend([{'kind': 'repair', 'result': implementation(repaired)},
                   {'kind': 'assessment', 'result': assessment(candidate=repaired)}])
    run_to_end(lifecycle, runtime(tmp_path, script))
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assignments = lifecycle.assignments()
    assert [json.loads(a['scope'])['di_task_id'] for a in assignments] == ['task-0', 'task-1', 'task-2', 'task-3', 'task-2', 'task-3']
    events = milestone_events(store)
    assert [event['milestone_id'] for event in events] == ['gate-0', 'gate-1', 'gate-2', 'gate-2', 'gate-3']
    original_checkpoint = next(event for event in events if event['milestone_id'] == 'gate-1')
    with store._connection() as db:
        preserved = db.execute('SELECT assignment_id FROM completion WHERE receipt_id=?',
                               (original_checkpoint['receipt_id'],)).fetchone()
    assert preserved['assignment_id'] == assignments[1]['assignment_id']
    assert json.loads(assignments[4]['scope'])['di_dependencies'] == {'gate-1': digest(original_checkpoint)}
    progress = store.read('Q1')['plan_progress']
    assert progress['tasks']['task-1']['status'] == 'completed'
    assert progress['tasks']['task-3']['candidate'] == repaired


@pytest.mark.parametrize('corruption', ['missing', 'fingerprint', 'result'])
def test_lost_or_changed_completion_receipt_prevents_next_writer_claim(tmp_path, corruption):
    store, lifecycle, _ = serial_setup(tmp_path)
    script = scripted_four()
    script[1]['polls'] = 1000
    fake = runtime(tmp_path, script)
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        checkpoint = lifecycle.assignments()[-1]
        assert lifecycle.complete('checkpoint-receipt', checkpoint['assignment_id'], assessment())['disposition'] == 'milestone_passed'
        with store._transaction() as db:
            if corruption == 'missing':
                db.execute('DELETE FROM completion WHERE receipt_id=?', ('checkpoint-receipt',))
            elif corruption == 'fingerprint':
                db.execute('UPDATE completion SET fingerprint=? WHERE receipt_id=?',
                           ('sha256:synthetic-tampering', 'checkpoint-receipt'))
            else:
                db.execute('UPDATE completion SET result=? WHERE receipt_id=?', ('{}', 'checkpoint-receipt'))
        worker.tick()
    assignments = lifecycle.assignments()
    assert len(assignments) == 3 and assignments[-1]['state'] == 'proposed'
    assert lifecycle.snapshot('Q1')['execution'] == 'held'
    assert lifecycle.snapshot('Q1')['delivery'] is None
    assert store.read('Q1')['plan_progress']['tasks'].get('task-1', {}).get('status') != 'completed'


def test_later_implementation_uses_exact_checkpoint_source_as_baseline(tmp_path):
    store, lifecycle, _ = serial_setup(tmp_path)
    script = scripted_four()
    script[2]['polls'] = 1000
    fake = runtime(tmp_path, script)
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        assignments = lifecycle.assignments()
        assert len(assignments) == 3 and assignments[-1]['state'] == 'observed_active'
        with store._connection() as db:
            _, bounds = lifecycle._run(db, 'Q1')
            assert plan_runtime.implementation_baseline_in(db, assignments[0], bounds) == bounds['baseline']
            assert plan_runtime.implementation_baseline_in(db, assignments[2], bounds) == CANDIDATE['source_identity']


@pytest.mark.parametrize('missing_upstream', [False, True])
def test_continuation_inherits_every_upstream_receipt_and_second_freeze(tmp_path, missing_upstream):
    store, lifecycle, current = serial_setup(tmp_path)
    frozen = {**CANDIDATE, 'identity': 'sha256:second-stage-frozen', 'source_identity': 'sha256:second-stage-source'}
    run_to_end(lifecycle, runtime(tmp_path, scripted_four(second_candidate=frozen, final_polls=1000)))
    assert len(lifecycle.assignments()) == 4
    old_proofs = milestone_events(store)
    assert [proof['milestone_id'] for proof in old_proofs] == ['gate-0', 'gate-1', 'gate-2']
    package = store.read('Q1')['concepts'][-1]['package']
    successor = store.present('continuation-present', 'Q1', store.read('Q1')['revision'], 'Q1-DI', 2,
        'Synthetic technical-continuation helper seam', package['acceptance'],
        package['team_envelope'], package['authority'], execution_envelope=package['execution_envelope'],
        di_plan=package['di_plan'])
    # Publication's qualified recovery checks are covered by the continuation
    # suite. This seam isolates the staged proof transfer inside its transaction.
    if missing_upstream:
        with store._transaction() as db:
            db.execute('DELETE FROM completion WHERE receipt_id=?', (old_proofs[1]['receipt_id'],))
        with pytest.raises(Rejected, match='every qualified upstream'):
            with store._transaction() as db:
                plan_runtime.inherit_frozen_candidate_in(lifecycle, db, 'Q1', current['digest'], frozen)
        assert len(milestone_events(store)) == 3
        return
    with store._transaction() as db:
        plan_runtime.inherit_frozen_candidate_in(lifecycle, db, 'Q1', current['digest'], frozen)
        scope = plan_runtime.bind_scope_in(db, 'Q1', 'assessment', {})
    inherited = milestone_events(store)[3:]
    assert [proof['milestone_id'] for proof in inherited] == ['gate-0', 'gate-1', 'gate-2']
    for original, copied in zip(old_proofs, inherited):
        assert copied['receipt_id'] == original['receipt_id']
        assert copied['concept_digest'] == successor['digest']
        assert copied['inherited_from_digest'] == current['digest']
        assert copied['inherited_proof_digest'] == digest(original)
        assert copied['candidate'] == original['candidate']
    assert scope['di_task_id'] == 'task-3'
    assert scope['di_dependencies'] == {'gate-2': digest(inherited[-1])}
    assert len(store.read('Q1')['decisions']) == 1
    assert store.read('Q1')['concept_decision'] == 'pending'


def test_reviewed_host_intermediate_native_failure_triages_repairs_then_finishes_plan(tmp_path, monkeypatch):
    from test_aos_startup_retry import claim
    import test_aos_reviewed_host_triage as reviewed

    original_allowance, original_contract = reviewed.allowance, reviewed.contract

    def four_task_allowance(directory, **changes):
        return original_allowance(directory, **{**changes,
            'max_assignments': changes['max_assignments'] + 2,
            'max_seconds': changes['max_seconds'] + 120})

    def four_task_contract(current, **changes):
        return original_contract(current, **{**changes, 'max_assignments': changes['max_assignments'] + 2})

    monkeypatch.setattr(reviewed, 'allowance', four_task_allowance)
    monkeypatch.setattr(reviewed, 'contract', four_task_contract)
    monkeypatch.setattr(reviewed, 'synthetic_plan', serial_plan)
    store, lifecycle = reviewed.setup(tmp_path)
    failed, failure, triage = reviewed.enter_triage(lifecycle)
    assert failure['reviewed_host_validation']['phase'] == 'native_failed'
    assert lifecycle.assessment_context(triage['assignment_id'])['origin']['assignment_id'] == failed['assignment_id']
    assert reviewed.complete(lifecycle, triage, reviewed.result(lifecycle, triage, 'triage'), 'triage-result')['disposition'] == 'repair'
    repair = claim(lifecycle)
    assert lifecycle.complete('repair-result', repair['assignment_id'], implementation())['disposition'] == 'assessment_required'
    reassessment = claim(lifecycle)
    assert reviewed.complete(lifecycle, reassessment, reviewed.result(lifecycle, reassessment, 'validated'),
                             'checkpoint-passed')['disposition'] == 'milestone_passed'
    assert lifecycle.snapshot(reviewed.WORK)['delivery'] is None
    writer = claim(lifecycle)
    assert lifecycle.complete('later-writer-result', writer['assignment_id'], implementation())['disposition'] == 'assessment_required'
    final = claim(lifecycle)
    assert reviewed.complete(lifecycle, final, reviewed.result(lifecycle, final, 'validated'), 'final-result')['disposition'] == 'delivered'
    assignments = lifecycle.assignments()
    assert len(assignments) == 7
    assert [a['kind'] for a in assignments] == ['implementation', 'assessment', 'assessment', 'repair',
                                             'assessment', 'implementation', 'assessment']
    assert [json.loads(a['scope'])['di_task_id'] for a in assignments] == ['task-0', 'task-1', 'task-1',
                                                                       'task-0', 'task-1', 'task-2', 'task-3']
    assert lifecycle.snapshot(reviewed.WORK)['execution'] == 'delivered'
    assert [event['milestone_id'] for event in milestone_events(store)] == ['gate-0', 'gate-0', 'gate-1', 'gate-2', 'gate-3']


@pytest.mark.parametrize('retries,jobs', [(0, 7), (1, 8)])
def test_reviewed_host_four_task_capacity_includes_triage_and_retry_allowance(tmp_path, retries, jobs):
    from agentic_tools.aos import budgets
    from test_aos_finish_capacity import allowance

    budget = allowance(tmp_path, max_assignments=jobs, max_seconds=jobs * 180, max_job_seconds=180)
    bounds = dict(max_repairs=1, max_seconds=180, max_assignments=jobs, max_startup_retries=retries,
                  reviewed_host=True, planned_assignments=4)
    result = budgets.finish_capacity(budget, **bounds)
    assert result['ready'] and result['required_assignments'] == jobs
    assert result['required_seconds'] == jobs * 180
    assert result['reservation_performed'] is False
    for tiny_budget, tiny_bounds in [({**budget, 'max_assignments': jobs - 1}, bounds),
                                    ({**budget, 'max_seconds': jobs * 180 - 1}, bounds),
                                    (budget, {**bounds, 'max_assignments': jobs - 1})]:
        preview = budgets.finish_capacity(tiny_budget, **tiny_bounds)
        assert not preview['ready'] and preview['blockers']
        assert preview['required_assignments'] == jobs


def test_continuation_retains_reasoned_forecasts_actuals_and_original_estimates(tmp_path):
    store, lifecycle, current = serial_setup(tmp_path)
    frozen = {**CANDIDATE, 'identity': 'sha256:forecast-stage-two', 'source_identity': 'sha256:forecast-stage-two-source'}
    run_to_end(lifecycle, runtime(tmp_path, scripted_four(second_candidate=frozen, final_polls=1000)))
    package = store.read('Q1')['concepts'][-1]['package']
    original = deepcopy(package['di_plan']['tasks'][2]['original_estimate'])
    revised = deepcopy(original)
    revised['effort_hours']['high'] = 99
    actuals = {'active_hours': 0.03, 'human_wait_hours': None, 'technical_wait_hours': 0.01, 'monetary': None}
    recorded = plan_runtime.record_forecast(lifecycle, 'observed-forecast', 'Q1', store.read('Q1')['revision'],
        current['digest'], 'task-2', 'A measured checkpoint exposed additional effort uncertainty', revised, actuals)
    assert recorded['outside_original_range'] and not recorded['limits_changed']
    before = store.read('Q1')['plan_progress']['tasks']['task-2']
    successor = store.present('forecast-continuation-present', 'Q1', store.read('Q1')['revision'], 'Q1-DI', 2,
        'Synthetic continuation preserves observed forecasting history', package['acceptance'],
        package['team_envelope'], package['authority'], execution_envelope=package['execution_envelope'],
        di_plan=package['di_plan'])
    with store._transaction() as db:
        plan_runtime.inherit_frozen_candidate_in(lifecycle, db, 'Q1', current['digest'], frozen)
    after = store.read('Q1')
    task_progress = after['plan_progress']['tasks']['task-2']
    assert task_progress['actuals'] == actuals
    assert len(task_progress['revised_forecasts']) == 1
    forecast = task_progress['revised_forecasts'][0]
    assert forecast['reason'] == before['revised_forecasts'][0]['reason']
    assert forecast['forecast'] == revised and forecast['actuals'] == actuals
    assert after['concepts'][-1]['package']['di_plan']['tasks'][2]['original_estimate'] == original
    assert after['concepts'][-1]['package']['execution_envelope'] == package['execution_envelope']
    assert successor['digest'] != current['digest'] and len(after['decisions']) == 1
