"""Disposable state and synthetic metadata receipts only; no real readiness/model."""
from di_plan_fixture import synthetic_plan
import copy
import fcntl
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from agentic_tools.aos import instructions
from agentic_tools.aos import predispatch_settings_rebind as rebind
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Store, Rejected, canonical, digest
from test_aos_lifecycle import contract
from test_aos_store import TEAM as BASE_TEAM
TEAM = [{**BASE_TEAM[0], 'name': name, 'role': role} for name, role in (('writer', 'Implementer'), ('assessor', 'Independent assessor'))]
from test_aos_legacy_recovery import legacy


def write(path, value):
    path.write_text(canonical(value) + '\n')
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def approve(store, work, concept_digest, operation):
    # Explicit fixture seam: this is no claim of an actual user interaction.
    cap = store.host_capability('fixture-qualified-host', 'SIMULATED:unit-test-only')
    challenge = store.begin_approval(operation, work, store.read(work)['revision'], concept_digest, capability=cap)
    return store.finish_approval(challenge['challenge_id'], 'accept', True, capability=cap,
        exact_answer='Approve fixture', source_channel='fixture-host', interaction_reference=operation)['receipt']


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path.resolve()
        self.work = 'RF-FIXTURE'
        self.source, self.outputs, self.private = [self.root / p for p in ('source', 'outputs', 'private')]
        for p in (self.source, self.outputs, self.private): p.mkdir(mode=0o700)
        self.home = self.root / 'home'; self.home.mkdir(mode=0o700)
        (self.home / '.codex').mkdir(mode=0o700)
        self.config = self.home / '.codex/config.toml'; self.config.write_text('model = "old-model"\n')
        monkeypatch.setenv('HOME', str(self.home))
        (self.source / 'fixture.py').write_text('VALUE = 1\n')
        self.store = Store(self.root / 'state', (self.source, self.outputs))
        self.lifecycle = Lifecycle(self.store)
        self.store.introduce('intro', self.work, 'Fixture', ['source'], TEAM)
        self.recipe = {'execution_policy': 'isolated-checkout-v1', 'source': str(self.source), 'outputs': str(self.outputs),
            'read_roots': [], 'native_write_roots': [], 'native_protected_paths': [], 'max_seconds': 14400,
            'probe_interpreter': str(Path(sys.executable).resolve()), 'requires_detached_processes': False,
            'native_process_ownership': 'host-owned-v1', 'implementation_prompt': 'Fixture writer', 'assessment_prompt': 'Fixture reviewer',
            'checks': [{'name': 'fixture', 'argv': ['python', '-m', 'pytest'], 'scope': 'Fixture', 'timeout_seconds': 600}],
            'checkout_policy': {'change_roots': ['fixture.py'], 'protected_roots': [], 'native_validation': 'reviewed-host-v1'},
            'instruction_manifest': instructions.discover(self.source)}
        self.readiness = self.private / 'readiness'; self.readiness.mkdir(mode=0o700)
        (self.readiness / 'control').mkdir(mode=0o700)
        self.journal = self.readiness / 'control/readiness.json'
        initial = self.result(self.recipe, 'readiness-1')
        initial_ref = write(self.readiness / 'control/readiness-1.json', initial)
        write(self.journal, {'format': 'aos-readiness-v1', 'state_dir': str(self.store.directory), 'source': str(self.source),
            'max_seconds': 600, 'max_attempts': 3, 'runs': [self.entry('readiness-1', initial_ref)]})
        inputs, hashes = [], {}
        for key in rebind.CODE_FIELDS:
            p = self.private / (key + '.py'); p.write_text('# original ' + key + '\n')
            ref = rebind._file(p)[1]; inputs.append(ref); hashes[key] = ref['sha256']
        inputs.append(initial_ref)
        self.control = self.private / 'control'; self.control.mkdir(mode=0o700)
        (self.control / 'clock.lock').touch(mode=0o600)
        self.old_controller = {'format': 'rnf-controller-contract-v1', 'work_id': self.work,
            'state_dir': str(self.store.directory), 'worker_roots': list(map(str, self.store.worker_writable_roots)),
            'control_root': str(self.control), 'wall_seconds': 172800, 'active_seconds': 21600,
            'coordinator_operations': ['baseline-commit'], 'input_files': inputs, **hashes,
            'baseline_inventory': {'fixture.py': {'sha256': hashlib.sha256((self.source / 'fixture.py').read_bytes()).hexdigest(),
                'size': (self.source / 'fixture.py').stat().st_size, 'mode': '100644'}}}
        self.old_path = self.private / 'old-controller.json'; write(self.old_path, self.old_controller)
        self.new_controller = copy.deepcopy(self.old_controller)
        for key in rebind.CODE_FIELDS:
            p = self.private / ('new-' + key + '.py'); p.write_text('# new ' + key + '\n')
            ref = rebind._file(p)[1]; self.new_controller['input_files'].append(ref); self.new_controller[key] = ref['sha256']
        self.new_path = self.private / 'new-controller.json'; write(self.new_path, self.new_controller)
        bounds = contract({'digest': 'placeholder'}, 'feature-change', execution_policy='isolated-checkout-v1',
            max_assignments=10, max_seconds=14400, max_startup_retries=2,
            resources=['source', 'rnf-controller:' + digest(self.old_controller)], runtime_recipe=self.recipe,
            runtime_recipe_digest=digest(self.recipe), product_budget={'budget_id': 'fixture-budget', 'product_id': 'fixture',
                'ledger_directory': str(self.store.directory), 'max_assignments': 10, 'max_job_seconds': 14400, 'max_seconds': 144000})
        bounds.pop('concept_revision'); bounds.pop('concept_digest')
        old = self.store.present('present', self.work, 1, 'FIXTURE-DI', 1, 'Unchanged feature', ['Check fixture'], TEAM,
                                 bounds['authority'], execution_envelope=bounds, di_plan=synthetic_plan(bounds))
        self.digest = old['digest']
        self.lifecycle.register('register', self.work, {**bounds, 'concept_revision': 1, 'concept_digest': self.digest})
        self.decision = approve(self.store, self.work, self.digest, 'original-decision')
        approved = datetime.fromisoformat(self.decision['created_at']).timestamp()
        start = {'identity': self.digest, 'operation': 'baseline-commit', 'seconds': 600, 'started_at': approved}
        write(self.control / 'operation-baseline-commit.json', start)
        write(self.control / 'completed-baseline-commit.json', {'start_digest': digest(start), 'completed_at': approved})
        write(self.control / 'clock.json', {'identity': self.digest, 'approved_at': approved, 'deadline': approved + 172800,
            'active_limit': 21600, 'spent': 600, 'last': approved, 'open': None, 'held': False})
        with sqlite3.connect(self.store.directory / 'codex-runtime.sqlite3') as db:
            db.execute('CREATE TABLE attempt (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE native_check (id TEXT PRIMARY KEY,attempt_id TEXT,record TEXT NOT NULL)')
            db.execute('CREATE TABLE runtime_budget (id TEXT PRIMARY KEY,record TEXT NOT NULL)')
            db.execute('CREATE TABLE product_budget_scope (scope TEXT PRIMARY KEY,budget_id TEXT NOT NULL)')
        (self.store.directory / 'codex-runtime.sqlite3').chmod(0o600)
        self.destination = self.private / 'refresh'; self.destination.mkdir(mode=0o700)
        self.request = {'operation_id': 'refresh-settings', 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'], 'digest': self.digest,
            'decision_id': self.decision['decision_id'], 'original_controller_contract_path': str(self.old_path),
            'controller_contract_path': str(self.new_path), 'original_readiness_receipt_path': initial_ref['path'],
            'readiness_directory': str(self.readiness), 'destination_directory': str(self.destination)}
        self.config.write_text('model = "new-model"\n')
        self.calls = []
        monkeypatch.setattr(rebind.readiness, 'run_readiness', self.metadata)

    def result(self, recipe, rid):
        return {'id': rid, 'ready': True, 'status': 'passed', 'simulated': False, 'model_turns': 0, 'product_attempts': 0,
            'limits': {'max_attempts': 3, 'max_seconds': 600},
            'product_reservations': 0, 'cleanup_verified': True, 'readiness_scope': 'model-metadata-only',
            'requested_native_checks': [], 'native': {'checks': [], 'execution': 'not_requested'},
            'roles': [{'role': role, 'passed': True, 'configuration_verified': True, 'model_turns': 0,
                'thread_id': 'fixture-only', 'cleanup': {'verified': True, 'no_launch': True}} for role in ['implementation', 'assessment']],
            'prepared_recipe': copy.deepcopy(recipe), 'runtime_identity': rebind.readiness._runtime_identity(),
            'resource_output': str(self.readiness / 'outputs' / rid)}

    def entry(self, rid, ref):
        return {'id': rid, 'input_key': 'fixture', 'receipt': ref['path'], 'sha256': ref['sha256'], 'state': 'passed'}

    def metadata(self, recipe, **kwargs):
        self.calls.append(kwargs)
        journal = json.loads(self.journal.read_text()); rid = 'readiness-' + str(len(journal['runs']) + 1)
        result = self.result(recipe, rid); ref = write(self.readiness / 'control' / (rid + '.json'), result)
        journal['runs'].append(self.entry(rid, ref)); write(self.journal, journal)
        return result

    def prepare(self):
        return rebind.prepare(self.lifecycle, self.request)

    def publish(self, prepared):
        presented = self.store.present(**prepared['packet']['concept'])
        saved = approve(self.store, self.work, presented['digest'], 'technical-decision')
        return {'operation_id': self.request['operation_id'], 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'], 'digest': presented['digest'],
            'decision_id': saved['decision_id'], 'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}


@pytest.fixture
def f(tmp_path, monkeypatch):
    return Fixture(tmp_path, monkeypatch)


def test_prepare_apply_verify_preserve_clock_and_allow_normal_progress(f):
    before = (f.control / 'clock.json').read_bytes(); work_before = f.store.read(f.work)
    prepared = f.prepare()
    assert f.store.read(f.work)['revision'] == work_before['revision']
    assert len(f.calls) == 1 and f.calls[0]['native_checks'] == [] and f.calls[0]['max_attempts'] == 3
    assert (f.control / 'clock.json').read_bytes() == before
    request = f.publish(prepared); receipt = rebind.apply(f.lifecycle, request)
    assert (f.control / 'clock.json').read_bytes() == before
    assert f.store.read(f.work)['execution'] == 'held'
    with f.store._connection() as db:
        assert db.execute('SELECT admitted FROM lifecycle WHERE work_id=?', (f.work,)).fetchone()[0] == 0
    ref = {'operation_id': request['operation_id'], 'work_id': f.work, 'receipt_digest': digest(receipt)}
    assert rebind.verify(f.store, ref) == receipt
    assert rebind.apply(f.lifecycle, request) == receipt
    clock = json.loads(before); clock['spent'] += 100; write(f.control / 'clock.json', clock)
    assert rebind.verify(f.store, ref) == receipt


@pytest.mark.parametrize('field,value', [('wall_seconds', 172801), ('active_seconds', 21601), ('worker_roots', ['/']), ('coordinator_operations', ['other'])])
def test_prepare_rejects_controller_scope_change(f, field, value):
    f.new_controller[field] = value; write(f.new_path, f.new_controller)
    with pytest.raises(Rejected): f.prepare()
    assert not f.calls


@pytest.mark.parametrize('mutation', ['missing', 'late', 'reversed', 'bool'])
def test_invalid_original_completion_is_not_settled(f, mutation):
    path = f.control / 'completed-baseline-commit.json'; done = json.loads(path.read_text())
    if mutation == 'missing': done.pop('completed_at')
    elif mutation == 'late': done['completed_at'] += 601
    elif mutation == 'reversed': done['completed_at'] -= 1
    else: done['completed_at'] = True
    write(path, done)
    with pytest.raises(Rejected): f.prepare()


def test_orphan_runtime_attempt_blocks_preparation(f):
    with sqlite3.connect(f.store.directory / 'codex-runtime.sqlite3') as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', ('orphan', canonical({'assignment': {'work_id': f.work}, 'model_calls_attempted': 0})))
    with pytest.raises(Rejected, match='retained runtime attempt'): f.prepare()


def test_apply_requires_new_exact_saved_decision(f):
    prepared = f.prepare(); present = f.store.present(**prepared['packet']['concept'])
    request = {'operation_id': f.request['operation_id'], 'work_id': f.work, 'expected_work_revision': f.store.read(f.work)['revision'],
        'digest': present['digest'], 'decision_id': f.decision['decision_id'], 'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}
    with pytest.raises(Rejected, match='approval'): rebind.apply(f.lifecycle, request)


def test_extra_receipt_or_changed_clock_after_prepare_blocks_apply(f):
    prepared = f.prepare(); request = f.publish(prepared)
    write(f.control / 'operation-extra.json', {'identity': f.digest})
    with pytest.raises(Rejected, match='history|reservation'): rebind.apply(f.lifecycle, request)


@pytest.mark.parametrize('field,value', [('max_assignments', 11), ('model', 'another-model'), ('authority', 'broader'), ('prerequisites', {'yes': True}), ('resources', ['other']), ('required_checks', [])])
def test_tampered_prepared_envelope_cannot_expand_scope_even_if_approved(f, field, value):
    prepared = f.prepare(); packet = copy.deepcopy(prepared['packet'])
    packet['concept']['execution_envelope'][field] = value
    path = f.destination / 'tampered.json'; rebind._retain(path, packet)
    prepared = {'packet': packet, 'packet_path': str(path), 'packet_sha256': rebind._file(path)[1]['sha256']}
    if field == 'required_checks':
        # The DI contract now rejects this incomplete gate before a human can approve it.
        before = f.store.read(f.work)
        with pytest.raises(Rejected, match='invalid_di_plan'):
            f.publish(prepared)
        assert f.store.read(f.work)['revision'] == before['revision']
        return
    request = f.publish(prepared)
    with pytest.raises(Rejected, match='allowed settings refresh'): rebind.apply(f.lifecycle, request)


def test_copied_readiness_journal_cannot_reset_original_allowance(f):
    clone = f.private / 'clone'; clone.mkdir(mode=0o700)
    f.request['readiness_directory'] = str(clone)
    with pytest.raises(Rejected, match='original approved journal'): f.prepare()
    assert not f.calls


def test_config_drift_after_prepare_blocks_apply(f):
    prepared = f.prepare(); request = f.publish(prepared)
    f.config.write_text('model = "changed-again"\n')
    with pytest.raises(Rejected, match='instruction_drift'): rebind.apply(f.lifecycle, request)


def test_scope_changes_to_policy_manifest_are_rejected():
    old = {'configs': [{'path': '/a', 'resolved': '/a', 'aliases': [], 'sha256': 'a', 'bytes': 1}], 'chain': [], 'settings': {'limit': 1}}
    new = copy.deepcopy(old); new['configs'][0]['sha256'] = 'b'; new['settings']['limit'] = 2
    with pytest.raises(Rejected): rebind._manifest(old, new)


def test_existing_coordinator_lock_blocks_preparation_without_attempt(f):
    with (f.control / 'clock.lock').open('r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Rejected, match='currently owned'): f.prepare()
    assert not f.calls


def test_missing_clock_lock_is_not_reinitialized(f):
    (f.control / 'clock.lock').unlink()
    with pytest.raises(FileNotFoundError): f.prepare()
    assert not (f.control / 'clock.lock').exists()


def test_technical_decision_context_explains_exact_preserved_authority(f):
    packet = f.prepare()['packet']; concept = packet['concept']
    context = concept['decision_context']; metadata = packet['metadata']
    assert set(context) == {'problem', 'example', 'effort', 'dependencies', 'evidence', 'stopping_point'}
    assert all(isinstance(value, str) and 1 <= len(value) <= 4096 for value in context.values())
    assert f.decision['decision_id'] in context['effort']
    assert '600/21600' in context['effort'] and '2/3 attempts at 600 seconds' in context['effort']
    assert metadata['engine_digest'] in context['dependencies']
    assert 'held until this exact technical package' in context['stopping_point']
    assert concept['authority'] == metadata['original_package']['authority']
    assert concept['acceptance'] == metadata['original_package']['acceptance']


def retired(legacy):
    store, lifecycle, request, evidence, record = legacy
    rebind.legacy_recovery.recover(lifecycle, request, apply=True)
    return store, lifecycle, request, evidence, record


def test_saved_foreign_retirement_is_read_only_and_keeps_unknown_history(legacy):
    store, life, request, _, record = retired(legacy)
    before = store.path.read_bytes(); runtime = store.directory / 'codex-runtime.sqlite3'
    before_runtime = runtime.read_bytes()
    with store._connection() as db:
        assert rebind._retired_foreign(life, db, 'Q1', request['attempt_id'])
    assert store.path.read_bytes() == before and runtime.read_bytes() == before_runtime
    with sqlite3.connect(runtime) as db:
        assert json.loads(db.execute('SELECT record FROM attempt').fetchone()[0]) == record


@pytest.mark.parametrize('mutation', ['event', 'operation', 'evidence', 'runtime', 'budget', 'assignment', 'resource', 'conflict'])
def test_saved_foreign_retirement_rejects_changed_proof(legacy, mutation):
    store, life, request, _, _ = retired(legacy)
    if mutation == 'evidence':
        Path(request['evidence_path']).write_text('{}')
    elif mutation in ('runtime', 'budget'):
        with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
            db.execute('UPDATE ' + ('attempt' if mutation == 'runtime' else 'runtime_budget') + " SET record='{}'")
    else:
        with store._transaction() as db:
            if mutation == 'event': db.execute("DELETE FROM lifecycle_event WHERE kind='legacy_retired'")
            elif mutation == 'operation': db.execute('DELETE FROM operation WHERE operation_id=?', (request['operation_id'],))
            elif mutation == 'assignment': db.execute("UPDATE assignment SET executor='changed'")
            elif mutation == 'resource': db.execute("INSERT INTO resource VALUES('new','Q1',NULL,'owned','later')")
            else: life._touch(db, 'Q1', 'owned_stop_conflict', {'retained': {}, 'received': {}})
    with store._connection() as db, pytest.raises((Rejected, KeyError)):
        rebind._retired_foreign(life, db, 'Q1', request['attempt_id'])


def test_saved_foreign_retirement_retains_original_reservations_when_later_added(legacy):
    store, life, request, _, record = retired(legacy)
    later = {**record, 'attempt_id': 'later', 'budget_seconds': 1800}
    with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', ('later', canonical(later)))
    with store._connection() as db:
        assert rebind._retired_foreign(life, db, 'Q1', request['attempt_id'])
    # The encompassing passive guard separately requires assignment and cleanup
    # for each later attempt; retirement never excuses an orphan reservation.
    with sqlite3.connect(store.directory / 'codex-runtime.sqlite3') as db:
        db.execute("CREATE TABLE native_check(id TEXT PRIMARY KEY,attempt_id TEXT,record TEXT NOT NULL)")
    from test_aos_lifecycle import setup
    setup(store.directory.parent, store=store, work_id='OTHER', admitted=False)
    with rebind.fencing._connection(store) as (db, _), pytest.raises(Rejected, match='no assignment'):
        rebind._passive(life, db, 'OTHER')


@pytest.mark.parametrize('terminal_at', ['invalid', '2020-01-01T00:00:00', '2019-01-01T00:00:00+00:00', '9999-01-01T00:00:00+00:00'])
def test_retired_assignment_time_is_aware_and_bounded_by_saved_event(legacy, terminal_at):
    store, life, request, _, _ = retired(legacy)
    with store._transaction() as db:
        db.execute('UPDATE assignment SET terminal_at=?', (terminal_at,))
    with store._connection() as db, pytest.raises(Rejected, match='time'):
        rebind._retired_foreign(life, db, 'Q1', request['attempt_id'])
