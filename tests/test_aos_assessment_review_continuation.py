"""Real durable continuation transitions; model readiness/evidence production simulated."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agentic_tools.aos import assessment_continuation as previous
from agentic_tools.aos import assessment_review_continuation as review
from agentic_tools.aos import budgets, project, supervisor
from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.store import Rejected, canonical, digest, now
from agentic_tools.aos.worker import FakeRuntime
from test_aos_assessment_continuation import recovered
from test_aos_lifecycle import approve, assessment
from test_aos_project import setup as project_setup
from test_aos_supervisor import SimulatedService, answer


@pytest.fixture
def blocked_review(recovered, monkeypatch):
    p = recovered
    p.previous_packet = p.prepare()
    supervisor._retain(p.state / 'prepared-assessment-FIXTURE-fixture-continuation.json', p.previous_packet)
    target = previous.publish(p.lifecycle, p.previous_packet)
    p.approve_continuation(target)
    third = previous.admit(p.lifecycle, p.previous_packet)['assignment_id']
    (p.source / 'app.py').write_text('answer = 3\n')
    with p.store._transaction() as db:
        _, old = p.lifecycle._run(db, 'FIXTURE')
        db.execute("UPDATE assignment SET state='failed',terminal_at=? WHERE assignment_id=?", (now(), third))
        repair = p.lifecycle._enqueue(db, 'FIXTURE', 'repair', 'fixture-doc-repair')
        db.execute("UPDATE assignment SET state='completed',terminal_at=? WHERE assignment_id=?", (now(), repair))
        frozen = candidate(p.source, recipe=old['runtime_recipe'])
        from agentic_tools.aos import plan_runtime
        completed_repair = dict(db.execute('SELECT * FROM assignment WHERE assignment_id=?', (repair,)).fetchone())
        plan_runtime.advance_in(p.lifecycle, db, completed_repair, {'candidate': frozen,
            'output': 'SIMULATED:qualified repair freeze', 'owned_processes': [], 'effects_reconciled': True})
        db.execute("UPDATE lifecycle SET candidate=?,cycle=1,execution='held' WHERE work_id='FIXTURE'", (canonical(frozen),))
        failed = p.lifecycle._enqueue(db, 'FIXTURE', 'assessment', 'fixture-blocked-review')
        out = p.outputs / failed
        out.mkdir()
        report = {'candidate_identity': frozen['identity'], 'verdict': 'blocked', 'findings': [],
                  'limits': 'Cannot read the exact original Git baseline within the declared scope.'}
        report_path = out / 'review.txt'
        report_path.write_text(canonical(report))
        reference = {'path': str(report_path), 'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}
        result = {'status': 'failed', 'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                  'partial_mutations': {'status': 'none', 'changes': []}, 'checks': [], 'native_checks': [],
                  'review_findings': [], 'candidate': frozen, 'reviewed_host_validation': {
                      'mode': 'reviewed-host-v1', 'phase': 'review_rejected', 'review': reference, 'review_verdict': 'blocked'}}
        db.execute("UPDATE assignment SET state='failed',terminal_at=?,result=? WHERE assignment_id=?", (now(), canonical(result), failed))
        db.execute('INSERT INTO completion VALUES(?,?,?,?,?,?)', ('fixture-failed-receipt', failed, digest(result), canonical(result), 'held', now()))
        db.execute("UPDATE outbox SET state='acknowledged'")
        db.execute("DELETE FROM technical_blocker WHERE work_id='FIXTURE'")
        p.lifecycle._block(db, 'FIXTURE', 'assessment_failed', 'Rejected source review needs a concrete in-scope finding and no native execution')
        for resource in old['resources']:
            db.execute('INSERT OR REPLACE INTO resource VALUES(?,?,?,?,?)', (resource, 'FIXTURE', failed, 'frozen', now()))
        db.execute('INSERT INTO worker_status VALUES(1,1,?,?,?)', ('fixture-owner', now(), 'stopped'))
        p.lifecycle._touch(db, 'FIXTURE', 'fixture-blocked-review', {})
    allowance = budgets.approved_budget(old, p.state)
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        db.execute('CREATE TABLE native_check(attempt_id TEXT,record TEXT)')
        for attempt_id in (third, repair, failed):
            record = {'budget_id': allowance['budget_id'], 'budget_seconds': 60}
            if attempt_id == failed:
                record.update(phase='terminal', terminal_observation={'result': result},
                    stop_receipt={'stopped': True, 'effects_reconciled': True, 'owned_processes': [], 'native_checks': []})
            db.execute('INSERT INTO attempt VALUES(?,?)', (attempt_id, canonical(record)))
    p.descriptor = {'format': 'aos-source-review-descriptor-v1', 'path': str(p.control / 'source-evidence.json'),
        'sha256': 'a' * 64, 'work_id': 'FIXTURE', 'baseline_identity': old['baseline'],
        'candidate_identity': frozen['identity'], 'git_base': 'b' * 40}
    def validate_descriptor(descriptor, **kwargs):
        if descriptor != p.descriptor or candidate(p.source, recipe=kwargs['recipe']) != kwargs['candidate']:
            raise Rejected('fixture_evidence', 'Source evidence or candidate changed')
    monkeypatch.setattr(review.source_evidence, 'validate', validate_descriptor)
    monkeypatch.setattr(review.readiness, '_runtime_identity', lambda: {'runtime_source_digest': project.source_digest()})
    p.old_contract, p.final_candidate, p.failed, p.report_path = old, frozen, failed, report_path
    p.review_prepare = lambda: review.prepare(p.lifecycle, operation_id='fixture-review', work_id='FIXTURE',
        failed_assignment_id=failed, source_review_evidence=p.descriptor, readiness_runner=p.runner)
    return p


def test_final_assessment_needs_new_decision_and_preserves_every_charge(blocked_review):
    p = blocked_review
    packet = p.review_prepare()
    assert packet['ready'] is True
    assert packet['finish_capacity']['required_assignments'] == 1
    assert packet['finish_capacity']['lifecycle_assignments'] == 5
    assert packet['finish_capacity']['attempts_reserved'] == 4
    assert packet['finish_capacity']['seconds_reserved'] == 240
    before = p.lifecycle.assignments()
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        charges = db.execute('SELECT * FROM attempt ORDER BY id').fetchall()
    target = review.publish(p.lifecycle, packet)
    assert len(p.store.read('FIXTURE')['decisions']) == 2
    with pytest.raises(Rejected, match='approval'):
        review.admit(p.lifecycle, packet)
    approve(p.store, {'digest': target}, 'FIXTURE', operation='approve-final-review')
    admitted = review.admit(p.lifecycle, packet)
    assert review.admit(p.lifecycle, packet)['already_admitted']
    assert review.publish(p.lifecycle, packet) == target
    with p.store._connection() as db:
        run, contract = p.lifecycle._run(db, 'FIXTURE')
        assignment = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (admitted['assignment_id'],)).fetchone()
        assert p.lifecycle._assessment_lineage_in(db, assignment, contract, 1) == {'purpose': 'assessment'}
        assert run['cycle'] == 1
        assert json.loads(assignment['candidate']) == p.final_candidate
        assert contract['baseline'] == p.old_contract['baseline']
        assert contract['product_budget'] == p.old_contract['product_budget']
        assert (contract['max_assignments'], contract['max_repairs'], contract['max_startup_retries']) == (6, 1, 1)
    after = p.lifecycle.assignments()
    assert len(after) == 6
    assert all(row in after for row in before)
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        assert db.execute('SELECT * FROM attempt ORDER BY id').fetchall() == charges


def test_preparation_is_idempotent_and_does_not_publish(blocked_review):
    p = blocked_review
    packet = p.review_prepare()
    path = p.state / 'prepared-assessment-review-FIXTURE-fixture-review.json'
    supervisor._retain(path, packet)
    count = len(p.calls)
    assert p.review_prepare() == packet
    assert len(p.calls) == count
    assert len(p.store.read('FIXTURE')['decisions']) == 2
    assert len(p.lifecycle.assignments()) == 5


@pytest.mark.parametrize('field,value', [('read_roots', ['/']), ('checks', []), ('max_seconds', 120)])
def test_scope_change_cannot_hide_in_technical_continuation(blocked_review, field, value):
    p = blocked_review
    packet = p.review_prepare()
    packet['concept']['execution_envelope']['runtime_recipe'][field] = value
    with pytest.raises(Rejected, match='Only the exact'):
        review.validate(packet, p.original['input_bindings'], p.config, p.request)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'owned', 'foreign-assignment'])
def test_invalid_resource_ownership_rejected_before_readiness(blocked_review, mutation):
    p = blocked_review
    with p.store._transaction() as db:
        if mutation == 'missing':
            db.execute("DELETE FROM resource WHERE work_id='FIXTURE'")
        elif mutation == 'extra':
            db.execute('INSERT INTO resource VALUES(?,?,?,?,?)', ('extra', 'FIXTURE', None, 'frozen', now()))
        elif mutation == 'owned':
            db.execute("UPDATE resource SET state='owned'")
        else:
            db.execute("UPDATE resource SET assignment_id='other-assignment'")
    count = len(p.calls)
    with pytest.raises(Rejected, match='resources must remain frozen'):
        p.review_prepare()
    assert len(p.calls) == count


def test_no_native_or_report_drift_can_qualify(blocked_review):
    p = blocked_review
    packet = p.review_prepare()
    p.report_path.write_text('{}')
    with pytest.raises(Rejected, match='artifact changed'):
        review.publish(p.lifecycle, packet)


def test_supervisor_presents_new_review_packet_then_dispatches_only_assessor(blocked_review, monkeypatch):
    p = blocked_review
    packet = p.review_prepare()
    path = p.state / 'prepared-assessment-review-FIXTURE-fixture-review.json'
    supervisor._retain(path, packet)
    seen = []
    # Synthetic receipt acceptance is qualified independently by native-gate tests.
    monkeypatch.setattr(p.lifecycle.__class__, '_assessment_error', lambda *_, **__: None)
    monkeypatch.setattr(supervisor, '_runtime_identity', lambda: {'runtime_source_digest': project.source_digest()})
    cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
               'partial_mutations': {'status': 'none', 'changes': []}}
    result = assessment(candidate=p.final_candidate, **cleanup)
    result['checks'][0]['name'] = p.old_contract['required_checks'][0]
    reference = {'path': str(p.outputs / 'simulated-passing-review'), 'sha256': 'c' * 64}
    for check in result['checks']:
        check.update(native_check_id='simulated-native-check', cleanup=cleanup)
    result.update(runtime_turn_status='completed', resource_limits={'breach': None}, review_findings=[],
        native_checks=['simulated-native-check'],
        initial_reports={'execution': [c['output'] for c in result['checks']], 'review': reference, 'independent': True},
        reviewed_host_validation={'mode': 'reviewed-host-v1', 'phase': 'validated', 'review': reference, 'review_verdict': 'pass'})
    def runtime(lifecycle, recipe, work_id):
        assert recipe['source_review_evidence'] == p.descriptor
        selected = FakeRuntime(p.state / 'fixture-final-runtime.sqlite3', [{'kind': 'assessment', 'result': result}])
        original = selected.start
        def launch(assignment):
            seen.append(assignment['kind'])
            return original(assignment)
        selected.start = launch
        return selected
    progress = []
    def emit(value):
        progress.append(value)
        if value['state'] == 'pending':
            answer(p.store, value['decision_url'].split('challenge=')[1])
    final = supervisor.start(p.project_file, p.request_file, continuation_path=path, runtime_factory=runtime,
        service_factory=SimulatedService, emit=emit, interval=.01)
    assert final['state'] == 'delivered', final
    assert seen == ['assessment']
    assert sum(row['state'] == 'pending' for row in progress) == 1
    assert len(p.lifecycle.assignments()) == 6


def test_native_execution_disqualifies_technical_review_retry(blocked_review):
    p = blocked_review
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        db.execute('INSERT INTO native_check VALUES(?,?)', (p.failed, '{}'))
    count = len(p.calls)
    with pytest.raises(Rejected, match='native effects'):
        p.review_prepare()
    assert len(p.calls) == count


def test_resource_and_charge_drift_after_approval_cannot_allocate_last_slot(blocked_review):
    p = blocked_review
    packet = p.review_prepare()
    target = review.publish(p.lifecycle, packet)
    approve(p.store, {'digest': target}, 'FIXTURE', operation='approve-last-slot-drift')
    with p.store._transaction() as db:
        db.execute("UPDATE resource SET assignment_id=NULL WHERE work_id='FIXTURE'")
        p.lifecycle._touch(db, 'FIXTURE', 'fixture-resource-drift', {})
    with pytest.raises(Rejected, match='ownership changed'):
        review.admit(p.lifecycle, packet)
    assert len(p.lifecycle.assignments()) == 5
