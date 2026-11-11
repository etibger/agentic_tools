"""Disposable full-workflow recovery; saved approval and metadata use fixture seams."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import pytest
from agentic_tools.aos import predispatch_feature_amendment as amendment
from agentic_tools.aos import predispatch_recovery as recovery
from agentic_tools.aos.store import Rejected, digest
from test_aos_predispatch_feature_amendment import Fixture, applied, approve, charge, write


class RecoveryFixture(Fixture):
    READINESS_ATTEMPTS = 2  # Original amendment consumes precisely the third slot.

    def __init__(self, tmp_path, monkeypatch, *, configuration_effort=False):
        super().__init__(tmp_path, monkeypatch)
        if configuration_effort:
            self.config.write_text('model = "new-model"\nmodel_reasoning_effort = "medium"\n')
        # A retained failed check must not be erased by either amendment.
        journal = json.loads(self.journal.read_text())
        first = Path(journal['runs'][0]['receipt'])
        failure = json.loads(first.read_text()); failure.update(ready=False, status='failed')
        ref = write(first, failure); journal['runs'][0].update(state='failed', sha256=ref['sha256'])
        write(self.journal, journal)
        self.original_receipt, self.original_reference, self.original_prepared = applied(self)
        charge(self, self.original_receipt, 'colima-transition')
        self.original_controller = copy.deepcopy(self.original_prepared['packet']['metadata']['controller_contract'])
        self.original_controller_path = self.private / 'applied-controller.json'
        write(self.original_controller_path, self.original_controller)
        self.prior_engine = self.private / 'prior-engine'
        shutil.copytree(Path(amendment.__file__).parent.parent, self.prior_engine,
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.recovery_destination = self.private / 'supplemental'; self.recovery_destination.mkdir(mode=0o700)
        self.before_source = next(r for r in self.original_controller['input_files']
                                  if r['sha256'] == self.original_controller['controller_sha256'])
        self.after_source_path = self.private / 'corrected-controller.py'
        self.after_source_path.write_text('# Corrected bounded helper; synthetic fixture only.\n')
        self.after_source = amendment._file(self.after_source_path)[1]
        self.controller = copy.deepcopy(self.original_controller)
        self.controller['controller_sha256'] = self.after_source['sha256']
        self.controller['input_files'] = [self.after_source if ref == self.before_source else ref
                                          for ref in self.controller['input_files']]
        self.controller['supplemental_recovery'] = {'operation_id': 'recover-protected-process'}
        self.controller_path = self.private / 'recovery-controller.json'; write(self.controller_path, self.controller)
        self.recovery_update = self.private / 'recovery-concept.json'
        write(self.recovery_update, {
            'body': 'Correct one protected-process helper; preserve all original attempts and charges.',
            'decision_context': {k: 'Exact-source recovery; one metadata attempt and one bounded runtime slot.'
                                 for k in ('problem', 'example', 'effort', 'dependencies', 'evidence', 'stopping_point')},
            'recommendation': 'Approve exact supplemental recovery', 'alternative': 'Retain held state',
            'consequence': 'No admission or runtime transition follows automatically'})
        self.recovery_request = {
            'operation_id': 'recover-protected-process', 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'],
            'digest': self.original_receipt['to_concept_digest'], 'decision_id': self.original_receipt['to_decision_id'],
            'prior_amendment': self.original_reference, 'prior_engine_directory': str(self.prior_engine),
            'original_controller_contract_path': str(self.original_controller_path),
            'controller_contract_path': str(self.controller_path),
            'controller_resource': 'rnf-controller:' + digest(self.original_controller),
            'source_changes': [{'before': self.before_source, 'after': self.after_source}], 'additional_inputs': [],
            'readiness_directory': str(self.readiness), 'destination_directory': str(self.recovery_destination),
            'concept_update_path': str(self.recovery_update),
            'allowances': {'metadata_attempts': 1, 'metadata_max_seconds': 600,
                           'runtime_slots': {'recovery-test-1': 3600}, 'effective_limit_seconds': 73500}}
        self.original_bytes = {str(p): p.read_bytes() for p in (
            self.journal, self.control / 'clock.json', *self.control.glob('operation-*.json'),
            *self.control.glob('completed-*.json'), *self.readiness.glob('control/readiness-*.json'))}
        self.original_database = self.history(); self.recovery_calls = []; self.metadata_outcome = 'passed'
        monkeypatch.setattr(recovery.readiness, 'run_readiness', self.supplemental_metadata)

    def history(self):
        with self.store._connection() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                    for name in ('decision', 'decision_readback', amendment.TABLE)}

    def supplemental_metadata(self, recipe, **kwargs):
        """Provider substitute; no actual model, native check or VM command runs."""
        self.recovery_calls.append(copy.deepcopy(kwargs))
        directory = Path(kwargs['readiness_dir']); control = directory / 'control'
        control.mkdir(mode=0o700, parents=True, exist_ok=True); journal_path = control / 'readiness.json'
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
            'format': 'aos-readiness-v1', 'state_dir': str(self.store.directory), 'source': recipe['source'],
            'max_attempts': kwargs['max_attempts'], 'max_seconds': kwargs['max_seconds'], 'runs': []}
        assert len(journal['runs']) < kwargs['max_attempts']
        rid = 'readiness-' + str(len(journal['runs']) + 1)
        entry = {'id': rid, 'input_key': 'supplemental-fixture', 'state': 'running'}
        journal['runs'].append(entry); write(journal_path, journal)
        if self.metadata_outcome == 'interrupt': raise KeyboardInterrupt('Interrupted after durable reservation')
        result = self.result(recipe, rid)
        result.update(resource_output=str(directory / 'outputs' / rid),
                      limits={'max_attempts': kwargs['max_attempts'], 'max_seconds': kwargs['max_seconds']})
        if self.metadata_outcome == 'failed': result.update(ready=False, status='failed')
        ref = write(control / (rid + '.json'), result)
        entry.update(self.entry(rid, ref), input_key='supplemental-fixture', state=result['status'])
        write(journal_path, journal)
        return result

    def prepare_recovery(self): return recovery.prepare(self.lifecycle, self.recovery_request)

    def publish_recovery(self, prepared):
        presented = self.store.present(**prepared['packet']['concept'])
        saved = approve(self.store, self.work, presented['digest'], 'supplemental-decision')
        return {'operation_id': self.recovery_request['operation_id'], 'work_id': self.work,
                'expected_work_revision': self.store.read(self.work)['revision'], 'digest': presented['digest'],
                'decision_id': saved['decision_id'], 'packet_path': prepared['packet_path'],
                'packet_sha256': prepared['packet_sha256']}

    def approved(self):
        prepared = self.prepare_recovery()
        return prepared, self.publish_recovery(prepared)

    def qualified(self):
        prepared, request = self.approved()
        return prepared, request, recovery.qualify(self.lifecycle, request)

    def recovered(self):
        prepared, request, qualified = self.qualified(); receipt = recovery.apply(self.lifecycle, request)
        reference = {'operation_id': request['operation_id'], 'work_id': self.work, 'receipt_digest': digest(receipt)}
        return prepared, request, qualified, receipt, reference

    def assert_history_preserved(self):
        assert all(Path(path).read_bytes() == raw for path, raw in self.original_bytes.items())
        now = self.history()
        for name, rows in self.original_database.items(): assert all(row in now[name] for row in rows)
        assert now[amendment.TABLE] == self.original_database[amendment.TABLE]


@pytest.fixture
def r(tmp_path, monkeypatch): return RecoveryFixture(tmp_path, monkeypatch)


def test_prepare_is_passive_and_binds_exact_exhausted_history(r):
    prepared = r.prepare_recovery()
    assert not r.recovery_calls
    assert len(json.loads(r.journal.read_text())['runs']) == 3
    assert json.loads(r.journal.read_text())['runs'][0]['state'] == 'failed'
    assert json.loads((r.control / 'clock.json').read_text())['spent'] == 6900
    assert prepared['packet']['request'] == r.recovery_request
    assert prepared['packet']['concept']['execution_envelope']['product_budget'] == r.original_receipt['registered_contract']['product_budget']
    assert r.store.read(r.work)['execution'] == 'held'
    r.assert_history_preserved(); assert r.prepare_recovery() == prepared


def test_supported_saved_decision_qualification_apply_verify_and_replay(r):
    prepared, request, qualification, receipt, reference = r.recovered()
    assert len(r.recovery_calls) == 1
    call = r.recovery_calls[0]
    assert call['max_attempts'] == 1 and call['max_seconds'] == 600 and call['native_checks'] == []
    assert Path(call['readiness_dir']) != r.readiness
    assert recovery.verify(r.store, reference) == receipt
    assert recovery.apply(r.lifecycle, request) == receipt
    assert len(r.recovery_calls) == 1
    state = r.store.read(r.work)
    assert state['execution'] == 'held'
    with r.store._connection() as db:
        assert db.execute('SELECT admitted FROM lifecycle WHERE work_id=?', (r.work,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM assignment WHERE work_id=?', (r.work,)).fetchone()[0] == 0
    assert receipt['accounting_origin'] == r.original_receipt['accounting_origin']
    assert receipt['to_decision_id'] == request['decision_id'] and receipt['to_concept_digest'] == request['digest']
    assert receipt['to_recipe_digest'] == r.original_receipt['to_recipe_digest']
    r.assert_history_preserved()


def test_no_metadata_before_exact_new_saved_approval(r):
    prepared = r.prepare_recovery(); old = r.original_receipt['apply_request']
    request = {**old, 'operation_id': r.recovery_request['operation_id'],
               'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls; r.assert_history_preserved()


def test_stale_work_revision_fails_before_metadata(r):
    _, request = r.approved(); request['expected_work_revision'] -= 1
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls


def test_prior_decision_cannot_approve_new_packet(r):
    _, request = r.approved(); request['decision_id'] = r.original_receipt['to_decision_id']
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls


def test_apply_requires_completed_qualification(r):
    _, request = r.approved()
    with pytest.raises(Rejected): recovery.apply(r.lifecycle, request)
    assert not r.recovery_calls and r.store.read(r.work)['execution'] == 'held'
    r.assert_history_preserved()


@pytest.mark.parametrize('outcome', ['failed', 'interrupt'])
def test_failed_or_interrupted_allowance_cannot_be_reused(r, outcome):
    _, request = r.approved(); r.metadata_outcome = outcome
    with pytest.raises((Rejected, KeyboardInterrupt)): recovery.qualify(r.lifecycle, request)
    assert len(r.recovery_calls) == 1
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    with pytest.raises(Rejected): recovery.apply(r.lifecycle, request)
    assert len(r.recovery_calls) == 1; r.assert_history_preserved()


def test_successful_qualification_replay_does_not_consume_another_attempt(r):
    _, request, result = r.qualified()
    assert recovery.qualify(r.lifecycle, request) == result
    assert len(r.recovery_calls) == 1; r.assert_history_preserved()


@pytest.mark.parametrize('lock', ['worker.lock', 'clock.lock'])
def test_active_ownership_fences_qualification(r, lock):
    _, request = r.approved()
    path = r.store.directory / lock if lock == 'worker.lock' else r.control / lock
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls; r.assert_history_preserved()


def test_concurrent_nested_qualification_launches_at_most_once(r, monkeypatch):
    _, request = r.approved(); provider = r.supplemental_metadata
    def concurrent(recipe, **kwargs):
        with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
        return provider(recipe, **kwargs)
    monkeypatch.setattr(recovery.readiness, 'run_readiness', concurrent)
    recovery.qualify(r.lifecycle, request)
    assert len(r.recovery_calls) == 1


@pytest.mark.parametrize('phase', ['prepare', 'qualify', 'apply'])
def test_exact_source_drift_holds_at_each_gate(r, phase):
    if phase == 'prepare': command = lambda: r.prepare_recovery()
    elif phase == 'qualify':
        _, request = r.approved(); command = lambda: recovery.qualify(r.lifecycle, request)
    else:
        _, request, _ = r.qualified(); command = lambda: recovery.apply(r.lifecycle, request)
    r.after_source_path.write_text('# Changed after approval\n')
    with pytest.raises(Rejected): command()
    assert r.store.read(r.work)['execution'] == 'held'; r.assert_history_preserved()


@pytest.mark.parametrize('key,value', [('active_seconds', 100000), ('wall_seconds', 999999),
                                      ('stage_order', []), ('repository', '/unrelated')])
def test_arbitrary_controller_mutation_rejected(r, key, value):
    r.controller[key] = value; write(r.controller_path, r.controller)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


@pytest.mark.parametrize('change', ['refund', 'open', 'deadline', 'used-recovery-name'])
def test_accounting_reset_or_consumed_slot_reuse_is_rejected(r, change):
    if change == 'used-recovery-name': r.recovery_request['allowances']['runtime_slots'] = {'colima-transition': 3600}
    else:
        path = r.control / 'clock.json'; clock = json.loads(path.read_text())
        if change == 'refund': clock['spent'] -= 3600
        elif change == 'open': clock['open'] = {'id': 'unknown', 'reserved': 3600, 'started_at': clock['last']}
        else: clock['deadline'] += 3600
        write(path, clock)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_changed_packet_replay_does_not_reuse_decision(r):
    prepared, request = r.approved(); modified = copy.deepcopy(prepared['packet'])
    modified['request']['allowances']['runtime_slots']['recovery-test-1'] += 1
    path = r.recovery_destination / 'modified-packet.json'; ref = amendment._retain(path, modified)
    request.update(packet_path=str(path), packet_sha256=ref['sha256'])
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls


def test_wrong_prior_engine_is_not_a_historical_approval_bypass(r):
    path = r.prior_engine / 'aos/store.py'; path.write_text(path.read_text() + '\n# different prior source\n')
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_prospective_recovery_charge_preserves_original_origin_and_limits(r):
    _, _, _, receipt, reference = r.recovered()
    original_starts = {str(p): p.read_bytes() for p in r.control.glob('operation-*.json')}
    charge(r, receipt, 'recovery-test-1', seconds=3600)
    accounting = recovery.current_accounting(r.store, receipt, settled=True)
    assert accounting['clock']['spent'] == 10500
    assert accounting['clock']['active_limit'] == 21600
    assert accounting['clock']['identity'] == r.original_receipt['accounting_origin']['clock_identity']
    assert accounting['clock']['deadline'] == r.original_receipt['accounting_origin']['deadline']
    assert all(Path(path).read_bytes() == raw for path, raw in original_starts.items())
    assert recovery.verify(r.store, reference) == receipt


def test_original_consumed_runtime_slot_cannot_be_reused_under_recovery_authority(r):
    _, _, _, receipt, _ = r.recovered()
    # A contradictory replacement of history must fail; the fixture alone mutates it.
    path = r.control / 'operation-colima-transition.json'
    old = json.loads(path.read_text()); old['authority_receipt_digest'] = digest(receipt)
    path.chmod(0o600); write(path, old); path.chmod(0o400)
    with pytest.raises(Rejected): recovery.current_accounting(r.store, receipt, settled=True)


@pytest.mark.parametrize('field,value', [('metadata_attempts', 0), ('metadata_attempts', 2),
    ('metadata_attempts', True), ('metadata_max_seconds', 601), ('metadata_max_seconds', 0),
    ('runtime_slots', {'recovery-test-1': 0}), ('runtime_slots', {'recovery-test-1': True}),
    ('effective_limit_seconds', 1)])
def test_allowances_are_finite_explicit_and_cannot_reduce_historical_charges(r, field, value):
    r.recovery_request['allowances'][field] = value
    with pytest.raises((Rejected, ValueError)): r.prepare_recovery()
    assert not r.recovery_calls


def test_presenting_a_different_concept_invalidates_prepared_recovery(r):
    prepared = r.prepare_recovery()
    replacement = copy.deepcopy(prepared['packet']['concept'])
    replacement['operation_id'] = 'different-present'; replacement['body'] += ' Different proposal.'
    r.store.present(**replacement)
    with pytest.raises(Rejected): r.publish_recovery(prepared)
    assert not r.recovery_calls


@pytest.mark.parametrize('slot', ['stage1-runtime', 'qualification-1'])
def test_original_writer_slot_cannot_be_spoofed_as_coordinator_operation(r, slot):
    _, _, _, receipt, _ = r.recovered()
    charge(r, receipt, slot)
    with pytest.raises(Rejected): recovery.current_accounting(r.store, receipt, settled=True)


def test_changed_qualification_reservation_is_not_approval(r):
    _, request, _ = r.qualified()
    path = r.recovery_destination / 'qualification-reservation.json'
    value = json.loads(path.read_text()); value['decision_id'] = r.original_receipt['to_decision_id']
    path.chmod(0o600); write(path, value); path.chmod(0o400)
    with pytest.raises(Rejected): recovery.apply(r.lifecycle, request)
    assert len(r.recovery_calls) == 1
    r.assert_history_preserved()


def test_new_engine_can_verify_preserved_historical_engine_approval(r, monkeypatch):
    # Only the metadata provider identity is synthetic; old source files and
    # their manifest remain independently checked against the original receipt.
    current = recovery.readiness._runtime_identity()
    assert current['runtime_source_digest'] == r.original_receipt['engine_digest']
    monkeypatch.setattr(recovery.readiness, '_runtime_identity',
                        lambda: {**current, 'runtime_source_digest': 'sha256:' + 'a' * 64})
    _, _, _, receipt, reference = r.recovered()
    assert receipt['engine_digest'] != receipt['original_amendment_receipt']['engine_digest']
    assert recovery.verify(r.store, reference) == receipt
    r.assert_history_preserved()


def test_interrupted_apply_rolls_back_contract_and_replays_same_qualified_attempt(r, monkeypatch):
    _, request, _ = r.qualified()
    original_touch = r.lifecycle._touch
    def interrupted(db, work_id, kind, detail):
        original_touch(db, work_id, kind, detail)
        if kind == 'predispatch_recovered': raise KeyboardInterrupt('After contract update, before commit')
    with monkeypatch.context() as patch:
        patch.setattr(r.lifecycle, '_touch', interrupted)
        with pytest.raises(KeyboardInterrupt): recovery.apply(r.lifecycle, request)
    assert r.store.read(r.work)['revision'] == request['expected_work_revision']
    with r.store._connection() as db:
        registered = json.loads(db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (r.work,)).fetchone()[0])
        assert registered == r.original_receipt['registered_contract']
        assert db.execute('SELECT count(*) FROM ' + recovery.TABLE).fetchone()[0] == 0
    receipt = recovery.apply(r.lifecycle, request)
    assert receipt['to_decision_id'] == request['decision_id'] and len(r.recovery_calls) == 1
    r.assert_history_preserved()


@pytest.mark.parametrize('field', ['approved_di', 'approved_task_plan', 'native_catalog'])
def test_source_replacement_cannot_rewrite_approved_semantic_inputs(r, field):
    old = r.original_controller[field]
    before = old if isinstance(old, dict) else next(ref for ref in r.original_controller['input_files'] if ref['path'] == old)
    path = r.private / ('changed-' + Path(before['path']).name)
    path.write_text('Changed semantic authority; not a helper repair.\n')
    after = amendment._file(path)[1]
    r.recovery_request['source_changes'].append({'before': before, 'after': after})
    r.controller[field] = after if isinstance(old, dict) else after['path']
    if field == 'native_catalog': r.controller['native_catalog_sha256'] = after['sha256']
    r.controller['input_files'] = [after if ref == before else ref for ref in r.controller['input_files']]
    write(r.controller_path, r.controller)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_applied_recovery_authority_remains_verifiable_as_product_source_evolves(r):
    _, _, _, receipt, reference = r.recovered()
    (r.source / 'fixture.py').write_text('VALUE = 2  # later product implementation\n')
    assert recovery.verify(r.store, reference) == receipt
    r.assert_history_preserved()


@pytest.mark.parametrize('phase', ['prepare', 'qualify', 'apply'])
def test_product_source_drift_before_apply_still_holds(r, phase):
    if phase == 'prepare': command = lambda: r.prepare_recovery()
    elif phase == 'qualify':
        _, request = r.approved(); command = lambda: recovery.qualify(r.lifecycle, request)
    else:
        _, request, _ = r.qualified(); command = lambda: recovery.apply(r.lifecycle, request)
    (r.source / 'fixture.py').write_text('VALUE = 2  # unauthorised predispatch drift\n')
    with pytest.raises(Rejected): command()
    assert r.store.read(r.work)['execution'] == 'held'
    r.assert_history_preserved()


def test_genuinely_concurrent_prepare_retains_exactly_one_recovery_intent(r, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Barrier

    second = copy.deepcopy(r.recovery_request)
    second['operation_id'] = 'competing-recovery'
    second_destination = r.private / 'competing'; second_destination.mkdir(mode=0o700)
    second['destination_directory'] = str(second_destination)
    controls = copy.deepcopy(r.controller)
    controls['supplemental_recovery']['operation_id'] = second['operation_id']
    second_controller = r.private / 'competing-controller.json'; write(second_controller, controls)
    second['controller_contract_path'] = str(second_controller)
    original_fence = recovery.fencing._worker_fence
    simultaneous = Barrier(2)

    @contextmanager
    def concurrent_fence(store):
        simultaneous.wait(timeout=10)
        with original_fence(store) as fence:
            yield fence

    def attempt(request):
        try:
            return recovery.prepare(r.lifecycle, request)
        except Rejected as error:
            return error

    monkeypatch.setattr(recovery.fencing, '_worker_fence', concurrent_fence)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (r.recovery_request, second)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, Rejected) for outcome in outcomes) == 1
    assert not r.recovery_calls
    with r.store._connection() as db:
        assert db.execute('SELECT count(*) FROM ' + recovery.INTENT).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM ' + recovery.TABLE).fetchone()[0] == 0
    r.assert_history_preserved()


def configuration_refresh(r, *, effort=False):
    before = r.config.read_bytes()
    replacements = [{'key': 'model', 'before': 'new-model', 'after': 'recovered-model'}]
    if effort:
        replacements.append({'key': 'model_reasoning_effort', 'before': 'medium', 'after': 'high'})
    text = before.decode()
    for replacement in replacements:
        prior = replacement['key'] + ' = "' + replacement['before'] + '"'
        changed = replacement['key'] + ' = "' + replacement['after'] + '"'
        assert text.count(prior) == 1
        text = text.replace(prior, changed)
    r.config.write_text(text)
    recipe = copy.deepcopy(r.original_receipt['registered_contract']['runtime_recipe'])
    prior = recipe['instruction_manifest']
    recipe['instruction_manifest'] = recovery.instructions.discover(
        r.source, support_files=[ref['path'] for ref in prior.get('support', [])],
        project_doc_max_bytes=prior.get('runtime_overrides', {}).get('project_doc_max_bytes'))
    path = r.private / 'recovery-recipe.json'; write(path, recipe)
    r.recovery_request['recipe_path'] = str(path)
    r.recovery_request['configuration_refresh'] = [{
        'path': str(r.config), 'before_sha256': hashlib.sha256(before).hexdigest(),
        'after_sha256': hashlib.sha256(r.config.read_bytes()).hexdigest(), 'replacements': replacements}]
    return recipe, path


def test_exact_explicit_config_pin_refresh_preserves_all_other_recipe_authority(r):
    recipe, _ = configuration_refresh(r)
    _, _, _, receipt, reference = r.recovered()
    assert receipt['to_recipe_digest'] == digest(recipe)
    assert receipt['to_recipe_digest'] != r.original_receipt['to_recipe_digest']
    original = r.original_receipt['registered_contract']['runtime_recipe']
    assert {k: v for k, v in recipe.items() if k != 'instruction_manifest'} == {
        k: v for k, v in original.items() if k != 'instruction_manifest'}
    assert recovery.verify(r.store, reference) == receipt
    r.assert_history_preserved()


def test_configuration_drift_without_explicit_recipe_cannot_be_normalized(r):
    r.config.write_text(r.config.read_text() + '\n# Unproposed configuration drift\n')
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_configuration_drift_after_saved_decision_still_rejects(r):
    configuration_refresh(r); _, request = r.approved()
    r.config.write_text(r.config.read_text() + '\n# Changed after the decision\n')
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    assert not r.recovery_calls


@pytest.mark.parametrize('field,value', [('max_seconds', 28801), ('source', '/different-source'),
    ('outputs', '/different-outputs'), ('read_roots', ['/']), ('native_write_roots', ['/']),
    ('implementation_prompt', 'Different work'), ('checks', [])])
def test_config_refresh_cannot_change_other_runtime_recipe_fields(r, field, value):
    recipe, path = configuration_refresh(r); recipe[field] = value; write(path, recipe)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


@pytest.mark.parametrize('part', ['chain', 'support', 'settings', 'source', 'runtime_overrides', 'config-path'])
def test_config_refresh_cannot_change_instruction_scope_or_aliases(r, part):
    recipe, path = configuration_refresh(r); manifest = recipe['instruction_manifest']
    if part in ('chain', 'support'): manifest[part] = [{'path': '/unapproved', 'sha256': 'f' * 64, 'bytes': 1}]
    elif part == 'settings': manifest[part] = {**manifest[part], 'project_doc_max_bytes': 1}
    elif part == 'source': manifest[part] = '/unapproved'
    elif part == 'runtime_overrides': manifest[part] = {'project_doc_max_bytes': 1}
    else: manifest['configs'][0]['path'] = '/unapproved-config'
    write(path, recipe)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


@pytest.mark.parametrize('field,value', [('id', 'readiness-9'), ('state', 'failed')])
def test_supplemental_journal_must_match_actual_terminal_receipt(r, monkeypatch, field, value):
    _, request = r.approved(); provider = r.supplemental_metadata
    def contradictory(recipe, **kwargs):
        result = provider(recipe, **kwargs)
        path = Path(kwargs['readiness_dir']) / 'control/readiness.json'
        journal = json.loads(path.read_text()); journal['runs'][0][field] = value; write(path, journal)
        return result
    monkeypatch.setattr(recovery.readiness, 'run_readiness', contradictory)
    with pytest.raises(Rejected): recovery.qualify(r.lifecycle, request)
    with pytest.raises(Rejected): recovery.apply(r.lifecycle, request)
    assert len(r.recovery_calls) == 1
    r.assert_history_preserved()


def test_additional_runtime_slot_cannot_claim_reserved_product_qualification_namespace(r):
    r.recovery_request['allowances']['runtime_slots'] = {'qualification-3': 3600}
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_exact_model_and_effort_scalar_refresh_is_accepted(tmp_path, monkeypatch):
    r = RecoveryFixture(tmp_path, monkeypatch, configuration_effort=True)
    recipe, _ = configuration_refresh(r, effort=True)
    _, _, _, receipt, reference = r.recovered()
    assert receipt['to_recipe_digest'] == digest(recipe)
    assert recovery.verify(r.store, reference) == receipt
    r.assert_history_preserved()


def update_proposed_configuration_pin(r, recipe, path):
    recipe['instruction_manifest'] = recovery.instructions.discover(r.source)
    write(path, recipe)
    r.recovery_request['configuration_refresh'][0]['after_sha256'] = hashlib.sha256(r.config.read_bytes()).hexdigest()


@pytest.mark.parametrize('addition', [
    'notify = ["unreviewed-command"]\n', 'browser = "unreviewed-browser"\n',
    'model_provider = "unreviewed-provider"\n', 'unrecognized_setting = true\n',
    '[browser]\nenabled = true\n', '# Newly inserted unrelated comment\n'])
def test_valid_model_proof_cannot_hide_other_configuration_changes(r, addition):
    recipe, path = configuration_refresh(r)
    r.config.write_text(r.config.read_text() + addition)
    update_proposed_configuration_pin(r, recipe, path)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_valid_model_proof_cannot_hide_whitespace_change(r):
    recipe, path = configuration_refresh(r)
    r.config.write_text(r.config.read_text().replace('model = ', 'model  = '))
    update_proposed_configuration_pin(r, recipe, path)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


@pytest.mark.parametrize('field', ['before_sha256', 'after_sha256'])
def test_config_scalar_proof_hash_must_match_original_and_proposed_manifest(r, field):
    configuration_refresh(r)
    r.recovery_request['configuration_refresh'][0][field] = 'f' * 64
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


@pytest.mark.parametrize('field', ['before', 'after'])
def test_config_scalar_proof_values_must_reconstruct_exact_original_bytes(r, field):
    configuration_refresh(r)
    r.recovery_request['configuration_refresh'][0]['replacements'][0][field] = 'wrong-model'
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_table_level_model_cannot_impersonate_top_level_scalar_refresh(r):
    recipe, path = configuration_refresh(r)
    r.config.write_text('[profiles.fixture]\nmodel = "recovered-model"\n')
    update_proposed_configuration_pin(r, recipe, path)
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_changed_config_requires_explicit_scalar_reconstruction_proof(r):
    configuration_refresh(r); r.recovery_request.pop('configuration_refresh')
    with pytest.raises(Rejected): r.prepare_recovery()
    assert not r.recovery_calls


def test_authority_appendix_is_generated_from_exact_finite_allowances(r):
    prepared = r.prepare_recovery()
    concept = prepared['packet']['concept']; envelope = concept['execution_envelope']
    assert concept['authority'] == envelope['authority']
    assert envelope['authority'].startswith(r.original_receipt['registered_contract']['authority'])
    for stated in ('600 seconds', '"recovery-test-1":3600', '73500 seconds', 'no refund, slot reuse or automatic retry'):
        assert stated in envelope['authority']
    assert envelope['predispatch_recovery']['allowances'] == r.recovery_request['allowances']
