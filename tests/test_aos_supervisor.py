"""Foreground continuation fixtures. All decisions/runtimes/listeners are SIMULATED.

Real HTTPS and model boundaries are qualified separately. These tests exercise
actual durable coordinator transitions and worker ownership without a model job.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import select
import signal
import sqlite3
import subprocess
import sys
import threading

import pytest

from agentic_tools.aos import supervisor
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store
from agentic_tools.aos.worker import FakeRuntime, Worker
from test_aos_lifecycle import implementation, assessment
from test_aos_project import setup as project_setup


class SimulatedService:
    """No listener; a separate HTTP suite verifies actual server mechanics."""
    def __init__(self, store, **kwargs):
        self.store, self.options = store, kwargs
        self.origin = 'https://127.0.0.1:8443'
        self.closed = False
        self.cleanup = {'listener_stopped': False, 'requests_stopped': False}
    def __enter__(self):
        self.listening = True
        return self
    def check(self):
        assert self.listening and not self.closed
    def close(self):
        self.closed = True
        self.cleanup = {'listener_stopped': True, 'requests_stopped': True}
        return self.cleanup


class SimulatedPowerLoss(BaseException):
    pass


def answer(store, challenge, outcome='accept'):
    capability = store.host_capability('fixture-host', 'SIMULATED supervisor decision fixture')
    return store.finish_approval(challenge, outcome, outcome == 'accept', capability=capability,
        exact_answer='SIMULATED explicit fixture answer', source_channel='simulated-host',
        interaction_reference='simulated-supervisor-interaction')


@pytest.fixture
def context(project_setup, monkeypatch):
    p = project_setup
    monkeypatch.setattr(supervisor, '_runtime_identity', lambda: {'runtime_source_digest': supervisor.project.source_digest()})
    p.prepares = 0
    p.answers = 0
    p.created = []
    p.services = []
    p.progress = []
    p.close_locked = []
    p.plan = [{'kind': 'implementation', 'result': implementation()},
              {'kind': 'assessment', 'result': assessment()}]
    # Native typed receipt validation has its own execution tests. This seam
    # allows synthetic assessment outputs while keeping real admission/controls.
    monkeypatch.setattr(Lifecycle, '_assessment_error', lambda *_: None)
    def prepare(*_):
        p.prepares += 1
        return p.run()
    def service(store, **options):
        selected = SimulatedService(store, **options)
        p.services.append(selected)
        return selected
    def runtime(lifecycle, recipe, work_id):
        with pytest.raises(Rejected, match='worker_owned'):
            with Worker(lifecycle, work_id=work_id):
                pytest.fail('Runtime constructed without the owner lock')
        selected = FakeRuntime(p.state / 'simulated-runtime.sqlite3', p.plan)
        def close():
            with pytest.raises(Rejected, match='worker_owned'):
                with Worker(lifecycle, work_id=work_id):
                    pytest.fail('Owner lock released before runtime close')
            p.close_locked.append(True)
        selected.close = close
        p.created.append(selected)
        return selected
    def emit(value):
        p.progress.append(copy.deepcopy(value))
        if value['state'] == 'pending':
            assert p.services[-1].listening
            store = p.services[-1].store
            challenge = value['decision_url'].split('challenge=')[1]
            answer(store, challenge)
            p.answers += 1
    p.prepare, p.service, p.runtime, p.emit = prepare, service, runtime, emit
    p.start = lambda **changes: supervisor.start(p.project_file, p.request_file,
        **{'prepare': prepare, 'service_factory': service, 'runtime_factory': runtime,
           'emit': emit, 'interval': .01, **changes})
    p.store = lambda: Store(p.state, (p.source, p.outputs))
    p.count = lambda: sqlite3.connect(p.state / 'simulated-runtime.sqlite3').execute('SELECT count(*) FROM attempt').fetchone()[0]
    return p


def test_once_approved_owner_delivers_without_chat_wakeup(context):
    p = context
    result = p.start()
    assert result['state'] == 'delivered', result
    assert p.prepares == p.answers == 1
    assert p.count() == 2 and p.close_locked == [True]
    assert all(p.services[0].cleanup.values())
    record = p.store().read('FIXTURE')
    assert record['lifecycle']['resources'] == []
    assert len(record['decisions']) == 1 and record['decisions'][0]['source_channel'] == 'simulated-host'
    assert p.services[0].options['work_id'] == 'FIXTURE'
    assert p.services[0].options['digest'] == record['concepts'][0]['digest']
    with Worker(Lifecycle(p.store()), work_id='FIXTURE'):
        pass


@pytest.mark.parametrize('stage', ['after_preparation', 'after_introduction', 'after_presentation',
    'after_registration', 'after_listener', 'after_challenge', 'after_admission'])
def test_restart_reuses_preparation_decision_and_admission(context, stage):
    p = context
    def crash(at):
        if at == stage:
            raise SimulatedPowerLoss(at)
    with pytest.raises(SimulatedPowerLoss):
        p.start(fault=crash)
    if stage == 'after_challenge':
        with p.store()._connection() as db:
            assert db.execute('SELECT count(*) FROM projection').fetchone()[0] == 0
    result = p.start()
    assert result['state'] == 'delivered', result
    assert p.prepares == p.answers == 1
    assert p.count() == 2
    with p.store()._connection() as db:
        assert db.execute('SELECT count(*) FROM challenge').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM decision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM assignment').fetchone()[0] == 2


def test_completed_restart_does_not_recheck_old_source_baseline(context):
    p = context
    assert p.start()['state'] == 'delivered'
    (p.source / 'app.py').write_text('answer = 2\n')  # Simulated authorized implementation.
    assert p.start()['state'] == 'delivered'
    assert p.prepares == p.answers == 1 and len(p.created) == 1 and p.count() == 2


def test_preparation_failure_has_no_concept_runtime_or_decision(context):
    p = context
    result = p.start(prepare=lambda *_: {'ready': False, 'blocker': {'code': 'fixture_not_ready', 'detail': 'SIMULATED missing check'}})
    assert result['state'] == 'held' and result['blocker'] == 'fixture_not_ready'
    assert p.created == p.services == [] and p.answers == 0
    with p.store()._connection() as db:
        for table in ('work', 'challenge', 'assignment'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    assert not (p.state / 'codex-runtime.sqlite3').exists()


def test_changed_kickoff_packet_is_held_without_preparation_or_reapproval(context):
    p = context
    assert p.start()['state'] == 'delivered'
    p.config['readiness']['max_seconds'] += 1
    p.save()
    result = p.start()
    assert result['state'] == 'held' and result['blocker'] == 'supervisor_packet'
    assert p.prepares == p.answers == 1 and p.count() == 2


def test_saved_pause_requires_explicit_resume_and_keeps_approval(context):
    p = context
    def stop_after_admit(stage):
        if stage == 'after_admission':
            raise SimulatedPowerLoss(stage)
    with pytest.raises(SimulatedPowerLoss):
        p.start(fault=stop_after_admit)
    store = p.store()
    lifecycle = Lifecycle(store)
    record = store.read('FIXTURE')
    lifecycle.control('fixture-pause', 'FIXTURE', 'pause', expected_work_revision=record['revision'],
                      expected_digest=record['concepts'][0]['digest'])
    assert p.start()['state'] == 'paused'
    assert p.answers == 1 and p.created == []
    record = store.read('FIXTURE')
    lifecycle.control('fixture-resume', 'FIXTURE', 'resume', expected_work_revision=record['revision'],
                      expected_digest=record['concepts'][0]['digest'])
    assert p.start()['state'] == 'delivered'
    assert p.prepares == p.answers == 1 and p.count() == 2


def test_listener_failure_precedes_challenge_and_same_start_reopens(context):
    p = context
    class FailedService(SimulatedService):
        def __enter__(self):
            raise Rejected('decision_port_in_use', 'SIMULATED occupied port')
    first = p.start(service_factory=FailedService)
    assert first['state'] == 'held' and p.answers == 0 and p.created == []
    with p.store()._connection() as db:
        assert db.execute('SELECT count(*) FROM challenge').fetchone()[0] == 0
    assert p.start()['state'] == 'delivered'
    assert p.prepares == p.answers == 1


@pytest.mark.parametrize('unknown', [False, True])
def test_cancellation_uses_saved_attempt_cleanup(context, unknown):
    p = context
    p.plan = [{'kind': 'implementation', 'result': implementation(), 'polls': 100,
               'unknown_effects': unknown}]
    def emit(value):
        p.emit(value)
        if value['state'] == 'running':
            store = p.store()
            record = store.read('FIXTURE')
            Lifecycle(store).control('fixture-cancel', 'FIXTURE', 'cancel',
                expected_work_revision=record['revision'], expected_digest=record['concepts'][0]['digest'])
    result = p.start(emit=emit)
    assert result['state'] == ('held' if unknown else 'cancelled'), result
    record = p.store().read('FIXTURE')
    assert bool(record['lifecycle']['resources']) is unknown
    assert p.prepares == p.answers == 1 and p.count() == 1
    if unknown:
        assert result['cleanup']['owned_stop']['known_stopped'] is False


def test_interrupt_stops_owned_runtime_but_does_not_clear_unknown_effects(context):
    p = context
    p.plan = [{'kind': 'implementation', 'result': implementation(), 'polls': 100,
               'unknown_effects': True}]
    stop = threading.Event()
    def emit(value):
        p.emit(value)
        if value['state'] == 'running':
            stop.set()
    first = p.start(emit=emit, stop_event=stop)
    assert first['state'] == 'held' and not first['cleanup']['owned_stop']['known_stopped']
    second = p.start()
    assert second['state'] == 'held'
    assert p.answers == p.prepares == p.count() == 1
    assert p.store().read('FIXTURE')['lifecycle']['resources']


def test_unknown_existing_work_is_not_adopted(context):
    p = context
    store = p.store()
    store.introduce(**p.request['introduction'])
    before = store.read('FIXTURE')
    result = p.start()
    after = store.read('FIXTURE')
    assert result['state'] == 'held' and result['blocker'] == 'supervisor_unbound'
    assert before['revision'] == after['revision'] and before['concepts'] == after['concepts']
    assert p.prepares == p.answers == 0 and not p.created


def test_separate_process_observes_saved_simulated_answer_while_chats_are_absent(context):
    p = context
    packet = p.run()
    assert packet['ready']
    supervisor._retain(p.state / 'prepared-FIXTURE.json', packet)
    plan_path = p.control / 'plan.json'
    plan_path.write_text(json.dumps(p.plan))
    script = '''import json,sys
from pathlib import Path
from agentic_tools.aos import supervisor
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.worker import FakeRuntime
class Service:
 def __init__(self,store,**kwargs): self.origin='https://127.0.0.1:8443'; self.cleanup={}
 def __enter__(self): return self
 def check(self): pass
 def close(self): self.cleanup={'listener_stopped':True,'requests_stopped':True}; return self.cleanup
Lifecycle._assessment_error=lambda *args: None
supervisor._runtime_identity=lambda: {'runtime_source_digest':supervisor.project.source_digest()}
plan=json.loads(Path(sys.argv[3]).read_text())
def runtime(lifecycle,recipe,work_id): return FakeRuntime(lifecycle.store.directory/'simulated-runtime.sqlite3',plan)
def forbidden(*args): raise AssertionError('Prepared restart must not rerun readiness')
result=supervisor.start(Path(sys.argv[1]),Path(sys.argv[2]),prepare=forbidden,runtime_factory=runtime,service_factory=Service,interval=.01)
print(json.dumps(result),flush=True)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(p.project_file), str(p.request_file), str(plan_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, 'SIMULATED owner failed to present its pending state'
        pending = json.loads(child.stdout.readline())
        assert pending['state'] == 'pending'
        # A duplicate owner cannot prepare or launch while the first waits.
        with pytest.raises(Rejected, match='worker_owned'):
            p.start(prepare=lambda *_: pytest.fail('Duplicate start reached readiness'))
        store = p.store()
        answer(store, pending['decision_url'].split('challenge=')[1])
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        result = json.loads(stdout.strip().splitlines()[-1])
        assert result['state'] == 'delivered', result
        assert p.count() == 2
        assert len(store.read('FIXTURE')['decisions']) == 1
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            child.communicate(timeout=10)
        assert child.poll() is not None


def test_wrapped_sqlite_contention_reads_same_saved_decision(context, monkeypatch):
    p = context
    verify = Store.verify_decision
    seen = []
    def transient(store, decision_id, *args, **kwargs):
        if not kwargs and not seen:
            seen.append(decision_id)
            failure = sqlite3.OperationalError('SIMULATED busy readback')
            failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise Rejected('decision_readback_failed', 'SIMULATED readback contention') from failure
        return verify(store, decision_id, *args, **kwargs)
    monkeypatch.setattr(Store, 'verify_decision', transient)
    result = p.start()
    assert result['state'] == 'delivered', result
    assert len(seen) == p.answers == p.prepares == 1
    assert len(p.store().read('FIXTURE')['decisions']) == 1


def test_invalid_decision_evidence_is_not_retried(context, monkeypatch):
    p = context
    verify = Store.verify_decision
    seen = []
    def invalid(store, decision_id, *args, **kwargs):
        if not kwargs:
            seen.append(decision_id)
            raise Rejected('decision_readback_failed', 'SIMULATED changed evidence') from ValueError('bad evidence')
        return verify(store, decision_id, *args, **kwargs)
    monkeypatch.setattr(Store, 'verify_decision', invalid)
    result = p.start()
    assert result['state'] == 'held' and len(seen) == 1
    assert p.answers == 1 and p.created == []


def test_changed_pinned_runtime_holds_before_reusing_readiness(context, monkeypatch):
    p = context
    def crash(stage):
        if stage == 'after_preparation':
            raise SimulatedPowerLoss(stage)
    with pytest.raises(SimulatedPowerLoss):
        p.start(fault=crash)
    monkeypatch.setattr(supervisor, '_runtime_identity', lambda: {
        'runtime_source_digest': supervisor.project.source_digest(), 'binary_sha256': 'changed-fixture-binary'})
    result = p.start()
    assert result['state'] == 'held' and result['blocker'] == 'supervisor_runtime_changed'
    assert p.prepares == 1 and p.answers == 0 and p.created == []


def test_deferring_pending_projection_does_not_bypass_corrupt_saved_projection(context):
    p = context
    def crash(stage):
        if stage == 'after_challenge':
            raise SimulatedPowerLoss(stage)
    with pytest.raises(SimulatedPowerLoss):
        p.start(fault=crash)
    store = p.store()
    Lifecycle(store).reconcile('FIXTURE')
    with store._transaction() as db:
        db.execute("UPDATE projection SET snapshot=? WHERE work_id='FIXTURE'", ('{"broken_fixture":true}',))
    result = p.start()
    assert result['state'] == 'held' and result['blocker'] == 'projection_mismatch'
    assert p.answers == 1 and p.created == []


def test_start_initializes_only_declared_fresh_directories_before_one_owned_readiness(context, monkeypatch):
    p = context
    p.state.rmdir()
    p.outputs.rmdir()
    generated = p.source / 'target'
    xdg = p.outputs / 'config'
    p.config['recipe']['native_write_roots'] = [{'path': str(generated), 'kind': 'generated'}]
    p.config['recipe']['checkout_policy']['native_environment'] = {'XDG_CONFIG_HOME': str(xdg)}
    p.save()
    initialize = supervisor.project.prepare_directories
    phases = []
    def checked(*args, **kwargs):
        phases.append(kwargs.get('state_only', False))
        if not kwargs.get('state_only'):
            with pytest.raises(Rejected, match='worker_owned'):
                with Worker(Lifecycle(p.store()), work_id='FIXTURE'):
                    pytest.fail('Output directory initialization must happen owned')
        result = initialize(*args, **kwargs)
        if kwargs.get('state_only'):
            assert p.state.is_dir()
            assert not p.outputs.exists() and not generated.exists() and not xdg.exists()
        return result
    monkeypatch.setattr(supervisor.project, 'prepare_directories', checked)
    def prepare(*args):
        with pytest.raises(Rejected, match='worker_owned'):
            with Worker(Lifecycle(p.store()), work_id='FIXTURE'):
                pytest.fail('Readiness must happen owned')
        return p.prepare(*args)
    result = p.start(prepare=prepare)
    assert result['state'] == 'delivered', result
    assert phases == [True, False]
    assert all(path.is_dir() and path.stat().st_mode & 0o777 == 0o700
               for path in (p.state, p.outputs, generated, xdg))
    assert p.prepares == len(p.calls) == p.answers == 1 and p.count() == 2


def test_duplicate_owner_cannot_initialize_outputs_or_enter_metadata_readiness(context, monkeypatch):
    p = context
    p.outputs.rmdir()
    lifecycle = Lifecycle(p.store())
    monkeypatch.setattr(supervisor.project, 'static_plan', lambda *args:
                        pytest.fail('Duplicate start must not begin directory planning or metadata readiness'))
    with Worker(lifecycle, work_id='FIXTURE', reconcile_on_enter=False):
        with pytest.raises(Rejected, match='worker_owned'):
            p.start(prepare=lambda *args: pytest.fail('Duplicate start reached readiness'))
    assert not p.outputs.exists()
    assert p.prepares == p.answers == 0 and p.calls == [] and p.services == []


def test_fresh_state_is_not_created_for_invalid_full_project_request(context):
    p = context
    p.state.rmdir()
    p.outputs.rmdir()
    p.request['concept']['authority'] = 'Invalid changed authority'
    p.save()
    result = p.start()
    assert result['state'] == 'held' and result['blocker'] == 'project_request'
    assert not p.state.exists() and not p.outputs.exists()
    assert p.prepares == p.answers == 0 and p.calls == [] and p.services == []


def test_restart_never_recreates_missing_retained_output_evidence(context, monkeypatch):
    p = context
    assert p.start()['state'] == 'delivered'
    p.outputs.rmdir()
    monkeypatch.setattr(supervisor.project, 'prepare_directories', lambda *args, **kwargs:
                        pytest.fail('Retained execution output must never be recreated'))
    result = p.start()
    assert result['state'] == 'held'
    assert not p.outputs.exists()
    assert p.prepares == p.answers == 1 and p.count() == 2



def _pause_when_running(p):
    paused = []
    def emit(value):
        p.emit(value)
        if value['state'] == 'running' and not paused:
            store = p.store()
            record = store.read('FIXTURE')
            Lifecycle(store).control('fixture-pause-active', 'FIXTURE', 'pause',
                expected_work_revision=record['revision'], expected_digest=record['concepts'][0]['digest'])
            paused.append(True)
    return emit


def test_active_writer_pause_drains_then_resumes_same_approval(context):
    p = context
    p.plan[0]['polls'] = 2
    result = p.start(emit=_pause_when_running(p))
    assert result['state'] == 'paused', result
    record = p.store().read('FIXTURE')
    assert [a['state'] for a in record['lifecycle']['assignments']] == ['completed', 'proposed']
    assert [a['kind'] for a in record['lifecycle']['assignments']] == ['implementation', 'assessment']
    assert record['lifecycle']['candidate'] is not None
    assert record['lifecycle']['owned_stops'] == record['lifecycle']['technical_blockers'] == []
    assert p.count() == p.answers == p.prepares == 1
    assert p.start()['state'] == 'paused' and p.count() == 1
    store = p.store()
    record = store.read('FIXTURE')
    Lifecycle(store).control('fixture-resume-drained', 'FIXTURE', 'resume',
        expected_work_revision=record['revision'], expected_digest=record['concepts'][0]['digest'])
    assert p.start()['state'] == 'delivered'
    assert p.count() == 2 and p.answers == p.prepares == 1


def test_active_final_assessor_pause_can_finish_delivery(context):
    p = context
    p.plan[1]['polls'] = 2
    result = p.start(emit=_pause_when_running(p))
    assert result['state'] == 'delivered', result
    record = p.store().read('FIXTURE')
    assert record['lifecycle']['owned_stops'] == record['lifecycle']['technical_blockers'] == []
    assert record['lifecycle']['resources'] == []
    assert p.count() == 2 and p.answers == p.prepares == 1


def _paused_sent_intent(p, *, seconds_ago=None):
    def crash(stage):
        if stage == 'after_admission':
            raise SimulatedPowerLoss(stage)
    with pytest.raises(SimulatedPowerLoss):
        p.start(fault=crash)
    store = p.store()
    lifecycle = Lifecycle(store)
    with Worker(lifecycle, work_id='FIXTURE', reconcile_on_enter=False) as owner:
        pending = owner.assignments()[0]
        lifecycle.claim(pending['assignment_id'], owner.epoch)
        if seconds_ago is not None:
            sent_at = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
            with store._transaction() as db:
                db.execute('UPDATE assignment SET sent_at=? WHERE assignment_id=?', (sent_at, pending['assignment_id']))
        record = store.read('FIXTURE')
        lifecycle.control('fixture-pause-sent', 'FIXTURE', 'pause', expected_work_revision=record['revision'],
                          expected_digest=record['concepts'][0]['digest'])
        return owner.assignments()[0]


def test_paused_definitively_absent_intent_is_retained_without_start_cancel_or_clock_reset(context):
    p = context
    sent = _paused_sent_intent(p)
    result = p.start()
    assert result['state'] == 'paused', result
    assert result['cleanup']['owned_stop']['known_stopped'] is True
    assert result['cleanup']['owned_stop']['unlaunched_intents'] == [sent['attempt_id']]
    record = p.store().read('FIXTURE')
    retained = record['lifecycle']['assignments'][0]
    assert retained['state'] == 'sent' and retained['sent_at'] == sent['sent_at']
    assert record['lifecycle']['owned_stops'] == record['lifecycle']['technical_blockers'] == []
    assert p.count() == 0 and p.answers == p.prepares == 1
    store = p.store()
    Lifecycle(store).control('fixture-resume-sent', 'FIXTURE', 'resume',
        expected_work_revision=record['revision'], expected_digest=record['concepts'][0]['digest'])
    assert p.start()['state'] == 'delivered'
    after = store.read('FIXTURE')['lifecycle']['assignments']
    assert len(after) == p.count() == 2
    assert after[0]['attempt_id'] == sent['attempt_id'] and after[0]['sent_at'] == sent['sent_at']
    assert p.answers == p.prepares == 1


def test_paused_absent_intent_still_holds_after_narrower_recipe_deadline(context):
    p = context
    p.config['recipe']['max_seconds'] = 1
    p.config['recipe']['checks'][0]['timeout_seconds'] = 1
    p.save()
    sent = _paused_sent_intent(p, seconds_ago=2)
    result = p.start()
    assert result['state'] == 'held' and result['blocker'] == 'assignment_deadline', result
    assert result['cleanup']['owned_stop']['unlaunched_intents'] == []
    record = p.store().read('FIXTURE')
    assert record['lifecycle']['assignments'][0]['sent_at'] == sent['sent_at']
    assert record['lifecycle']['owned_stops'][0]['reason'] == 'assignment_deadline'
    assert p.count() == 0 and p.answers == p.prepares == 1


def test_paused_unknown_sent_intent_is_never_preserved_as_absent(context):
    p = context
    _paused_sent_intent(p)
    def runtime(lifecycle, recipe, work_id):
        selected = p.runtime(lifecycle, recipe, work_id)
        selected.lookup = lambda aid: {'state': 'unknown', 'attempt_id': aid, 'definitive': False}
        selected.cancel = lambda aid: {'attempt_id': aid, 'stopped': False, 'owned_processes': None,
                                      'effects_reconciled': False, 'partial_mutations': 'unknown'}
        return selected
    result = p.start(runtime_factory=runtime)
    assert result['state'] == 'held' and not result['cleanup']['owned_stop']['known_stopped']
    assert result['cleanup']['owned_stop']['unlaunched_intents'] == []
    assert p.store().read('FIXTURE')['lifecycle']['resources']
    assert p.count() == 0 and p.answers == p.prepares == 1
