"""Disposable successor qualification; approval and metadata are fixture seams."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil

import pytest

from agentic_tools.aos import predispatch_recovery as recovery
from agentic_tools.aos import predispatch_feature_amendment as amendment
from agentic_tools.aos.store import Rejected, digest
from test_aos_predispatch_feature_amendment import approve, charge, write
from test_aos_predispatch_recovery import RecoveryFixture


class SuccessorFixture(RecoveryFixture):
    """Retain a genuinely consumed, terminal, zero-role failed first attempt."""

    def __init__(self, tmp_path, monkeypatch, *, root_outcome='failed', failure_change=None,
                 apply_root=False):
        self.failure_change = failure_change
        super().__init__(tmp_path, monkeypatch)
        self.first_prepared, self.first_request = self.approved()
        self.metadata_outcome = root_outcome
        if root_outcome in ('failed', 'interrupt'):
            with pytest.raises((Rejected, KeyboardInterrupt)):
                recovery.qualify(self.lifecycle, self.first_request)
            assert len(self.recovery_calls) == 1, 'Fixture must reach the intended metadata provider failure'
            if root_outcome == 'failed':
                assert (self.recovery_destination / 'qualification-result.json').is_file(), 'Fixture must retain the actual intended terminal failure'
        else:
            recovery.qualify(self.lifecycle, self.first_request)
        if apply_root:
            recovery.apply(self.lifecycle, self.first_request)
        self.first_engine = self.private / 'first-recovery-engine'
        shutil.copytree(Path(recovery.__file__).parent.parent, self.first_engine,
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.first_result_path = self.recovery_destination / 'qualification-result.json'
        if self.first_result_path.exists():
            result_ref = amendment._file(self.first_result_path)[1]
        else:
            result_ref = {'path': str(self.first_result_path), 'sha256': '0' * 64}
        self.first_files = {str(p): p.read_bytes() for p in self.recovery_destination.rglob('*')
                            if p.is_file()}
        self.first_history = self.history()
        with self.store._connection() as db:
            self.first_intent = [tuple(row) for row in db.execute('SELECT * FROM ' + recovery.INTENT)]
        self.successor_destination = self.private / 'successor'; self.successor_destination.mkdir(mode=0o700)
        self.successor_source = self.private / 'second-corrected-controller.py'
        self.successor_source.write_text('# Bounded metadata-boundary correction; fixture only.\n')
        self.successor_source_ref = amendment._file(self.successor_source)[1]
        self.successor_controller = copy.deepcopy(self.original_controller)
        self.successor_controller['controller_sha256'] = self.successor_source_ref['sha256']
        self.successor_controller['input_files'] = [
            self.successor_source_ref if ref == self.before_source else ref
            for ref in self.successor_controller['input_files']]
        self.successor_controller['supplemental_recovery'] = {'operation_id': 'recover-metadata-boundary'}
        self.successor_controller_path = self.private / 'successor-controller.json'
        write(self.successor_controller_path, self.successor_controller)
        self.successor_request = copy.deepcopy(self.recovery_request)
        self.successor_request.update(
            operation_id='recover-metadata-boundary',
            expected_work_revision=self.store.read(self.work)['revision'],
            digest=self.first_request['digest'], decision_id=self.first_request['decision_id'],
            controller_contract_path=str(self.successor_controller_path),
            source_changes=[{'before': self.before_source, 'after': self.successor_source_ref}],
            destination_directory=str(self.successor_destination),
            predecessor={
                'packet': {'path': self.first_prepared['packet_path'],
                           'sha256': self.first_prepared['packet_sha256']},
                'qualification': result_ref, 'engine_directory': str(self.first_engine)})
        self.metadata_outcome = 'passed'

    def supplemental_metadata(self, recipe, **kwargs):
        result = super().supplemental_metadata(recipe, **kwargs)
        if self.metadata_outcome == 'failed':
            result.update(roles=[], native=None, blockers=[{
                'code': 'checkout_scope', 'detail': 'Fixture protected-root boundary failed before roles'}])
            if self.failure_change:
                key, value = self.failure_change
                result[key] = copy.deepcopy(value)
            control = Path(kwargs['readiness_dir']) / 'control'
            ref = write(control / 'readiness-1.json', result)
            journal_path = control / 'readiness.json'
            journal = json.loads(journal_path.read_text())
            journal['runs'][0].update(receipt=ref['path'], sha256=ref['sha256'], state=result['status'])
            write(journal_path, journal)
        return result

    def prepare_successor(self):
        return recovery.prepare(self.lifecycle, self.successor_request)

    def publish_successor(self, prepared):
        presented = self.store.present(**prepared['packet']['concept'])
        saved = approve(self.store, self.work, presented['digest'], 'successor-decision')
        return {'operation_id': self.successor_request['operation_id'], 'work_id': self.work,
                'expected_work_revision': self.store.read(self.work)['revision'],
                'digest': presented['digest'], 'decision_id': saved['decision_id'],
                'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}

    def approved_successor(self):
        prepared = self.prepare_successor()
        return prepared, self.publish_successor(prepared)

    def qualified_successor(self):
        prepared, request = self.approved_successor()
        return prepared, request, recovery.qualify(self.lifecycle, request)

    def recovered_successor(self):
        prepared, request, result = self.qualified_successor()
        receipt = recovery.apply(self.lifecycle, request)
        reference = {'operation_id': request['operation_id'], 'work_id': self.work,
                     'receipt_digest': digest(receipt)}
        return prepared, request, result, receipt, reference

    def assert_first_preserved(self):
        self.assert_history_preserved()
        assert all(Path(path).read_bytes() == raw for path, raw in self.first_files.items())
        current = self.history()
        for table, rows in self.first_history.items():
            assert all(row in current[table] for row in rows)
        with self.store._connection() as db:
            assert [tuple(row) for row in db.execute('SELECT * FROM ' + recovery.INTENT)] == self.first_intent


@pytest.fixture
def successor(tmp_path, monkeypatch):
    return SuccessorFixture(tmp_path, monkeypatch)


def test_successor_preparation_is_passive_and_carries_exact_unspent_runtime_grant(successor):
    s = successor
    prepared = s.prepare_successor()
    assert len(s.recovery_calls) == 1  # Only the retained failed predecessor ran.
    assert prepared['packet']['concept']['revision'] == s.first_prepared['packet']['concept']['revision'] + 1
    new = prepared['packet']['request']['allowances']
    old = s.first_prepared['packet']['request']['allowances']
    assert new['runtime_slots'] == old['runtime_slots']
    assert new['effective_limit_seconds'] == old['effective_limit_seconds']
    assert new['metadata_attempts'] == 1
    authority = prepared['packet']['concept']['authority']
    assert 'unchanged' in authority and 'forward' in authority and 'adds no runtime' in authority
    assert '73500' in authority and '600' in authority
    assert s.prepare_successor() == prepared
    s.assert_first_preserved()


def test_accepted_successor_preserves_failed_attempt_and_never_dispatches(successor):
    s = successor
    prepared, request, result, receipt, reference = s.recovered_successor()
    assert len(s.recovery_calls) == 2
    assert s.recovery_calls[0]['readiness_dir'] != s.recovery_calls[1]['readiness_dir']
    assert all(call['max_attempts'] == 1 and call['max_seconds'] == 600 for call in s.recovery_calls)
    original = json.loads(s.journal.read_text())
    first = json.loads((s.recovery_destination / 'readiness/control/readiness.json').read_text())
    second = json.loads((s.successor_destination / 'readiness/control/readiness.json').read_text())
    assert len(original['runs']) == 3
    assert len(first['runs']) == len(second['runs']) == 1
    assert first['runs'][0]['state'] == 'failed' and second['runs'][0]['state'] == 'passed'
    assert recovery.verify(s.store, reference) == receipt
    assert recovery.apply(s.lifecycle, request) == receipt
    assert len(s.recovery_calls) == 2
    assert receipt['runtime_slots'] == s.recovery_request['allowances']['runtime_slots']
    assert receipt['effective_limit_seconds'] == 73500
    accounting = recovery.current_accounting(s.store, receipt, settled=True)
    assert accounting['clock']['spent'] == 6900 and accounting['clock']['open'] is None
    assert accounting['clock']['active_limit'] == 21600
    assert accounting['clock']['deadline'] == s.original_receipt['accounting_origin']['deadline']
    with s.store._connection() as db:
        assert db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (s.work,)).fetchone()[0] == 0
        row = db.execute('SELECT execution,admitted FROM lifecycle WHERE work_id=?', (s.work,)).fetchone()
        assert tuple(row) == ('held', 0)
    s.assert_first_preserved()


def test_predecessor_decision_does_not_approve_successor_metadata(successor):
    s = successor
    prepared = s.prepare_successor()
    request = {**s.first_request, 'operation_id': s.successor_request['operation_id'],
               'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}
    with pytest.raises(Rejected):
        recovery.qualify(s.lifecycle, request)
    assert len(s.recovery_calls) == 1
    s.assert_first_preserved()


def test_carried_runtime_grant_can_be_charged_once_after_successor_apply(successor):
    s = successor
    _, _, _, receipt, reference = s.recovered_successor()
    retained = {path: raw for path, raw in s.first_files.items()}
    charge(s, receipt, 'recovery-test-1', seconds=3600)
    accounting = recovery.current_accounting(s.store, receipt, settled=True)
    assert accounting['clock']['spent'] == 10500
    assert accounting['clock']['active_limit'] == 21600
    assert accounting['clock']['identity'] == s.original_receipt['accounting_origin']['clock_identity']
    assert accounting['clock']['deadline'] == s.original_receipt['accounting_origin']['deadline']
    assert accounting['used_slots'].count('recovery-test-1') == 1
    assert recovery.verify(s.store, reference) == receipt
    assert all(Path(path).read_bytes() == raw for path, raw in retained.items())


@pytest.mark.parametrize('field', ['decision_id', 'digest', 'expected_work_revision'])
def test_successor_must_bind_current_pending_approval(successor, field):
    s = successor
    values = {'decision_id': s.original_receipt['to_decision_id'],
              'digest': s.original_receipt['to_concept_digest'],
              'expected_work_revision': s.successor_request['expected_work_revision'] - 1}
    s.successor_request[field] = values[field]
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


@pytest.mark.parametrize('field,value', [
    ('roles', [{'role': 'implementation', 'passed': False, 'cleanup': {'verified': True, 'no_launch': True}}]),
    ('cleanup_verified', False), ('model_turns', 1), ('product_attempts', 1),
    ('product_reservations', 1), ('ready', True), ('simulated', True),
    ('requested_native_checks', [{'name': 'unexpected'}]),
    ('native', {'checks': [], 'execution': 'not_requested'}),
    ('phase', {'state': 'running'}), ('model_turns', False), ('blockers', []),
])
def test_nonzero_or_unknown_effect_predecessor_is_rejected(tmp_path, monkeypatch, field, value):
    s = SuccessorFixture(tmp_path, monkeypatch, failure_change=(field, value))
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


@pytest.mark.parametrize('outcome,applied', [('passed', False), ('passed', True), ('interrupt', False)])
def test_only_terminal_failed_unapplied_predecessor_can_have_successor(tmp_path, monkeypatch, outcome, applied):
    s = SuccessorFixture(tmp_path, monkeypatch, root_outcome=outcome, apply_root=applied)
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


@pytest.mark.parametrize('field,value', [
    ('runtime_slots', {'recovery-test-1': 3601}),
    ('runtime_slots', {'new-runtime-slot': 3600}),
    ('runtime_slots', {'recovery-test-1': 3600, 'additional-slot': 900}),
    ('effective_limit_seconds', 74400),
])
def test_successor_cannot_silently_add_or_replace_runtime_allowance(successor, field, value):
    s = successor
    s.successor_request['allowances'][field] = value
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


@pytest.mark.parametrize('phase', ['prepare', 'qualify', 'apply'])
def test_changed_failed_predecessor_bytes_hold_every_successor_gate(successor, phase):
    s = successor
    if phase == 'prepare':
        command = s.prepare_successor
    elif phase == 'qualify':
        _, request = s.approved_successor()
        command = lambda: recovery.qualify(s.lifecycle, request)
    else:
        _, request, _ = s.qualified_successor()
        command = lambda: recovery.apply(s.lifecycle, request)
    path = s.first_result_path
    path.chmod(0o600)
    value = json.loads(path.read_text()); value['completed_at'] = '2099-01-01T00:00:00+00:00'
    write(path, value); path.chmod(0o400)
    with pytest.raises(Rejected):
        command()
    assert s.store.read(s.work)['execution'] == 'held'


def test_historical_predecessor_engine_must_match_exact_manifest(successor):
    s = successor
    path = s.first_engine / 'aos/store.py'
    path.write_text(path.read_text() + '\n# mutated old engine fixture\n')
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


def test_predecessor_remains_historical_under_new_engine_identity(successor, monkeypatch):
    s = successor
    prior = recovery.readiness._runtime_identity()
    monkeypatch.setattr(recovery.readiness, '_runtime_identity',
                        lambda: {**prior, 'runtime_source_digest': 'sha256:' + 'c' * 64})
    _, _, _, receipt, reference = s.recovered_successor()
    assert receipt['engine_digest'] == 'sha256:' + 'c' * 64
    assert recovery.verify(s.store, reference) == receipt
    s.assert_first_preserved()


def test_a_prepared_successor_cannot_be_replaced_with_another_operation_id(successor):
    s = successor
    s.prepare_successor()
    s.successor_request['operation_id'] = 'competing-successor'
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


def test_interrupted_successor_apply_rolls_back_then_replays_once(successor, monkeypatch):
    s = successor
    _, request, _ = s.qualified_successor()
    touch = s.lifecycle._touch

    def interrupted(db, work_id, kind, detail):
        touch(db, work_id, kind, detail)
        if kind == 'predispatch_recovered':
            raise KeyboardInterrupt('After successor contract CAS, before transaction commit')

    with monkeypatch.context() as patch:
        patch.setattr(s.lifecycle, '_touch', interrupted)
        with pytest.raises(KeyboardInterrupt):
            recovery.apply(s.lifecycle, request)
    assert s.store.read(s.work)['revision'] == request['expected_work_revision']
    with s.store._connection() as db:
        assert json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (s.work,)).fetchone()[0]) == s.original_receipt['registered_contract']
        assert db.execute('SELECT count(*) FROM ' + recovery.TABLE).fetchone()[0] == 0
    receipt = recovery.apply(s.lifecycle, request)
    assert recovery.apply(s.lifecycle, request) == receipt
    assert len(s.recovery_calls) == 2
    s.assert_first_preserved()


@pytest.mark.parametrize('outcome', ['failed', 'interrupt'])
def test_successor_metadata_failure_or_interruption_never_reuses_slot(successor, outcome):
    s = successor
    _, request = s.approved_successor(); s.metadata_outcome = outcome
    with pytest.raises((Rejected, KeyboardInterrupt)):
        recovery.qualify(s.lifecycle, request)
    with pytest.raises(Rejected):
        recovery.qualify(s.lifecycle, request)
    with pytest.raises(Rejected):
        recovery.apply(s.lifecycle, request)
    assert len(s.recovery_calls) == 2
    s.assert_first_preserved()


def test_a_failed_successor_does_not_authorize_an_unbounded_third_preparation(successor):
    s = successor
    prepared, request = s.approved_successor(); s.metadata_outcome = 'failed'
    with pytest.raises(Rejected):
        recovery.qualify(s.lifecycle, request)
    assert len(s.recovery_calls) == 2
    third = copy.deepcopy(s.successor_request)
    third.update(operation_id='third-recovery', decision_id=request['decision_id'],
                 digest=request['digest'], expected_work_revision=s.store.read(s.work)['revision'])
    third_destination = s.private / 'third'; third_destination.mkdir(mode=0o700)
    third['destination_directory'] = str(third_destination)
    controls = copy.deepcopy(s.successor_controller)
    controls['supplemental_recovery']['operation_id'] = third['operation_id']
    path = s.private / 'third-controller.json'; write(path, controls)
    third['controller_contract_path'] = str(path)
    third['predecessor'] = {
        'packet': {'path': prepared['packet_path'], 'sha256': prepared['packet_sha256']},
        'qualification': amendment._file(s.successor_destination / 'qualification-result.json')[1],
        'engine_directory': str(s.first_engine)}
    with pytest.raises(Rejected):
        recovery.prepare(s.lifecycle, third)
    assert len(s.recovery_calls) == 2
    s.assert_first_preserved()


def test_changed_accounting_cannot_be_carried_into_successor(successor):
    s = successor
    clock_path = s.control / 'clock.json'
    clock = json.loads(clock_path.read_text()); clock['spent'] += 1
    write(clock_path, clock)
    with pytest.raises(Rejected):
        s.prepare_successor()
    assert len(s.recovery_calls) == 1


def test_preflight_failure_precedes_successor_intent_and_metadata_reservation(successor, monkeypatch):
    s = successor
    revision = s.store.read(s.work)['revision']

    def rejected_preflight(*args, **kwargs):
        raise Rejected('fixture_preflight', 'Static metadata profile is invalid')

    monkeypatch.setattr(recovery.readiness, 'preflight_metadata', rejected_preflight)
    with pytest.raises(Rejected, match='Static metadata profile'):
        s.prepare_successor()
    assert s.store.read(s.work)['revision'] == revision
    assert len(s.recovery_calls) == 1
    assert not (s.successor_destination / 'packet.json').exists()
    assert not (s.successor_destination / 'qualification-reservation.json').exists()
    with s.store._connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (recovery.SUCCESSOR,)).fetchone()
        assert not exists or db.execute('SELECT count(*) FROM ' + recovery.SUCCESSOR).fetchone()[0] == 0
    s.assert_first_preserved()


@pytest.mark.parametrize('phase', ['qualify', 'apply'])
def test_preflight_drift_after_saved_approval_holds_without_extra_execution(successor, monkeypatch, phase):
    s = successor
    if phase == 'qualify':
        _, request = s.approved_successor()
        command = lambda: recovery.qualify(s.lifecycle, request)
        before_calls = 1
    else:
        _, request, _ = s.qualified_successor()
        command = lambda: recovery.apply(s.lifecycle, request)
        before_calls = 2
    preflight = recovery.readiness.preflight_metadata

    def changed_preflight(*args, **kwargs):
        value = preflight(*args, **kwargs)
        return {**value, 'plan_digest': 'sha256:' + 'f' * 64}

    monkeypatch.setattr(recovery.readiness, 'preflight_metadata', changed_preflight)
    with pytest.raises(Rejected):
        command()
    assert len(s.recovery_calls) == before_calls
    if phase == 'qualify':
        assert not (s.successor_destination / 'qualification-reservation.json').exists()
    with s.store._connection() as db:
        assert db.execute('SELECT count(*) FROM ' + recovery.TABLE).fetchone()[0] == 0
    s.assert_first_preserved()


def test_genuinely_concurrent_successor_preparations_accept_only_one(successor, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Barrier

    s = successor
    other = copy.deepcopy(s.successor_request); other['operation_id'] = 'competing-successor'
    destination = s.private / 'competing-successor'; destination.mkdir(mode=0o700)
    other['destination_directory'] = str(destination)
    controls = copy.deepcopy(s.successor_controller)
    controls['supplemental_recovery']['operation_id'] = other['operation_id']
    path = s.private / 'competing-successor-controller.json'; write(path, controls)
    other['controller_contract_path'] = str(path)
    fence = recovery.fencing._worker_fence; together = Barrier(2)

    @contextmanager
    def concurrent_fence(store):
        together.wait(timeout=10)
        with fence(store) as check:
            yield check

    def attempt(request):
        try:
            return recovery.prepare(s.lifecycle, request)
        except Rejected as error:
            return error

    monkeypatch.setattr(recovery.fencing, '_worker_fence', concurrent_fence)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (s.successor_request, other)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, Rejected) for outcome in outcomes) == 1
    assert len(s.recovery_calls) == 1
    s.assert_first_preserved()
