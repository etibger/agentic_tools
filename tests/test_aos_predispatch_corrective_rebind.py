"""Disposable corrective rebind; approvals and metadata are explicit fixture seams."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import types

import pytest

from agentic_tools.aos import predispatch_feature_amendment as amendment
from agentic_tools.aos import predispatch_recovery as recovery
from agentic_tools.aos import predispatch_corrective_rebind as corrective
from agentic_tools.aos import native_processes, owned_host_command
from agentic_tools.aos.store import Rejected, digest
from test_aos_predispatch_feature_amendment import approve, charge, write
from test_aos_predispatch_recovery import RecoveryFixture


class CorrectiveFixture(RecoveryFixture):
    """Begin with an applied recovery and its fully charged, settled operation."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        (self.applied_prepared, self.applied_request, self.applied_qualification,
         self.applied_receipt, self.applied_reference) = self.recovered()
        charge(self, self.applied_receipt, 'recovery-test-1', seconds=3600)
        self.applied_accounting = recovery.current_accounting(self.store, self.applied_receipt, settled=True)
        assert self.applied_accounting['clock']['spent'] == 10500
        self.applied_engine = self.private / 'applied-recovery-engine'
        shutil.copytree(Path(recovery.__file__).parent.parent, self.applied_engine,
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.applied_controller = copy.deepcopy(self.applied_prepared['packet']['metadata']['controller_contract'])
        self.applied_controller_path = self.private / 'applied-recovery-controller.json'
        write(self.applied_controller_path, self.applied_controller)
        self.corrective_destination = self.private / 'corrective'
        self.corrective_destination.mkdir(mode=0o700)
        self.corrective_source = self.private / 'corrective-controller.py'
        self.corrective_source.write_text('# Bounded corrective implementation; synthetic fixture only.\n')
        self.corrective_source_ref = amendment._file(self.corrective_source)[1]
        self.diagnostic_source = self.private / 'local-diagnostic.py'
        self.diagnostic_source.write_text(
            'import json, os\n'
            'print(json.dumps({"format": "aos-corrective-diagnostic-v1", '
            '"packet_digest": os.environ["AOS_CORRECTIVE_PACKET_DIGEST"], '
            '"passed": True, "runtime_qualified": False, "model_turns": 0, '
            '"product_attempts": 0}))\n')
        self.diagnostic_source_ref = amendment._file(self.diagnostic_source)[1]
        self.historical_owner = {'pid': 59001, 'uid': os.geteuid(), 'birth': [100, 1],
                                 'parent': 1, 'group': 59001, 'status': 2}
        self.preserved_process = {'pid': 59002, 'uid': os.geteuid(), 'birth': [100, 2],
                                  'parent': 1, 'group': 59002, 'status': 2}
        self.historical_supervisor = {'pid': 59003, 'uid': os.geteuid(), 'birth': [100, 3],
                                     'parent': 59001, 'group': 59003, 'status': 2}
        self._install_identity_and_census_seams(monkeypatch)
        self._build_request()
        self.baseline_history = self.history()
        with self.store._connection() as db:
            self.applied_rows = {
                table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                for table in (recovery.TABLE, recovery.INTENT)
            }
        roots = (self.control, self.readiness, self.destination, self.recovery_destination)
        self.baseline_files = {str(path): path.read_bytes() for root in roots for path in root.rglob('*')
                               if path.is_file()}
        refs = [self.proof[k] for k in ('failure', 'effects', 'settlement', 'closed_owner', 'observation')]
        refs += self.proof['retained_outputs'] + self.proof['preserved_files'] + [self.historical_command_reference]
        self.baseline_files.update({ref['path']: Path(ref['path']).read_bytes() for ref in refs})
        path = str(Path(self.historical_command_record['control']) / 'receipt.json')
        self.baseline_files[path] = Path(path).read_bytes()

    def assert_applied_history_preserved(self, *, clock_unchanged=True):
        current = self.history()
        for table, rows in self.baseline_history.items():
            assert all(row in current[table] for row in rows)
        with self.store._connection() as db:
            for table, rows in self.applied_rows.items():
                assert [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')] == rows
        clock_path = str(self.control / 'clock.json')
        assert all(Path(path).read_bytes() == raw for path, raw in self.baseline_files.items()
                   if clock_unchanged or path != clock_path)

    def _install_identity_and_census_seams(self, monkeypatch):
        """Actual harmless children run; synthetic identities replace host inspection."""
        self.children = {}
        self.census_calls = []
        original_popen = owned_host_command.subprocess.Popen

        def popen(argv, **kwargs):
            assert str(owned_host_command.SUPERVISOR) in argv
            process = original_popen(argv, **kwargs)
            self.children[process.pid] = process
            return process

        def identity(pid):
            if pid == self.preserved_process['pid']:
                return copy.deepcopy(self.preserved_process)
            if pid in (self.historical_owner['pid'], self.historical_supervisor['pid']):
                return None
            if pid == os.getpid():
                return {'pid': pid, 'uid': os.geteuid(), 'birth': [200, 1],
                        'parent': os.getppid(), 'group': os.getpgrp(), 'status': 2}
            child = self.children.get(pid)
            if child is not None:
                if child.poll() is not None:
                    return None
                return {'pid': pid, 'uid': os.geteuid(), 'birth': [200, pid],
                        'parent': os.getpid(), 'group': pid, 'status': 2}
            raise AssertionError('Fixture cannot inspect an unknown host process: ' + str(pid))

        def observe(deadline, audit, **kwargs):
            self.census_calls.append(deadline)
            return {}

        monkeypatch.setattr(native_processes, 'identity', identity)
        monkeypatch.setattr(owned_host_command, '_observe', observe)
        original = owned_host_command.subprocess
        monkeypatch.setattr(owned_host_command, 'subprocess', types.SimpleNamespace(
            Popen=popen, PIPE=original.PIPE, DEVNULL=original.DEVNULL, TimeoutExpired=original.TimeoutExpired))

    def historical_command(self, name='probe-before-failure'):
        """Build retained historical evidence and invoke the real verifier later."""
        self.retained_effects = self.private / 'retained-effects'
        self.retained_effects.mkdir(mode=0o700)
        control = self.retained_effects / (name + '-executor')
        control.mkdir(mode=0o700)
        argv = [str(Path(sys.executable).resolve()), str(self.diagnostic_source)]
        environment = {'TMPDIR': str(self.private), 'LANG': 'C'}
        environment_sha = hashlib.sha256(json.dumps(environment, sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest()
        snapshot = {'format': 'owned-host-command-v1', 'backend': 'host-owned-v1',
            'argv': argv, 'cwd': str(self.private), 'control': str(control),
            'environment_sha256': environment_sha, 'controller': self.historical_owner,
            'deadline_monotonic': 100.0,
            'cleanup_deadline_monotonic': 100.0 + owned_host_command.CLEANUP_RESERVE_SECONDS,
            'phase': 'complete', 'passed': True, 'cleanup_verified': True,
            'controller_finished': True, 'cancel_requested': False, 'timed_out': False,
            'owner': self.historical_supervisor, 'owner_exit': 0, 'owner_reaped': True,
            'owner_absent': True, 'group_empty': True, 'pipes_eof': True,
            'readers_joined': True, 'stdin_closed': True, 'output_files_closed': True,
            'owned_remaining': {}, 'observed': {}, 'errors': [], 'error': None,
            'launch_authorized': True, 'release_written': True, 'boundary_rechecked': True,
            'started_at': self.applied_accounting['operations']['recovery-test-1']['reservation']['started_at'],
            'checkpoint_at': self.applied_accounting['operations']['recovery-test-1']['result']['completed_at'],
            'child': {'exit_code': 0}}
        write(control / 'receipt.json', snapshot)
        reference = write(self.retained_effects / (name + '.json'), snapshot)
        assert owned_host_command.verify_completed(snapshot)['passed'] is True
        return {'name': name, 'receipt': reference, 'argv': argv, 'cwd': str(self.private),
                'environment_sha256': environment_sha}, snapshot

    def _build_request(self):
        command, snapshot = self.historical_command()
        self.historical_command_record = snapshot
        self.historical_command_reference = command['receipt']
        operation = self.applied_accounting['operations']['recovery-test-1']
        start = operation['reservation']
        preserved = self.private / 'preserved-runtime-state.json'
        config_ref = write(preserved, {'identity': 'unchanged fixture runtime'})
        pidfiles = {}
        pidrefs = []
        for name in ('ha.pid', 'vz.pid'):
            path = self.private / name
            path.write_text(str(self.preserved_process['pid']) + '\n')
            pidfiles[name] = str(self.preserved_process['pid'])
            pidrefs.append(amendment._file(path)[1])
        failed_clock = copy.deepcopy(self.applied_accounting['clock'])
        failed_clock['open'] = {'id': start['operation'], 'reserved': start['seconds'],
                                'started_at': start['started_at']}
        failure = {'owner_pid': self.historical_owner['pid'], 'lifecycle_command_started': False,
                   'automatic_retry': False, 'phase': 'exact-boundary-failed',
                   'clock': failed_clock, 'observed_at': start['started_at']}
        failure_ref = amendment._retain(self.private / 'failed-runtime.json', failure)
        absent = str(self.private / 'forbidden-lifecycle-output.json')
        effects = {'owner_pid': self.historical_owner['pid'], 'outcome': 'held-before-lifecycle',
                   'runtime_qualified': False, 'all_launched_commands_ended': True,
                   'failure': failure_ref, 'attempted_roles': [command['name']], 'absent_paths': [absent],
                   'old_vm': {'identity': self.preserved_process, 'config_sha256': config_ref['sha256']},
                   'commands': {command['name']: {'reference': command['receipt'], 'passed': True,
                                                'cleanup_verified': True, 'child': {'exit_code': 0}}}}
        effects_ref = amendment._retain(self.private / 'known-effects.json', effects)
        settlement = {'owner_pid': self.historical_owner['pid'], 'outcome': 'failed-held',
                      'runtime_qualified': False, 'new_attempt_authorized': False,
                      'full_charge_seconds': start['seconds'], 'completion': operation['completion'],
                      'evidence': effects_ref, 'clock': self.applied_accounting['clock']}
        settlement_ref = amendment._retain(self.private / 'settlement.json', settlement)
        closed = {'owner_pid': self.historical_owner['pid'], 'foreground_session_exit_code': 0,
                  'assignments': 0, 'lifecycle': {'execution': 'held', 'control': 'none', 'admitted': 0},
                  'clock': self.applied_accounting['clock']}
        closed_ref = amendment._retain(self.private / 'closed-owner.json', closed)
        output = Path(snapshot['control']) / 'stdout.log'
        output.write_text('Retained synthetic prior probe output.\n')
        controller_lock = self.control / 'controller.lock'
        controller_lock.touch(mode=0o600)
        info = controller_lock.stat()
        absent = str(self.private / 'forbidden-lifecycle-output.json')
        observation = {
            'references': {
                'applied_receipt': amendment._retain(self.private / 'applied-recovery-receipt.json', self.applied_receipt),
                'effects': effects_ref, 'failure': failure_ref, 'settlement': settlement_ref, 'closed_owner': closed_ref},
            'old_vm': {'observation': {'identity': self.preserved_process}, 'config': config_ref,
                       'pidfiles': pidfiles, 'absence_checks': {absent: True}},
            'locks': [{'path': str(controller_lock), 'before': {'device': info.st_dev, 'inode': info.st_ino}}],
            'effects': {'retained_reference_checks': [command['receipt']]}}
        observation_ref = amendment._retain(self.private / 'read-only-observation.json', observation)
        self.proof = {'operation_id': 'recovery-test-1', 'failure': failure_ref, 'effects': effects_ref,
                      'settlement': settlement_ref, 'closed_owner': closed_ref, 'observation': observation_ref,
                      'controller_lock': {'path': str(controller_lock), 'device': info.st_dev, 'inode': info.st_ino},
                      'commands': [command],
                      'retained_outputs': [amendment._file(path)[1] for path in sorted(self.retained_effects.rglob('*')) if path.is_file()],
                      'preserved_files': [config_ref, *pidrefs], 'absent_paths': [absent],
                      'preserved_process': copy.deepcopy(self.preserved_process), 'failure_phase': failure['phase']}
        self.corrective_controller = copy.deepcopy(self.applied_controller)
        self.corrective_controller['controller_sha256'] = self.corrective_source_ref['sha256']
        self.corrective_controller['input_files'] = [
            self.corrective_source_ref if ref == self.after_source else ref
            for ref in self.corrective_controller['input_files']]
        self.corrective_controller['corrective_rebind'] = {'operation_id': 'correct-settled-runtime'}
        self.corrective_controller_path = self.private / 'corrective-controller.json'
        write(self.corrective_controller_path, self.corrective_controller)
        interpreter = Path(sys.executable)
        interpreter_ref = {'path': str(interpreter), 'sha256': hashlib.sha256(interpreter.read_bytes()).hexdigest()}
        self.diagnostic_closure_path = self.private / 'diagnostic-closure.json'
        closure_ref = write(self.diagnostic_closure_path, {'format': 'fixture-diagnostic-closure-v1',
                                                        'files': [self.diagnostic_source_ref]})
        self.corrective_request = {
            'operation_id': 'correct-settled-runtime', 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'],
            'digest': self.applied_receipt['to_concept_digest'], 'decision_id': self.applied_receipt['to_decision_id'],
            'applied_recovery': self.applied_reference, 'applied_engine_directory': str(self.applied_engine),
            'original_controller_contract_path': str(self.applied_controller_path),
            'controller_contract_path': str(self.corrective_controller_path),
            'controller_resource': 'rnf-controller:' + digest(self.applied_controller),
            'source_changes': [{'before': self.after_source, 'after': self.corrective_source_ref}],
            'additional_inputs': [], 'destination_directory': str(self.corrective_destination),
            'concept_update_path': str(self.recovery_update), 'settled_failure': self.proof,
            'diagnostic': {'argv': [str(interpreter), '-I', '-B', str(self.diagnostic_source)], 'cwd': str(self.private),
                           'environment': {'TMPDIR': str(self.private), 'LANG': 'C', 'PYTHONDONTWRITEBYTECODE': '1'},
                           'source_closure': closure_ref, 'sources': [interpreter_ref, closure_ref, self.diagnostic_source_ref]},
            'allowances': {'metadata_attempts': 1, 'metadata_max_seconds': 600,
                           'diagnostic_attempts': 1, 'diagnostic_max_seconds': 60,
                           'runtime_slots': {'corrective-test-1': 3600}, 'effective_limit_seconds': 77100}}

    def rewrite_proof(self, kind, field, value):
        """Change semantic proof content while keeping every declared hash coherent."""
        def save(name, data):
            path = Path(self.proof[name]['path'])
            path.chmod(0o600)
            self.proof[name] = write(path, data)
            path.chmod(0o400)
        data = json.loads(Path(self.proof[kind]['path']).read_text())
        data[field] = value
        save(kind, data)
        if kind == 'failure':
            effects = json.loads(Path(self.proof['effects']['path']).read_text())
            effects['failure'] = self.proof['failure']
            save('effects', effects)
        if kind in ('failure', 'effects'):
            settlement = json.loads(Path(self.proof['settlement']['path']).read_text())
            settlement['evidence'] = self.proof['effects']
            save('settlement', settlement)
        observation = json.loads(Path(self.proof['observation']['path']).read_text())
        for name in ('failure', 'effects', 'settlement', 'closed_owner'):
            observation['references'][name] = self.proof[name]
        save('observation', observation)

    def diagnostic_payload(self, change):
        script = ('import json, os\n'
                  'value = {"format": "aos-corrective-diagnostic-v1", '
                  '"packet_digest": os.environ["AOS_CORRECTIVE_PACKET_DIGEST"], '
                  '"passed": True, "runtime_qualified": False, "model_turns": 0, "product_attempts": 0}\n'
                  'value.update(' + repr(change) + ')\nprint(json.dumps(value))\n')
        self.diagnostic_source.write_text(script)
        self.refresh_diagnostic_source()

    def refresh_diagnostic_source(self):
        self.diagnostic_source_ref = amendment._file(self.diagnostic_source)[1]
        closure_ref = write(self.diagnostic_closure_path, {'format': 'fixture-diagnostic-closure-v1',
                                                        'files': [self.diagnostic_source_ref]})
        plan = self.corrective_request['diagnostic']
        plan['source_closure'] = closure_ref
        plan['sources'] = [plan['sources'][0], closure_ref, self.diagnostic_source_ref]

    def prepare_corrective(self):
        return corrective.prepare(self.lifecycle, self.corrective_request)

    def publish_corrective(self, prepared):
        presented = self.store.present(**prepared['packet']['concept'])
        saved = approve(self.store, self.work, presented['digest'], 'corrective-decision')
        return {'operation_id': self.corrective_request['operation_id'], 'work_id': self.work,
                'expected_work_revision': self.store.read(self.work)['revision'],
                'digest': presented['digest'], 'decision_id': saved['decision_id'],
                'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}

    def approved_corrective(self):
        prepared = self.prepare_corrective()
        return prepared, self.publish_corrective(prepared)

    def qualified_corrective(self):
        prepared, request = self.approved_corrective()
        return prepared, request, corrective.qualify(self.lifecycle, request)

    def rebound(self):
        prepared, request, qualification = self.qualified_corrective()
        receipt = corrective.apply(self.lifecycle, request)
        reference = {'operation_id': request['operation_id'], 'work_id': self.work,
                     'receipt_digest': digest(receipt)}
        return prepared, request, qualification, receipt, reference


@pytest.fixture
def c(tmp_path, monkeypatch):
    return CorrectiveFixture(tmp_path, monkeypatch)


def test_prepare_preserves_settled_charge_and_launches_nothing(c):
    prepared = c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children
    assert prepared['packet']['request'] == c.corrective_request
    assert c.prepare_corrective() == prepared
    assert c.store.read(c.work)['execution'] == 'held'
    assert json.loads((c.control / 'clock.json').read_text())['spent'] == 10500
    c.assert_applied_history_preserved()


def test_supported_saved_decision_metadata_diagnostic_apply_verify_and_replay(c):
    _, request, qualification = c.qualified_corrective()
    assert corrective.qualify(c.lifecycle, request) == qualification
    receipt = corrective.apply(c.lifecycle, request)
    reference = {'operation_id': request['operation_id'], 'work_id': c.work, 'receipt_digest': digest(receipt)}
    assert len(c.recovery_calls) == 2 and len(c.children) == 1
    assert c.recovery_calls[-1]['max_attempts'] == 1
    assert c.recovery_calls[-1]['max_seconds'] == 600
    assert c.recovery_calls[-1]['native_checks'] == []
    assert corrective.verify(c.store, reference) == receipt
    assert corrective.apply(c.lifecycle, request) == receipt
    assert len(c.recovery_calls) == 2 and len(c.children) == 1
    assert receipt['effective_limit_seconds'] == 77100
    assert receipt['runtime_slots'] == {'corrective-test-1': 3600}
    assert receipt['accounting_origin'] == c.original_receipt['accounting_origin']
    with c.store._connection() as db:
        row = db.execute('SELECT execution,control,admitted FROM lifecycle WHERE work_id=?', (c.work,)).fetchone()
        assert tuple(row) == ('held', 'none', 0)
        assert db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (c.work,)).fetchone()[0] == 0
    c.assert_applied_history_preserved()


def test_new_runtime_charge_preserves_every_prior_authority(c):
    _, _, _, receipt, reference = c.rebound()
    charge(c, receipt, 'corrective-test-1', seconds=3600)
    accounting = corrective.current_accounting(c.store, receipt, settled=True)
    assert accounting['clock']['spent'] == 14100
    assert accounting['clock']['active_limit'] == 21600
    assert accounting['clock']['deadline'] == c.applied_accounting['clock']['deadline']
    assert set(accounting['used_slots']) >= {'baseline-commit', 'colima-transition', 'recovery-test-1', 'corrective-test-1'}
    assert corrective.verify(c.store, reference) == receipt
    c.assert_applied_history_preserved(clock_unchanged=False)


@pytest.mark.parametrize('field', ['decision_id', 'digest', 'expected_work_revision'])
def test_stale_saved_authority_rejects_before_new_execution(c, field):
    _, request = c.approved_corrective()
    old = {'decision_id': c.applied_request['decision_id'], 'digest': c.applied_request['digest'],
           'expected_work_revision': request['expected_work_revision'] - 1}
    request[field] = old[field]
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(c.recovery_calls) == 1 and not c.children
    c.assert_applied_history_preserved()


def test_apply_requires_completed_metadata_and_diagnostic(c):
    _, request = c.approved_corrective()
    with pytest.raises(Rejected):
        corrective.apply(c.lifecycle, request)
    assert len(c.recovery_calls) == 1 and not c.children
    c.assert_applied_history_preserved()


@pytest.mark.parametrize('phase', ['prepare', 'qualify', 'apply'])
def test_source_drift_rejects_at_each_gate(c, phase):
    if phase == 'prepare':
        command = c.prepare_corrective
    elif phase == 'qualify':
        _, request = c.approved_corrective()
        command = lambda: corrective.qualify(c.lifecycle, request)
    else:
        _, request, _ = c.qualified_corrective()
        command = lambda: corrective.apply(c.lifecycle, request)
    c.corrective_source.write_text('# Unapproved source change\n')
    with pytest.raises(Rejected):
        command()
    assert c.store.read(c.work)['execution'] == 'held'
    c.assert_applied_history_preserved()


@pytest.mark.parametrize('field,value', [
    ('runtime_slots', {'corrective-test-1': 3601}),
    ('runtime_slots', {'recovery-test-1': 3600}),
    ('runtime_slots', {'corrective-test-1': 3600, 'extra': 3600}),
    ('effective_limit_seconds', 77101), ('metadata_attempts', 2), ('diagnostic_attempts', 2),
])
def test_corrective_allowances_cannot_be_reused_or_enlarged(c, field, value):
    c.corrective_request['allowances'][field] = value
    with pytest.raises((Rejected, ValueError)):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


@pytest.mark.parametrize('outcome', ['failed', 'interrupt'])
def test_metadata_failure_or_interruption_consumes_attempt(c, outcome):
    _, request = c.approved_corrective()
    c.metadata_outcome = outcome
    with pytest.raises((Rejected, KeyboardInterrupt)):
        corrective.qualify(c.lifecycle, request)
    assert len(c.recovery_calls) == 2 and len(c.children) == 1
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    with pytest.raises(Rejected):
        corrective.apply(c.lifecycle, request)
    assert len(c.recovery_calls) == 2 and len(c.children) == 1
    c.assert_applied_history_preserved()


def test_concurrent_nested_qualification_launches_each_stage_at_most_once(c, monkeypatch):
    _, request = c.approved_corrective()
    provider = c.supplemental_metadata
    def concurrent(recipe, **kwargs):
        with pytest.raises(Rejected):
            corrective.qualify(c.lifecycle, request)
        return provider(recipe, **kwargs)
    monkeypatch.setattr(corrective.readiness, 'run_readiness', concurrent)
    corrective.qualify(c.lifecycle, request)
    assert len(c.recovery_calls) == 2 and len(c.children) == 1


def test_interrupted_apply_rolls_back_then_identical_retry_applies_once(c, monkeypatch):
    _, request, _ = c.qualified_corrective()
    revision = c.store.read(c.work)['revision']
    with c.store._connection() as db:
        contract_before = db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (c.work,)).fetchone()[0]
    original_touch = c.lifecycle._touch
    def interrupted(*args, **kwargs):
        original_touch(*args, **kwargs)
        raise KeyboardInterrupt('Fixture interruption after contract compare-and-swap')
    with monkeypatch.context() as patch:
        patch.setattr(c.lifecycle, '_touch', interrupted)
        with pytest.raises(KeyboardInterrupt):
            corrective.apply(c.lifecycle, request)
    assert c.store.read(c.work)['revision'] == revision
    with c.store._connection() as db:
        assert db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (c.work,)).fetchone()[0] == contract_before
    receipt = corrective.apply(c.lifecycle, request)
    assert corrective.apply(c.lifecycle, request) == receipt
    assert len(c.recovery_calls) == 2 and len(c.children) == 1
    c.assert_applied_history_preserved()


def test_static_preview_creates_no_intent_and_observes_no_processes(c, monkeypatch):
    def forbidden(pid):
        raise AssertionError('Static preview must not inspect live identities')
    monkeypatch.setattr(native_processes, 'identity', forbidden)
    result = corrective.preview(c.lifecycle, c.corrective_request)
    assert result['approval_recorded'] is False and result['intent_created'] is False
    assert len(c.recovery_calls) == 1 and not c.children and not c.census_calls
    assert not (c.corrective_destination / 'packet.json').exists()
    with c.store._connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (corrective.INTENT,)).fetchone()


@pytest.mark.parametrize('kind,field,value', [
    ('failure', 'lifecycle_command_started', True), ('failure', 'automatic_retry', True),
    ('effects', 'all_launched_commands_ended', False), ('effects', 'runtime_qualified', True),
    ('effects', 'attempted_roles', ['probe-before-failure', 'unknown-command']),
    ('settlement', 'new_attempt_authorized', True), ('settlement', 'full_charge_seconds', 0),
    ('closed_owner', 'foreground_session_exit_code', 1), ('closed_owner', 'foreground_session_exit_code', False),
    ('closed_owner', 'assignments', False),
])
def test_unknown_or_contradictory_effects_never_prepare(c, kind, field, value):
    c.rewrite_proof(kind, field, value)
    with pytest.raises(Rejected):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


@pytest.mark.parametrize('change', ['owner-alive', 'preserved-birth', 'preserved-zombie', 'output', 'preserved-file', 'absent-path', 'lock-inode'])
def test_preservation_and_positive_owner_absence_are_live_gates(c, monkeypatch, change):
    if change in ('owner-alive', 'preserved-birth', 'preserved-zombie'):
        identity = native_processes.identity
        def changed(pid):
            if change == 'owner-alive' and pid == c.historical_owner['pid']:
                return c.historical_owner
            value = identity(pid)
            if change == 'preserved-birth' and pid == c.preserved_process['pid']:
                value['birth'][1] += 1
            if change == 'preserved-zombie' and pid == c.preserved_process['pid']:
                value['status'] = 5
            return value
        monkeypatch.setattr(native_processes, 'identity', changed)
    elif change in ('output', 'preserved-file'):
        key = 'retained_outputs' if change == 'output' else 'preserved_files'
        Path(c.proof[key][0]['path']).write_text('Unapproved changed bytes\n')
    elif change == 'absent-path':
        Path(c.proof['absent_paths'][0]).write_text('{}\n')
    else:
        path = Path(c.proof['controller_lock']['path'])
        path.rename(path.with_suffix('.old'))
        path.touch(mode=0o600)
    with pytest.raises(Rejected):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


def test_executor_receipt_is_verified_independently_of_effect_summary(c):
    path = Path(c.historical_command_record['control']) / 'receipt.json'
    record = json.loads(path.read_text())
    record['cleanup_verified'] = False
    write(path, record)
    with pytest.raises(Rejected):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


@pytest.mark.parametrize('change', [
    {'packet_digest': 'sha256:' + '0' * 64}, {'passed': False}, {'runtime_qualified': True},
    {'model_turns': 1}, {'product_attempts': 1}, {'model_turns': False}, {'product_attempts': False},
])
def test_diagnostic_cannot_claim_unapproved_result_or_effects(c, change):
    c.diagnostic_payload(change)
    _, request = c.approved_corrective()
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    with pytest.raises(Rejected):
        corrective.apply(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    c.assert_applied_history_preserved()


def test_interrupted_diagnostic_reservation_cannot_launch_again(c, monkeypatch):
    _, request = c.approved_corrective()
    original_run = owned_host_command.run_owned
    launches = []
    def interrupted(*args, **kwargs):
        launches.append(True)
        original_run(*args, **kwargs)
        raise KeyboardInterrupt('Fixture interruption after harmless diagnostic settled')
    monkeypatch.setattr(owned_host_command, 'run_owned', interrupted)
    with pytest.raises(KeyboardInterrupt):
        corrective.qualify(c.lifecycle, request)
    assert len(launches) == len(c.children) == 1 and len(c.recovery_calls) == 1
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    with pytest.raises(Rejected):
        corrective.apply(c.lifecycle, request)
    assert len(launches) == len(c.children) == 1 and len(c.recovery_calls) == 1
    c.assert_applied_history_preserved()


def test_different_operation_id_cannot_replenish_reserved_correction(c):
    c.prepare_corrective()
    different = copy.deepcopy(c.corrective_request)
    different['operation_id'] = 'another-corrective-attempt'
    with pytest.raises(Rejected):
        corrective.prepare(c.lifecycle, different)
    assert len(c.recovery_calls) == 1 and not c.children


def test_both_qualification_allowances_must_fit_before_reservation(c, monkeypatch):
    _, request = c.approved_corrective()
    deadline = c.applied_accounting['clock']['deadline']
    monkeypatch.setattr(corrective, 'time', types.SimpleNamespace(
        time=lambda: deadline - 659, monotonic=time.monotonic))
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert not (c.corrective_destination / 'qualification-reservation.json').exists()
    assert len(c.recovery_calls) == 1 and not c.children
    c.assert_applied_history_preserved()


def test_elapsed_diagnostic_cannot_extend_original_metadata_deadline(c, monkeypatch):
    _, request = c.approved_corrective()
    deadline = c.applied_accounting['clock']['deadline']
    observed = [time.time()]
    monkeypatch.setattr(corrective, 'time', types.SimpleNamespace(
        time=lambda: observed[0], monotonic=time.monotonic))
    original_run = owned_host_command.run_owned
    def elapsed(*args, **kwargs):
        result = original_run(*args, **kwargs)
        observed[0] = deadline - 599
        return result
    monkeypatch.setattr(owned_host_command, 'run_owned', elapsed)
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    assert (c.corrective_destination / 'qualification-reservation.json').is_file()
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    c.assert_applied_history_preserved()


def test_applied_history_verifies_after_preserved_runtime_files_are_retired(c):
    _, _, _, receipt, reference = c.rebound()
    for ref in c.proof['preserved_files']:
        Path(ref['path']).unlink()
    assert corrective.verify(c.store, reference) == receipt
    charge(c, receipt, 'corrective-test-1', seconds=3600)
    assert corrective.current_accounting(c.store, receipt, settled=True)['clock']['spent'] == 14100
    assert corrective.verify(c.store, reference) == receipt


def test_tampered_embedded_runtime_snapshot_is_not_historical_evidence(c):
    prepared = c.prepare_corrective()
    packet = copy.deepcopy(prepared['packet'])
    packet['metadata']['settled_failure']['preserved_file_snapshots'][0]['bytes_base64'] = 'dGFtcGVyZWQ='
    with pytest.raises(Rejected):
        corrective._validate_packet(c.store, packet, snapshot=False)
    assert len(c.recovery_calls) == 1 and not c.children


def test_every_retained_effect_file_must_be_explicitly_bound(c):
    c.proof['retained_outputs'].pop()
    with pytest.raises(Rejected):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


@pytest.mark.parametrize('identity_change', [None, 'zombie', 'reparented', 'regrouped'])
def test_owned_diagnostic_child_reads_actual_reserved_authority(c, identity_change):
    c.diagnostic_source.write_text(
        'import hashlib, json, os, sys\n'
        'sys.path.insert(0, ' + repr(str(Path(corrective.__file__).parents[2])) + ')\n'
        'from pathlib import Path\n'
        'from agentic_tools.aos import predispatch_corrective_rebind as correction, native_processes\n'
        'path = Path(os.environ["AOS_CORRECTIVE_GUARD_PATH"])\n'
        'raw = path.read_bytes()\n'
        'guard = json.loads(raw)\n'
        'def identity(pid):\n'
        '    for value in (guard["supervisor"], guard["coordinator"]):\n'
        '        if value["pid"] == pid:\n'
        '            result = dict(value)\n'
        '            change = ' + repr(identity_change) + '\n'
        '            if change == "zombie" and pid == guard["coordinator"]["pid"]: result["status"] = 5\n'
        '            if change == "reparented" and pid == guard["supervisor"]["pid"]: result["parent"] += 1\n'
        '            if change == "regrouped" and pid == guard["supervisor"]["pid"]: result["group"] += 1\n'
        '            return result\n'
        '    raise AssertionError("No host identity inspection in fixture")\n'
        'native_processes.identity = identity\n'
        'authority = correction.diagnostic_authority(str(path), hashlib.sha256(raw).hexdigest())\n'
        'print(json.dumps({"format": "aos-corrective-diagnostic-v1", '
        '"packet_digest": authority["packet_digest"], "passed": True, '
        '"runtime_qualified": False, "model_turns": 0, "product_attempts": 0, '
        '"observations": {"reserved_authority_read": True}}))\n')
    c.refresh_diagnostic_source()
    if identity_change is not None:
        _, request = c.approved_corrective()
        with pytest.raises(Rejected):
            corrective.qualify(c.lifecycle, request)
        stderr = (c.corrective_destination / 'diagnostic/stderr.log').read_text()
        assert 'original owned process chain' in stderr
        assert len(c.children) == 1 and len(c.recovery_calls) == 1
        return
    _, request, qualification, receipt, reference = c.rebound()
    output = json.loads(Path(qualification['diagnostic']['output']['path']).read_text())
    assert output['observations']['reserved_authority_read'] is True
    assert corrective.verify(c.store, reference) == receipt
    guard = qualification['diagnostic']['guard']
    with pytest.raises(Rejected):
        corrective.diagnostic_authority(guard['path'], guard['sha256'])
    assert len(c.children) == 1 and len(c.recovery_calls) == 2


@pytest.mark.parametrize('field', ['packet', 'request', 'reservation', 'feature_deadline', 'extra-field', 'duration'])
def test_diagnostic_guard_cannot_rebind_consumed_saved_authority(c, field):
    prepared, request, qualification = c.qualified_corrective()
    expected = {'packet_ref': qualification['packet'], 'request': request,
                'reservation_ref': qualification['reservation']}
    corrective._diagnostic_result(c.store, prepared['packet'], qualification['diagnostic'], **expected)
    value = copy.deepcopy(qualification['diagnostic'])
    path = Path(value['guard']['path'])
    guard = json.loads(path.read_text())
    if field == 'packet':
        guard['packet']['sha256'] = '0' * 64
    elif field == 'request':
        guard['request']['expected_work_revision'] += 1
    elif field == 'reservation':
        guard['reservation']['sha256'] = '0' * 64
    elif field == 'feature_deadline':
        guard['feature_deadline'] += 1
    elif field == 'duration':
        guard['started_monotonic'] -= 1
    else:
        guard['unapproved_extension'] = True
    path.chmod(0o600)
    value['guard'] = write(path, guard)
    path.chmod(0o400)
    with pytest.raises(Rejected):
        corrective._diagnostic_result(c.store, prepared['packet'], value, **expected)
    assert len(c.children) == 1 and len(c.recovery_calls) == 2


@pytest.mark.parametrize('resolved', [False, True])
def test_diagnostic_interpreter_cannot_overlap_worker_roots(c, monkeypatch, resolved):
    interpreter = Path(sys.executable)
    if resolved:
        interpreter = interpreter.resolve()
    monkeypatch.setattr(c.store, 'worker_writable_roots', (*c.store.worker_writable_roots, interpreter.parent))
    with pytest.raises(Rejected):
        corrective._diagnostic_plan(c.store, c.corrective_request)
    assert len(c.recovery_calls) == 1 and not c.children


def test_concurrent_preparations_have_one_durable_owner(c, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import threading
    competing = copy.deepcopy(c.corrective_request)
    competing['operation_id'] = 'competing-corrective-request'
    directory = c.private / 'competing-corrective'
    directory.mkdir(mode=0o700)
    competing['destination_directory'] = str(directory)
    controller = copy.deepcopy(c.corrective_controller)
    controller['corrective_rebind'] = {'operation_id': competing['operation_id']}
    path = c.private / 'competing-controller.json'
    write(path, controller)
    competing['controller_contract_path'] = str(path)
    gate = threading.Barrier(2)
    original = corrective._fences
    @contextmanager
    def simultaneous(*args, **kwargs):
        gate.wait(timeout=20)
        with original(*args, **kwargs) as fence:
            yield fence
    monkeypatch.setattr(corrective, '_fences', simultaneous)
    def prepare(request):
        try:
            return corrective.prepare(c.lifecycle, request)
        except Rejected as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, (c.corrective_request, competing)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(isinstance(value, Rejected) for value in results) == 1
    with c.store._connection() as db:
        assert db.execute('SELECT count(*) FROM ' + corrective.INTENT).fetchone()[0] == 1
    assert len(c.recovery_calls) == 1 and not c.children


@pytest.mark.parametrize('prefix', [[], ['-I'], ['-B']])
def test_diagnostic_requires_isolated_no_bytecode_python_prefix(c, prefix):
    argv = c.corrective_request['diagnostic']['argv']
    c.corrective_request['diagnostic']['argv'] = [argv[0], *prefix, argv[-1]]
    with pytest.raises((Rejected, ValueError)):
        c.prepare_corrective()
    assert len(c.recovery_calls) == 1 and not c.children


def test_isolated_diagnostic_ignores_ambient_shadow_modules(c):
    (c.private / 'json.py').write_text('raise RuntimeError("Ambient json shadow was imported")\n')
    (c.private / 'sitecustomize.py').write_text('raise RuntimeError("Ambient startup hook was imported")\n')
    c.corrective_request['diagnostic']['environment']['PYTHONPATH'] = str(c.private)
    c.qualified_corrective()
    assert len(c.children) == 1 and len(c.recovery_calls) == 2


@pytest.mark.parametrize('phase', ['before-diagnostic-release', 'after-diagnostic'])
@pytest.mark.parametrize('change', ['revision', 'current-concept'])
def test_stale_authority_between_stages_never_spends_metadata(c, monkeypatch, phase, change):
    _, request = c.approved_corrective()
    mutations = []
    def stale():
        mutations.append(True)
        with c.store._connection() as db:
            if change == 'revision':
                c.lifecycle._touch(db, c.work, 'fixture_concurrent_change', {'scope': 'synthetic only'})
            else:
                db.execute('UPDATE work SET current_concept=? WHERE work_id=?',
                           (c.applied_receipt['registered_contract']['concept_revision'], c.work))
            db.commit()
    if phase == 'after-diagnostic':
        original = corrective._diagnostic_result
        def after_verified(*args, **kwargs):
            result = original(*args, **kwargs)
            stale()
            return result
        monkeypatch.setattr(corrective, '_diagnostic_result', after_verified)
    else:
        original = owned_host_command.run_owned
        def before_release(*args, **kwargs):
            before = kwargs['before_go']
            def changed(deadline):
                stale()
                return before(deadline)
            kwargs['before_go'] = changed
            return original(*args, **kwargs)
        monkeypatch.setattr(owned_host_command, 'run_owned', before_release)
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(mutations) == 1 and len(c.children) == 1 and len(c.recovery_calls) == 1
    assert (c.corrective_destination / 'qualification-reservation.json').is_file()
    assert not (c.corrective_destination / 'readiness').exists()
    if phase == 'before-diagnostic-release':
        execution = json.loads((c.corrective_destination / 'diagnostic-execution.json').read_text())
        assert execution['launch_authorized'] is False and execution['child'] is None
        assert not (c.corrective_destination / 'diagnostic/status.json').exists()
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    with pytest.raises(Rejected):
        corrective.apply(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    c.assert_applied_history_preserved()


def test_metadata_deadline_is_rechecked_after_last_authority_guard(c, monkeypatch):
    _, request = c.approved_corrective()
    observed = [time.time()]
    deadline = c.applied_accounting['clock']['deadline']
    monkeypatch.setattr(corrective, 'time', types.SimpleNamespace(
        time=lambda: observed[0], monotonic=time.monotonic))
    original = corrective._guard
    guards = []
    def aging_guard(*args, **kwargs):
        result = original(*args, **kwargs)
        guards.append(True)
        if len(guards) == 3:  # Initial qualify, diagnostic release, then metadata handoff.
            observed[0] = deadline - 599
        return result
    monkeypatch.setattr(corrective, '_guard', aging_guard)
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(guards) == 3 and len(c.children) == 1 and len(c.recovery_calls) == 1
    directory = c.corrective_destination / 'readiness'
    assert not directory.exists() or not list(directory.iterdir())
    assert (c.corrective_destination / 'qualification-reservation.json').is_file()
    with pytest.raises(Rejected):
        corrective.qualify(c.lifecycle, request)
    assert len(c.children) == 1 and len(c.recovery_calls) == 1
    c.assert_applied_history_preserved()


@pytest.mark.parametrize('change', ['new-birth', 'same-birth', 'unreadable', 'malformed', 'contradictory-uid'])
def test_historical_owner_pid_reuse_requires_positive_different_birth(c, monkeypatch, change):
    _, _, _, receipt, reference = c.rebound()
    original = native_processes.identity
    def identity(pid):
        if pid != c.historical_owner['pid']:
            return original(pid)
        if change == 'unreadable':
            raise PermissionError('Fixture identity unavailable')
        current = copy.deepcopy(c.historical_owner)
        if change == 'new-birth':
            current['birth'] = [900, 1]
        elif change == 'malformed':
            current['birth'] = [900]
        elif change == 'contradictory-uid':
            current['uid'] += 1
        return current
    monkeypatch.setattr(native_processes, 'identity', identity)
    signals = []
    def forbidden_signal(*args):
        signals.append(args)
        raise AssertionError('Historical verification must not signal any process')
    monkeypatch.setattr(owned_host_command.os, 'kill', forbidden_signal)
    if change != 'new-birth':
        with pytest.raises((Rejected, ValueError)):
            corrective.verify(c.store, reference)
        assert not signals
        return
    assert corrective.verify(c.store, reference) == receipt
    charge(c, receipt, 'corrective-test-1', seconds=3600)
    assert corrective.current_accounting(c.store, receipt, settled=True)['clock']['spent'] == 14100
    assert corrective.verify(c.store, reference) == receipt
    assert not signals
    c.assert_applied_history_preserved(clock_unchanged=False)
