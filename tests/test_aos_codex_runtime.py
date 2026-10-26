"""Deterministic adapter tests; scripted RPC is never real model qualification."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos.codex_runtime import (CodexRuntime, FEATURES, PROFILE, StdioRPC, candidate,
                                             changed, clean_environment, inventory, process_groups)
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import contract
from test_aos_store import TEAM


class Process:
    pid = 99999999
    exited = False

    def poll(self):
        return 0 if self.exited else None


class ScriptedRPC:
    def __init__(self, backend, binary, overrides, cwd):
        self.backend = backend
        self.closed = False
        self.events = []
        self.proc = Process()
        self.config = {}
        self.cwd = str(cwd)
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict):
                    merge(a.setdefault(k, {}), v)
                else:
                    a[k] = v
        for item in overrides:
            if item.startswith(('mcp_servers.', 'plugins.')):
                table, name, _ = item.split('.', 2)
                self.config.setdefault(table, {})[name] = {'enabled': False}
            else:
                merge(self.config, tomllib.loads(item))
        self.config.setdefault('mcp_servers', {})
        self.config.setdefault('plugins', {})
        backend.clients.append(self)

    def initialize(self):
        return {'serverInfo': {'version': '0.163.0'}}

    def request(self, method, params=None):
        self.backend.calls.append((method, copy.deepcopy(params)))
        if method == 'config/read':
            if self.backend.bad_config:
                self.config['features']['apps'] = True
            return {'config': self.config}
        if method == 'thread/start':
            tid = 'thread-' + str(len(self.backend.threads))
            self.backend.threads[tid] = {'id': tid, 'cwd': self.cwd, 'turns': []}
            if self.backend.lost_thread_ack:
                raise Rejected('lost_ack', 'scripted missing thread acknowledgement')
            return {'thread': {'id': tid}, 'model': self.backend.model,
                    'reasoningEffort': 'high', 'approvalPolicy': 'never', 'cwd': self.cwd}
        if method == 'turn/start':
            turn = {'id': 'turn-' + params['threadId'], 'status': 'inProgress', 'items': []}
            self.backend.threads[params['threadId']]['turns'].append(turn)
            if self.backend.lost_turn_ack:
                raise Rejected('lost_ack', 'scripted missing turn acknowledgement')
            return {'turn': turn}
        if method == 'thread/read':
            if self.backend.read_results:
                value = self.backend.read_results.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value
            return {'thread': copy.deepcopy(self.backend.threads[params['threadId']])}
        if method == 'thread/resume':
            return {}
        if method == 'thread/backgroundTerminals/list':
            return {'data': copy.deepcopy(self.backend.jobs)}
        if method == 'thread/backgroundTerminals/terminate':
            self.backend.jobs = [j for j in self.backend.jobs if j['processId'] != params['processId']]
            return {}
        if method == 'turn/interrupt':
            self.backend.threads[params['threadId']]['turns'][0]['status'] = 'interrupted'
            return {}
        raise AssertionError(method)

    def drain_events(self):
        values, self.events = self.events, []
        return values

    def close(self):
        self.closed = True
        self.proc.exited = True


class Backend:
    def __init__(self):
        self.clients = []
        self.calls = []
        self.threads = {}
        self.jobs = []
        self.bad_config = self.lost_thread_ack = self.lost_turn_ack = False
        self.model = 'gpt-6.1-sol'
        self.read_results = []

    def factory(self, *args):
        return ScriptedRPC(self, *args)

    def finish(self, tid, status='completed'):
        self.threads[tid]['turns'][0]['status'] = status

    def count(self, name):
        return sum(m == name for m, _ in self.calls)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir()
    outputs.mkdir()
    (source / 'example.py').write_text('def add(a,b):\n return a-b\n')
    (source / 'test_example.py').write_text('from example import add\ndef test_add():\n assert add(2,3)==5\n')
    recipe = {'source': str(source), 'outputs': str(outputs), 'read_roots': [str(Path(sys.base_prefix).resolve())],
              'max_seconds': 60, 'requires_detached_processes': False,
              'mutable': ['example.py'], 'immutable': {'test_example.py': inventory(source)['test_example.py']},
              'implementation_prompt': 'Fix add; only example.py is writable. No detached processes.',
              'assessment_prompt': 'Review frozen source. Write review-<Attempt>.txt in outputs. No detached processes.',
              'checks': [{'name': 'fixture', 'argv': [sys.executable, '-m', 'pytest', '-q', 'test_example.py'],
                          'timeout_seconds': 10, 'scope': 'add behavior and fixture consumer'}]}
    store = Store(tmp_path / 'state', (source, outputs))
    store.introduce('intro', 'Q2', 'Scripted adapter boundary', ['fixture'], TEAM)
    bounds = contract({'digest': 'not-self-referential'})
    bounds.pop('concept_digest')
    bounds.pop('concept_revision')
    bounds['runtime_recipe_digest'] = digest(recipe)
    bounds['baseline'] = candidate(source)['identity']
    bounds['shared_qualification_budget'] = {'budget_id': 'aos-q2q3-runtime-v1',
                                           'ledger_directory': str(store.directory.resolve()),
                                           'max_assignments': 8, 'max_seconds': 2400}
    team = [{**TEAM[0], 'name': person, 'role': role} for person, role in [('writer', 'Implementer'), ('assessor', 'Independent assessor')]]
    concept = store.present('present', 'Q2', 1, 'Q2-DI', 1, 'Scripted fixture', ['native add check'], team,
                            bounds['authority'], execution_envelope=bounds)
    cap = store.host_capability('test-host', 'SIMULATED:adapter')
    challenge = store.begin_approval('approval', 'Q2', store.read('Q2')['revision'], concept['digest'], capability=cap)
    store.finish_approval(challenge['challenge_id'], 'accept', True, capability=cap,
                          exact_answer='Approve exact scripted fixture', source_channel='simulated-host',
                          interaction_reference='scripted-interaction')
    lifecycle = Lifecycle(store)
    lifecycle.register('register', 'Q2', contract(concept, baseline=bounds['baseline']))
    lifecycle.admit('admit', 'Q2')
    backend = Backend()
    runtime = CodexRuntime(lifecycle, {'Q2': recipe}, transport_factory=backend.factory)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes', lambda: {})
    monkeypatch.setattr(runtime, '_watch', lambda *_args: None)
    monkeypatch.setattr(runtime, '_probe_sandbox', lambda *_args: {'SIMULATED': True})
    yield SimpleNamespace(source=source, outputs=outputs, store=store, lifecycle=lifecycle, recipe=recipe,
                          runtime=runtime, backend=backend)
    runtime.close()


def claim(p):
    with Worker(p.lifecycle, p.runtime) as worker:
        return p.lifecycle.claim(p.lifecycle.assignments()[0]['assignment_id'], worker.epoch)


def test_clean_environment_drops_inherited_secrets(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('GITLAB_GEO_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('CODEX_HOME', '/untrusted')
    environment = clean_environment()
    assert set(environment) == {'HOME', 'PATH', 'LANG', 'PYTHONDONTWRITEBYTECODE'}
    assert 'synthetic-secret' not in str(environment)


def test_source_inventory_rejects_symlinks_and_observes_new_deleted_files(tmp_path):
    (tmp_path / 'file').write_text('one')
    before = inventory(tmp_path)
    (tmp_path / 'file').unlink()
    (tmp_path / 'new').write_text('two')
    assert changed(before, inventory(tmp_path))['changes'] == ['file', 'new']
    (tmp_path / 'link').symlink_to(tmp_path / 'new')
    with pytest.raises(Rejected, match='link'):
        inventory(tmp_path)


def test_recipe_must_be_bound_to_exact_approved_envelope(prepared):
    p = prepared
    p.recipe['implementation_prompt'] += ' modified'
    with pytest.raises(Rejected, match='exact approved'):
        p.runtime.start(claim(p))
    assert p.backend.count('turn/start') == 0


def test_effective_config_rejects_inherited_tool_before_model(prepared):
    p = prepared
    p.backend.bad_config = True
    result = p.runtime.start(claim(p))
    assert result['state'] == 'unknown'
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0


def test_model_mismatch_never_starts_turn(prepared):
    p = prepared
    p.backend.model = 'wrong-model'
    result = p.runtime.start(claim(p))
    assert result['state'] == 'unknown'
    assert result['model'] == 'wrong-model'
    assert p.backend.count('turn/start') == 0


def test_lost_thread_ack_remains_held_never_repeated(prepared):
    p = prepared
    p.backend.lost_thread_ack = True
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert p.backend.count('thread/start') == 1
    assert p.backend.count('turn/start') == 0


def test_lost_turn_ack_recovers_exact_single_turn_without_relaunch(prepared):
    p = prepared
    p.backend.lost_turn_ack = True
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    recovered = p.runtime.lookup(assignment['attempt_id'])
    assert recovered['state'] == 'active' and recovered['turn_id'] == 'turn-thread-0'
    assert p.runtime.start(assignment)['turn_id'] == 'turn-thread-0'
    assert p.backend.count('turn/start') == 1


def test_intent_without_turn_and_missing_turn_are_unknown(prepared):
    p = prepared
    def crash(stage):
        if stage == 'before_turn_request':
            raise OSError('scripted crash before request')
    p.runtime._fault = crash
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.backend.count('turn/start') == 0


def test_extra_turn_identity_never_advances(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    p.backend.threads['thread-0']['turns'].append({'id': 'unknown', 'status': 'completed'})
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'


def test_native_source_identity_replaces_agent_success_claim(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
    p.backend.finish('thread-0')
    result = p.runtime.poll(assignment['attempt_id'])
    assert result['state'] == 'completed'
    assert result['result']['candidate'] == candidate(p.source)
    assert result['result']['partial_mutations']['changes'] == ['example.py']
    assert result['result']['owned_processes'] == []
    assert p.runtime.poll(assignment['attempt_id']) == result


@pytest.mark.parametrize('drift', ['test_example.py', 'unexpected.py'])
def test_immutable_or_out_of_scope_source_drift_is_held(prepared, drift):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    (p.source / drift).write_text('unauthorized change')
    p.backend.finish('thread-0')
    outcome = p.runtime.poll(assignment['attempt_id'])
    assert outcome['state'] == 'failed'
    assert outcome['result']['effects_reconciled'] is True


def test_exact_owned_terminal_cleanup_not_interruption_alone(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    p.backend.jobs = [{'processId': 'owned-1'}, {'processId': 'owned-2'}]
    result = p.runtime.cancel(assignment['attempt_id'])
    assert result['stopped'] and result['effects_reconciled'] and result['owned_processes'] == []
    terms = [params for name, params in p.backend.calls if name == 'thread/backgroundTerminals/terminate']
    assert terms == [{'threadId': 'thread-0', 'processId': 'owned-1'}, {'threadId': 'thread-0', 'processId': 'owned-2'}]
    assert p.backend.count('turn/interrupt') == 1
    assert p.backend.count('thread/backgroundTerminals/clean') == 0


def test_unknown_cancel_never_releases_ownership(prepared):
    assert prepared.runtime.cancel('unknown-attempt')['effects_reconciled'] is False


def test_surviving_process_identity_is_held(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    record['observed_processes'] = {'123': 'same-start'}
    p.runtime._save(record)
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes', lambda: {123: (1, 'same-start')})
    # Bound this negative test without waiting three seconds.
    ticks = iter([0, 4, 8])
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.time.monotonic', lambda: next(ticks))
    stop = p.runtime.cancel(assignment['attempt_id'])
    assert not stop['effects_reconciled'] and stop['owned_processes'] == ['123']


def test_usage_replay_and_reset_do_not_inflate_total(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    client = p.backend.clients[0]
    def event(total):
        return {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-0', 'turnId': 'turn-thread-0',
                'tokenUsage': {'total': {'inputTokens': total, 'outputTokens': 2, 'totalTokens': total+2}}}}
    client.events = [event(100), event(100), event(4)]
    p.runtime._events(record, client)
    assert len(record['usage_raw']) == 2
    assert record['usage']['tokens']['totalTokens'] == 102
    assert record['usage']['partial'] is True
    assert record['usage']['billing'] == 'unavailable' and record['usage']['cost'] is None


@pytest.mark.parametrize('review_verdict,findings,expected', [
    ('pass', [], 'delivered'), ('fail', ['remaining source defect'], 'held'),
    ('blocked', [], 'held'), ('pass', ['contradictory finding'], 'held')])
def test_worker_handoff_and_idle_reconcile_need_no_model_calls(prepared, monkeypatch, review_verdict, findings, expected):
    p = prepared
    with Worker(p.lifecycle, p.runtime) as worker:
        first = worker.tick()
        assert first['model_calls'] == 1
        assert worker.tick()['model_calls'] == 0
        (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
        p.backend.finish('thread-0')
        handoff = worker.tick()
        assert handoff['dispatched'] == handoff['model_calls'] == 1
        assessment = p.lifecycle.assignments()[-1]
        assert assessment['kind'] == 'assessment'
        (p.outputs / assessment['attempt_id'] / ('review-' + assessment['attempt_id'] + '.txt')).write_text(json.dumps(
            {'candidate_identity': candidate(p.source)['identity'], 'verdict': review_verdict, 'findings': findings, 'limits': 'Scripted review only'}))
        def check(recipe, descriptor, output, executor, frozen):
            # Scripted native executor seam only; actual sandbox is separately qualified.
            saved = p.runtime._get(recipe['_attempt_id'])
            assert saved.get('model_stop_receipt') and not saved.get('stop_receipt')
            check_id = 'nc-scripted-' + recipe['_attempt_id']
            cleanup = {'effects_reconciled': True, 'owned_processes': [], 'stopped': True,
                       'output_mutations': {'status': 'none', 'changes': []}}
            row = {'check_id': check_id, 'attempt_id': recipe['_attempt_id'], 'phase': 'clean',
                   'owner': {'pid': 99999991, 'started': 'scripted-owner'}, 'members': {}, 'cleanup': cleanup}
            with p.runtime._db() as db:
                db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, row['attempt_id'], canonical(row)))
            return {'name': 'fixture', 'command': ['python', '-m', 'pytest'], 'environment': 'SIMULATED',
                    'executor': executor, 'selected': 1, 'result': 'pass', 'output': 'SIMULATED:check',
                    'candidate': frozen, 'scope': 'fixture', 'limits': 'scripted',
                    'native_check_id': check_id, 'cleanup': cleanup}
        monkeypatch.setattr(p.runtime, '_check', check)
        p.backend.finish('thread-1')
        assert worker.tick()['model_calls'] == 0
        final = p.runtime._get(assessment['attempt_id'])
        assert final['stop_receipt']['native_checks']
        assert final['stop_receipt']['observed_at'] >= final['model_stop_receipt']['observed_at']
    assert p.lifecycle.snapshot('Q2')['execution'] == expected
    assert p.backend.count('turn/start') == 2


def test_reopen_journal_recovers_turn_without_start(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    reopened = CodexRuntime(p.lifecycle, {'Q2': p.recipe}, transport_factory=p.backend.factory)
    monkeypatch.setattr(reopened, '_watch', lambda *_: None)
    monkeypatch.setattr(reopened, '_probe_sandbox', lambda *_: {'SIMULATED': True})
    try:
        assert reopened.lookup(assignment['attempt_id'])['state'] == 'active'
        assert p.backend.count('turn/start') == 1 and reopened.model_calls == 0
    finally:
        reopened.close()


def empty_metadata_error(thread_id='thread-0'):
    path = '/retained/rollout-2026-10-25T12-22-49-' + thread_id + '.jsonl'
    return Rejected('runtime_rpc', 'thread/read failed: failed to read thread: thread-store internal error: '
                    'failed to read session metadata ' + path + ': rollout at ' + path + ' is empty')


@pytest.mark.parametrize('delayed_reads', [1, 3])
def test_acknowledged_metadata_gap_waits_for_same_turn_and_retains_errors(prepared, delayed_reads):
    p = prepared
    assignment = claim(p)
    acknowledged = p.runtime.start(assignment)
    p.backend.read_results = [empty_metadata_error() for _ in range(delayed_reads)]
    deadline = None
    for _ in range(delayed_reads):
        waiting = p.runtime.start(assignment)  # Saved Start is lookup only.
        assert waiting['state'] == 'waiting'
        assert waiting['thread_id'] == acknowledged['thread_id']
        assert waiting['turn_id'] == acknowledged['turn_id']
        assert not waiting.get('result')
        deadline = deadline or waiting['status_wait']['deadline']
        assert waiting['status_wait']['deadline'] == deadline
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'active'
    record = p.runtime._get(assignment['attempt_id'])
    assert record['status_wait']['state'] == 'readable'
    assert len(record['error_history']) == delayed_reads
    assert all('is empty' in e['detail'] for e in record['error_history'])
    assert 'is empty' in record['error'] and not record.get('stop_receipt')
    assert p.backend.count('thread/start') == p.backend.count('turn/start') == 1


def test_worker_wait_does_not_hold_or_create_successors_from_duplicate_events(prepared):
    p = prepared
    p.backend.read_results = [empty_metadata_error(), empty_metadata_error()]
    with Worker(p.lifecycle, p.runtime) as worker:
        assert worker.tick()['dispatched'] == 1
        first = p.lifecycle.snapshot('Q2')
        assert not first['technical_blockers'] and len(first['assignments']) == 1
        assert first['assignments'][0]['state'] == 'acknowledged'
        assert first['resources']
        completion = {'method': 'turn/completed', 'params': {'threadId': 'thread-0',
                      'turn': {'id': 'turn-thread-0', 'status': 'completed'}}}
        p.backend.clients[0].events = [completion, completion]
        assert worker.tick()['dispatched'] == 0
        assert len(p.lifecycle.assignments()) == 1
        assert not p.lifecycle.snapshot('Q2')['technical_blockers']
        # No completion event remains: authoritative read drives one successor.
        (p.source / 'example.py').write_text('def add(a,b):\n return a+b\n')
        p.backend.finish('thread-0')
        assert worker.tick()['dispatched'] == 1
        for _ in range(2):
            assert worker.tick()['dispatched'] == 0
        assert len(p.lifecycle.assignments()) == 2
        assert p.backend.count('turn/start') == 2


def test_persistent_gap_uses_original_deadline_and_keeps_unknown_effects_owned(prepared, monkeypatch):
    p = prepared
    p.backend.read_results = [empty_metadata_error() for _ in range(10)]
    clock = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.datetime', Clock)
    monkeypatch.setattr('agentic_tools.aos.worker.datetime', Clock)
    with Worker(p.lifecycle, p.runtime) as worker:
        worker.tick()
        assignment = p.lifecycle.assignments()[0]
        before = p.runtime._get(assignment['attempt_id'])
        deadline = before['status_wait']['deadline']
        clock[0] = datetime.fromisoformat(deadline) + timedelta(microseconds=1)
        assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'
        worker.tick()
        snapshot = p.lifecycle.snapshot('Q2')
        assert snapshot['resources'] and snapshot['technical_blockers']
        assert len(snapshot['assignments']) == 1
        assert p.backend.count('turn/interrupt') == 0
        assert p.backend.count('turn/start') == 1
        assert p.runtime._get(assignment['attempt_id'])['status_wait']['deadline'] == deadline


@pytest.mark.parametrize('kind', ['wrong_thread', 'permission', 'transport_timeout', 'different_error'])
def test_only_demonstrated_metadata_gap_can_wait(prepared, kind):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    error = {'wrong_thread': empty_metadata_error('other-thread'),
             'permission': Rejected('runtime_rpc', 'thread/read failed: Permission denied'),
             'transport_timeout': Rejected('runtime_timeout', 'thread/read reply unavailable'),
             'different_error': Rejected('runtime_rpc', 'thread/read failed: session metadata unavailable')}[kind]
    p.backend.read_results = [error]
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'
    assert not p.runtime._get(assignment['attempt_id']).get('status_wait')
    assert p.backend.count('turn/start') == 1


def test_gap_after_readable_metadata_is_unknown(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'active'
    p.backend.read_results = [empty_metadata_error()]
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'


def test_gap_without_durable_turn_ack_is_unknown(prepared):
    p = prepared
    p.backend.lost_turn_ack = True
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    p.backend.read_results = [empty_metadata_error()]
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'
    assert not p.runtime._get(assignment['attempt_id']).get('status_wait')
    assert p.backend.count('turn/start') == 1


@pytest.mark.parametrize('wrong', ['thread', 'scope', 'turn', 'extra_turn', 'missing_turn'])
def test_cancel_validates_exact_read_identity_before_any_interrupt_or_cleanup(prepared, wrong):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    thread = p.backend.threads['thread-0']
    if wrong == 'thread':
        thread['id'] = 'wrong-thread'
    elif wrong == 'scope':
        thread['cwd'] = '/wrong-source'
    elif wrong == 'turn':
        thread['turns'][0]['id'] = 'wrong-turn'
    elif wrong == 'extra_turn':
        thread['turns'].append({'id': 'extra-turn', 'status': 'completed'})
    else:
        thread['turns'] = []
    stop = p.runtime.cancel(assignment['attempt_id'])
    assert not stop['effects_reconciled'] and stop['owned_processes'] is None
    assert p.backend.count('turn/interrupt') == p.backend.count('thread/backgroundTerminals/list') == 0
    assert 'runtime_identity' in stop['detail']


@pytest.mark.parametrize('status', ['inProgress', 'completed', 'failed', 'interrupted'])
def test_reconnected_cancel_loads_and_reads_saved_turn_before_interrupt(prepared, monkeypatch, status):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    p.backend.finish('thread-0', status)
    p.backend.clients[0].close()
    reopened = CodexRuntime(p.lifecycle, {'Q2': p.recipe}, transport_factory=p.backend.factory)
    monkeypatch.setattr(reopened, '_watch', lambda *_: None)
    monkeypatch.setattr(reopened, '_probe_sandbox', lambda *_: {'SIMULATED': True})
    p.backend.jobs = [{'processId': 'exact-owned-terminal'}]
    start_at = len(p.backend.calls)
    try:
        stop = reopened.cancel(assignment['attempt_id'])
        assert stop['stopped'] and stop['effects_reconciled'] and stop['owned_processes'] == []
        calls = [name for name, _ in p.backend.calls[start_at:]]
        assert calls.index('thread/resume') < calls.index('thread/read')
        assert p.backend.count('turn/interrupt') == (1 if status == 'inProgress' else 0)
        terminal = reopened._get(assignment['attempt_id'])['terminal_turn_observed']
        assert terminal['id'] == 'turn-thread-0'
        assert terminal['status'] == ('interrupted' if status == 'inProgress' else status)
        assert p.backend.count('turn/start') == 1 and reopened.model_calls == 0
    finally:
        reopened.close()


@pytest.mark.parametrize('boundary', ['thread_ack', 'before_turn', 'turn_ack'])
def test_cancellation_before_ack_never_blindly_interrupts_or_restarts(prepared, boundary):
    p = prepared
    if boundary == 'thread_ack':
        p.backend.lost_thread_ack = True
    elif boundary == 'turn_ack':
        p.backend.lost_turn_ack = True
    else:
        p.runtime._fault = lambda stage: (_ for _ in ()).throw(OSError('before turn')) if stage == 'before_turn_request' else None
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    stop = p.runtime.cancel(assignment['attempt_id'])
    assert stop['effects_reconciled'] == (boundary == 'turn_ack')
    assert p.backend.count('turn/interrupt') == (1 if boundary == 'turn_ack' else 0)
    assert p.backend.count('thread/start') == 1
    assert p.backend.count('turn/start') == (1 if boundary == 'turn_ack' else 0)


def test_cancel_during_metadata_gap_holds_until_same_terminal_is_readable(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    p.backend.read_results = [empty_metadata_error()]
    stop = p.runtime.cancel(assignment['attempt_id'])
    assert not stop['effects_reconciled'] and not stop['stopped']
    assert p.backend.count('turn/interrupt') == 0
    assert not p.runtime._get(assignment['attempt_id']).get('stop_receipt')
    p.backend.finish('thread-0', 'interrupted')
    stop = p.runtime.cancel(assignment['attempt_id'])
    assert stop['effects_reconciled'] and stop['stopped']
    assert p.backend.count('turn/interrupt') == 0 and p.backend.count('turn/start') == 1
    assert 'is empty' in p.runtime._get(assignment['attempt_id'])['error_history'][0]['detail']


def test_interrupt_ack_without_terminal_status_holds_and_is_not_repeated(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    client = p.backend.clients[0]
    original = client.request
    def delayed_interrupt(method, params=None):
        if method == 'turn/interrupt':
            p.backend.calls.append((method, params))
            return {}  # ACK is not terminal evidence.
        return original(method, params)
    monkeypatch.setattr(client, 'request', delayed_interrupt)
    for _ in range(2):
        stop = p.runtime.cancel(assignment['attempt_id'])
        assert not stop['effects_reconciled']
        assert p.backend.count('thread/backgroundTerminals/list') == 0
    assert p.backend.count('turn/interrupt') == 1
    p.backend.finish('thread-0', 'interrupted')
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled']
    assert p.backend.count('turn/interrupt') == 1


def test_cancel_rejects_wrong_resumed_thread_before_read_or_cleanup(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    client = p.backend.clients[0]
    original = client.request
    def wrong_resume(method, params=None):
        if method == 'thread/resume':
            return {'thread': {'id': 'other-thread', 'cwd': str(p.source)}}
        return original(method, params)
    monkeypatch.setattr(client, 'request', wrong_resume)
    assert not p.runtime.cancel(assignment['attempt_id'])['effects_reconciled']
    assert p.backend.count('thread/read') == p.backend.count('turn/interrupt') == 0


def test_terminal_status_contradiction_during_cleanup_remains_unknown(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    thread = copy.deepcopy(p.backend.threads['thread-0'])
    thread['turns'][0]['status'] = 'completed'
    p.backend.read_results = [{'thread': thread}]
    p.backend.finish('thread-0', 'interrupted')
    observation = p.runtime.poll(assignment['attempt_id'])
    assert observation['state'] == 'unknown' and 'status changed' in observation['detail']
    assert p.backend.count('thread/backgroundTerminals/list') == 0
    assert not p.runtime._get(assignment['attempt_id']).get('stop_receipt')


@pytest.mark.parametrize('status,state', [('completed', 'completed'), ('interrupted', 'failed')])
def test_reconnect_terminal_poll_never_interrupts_or_starts_again(prepared, monkeypatch, status, state):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    p.backend.finish('thread-0', status)
    p.backend.clients[0].close()
    reopened = CodexRuntime(p.lifecycle, {'Q2': p.recipe}, transport_factory=p.backend.factory)
    monkeypatch.setattr(reopened, '_watch', lambda *_: None)
    monkeypatch.setattr(reopened, '_probe_sandbox', lambda *_: {'SIMULATED': True})
    try:
        outcome = reopened.lookup(assignment['attempt_id'])
        assert outcome['state'] == state
        assert outcome['result']['runtime_turn_status'] == status
        assert outcome['result']['effects_reconciled']
        assert reopened.lookup(assignment['attempt_id']) == outcome
        assert p.backend.count('turn/interrupt') == 0
        assert p.backend.count('turn/start') == 1 and reopened.model_calls == 0
    finally:
        reopened.close()


def test_waiting_does_not_bypass_changed_authority(prepared):
    p = prepared
    p.backend.read_results = [empty_metadata_error(), empty_metadata_error()]
    with Worker(p.lifecycle, p.runtime) as worker:
        worker.tick()
        with p.store._transaction() as db:
            value = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='Q2'").fetchone()[0])
            value['authority'] = 'Unapproved expanded authority'
            db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q2'", (canonical(value),))
        worker.tick()
        snapshot = p.lifecycle.snapshot('Q2')
        assert snapshot['technical_blockers'] and snapshot['resources']
        assert len(snapshot['assignments']) == 1
        assert p.backend.count('turn/start') == 1


@pytest.mark.parametrize('config_change', ['filesystem', 'network', 'environment'])
def test_effective_restrictions_fail_closed(prepared, config_change):
    p = prepared
    overrides, expected = p.runtime._profile(p.recipe)
    client = p.backend.factory(None, overrides, p.source)
    if config_change == 'filesystem':
        client.config['permissions'][PROFILE]['filesystem'][':root'] = 'read'
    elif config_change == 'network':
        client.config['permissions'][PROFILE]['network']['enabled'] = True
    else:
        client.config['shell_environment_policy']['inherit'] = 'all'
    with pytest.raises(Rejected, match='Effective'):
        p.runtime._verify_config(client.config, expected)


def test_reviewer_profile_source_readonly_outputs_writable(prepared):
    _, profile = prepared.runtime._profile(prepared.recipe, assessment=True)
    assert profile['filesystem'][str(prepared.source)] == 'read'
    assert profile['filesystem'][str(prepared.outputs)] == 'write'
    assert profile['filesystem'][str(prepared.store.directory)] == 'deny'
    assert profile['filesystem'][':root'] == 'deny'
    assert profile['network']['enabled'] is False


def test_shared_budget_is_durable_and_counts_failed_ambiguous_attempts(prepared):
    p = prepared
    p.backend.lost_thread_ack = True
    assignment = claim(p)
    assert p.runtime.start(assignment)['state'] == 'unknown'
    first = p.runtime.budget_receipt()
    assert first['attempts_reserved'] == 1 and first['seconds_reserved'] == 60
    p.runtime.start(assignment)
    assert p.runtime.budget_receipt() == first
    with p.runtime._db() as db:
        for index in range(7):
            record = {'attempt_id': 'reserved-' + str(index), 'budget_seconds': 300}
            db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    # The previous ambiguous attempt remains counted across reopening.
    assert p.runtime.budget_receipt()['attempts_reserved'] == 8
    assert p.backend.count('thread/start') == 1


def test_full_shared_budget_rejects_first_launch_without_model(prepared):
    p = prepared
    with p.runtime._db() as db:
        for index in range(8):
            record = {'attempt_id': 'reserved-' + str(index), 'budget_seconds': 300}
            db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    with pytest.raises(Rejected, match='reservation exhausted'):
        p.runtime.start(claim(p))
    assert p.backend.count('thread/start') == 0


def test_output_scope_is_new_per_attempt_and_hides_prior_reports(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    assert Path(record['run_outputs']) == p.outputs / assignment['attempt_id']
    client = p.backend.clients[0]
    profile = client.config['permissions'][PROFILE]['filesystem']
    assert profile[record['run_outputs']] == 'write'
    assert str(p.outputs) not in profile


def test_live_saved_server_without_transport_holds_recovery(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    record['server_history'] = [{'pid': 1234, 'started': 'same-process'}]
    p.runtime._save(record)
    p.runtime.clients.clear()
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes', lambda: {1234: (1, 'same-process')})
    result = p.runtime.poll(assignment['attempt_id'])
    assert result['state'] == 'unknown' and 'control transport' in result['detail']
    assert p.backend.count('turn/start') == 1


def test_real_telemetry_projects_partial_coverage_and_never_zero(prepared):
    p = prepared
    with Worker(p.lifecycle, p.runtime) as worker:
        worker.tick()
        source = p.lifecycle.snapshot('Q2')
        assert source['usage']['model_calls'] == 1
        assert source['usage']['recorded_tokens'] is None
        assert source['usage']['partial'] is True
        assert source['usage']['billing'] == 'unavailable'
        assert worker.tick()['model_calls'] == 0
        assert p.lifecycle.snapshot('Q2')['usage']['model_calls'] == 1


def native_fixture(tmp_path, mode='child', *, attempt_id=None):
    source, outputs, state = (tmp_path / name for name in ('native-source', 'native-outputs', 'native-state'))
    for path in (source, outputs, state):
        path.mkdir()
    body = 'def test_native():\n'
    if mode != 'no-child':
        body = ('import subprocess,sys,time\nfrom pathlib import Path\n' + body +
                " p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n" +
                f" Path({str(outputs / 'child.pid')!r}).write_text(str(p.pid))\n")
    if mode in ('timeout', 'cancel'):
        body += ' time.sleep(30)\n'
    elif mode == 'failure':
        body += ' assert False, "expected failing native check"\n'
    else:
        body += ' assert True\n'
    (source / 'test_native.py').write_text(body)
    (source / 'pytest.ini').write_text('[pytest]\n')
    recipe = {'source': str(source), 'outputs': str(outputs),
              'read_roots': sorted({str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve())}),
              'immutable': inventory(source), 'max_seconds': 10,
              'checks': [{'name': 'native', 'argv': [sys.executable, '-m', 'pytest', '-q', '-c', 'pytest.ini', '--rootdir=.', 'test_native.py'],
                          'timeout_seconds': 1 if mode == 'timeout' else 10, 'scope': 'ordinary owned check process/effects'}]}
    if attempt_id:
        recipe['_attempt_id'] = attempt_id
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=state)), {'N': recipe})
    return SimpleNamespace(runtime=runtime, recipe=recipe, source=source, outputs=outputs, state=state,
                           check=recipe['checks'][0], frozen=candidate(source))


@pytest.mark.parametrize('mode,expected', [('no-child', 'pass'), ('child', 'pass'), ('failure', 'fail'), ('timeout', 'timeout')])
def test_real_native_check_reconciles_children_on_every_terminal_path(tmp_path, mode, expected):
    p = native_fixture(tmp_path, mode)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == expected
        assert result['cleanup']['effects_reconciled'] is True
        assert result['cleanup']['owned_processes'] == []
        assert result['cleanup']['source_after'] == p.frozen['inventory']
        if (p.outputs / 'child.pid').exists():
            assert int((p.outputs / 'child.pid').read_text()) not in process_groups()
        row = p.runtime._native_get(result['native_check_id'])
        assert row['owner']['pid'] not in process_groups()
    finally:
        p.runtime.close()


def test_native_owner_identity_change_never_signals_unrelated_process_or_reuses_model_stop(prepared, monkeypatch):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    record['model_stop_receipt'] = record['stop_receipt'] = {
        'attempt_id': assignment['attempt_id'], 'stopped': True, 'effects_reconciled': True,
        'owned_processes': [], 'partial_mutations': {'status': 'none', 'changes': []}}
    p.runtime._save(record)
    row = {'check_id': 'nc-unknown', 'attempt_id': assignment['attempt_id'], 'phase': 'running',
           'owner': {'pid': 4567, 'started': 'original'}, 'members': {}}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], row['attempt_id'], canonical(row)))
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', lambda: {4567: {'parent': 1, 'group': 4567, 'started': 'different'}})
    signals = []
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.os.kill', lambda *args: signals.append(args))
    stopped = p.runtime.cancel(assignment['attempt_id'])
    assert stopped['effects_reconciled'] is False and stopped['stopped'] is False
    assert signals == []
    assert p.runtime._native_get('nc-unknown')['phase'] == 'cleanup_unknown'


def test_real_cancel_reconciles_native_check_before_cached_model_stop(tmp_path):
    p = native_fixture(tmp_path, 'cancel', attempt_id='native-cancel')
    record = {'attempt_id': 'native-cancel', 'assignment': {'work_id': 'N', 'kind': 'assessment'},
              'recipe_digest': digest(p.recipe), 'thread_id': 'scripted-only', 'baseline': inventory(p.source),
              'model_stop_receipt': {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                                     'partial_mutations': {'status': 'none', 'changes': []}}}
    record['stop_receipt'] = record['model_stop_receipt']
    with p.runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)))
    try:
        thread.start()
        until = time.monotonic() + 5
        while not (p.outputs / 'child.pid').exists() and time.monotonic() < until:
            time.sleep(.025)
        assert (p.outputs / 'child.pid').exists()
        stopped = p.runtime.cancel('native-cancel')
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert stopped['effects_reconciled'] is True and stopped['native_checks']
        assert outcomes[0]['result'] == 'cancelled'
        assert int((p.outputs / 'child.pid').read_text()) not in process_groups()
    finally:
        p.runtime.close()
        thread.join(timeout=5)


def test_real_worker_death_native_recovery_never_reexecutes_check(tmp_path):
    p = native_fixture(tmp_path, 'child', attempt_id='native-restart')
    script = '''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from agentic_tools.aos.codex_runtime import CodexRuntime,candidate
r=CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=Path(sys.argv[1]))),{})
recipe=json.loads(sys.argv[2])
r._fault=lambda stage: os._exit(73) if stage=='native_finished_before_cleanup' else None
r._check(recipe,recipe['checks'][0],Path(recipe['outputs'])/'check','native-test',candidate(Path(recipe['source'])))
'''
    try:
        child = subprocess.run([sys.executable, '-c', script, str(p.state), canonical(p.recipe)],
                               capture_output=True, text=True, timeout=15)
        assert child.returncode == 73, child.stderr
        pid = int((p.outputs / 'child.pid').read_text())
        assert pid in process_groups()
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass'
        assert pid not in process_groups()
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 1
        assert p.runtime._native_get(result['native_check_id'])['phase'] == 'clean'
    finally:
        p.runtime.close()


def test_legacy_assessment_terminal_cannot_reuse_pre_native_stop(prepared):
    p = prepared
    assignment = claim(p)
    p.runtime.start(assignment)
    record = p.runtime._get(assignment['attempt_id'])
    record['assignment']['kind'] = 'assessment'
    record['terminal_observation'] = {'state': 'completed', 'result': {'effects_reconciled': True}}
    record['phase'] = 'terminal'
    p.runtime._save(record)
    assert p.runtime.lookup(assignment['attempt_id'])['state'] == 'unknown'
    assert p.runtime.poll(assignment['attempt_id'])['state'] == 'unknown'
    assert p.backend.count('turn/start') == 1


def test_native_admission_after_cancellation_cannot_start_work(tmp_path):
    p = native_fixture(tmp_path, 'child', attempt_id='already-cancelled')
    record = {'attempt_id': 'already-cancelled', 'native_cancel_requested': True}
    with p.runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    try:
        with pytest.raises(Rejected, match='durably cancelled'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert not (p.outputs / 'child.pid').exists()
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    finally:
        p.runtime.close()
