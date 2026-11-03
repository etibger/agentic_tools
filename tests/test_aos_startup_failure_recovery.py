"""Exact failed-startup recovery with real atomic journals and simulated observations."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from agentic_tools.aos import startup_failure_recovery as recovery
from agentic_tools.aos import resource_limits
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_lifecycle import setup


NOW = 1800000000.0
TIMES = ['2026-01-01T00:00:0%d+00:00' % value for value in range(5)]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def retain(path, value, *, immutable=False):
    if path.exists():
        path.chmod(0o600)
    raw = value if isinstance(value, bytes) else canonical(value).encode()
    path.write_bytes(raw)
    path.chmod(0o400 if immutable else 0o600)
    return {'path': str(path), 'sha256': sha(raw)}


def sql(path, statement, args=()):
    with closing(sqlite3.connect(path)) as db, db:
        return db.execute(statement, args).fetchall()


def tables(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
                for name in names}


def rewrite_bundle(c):
    reference = retain(c.packet_path, c.evidence)
    c.request.update(evidence_path=reference['path'], evidence_sha256=reference['sha256'])


def seal(c):
    """Repin deliberately changed synthetic evidence, without bypassing classification."""
    raw = canonical(c.record)
    sql(c.runtime, 'UPDATE attempt SET record=? WHERE id=?', (raw, c.aid))
    c.request['runtime_record_sha256'] = sha(raw.encode())
    c.evidence['runtime_record'] = retain(c.evidence_root / 'runtime-original.json', raw.encode(), immutable=True)
    with c.store._connection() as db:
        assignment = dict(db.execute('SELECT * FROM assignment WHERE attempt_id=?', (c.aid,)).fetchone())
        observations = [dict(row) for row in db.execute('SELECT * FROM runtime_observation WHERE attempt_id=?', (c.aid,))]
        worker = [dict(row) for row in db.execute('SELECT * FROM worker_status ORDER BY singleton')]
    guard = json.loads(sql(c.runtime, "SELECT record FROM resource_limit WHERE work_id='Q1'")[0][0])
    c.evidence['failure_readback'] = retain(c.evidence_root / 'failure-readback.json', {
        'attempt': c.record, 'assignment': assignment, 'runtime_observations': observations, 'worker': worker,
        'resource_guard_summary': {key: guard.get(key) for key in (
            'active_attempt', 'breach', 'carry_forward', 'high_water_growth_bytes', 'owner_generation', 'scope_digest')}})
    mapping = retain(c.evidence_root / 'worker-recipes.json', {'Q1': c.recipe})
    c.worker_configuration['runtime_recipes'] = {**mapping, 'recipe_digest': digest(c.recipe)}
    c.worker_completion['worker_configuration'] = retain(c.evidence_root / 'worker-configuration.json', c.worker_configuration)
    c.evidence['worker_completion'] = retain(c.evidence_root / 'worker-completion.json', c.worker_completion, immutable=True)
    c.evidence['snapshot'] = recovery.capture(c.lifecycle, c.request)
    c.report = {'format': recovery.FORMAT + '-review', 'work_id': 'Q1', 'attempt_id': c.aid,
                'snapshot_digest': digest(c.evidence['snapshot']),
                'runtime_record_sha256': c.request['runtime_record_sha256'],
                'producer_manifest_sha256': recovery.PRODUCER_MANIFEST,
                'worker_completion_sha256': c.evidence['worker_completion']['sha256'],
                'verdict': 'pass', 'author': 'fixture-author', 'reviewer': 'fixture-reviewer',
                'reason': 'SIMULATED independent review of this exact disposable fixture'}
    c.evidence['review'] = {'author': c.report['author'], 'reviewer': c.report['reviewer'],
                            'report': retain(c.evidence_root / 'review.json', c.report)}
    rewrite_bundle(c)


@pytest.fixture
def failed_startup(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    store, lifecycle, concept = setup(root)
    setup(root, store=store, work_id='OTHER', admitted=False)
    source, outputs = root / 'fixture', root / 'outputs'
    source.mkdir()
    outputs.mkdir()
    (source / 'source.txt').write_text('unchanged fixture source\n')
    with store._transaction() as db:
        row = dict(db.execute("SELECT * FROM assignment WHERE work_id='Q1'").fetchone())
        aid = row['attempt_id']
        db.execute("UPDATE assignment SET state='blocked_unknown_effects',sent_at=? WHERE attempt_id=?", (TIMES[0], aid))
        db.execute("UPDATE lifecycle SET execution='held',control='none' WHERE work_id='Q1'")
        contract = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()[0])
        recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': contract['max_seconds'],
                  'model_write_scope': 'existing-mutable-files', 'mutable': ['source.txt'], 'immutable': {}}
        contract.update(runtime_recipe=recipe, resources=[str(source), str(outputs), 'native-executor'])
        db.execute("UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(contract),))
        db.execute("DELETE FROM resource WHERE work_id='Q1'")
        for name in contract['resources']:
            db.execute('INSERT INTO resource VALUES(?,?,?,?,?)', (name, 'Q1', row['assignment_id'], 'owned', TIMES[0]))
        db.execute('INSERT INTO resource VALUES(?,?,?,?,?)', ('other-logical-resource', 'OTHER', None, 'owned', TIMES[0]))
        db.execute("INSERT INTO worker_status VALUES(1,1,'fixture-stopped-worker',?,'stopped')", (TIMES[4],))
        lifecycle._block(db, 'Q1', 'unknown_effects', 'Retained startup timeout')
        lifecycle._block(db, 'Q1', 'unrelated_obligation', 'Must remain held')
        assignment = dict(db.execute('SELECT * FROM assignment WHERE attempt_id=?', (aid,)).fetchone())
        observation = {'attempt_id': aid, 'state': 'unknown', 'model': None, 'effort': None,
                       'thread_id': None, 'turn_id': None, 'usage': None,
                       'model_calls_attempted': 0, 'detail': recovery.FAILURE}
        db.execute('INSERT INTO runtime_observation VALUES(?,?,?)', (aid, canonical(observation), TIMES[3]))
    run_outputs = outputs / aid
    run_outputs.mkdir()
    native = {'generated': {}, 'cache': {}}
    record = {'attempt_id': aid, 'assignment': assignment, 'phase': 'intent', 'startup_version': 1,
              'startup_qualification_only': False, 'model_calls_attempted': 0, 'usage_raw': [],
              'native_cancel_requested': True, 'runtime_version': 'codex-cli 0.163.0',
              'recipe_digest': digest(recipe), 'run_outputs': str(run_outputs),
              'budget_id': 'original-product-budget', 'budget_seconds': contract['max_seconds'],
              'created_at': TIMES[0], 'configuration_verified_at': TIMES[1],
              'startup_deadline': '2026-01-01T00:01:00+00:00',
              'server_history': [{'pid': 57121, 'group': 57121, 'started': 'retained-owner-birth'}],
              'startup_owner_exit': {'pid': 57121, 'started': 'retained-owner-birth', 'observed_at': TIMES[2]},
              'startup_observed_processes': {}, 'observed_processes': {},
              'baseline': {'source.txt': sha((source / 'source.txt').read_bytes())},
              'startup_outputs': {}, 'startup_paths': {
                  str(path): recovery.directory_identity(path) for path in (source, run_outputs)},
              'startup_native_paths': native,
              'startup_stop_receipt': {'attempt_id': aid, 'stopped': True, 'owned_processes': [],
                  'effects_reconciled': False, 'startup_outcome': None, 'observed_at': TIMES[4],
                  'partial_mutations': {'status': 'none', 'changes': []},
                  'output_mutations': {'status': 'none', 'changes': []},
                  'native_path_effects': {'observed': True, 'changes': {'generated': [], 'cache': []}, 'after': native}}}
    build_outputs = root / 'build-outputs'
    build_outputs.mkdir()
    scope = {'output_roots': [str(build_outputs)], 'source_evidence_roots': sorted([str(source), str(outputs)]),
             'generated_roots': [], 'temporary_paths': [], 'max_output_growth_bytes': 1024,
             'max_source_evidence_growth_bytes': 2048, 'min_free_bytes': 100,
             'sample_interval_seconds': 2, 'max_observation_gap_seconds': 14,
             'source': str(source), 'scan_seconds': resource_limits.SCAN_SECONDS}
    recipe['resource_limits'] = {key: value for key, value in scope.items() if key not in (
        'source', 'generated_roots', 'temporary_paths', 'scan_seconds', 'max_observation_gap_seconds')}
    baseline = {'identities': {str(source): {'device': 1, 'inode': 10}},
                'files': {'output_roots': {'output/file': 10}, 'source_evidence_roots': {'source/file': 20}},
                'available_bytes': {'volume': 10000}, 'observed_at': NOW - 600}
    guard = {'scope': scope, 'scope_digest': digest(scope), 'baseline': baseline,
             'last_observation': copy.deepcopy(baseline), 'active_attempt': aid, 'owner_generation': 7,
             'high_water_growth_bytes': {'output_roots': 20, 'source_evidence_roots': 5},
             'breach': {'reason': 'observation_failed', 'error_code': 'resource_observation',
                        'error_message': 'Resource observation exceeded its bounded scan', 'observed_at': NOW - 500}}
    observed = {'identities': copy.deepcopy(baseline['identities']),
                'files': {'output_roots': {'output/file': 70}, 'source_evidence_roots': {'source/file': 30}},
                'available_bytes': {'volume': 9000}, 'observed_at': NOW}
    runtime = store.directory / 'codex-runtime.sqlite3'
    with closing(sqlite3.connect(runtime)) as db, db:
        db.executescript('''CREATE TABLE attempt(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE runtime_budget(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE product_budget_scope(scope TEXT PRIMARY KEY,budget_id TEXT NOT NULL);
            CREATE TABLE observed_process(attempt_id TEXT,pid TEXT,started TEXT,PRIMARY KEY(attempt_id,pid,started));
            CREATE TABLE native_check(id TEXT PRIMARY KEY,attempt_id TEXT,record TEXT NOT NULL);
            CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY,record TEXT NOT NULL);''')
        db.execute('INSERT INTO attempt VALUES(?,?)', (aid, canonical(record)))
        db.execute('INSERT INTO attempt VALUES(?,?)', ('unrelated-failed-attempt', canonical({
            'attempt_id': 'unrelated-failed-attempt', 'budget_id': 'other-budget', 'budget_seconds': 11, 'phase': 'failed'})))
        db.execute('INSERT INTO runtime_budget VALUES(?,?)', (record['budget_id'], canonical({
            'kind': 'product', 'source': str(source), 'budget_id': record['budget_id'], 'product_id': 'Q1',
            'max_assignments': 4, 'max_seconds': 240,
            'max_job_seconds': 60, 'held_reservation': 'retain-this-exact-sentinel'})))
        for name in ('source:' + str(source), 'product:Q1'):
            db.execute('INSERT INTO product_budget_scope VALUES(?,?)', (name, record['budget_id']))
        db.execute('INSERT INTO observed_process VALUES(?,?,?)', (aid, '57122', 'retained-child-birth'))
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-unrelated-clean', None, canonical({'phase': 'clean'})))
        db.execute('INSERT INTO resource_limit VALUES(?,?)', ('Q1', canonical(guard)))
        db.execute('INSERT INTO resource_limit VALUES(?,?)', ('OTHER', canonical({'active_attempt': None, 'preserved': True})))
    runtime.chmod(0o600)
    evidence_root = root / 'protected-evidence'
    evidence_root.mkdir(mode=0o700)
    capsule = {'format': 'aos-technical-resource-carry-forward-v1', 'accepted': False,
               'all_phases_stopped': True, 'breach': None, 'target_work_id': 'Q1',
               'qualification_id': 'historical-qualification', 'scope': scope, 'baseline': baseline,
               'charged_growth_bytes': {'output_roots': 1000, 'source_evidence_roots': 2000},
               'original_ceilings': {'max_output_growth_bytes': 2024, 'max_source_evidence_growth_bytes': 4048}}
    capsule_ref = retain(evidence_root / 'original-carry-forward.json', capsule)
    recipe['resource_limits_carry_forward'] = capsule_ref
    recipe['native_protected_paths'] = [capsule_ref['path']]
    record['recipe_digest'] = digest(recipe)
    guard['carry_forward_reference'] = capsule_ref
    guard['carry_forward'] = {key: capsule[key] for key in (
        'qualification_id', 'charged_growth_bytes', 'original_ceilings')}
    guard['carry_forward']['capsule_digest'] = capsule_ref['sha256']
    contract['runtime_recipe'] = recipe
    sql(store.path, "UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(contract),))
    sql(runtime, "UPDATE resource_limit SET record=? WHERE work_id='Q1'", (canonical(guard),))
    producer = {name: retain(evidence_root / name, ('SIMULATED producer ' + name).encode())
                for name in recovery.PRODUCER_FILES}
    producer['manifest'] = retain(evidence_root / 'producer-manifest.json', {'fixture': 'producer R21'})
    monkeypatch.setattr(recovery, 'PRODUCER_MANIFEST', producer['manifest']['sha256'])
    monkeypatch.setattr(recovery, 'PRODUCER_FILES', {name: producer[name]['sha256'] for name in producer if name != 'manifest'})
    sidecar_value = {'work_id': 'Q1', 'scope_digest': guard['scope_digest'], 'breach': guard['breach'],
                     'high_water_growth_bytes': {'output_roots': 40, 'source_evidence_roots': 10}}
    sidecars = []
    for database in (store.path, runtime):
        sidecars.append(retain(resource_limits._failure_path(database, 'Q1'), sidecar_value))
    request = {'operation_id': 'recover-pre-thread-fixture', 'work_id': 'Q1', 'attempt_id': aid,
               'expected_work_revision': store.read('Q1')['revision'], 'digest': concept['digest'],
               'runtime_record_sha256': sha(canonical(record).encode())}
    worker_argv = [str(root / 'fixture-python'), '-I', '-m', 'agentic_tools.aos.cli', 'worker',
                   '--state-dir', str(store.directory), '--runtime-recipes', str(evidence_root / 'worker-recipes.json')]
    worker_configuration = {'shared_state': {'path': str(store.directory)},
                            'qualified_source': {'candidate': {'sha256': producer['manifest']['sha256']}},
                            'runtime_recipes': None, 'cwd': str(root), 'worker_argv': worker_argv}
    command = shlex.join(['./fixture-python', *worker_argv[1:]])
    payloads = [
        {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'fixture-launch',
         'input': 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'},
        {'type': 'custom_tool_call_output', 'call_id': 'fixture-launch', 'output': [
            {'type': 'input_text', 'text': canonical({'session_id': 7123, 'exit_code': None, 'output': 'SIMULATED running worker'})}]},
        {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'fixture-stop',
         'input': 'text(await tools.write_stdin({session_id:7123,chars:' + json.dumps('\x03') + '}));'},
        {'type': 'custom_tool_call_output', 'call_id': 'fixture-stop', 'output': [
            {'type': 'input_text', 'text': canonical({'exit_code': 1,
                'output': 'SIMULATED retained traceback: worker.run(\n time.sleep(interval)\nKeyboardInterrupt'})}]},
    ]
    events = []
    for index, payload in enumerate(payloads):
        event = {'type': 'response_item', 'timestamp': TIMES[index], 'payload': payload}
        events.append({'line': index + 1, 'line_sha256': sha(canonical(event).encode()), 'event': event})
    worker_completion = {'format': 'aos-owned-worker-command-evidence-v1', 'work_id': 'Q1', 'attempt_id': aid,
                         'worker_epoch': 1, 'worker_owner': 'fixture-stopped-worker',
                         'session_id': 7123, 'exit_code': 1, 'events': events, 'worker_configuration': None}
    evidence = {'format': recovery.FORMAT, 'work_id': 'Q1', 'attempt_id': aid, 'producer': producer,
                'worker_completion': None,
                'protected_files': [capsule_ref], 'authority': retain(evidence_root / 'authority.txt',
                    b'SIMULATED explicit delegated host recovery authority'),
                'snapshot': None, 'failure_readback': None, 'runtime_record': None, 'review': None}
    processes = {'observed_pids': [57121, 57122], 'observed_groups': [57121],
                 'present': [], 'method': 'SIMULATED fixture observation; no host processes queried'}
    effects = {'source_inventory_digest': digest(record['baseline']), 'output_inventory_digest': digest({}),
               'native_snapshot_digest': digest(native), 'source_changes': [], 'output_changes': [],
               'native_changes': {'generated': [], 'cache': []}, 'native_attribution': 'SIMULATED'}
    c = SimpleNamespace(store=store, lifecycle=lifecycle, runtime=runtime, root=root, source=source,
                        outputs=outputs, aid=aid, record=record, recipe=recipe, guard=guard,
                        observed=observed, request=request, evidence=evidence, evidence_root=evidence_root,
                        packet_path=evidence_root / 'packet.json', processes=processes, effects=effects,
                        sidecars=sidecars, capsule=capsule,
                        worker_configuration=worker_configuration, worker_completion=worker_completion,
                        real_observe_processes=recovery._observe_processes)
    monkeypatch.setattr(recovery, '_observe_processes', lambda *args: copy.deepcopy(c.processes))
    monkeypatch.setattr(recovery, '_observe_effects', lambda *args: copy.deepcopy(c.effects))
    monkeypatch.setattr(resource_limits, 'observe', lambda scope: copy.deepcopy(c.observed))
    monkeypatch.setattr(recovery.time, 'time', lambda: NOW)
    seal(c)
    return c


def recover(c, *, apply=False):
    return recovery.recover(c.lifecycle, c.request, apply=apply)


def assert_rejected_without_writes(c, *, apply=True):
    before = (tables(c.store.path), tables(c.runtime))
    with pytest.raises((Rejected, ValueError)):
        recover(c, apply=apply)
    assert (tables(c.store.path), tables(c.runtime)) == before


def test_capture_and_preview_do_not_rewrite_any_journal_or_original_false_receipt(failed_startup):
    c = failed_startup
    assert sql(c.store.path, "SELECT execution,control,admitted FROM lifecycle WHERE work_id='Q1'") == [('held', 'none', 1)]
    before = (c.store.path.read_bytes(), c.runtime.read_bytes())
    assert recovery.capture(c.lifecycle, c.request) == c.evidence['snapshot']
    result = recover(c)
    assert result['ready'] is True and result['applied'] is False
    assert result['runtime_and_budget_modified'] is result['resource_breach_acknowledged'] is False
    assert result['new_execution_authorized'] is False
    assert result['terminal_receipt']['provenance'] == 'separately-derived-after-failure'
    assert result['terminal_receipt']['original_cleanup_effects_reconciled'] is False
    assert result['terminal_receipt']['worker_completion']['worker_pid_or_detached_census_claimed'] is False
    assert (c.store.path.read_bytes(), c.runtime.read_bytes()) == before
    assert c.record['startup_stop_receipt']['effects_reconciled'] is False


def test_apply_changes_only_recovery_disposition_and_stopped_guard_preserving_history(failed_startup):
    c = failed_startup
    before_main, before_runtime = tables(c.store.path), tables(c.runtime)
    protected_before = {ref['path']: Path(ref['path']).read_bytes()
                        for ref in [*c.evidence['protected_files'], *c.sidecars]}
    result = recover(c, apply=True)
    after_main, after_runtime = tables(c.store.path), tables(c.runtime)
    assert result['applied'] is True and result['execution'] == 'held'
    assert result['original_runtime_and_budget_modified'] is False
    assert result['new_execution_authorized'] is result['resource_breach_acknowledged'] is False
    for table in before_runtime:
        if table != 'resource_limit':
            assert after_runtime[table] == before_runtime[table]
    for table in ('runtime_observation', 'owned_stop', 'concept', 'decision', 'decision_readback', 'worker_status'):
        assert after_main[table] == before_main[table]
    assert dict(after_runtime['resource_limit'])['OTHER'] == dict(before_runtime['resource_limit'])['OTHER']
    guard = json.loads(dict(after_runtime['resource_limit'])['Q1'])
    assert guard['baseline'] == c.guard['baseline'] and guard['scope'] == c.guard['scope']
    assert guard['scope_digest'] == c.guard['scope_digest'] and guard['breach'] == c.guard['breach']
    assert guard['active_attempt'] is None and guard['owner_generation'] == c.guard['owner_generation'] + 1
    assert guard['high_water_growth_bytes'] == {'output_roots': 60, 'source_evidence_roots': 10}
    assert guard['last_observation'] == c.observed
    with c.store._connection() as db:
        assignment = dict(db.execute('SELECT * FROM assignment WHERE attempt_id=?', (c.aid,)).fetchone())
        run = dict(db.execute("SELECT * FROM lifecycle WHERE work_id='Q1'").fetchone())
        assert assignment['state'] == 'failed' and assignment['terminal_at'] is not None
        assert run['execution'] == 'held' and run['control'] == 'none' and run['admitted'] == 0
        assert db.execute("SELECT COUNT(*) FROM resource WHERE work_id='Q1'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM resource WHERE work_id='OTHER'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM technical_blocker WHERE work_id='Q1' AND code='unrelated_obligation'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM completion").fetchone()[0] == 1
        audit = json.loads(db.execute(f'SELECT record FROM {recovery.TABLE}').fetchone()[0])
    assert audit['original_runtime_sha256'] == c.request['runtime_record_sha256']
    assert audit['original_deadline'] == c.record['startup_deadline']
    assert audit['original_budget_seconds'] == c.record['budget_seconds']
    assert audit['terminal_receipt']['original_row_modified'] is False
    assert {path: Path(path).read_bytes() for path in protected_before} == protected_before
    assert json.loads(dict(after_runtime['attempt'])[c.aid])['startup_stop_receipt']['effects_reconciled'] is False


def test_successful_stable_operation_replays_without_new_observations_or_charges(failed_startup, monkeypatch):
    c = failed_startup
    result = recover(c, apply=True)
    before = (tables(c.store.path), tables(c.runtime))
    monkeypatch.setattr(recovery, '_observe_processes', lambda *args: pytest.fail('Replay must not observe again'))
    assert recover(c, apply=True) == recover(c) == result
    assert (tables(c.store.path), tables(c.runtime)) == before
    c.request['evidence_sha256'] = 'f' * 64
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('field,value', [
    ('unexpected', True), ('expected_work_revision', '1'), ('expected_work_revision', 0),
    ('runtime_record_sha256', 'bad'), ('evidence_sha256', 'bad'), ('digest', 'wrong'),
    ('operation_id', '../unsafe-operation'),
])
def test_request_shape_is_strict_before_any_mutation(failed_startup, field, value):
    c = failed_startup
    c.request[field] = value
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('pin', ['manifest', 'codex_runtime.py', 'worker.py', 'lifecycle.py', 'packet', 'runtime-backup'])
def test_changed_producer_or_evidence_bytes_are_rejected(failed_startup, pin):
    c = failed_startup
    reference = ({'path': str(c.packet_path)} if pin == 'packet' else c.evidence['runtime_record']
                 if pin == 'runtime-backup' else c.evidence['producer'][pin])
    path = Path(reference['path'])
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b'changed')
    if pin == 'runtime-backup':
        path.chmod(0o400)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['same-reviewer', 'failed-verdict', 'snapshot-pin', 'different-producer', 'writable-backup'])
def test_resealed_unqualified_proof_is_not_accepted(failed_startup, change, monkeypatch):
    c = failed_startup
    if change == 'same-reviewer':
        c.evidence['review']['reviewer'] = c.evidence['review']['author']
    elif change == 'different-producer':
        monkeypatch.setattr(recovery, 'PRODUCER_MANIFEST', 'f' * 64)
    elif change == 'writable-backup':
        Path(c.evidence['runtime_record']['path']).chmod(0o600)
    else:
        c.report['verdict' if change == 'failed-verdict' else 'snapshot_digest'] = 'different'
        c.evidence['review']['report'] = retain(Path(c.evidence['review']['report']['path']), c.report)
    rewrite_bundle(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['phase', 'thread', 'turn', 'model', 'qualification-only', 'native-check',
                                  'original-clean', 'original-not-stopped', 'effect-change', 'missing-exit', 'wrong-order'])
def test_resealed_other_failure_class_cannot_be_reinterpreted(failed_startup, change):
    c = failed_startup
    if change == 'phase':
        c.record['phase'] = 'thread_request'
    elif change in ('thread', 'turn'):
        c.record[change + '_id'] = 'possibly-created'
    elif change == 'model':
        c.record['model_calls_attempted'] = 1
    elif change == 'qualification-only':
        c.record['startup_qualification_only'] = True
    elif change == 'native-check':
        sql(c.runtime, 'INSERT INTO native_check VALUES(?,?,?)', ('nc-attempt', c.aid, canonical({'phase': 'clean'})))
    elif change == 'original-clean':
        c.record['startup_stop_receipt']['effects_reconciled'] = True
    elif change == 'original-not-stopped':
        c.record['startup_stop_receipt']['stopped'] = False
    elif change == 'effect-change':
        c.record['startup_stop_receipt']['native_path_effects']['changes']['cache'] = ['changed']
    elif change == 'missing-exit':
        c.record.pop('startup_owner_exit')
    else:
        c.record['startup_owner_exit']['observed_at'] = '2025-01-01T00:00:00+00:00'
    seal(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('field,value', [('concept_revision', 2), ('concept_digest', 'sha256:' + 'f' * 64)])
def test_resealed_contract_must_match_assignment_and_current_concept(failed_startup, field, value):
    c = failed_startup
    contract = json.loads(sql(c.store.path, "SELECT contract FROM lifecycle WHERE work_id='Q1'")[0][0])
    contract[field] = value
    sql(c.store.path, "UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(contract),))
    seal(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('field,value', [
    ('control', 'cancel_requested'), ('control', 'reconciling'),
    ('control', 'blocked_unknown_effects'), ('control', 'cancelled'),
    ('execution', 'paused'), ('execution', 'eligible'), ('admitted', 0),
])
def test_resealed_cancellation_or_nonordinary_hold_is_not_startup_recovery(
        failed_startup, monkeypatch, field, value):
    c = failed_startup
    sql(c.store.path, f"UPDATE lifecycle SET {field}=? WHERE work_id='Q1'", (value,))
    seal(c)
    monkeypatch.setattr(recovery, '_observe_processes', lambda *args: pytest.fail('Reject the class before fresh observations'))
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['missing', 'owner', 'dependency'])
def test_resealed_unknown_effects_blocker_identity_is_required(failed_startup, monkeypatch, change):
    c = failed_startup
    if change == 'missing':
        sql(c.store.path, "DELETE FROM technical_blocker WHERE work_id='Q1' AND code='unknown_effects'")
    else:
        sql(c.store.path, f"UPDATE technical_blocker SET {change}='other' WHERE work_id='Q1' AND code='unknown_effects'")
    seal(c)
    monkeypatch.setattr(recovery, '_observe_processes', lambda *args: pytest.fail('Reject the class before fresh observations'))
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('state', ['requested', 'unknown', 'stopped'])
def test_resealed_owned_stop_requires_separate_reconciliation(failed_startup, monkeypatch, state):
    c = failed_startup
    sql(c.store.path, 'INSERT INTO owned_stop VALUES(?,?,?,?,?,?,?)',
        (c.aid, 'Q1', 'cancel', state, TIMES[4], None, None))
    seal(c)
    monkeypatch.setattr(recovery, '_observe_processes', lambda *args: pytest.fail('Reject the class before fresh observations'))
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('table,statement', [
    ('work', "UPDATE work SET revision=revision+1 WHERE work_id='Q1'"),
    ('worker', "UPDATE worker_status SET state='running'"),
    ('attempt', "UPDATE attempt SET record='{}' WHERE id=(SELECT id FROM attempt WHERE id<>'unrelated-failed-attempt')"),
    ('budget', "UPDATE runtime_budget SET record='{}'"),
    ('resource', "DELETE FROM resource WHERE resource_id='native-executor'"),
    ('observation', "UPDATE runtime_observation SET observation='{}'"),
])
def test_reviewed_sql_snapshot_drift_never_releases_ownership(failed_startup, table, statement):
    c = failed_startup
    sql(c.runtime if table in ('attempt', 'budget') else c.store.path, statement)
    assert_rejected_without_writes(c)


def test_worker_lock_is_exclusive_even_with_stopped_status(failed_startup):
    c = failed_startup
    with (c.store.directory / 'worker.lock').open('r+') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert_rejected_without_writes(c)


@pytest.mark.parametrize('present', ['owner', 'observed-child', 'owner-group'])
def test_actual_process_validator_rejects_live_or_reused_ownership_without_host_calls(failed_startup, monkeypatch, present):
    c = failed_startup
    rows = {}
    if present == 'owner':
        rows[57121] = {'group': 99999, 'started': 'reused-owner-pid'}
    elif present == 'observed-child':
        rows[57122] = {'group': 99999, 'started': 'reused-child-pid'}
    elif present == 'owner-group':
        rows[57199] = {'group': 57121, 'started': 'new-group-member'}
    monkeypatch.setattr(recovery, '_observe_processes', c.real_observe_processes)
    monkeypatch.setattr(recovery, 'process_groups', lambda **kwargs: rows)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['session', 'call-id', 'configuration', 'other-mapping', 'epoch', 'running-completion', 'mixed-running-completed'])
def test_resealed_worker_completion_must_match_exact_owned_command(failed_startup, change):
    c = failed_startup
    events = c.worker_completion['events']
    if change == 'session':
        events[2]['event']['payload']['input'] = events[2]['event']['payload']['input'].replace('7123', '9999')
    elif change == 'call-id':
        events[3]['event']['payload']['call_id'] = 'different-stop-call'
    elif change == 'configuration':
        c.worker_configuration['shared_state']['path'] = str(c.root / 'foreign-state')
    elif change == 'other-mapping':
        argv = c.worker_configuration['worker_argv']
        argv[argv.index('--runtime-recipes') + 1] = str(c.evidence_root / 'foreign-recipes.json')
        command = shlex.join(['./fixture-python', *argv[1:]])
        events[0]['event']['payload']['input'] = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
    elif change == 'epoch':
        c.worker_completion['worker_epoch'] += 1
    elif change == 'mixed-running-completed':
        events[3]['event']['payload']['output'].append(
            {'type': 'input_text', 'text': canonical({'session_id': 7123, 'output': 'SIMULATED still running'})})
    else:
        result = events[3]['event']['payload']['output'][0]
        result['text'] = canonical({**json.loads(result['text']), 'session_id': 7123})
    for item in events:
        item['line_sha256'] = sha(canonical(item['event']).encode())
    seal(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('observation', ['processes', 'effects', 'scan'])
def test_unavailable_fresh_observation_keeps_both_journals_unchanged(failed_startup, monkeypatch, observation):
    c = failed_startup
    def unavailable(*args, **kwargs):
        raise Rejected('fixture_observation', 'SIMULATED unavailable observation')
    if observation == 'scan':
        monkeypatch.setattr(resource_limits, 'observe', unavailable)
    else:
        monkeypatch.setattr(recovery, '_observe_' + observation, unavailable)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['identities', 'output-cap', 'evidence-cap', 'free-space', 'stale', 'future'])
def test_fresh_guard_reduction_keeps_original_caps_and_observation_bounds(failed_startup, change):
    c = failed_startup
    if change == 'identities':
        c.observed['identities'] = {}
    elif change in ('output-cap', 'evidence-cap'):
        group = 'output_roots' if change == 'output-cap' else 'source_evidence_roots'
        c.observed['files'][group]['large-new-file'] = 100000
    elif change == 'free-space':
        c.observed['available_bytes']['volume'] = 1
    else:
        c.observed['observed_at'] = NOW + (1 if change == 'future' else -15)
    assert_rejected_without_writes(c)


def test_original_and_sidecar_high_water_floors_never_shrink(failed_startup):
    c = failed_startup
    c.observed['files'] = copy.deepcopy(c.guard['baseline']['files'])
    result = recover(c, apply=True)
    assert result['terminal_receipt']['resource_limits']['high_water_growth_bytes'] == {
        'output_roots': 40, 'source_evidence_roots': 10}
    assert result['terminal_receipt']['resource_limits']['breach'] == c.guard['breach']


@pytest.mark.parametrize('change', ['work', 'scope', 'breach', 'negative-floor', 'boolean-floor', 'missing-floor', 'over-cap'])
def test_resealed_contradictory_or_excessive_sidecar_never_clears_the_failure(failed_startup, change):
    c = failed_startup
    path = Path(c.sidecars[0]['path'])
    value = json.loads(path.read_bytes())
    if change == 'work':
        value['work_id'] = 'OTHER'
    elif change == 'scope':
        value['scope_digest'] = 'sha256:' + 'e' * 64
    elif change == 'breach':
        value['breach'] = {'reason': 'different-failure'}
    elif change == 'missing-floor':
        value['high_water_growth_bytes'].pop('source_evidence_roots')
    else:
        value['high_water_growth_bytes']['output_roots'] = {
            'negative-floor': -1, 'boolean-floor': True, 'over-cap': 1025}[change]
    retain(path, value)
    seal(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('change', ['accepted', 'active-phase', 'breach', 'charge', 'baseline', 'target'])
def test_real_carry_forward_validation_rejects_resealed_invalid_capsule(failed_startup, change):
    c = failed_startup
    capsule = copy.deepcopy(c.capsule)
    if change == 'accepted':
        capsule['accepted'] = True
    elif change == 'active-phase':
        capsule['all_phases_stopped'] = False
    elif change == 'breach':
        capsule['breach'] = {'reason': 'unacknowledged'}
    elif change == 'charge':
        capsule['charged_growth_bytes']['output_roots'] += 1
    elif change == 'baseline':
        capsule['baseline']['files']['output_roots']['output/file'] += 1
    else:
        capsule['target_work_id'] = 'OTHER'
    reference = retain(Path(c.recipe['resource_limits_carry_forward']['path']), capsule)
    c.recipe['resource_limits_carry_forward'] = reference
    c.record['recipe_digest'] = digest(c.recipe)
    c.guard['carry_forward_reference'] = reference
    c.guard['carry_forward'] = {key: capsule[key] for key in (
        'qualification_id', 'charged_growth_bytes', 'original_ceilings')}
    c.guard['carry_forward']['capsule_digest'] = reference['sha256']
    c.evidence['protected_files'] = [reference]
    contract = json.loads(sql(c.store.path, "SELECT contract FROM lifecycle WHERE work_id='Q1'")[0][0])
    contract['runtime_recipe'] = c.recipe
    sql(c.store.path, "UPDATE lifecycle SET contract=? WHERE work_id='Q1'", (canonical(contract),))
    sql(c.runtime, "UPDATE resource_limit SET record=? WHERE work_id='Q1'", (canonical(c.guard),))
    seal(c)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('stage', ['startup_recovery_before_commit', 'startup_recovery_after_updates'])
def test_injected_exception_rolls_back_main_and_attached_runtime_together(failed_startup, stage):
    c = failed_startup
    before = (tables(c.store.path), tables(c.runtime))
    def fail(where):
        if where == stage:
            raise RuntimeError('SIMULATED crash before atomic commit')
    c.lifecycle._fault = fail
    with pytest.raises(RuntimeError, match='SIMULATED crash'):
        recover(c, apply=True)
    assert (tables(c.store.path), tables(c.runtime)) == before
    assert recovery.TABLE not in tables(c.store.path)


@pytest.mark.parametrize('target', ['guard', 'attempt', 'observation', 'worker'])
def test_commit_compare_and_set_rejects_new_authoritative_change(failed_startup, target):
    c = failed_startup
    changed = []
    def interfere(stage):
        if stage != 'startup_recovery_before_commit':
            return
        if target == 'guard':
            altered = {**c.guard, 'owner_generation': c.guard['owner_generation'] + 1}
            sql(c.runtime, "UPDATE resource_limit SET record=? WHERE work_id='Q1'", (canonical(altered),))
        elif target == 'attempt':
            sql(c.runtime, 'UPDATE attempt SET record=? WHERE id=?', (canonical({**c.record, 'new-field': True}), c.aid))
        elif target == 'worker':
            sql(c.store.path, "UPDATE worker_status SET epoch=epoch+1")
        else:
            sql(c.store.path, "UPDATE runtime_observation SET observed_at='changed'")
        changed.append((tables(c.store.path), tables(c.runtime)))
    c.lifecycle._fault = interfere
    with pytest.raises(Rejected):
        recover(c, apply=True)
    assert len(changed) == 1
    assert (tables(c.store.path), tables(c.runtime)) == changed[0]


@pytest.mark.parametrize('target', ['evidence', 'sidecar', 'lock', 'freshness'])
def test_post_update_final_fences_roll_back_both_journals(failed_startup, monkeypatch, target):
    c = failed_startup
    before = (tables(c.store.path), tables(c.runtime))
    def interfere(stage):
        if stage != 'startup_recovery_after_updates':
            return
        if target == 'freshness':
            monkeypatch.setattr(recovery.time, 'time', lambda: NOW + 15)
        elif target == 'lock':
            path = c.store.directory / 'worker.lock'
            replacement = path.with_name('replacement-worker.lock')
            replacement.write_bytes(b'')
            replacement.chmod(0o600)
            os.replace(replacement, path)
        else:
            path = c.packet_path if target == 'evidence' else Path(c.sidecars[0]['path'])
            path.write_bytes(path.read_bytes() + b' ')
    c.lifecycle._fault = interfere
    with pytest.raises(Rejected):
        recover(c, apply=True)
    assert (tables(c.store.path), tables(c.runtime)) == before


@pytest.mark.parametrize('journal', ['main', 'runtime'])
def test_wal_journals_are_rejected_without_attempting_atomic_recovery(failed_startup, journal):
    c = failed_startup
    path = c.store.path if journal == 'main' else c.runtime
    assert sql(path, 'PRAGMA journal_mode=WAL')[0][0] == 'wal'
    assert_rejected_without_writes(c)


def test_different_database_devices_are_rejected_before_atomic_application(failed_startup, monkeypatch):
    c = failed_startup
    original_identity = recovery._identity
    def another_device(path, **kwargs):
        identity = original_identity(path, **kwargs)
        if Path(path) == c.runtime:
            return {**identity, 'device': identity['device'] + 1}
        return identity
    monkeypatch.setattr(recovery, '_identity', another_device)
    assert_rejected_without_writes(c)


@pytest.mark.parametrize('damage', ['public', 'hardlink', 'symlink'])
def test_unsafe_runtime_file_identity_never_releases_resources(failed_startup, damage):
    c = failed_startup
    if damage == 'public':
        c.runtime.chmod(0o644)
    elif damage == 'hardlink':
        os.link(c.runtime, c.root / 'runtime-hardlink')
    else:
        moved = c.runtime.with_name('original-runtime.sqlite3')
        c.runtime.rename(moved)
        c.runtime.symlink_to(moved)
    assert_rejected_without_writes(c)


def test_replaced_runtime_between_preview_and_commit_is_rejected(failed_startup):
    c = failed_startup
    before = (tables(c.store.path), tables(c.runtime))
    original_inode = c.runtime.stat().st_ino
    def replace(stage):
        if stage == 'startup_recovery_before_commit':
            replacement = c.runtime.with_name('replacement-runtime.sqlite3')
            shutil.copyfile(c.runtime, replacement)
            replacement.chmod(0o600)
            os.replace(replacement, c.runtime)
    c.lifecycle._fault = replace
    with pytest.raises(Rejected):
        recover(c, apply=True)
    assert c.runtime.stat().st_ino != original_inode
    assert (tables(c.store.path), tables(c.runtime)) == before
