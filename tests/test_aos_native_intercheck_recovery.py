"""Inter-check recovery requires completed work followed by a proved no-launch tail."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_intercheck_recovery as recovery
from agentic_tools.aos import native_journal, native_paths, native_preparation, native_processes, native_renderer
from agentic_tools.aos import resource_limits, technical_control
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical, digest
from agentic_tools.check_adapters import identities, select


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pin(path, value):
    path.write_text(canonical(value))
    return {'path': str(path), 'sha256': _sha(path.read_bytes())}


def _snapshot(c):
    with c.q.runtime._db() as db:
        return {'archive_revision': technical_control.revision(db, c.q.qualification_id),
                'archive_raw': db.execute('SELECT record FROM technical_qualification WHERE id=?',
                                          (c.q.qualification_id,)).fetchone()[0],
                'native_revision': native_journal.revision(db),
                'native_rows': dict(db.execute('SELECT id,record FROM native_check ORDER BY id')),
                'resource_guards': dict(db.execute('SELECT work_id,record FROM resource_limit ORDER BY work_id'))}


def _sync(c, *, refresh=True):
    with c.q.runtime._db() as db:
        db.execute('UPDATE technical_qualification SET record=? WHERE id=?',
                   (canonical(c.record), c.q.qualification_id))
        db.execute('DELETE FROM native_check')
        for row in c.rows:
            db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
        for resource_id, guard in c.guards.items():
            db.execute('INSERT OR REPLACE INTO resource_limit VALUES(?,?)', (resource_id, canonical(guard)))
        c.control = technical_control.save(db, c.q.qualification_id, c.phase)
    c.snapshot = _snapshot(c)
    if refresh:
        c.packet.update(
            qualification_digest=digest(c.record), phase_digest=digest(c.phase),
            control_digest=digest(c.control) if c.control is not None else None,
            archive_revision=c.snapshot['archive_revision'], native_revision=c.snapshot['native_revision'],
            native_rows={key: _sha(raw.encode()) for key, raw in c.snapshot['native_rows'].items()},
            resource_guards={key: _sha(raw.encode()) for key, raw in c.snapshot['resource_guards'].items()},
            resource_guard_digest=digest(c.guard), guard_baseline_digest=digest(c.guard['baseline']),
            guard_scope_digest=c.guard['scope_digest'], source_baseline_digest=digest(c.record['source_baseline']),
            budget_before=copy.deepcopy(c.record['budget_before']),
            prior_charged_growth_bytes=copy.deepcopy(c.record['charged_growth_bytes']))
    _seal(c)


def _seal(c):
    c.packet_path.write_text(canonical(c.packet))
    c.packet_sha = _sha(c.packet_path.read_bytes())


def _validate(c, *, explicit=True):
    return recovery.validate(c.q, c.record, c.phase, c.guard,
                             recovery.evidence(c.packet_path, c.packet_sha), control=c.control,
                             snapshot=c.snapshot if explicit else None)


@pytest.fixture
def intercheck_case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    directory, source, outputs = (root / name for name in ('private', 'source', 'outputs'))
    for path in (directory, source, outputs):
        path.mkdir(mode=0o700)
    statuses = directory / 'native-status'
    statuses.mkdir()
    database = directory / 'runtime.sqlite3'
    budget = root / 'product-budget-proof'
    budget.write_bytes(b'unchanged product reservation bytes')

    @contextmanager
    def db():
        connection = sqlite3.connect(database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    q = SimpleNamespace(qualification_id='intercheck-fixture', directory=directory,
                        runtime=SimpleNamespace(path=database, _db=db), budget_paths=(str(budget),))
    checks = [{'name': name, 'argv': ['/fixture/pytest', name], 'result_kind': 'pytest',
               'adapter_id': 'pytest-v1', 'timeout_seconds': 30} for name in ('prefix', 'tail', 'later')]
    limits = {'output_roots': [], 'source_evidence_roots': sorted([str(source), str(outputs)]),
             'max_output_growth_bytes': 4096,
             'max_source_evidence_growth_bytes': 2048, 'min_free_bytes': 10,
             'sample_interval_seconds': 2}
    scope = {**copy.deepcopy(limits), 'generated_roots': [], 'source': str(source),
             'temporary_paths': [], 'scan_seconds': 10, 'max_observation_gap_seconds': 14}
    recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': 90, 'checks': checks,
              '_adapter_bindings': identities(), 'resource_limits': limits}
    source_anchor = {'source.txt': {'sha256': 'a' * 64, 'size': 17}}
    breach = {'reason': 'observation_failed', 'error': 'Rejected', 'error_code': 'resource_observation',
              'error_message': 'Resource observation exceeded its bounded scan', 'observed_at': 150.0}
    guard = {'scope': scope, 'scope_digest': digest(scope),
             'baseline': {'files': {'output_roots': {}, 'source_evidence_roots': {}},
                          'identities': {}, 'available_bytes': {'0': 10000}, 'observed_at': 100.0},
             'high_water_growth_bytes': {'output_roots': 31, 'source_evidence_roots': 17},
             'active_attempt': None, 'breach': breach,
             'last_observation': {'available_bytes': {'0': 10000}, 'observed_at': 152.0}}
    owner = {'pid': 47291, 'uid': os.getuid(), 'birth': [101, 321]}
    phase = {'id': 'interrupted-between-checks', 'state': 'stopped', 'recipe': recipe,
             'recipe_digest': digest(recipe), 'input_digest': digest(recipe), 'max_seconds': 90,
             'resource_id': 'intercheck-resources', 'started_at': 100.0, 'deadline': 190.0,
             'started_monotonic': 100.0, 'deadline_monotonic': 190.0,
             'output': str(outputs / 'technical-interrupted-between-checks'), 'execution_started': True,
             'observer_owner': owner, 'observer_joined': {'owner': copy.deepcopy(owner), 'observed_at': 160.0,
                 'watcher_joined': True, 'cancellers_joined': True}, 'resource_receipt': resource_limits.receipt(guard)}
    rows = []
    for check in checks[:2]:
        output = Path(phase['output']) / check['name']
        output.mkdir(parents=True)
        command = select(check).command(check['argv'], output)
        key = 'nc-' + digest([str(output), command, phase['recipe_digest']])[7:39]
        rows.append({'check_id': key, 'attempt_id': None, 'phase': 'clean', 'command': command,
                     'candidate': phase['recipe_digest'], 'output': str(output), 'source': str(source),
                     'outputs_root': str(outputs), 'ownership_backend': native_processes.BACKEND,
                     'native_path_contract': native_paths.contract(recipe), 'before_source': copy.deepcopy(source_anchor),
                     'before_native_paths': {}, 'before_outputs': {}, 'members': {},
                     'status': str(statuses / (key + '.json')), 'release': str(statuses / (key + '.release')),
                     'cancel_path': str(statuses / (key + '.cancel'))})
    prefix, tail = rows
    transcript, stdout = Path(prefix['output']) / 'output.txt', Path(prefix['output']) / 'stdout.txt'
    transcript.write_bytes(b'completed fixture transcript\n')
    stdout.write_bytes(b'completed fixture output\n')
    output_anchor = {str(transcript.relative_to(outputs)): {'sha256': _sha(transcript.read_bytes()), 'size': transcript.stat().st_size},
                     str(stdout.relative_to(outputs)): {'sha256': _sha(stdout.read_bytes()), 'size': stdout.stat().st_size}}
    census = {'boundary_verified': True, 'reconciled': True, 'remaining': [], 'errors': []}
    prefix_cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                      'process_ownership': census, 'source_after': copy.deepcopy(source_anchor),
                      'outputs_after': copy.deepcopy(output_anchor), 'native_path_effects': {'after': {}},
                      'native_temporary_effects': {'before': {}, 'after': {}}}
    receipt = {'native_check_id': prefix['check_id'], 'name': checks[0]['name'], 'result': 'pass',
               'candidate': phase['recipe_digest'], 'command': copy.deepcopy(prefix['command']),
               'executor': 'technical-qualification-only',
               'output': str(transcript), 'output_sha256': _sha(transcript.read_bytes()),
               'raw_outputs': {str(stdout): _sha(stdout.read_bytes())}, 'cleanup': copy.deepcopy(prefix_cleanup)}
    profile = statuses / (prefix['check_id'] + '.sb')
    profile.write_text('(version 1)\n(deny default)\n')
    prefix.update(cleanup=prefix_cleanup, receipt=receipt, native_process_receipt=census,
                  native_sandbox_boundary={'path': str(profile), 'sha256': _sha(profile.read_bytes()),
                      'renderer_receipt': {'component_sources': dict(recovery.SUPPORTED_SOURCES)}})
    sample = resource_limits.receipt(guard)
    sample.update(breach=None, active_attempt=phase['id'], observed_at=120.0)
    receipt['resource_guard'] = copy.deepcopy(sample)
    status = {'census': census, 'resource_guard': sample, 'resource_failure': None, 'exit_code': 0}
    status_path = Path(prefix['status'])
    status_path.write_text(canonical(status))
    tail_cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                    'native_admission': {'launched': False, 'reason': 'preparation_failed'},
                    'source_after': copy.deepcopy(source_anchor), 'outputs_after': copy.deepcopy(output_anchor),
                    'native_path_effects': {'after': {}},
                    'native_temporary_effects': {'before': {}, 'after': {}, 'preexisting_preserved': True},
                    'output_mutations': {'status': 'none', 'changes': []},
                    'partial_mutations': {'status': 'none', 'changes': []}, 'owned_temporary': None}
    proof = {'format': native_preparation.FORMAT, 'check_id': tail['check_id'], 'stage': 'profile',
             'supervisor_popen': 'not_called', 'probe': {'state': 'not_started'},
             'renderer': {'format': native_renderer.FORMAT, 'state': 'not_started'}, 'profile': None,
             'owned_temporary': None, 'native_observer_timeouts': []}
    tail.update(before_outputs=copy.deepcopy(output_anchor), native_temporary_before={}, cleanup=tail_cleanup,
                preparation_failure={'proof': proof,
                    'error': 'Rejected: resource_limit_exceeded: Retained resource guard violation: observation_failed',
                    'effects': copy.deepcopy(tail_cleanup), 'receipt_digest': digest([proof, tail_cleanup])})
    phase.update(checks=[receipt], execution_errors=[tail['preparation_failure']['error']],
                 receipt={'status': 'blocked', 'stopped': True, 'accepted': False, 'model_calls': 0,
                          'source_unchanged': True, 'product_budget_unchanged': True, 'checks': [receipt],
                          'resource_limits': resource_limits.receipt(guard), 'elapsed_seconds': 60})
    record = {'qualification_id': q.qualification_id, 'active_phase': None, 'export': None, 'phases': [phase],
              'breach': breach, 'unresolved_failures': [], 'source_baseline': source_anchor,
              'budget_before': {str(budget): _sha(budget.read_bytes())}, 'budget_paths': [str(budget)],
              'charged_growth_bytes': {'output_roots': 31, 'source_evidence_roots': 17},
              'quiescent_gap_dispositions': [], 'prelaunch_observation_dispositions': [],
              'rejected_admission_dispositions': [], 'intercheck_observation_dispositions': []}
    with db() as connection:
        connection.execute('CREATE TABLE native_check (id TEXT PRIMARY KEY, attempt_id TEXT, record TEXT NOT NULL)')
        connection.execute('CREATE TABLE resource_limit (work_id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        connection.execute('CREATE TABLE technical_qualification (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        technical_control.initialize(connection)
        assert native_journal.initialize(connection)
        connection.execute('INSERT INTO technical_qualification VALUES(?,?)', (q.qualification_id, canonical(record)))
    supported = dict(recovery.SUPPORTED_SOURCES)
    source_digest = digest(supported)
    packet = {'format': recovery.FORMAT, 'read_only': True, 'qualification_id': q.qualification_id,
              'phase_id': phase['id'], 'prior_dispositions': [],
              'execution_source': {'semantics': recovery.SEMANTICS, 'source_digest': source_digest,
                  'manifest': _pin(directory / 'source-manifest.json', {'files': {
                      'src/agentic_tools/' + key: value for key, value in supported.items()}}),
                  'verification': _pin(directory / 'source-verification.json', {
                      'source_unchanged': True, 'source_digest': source_digest, 'pytest': {'exit_code': 0}})},
              'sidecars': {phase['resource_id']: {'path': str(resource_limits._failure_path(database, phase['resource_id'])),
                                                'exists': False}},
              'artifacts': {str(path): _sha(path.read_bytes()) for path in (status_path, profile, transcript, stdout)}}
    c = SimpleNamespace(q=q, directory=directory, database=database, source=source, outputs=outputs, budget=budget,
                         record=record, phase=phase, guard=guard, guards={phase['resource_id']: guard},
                         rows=rows, prefix=prefix, tail=tail, packet=packet, control=None, status=status,
                         packet_path=directory / 'reviewed-intercheck.json', completed_calls=[], preparation_calls=[])
    monkeypatch.setattr(native_processes, 'identity', lambda pid: None)

    def completed(row):
        c.completed_calls.append(row['check_id'])
        return copy.deepcopy(row)

    def preparation(row):
        c.preparation_calls.append(row['check_id'])
        return copy.deepcopy(row)

    monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(completed))
    monkeypatch.setattr(native_preparation, 'verify', preparation)
    _sync(c)
    return c


@pytest.fixture
def multi_prefix_case(intercheck_case):
    """Use real derived IDs whose archival sort reverses execution order."""
    c = intercheck_case
    recipe = copy.deepcopy(c.phase['recipe'])
    names = ('first-executed', 'second-executed', 'not-launched', 'later-unadmitted')
    recipe['checks'] = [{**copy.deepcopy(recipe['checks'][0]), 'name': name,
                         'argv': ['/fixture/pytest', name]} for name in names]
    for nonce in range(256):
        recipe['checks'][1]['argv'] = ['/fixture/pytest', names[1], str(nonce)]
        recipe_digest = digest(recipe)
        planned = []
        for check in recipe['checks']:
            output = Path(c.phase['output']) / check['name']
            command = select(check).command(check['argv'], output)
            key = 'nc-' + digest([str(output), command, recipe_digest])[7:39]
            planned.append((key, output, command))
        if planned[0][0] > planned[1][0]:
            break
    else:
        pytest.fail('Bounded fixture construction did not obtain reversed check-ID ordering')
    c.phase.update(recipe=recipe, recipe_digest=recipe_digest, input_digest=recipe_digest)
    completed_template, tail_template = copy.deepcopy(c.prefix), copy.deepcopy(c.tail)
    output_inventory = copy.deepcopy(c.tail['before_outputs'])
    executed, artifacts = [], {}
    for index, (key, output, command) in enumerate(planned[:2]):
        output.mkdir()
        transcript, stdout = output / 'output.txt', output / 'stdout.txt'
        transcript.write_text('completed check ' + str(index))
        stdout.write_text('native fixture output ' + str(index))
        before_outputs = copy.deepcopy(output_inventory)
        for path in (transcript, stdout):
            output_inventory[str(path.relative_to(c.outputs))] = {
                'sha256': _sha(path.read_bytes()), 'size': path.stat().st_size}
        row = copy.deepcopy(completed_template)
        row.update(check_id=key, output=str(output), command=command, candidate=recipe_digest,
                   native_path_contract=native_paths.contract(recipe), before_outputs=before_outputs,
                   before_native_paths=copy.deepcopy(executed[-1]['cleanup']['native_path_effects']['after'])
                   if executed else {},
                   status=str(c.directory / 'native-status' / (key + '.json')),
                   release=str(c.directory / 'native-status' / (key + '.release')),
                   cancel_path=str(c.directory / 'native-status' / (key + '.cancel')))
        row['cleanup']['native_path_effects']['after'] = {'cache-state': {'revision': index + 1}}
        row['cleanup']['outputs_after'] = copy.deepcopy(output_inventory)
        profile = c.directory / 'native-status' / (key + '.sb')
        profile.write_text('(version 1)\n(deny default)\n')
        row['native_sandbox_boundary'].update(path=str(profile), sha256=_sha(profile.read_bytes()))
        sample = copy.deepcopy(c.status['resource_guard'])
        sample['observed_at'] = 110.0 + 10 * index
        row['receipt'].update(native_check_id=key, name=names[index], command=command,
                              candidate=recipe_digest, result='pass' if index == 0 else 'fail',
                              output=str(transcript), output_sha256=_sha(transcript.read_bytes()),
                              raw_outputs={str(stdout): _sha(stdout.read_bytes())},
                              cleanup=copy.deepcopy(row['cleanup']), resource_guard=sample)
        status = copy.deepcopy(c.status)
        status.update(resource_guard=sample, exit_code=index)
        Path(row['status']).write_text(canonical(status))
        for path in (Path(row['status']), profile, transcript, stdout):
            artifacts[str(path)] = _sha(path.read_bytes())
        executed.append(row)
    key, output, command = planned[2]
    output.mkdir()
    tail = tail_template
    tail.update(check_id=key, output=str(output), command=command, candidate=recipe_digest,
                native_path_contract=native_paths.contract(recipe),
                before_native_paths=copy.deepcopy(executed[-1]['cleanup']['native_path_effects']['after']),
                before_outputs=copy.deepcopy(output_inventory),
                status=str(c.directory / 'native-status' / (key + '.json')),
                release=str(c.directory / 'native-status' / (key + '.release')),
                cancel_path=str(c.directory / 'native-status' / (key + '.cancel')))
    tail['cleanup']['native_path_effects']['after'] = copy.deepcopy(tail['before_native_paths'])
    tail['cleanup']['outputs_after'] = copy.deepcopy(output_inventory)
    tail['preparation_failure']['proof']['check_id'] = key
    tail['preparation_failure']['effects'] = copy.deepcopy(tail['cleanup'])
    tail['preparation_failure']['receipt_digest'] = digest([tail['preparation_failure']['proof'], tail['cleanup']])
    archived = [copy.deepcopy(row['receipt']) for row in sorted(executed, key=lambda row: row['check_id'])]
    c.phase['checks'] = archived
    c.phase['receipt']['checks'] = copy.deepcopy(archived)
    c.packet['artifacts'] = artifacts
    c.executed, c.prefix, c.tail, c.rows = executed, executed[0], tail, [*executed, tail]
    _sync(c)
    return c


def test_id_sorted_archive_preserves_recipe_order_and_last_executed_anchor(multi_prefix_case):
    c = multi_prefix_case
    execution_ids = [row['check_id'] for row in c.executed]
    archived_ids = [receipt['native_check_id'] for receipt in c.phase['checks']]
    assert archived_ids == sorted(execution_ids) == list(reversed(execution_ids))
    before = copy.deepcopy(c.phase)
    proof = _validate(c)
    assert [row['check_id'] for row in proof['completed_prefix']] == execution_ids
    assert [row['outcome'] for row in proof['completed_prefix']] == ['pass', 'fail']
    assert proof['healthy_anchor']['check_id'] == execution_ids[-1] == archived_ids[0]
    assert proof['healthy_anchor']['resource_sample']['observed_at'] == 120.0
    assert proof['endpoint_digests'] == {
        'source': digest(c.executed[-1]['cleanup']['source_after']),
        'native': digest(c.executed[-1]['cleanup']['native_path_effects']['after'])}
    assert proof['next_preparation']['check_id'] == c.tail['check_id']
    assert c.completed_calls == execution_ids and c.preparation_calls == [c.tail['check_id']]
    assert c.phase == before


@pytest.mark.parametrize('damage', ['recipe-order', 'duplicate', 'missing', 'extra', 'full-receipt-field',
                                   'phase-receipt-order'])
def test_multi_prefix_archive_requires_exact_id_sorted_full_receipts(multi_prefix_case, damage):
    c = multi_prefix_case
    archive = copy.deepcopy(c.phase['checks'])
    if damage in ('recipe-order', 'phase-receipt-order'):
        archive = [copy.deepcopy(row['receipt']) for row in c.executed]
    elif damage == 'duplicate':
        archive[1] = copy.deepcopy(archive[0])
    elif damage == 'missing':
        archive.pop()
    elif damage == 'extra':
        archive.append({**copy.deepcopy(archive[0]), 'native_check_id': 'nc-unplanned-receipt'})
    else:
        archive[0]['output_sha256'] = '0' * 64
    if damage != 'phase-receipt-order':
        c.phase['checks'] = copy.deepcopy(archive)
    c.phase['receipt']['checks'] = archive
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)
    assert c.completed_calls == [row['check_id'] for row in c.executed]
    assert c.preparation_calls == []


def test_id_last_archived_check_cannot_replace_the_last_executed_endpoint(multi_prefix_case):
    c = multi_prefix_case
    first_executed = c.executed[0]
    assert first_executed['check_id'] == c.phase['checks'][-1]['native_check_id']
    wrong_endpoint = copy.deepcopy(first_executed['cleanup']['native_path_effects']['after'])
    assert wrong_endpoint != c.executed[-1]['cleanup']['native_path_effects']['after']
    c.tail['before_native_paths'] = wrong_endpoint
    c.tail['cleanup']['native_path_effects']['after'] = copy.deepcopy(wrong_endpoint)
    c.tail['preparation_failure']['effects'] = copy.deepcopy(c.tail['cleanup'])
    c.tail['preparation_failure']['receipt_digest'] = digest([
        c.tail['preparation_failure']['proof'], c.tail['cleanup']])
    _sync(c)
    with pytest.raises(Rejected, match='endpoints'):
        _validate(c)
    assert c.completed_calls == [row['check_id'] for row in c.executed]
    assert c.preparation_calls == [c.tail['check_id']]


@pytest.mark.parametrize('explicit', [False, True])
def test_exact_completed_prefix_and_profile_only_tail_preserve_all_evidence(intercheck_case, explicit):
    c = intercheck_case
    before = copy.deepcopy((c.record, c.phase, c.guard, c.packet, c.snapshot))
    database, budget = c.database.read_bytes(), c.budget.read_bytes()
    result = _validate(c, explicit=explicit)
    assert result['kind'] == 'intercheck-observation-failure'
    assert result['completed_prefix'][0]['outcome'] == 'pass'
    assert result['next_preparation']['check_id'] == c.tail['check_id']
    assert result['minimum_high_water_growth_bytes'] == {'output_roots': 31, 'source_evidence_roots': 17}
    assert c.completed_calls == [c.prefix['check_id']]
    assert c.preparation_calls == [c.tail['check_id']]
    assert (c.record, c.phase, c.guard, c.packet, c.snapshot) == before
    assert c.database.read_bytes() == database and c.budget.read_bytes() == budget


@pytest.mark.parametrize('field', ['qualification_digest', 'phase_digest', 'control_digest', 'archive_revision',
    'native_revision', 'native_rows', 'resource_guards', 'resource_guard_digest', 'guard_baseline_digest',
    'guard_scope_digest', 'source_baseline_digest', 'budget_before', 'prior_charged_growth_bytes', 'prior_dispositions'])
def test_each_reviewed_snapshot_binding_is_required(intercheck_case, field):
    c = intercheck_case
    c.packet[field] = 'stale'
    _seal(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing-prefix', 'missing-tail', 'later-row', 'swapped-order', 'prefix-as-tail',
                                   'tail-receipt', 'phase-result', 'held-history'])
def test_only_an_ordered_complete_prefix_and_exact_next_tail_are_supported(intercheck_case, damage):
    c = intercheck_case
    if damage == 'missing-prefix':
        c.rows.remove(c.prefix)
    elif damage == 'missing-tail':
        c.rows.remove(c.tail)
    elif damage in ('later-row', 'held-history'):
        row = copy.deepcopy(c.prefix)
        row['check_id'] = 'nc-unexpected-later'
        row['output'] = str(Path(c.phase['output']) / 'later') if damage == 'later-row' else str(c.outputs / 'other-phase')
        if damage == 'held-history':
            row['phase'] = 'cleanup_unknown'
        c.rows.append(row)
    elif damage == 'swapped-order':
        c.phase['recipe']['checks'][0:2] = list(reversed(c.phase['recipe']['checks'][0:2]))
        c.phase['recipe_digest'] = digest(c.phase['recipe'])
    elif damage == 'prefix-as-tail':
        c.prefix['preparation_failure'] = copy.deepcopy(c.tail['preparation_failure'])
    elif damage == 'tail-receipt':
        c.tail['receipt'] = copy.deepcopy(c.prefix['receipt'])
    else:
        c.phase['receipt']['checks'] = []
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing-failure-key', 'failure', 'breach', 'scope', 'wrong-phase', 'too-late', 'nonfinite',
                                   'sample-missing', 'status-hash', 'profile-pin', 'transcript-hash'])
def test_last_completed_check_needs_a_bound_healthy_final_sample(intercheck_case, damage):
    c = intercheck_case
    status_path = Path(c.prefix['status'])
    if damage == 'missing-failure-key':
        c.status.pop('resource_failure')
    elif damage == 'failure':
        c.status['resource_failure'] = 'observation unavailable'
    elif damage == 'breach':
        c.status['resource_guard']['breach'] = copy.deepcopy(c.guard['breach'])
    elif damage == 'scope':
        c.status['resource_guard']['scope_digest'] = 'sha256:' + '0' * 64
    elif damage == 'wrong-phase':
        c.status['resource_guard']['active_attempt'] = 'another-phase'
    elif damage in ('too-late', 'nonfinite'):
        c.status['resource_guard']['observed_at'] = 151.0 if damage == 'too-late' else float('inf')
    elif damage == 'sample-missing':
        c.status.pop('resource_guard')
    elif damage == 'status-hash':
        status_path.write_text('{}')
    elif damage == 'profile-pin':
        c.packet['artifacts'][c.prefix['native_sandbox_boundary']['path']] = '0' * 64
    else:
        path = Path(c.prefix['receipt']['output'])
        path.write_text('changed evidence')
    if damage not in ('status-hash', 'profile-pin', 'transcript-hash'):
        status_path.write_text(json.dumps(c.status, allow_nan=True) if damage == 'nonfinite' else canonical(c.status))
        c.packet['artifacts'][str(status_path)] = _sha(status_path.read_bytes())
        if 'resource_guard' in c.status and damage != 'nonfinite':
            c.prefix['receipt']['resource_guard'] = copy.deepcopy(c.status['resource_guard'])
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['probe', 'renderer', 'stage', 'profile', 'owned', 'owner', 'members', 'boundary',
                                   'error', 'source-before', 'source-after', 'native-before', 'native-after',
                                   'temporary', 'output-before', 'output-after', 'output-mutation'])
def test_tail_cannot_cover_a_probe_launch_or_unexplained_effects(intercheck_case, damage):
    c = intercheck_case
    proof = c.tail['preparation_failure']['proof']
    if damage == 'probe':
        proof['probe']['state'] = 'reaped'
    elif damage == 'renderer':
        proof['renderer']['state'] = 'reaped'
    elif damage == 'stage':
        proof['stage'] = 'permission_probe'
    elif damage == 'profile':
        proof['profile'] = {'sha256': '0' * 64}
    elif damage == 'owned':
        c.tail['owned_temporary'] = {'roots': []}
    elif damage == 'owner':
        c.tail['owner'] = {'pid': 9999}
    elif damage == 'members':
        c.tail['members'] = {'9999': 'unknown'}
    elif damage == 'boundary':
        c.tail['native_sandbox_boundary'] = copy.deepcopy(c.prefix['native_sandbox_boundary'])
    elif damage == 'error':
        c.tail['preparation_failure']['error'] = 'different failure'
    elif damage == 'source-before':
        c.tail['before_source'] = {'changed': True}
    elif damage == 'source-after':
        c.tail['cleanup']['source_after'] = {'changed': True}
    elif damage == 'native-before':
        c.tail['before_native_paths'] = {'changed': True}
    elif damage == 'native-after':
        c.tail['cleanup']['native_path_effects']['after'] = {'changed': True}
    elif damage == 'temporary':
        c.tail['cleanup']['native_temporary_effects']['after'] = {'new-path': {}}
    elif damage == 'output-before':
        c.tail['before_outputs'] = {'changed': True}
    elif damage == 'output-after':
        c.tail['cleanup']['outputs_after'] = {'changed': True}
    else:
        c.tail['cleanup']['output_mutations'] = {'status': 'changed', 'changes': ['unexpected']}
    c.tail['preparation_failure']['effects'] = copy.deepcopy(c.tail['cleanup'])
    c.tail['preparation_failure']['receipt_digest'] = digest([proof, c.tail['cleanup']])
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['gap', 'other-error', 'extra-failure', 'active', 'source', 'budget',
                                   'model', 'output-cap', 'source-cap', 'free-floor'])
def test_other_failures_and_resource_limits_cannot_hide_in_intercheck_recovery(intercheck_case, damage):
    c = intercheck_case
    if damage == 'gap':
        c.guard['breach']['reason'] = 'active_observation_gap'
    elif damage == 'other-error':
        c.guard['breach']['error_message'] = 'unrelated failure'
    elif damage == 'extra-failure':
        c.record['unresolved_failures'].append({'reason': 'other', 'observed_at': 170.0})
    elif damage == 'active':
        c.guard['active_attempt'] = c.phase['id']
    elif damage in ('source', 'budget'):
        c.phase['receipt']['source_unchanged' if damage == 'source' else 'product_budget_unchanged'] = False
    elif damage == 'model':
        c.phase['receipt']['model_calls'] = 1
    elif damage in ('output-cap', 'source-cap'):
        c.guard['high_water_growth_bytes']['output_roots' if damage == 'output-cap' else 'source_evidence_roots'] = 9999
    else:
        c.guard['last_observation']['available_bytes'] = {'0': 9}
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


def _sidecar_state(path, content):
    path.write_text(canonical(content))
    path.chmod(0o600)
    info = path.stat()
    return {'path': str(path), 'exists': True, 'sha256': _sha(path.read_bytes()),
            'content': copy.deepcopy(content),
            'identity': {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
                         'mode': stat.S_IMODE(info.st_mode), 'size': info.st_size,
                         'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}}


def _prior_acknowledged_sidecar(c):
    primary = {'reason': 'active_observation_gap', 'observed_at': 80.0}
    secondary = {'reason': 'resource_observation_unavailable', 'observed_at': 81.0}
    old = copy.deepcopy(c.guard)
    old.update(breach=primary, high_water_growth_bytes={'output_roots': 7, 'source_evidence_roots': 11})
    passive = copy.deepcopy(old)
    passive.update(breach=None, high_water_growth_bytes={'output_roots': 9, 'source_evidence_roots': 13})
    old_phase = {'id': 'old-rejected', 'state': 'stopped', 'resource_id': 'old-resource',
                 'recipe': copy.deepcopy(c.phase['recipe']),
                 'resource_receipt': resource_limits.receipt(old),
                 'receipt': {'stopped': True, 'status': 'blocked', 'source_unchanged': True,
                             'product_budget_unchanged': True, 'model_calls': 0, 'checks': []}}
    c.record['phases'].insert(0, old_phase)
    c.record['unresolved_failures'].extend([primary, secondary])
    c.guards.update({'old-resource': old, 'old-passive': passive})
    path = resource_limits._failure_path(c.database, 'old-resource')
    content = {'work_id': 'old-resource', 'scope_digest': old['scope_digest'], 'breach': secondary,
               'high_water_growth_bytes': copy.deepcopy(passive['high_water_growth_bytes'])}
    saved = _sidecar_state(path, content)
    ack = {'accepted': False, 'failure_sidecar': {key: copy.deepcopy(saved[key]) for key in ('path', 'sha256', 'content')}}
    item = {'operation_id': 'old-reviewed-rejection', 'phase_id': old_phase['id'],
            'historical_breach': primary, 'historical_breaches': [primary, secondary],
            'resource_id': 'old-passive', 'resource_receipt': copy.deepcopy(resource_limits.receipt(passive)),
            'acknowledgment': ack}
    c.record['rejected_admission_dispositions'] = [item]
    c.record['charged_growth_bytes'] = {'output_roots': 40, 'source_evidence_roots': 30}
    c.packet['prior_dispositions'] = [{key: copy.deepcopy(item[key]) for key in
                                       ('operation_id', 'phase_id', 'resource_id', 'resource_receipt')} |
                                      {'acknowledgment_digest': digest(ack)}]
    c.packet['sidecars'].update({'old-resource': saved, 'old-passive': {
        'path': str(resource_limits._failure_path(c.database, 'old-passive')), 'exists': False}})
    _sync(c)
    return path, item


def test_prior_acknowledged_failure_latch_and_its_floors_remain_intact(intercheck_case):
    c = intercheck_case
    path, item = _prior_acknowledged_sidecar(c)
    before = copy.deepcopy((c.record, c.snapshot, item)), path.read_bytes()
    result = _validate(c)
    assert result['resource_high_water_floors']['old-resource'] == {'output_roots': 9, 'source_evidence_roots': 13}
    assert result['prior_charged_growth_bytes'] == {'output_roots': 40, 'source_evidence_roots': 30}
    assert (c.record, c.snapshot, item) == before[0] and path.read_bytes() == before[1]


@pytest.mark.parametrize('damage', ['missing', 'changed', 'unacknowledged', 'scope', 'floor', 'baseline-reset',
                                   'continuation-scope', 'continuation-floor', 'new-current-latch'])
def test_historical_or_new_failure_latches_cannot_disappear_or_be_ignored(intercheck_case, damage):
    c = intercheck_case
    path, item = _prior_acknowledged_sidecar(c)
    if damage == 'missing':
        path.unlink()
        c.packet['sidecars']['old-resource'] = {'path': str(path), 'exists': False}
    elif damage == 'changed':
        content = copy.deepcopy(c.packet['sidecars']['old-resource']['content'])
        content['high_water_growth_bytes']['output_roots'] += 1
        c.packet['sidecars']['old-resource'] = _sidecar_state(path, content)
    elif damage == 'unacknowledged':
        item['acknowledgment'].pop('failure_sidecar')
        c.packet['prior_dispositions'][0]['acknowledgment_digest'] = digest(item['acknowledgment'])
    elif damage == 'scope':
        content = copy.deepcopy(c.packet['sidecars']['old-resource']['content'])
        content['scope_digest'] = 'sha256:' + '0' * 64
        c.packet['sidecars']['old-resource'] = _sidecar_state(path, content)
    elif damage == 'floor':
        item['resource_receipt']['high_water_growth_bytes']['output_roots'] = 8
        c.packet['prior_dispositions'][0]['resource_receipt'] = copy.deepcopy(item['resource_receipt'])
    elif damage == 'baseline-reset':
        c.guards['old-passive']['baseline']['observed_at'] += 1
    elif damage == 'continuation-scope':
        c.guards['old-passive']['scope']['max_output_growth_bytes'] += 1
        c.guards['old-passive']['scope_digest'] = digest(c.guards['old-passive']['scope'])
    elif damage == 'continuation-floor':
        c.guards['old-passive']['high_water_growth_bytes']['output_roots'] = 8
    else:
        current = resource_limits._failure_path(c.database, c.phase['resource_id'])
        content = {'work_id': c.phase['resource_id'], 'scope_digest': c.guard['scope_digest'],
                   'breach': {'reason': 'resource_observation_unavailable', 'observed_at': 151.0},
                   'high_water_growth_bytes': copy.deepcopy(c.guard['high_water_growth_bytes'])}
        c.packet['sidecars'][c.phase['resource_id']] = _sidecar_state(current, content)
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('kind', ['quiescent_gap_dispositions', 'prelaunch_observation_dispositions',
                                 'rejected_admission_dispositions', 'intercheck_observation_dispositions'])
def test_any_existing_disposition_for_this_phase_blocks_another_acknowledgment(intercheck_case, kind):
    c = intercheck_case
    c.record[kind].append({'phase_id': c.phase['id']})
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('outcome', ['fail', 'blocked', 'cancelled'])
def test_completed_failure_outcomes_are_preserved_without_becoming_acceptance(intercheck_case, outcome):
    c = intercheck_case
    c.prefix['receipt']['result'] = outcome
    _sync(c)
    result = _validate(c)
    assert result['completed_prefix'][0]['outcome'] == outcome
    assert c.phase['receipt']['status'] == 'blocked'
    assert c.phase['receipt']['accepted'] is False


def test_unknown_completed_outcome_cannot_be_treated_as_qualified_history(intercheck_case):
    c = intercheck_case
    c.prefix['receipt']['result'] = 'unknown-success'
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['archive-raw', 'archive-revision', 'native-revision', 'missing-row', 'extra-guard'])
def test_caller_snapshot_must_exactly_match_the_reviewed_archive_and_inventories(intercheck_case, damage):
    c = intercheck_case
    if damage == 'archive-raw':
        c.snapshot['archive_raw'] = '{}'
    elif damage in ('archive-revision', 'native-revision'):
        c.snapshot[damage.replace('-', '_')] += 1
    elif damage == 'missing-row':
        c.snapshot['native_rows'].pop(c.prefix['check_id'])
    else:
        c.snapshot['resource_guards']['unexpected'] = canonical(c.guard)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['semantics', 'producer', 'verification', 'boolean-exit'])
def test_post_census_sample_semantics_require_the_supported_executed_producer(intercheck_case, damage):
    c = intercheck_case
    source = c.packet['execution_source']
    if damage == 'semantics':
        source['semantics'] = 'generic-healthy-sample'
    elif damage == 'producer':
        path = Path(source['manifest']['path'])
        manifest = json.loads(path.read_text())
        manifest['files']['src/agentic_tools/aos/native_processes.py'] = '0' * 64
        source['manifest'] = _pin(path, manifest)
        source['source_digest'] = digest({key.removeprefix('src/agentic_tools/'): value
                                         for key, value in manifest['files'].items()})
        path = Path(source['verification']['path'])
        verified = json.loads(path.read_text())
        verified['source_digest'] = source['source_digest']
        source['verification'] = _pin(path, verified)
    else:
        path = Path(source['verification']['path'])
        verified = json.loads(path.read_text())
        verified['pytest']['exit_code'] = False if damage == 'boolean-exit' else 1
        source['verification'] = _pin(path, verified)
    _seal(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_each_completed_renderer_binds_the_exact_supported_source_closure(intercheck_case, damage):
    c = intercheck_case
    renderer = c.prefix['native_sandbox_boundary']['renderer_receipt']
    if damage == 'missing':
        renderer.pop('component_sources')
    else:
        renderer['component_sources']['aos/native_processes.py'] = '0' * 64
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['missing', 'active', 'binding', 'owner'])
def test_control_and_original_observer_evidence_remain_required(intercheck_case, monkeypatch, damage):
    c = intercheck_case
    if damage == 'missing':
        c.control = None
    elif damage == 'active':
        c.control['active'] = True
    elif damage == 'binding':
        c.control['archive_binding'] = 'sha256:' + '0' * 64
    else:
        monkeypatch.setattr(native_processes, 'identity', lambda pid: copy.deepcopy(c.phase['observer_owner']))
    c.packet['control_digest'] = digest(c.control)
    _seal(c)
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('which', ['completed', 'preparation'])
def test_existing_pure_verification_failures_are_not_overridden(intercheck_case, monkeypatch, which):
    c = intercheck_case
    def failed(row):
        raise Rejected('fixture-verifier', 'retained evidence does not verify')
    if which == 'completed':
        monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(failed))
    else:
        monkeypatch.setattr(native_preparation, 'verify', failed)
    with pytest.raises(Rejected, match='fixture-verifier'):
        _validate(c)


@pytest.mark.parametrize('artifact', ['status', 'release', 'cancel', 'profile', 'stdout', 'stderr', 'output'])
def test_tail_launch_artifacts_must_remain_absent(intercheck_case, artifact):
    c = intercheck_case
    paths = {'status': c.tail['status'], 'release': c.tail['release'], 'cancel': c.tail['cancel_path'],
             'profile': str(c.directory / 'native-status' / (c.tail['check_id'] + '.sb')),
             **{name: str(Path(c.tail['output']) / (name + '.txt')) for name in ('stdout', 'stderr', 'output')}}
    Path(paths[artifact]).write_text('unexpected launch evidence')
    with pytest.raises(Rejected):
        _validate(c)


@pytest.mark.parametrize('damage', ['content', 'same-bytes-inode', 'symlink', 'absent-created',
                                   'producer-manifest', 'sidecar-missing', 'sidecar-new'])
def test_final_file_fence_detects_changes_after_validation(intercheck_case, damage):
    c = intercheck_case
    historical_path = None
    if damage == 'sidecar-missing':
        historical_path, _ = _prior_acknowledged_sidecar(c)
    proof = _validate(c)
    recovery.recheck(proof)
    recovery.recheck_sidecars(proof)
    path = Path(c.prefix['receipt']['output'])
    if damage == 'content':
        path.write_text('changed retained output')
    elif damage == 'same-bytes-inode':
        replacement = path.with_name('replacement.txt')
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
    elif damage == 'symlink':
        moved = path.with_name('moved-original.txt')
        path.rename(moved)
        path.symlink_to(moved)
    elif damage == 'absent-created':
        Path(c.tail['status']).write_text('{}')
    elif damage == 'producer-manifest':
        Path(c.packet['execution_source']['manifest']['path']).write_text('{}')
    elif damage == 'sidecar-missing':
        historical_path.unlink()
    else:
        resource_limits._failure_path(c.database, c.phase['resource_id']).write_text('{}')
    with pytest.raises(Rejected):
        recovery.recheck(proof)
    if damage in ('sidecar-missing', 'sidecar-new'):
        with pytest.raises(Rejected):
            recovery.recheck_sidecars(proof)


@pytest.mark.parametrize('field', ['max_output_growth_bytes', 'source', 'generated_roots', 'temporary_paths',
                                 'scan_seconds', 'max_observation_gap_seconds'])
def test_guard_scope_cannot_diverge_from_the_original_recipe(intercheck_case, field):
    c = intercheck_case
    if field in ('max_output_growth_bytes', 'scan_seconds', 'max_observation_gap_seconds'):
        c.guard['scope'][field] += 1
    elif field == 'source':
        c.guard['scope'][field] = str(c.outputs)
    else:
        c.guard['scope'][field] = ['unexpected']
    c.guard['scope_digest'] = digest(c.guard['scope'])
    c.status['resource_guard']['scope_digest'] = c.guard['scope_digest']
    c.prefix['receipt']['resource_guard'] = copy.deepcopy(c.status['resource_guard'])
    status = Path(c.prefix['status'])
    status.write_text(canonical(c.status))
    c.packet['artifacts'][str(status)] = _sha(status.read_bytes())
    _sync(c)
    with pytest.raises(Rejected):
        _validate(c)
