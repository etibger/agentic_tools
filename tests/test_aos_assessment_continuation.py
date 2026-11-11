"""Continuation mechanics with simulated readiness and recovery evidence.

The separate recovery suite validates historical proof. These fixtures exercise
real durable concept/contract/assignment transitions without any model or check.
"""
import copy
import hashlib
import json
import sqlite3

import pytest

from agentic_tools.aos import assessment_continuation as continuation
from agentic_tools.aos import budgets, project, supervisor
from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store, canonical, digest
from test_aos_lifecycle import approve
from test_aos_project import setup as project_setup


@pytest.fixture
def recovered(project_setup, monkeypatch, request):
    p = project_setup
    p.config['recipe']['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    p.config['recipe']['native_process_ownership'] = 'host-owned-v1'
    p.config['readiness']['checks'] = []
    p.path_entries = None
    if getattr(request, 'param', None) == 'missing-path':
        toolroot = p.source.parent / 'tools'
        toolroot.mkdir()
        p.path_entries = [toolroot / name for name in ('first', 'removed', 'last')]
        for path in p.path_entries:
            path.mkdir()
        p.config['recipe']['read_roots'].append(str(toolroot))
        p.config['recipe']['checkout_policy']['native_environment'] = {'PATH': ':'.join(map(str, p.path_entries))}
    bounds = p.request['concept']['execution_envelope']
    bounds.update(max_assignments=6, max_repairs=1, max_startup_retries=1, execution_policy='isolated-checkout-v1')
    bounds['product_budget'].update(max_assignments=6, max_seconds=360)
    p.save()
    original = p.run()
    assert original['ready'], original['blocker']
    if p.path_entries:
        p.path_entries[1].rmdir()
    p.original_path = p.state / 'prepared-FIXTURE.json'
    supervisor._retain(p.original_path, original)
    p.store = Store(p.state, (p.source, p.outputs))
    p.lifecycle = Lifecycle(p.store)
    p.store.introduce(**original['introduction'])
    presented = p.store.present(**original['concept'])
    contract = {**original['concept']['execution_envelope'], 'concept_revision': 1,
                'concept_digest': presented['digest']}
    p.lifecycle.register('fixture-register', 'FIXTURE', contract)
    approve(p.store, presented, 'FIXTURE')
    (p.source / 'app.py').write_text('answer = 2\n')
    frozen = candidate(p.source, recipe=contract['runtime_recipe'])
    with p.store._transaction() as db:
        writer = p.lifecycle._enqueue(db, 'FIXTURE', 'implementation', 'fixture-original')
        db.execute("UPDATE assignment SET state='completed',result=? WHERE assignment_id=?", (canonical({'candidate': frozen}), writer))
        db.execute("UPDATE lifecycle SET candidate=?,execution='held' WHERE work_id='FIXTURE'", (canonical(frozen),))
        assessor = p.lifecycle._enqueue(db, 'FIXTURE', 'assessment', 'fixture-original-assessment')
        db.execute("UPDATE assignment SET state='failed' WHERE assignment_id=?", (assessor,))
        p.lifecycle._block(db, 'FIXTURE', 'assessment_recovered', 'SIMULATED historical recovery')
        p.lifecycle._touch(db, 'FIXTURE', 'fixture_recovery', {})
        audit = {'format': 'SIMULATED-recovery', 'operation_id': 'fixture-recovery', 'work_id': 'FIXTURE',
                 'attempt_id': assessor, 'candidate': frozen, 'original_contract': contract,
                 'original_prepared_packet': {'path': str(p.original_path),
                     'sha256': hashlib.sha256(p.original_path.read_bytes()).hexdigest()}}
        db.execute('CREATE TABLE assessment_recovery(operation_id TEXT PRIMARY KEY,attempt_id TEXT UNIQUE,record TEXT)')
        db.execute('INSERT INTO assessment_recovery VALUES(?,?,?)', ('fixture-recovery', assessor, canonical(audit)))
    def read_audit(db, operation, work):
        row = db.execute('SELECT record FROM assessment_recovery WHERE operation_id=?', (operation,)).fetchone()
        if not row or json.loads(row[0])['work_id'] != work:
            raise Rejected('fixture_recovery', 'SIMULATED missing recovery')
        return json.loads(row[0])
    monkeypatch.setattr(continuation.recovery, 'read_audit', read_audit)
    monkeypatch.setattr(continuation.instructions, 'discover', lambda *args, **kw:
                        {'version': 1, 'configs': [], 'chain': [], 'support': [], 'changed': True})
    monkeypatch.setattr(continuation, '_runtime_identity', lambda: {'runtime_source_digest': project.source_digest()})
    allowance = budgets.approved_budget(contract, p.state)
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        budgets.initialize(db)
        db.execute('CREATE TABLE attempt(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO runtime_budget VALUES(?,?)', (allowance['budget_id'], canonical(budgets._budget_identity(allowance, str(p.source)))))
        for scope in ('product:' + allowance['product_id'], 'source:' + str(p.source)):
            db.execute('INSERT INTO product_budget_scope VALUES(?,?)', (scope, allowance['budget_id']))
        db.execute('INSERT INTO attempt VALUES(?,?)', (writer, canonical({'budget_id': allowance['budget_id'], 'budget_seconds': 60})))
    p.audit, p.original, p.contract, p.frozen = audit, original, contract, frozen
    p.prepare = lambda: continuation.prepare(p.lifecycle, operation_id='fixture-continuation',
        recovery_operation_id='fixture-recovery', readiness_runner=p.runner)
    p.approve_continuation = lambda target: approve(p.store, {'digest': target}, 'FIXTURE', operation='fixture-continuation-answer')
    return p


def test_prepare_keeps_original_baseline_and_uses_frozen_candidate(recovered):
    p = recovered
    packet = p.prepare()
    assert packet['concept']['execution_envelope']['baseline'] == p.contract['baseline']
    assert packet['readiness']['source_after'] == p.frozen
    assert p.frozen['identity'] != p.contract['baseline']
    assert packet['finish_capacity']['required_assignments'] == 4
    assert packet['finish_capacity']['seconds_reserved'] == 60
    assert packet['finish_capacity']['lifecycle_remaining_assignments'] == 4
    assert len(p.store.read('FIXTURE')['decisions']) == 1
    assert len(p.store.read('FIXTURE')['lifecycle']['assignments']) == 2
    assert continuation._file(p.original_path)[0] == p.original


def test_exact_new_decision_required_then_one_successor_and_no_reset(recovered):
    p = recovered
    packet = p.prepare()
    target = continuation.publish(p.lifecycle, packet)
    assert target != p.contract['concept_digest']
    with pytest.raises(Rejected, match='approval'):
        continuation.admit(p.lifecycle, packet)
    p.approve_continuation(target)
    admitted = continuation.admit(p.lifecycle, packet)
    replay = continuation.admit(p.lifecycle, packet)
    assert replay['already_admitted'] and admitted['assignment_id'] == replay['assignment_id']
    assert continuation.publish(p.lifecycle, packet) == target
    state = p.store.read('FIXTURE')
    assert len(state['decisions']) == 2
    assert len(state['lifecycle']['assignments']) == 3
    assert p.lifecycle.startup_retries_used('FIXTURE') == 1
    with p.store._connection() as db:
        assignment = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (admitted['assignment_id'],)).fetchone()
        _, current = p.lifecycle._run(db, 'FIXTURE')
        assert continuation.assessment_context(db, assignment, current) == {'purpose': 'assessment'}
        assert current['product_budget'] == p.contract['product_budget']
        assert current['baseline'] == p.contract['baseline']
        assert json.loads(assignment['candidate']) == p.frozen
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 1


@pytest.mark.parametrize('field,value', [('max_seconds', 120), ('read_roots', ['/']), ('checks', [])])
def test_packet_rejects_changed_execution_scope(recovered, field, value):
    p = recovered
    packet = p.prepare()
    packet['concept']['execution_envelope']['runtime_recipe'][field] = value
    with pytest.raises(Rejected, match='scope|permissions|commands'):
        continuation.validate(packet, p.original['input_bindings'], p.config, p.request)


def test_changed_candidate_is_rejected_before_admission(recovered):
    p = recovered
    packet = p.prepare()
    target = continuation.publish(p.lifecycle, packet)
    p.approve_continuation(target)
    (p.source / 'app.py').write_text('answer = 3\n')
    with pytest.raises(Rejected, match='candidate changed'):
        continuation.admit(p.lifecycle, packet)
    assert len(p.store.read('FIXTURE')['lifecycle']['assignments']) == 2


def test_retained_preparation_reuses_readiness(recovered):
    p = recovered
    packet = p.prepare()
    retained = p.state / 'prepared-assessment-FIXTURE-fixture-continuation.json'
    supervisor._retain(retained, packet)
    before = len(p.calls)
    assert p.prepare() == packet
    assert len(p.calls) == before


def test_unrelated_hold_and_exhausted_budget_are_not_cleared(recovered):
    p = recovered
    with p.store._transaction() as db:
        p.lifecycle._block(db, 'FIXTURE', 'unknown_effects', 'SIMULATED unresolved effect')
    with pytest.raises(Rejected, match='unrelated hold'):
        p.prepare()
    with p.store._transaction() as db:
        db.execute("DELETE FROM technical_blocker WHERE code='unknown_effects'")
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        budget = p.contract['product_budget']['budget_id']
        db.execute('INSERT INTO attempt VALUES(?,?)', ('historical-expense', canonical({'budget_id': budget, 'budget_seconds': 180})))
    with pytest.raises(Rejected, match='allowance cannot finish'):
        p.prepare()


def test_continuation_rejects_an_unsupported_additional_startup_retry(recovered):
    p = recovered
    with p.store._connection() as db:
        contract = {**p.contract, '_work_id': 'FIXTURE', 'max_startup_retries': 2}
        with pytest.raises(Rejected, match='exactly one remaining startup retry'):
            continuation._capacity(p.lifecycle, db, contract)


def test_unavailable_path_removal_preserves_order_and_all_other_scope(tmp_path):
    first, absent, last = (tmp_path / name for name in ('first', 'absent', 'last'))
    first.mkdir()
    last.mkdir()
    original = {'checkout_policy': {'native_environment': {'PATH': f'{first}:{absent}:{last}', 'RUSTC': '/pinned/rustc'},
                                    'change_roots': ['source']}, 'read_roots': ['/pinned']}
    recipe, removed = continuation._prune_unavailable_path_entries(original)
    assert removed == [str(absent)]
    assert recipe['checkout_policy']['native_environment']['PATH'] == f'{first}:{last}'
    assert original['checkout_policy']['native_environment']['PATH'] == f'{first}:{absent}:{last}'
    assert recipe['checkout_policy']['native_environment']['RUSTC'] == '/pinned/rustc'
    assert recipe['read_roots'] == original['read_roots']
    absent.symlink_to(tmp_path / 'missing')
    unchanged, removed = continuation._prune_unavailable_path_entries(original)
    assert removed == [] and unchanged == original


@pytest.mark.parametrize('replacement', ['/usr/bin', '/usr/bin:/bin:/sbin'])
def test_continuation_rejects_removing_existing_or_adding_path_entries(recovered, replacement):
    p = recovered
    packet = p.prepare()
    # Original fixture has no PATH override, so any addition is forbidden.
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    recipe['checkout_policy'].setdefault('native_environment', {})['PATH'] = replacement
    with pytest.raises(Rejected, match='scope|permissions|commands'):
        continuation.validate(packet, p.original['input_bindings'], p.config, p.request)


def test_removed_path_reappearance_or_false_removal_is_rejected(recovered):
    p = recovered
    packet = p.prepare()
    packet['assessment_continuation']['removed_unavailable_path_entries'] = ['/usr/bin']
    with pytest.raises(Rejected, match='unavailable PATH entries changed'):
        continuation.validate(packet, p.original['input_bindings'], p.config, p.request)


@pytest.mark.parametrize('recovered', ['missing-path'], indirect=True)
def test_missing_path_is_bound_in_actual_packet_and_rechecked(recovered):
    p = recovered
    packet = p.prepare()
    first, removed, last = p.path_entries
    assert packet['assessment_continuation']['removed_unavailable_path_entries'] == [str(removed)]
    assert str(removed) in packet['concept']['decision_context']['dependencies']
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    assert recipe['checkout_policy']['native_environment']['PATH'] == f'{first}:{last}'
    assert continuation.validate(packet, p.original['input_bindings'], p.config, p.request) == packet
    for changed in [str(last), f'{first}:{last}:/usr/bin']:
        tampered = copy.deepcopy(packet)
        tampered['concept']['execution_envelope']['runtime_recipe']['checkout_policy']['native_environment']['PATH'] = changed
        with pytest.raises(Rejected, match='scope|permissions|commands'):
            continuation.validate(tampered, p.original['input_bindings'], p.config, p.request)
    removed.mkdir()
    with pytest.raises(Rejected, match='unavailable PATH entries changed'):
        continuation.validate(packet, p.original['input_bindings'], p.config, p.request)


@pytest.fixture
def exhausted_readiness(recovered):
    p = recovered
    directory = p.source.parent / 'readiness'
    control = directory / 'control'
    control.mkdir(parents=True)
    (control / 'readiness.lock').touch(mode=0o600)
    runs = []
    for index in (1, 2):
        path = control / f'readiness-{index}.json'
        receipt = {'id': f'readiness-{index}', 'status': 'passed', 'ready': True, 'simulated': False,
                   'readiness_scope': 'model-metadata-only', 'model_turns': 0, 'product_attempts': 0,
                   'product_reservations': 0, 'cleanup_verified': True, 'roles': [], 'requested_native_checks': [],
                   'native': {'checks': [], 'execution': 'not_requested'}}
        path.write_text(canonical(receipt))
        runs.append({'id': receipt['id'], 'receipt': str(path), 'state': 'passed',
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    journal = {'format': continuation.readiness.FORMAT, 'state_dir': str(p.state), 'source': str(p.source),
               'max_attempts': 2, 'max_seconds': 60, 'runs': runs}
    p.readiness_journal = control / 'readiness.json'
    p.readiness_journal.write_text(canonical(journal))
    p.original_readiness_journal = journal
    p.extend = lambda operation='fixture-continuation': continuation._extend_readiness(p.config, p.audit, operation)
    return p


def test_extra_readiness_attempt_is_explicit_cumulative_and_idempotent(exhausted_readiness):
    p = exhausted_readiness
    before = p.readiness_journal.read_bytes()
    reference = p.extend()
    current = json.loads(p.readiness_journal.read_text())
    assert current['max_attempts'] == 3 and current['max_seconds'] == 60
    assert current['runs'] == p.original_readiness_journal['runs']
    extension = continuation._check_extension(reference, p.config, audit=p.audit)
    assert extension['original_journal_raw'].encode() == before
    assert extension['product_budget_changed'] is False
    assert p.extend() == reference
    with pytest.raises(Rejected, match='exhausted original technical allowance once'):
        p.extend('second-extension')
    with sqlite3.connect(p.state / 'codex-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['incomplete', 'native', 'cleanup', 'changed-history'])
def test_readiness_extension_rejects_unproved_history(exhausted_readiness, change):
    p = exhausted_readiness
    journal = json.loads(p.readiness_journal.read_text())
    path = p.source.parent / 'readiness' / 'control' / 'readiness-2.json'
    receipt = json.loads(path.read_text())
    if change == 'incomplete':
        journal['runs'][1]['state'] = 'running'
    elif change == 'native':
        receipt['requested_native_checks'] = [{'name': 'unapproved'}]
    elif change == 'cleanup':
        receipt['cleanup_verified'] = False
    else:
        journal['runs'][1]['sha256'] = '0' * 64
    path.write_text(canonical(receipt))
    if change in ('native', 'cleanup'):
        journal['runs'][1]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    p.readiness_journal.write_text(canonical(journal))
    with pytest.raises(Rejected):
        p.extend()
    assert json.loads(p.readiness_journal.read_text())['max_attempts'] == 2


def test_prepare_discloses_exact_one_extra_technical_attempt(exhausted_readiness):
    p = exhausted_readiness
    def runner(recipe, **kwargs):
        receipt = p.runner(recipe, **kwargs)
        receipt.update(id='readiness-3', requested_native_checks=[], readiness_scope='model-metadata-only',
                       native={'checks': [], 'execution': 'not_requested'}, roles=[],
                       limits={'max_seconds': kwargs['max_seconds'], 'max_attempts': kwargs['max_attempts']})
        path = p.readiness_journal.parent / 'readiness-3.json'
        path.write_text(canonical(receipt))
        journal = json.loads(p.readiness_journal.read_text())
        journal['runs'].append({'id': 'readiness-3', 'receipt': str(path), 'state': 'passed',
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        p.readiness_journal.write_text(canonical(journal))
        return receipt
    packet = continuation.prepare(p.lifecycle, operation_id='fixture-continuation',
        recovery_operation_id='fixture-recovery', readiness_extra_attempt=True, readiness_runner=runner)
    assert p.calls[-1][1]['max_attempts'] == 3
    assert p.calls[-1][1]['max_seconds'] == 60
    assert 'attempts 2 to 3' in packet['concept']['decision_context']['effort']
    assert packet['assessment_continuation']['readiness_extension']['sha256'] in packet['concept']['decision_context']['evidence']
    assert packet['concept']['execution_envelope']['product_budget'] == p.contract['product_budget']
    assert continuation.validate(packet, p.original['input_bindings'], p.config, p.request) == packet


@pytest.mark.parametrize('field,value', [('format', 'foreign'), ('operation_id', 'foreign'), ('state_dir', '/foreign'),
    ('original_max_attempts', 1), ('product_budget_changed', True), ('original_journal_sha256', '0' * 64)])
def test_invalid_extension_crash_replay_does_not_change_journal(exhausted_readiness, field, value):
    p = exhausted_readiness
    reference = p.extend()
    path = p.readiness_journal.parent / 'assessment-readiness-extension-fixture-continuation.json'
    extension = json.loads(path.read_text())
    p.readiness_journal.write_text(extension['original_journal_raw'])
    before = p.readiness_journal.read_bytes()
    # Deliberately corrupt a synthetic retained record to exercise fail-closed replay.
    path.chmod(0o600)
    extension[field] = value
    path.write_text(canonical(extension))
    path.chmod(0o400)
    with pytest.raises(Rejected):
        p.extend()
    assert p.readiness_journal.read_bytes() == before
