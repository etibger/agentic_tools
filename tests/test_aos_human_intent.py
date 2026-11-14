"""Disposable evidence fixtures; no host transcript or live process authority."""
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from agentic_tools.aos import human_intent as intent
from agentic_tools.aos.coordinator_mcp import ControlOperation, schema_for
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.projection import collect, dashboard
from agentic_tools.aos.store import Rejected, Store, canonical
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import setup, runtime, count_attempts, implementation, assessment


def evidence(key, action, supersedes=(), scope=('Q1',), **changes):
    return {'instruction_id': key, 'human_reference': 'synthetic-human-message:' + key,
            'source_kind': 'direct_human', 'scope': list(scope), 'action': action,
            'supersedes': list(supersedes), 'provenance_available': True, 'context': 'original', **changes}


def enrolled(tmp_path, *, admitted=True):
    store, lifecycle, current = setup(tmp_path, admitted=admitted)
    cap = store.host_capability('synthetic trusted host', 'SIMULATED:original-input-inspected')
    intent.enable(lifecycle, 'Q1', 'fixture-owner', capability=cap)
    return store, lifecycle, current, cap


def record(lifecycle, cap, key, action, supersedes=(), scope=('Q1',), **changes):
    return intent.record(lifecycle, evidence(key, action, supersedes, scope, **changes), capability=cap)


def view(lifecycle, work_id='Q1'):
    return lifecycle.snapshot(work_id)['human_intent']


def test_stop_resume_stale_replay_and_genuinely_new_stop(tmp_path):
    store, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'stop-1', 'stop')
    lifecycle.control('pause-1', 'Q1', 'pause', instruction_id='stop-1')
    record(lifecycle, cap, 'resume-2', 'resume', ['stop-1'])
    lifecycle.control('resume-2', 'Q1', 'resume', instruction_id='resume-2')
    before = store.read('Q1')['revision']
    assert record(lifecycle, cap, 'stop-1', 'stop')['duplicate']
    for op in ('pause-1', 'fresh-stale-stop'):
        with pytest.raises(Rejected, match='superseded'):
            lifecycle.control(op, 'Q1', 'pause', instruction_id='stop-1')
    assert store.read('Q1')['revision'] == before
    assert lifecycle.snapshot('Q1')['execution'] == 'eligible'
    assert view(lifecycle)['controlling_instruction'] == 'resume-2'
    record(lifecycle, cap, 'stop-3', 'stop', ['resume-2'])
    lifecycle.control('pause-3', 'Q1', 'pause', instruction_id='stop-3')
    for ref in (None, 'resume-2', 'stop-3'):
        with pytest.raises(Rejected, match='human_intent'):
            lifecycle.control('bypass-' + str(ref), 'Q1', 'resume', instruction_id=ref)
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        assert worker.tick()['dispatched'] == 0
    assert count_attempts(fake) == 0
    assert view(lifecycle)['disposition'] == 'stop'
    assert [r['disposition'] for r in view(lifecycle)['history']].count('superseded') == 2


def test_scope_global_stop_narrow_resume_leaves_other_work_stopped(tmp_path):
    store, lifecycle, _, cap = enrolled(tmp_path, admitted=False)
    setup(tmp_path, store=store, work_id='OTHER', admitted=False)
    intent.enable(lifecycle, 'OTHER', 'fixture-owner', capability=cap)
    record(lifecycle, cap, 'global', 'stop', scope=('*',))
    record(lifecycle, cap, 'narrow', 'resume', ['global'])
    assert view(lifecycle)['disposition'] == 'running'
    assert view(lifecycle, 'OTHER')['disposition'] == 'stop'
    revision = store.read('Q1')['revision']
    record(lifecycle, cap, 'unrelated', 'stop', scope=('UNRELATED',))
    assert store.read('Q1')['revision'] == revision
    assert view(lifecycle)['controlling_instruction'] == 'narrow'
    assert lifecycle.admit('run-Q1', 'Q1')['execution'] == 'eligible'
    assert lifecycle.admit('held-OTHER', 'OTHER')['execution'] == 'held'


@pytest.mark.parametrize('kind', ['quote', 'history', 'status_question', 'other_chat_assertion'])
def test_nonhuman_content_cannot_be_recorded_as_authority(tmp_path, kind):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'current-stop', 'stop')
    with pytest.raises(ValidationError):
        record(lifecycle, cap, 'purported-resume', 'resume', ['current-stop'], source_kind=kind)
    assert view(lifecycle)['controlling_instruction'] == 'current-stop'
    with pytest.raises(Rejected, match='forbidden_decision'):
        intent.record(lifecycle, evidence('forged', 'resume'), capability=object())


def test_reordered_authorized_relays_hold_until_original_predecessor_arrives(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    relay = dict(source_kind='authorized_relay', relay_authorization='synthetic-human-relay-grant')
    record(lifecycle, cap, 'resume', 'resume', ['stop'], **relay)
    assert view(lifecycle)['disposition'] == 'ambiguous'
    assert 'Missing superseded evidence: stop' in view(lifecycle)['problems']
    record(lifecycle, cap, 'stop', 'stop', **relay)
    assert view(lifecycle)['disposition'] == 'running'
    assert record(lifecycle, cap, 'stop', 'stop', **relay)['duplicate']
    with pytest.raises(ValidationError):
        record(lifecycle, cap, 'unauthorized-relay', 'resume', source_kind='authorized_relay')
    assert record(lifecycle, cap, 'alias', 'stop', human_reference='synthetic-human-message:stop')['conflict']
    assert record(lifecycle, cap, 'stop', 'resume', **relay)['conflict']
    assert view(lifecycle)['disposition'] == 'ambiguous'


def test_reconstructed_context_reads_durable_authority_without_new_instruction(tmp_path):
    store, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'stop', 'pause')
    record(lifecycle, cap, 'resume', 'resume', ['stop'], context='reconstructed')
    lifecycle.reconcile('Q1')
    reopened = Lifecycle(Store(store.directory, (tmp_path / 'fixture',)))
    assert view(reopened) == view(lifecycle)
    assert intent.require_handoff(reopened, 'Q1', 'resume', 'fixture-owner')['disposition'] == 'running'
    # A summary with unavailable original evidence does not create authority.
    record(reopened, reopened.store.host_capability('synthetic host', 'SIMULATED'),
           'unknown', 'resume', ['resume'], context='reconstructed', provenance_available=False)
    assert view(reopened)['disposition'] == 'ambiguous'
    with pytest.raises(Rejected, match='human_intent'):
        intent.require_handoff(reopened, 'Q1', 'unknown', 'fixture-owner')
    record(reopened, reopened.store.host_capability('synthetic host', 'SIMULATED'),
           'clarification', 'resume', ['unknown'])
    assert view(reopened)['disposition'] == 'running'


def test_conflicting_streams_and_cycles_fail_closed(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'stop', 'stop')
    record(lifecycle, cap, 'resume', 'resume')  # no evidence of ordering
    assert view(lifecycle)['disposition'] == 'ambiguous'
    record(lifecycle, cap, 'resolved', 'resume', ['stop', 'resume'])
    assert view(lifecycle)['disposition'] == 'running'
    record(lifecycle, cap, 'cycle-a', 'resume', ['cycle-b'])
    with pytest.raises(Rejected, match='instruction_cycle'):
        record(lifecycle, cap, 'cycle-b', 'stop', ['cycle-a'])
    assert view(lifecycle)['disposition'] == 'ambiguous'


def test_missing_evidence_blocks_admission_but_no_history_is_fabricated(tmp_path):
    store, lifecycle, _, cap = enrolled(tmp_path, admitted=False)
    assert view(lifecycle)['history'] == []
    assert lifecycle.admit('missing', 'Q1')['execution'] == 'held'
    record(lifecycle, cap, 'current', 'resume')
    assert lifecycle.admit('reconciled', 'Q1')['execution'] == 'eligible'
    with store._connection() as db:
        row = db.execute('SELECT host,qualification FROM human_instruction').fetchone()
        assert tuple(row) == ('synthetic trusted host', 'SIMULATED:original-input-inspected')


def test_stop_between_claim_and_launch_and_active_stop_preserve_safety_fences(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        assignment = lifecycle.claim(lifecycle.assignments()[0]['assignment_id'], worker.epoch)
        assert lifecycle.can_launch_saved(assignment)
        record(lifecycle, cap, 'stop', 'stop', ['resume'])
        assert not lifecycle.can_launch_saved(assignment)
        assert lifecycle.assignment_authority_errors(assignment)
    assert count_attempts(fake) == 0
    # A human resume cannot erase an independent owned cleanup obligation.
    lifecycle.request_owned_stop(assignment['assignment_id'], 'synthetic-safety', 'unknown owned effects')
    record(lifecycle, cap, 'resume-again', 'resume', ['stop'])
    assert not lifecycle.can_launch_saved(assignment)
    assert lifecycle.pending_owned_stop(assignment['attempt_id'])


def test_worker_obeys_active_human_stop_without_cross_project_cancellation(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    fake = runtime(tmp_path, [{'kind': 'implementation', 'polls': 100, 'result': implementation()}])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        assert count_attempts(fake) == 1
        record(lifecycle, cap, 'stop', 'stop', ['resume'])
        assert worker.tick()['dispatched'] == 0
    assert count_attempts(fake) == 1
    saved = lifecycle.snapshot('Q1')
    assert saved['owned_stops'] and saved['owned_stops'][0]['state'] == 'stopped'


@pytest.mark.parametrize('kind', ['implementation', 'assessment'])
def test_terminal_result_after_stop_cannot_create_successor_or_handoff(tmp_path, kind):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    fake = runtime(tmp_path)
    with Worker(lifecycle, fake) as worker:
        first = lifecycle.claim(lifecycle.assignments()[0]['assignment_id'], worker.epoch)
        assignment = first
        if kind == 'assessment':
            lifecycle.complete('impl', first['assignment_id'], implementation())
            assignment = lifecycle.claim(lifecycle.assignments()[-1]['assignment_id'], worker.epoch)
        record(lifecycle, cap, 'stop', 'stop', ['resume'])
        result = lifecycle.complete('terminal', assignment['assignment_id'],
                                    assessment() if kind == 'assessment' else implementation())
        assert result['disposition'] == 'held'
        assert len(lifecycle.assignments()) == (2 if kind == 'assessment' else 1)
        assert lifecycle.snapshot('Q1')['delivery'] is None


def test_handoff_cannot_change_owner_and_status_names_owner_next_action(tmp_path):
    store, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    with pytest.raises(Rejected, match='handoff_owner'):
        intent.require_handoff(lifecycle, 'Q1', 'resume', 'different-chat')
    with pytest.raises(Rejected, match='handoff_owner'):
        intent.enable(lifecycle, 'Q1', 'different-chat', capability=cap)
    with store._transaction() as db:
        contract = json.loads(db.execute('SELECT contract FROM lifecycle').fetchone()[0])
        contract['receiving_owner'] = 'different-chat'
        db.execute('UPDATE lifecycle SET contract=?', (canonical(contract),))
        assert intent.errors_in(db, 'Q1')
    record(lifecycle, cap, 'stop', 'stop', ['resume'])
    status = dashboard(collect(store, 'Q1'))
    assert status['next']['owner'] == 'fixture-owner'
    assert 'explicit human resume' in status['next']['action']


def test_cli_control_consumes_same_current_instruction(tmp_path):
    store, lifecycle, current, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'stop', 'stop')
    command = [sys.executable, '-m', 'agentic_tools.aos.cli', 'pause', '--state-dir', str(store.directory),
               '--worker-writable-root', str(tmp_path / 'fixture'), '--operation-id', 'cli-pause',
               '--work-id', 'Q1', '--expected-work-revision', str(store.read('Q1')['revision']),
               '--digest', current['digest']]
    missing = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert missing.returncode != 0 and 'human_intent' in missing.stdout + missing.stderr
    saved = subprocess.run(command + ['--instruction-id', 'stop'], capture_output=True, text=True, timeout=15)
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert lifecycle.snapshot('Q1')['execution'] == 'paused'
    assert schema_for('pause') is ControlOperation
    assert ControlOperation.model_validate(dict(operation_id='x', work_id='Q1', expected_work_revision=1,
                                                digest=current['digest'], instruction_id='stop')).instruction_id == 'stop'


def test_technical_owner_interrupt_does_not_invent_human_history(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    lifecycle.pause_for_owner_interrupt('Q1')
    assert lifecycle.snapshot('Q1')['execution'] == 'paused'
    assert view(lifecycle)['controlling_instruction'] == 'resume'
    lifecycle.control('resume-after-interrupt', 'Q1', 'resume', instruction_id='resume')
    assert lifecycle.snapshot('Q1')['execution'] == 'eligible'


def test_declared_route_enrolls_before_admission_and_cannot_drop_stop(tmp_path, monkeypatch):
    import test_aos_lifecycle as fixture
    original = fixture.contract
    monkeypatch.setattr(fixture, 'contract', lambda *a, **kw: {**original(*a, **kw), 'human_intent_policy': intent.POLICY})
    store, lifecycle, _ = setup(tmp_path, admitted=False)
    assert view(lifecycle)['disposition'] == 'ambiguous'
    assert lifecycle.admit('first', 'Q1')['execution'] == 'held'
    cap = store.host_capability('host', 'SIMULATED')
    record(lifecycle, cap, 'stop', 'stop')
    with store._transaction() as db:
        contract = json.loads(db.execute('SELECT contract FROM lifecycle').fetchone()[0])
        contract.pop('human_intent_policy')
        db.execute('UPDATE lifecycle SET contract=?', (canonical(contract),))
        assert intent.errors_in(db, 'Q1')
    assert view(lifecycle)['disposition'] == 'stop'


def test_mcp_route_enforces_instruction_and_exposes_no_ingestion(tmp_path):
    import asyncio
    from mcp.shared.memory import create_connected_server_and_client_session
    from agentic_tools.aos.coordinator_mcp import create_coordinator_server
    store, lifecycle, current, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'stop', 'stop')

    async def check():
        server = create_coordinator_server(store, decision_origin='https://127.0.0.1:8443')
        async with create_connected_server_and_client_session(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert not any('instruction' in name or 'intent' in name for name in names)
            request = dict(operation_id='mcp-pause', work_id='Q1', expected_work_revision=store.read('Q1')['revision'],
                           digest=current['digest'])
            missing = await client.call_tool('coordinator_operation', {'operation': 'pause', 'request': request})
            assert missing.isError
            saved = await client.call_tool('coordinator_operation', {'operation': 'pause',
                                            'request': {**request, 'instruction_id': 'stop'}})
            assert not saved.isError
            record(lifecycle, cap, 'resume', 'resume', ['stop'])
            stale = await client.call_tool('coordinator_operation', {'operation': 'cancel',
                                            'request': {**request, 'instruction_id': 'stop'}})
            assert stale.isError
    asyncio.run(check())
    assert lifecycle.snapshot('Q1')['execution'] == 'paused'


def test_resume_after_worker_observation_prevents_stale_owned_stop(tmp_path, monkeypatch):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    fake = runtime(tmp_path, [{'kind': 'implementation', 'polls': 100, 'result': implementation()}])
    cancelled = []
    original_cancel = fake.cancel
    monkeypatch.setattr(fake, 'cancel', lambda attempt: (cancelled.append(attempt), original_cancel(attempt))[1])
    with Worker(lifecycle, fake) as worker:
        worker.tick()
        record(lifecycle, cap, 'stop', 'stop', ['resume'])
        original = lifecycle.assignment_authority_errors
        def read_then_resume(assignment):
            errors = original(assignment)
            if errors:
                record(lifecycle, cap, 'new-resume', 'resume', ['stop'])
            return errors
        monkeypatch.setattr(lifecycle, 'assignment_authority_errors', read_then_resume)
        worker.tick()
    assert cancelled == []
    assert lifecycle.snapshot('Q1')['owned_stops'] == []
    assert view(lifecycle)['disposition'] == 'running'


def test_conflicting_id_holds_until_explicit_resolution_and_corroboration_is_durable(tmp_path):
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    conflict = record(lifecycle, cap, 'resume', 'stop')
    assert conflict['conflict']
    assert view(lifecycle)['disposition'] == 'ambiguous'
    assert record(lifecycle, cap, 'resume', 'stop')['duplicate']
    record(lifecycle, cap, 'clarification', 'resume', ['resume', conflict['conflict_id']])
    assert view(lifecycle)['disposition'] == 'running'
    record(lifecycle, cap, 'uncertain', 'resume', ['clarification'], provenance_available=False)
    assert view(lifecycle)['disposition'] == 'ambiguous'
    assert record(lifecycle, cap, 'uncertain', 'resume', ['clarification'])['corroborated']
    assert view(lifecycle)['disposition'] == 'running'
    assert view(lifecycle)['history'][-1]['provenance_available'] is False
    assert 'uncertain' in view(lifecycle)['corroborated_instructions']


def test_enrolled_route_can_complete_without_duplicate_dispatch(tmp_path):
    from test_aos_lifecycle import run_to_end
    _, lifecycle, _, cap = enrolled(tmp_path)
    record(lifecycle, cap, 'resume', 'resume')
    fake = runtime(tmp_path)
    run_to_end(lifecycle, fake)
    run_to_end(lifecycle, fake)
    assert lifecycle.snapshot('Q1')['execution'] == 'delivered'
    assert count_attempts(fake) == 2
