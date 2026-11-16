"""Synthetic plan enforcement at the real approval/lifecycle/projection boundaries."""
from copy import deepcopy
import json

import pytest

from agentic_tools.aos.planning import validate_plan, validate_execution_plan, approval_summary_html
from agentic_tools.aos.plan_runtime import record_forecast
from agentic_tools.aos.projection import collect, dashboard
from agentic_tools.aos.store import Store, Rejected
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import setup, contract, implementation, assessment, runtime, run_to_end
from test_aos_store import TEAM
from di_plan_fixture import synthetic_plan


def proposal(tmp_path):
    store = Store(tmp_path / 'state', (tmp_path / 'fixture',))
    bounds = contract({'digest': 'placeholder'})
    bounds.pop('concept_revision')
    bounds.pop('concept_digest')
    team = [{**TEAM[0], 'name': name} for name in ('writer', 'assessor')]
    store.introduce('intro', 'PLAN', 'Disposable planning example', ['fixture'], team)
    return store, bounds, team


def present(store, bounds, team, plan, revision=1):
    return store.present('present-' + str(revision), 'PLAN', store.read('PLAN')['revision'], 'DI', revision,
                         'Synthetic example', ['Independent tests pass'], team, bounds['authority'],
                         execution_envelope=bounds, di_plan=plan)


@pytest.mark.parametrize('mutate,match', [
    (lambda p: p['tasks'][0].pop('outputs'), 'outputs'),
    (lambda p: p['tasks'][0].pop('definition_of_done'), 'definition_of_done'),
    (lambda p: p['tasks'][0].update(owner='unknown'), 'owners'),
    (lambda p: p['tasks'][0].update(depends_on=['missing']), 'unknown task'),
    (lambda p: p['tasks'][0].update(depends_on=['assess']), 'cycle'),
    (lambda p: p['milestones'][1].update(depends_on=['missing']), 'unknown milestone'),
    (lambda p: p['milestones'][0].update(verification=[]), 'verification'),
    (lambda p: p['tasks'][0]['original_estimate'].pop('assumptions'), 'assumptions'),
    (lambda p: p['tasks'][0]['original_estimate'].pop('confidence'), 'confidence'),
    (lambda p: p['tasks'][0]['original_estimate']['monetary']['agent_api'].pop('value'), 'value'),
    (lambda p: p['tasks'][0]['original_estimate']['effort_hours'].update(low=9), 'range'),
    (lambda p: p['tasks'][0]['original_estimate']['effort_hours'].update(low=True), 'number'),
    (lambda p: p['tasks'][0]['original_estimate']['elapsed_hours'].update(high=float('inf')), 'finite'),
    (lambda p: p['tasks'][0].update(id='T1'), 'pattern'),
    (lambda p: p.update(replan_triggers=[]), 'replan_triggers'),
])
def test_invalid_plan_rejected_before_presentation_without_side_effects(tmp_path, mutate, match):
    store, bounds, team = proposal(tmp_path)
    plan = synthetic_plan(bounds)
    mutate(plan)
    with pytest.raises(Rejected, match=match):
        present(store, bounds, team, plan)
    assert store.read('PLAN')['concepts'] == []
    assert store.read('PLAN')['revision'] == 1


def test_missing_and_unsupported_gates_reject(tmp_path):
    store, bounds, team = proposal(tmp_path)
    with pytest.raises(Rejected, match='invalid_di_plan'):
        present(store, bounds, team, None)
    plan = synthetic_plan(bounds)
    plan['milestones'][0]['verification'] = ['pretend-build']
    with pytest.raises(Rejected, match='candidate-frozen'):
        present(store, bounds, team, plan)
    plan = synthetic_plan(bounds)
    extra = deepcopy(plan['tasks'][0])
    extra['id'] = 'extra-task'
    plan['tasks'].append(extra)
    plan['milestones'][0]['task_ids'].append(extra['id'])
    with pytest.raises(Rejected, match='one aggregate task'):
        present(store, bounds, team, plan)


def test_revision_estimates_preserved_and_digest_changes(tmp_path):
    store, bounds, team = proposal(tmp_path)
    plan = synthetic_plan(bounds)
    first = present(store, bounds, team, plan)
    changed = deepcopy(plan)
    changed['tasks'][0]['original_estimate']['confidence'] = 'high'
    with pytest.raises(Rejected, match='original_estimate_changed'):
        present(store, bounds, team, changed, 2)
    changed = deepcopy(plan)
    changed['tasks'][0]['outputs'].append('New factual report')
    second = present(store, bounds, team, changed, 2)
    assert second['digest'] != first['digest']
    cap = store.host_capability('synthetic', 'SIMULATED')
    with pytest.raises(Rejected, match='stale_concept'):
        store.begin_approval('old', 'PLAN', second['work_revision'], first['digest'], capability=cap)


def test_freeze_and_acceptance_derive_from_actual_evidence(tmp_path):
    store, lifecycle, current = setup(tmp_path)
    before = dashboard(collect(store, 'Q1'))
    assert before['di_plan'] == store.read('Q1')['concepts'][-1]['package']['di_plan']
    assert next(n for n in before['phase_flow']['nodes'] if n['id'] == 'assess')['status'] == 'waiting'
    run_to_end(lifecycle, runtime(tmp_path))
    after = dashboard(collect(store, 'Q1'))
    assert {n['status'] for n in after['phase_flow']['nodes']} == {'accepted'}
    assert after['plan_progress']['milestones']['accept']['evidence']['fixture']['reference'] == 'SIMULATED:fixture'
    assert after['source_digest'] == collect(store, 'Q1')['source_digest']


def test_failed_freeze_prevents_assessment_dispatch(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    bad = implementation()
    bad.pop('output')
    run_to_end(lifecycle, runtime(tmp_path, [{'kind': 'implementation', 'result': bad}]))
    assert len(lifecycle.assignments()) == 1
    assert store.read('Q1')['execution'] == 'held'
    assert store.read('Q1')['plan_progress']['milestones']['freeze']['status'] == 'failed'


def test_failed_milestone_checks_never_pass_acceptance(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    bad = assessment()
    bad['checks'][0]['result'] = 'fail'
    run_to_end(lifecycle, runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()},
                                          {'kind': 'assessment', 'result': bad}]))
    view = dashboard(collect(store, 'Q1'))
    assert store.read('Q1')['execution'] == 'held'
    assert view['plan_progress']['milestones']['accept']['status'] == 'failed'
    assert next(n for n in view['phase_flow']['nodes'] if n['id'] == 'assess')['status'] == 'failed'


def test_removed_milestone_proof_holds_saved_assessment_claim(tmp_path, monkeypatch):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path)
    enqueue = lifecycle._enqueue
    def remove_proof(db, work_id, kind, predecessor):
        result = enqueue(db, work_id, kind, predecessor)
        if kind == 'assessment':
            db.execute("DELETE FROM lifecycle_event WHERE kind='di_milestone_passed'")
        return result
    monkeypatch.setattr(lifecycle, '_enqueue', remove_proof)
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        # Synthetic corruption seam: task status or frozen bytes alone cannot
        # substitute for the declared milestone's bound verification receipt.
        worker.tick()
    assert lifecycle.assignments()[-1]['state'] == 'proposed'
    assert store.read('Q1')['execution'] == 'held'


def test_forecasts_actuals_and_limits_stay_distinct(tmp_path):
    store, lifecycle, current = setup(tmp_path)
    before = store.read('Q1')
    original = before['concepts'][-1]['package']['di_plan']['tasks'][0]['original_estimate']
    revised = deepcopy(original)
    revised['effort_hours']['high'] = 99
    result = record_forecast(lifecycle, 'forecast', 'Q1', before['revision'], current['digest'], 'implement',
                            'New uncertainty observed', revised,
                            {'active_hours': 0.01, 'human_wait_hours': None, 'technical_wait_hours': None, 'monetary': None})
    after = store.read('Q1')
    assert result['outside_original_range'] and not result['limits_changed']
    assert after['execution'] == before['execution']
    assert after['concepts'][-1]['package']['di_plan']['tasks'][0]['original_estimate'] == original
    assert after['plan_progress']['tasks']['implement']['actuals']['human_wait_hours'] is None
    assert after['plan_progress']['tasks']['implement']['revised_forecasts'][0]['reason'] == 'New uncertainty observed'


def test_approval_summary_exposes_cost_uncertainty_and_limits(tmp_path):
    _, bounds, team = proposal(tmp_path)
    plan = synthetic_plan(bounds)
    plan['tasks'][0]['title'] = '<script>unsafe()</script>'
    html = approval_summary_html(plan, bounds)
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'Unknown' in html and 'confidence' in html.lower()
    assert '60' in html and '0.02' in html and 'execution limits' in html


def test_reintroduced_task_cannot_reset_original_estimate(tmp_path):
    store, bounds, team = proposal(tmp_path)
    plan = synthetic_plan(bounds)
    present(store, bounds, team, plan)
    renamed = deepcopy(plan)
    renamed['tasks'][0]['id'] = 'temporary-task'
    renamed['tasks'][1]['depends_on'] = ['temporary-task']
    renamed['milestones'][0]['task_ids'] = ['temporary-task']
    present(store, bounds, team, renamed, 2)
    changed = deepcopy(plan)
    changed['tasks'][0]['original_estimate']['confidence'] = 'high'
    with pytest.raises(Rejected, match='original_estimate_changed'):
        present(store, bounds, team, changed, 3)


def test_legacy_executable_package_cannot_gain_new_approval_or_dispatch(tmp_path):
    store, lifecycle, current = setup(tmp_path, approved=False, admitted=False)
    # Import a pre-P3 package through a database seam, never a supported API.
    from agentic_tools.aos.store import canonical, digest
    package = store.read('Q1')['concepts'][-1]['package']
    package.pop('di_plan')
    legacy_digest = digest(package)
    with store._transaction() as db:
        db.execute('UPDATE concept SET package=?,digest=?', (canonical(package), legacy_digest))
        row = db.execute('SELECT contract FROM lifecycle').fetchone()
        bounds = json.loads(row[0])
        bounds['concept_digest'] = legacy_digest
        db.execute('UPDATE lifecycle SET contract=?', (canonical(bounds),))
    cap = store.host_capability('synthetic', 'SIMULATED')
    with pytest.raises(Rejected, match='invalid_di_plan'):
        store.begin_approval('legacy-approval', 'Q1', store.read('Q1')['revision'], legacy_digest, capability=cap)
    held = lifecycle.admit('legacy-admit', 'Q1')
    assert held['execution'] == 'held' and any('invalid_di_plan' in x for x in held['blockers'])
    assert lifecycle.assignments() == []


def test_forecast_requires_reason_and_current_revision(tmp_path):
    store, lifecycle, current = setup(tmp_path)
    before = store.read('Q1')
    estimate = before['concepts'][-1]['package']['di_plan']['tasks'][0]['original_estimate']
    actuals = {'active_hours': None, 'human_wait_hours': None, 'technical_wait_hours': None, 'monetary': None}
    with pytest.raises(Rejected, match='reason'):
        record_forecast(lifecycle, 'blank', 'Q1', before['revision'], current['digest'], 'implement', ' ', estimate, actuals)
    with pytest.raises(Rejected, match='stale_revision'):
        record_forecast(lifecycle, 'stale', 'Q1', 1, current['digest'], 'implement', 'Observed uncertainty', estimate, actuals)


def test_plan_tampering_cannot_bypass_approved_digest(tmp_path):
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    from agentic_tools.aos.store import canonical
    package = store.read('Q1')['concepts'][-1]['package']
    package['di_plan']['tasks'][0]['outputs'] = ['Altered after approval']
    with store._transaction() as db:
        db.execute('UPDATE concept SET package=?', (canonical(package),))
    result = lifecycle.admit('tampered', 'Q1')
    assert result['execution'] == 'held'
    assert any('approved digest' in b for b in result['blockers'])


def test_lost_milestone_evidence_stops_an_active_assessor(tmp_path):
    store, lifecycle, _ = setup(tmp_path)
    fake = runtime(tmp_path, [{'kind': 'implementation', 'result': implementation()},
                              {'kind': 'assessment', 'result': assessment(), 'polls': 1000}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        active = lifecycle.assignments()[-1]
        assert active['kind'] == 'assessment' and active['state'] == 'observed_active'
        with store._transaction() as db:
            db.execute("DELETE FROM lifecycle_event WHERE kind='di_milestone_passed'")
        worker.tick()
    stop = lifecycle.snapshot('Q1')['owned_stops'][-1]
    assert stop['reason'] == 'approved_envelope' and stop['state'] == 'stopped'
    assert lifecycle.snapshot('Q1')['delivery'] is None
