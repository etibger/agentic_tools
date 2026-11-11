"""Reviewed-host admission and replay with a simulated runner; no host jobs."""
import copy
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import reviewed_host as host
from agentic_tools.aos.codex_runtime import CodexRuntime, candidate
from agentic_tools.aos.store import Rejected, canonical, digest
from agentic_tools.check_adapters import identities


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    source, outputs, state = (tmp_path / name for name in ('source', 'outputs', 'state'))
    source.mkdir()
    outputs.mkdir()
    state.mkdir()
    (source / 'main.rs').write_text('reviewed source')
    run_outputs = outputs / 'assessment-1'
    run_outputs.mkdir()
    descriptor = {'name': 'build', 'argv': ['/usr/bin/true'], 'timeout_seconds': 30,
                  'result_kind': 'build', 'adapter_id': 'native-build-v1', 'scope': 'source'}
    base = {'source': str(source), 'outputs': str(outputs), 'read_roots': [], 'max_seconds': 60,
            'checks': [descriptor], 'execution_policy': 'isolated-checkout-v1',
            'checkout_policy': {'native_validation': 'reviewed-host-v1'}}
    binding = identities()['native-build-v1']
    adapters = {'native-build-v1': binding}
    errors = []
    lifecycle = SimpleNamespace(store=SimpleNamespace(directory=state),
                                assignment_authority_errors=lambda assignment: list(errors))
    runtime = CodexRuntime(lifecycle, {'WORK': base}, work_id='WORK')
    # This existing fixture selects no coordinator evidence gate.
    monkeypatch.setattr(runtime, '_coordinator_evidence', lambda assignment, **kwargs: None)
    monkeypatch.setattr(runtime, '_bindings', lambda assignment, recipe: copy.deepcopy(adapters))
    monkeypatch.setattr(host.checkout_boundary, 'scoped_outputs',
                        lambda recipe, output: {**recipe, 'outputs': str(output)})
    environment = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    monkeypatch.setattr(host.checkout_boundary, 'environment', lambda recipe, native=False: dict(environment))
    frozen = candidate(source, recipe=base)
    report = run_outputs / 'review-assessment-1.txt'
    report.write_text(json.dumps({'candidate_identity': frozen['identity'], 'verdict': 'pass',
                                  'findings': [], 'limits': 'Independent source review only.'}))
    record = {'attempt_id': 'assessment-1', 'phase': 'checking', 'created_at': datetime.now(timezone.utc).isoformat(),
              'assignment': {'work_id': 'WORK', 'attempt_id': 'assessment-1', 'kind': 'assessment', 'executor': 'reviewer',
                             'candidate': canonical(frozen)},
              'recipe_digest': digest(base), 'model_calls_attempted': 1,
              'startup_monotonic': time.monotonic(), 'startup_deadline_monotonic': time.monotonic() + 60,
              'startup_deadline': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
              'startup_cleanup_reserve': 30,
              'terminal_turn': {'status': 'completed'}, 'run_outputs': str(run_outputs),
              'model_stop_receipt': {'attempt_id': 'assessment-1', 'stopped': True,
                                     'effects_reconciled': True, 'owned_processes': []}}
    if getattr(request, 'param', True):
        record['reviewed_host_gate'] = {
            'mode': 'reviewed-host-v1', 'candidate': frozen, 'recipe_digest': digest(base),
            'review': {'path': str(report), 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()},
            'checks': copy.deepcopy(base['checks']), 'adapters': copy.deepcopy(adapters)}
    with runtime._db() as db:
        db.execute('INSERT INTO attempt VALUES(?,?)', (record['attempt_id'], canonical(record)))
    scoped = {**copy.deepcopy(base), 'outputs': str(run_outputs), '_attempt_id': record['attempt_id'],
              '_adapter_bindings': adapters}
    scoped['checkout_policy']['protected_roots'] = [str(state)]
    value = SimpleNamespace(runtime=runtime, recipe=scoped, base=base, descriptor=descriptor,
                            frozen=frozen, record=record, output=run_outputs / 'build', report=report,
                            source=source, errors=errors, calls=[], reconciles=[], before=None,
                            runner_error=None, clean=True, cancel=False, timed_out=False, exit_code=0, after=None,
                            environment=environment, no_launch=False)

    def verify(snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('cleanup_verified') is not True:
            raise Rejected('check_cleanup_unknown', 'Simulated ownership remains unresolved')
        return copy.deepcopy(snapshot)

    def run_owned(argv, cwd, env, control, *, deadline, cleanup_deadline, before_go, cancelled, persist):
        value.calls.append({'argv': list(argv), 'deadline': deadline, 'cleanup_deadline': cleanup_deadline})
        if value.runner_error:
            raise value.runner_error
        control.mkdir()
        owner = {'pid': 7001, 'uid': 501, 'birth': [100, 1], 'parent': 7000, 'group': 7001}
        snapshot = {'backend': host.BACKEND, 'argv': list(argv), 'cwd': cwd, 'control': str(control),
                    'environment_sha256': hashlib.sha256(json.dumps(env, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                    'deadline_monotonic': deadline, 'cleanup_deadline_monotonic': min(deadline + 26, cleanup_deadline),
                    'owner': owner, 'controller': {**owner, 'pid': 7000}, 'phase': 'held',
                    'cleanup_verified': False, 'errors': [], 'launch_authorized': False}
        if value.no_launch:
            snapshot.update(owner=None, child=None, no_launch=True, phase='complete', cleanup_verified=True,
                            errors=['preparation failed before logs or supervisor'])
            persist(snapshot)
            return snapshot
        persist(snapshot)
        saved = rows(value)[0]
        assert saved['owner'] == owner  # Ownership is durable before GO.
        try:
            if value.before:
                value.before(value)
            before_go(deadline)
            assert not cancelled()
        except Exception as error:
            snapshot.update(cleanup_verified=True, phase='finished',
                            child={'exit_code': None}, errors=[str(error)])
            (control / 'stdout.log').write_text('')
            (control / 'stderr.log').write_text('pre-GO rejection')
            persist(snapshot)
            raise
        (control / 'stdout.log').write_text('build complete')
        (control / 'stderr.log').write_text('')
        if value.after:
            value.after(value)
        snapshot.update(launch_authorized=True, cleanup_verified=value.clean,
                        phase='finished' if value.clean else 'held', child={'exit_code': value.exit_code},
                        cancel_requested=value.cancel, timed_out=value.timed_out)
        persist(snapshot)
        return snapshot

    def reconcile(snapshot, *, deadline, persist, cancel=False):
        value.reconciles.append({'snapshot': copy.deepcopy(snapshot), 'deadline': deadline, 'cancel': cancel})
        persist(snapshot)
        return snapshot

    monkeypatch.setattr(host.owned_host_command, 'run_owned', run_owned)
    monkeypatch.setattr(host.owned_host_command, 'verify_completed', verify)
    monkeypatch.setattr(host.owned_host_command, 'reconcile_owned', reconcile)
    return value


def rows(value):
    with value.runtime._db() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check ORDER BY id')]


def update_attempt(value, transform):
    record = value.runtime._get(value.record['attempt_id'])
    transform(record)
    value.runtime._save(record)


def execute(value):
    return host.check(value.runtime, value.recipe, value.descriptor, value.output, 'reviewer', value.frozen)


@pytest.mark.parametrize('mutation', [
    lambda row: row.update(startup_qualification_only=True),
    lambda row: row.update(model_calls_attempted=0),
    lambda row: row['terminal_turn'].update(status='failed'),
    lambda row: row['model_stop_receipt'].update(stopped=False),
    lambda row: row['model_stop_receipt'].update(effects_reconciled=False),
    lambda row: row['model_stop_receipt'].update(owned_processes=[7002]),
    lambda row: row['model_stop_receipt'].update(attempt_id='another-attempt'),
    lambda row: row.update(native_cancel_requested=True),
])
def test_only_reviewed_completed_product_assessment_admits(case, mutation):
    update_attempt(case, mutation)
    with pytest.raises(Rejected):
        execute(case)
    assert rows(case) == [] and case.calls == [] and not case.output.exists()


@pytest.mark.parametrize('case', [False], indirect=True)
def test_never_reviewed_attempt_is_denied_before_native_admission(case):
    assert 'reviewed_host_gate' not in case.runtime._get(case.record['attempt_id'])
    with pytest.raises(Rejected, match='Saved review gate'):
        execute(case)
    assert rows(case) == [] and case.calls == [] and not case.output.exists()


def test_explicit_tier_and_exact_command_required(case):
    case.recipe['checkout_policy'].pop('native_validation')
    with pytest.raises(Rejected, match='explicit approved'):
        execute(case)
    case.recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    case.descriptor = {**case.descriptor, 'argv': ['/usr/bin/false']}
    with pytest.raises(Rejected, match='approved candidate or recipe'):
        execute(case)
    assert rows(case) == [] and case.calls == []


@pytest.mark.parametrize('change', ['review', 'source', 'authority', 'cancellation', 'adapter', 'runtime'])
def test_go_rechecks_review_source_authority_and_saved_bindings(case, change, monkeypatch):
    def alter(value):
        if change == 'review':
            value.report.write_text(value.report.read_text() + '\n')
        elif change == 'source':
            (value.source / 'main.rs').write_text('changed after review')
        elif change == 'authority':
            value.errors.append('assignment no longer sent')
        elif change == 'cancellation':
            update_attempt(value, lambda row: row.update(native_cancel_requested=True))
        elif change == 'adapter':
            monkeypatch.setattr(value.runtime, '_bindings', lambda *args: {})
        else:
            monkeypatch.setattr(host, 'source_manifest', lambda: {'changed.py': 'changed'})
    case.before = alter
    with pytest.raises(Rejected):
        execute(case)
    assert len(case.calls) == 1
    saved = rows(case)[0]
    assert saved['host_owner_receipt']['launch_authorized'] is False
    assert saved['phase'] == 'clean'


def test_success_retains_owned_cleanup_and_cached_replay_never_relaunches(case):
    receipt = execute(case)
    assert receipt['result'] == 'pass'
    assert receipt['cleanup']['stopped'] and receipt['cleanup']['effects_reconciled']
    assert receipt['cleanup']['owned_processes'] == []
    assert receipt['review'] == case.record['reviewed_host_gate']['review']
    assert 'ordinary-user' in receipt['environment']
    assert execute(case) == receipt
    assert len(case.calls) == 1
    checked = host.verify_completed(rows(case)[0])
    assert checked['receipt'] == receipt
    assert 'build/output.txt' in receipt['cleanup']['outputs_after']
    assert 'build/output.txt' in receipt['cleanup']['output_mutations']['changes']


def test_completed_raw_evidence_change_is_not_silently_reused(case):
    receipt = execute(case)
    Path(receipt['output']).write_text('replaced evidence')
    with pytest.raises(Rejected, match='raw check output'):
        execute(case)
    assert len(case.calls) == 1


def test_interrupted_evidence_finalization_resumes_without_rerunning_command(case, monkeypatch):
    adapter = host.adapter_for(case.descriptor)
    original = adapter.interpret
    def interrupted(*args):
        raise RuntimeError('interrupted after transcript creation')
    monkeypatch.setattr(adapter, 'interpret', interrupted)
    with pytest.raises(RuntimeError, match='transcript creation'):
        execute(case)
    assert rows(case)[0]['phase'] == 'clean' and not rows(case)[0].get('receipt')
    retained = (case.output / 'output.txt').read_bytes()
    monkeypatch.setattr(adapter, 'interpret', original)
    assert execute(case)['result'] == 'pass'
    assert len(case.calls) == 1 and (case.output / 'output.txt').read_bytes() == retained


@pytest.mark.parametrize('mode,expected', [('cancel', 'cancelled'), ('timed_out', 'timeout')])
def test_clean_owned_stop_does_not_reclassify_interruption_as_pass(case, mode, expected):
    setattr(case, mode, True)
    assert execute(case)['result'] == expected
    assert rows(case)[0]['phase'] == 'clean'


def test_runner_exception_without_owner_proof_is_held_and_never_relaunched(case):
    case.runner_error = OSError('ambiguous launch failure')
    with pytest.raises(OSError):
        execute(case)
    assert rows(case)[0]['phase'] == 'cleanup_unknown'
    assert rows(case)[0].get('cleanup') is None
    with pytest.raises(Rejected, match='never relaunch'):
        execute(case)
    assert len(case.calls) == 1


def test_incomplete_cleanup_cannot_be_promoted_by_replay_or_cancel(case):
    case.clean = False
    with pytest.raises(Rejected, match='cleanup remains unresolved'):
        execute(case)
    row = rows(case)[0]
    with pytest.raises(Rejected):
        host.stop(case.runtime, row['check_id'], cancel=True)
    assert rows(case)[0]['phase'] == 'cleanup_unknown'
    assert case.reconciles[-1]['cancel'] is True
    assert case.reconciles[-1]['deadline'] == row['host_owner_receipt']['cleanup_deadline_monotonic']
    assert len(case.calls) == 1


def test_unresolved_native_row_blocks_host_lane_but_clean_history_does_not(case):
    other = {'check_id': 'other', 'attempt_id': None, 'output': str(case.output.parent / 'other'), 'phase': 'running'}
    with case.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('other', None, canonical(other)))
    with pytest.raises(Rejected, match='serial host test lane'):
        execute(case)
    assert case.calls == []
    other['phase'] = 'clean'
    with case.runtime._db() as db:
        db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(other), 'other'))
    assert execute(case)['result'] == 'pass'


def test_original_deadline_is_not_replaced_by_per_check_timeout(case, monkeypatch):
    remaining = datetime.now(timezone.utc) + timedelta(seconds=2)
    monkeypatch.setattr(case.runtime, '_native_deadline', lambda *args: remaining)
    before = time.monotonic()
    assert execute(case)['result'] == 'pass'
    assert 0 < case.calls[0]['deadline'] - before <= 2
    assert case.calls[0]['cleanup_deadline'] <= case.record['startup_deadline_monotonic']


def test_expired_admission_creates_no_native_intent_or_runner(case, monkeypatch):
    monkeypatch.setattr(case.runtime, '_native_deadline',
                        lambda *args: datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(Rejected, match='deadline expired'):
        execute(case)
    assert rows(case) == [] and case.calls == []


def test_check_output_cannot_escape_exact_attempt(case):
    case.output = case.output.parent.parent / 'foreign'
    with pytest.raises(Rejected, match='exact attempt'):
        execute(case)
    assert rows(case) == [] and case.calls == []


def test_late_final_callback_cannot_resurrect_completed_check(case):
    execute(case)
    row = rows(case)[0]
    host._persist(case.runtime, row['check_id'], copy.deepcopy(row['host_owner_receipt']))
    assert rows(case)[0] == row
    altered = copy.deepcopy(row['host_owner_receipt'])
    altered['launch_authorized'] = False
    with pytest.raises(Rejected, match='Late ownership evidence'):
        host._persist(case.runtime, row['check_id'], altered)
    assert rows(case)[0] == row


def test_source_mutation_is_observed_and_never_reported_pass(case):
    case.after = lambda value: (value.source / 'main.rs').write_text('host command changed source')
    receipt = execute(case)
    assert receipt['result'] == 'blocked'
    assert receipt['cleanup']['partial_mutations']['changes'] == ['main.rs']
    assert receipt['cleanup']['stopped'] is True


def test_command_cannot_redirect_trusted_transcript_write(case, tmp_path):
    outside = tmp_path / 'preserve'
    outside.write_text('unrelated')
    case.after = lambda value: (value.output / 'output.txt').symlink_to(outside)
    with pytest.raises(Rejected):
        execute(case)
    assert outside.read_text() == 'unrelated'
    assert len(case.calls) == 1


def test_cross_runtime_stale_owner_update_cannot_clear_cancellation(case):
    case.clean = False
    with pytest.raises(Rejected):
        execute(case)
    row = rows(case)[0]
    stale = copy.deepcopy(row)
    other = CodexRuntime(case.runtime.lifecycle, {'WORK': case.base}, work_id='WORK')
    selected = other._native_get(row['check_id'])
    selected['cancelled'] = True
    other._native_save(selected)
    case.runtime._native_save(stale)
    assert case.runtime._native_get(row['check_id'])['cancelled'] is True


def test_latest_sticky_owner_failure_cannot_be_ignored_by_completed_readback(case, monkeypatch):
    execute(case)
    def latest(snapshot):
        return {**copy.deepcopy(snapshot), 'cancel_requested': True, 'errors': ['later retained cancellation']}
    monkeypatch.setattr(host.owned_host_command, 'verify_completed', latest)
    with pytest.raises(Rejected, match='Newer host ownership evidence'):
        host.verify_completed(rows(case)[0])
    with pytest.raises(Rejected, match='Newer host ownership evidence'):
        execute(case)
    assert len(case.calls) == 1


def test_known_no_launch_before_log_creation_is_blocked_with_positive_cleanup(case):
    case.no_launch = True
    receipt = execute(case)
    assert receipt['result'] == 'blocked' and receipt['exit_code'] is None
    assert receipt['raw_outputs'] == {}
    assert receipt['cleanup']['stopped'] is True
    assert rows(case)[0]['phase'] == 'clean'


def test_unicode_environment_binding_matches_maintained_runner_serialization(case):
    case.environment['LANGUAGE_LABEL'] = 'Norsk æøå'
    assert execute(case)['result'] == 'pass'


def test_clean_nonzero_command_exit_is_a_failed_check(case):
    case.exit_code = 1
    receipt = execute(case)
    assert receipt['result'] == 'fail' and receipt['exit_code'] == 1
    assert receipt['cleanup']['stopped'] is True
