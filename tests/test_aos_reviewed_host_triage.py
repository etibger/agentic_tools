"""Simulated reviewed-host receipts: no model, native command or original state."""
from copy import deepcopy
import json

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from test_aos_finish_capacity import allowance
from test_aos_lifecycle import CANDIDATE, approve, contract, implementation
from test_aos_startup_retry import claim, complete as complete_startup, failure
from test_aos_store import TEAM


WORK = 'HOST-TRIAGE'


def setup(tmp_path, *, repairs=1, retries=0):
    source = tmp_path / 'source'
    source.mkdir()
    store = Store(tmp_path / 'state', (source,))
    jobs = 2 + 3 * repairs + retries
    budget = allowance(store.directory, max_assignments=jobs, max_seconds=jobs * 60)
    budget.pop('kind')
    recipe = {'source': str(source), 'outputs': str(tmp_path / 'outputs'), 'max_seconds': 60,
              'execution_policy': 'isolated-checkout-v1',
              'checkout_policy': {'native_validation': 'reviewed-host-v1', 'change_roots': ['fixture.py'],
                                  'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 100000,
                                                      'max_output_bytes': 100000}},
              'checks': [{'name': 'fixture', 'result_kind': 'pytest', 'adapter_id': 'pytest-v1'}]}
    bounds = contract({'digest': 'not-self-referential'}, max_assignments=jobs, max_repairs=repairs,
                      max_startup_retries=retries, execution_policy='isolated-checkout-v1',
                      runtime_recipe=recipe, runtime_recipe_digest=digest(recipe), product_budget=budget,
                      check_adapters={'pytest-v1': check_adapters.identities()['pytest-v1']})
    bounds.pop('concept_revision'); bounds.pop('concept_digest')
    team = [{**TEAM[0], 'name': name, 'role': role}
            for name, role in [('writer', 'Implementer'), ('assessor', 'Independent assessor')]]
    store.introduce('intro', WORK, 'SIMULATED reviewed host triage', ['source'], team)
    current = store.present('present', WORK, 1, 'SIMULATED-DI', 1, 'SIMULATED failure triage',
                            ['No host commands'], team, bounds['authority'], execution_envelope=bounds)
    lifecycle = Lifecycle(store)
    lifecycle.register('register', WORK, {**bounds, 'concept_revision': 1, 'concept_digest': current['digest']})
    approve(store, current, WORK)
    assert lifecycle.admit('admit', WORK)['execution'] == 'eligible'
    writer = claim(lifecycle)
    assert lifecycle.complete('writer-result', writer['assignment_id'], implementation())['disposition'] == 'assessment_required'
    return store, lifecycle


def result(lifecycle, assignment, phase, *, confirmed=True):
    frozen = json.loads(assignment['candidate'])
    report = {'path': '/SIMULATED/review-' + assignment['attempt_id'] + '.txt', 'sha256': 'a' * 64}
    review_verdict = 'fail' if phase in ('review_rejected', 'triage') else 'pass'
    findings = [{'path': 'fixture.py', 'summary': 'wrong count', 'evidence': 'Observed 2; the fixture requires 3',
                 'confirmed': True, 'in_scope': True}] if review_verdict == 'fail' and confirmed else []
    checks = []
    if phase in ('native_failed', 'validated'):
        failed = phase == 'native_failed'
        checks = [{'name': 'fixture', 'command': ['pytest', 'fixture.py'], 'environment': 'SIMULATED',
                   'executor': 'assessor', 'kind': 'tests', 'recognized': True, 'selected': 1,
                   'passed': int(not failed), 'failed': int(failed), 'errors': 0, 'skipped': 0,
                   'exit_code': int(failed), 'result': 'fail' if failed else 'pass', 'artifacts': [],
                   'adapter': {**check_adapters.identities()['pytest-v1'], 'binding': 'approved-package',
                               'concept_digest': assignment['concept_digest']},
                   'output': '/SIMULATED/check.txt', 'output_sha256': 'b' * 64,
                   'raw_outputs': {'/SIMULATED/stdout.txt': 'c' * 64},
                   'candidate': frozen, 'scope': 'SIMULATED fixture', 'limits': 'SIMULATED only',
                   'native_check_id': 'SIMULATED-native-' + assignment['attempt_id'],
                   'cleanup': {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
                               'partial_mutations': {'status': 'none', 'changes': []}}}]
    value = {'status': 'completed' if phase == 'validated' else 'failed', 'executor': 'assessor',
             'candidate': frozen, 'observed_source_identity': frozen['source_identity'],
             'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
             'partial_mutations': {'status': 'none', 'changes': []},
             'resource_limits': {'breach': None}, 'runtime_turn_status': 'completed',
             'runtime_turn_id': 'SIMULATED-turn', 'checks': checks, 'review_findings': findings,
             'native_checks': [check['native_check_id'] for check in checks],
             'initial_reports': {'execution': [check['output'] for check in checks], 'review': report, 'independent': True},
             'reviewed_host_validation': {'mode': 'reviewed-host-v1', 'phase': phase,
                                         'review': report, 'review_verdict': review_verdict}}
    if findings:
        value['finding'] = {'confirmed': True, 'in_scope': True, 'summary': 'fixture.py: wrong count'}
    if phase == 'triage':
        value['reviewed_host_validation']['triage_origin'] = lifecycle.assessment_context(assignment['assignment_id'])['origin']
    return value


def complete(lifecycle, assignment, value, receipt='assessment-result'):
    return lifecycle.complete(receipt, assignment['assignment_id'], value)


def enter_triage(lifecycle):
    assessment = claim(lifecycle)
    value = result(lifecycle, assessment, 'native_failed')
    assert complete(lifecycle, assessment, value)['disposition'] == 'triage_required'
    return assessment, value, claim(lifecycle)


def test_static_review_finding_repairs_without_native_execution(tmp_path):
    _, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    value = result(lifecycle, assignment, 'review_rejected')
    assert value['checks'] == value['initial_reports']['execution'] == []
    assert complete(lifecycle, assignment, value)['disposition'] == 'repair'
    assert [a['kind'] for a in lifecycle.assignments()] == ['implementation', 'assessment', 'repair']
    assert lifecycle.snapshot(WORK)['candidate'] is None


def test_native_failure_queues_one_exact_triage_then_confirmed_repair_and_delivery(tmp_path):
    store, lifecycle = setup(tmp_path)
    failed, value, triage = enter_triage(lifecycle)
    context = Lifecycle(store).assessment_context(triage['assignment_id'])
    assert context['purpose'] == 'native_failure_triage'
    assert context['failure_result'] == value
    assert context['origin'] == {'receipt_id': 'assessment-result', 'assignment_id': failed['assignment_id'],
        'attempt_id': failed['attempt_id'], 'work_id': WORK, 'concept_revision': failed['concept_revision'],
        'concept_digest': failed['concept_digest'], 'contract_revision': failed['contract_revision'],
        'candidate_identity': CANDIDATE['identity'], 'source_identity': CANDIDATE['source_identity'],
        'result_sha256': digest(value), 'cycle': 0}
    assert complete(lifecycle, triage, result(lifecycle, triage, 'triage'), 'triage-result')['disposition'] == 'repair'
    repair = claim(lifecycle)
    assert lifecycle.complete('repair-result', repair['assignment_id'], implementation())['disposition'] == 'assessment_required'
    final = claim(lifecycle)
    assert lifecycle.assessment_context(final['assignment_id']) == {'purpose': 'assessment'}
    assert complete(lifecycle, final, result(lifecycle, final, 'validated'), 'final-result')['disposition'] == 'delivered'
    snap = lifecycle.snapshot(WORK)
    assert snap['execution'] == 'delivered' and snap['resources'] == []
    assert [a['kind'] for a in snap['assignments']] == ['implementation', 'assessment', 'assessment', 'repair', 'assessment']


@pytest.mark.parametrize('fault', ['inconclusive', 'out_of_scope', 'unconfirmed', 'wrong_origin', 'wrong_source',
                                  'wrong_review', 'ordinary_validation', 'native_execution', 'failed_turn', 'unknown_cleanup'])
def test_triage_cannot_deliver_or_repair_without_exact_confirmed_finding(tmp_path, fault):
    _, lifecycle = setup(tmp_path)
    _, _, triage = enter_triage(lifecycle)
    value = result(lifecycle, triage, 'triage', confirmed=fault != 'inconclusive')
    if fault == 'out_of_scope': value['review_findings'][0]['path'] = 'other.py'
    elif fault == 'unconfirmed': value['finding']['confirmed'] = False
    elif fault == 'wrong_origin': value['reviewed_host_validation']['triage_origin']['result_sha256'] = 'wrong'
    elif fault == 'wrong_source': value['observed_source_identity'] = 'wrong'
    elif fault == 'wrong_review': value['reviewed_host_validation']['review'] = {'path': 'wrong', 'sha256': 'f' * 64}
    elif fault == 'ordinary_validation': value = result(lifecycle, triage, 'validated')
    elif fault == 'native_execution': value['native_checks'] = ['executed-check']
    elif fault == 'failed_turn': value['runtime_turn_status'] = 'failed'
    elif fault == 'unknown_cleanup': value['stopped'] = False
    assert complete(lifecycle, triage, value, 'triage-result')['disposition'] == 'held'
    assert len(lifecycle.assignments()) == 3 and lifecycle.snapshot(WORK)['delivery'] is None
    with pytest.raises(Rejected, match='already consumed'):
        Lifecycle(lifecycle.store).reassess('manual-reassess', WORK)


@pytest.mark.parametrize('fault', ['missing_marker', 'wrong_mode', 'forged_triage', 'review_failed', 'review_findings',
                                  'unrecognized', 'blocked', 'timeout', 'cancelled', 'zero_selected', 'skipped_only',
                                  'signal_exit', 'wrong_adapter', 'missing_check', 'resource_failure', 'resource_breach',
                                  'wrong_output_binding', 'incomplete_counts', 'untyped', 'zero_exit',
                                  'missing_cleanup', 'unknown_native_cleanup', 'wrong_native_owner'])
def test_native_failure_requires_complete_recognized_qualified_evidence(tmp_path, fault):
    _, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    value = result(lifecycle, assignment, 'native_failed')
    check = value['checks'][0]
    if fault == 'missing_marker': value.pop('reviewed_host_validation')
    elif fault == 'wrong_mode': value['reviewed_host_validation']['mode'] = 'other'
    elif fault == 'forged_triage': value['reviewed_host_validation']['phase'] = 'triage'
    elif fault == 'review_failed': value['reviewed_host_validation']['review_verdict'] = 'fail'
    elif fault == 'review_findings': value['review_findings'] = [{'summary': 'not clean'}]
    elif fault == 'unrecognized': check['recognized'] = False
    elif fault in ('blocked', 'timeout', 'cancelled'): check['result'] = fault
    elif fault == 'zero_selected': check.update(selected=0, failed=0)
    elif fault == 'skipped_only': check.update(failed=0, skipped=1)
    elif fault == 'signal_exit': check['exit_code'] = -9
    elif fault == 'wrong_adapter': check['adapter'] = {'adapter_id': 'unqualified'}
    elif fault == 'missing_check': value['checks'] = []
    elif fault == 'resource_failure': check['resource_failure'] = {'reason': 'unknown gap'}
    elif fault == 'resource_breach': value['resource_limits']['breach'] = {'reason': 'limit'}
    elif fault == 'wrong_output_binding': value['initial_reports']['execution'] = ['other-output']
    elif fault == 'incomplete_counts': check['selected'] = 2
    elif fault == 'untyped': check.pop('kind')
    elif fault == 'zero_exit': check['exit_code'] = 0
    elif fault == 'missing_cleanup': check.pop('cleanup')
    elif fault == 'unknown_native_cleanup': check['cleanup']['stopped'] = False
    elif fault == 'wrong_native_owner': value['native_checks'] = ['foreign-native-check']
    assert complete(lifecycle, assignment, value)['disposition'] == 'held'
    assert len(lifecycle.assignments()) == 2


def test_duplicate_restart_and_alternate_receipts_do_not_repeat_triage(tmp_path):
    store, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    value = result(lifecycle, assignment, 'native_failed')
    def fault(stage):
        if stage == 'after_completion_commit': raise RuntimeError('SIMULATED crash')
    lifecycle._fault = fault
    with pytest.raises(RuntimeError, match='SIMULATED crash'):
        complete(lifecycle, assignment, value)
    lifecycle = Lifecycle(store)
    assert complete(lifecycle, assignment, value) == {'disposition': 'triage_required', 'duplicate': True}
    changed = deepcopy(value); changed['limits'] = 'changed'
    with pytest.raises(Rejected, match='different factual output'):
        complete(lifecycle, assignment, changed)
    assert complete(lifecycle, assignment, value, 'alternate-receipt')['disposition'] == 'historical'
    assert len(lifecycle.assignments()) == 3
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_event WHERE kind='native_failure_triage'").fetchone()[0] == 1


@pytest.mark.parametrize('retries', [0, 1, 2])
def test_triage_purpose_survives_exact_bounded_startup_retry_chain(tmp_path, retries):
    _, lifecycle = setup(tmp_path, retries=retries)
    _, _, triage = enter_triage(lifecycle)
    origin = lifecycle.assessment_context(triage['assignment_id'])['origin']
    for index in range(retries):
        observation = failure(lifecycle, triage)
        assert complete_startup(lifecycle, triage, observation)['disposition'] == 'startup_retry'
        triage = claim(lifecycle)
        assert lifecycle.assessment_context(triage['assignment_id'])['origin'] == origin
    assert complete(lifecycle, triage, result(lifecycle, triage, 'triage'), 'triage-result')['disposition'] == 'repair'


@pytest.mark.parametrize('fault', ['foreign_work', 'changed_receipt', 'wrong_disposition', 'missing_observation',
                                  'changed_successor', 'changed_candidate', 'changed_contract'])
def test_corrupt_retry_or_origin_history_never_becomes_ordinary_assessment(tmp_path, fault):
    store, lifecycle = setup(tmp_path, retries=1)
    _, _, triage = enter_triage(lifecycle)
    old = triage
    observation = failure(lifecycle, triage)
    assert complete_startup(lifecycle, triage, observation)['disposition'] == 'startup_retry'
    triage = claim(lifecycle)
    if fault == 'foreign_work':
        store.introduce('foreign-intro', 'foreign', 'SIMULATED foreign work', ['source'], TEAM)
    with store._transaction() as db:
        if fault == 'foreign_work': db.execute("UPDATE assignment SET work_id='foreign' WHERE assignment_id=?", (old['assignment_id'],))
        elif fault == 'changed_receipt': db.execute("UPDATE completion SET result='{}' WHERE receipt_id=?", (observation['receipt_id'],))
        elif fault == 'wrong_disposition': db.execute("UPDATE completion SET disposition='held' WHERE receipt_id=?", (observation['receipt_id'],))
        elif fault == 'missing_observation': db.execute("DELETE FROM lifecycle_event WHERE kind='startup_observation_received'")
        elif fault == 'changed_successor': db.execute("UPDATE lifecycle_event SET detail='{}' WHERE kind='startup_retry'")
        elif fault == 'changed_candidate': db.execute("UPDATE assignment SET candidate='{}' WHERE assignment_id=?", (old['assignment_id'],))
        elif fault == 'changed_contract': db.execute("UPDATE assignment SET contract_revision=9 WHERE assignment_id=?", (triage['assignment_id'],))
    with pytest.raises(Rejected):
        lifecycle.assessment_context(triage['assignment_id'])


def test_no_correction_allowance_does_not_launch_triage(tmp_path):
    _, lifecycle = setup(tmp_path, repairs=0)
    assignment = claim(lifecycle)
    assert complete(lifecycle, assignment, result(lifecycle, assignment, 'native_failed'))['disposition'] == 'held'
    assert len(lifecycle.assignments()) == 2


def test_pause_preserves_triage_successor_and_prior_approval(tmp_path):
    store, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    lifecycle.control('pause', WORK, 'pause')
    assert complete(lifecycle, assignment, result(lifecycle, assignment, 'native_failed'))['disposition'] == 'triage_required'
    assert lifecycle.snapshot(WORK)['execution'] == 'paused'
    successor = lifecycle.assignments()[-1]
    assert lifecycle.claim(successor['assignment_id'], 1) is None
    lifecycle.control('resume', WORK, 'resume')
    assert claim(lifecycle)['assignment_id'] == successor['assignment_id']
    assert len(store.read(WORK)['decisions']) == 1


@pytest.mark.parametrize('phase', ['review_rejected', 'native_failed'])
def test_unrelated_blocker_cannot_be_overridden_by_confirmed_finding(tmp_path, phase):
    _, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    with lifecycle.store._transaction() as db:
        lifecycle._block(db, WORK, 'unrelated', 'A separate exact ownership hold')
    assert complete(lifecycle, assignment, result(lifecycle, assignment, phase))['disposition'] == 'held'
    assert len(lifecycle.assignments()) == 2
    assert any(b['code'] == 'unrelated' for b in lifecycle.snapshot(WORK)['technical_blockers'])


def test_same_candidate_can_use_one_triage_in_each_distinct_correction_cycle(tmp_path):
    store, lifecycle = setup(tmp_path, repairs=2)
    for cycle in range(2):
        assessment = claim(lifecycle)
        value = result(lifecycle, assessment, 'native_failed')
        assert complete(lifecycle, assessment, value, 'failure-' + str(cycle))['disposition'] == 'triage_required'
        triage = claim(lifecycle)
        assert lifecycle.assessment_context(triage['assignment_id'])['origin']['cycle'] == cycle
        assert complete(lifecycle, triage, result(lifecycle, triage, 'triage'), 'triage-' + str(cycle))['disposition'] == 'repair'
        repair = claim(lifecycle)
        assert lifecycle.complete('repair-' + str(cycle), repair['assignment_id'], implementation())['disposition'] == 'assessment_required'
    final = claim(lifecycle)
    assert complete(lifecycle, final, result(lifecycle, final, 'validated'), 'final')['disposition'] == 'delivered'
    with store._connection() as db:
        assert [json.loads(row[0])['cycle'] for row in db.execute(
            "SELECT detail FROM lifecycle_event WHERE kind='native_failure_triage' ORDER BY event_id")] == [0, 1]


def test_unrecognized_assessment_predecessor_is_not_a_normal_review(tmp_path):
    store, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    with store._transaction() as db:
        db.execute("UPDATE assignment SET predecessor='invented-role' WHERE assignment_id=?", (assignment['assignment_id'],))
    with pytest.raises(Rejected, match='no intact'):
        lifecycle.assessment_context(assignment['assignment_id'])


@pytest.mark.parametrize('phase', ['native_failed', 'validated'])
def test_native_cleanup_contradiction_holds_even_with_positive_overall_cleanup(tmp_path, phase):
    _, lifecycle = setup(tmp_path)
    assignment = claim(lifecycle)
    value = result(lifecycle, assignment, phase)
    value['checks'][0]['cleanup']['effects_reconciled'] = False
    assert value['stopped'] is value['effects_reconciled'] is True
    assert complete(lifecycle, assignment, value)['disposition'] == 'held'
    assert lifecycle.assignments()[-1]['state'] == 'blocked_unknown_effects'
    assert lifecycle.snapshot(WORK)['delivery'] is None


def test_stale_triage_cycle_rejected_before_new_runtime_attempt(tmp_path):
    store, lifecycle = setup(tmp_path)
    _, _, triage = enter_triage(lifecycle)
    with store._transaction() as db:
        row = db.execute("SELECT event_id,detail FROM lifecycle_event WHERE kind='native_failure_triage'").fetchone()
        detail = json.loads(row['detail']); detail['cycle'] += 1
        db.execute('UPDATE lifecycle_event SET detail=? WHERE event_id=?', (canonical(detail), row['event_id']))
    with pytest.raises(Rejected, match='no intact exact completion'):
        lifecycle.assessment_context(triage['assignment_id'])


@pytest.mark.parametrize('retries', [0, 1, 2])
def test_exact_repair_handoff_reads_original_triage_evidence_after_cycle_advances(tmp_path, retries):
    store, lifecycle = setup(tmp_path, retries=retries)
    _, failed_result, triage = enter_triage(lifecycle)
    original_context = lifecycle.assessment_context(triage['assignment_id'])
    accepted = result(lifecycle, triage, 'triage')
    assert complete(lifecycle, triage, accepted, 'triage-result')['disposition'] == 'repair'
    with pytest.raises(Rejected, match='current frozen candidate'):
        lifecycle.assessment_context(triage['assignment_id'])
    repair = claim(lifecycle)
    for index in range(retries):
        observation = failure(lifecycle, repair)
        assert complete_startup(lifecycle, repair, observation)['disposition'] == 'startup_retry'
        repair = claim(lifecycle)
    context = Lifecycle(store).triage_context_for_repair(repair['assignment_id'], triage['assignment_id'])
    assert context['evidence_only'] is True
    assert context['origin'] == original_context['origin']
    assert context['failure_result'] == failed_result
    assert context['repair_assignment_id'] == repair['assignment_id']
    assert context['triage_receipt_id'] == 'triage-result'
    assert len(lifecycle.assignments()) == 4 + retries
    assert lifecycle.snapshot(WORK)['candidate'] is None


@pytest.mark.parametrize('fault', ['original_failure', 'accepted_origin', 'wrong_triage', 'wrong_disposition',
                                  'stale_cycle', 'changed_contract', 'changed_retry_link'])
def test_repair_handoff_rejects_corrupted_or_foreign_failure_provenance(tmp_path, fault):
    store, lifecycle = setup(tmp_path, retries=1)
    failed, failed_result, triage = enter_triage(lifecycle)
    assert complete(lifecycle, triage, result(lifecycle, triage, 'triage'), 'triage-result')['disposition'] == 'repair'
    repair = claim(lifecycle)
    if fault == 'changed_retry_link':
        observation = failure(lifecycle, repair)
        assert complete_startup(lifecycle, repair, observation)['disposition'] == 'startup_retry'
        repair = claim(lifecycle)
    with store._transaction() as db:
        if fault == 'original_failure':
            changed = deepcopy(failed_result)
            changed['initial_reports']['review']['sha256'] = 'f' * 64
            db.execute('UPDATE assignment SET result=? WHERE assignment_id=?', (canonical(changed), failed['assignment_id']))
            db.execute('UPDATE completion SET result=?,fingerprint=? WHERE receipt_id=?',
                       (canonical(changed), digest([failed['assignment_id'], changed]), 'assessment-result'))
        elif fault == 'accepted_origin':
            row = db.execute("SELECT result FROM completion WHERE receipt_id='triage-result'").fetchone()
            changed = json.loads(row[0]); changed['reviewed_host_validation']['triage_origin']['result_sha256'] = 'wrong'
            db.execute('UPDATE assignment SET result=? WHERE assignment_id=?', (canonical(changed), triage['assignment_id']))
            db.execute("UPDATE completion SET result=?,fingerprint=? WHERE receipt_id='triage-result'",
                       (canonical(changed), digest([triage['assignment_id'], changed])))
        elif fault == 'wrong_disposition':
            db.execute("UPDATE completion SET disposition='held' WHERE receipt_id='triage-result'")
        elif fault == 'stale_cycle': db.execute('UPDATE lifecycle SET cycle=0 WHERE work_id=?', (WORK,))
        elif fault == 'changed_contract': db.execute('UPDATE assignment SET contract_revision=9 WHERE assignment_id=?', (repair['assignment_id'],))
        elif fault == 'changed_retry_link': db.execute("UPDATE lifecycle_event SET detail='{}' WHERE kind='startup_retry'")
    with pytest.raises(Rejected):
        lifecycle.triage_context_for_repair(repair['assignment_id'], failed['assignment_id'] if fault == 'wrong_triage' else triage['assignment_id'])
