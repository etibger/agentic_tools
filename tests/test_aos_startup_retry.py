"""Simulated receipts exercise bounded recovery; no host or model jobs run."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest, now
from test_aos_finish_capacity import allowance, charge, journal, rows
from test_aos_lifecycle import CANDIDATE, approve, assessment, contract, implementation, setup as legacy_setup
from test_aos_store import TEAM

POLICY = 'isolated-checkout-v1'


def setup(tmp_path, *, retries=1, repairs=0, admitted=True, policy=POLICY, jobs=None, recipe_seconds=60):
    source = tmp_path / 'source'
    source.mkdir(parents=True)
    store = Store(tmp_path / 'state', (source,))
    jobs = jobs if jobs is not None else 2 + 2 * repairs + retries
    bounds = contract({'digest': 'not-self-referential'}, max_assignments=jobs,
                      max_repairs=repairs, max_startup_retries=retries)
    bounds.pop('concept_revision'); bounds.pop('concept_digest')
    recipe = {'source': str(source), 'max_seconds': recipe_seconds, 'execution_policy': policy,
              'native_write_roots': [{'path': str(tmp_path / 'generated')}]}
    budget = allowance(store.directory, max_assignments=jobs, max_seconds=jobs * 60)
    budget.pop('kind')
    bounds.update(execution_policy=policy, runtime_recipe=recipe,
                  runtime_recipe_digest=digest(recipe), product_budget=budget)
    team = [{**TEAM[0], 'name': name, 'role': role}
            for name, role in [('writer', 'Implementer'), ('assessor', 'Independent assessor')]]
    store.introduce('intro', 'RETRY', 'SIMULATED bounded startup recovery', ['source'], team)
    current = store.present('present', 'RETRY', 1, 'SIMULATED-DI', 1, 'SIMULATED recovery',
                            ['No model or host commands'], team, bounds['authority'], execution_envelope=bounds)
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'RETRY', {**bounds, 'concept_revision': 1, 'concept_digest': current['digest']})
    approve(store, current, 'RETRY')
    if admitted:
        assert lifecycle.admit('admit', 'RETRY')['execution'] == 'eligible'
    return store, lifecycle, current, budget, source


def claim(lifecycle, assignment=None):
    with lifecycle.store._transaction() as db:
        db.execute("INSERT OR REPLACE INTO worker_status VALUES(1,1,'SIMULATED',?,'running')", (now(),))
    assignment = assignment or next(a for a in lifecycle.assignments() if a['state'] == 'proposed')
    return lifecycle.claim(assignment['assignment_id'], 1)


def failure(lifecycle, assignment, *, kind='before_thread_request', receipt=None):
    receipt = receipt or 'startup-' + assignment['attempt_id']
    proof = {'kind': kind, 'attempt_id': assignment['attempt_id']}
    thread = None
    if kind == 'rejected':
        proof.update(method='thread/start', request_id='SIMULATED-request-1', code=-32603)
    if kind == 'acknowledged_no_turn_failure':
        thread = 'SIMULATED-created-thread'
        proof.update(method='thread/start', thread_id=thread, turn_started=False)
    with lifecycle.store._connection() as db:
        _, bounds = lifecycle._run(db, assignment['work_id'])
    result = {'status': 'failed', 'executor': assignment['executor'], 'attempt_id': assignment['attempt_id'],
              'error': 'SIMULATED known startup failure', 'stopped': True, 'owned_processes': [],
              'effects_reconciled': True, 'partial_mutations': {'status': 'none', 'changes': []},
              'output_mutations': {'status': 'none', 'changes': []}, 'startup_outcome': proof,
              'resource_limits': {'mode': 'endpoints', 'breach': None},
              'native_path_effects': {'observed': True, 'changes': {
                  row['path']: [] for row in bounds.get('runtime_recipe', {}).get('native_write_roots', [])}}}
    return {'state': 'startup_failed', 'attempt_id': assignment['attempt_id'], 'model': None, 'effort': None,
            'thread_id': thread, 'turn_id': None, 'model_calls_attempted': 0, 'simulated': True,
            'receipt_id': receipt, 'result': result}


def complete(lifecycle, assignment, observation):
    return lifecycle.complete(observation['receipt_id'], assignment['assignment_id'], observation['result'],
                              startup_observation=observation)


@pytest.mark.parametrize('kind', ['before_thread_request', 'rejected', 'acknowledged_no_turn_failure'])
def test_known_no_effect_startup_retries_once_and_retains_exact_failure(tmp_path, kind):
    store, lifecycle, _, budget, source = setup(tmp_path)
    assignment = claim(lifecycle)
    path = store.directory / 'codex-runtime.sqlite3'
    with journal(path) as db:
        charge(db, {**budget, 'kind': 'product'}, source, assignment['attempt_id'], following=2)
        original = rows(db)
    observation = failure(lifecycle, assignment, kind=kind)
    assert complete(lifecycle, assignment, observation) == {'disposition': 'startup_retry', 'duplicate': False}
    saved = lifecycle.snapshot('RETRY')
    assert saved['execution'] == 'eligible' and not saved['technical_blockers']
    old, new = saved['assignments']
    assert old['state'] == 'failed' and old['result'] == observation['result']
    assert new['kind'] == old['kind'] == 'implementation' and new['state'] == 'proposed'
    assert new['candidate'] == old['candidate'] is None
    assert new['predecessor'] == 'startup-retry:' + observation['receipt_id']
    assert all(r['state'] == 'reserved' and r['assignment_id'] is None for r in saved['resources'])
    with store._connection() as db:
        event = db.execute("SELECT detail FROM lifecycle_event WHERE kind='startup_observation_received'").fetchone()
        assert json.loads(event[0])['observation'] == observation
        assert db.execute('SELECT COUNT(*) FROM completion').fetchone()[0] == 1
    with sqlite3.connect(path) as db:
        assert rows(db) == original
    assert Lifecycle(store).startup_retries_used('RETRY') == 1


def test_duplicate_or_alternate_receipt_never_repeats_retry(tmp_path):
    store, lifecycle, _, _, _ = setup(tmp_path, retries=2)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    complete(lifecycle, assignment, observation)
    revision = store.read('RETRY')['revision']
    reopened = Lifecycle(store)
    assert complete(reopened, assignment, observation) == {'disposition': 'startup_retry', 'duplicate': True}
    assert store.read('RETRY')['revision'] == revision
    changed = deepcopy(observation)
    changed['detail'] = 'different full observation'
    with pytest.raises(Rejected, match='different factual output'):
        complete(reopened, assignment, changed)
    with pytest.raises(Rejected, match='different factual output'):
        reopened.complete(observation['receipt_id'], assignment['assignment_id'], observation['result'])
    alternate = failure(lifecycle, assignment, receipt='different-startup-receipt')
    assert complete(reopened, assignment, alternate)['disposition'] == 'historical'
    assert reopened.startup_retries_used('RETRY') == 1 and len(reopened.assignments()) == 2


@pytest.mark.parametrize('stage,committed', [('before_outbox_enqueue', False), ('after_completion_commit', True)])
def test_crash_boundary_replays_one_atomic_retry(tmp_path, stage, committed):
    store, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    def fault(point):
        if point == stage:
            raise RuntimeError('SIMULATED crash')
    lifecycle._fault = fault
    with pytest.raises(RuntimeError, match='SIMULATED crash'):
        complete(lifecycle, assignment, observation)
    reopened = Lifecycle(store)
    assert reopened.startup_retries_used('RETRY') == int(committed)
    assert complete(reopened, assignment, observation) == {'disposition': 'startup_retry', 'duplicate': committed}
    assert len(reopened.assignments()) == 2 and reopened.startup_retries_used('RETRY') == 1


@pytest.mark.parametrize('retries', [0, 1, 2])
def test_global_retry_allowance_never_becomes_unbounded_after_restart(tmp_path, retries):
    store, lifecycle, _, _, _ = setup(tmp_path, retries=retries)
    for index in range(retries + 1):
        assignment = claim(lifecycle)
        result = complete(lifecycle, assignment, failure(lifecycle, assignment))
        assert result['disposition'] == ('startup_retry' if index < retries else 'held')
        lifecycle = Lifecycle(store)
    assert lifecycle.startup_retries_used('RETRY') == retries
    assert len(lifecycle.assignments()) == retries + 1
    assert lifecycle.snapshot('RETRY')['execution'] == 'held'


@pytest.mark.parametrize('change', [
    ('observation', 'state', 'failed'), ('observation', 'attempt_id', 'other'),
    ('observation', 'model_calls_attempted', 1), ('observation', 'model_calls_attempted', False),
    ('observation', 'model_calls_attempted', None), ('observation', 'turn_id', 'started-turn'),
    ('observation', 'thread_id', ''), ('observation', 'model', 'wrong-model'),
    ('observation', 'effort', 'wrong-effort'), ('result', 'attempt_id', 'other'),
    ('result', 'executor', 'other'), ('result', 'stopped', False), ('result', 'stopped', 1),
    ('result', 'owned_processes', ['unknown']), ('result', 'effects_reconciled', False),
    ('result', 'partial_mutations', 'none'), ('result', 'partial_mutations', {'status': 'none'}),
    ('result', 'partial_mutations', {'status': 'recorded', 'changes': ['file']}),
    ('result', 'output_mutations', None), ('result', 'output_mutations', {'status': 'none', 'changes': ['log']}),
    ('result', 'native_path_effects', None), ('result', 'native_path_effects', {'observed': True, 'changes': {}}),
    ('result', 'native_path_effects', {'observed': False, 'changes': {}}),
    ('result', 'resource_limits', None), ('result', 'resource_limits', {}),
    ('result', 'resource_limits', {'breach': {'reason': 'limit'}}),
    ('result', 'resource_limits', {'breach': False}), ('result', 'native_checks', ['executed']),
    ('result', 'native_output_mutations', {'check': {'status': 'none', 'changes': []}}),
    ('proof', 'attempt_id', 'other'), ('proof', 'kind', 'unknown'), ('proof', 'kind', 'created_no_turn'),
])
def test_incomplete_or_contradictory_startup_evidence_holds(tmp_path, change):
    _, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    target = {'observation': observation, 'result': observation['result'],
              'proof': observation['result']['startup_outcome']}[change[0]]
    target[change[1]] = change[2]
    assert complete(lifecycle, assignment, observation)['disposition'] == 'held'
    assert lifecycle.startup_retries_used('RETRY') == 0 and len(lifecycle.assignments()) == 1


@pytest.mark.parametrize('kind,changes', [
    ('rejected', {'method': 'turn/start'}), ('rejected', {'request_id': ''}),
    ('rejected', {'code': False}), ('acknowledged_no_turn_failure', {'thread_id': 'other'}),
    ('acknowledged_no_turn_failure', {'turn_started': True}),
    ('acknowledged_no_turn_failure', {'method': 'turn/start'}),
])
def test_thread_rejection_and_acknowledgement_require_exact_proof(tmp_path, kind, changes):
    _, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment, kind=kind)
    observation['result']['startup_outcome'].update(changes)
    assert complete(lifecycle, assignment, observation)['disposition'] == 'held'
    assert lifecycle.startup_retries_used('RETRY') == 0


def test_native_path_change_and_missing_full_observation_are_not_retries(tmp_path):
    _, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    observation['result']['native_path_effects']['changes'][str(tmp_path / 'generated')] = ['new-file']
    assert complete(lifecycle, assignment, observation)['disposition'] == 'held'
    assert lifecycle.startup_retries_used('RETRY') == 0
    _, second, _, _, _ = setup(tmp_path / 'second')
    assignment = claim(second)
    observation = failure(second, assignment)
    assert second.complete(observation['receipt_id'], assignment['assignment_id'], observation['result'])['disposition'] == 'held'


def test_pause_keeps_retry_queued_without_dispatch_until_explicit_resume(tmp_path):
    _, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    lifecycle.control('pause', 'RETRY', 'pause')
    assert complete(lifecycle, assignment, failure(lifecycle, assignment))['disposition'] == 'startup_retry'
    assert lifecycle.snapshot('RETRY')['execution'] == 'paused'
    queued = next(a for a in lifecycle.assignments() if a['state'] == 'proposed')
    assert lifecycle.claim(queued['assignment_id'], 1) is None
    lifecycle.control('resume', 'RETRY', 'resume')
    assert lifecycle.claim(queued['assignment_id'], 1)['kind'] == 'implementation'


@pytest.mark.parametrize('fence', ['cancel', 'blocker', 'readback', 'scope', 'concept', 'deadline', 'missing-sent', 'resources'])
def test_control_authority_ownership_and_original_deadline_fence_retry(tmp_path, fence):
    store, lifecycle, _, _, _ = setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    if fence == 'cancel':
        lifecycle.control('cancel', 'RETRY', 'cancel')
    else:
        with store._transaction() as db:
            if fence == 'blocker':
                lifecycle._block(db, 'RETRY', 'retained', 'Unresolved cause')
            elif fence == 'readback':
                db.execute("UPDATE decision_readback SET status='failed'")
            elif fence == 'scope':
                db.execute("UPDATE assignment SET scope='{}' WHERE assignment_id=?", (assignment['assignment_id'],))
            elif fence == 'concept':
                db.execute("UPDATE work SET current_concept=current_concept+1 WHERE work_id='RETRY'")
            elif fence == 'deadline':
                db.execute('UPDATE assignment SET sent_at=? WHERE assignment_id=?',
                           ((datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(), assignment['assignment_id']))
            elif fence == 'missing-sent':
                db.execute('UPDATE assignment SET sent_at=NULL WHERE assignment_id=?', (assignment['assignment_id'],))
            elif fence == 'resources':
                db.execute("UPDATE resource SET state='unknown' WHERE work_id='RETRY'")
    outcome = complete(lifecycle, assignment, observation)
    assert outcome['disposition'] != 'startup_retry'
    assert lifecycle.startup_retries_used('RETRY') == 0 and len(lifecycle.assignments()) == 1


def test_assessment_retry_preserves_frozen_candidate_and_repair_cycle(tmp_path):
    store, lifecycle, _, _, _ = setup(tmp_path, repairs=1, retries=2)
    writer = claim(lifecycle)
    assert lifecycle.complete('written', writer['assignment_id'], implementation())['disposition'] == 'assessment_required'
    assessor = claim(lifecycle)
    assert complete(lifecycle, assessor, failure(lifecycle, assessor))['disposition'] == 'startup_retry'
    saved = lifecycle.snapshot('RETRY')
    assert saved['candidate'] == CANDIDATE
    retry = saved['assignments'][-1]
    assert retry['kind'] == 'assessment' and retry['candidate'] == CANDIDATE
    assert all(row['state'] == 'frozen' and row['assignment_id'] is None for row in saved['resources'])
    assessor = claim(lifecycle)
    finding = assessment(finding={'confirmed': True, 'in_scope': True, 'summary': 'SIMULATED correction'})
    finding['status'] = 'failed'
    assert lifecycle.complete('finding', assessor['assignment_id'], finding)['disposition'] == 'repair'
    repair = claim(lifecycle)
    assert complete(lifecycle, repair, failure(lifecycle, repair))['disposition'] == 'startup_retry'
    final = lifecycle.snapshot('RETRY')
    assert final['candidate'] is None and final['assignments'][-1]['kind'] == 'repair'
    with store._connection() as db:
        assert db.execute("SELECT cycle FROM lifecycle WHERE work_id='RETRY'").fetchone()[0] == 1
    assert lifecycle.startup_retries_used('RETRY') == 2


def test_legacy_contract_still_holds_known_failure_without_retry(tmp_path):
    _, lifecycle, _ = legacy_setup(tmp_path)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    assert complete(lifecycle, assignment, observation)['disposition'] == 'held'
    assert lifecycle.startup_retries_used('Q1') == 0


def test_expired_lower_recipe_ceiling_cannot_retry_within_larger_contract(tmp_path):
    store, lifecycle, _, _, _ = setup(tmp_path, recipe_seconds=30)
    assignment = claim(lifecycle)
    observation = failure(lifecycle, assignment)
    with store._transaction() as db:
        db.execute('UPDATE assignment SET sent_at=? WHERE assignment_id=?',
                   ((datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat(), assignment['assignment_id']))
    assert complete(lifecycle, assignment, observation)['disposition'] == 'held'
    assert lifecycle.startup_retries_used('RETRY') == 0
