"""Observed failed preparation never becomes an invented native census."""
import json
import hashlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_tools.aos import native_network, native_preparation, native_processes, native_renderer, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, digest
from test_aos_codex_runtime import native_fixture


@pytest.fixture
def prepared_native(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child')
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    p.recipe.update(native_process_ownership=native_processes.BACKEND, probe_interpreter=sys.executable,
                    native_sandbox={'version': native_sandbox.VERSION, 'temporary_paths': [
                        {'parent': str(temporary), 'name': 'test-{pid}', 'kind': 'file'}]})
    p.temporary = temporary
    p.calls = []
    p.renderers = []
    original_popen = subprocess.Popen
    def never_launch(args, **kwargs):
        if native_renderer.is_launcher(args):
            child = original_popen(args, **kwargs)
            p.renderers.append(child)
            return child
        p.calls.append('supervisor')
        raise OSError('SIMULATED ambiguous supervisor Popen')
    monkeypatch.setattr(subprocess, 'Popen', never_launch)
    yield p
    assert all(child.poll() is not None for child in p.renderers)
    # The trusted renderer is real and reaped; product creation remains mocked.
    monkeypatch.setattr(p.runtime, '_native_stop', lambda *a, **k: None)
    p.runtime.close()


def saved(p):
    with p.runtime._db() as db:
        rows = list(db.execute('SELECT record FROM native_check'))
    assert len(rows) == 1
    return json.loads(rows[0][0])


def run_check(p):
    return p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)


def probe(p, monkeypatch, outcome='denied'):
    def run(args, **kwargs):
        p.calls.append('probe')
        assert args[4:7] == ['-I', '-S', '-c']
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        if outcome == 'ambiguous':
            raise OSError('SIMULATED unknown probe launch/wait')
        if outcome == 'interrupt':
            raise KeyboardInterrupt('SIMULATED interrupted preparation')
        if outcome == 'effects':
            (p.source / 'probe-effect.txt').write_text('known permission-probe effect')
            (p.outputs / 'probe-effect.txt').write_text('retained probe output')
            (p.state / 'worker-forbidden-canary').write_text('unexpected private canary write')
            (p.temporary / 'test-1234').write_text('retained temporary effect')
        if outcome == 'unreconciled':
            (p.source / 'unexpected-link').symlink_to(p.outputs)
        return subprocess.CompletedProcess(args, 0 if outcome == 'pass' else 1,
                                           'not json' if outcome == 'malformed' else json.dumps({'access': outcome == 'pass'}), '')
    monkeypatch.setattr(subprocess, 'run', run)


@pytest.mark.parametrize('outcome', ['denied', 'malformed', 'timeout', 'effects'])
def test_observed_preparation_failure_is_terminal_without_native_launch(prepared_native, monkeypatch, outcome):
    p = prepared_native
    probe(p, monkeypatch, outcome)
    with pytest.raises((Rejected, subprocess.TimeoutExpired)):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'clean' and not row.get('owner')
    assert row['preparation_failure']['proof']['supervisor_popen'] == 'not_called'
    assert row['preparation_failure']['proof']['probe']['state'] == 'reaped'
    assert row['preparation_failure']['proof']['renderer']['state'] == 'reaped'
    assert row['preparation_failure']['proof']['renderer'] == row['native_sandbox_boundary']['renderer_receipt']
    assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
    assert row['cleanup']['effects_reconciled'] and row['cleanup']['owned_processes'] == []
    assert 'process_ownership' not in row['cleanup'] and 'native_process_receipt' not in row
    if outcome == 'effects':
        assert row['cleanup']['partial_mutations']['changes'] == ['probe-effect.txt']
        assert row['cleanup']['probe_canary_effects']['after']['kind'] == 'file'
        assert str(p.temporary / 'test-1234') in row['cleanup']['native_temporary_effects']['after']
    assert p.runtime._native_stop(row['check_id'], cancel=True)['phase'] == 'clean'
    assert not Path(row['cancel_path']).exists()
    with pytest.raises(Rejected, match='native_preparation_failed'):
        run_check(p)
    assert p.calls == ['probe']
    reopened = CodexRuntime(p.runtime.lifecycle, {'N': p.recipe})
    try:
        assert reopened._native_stop(row['check_id'])['preparation_failure'] == row['preparation_failure']
    finally:
        reopened.close()


def test_deadline_after_reaped_probe_retains_no_launch_proof(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch, 'pass')
    original = p.runtime._native_deadline
    monkeypatch.setattr(p.runtime, '_native_deadline', lambda recipe, check, row=None:
                        datetime.now(timezone.utc) - timedelta(seconds=1) if p.calls == ['probe'] else original(recipe, check, row))
    with pytest.raises(Rejected, match='assignment_deadline'):
        run_check(p)
    assert saved(p)['preparation_failure']['proof']['stage'] == 'deadline'
    assert p.calls == ['probe']


def test_profile_failure_before_probe_records_no_process_created(prepared_native, monkeypatch):
    p = prepared_native
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', lambda *a: (_ for _ in ()).throw(
        Rejected('native_sandbox', 'SIMULATED profile preparation failure')))
    with pytest.raises(Rejected, match='native_sandbox'):
        run_check(p)
    row = saved(p)
    assert row['preparation_failure']['proof']['probe'] == {'state': 'not_started'}
    assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'
    assert p.calls == []


def test_legacy_inline_profile_failure_replays_without_renderer_field(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch)
    def inline_profile(recipe, row, *rest):
        path = p.state / 'native-status' / (row['check_id'] + '.sb')
        path.write_text('(version 1)\n(deny default)\n')
        row['native_sandbox_boundary'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'temporary_before': {}, 'contract': recipe['native_sandbox'], 'network_observation': None}
        p.runtime._native_save(row)
        return ['/usr/bin/sandbox-exec', '-f', str(path)]
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', inline_profile)
    with pytest.raises(Rejected, match='runtime_sandbox'):
        run_check(p)
    row = saved(p)
    assert row['preparation_failure']['proof'].pop('renderer') == {'format': native_renderer.FORMAT, 'state': 'not_started'}
    row['preparation_failure']['receipt_digest'] = digest([row['preparation_failure']['proof'], row['cleanup']])
    p.runtime._native_save(row)
    assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'
    assert p.renderers == [] and p.calls == ['probe']


@pytest.mark.parametrize('failure', ['failed', 'timeout', 'interrupted'])
def test_renderer_failure_requires_actual_child_reaping_before_clean_receipt(prepared_native, monkeypatch, failure):
    p = prepared_native
    def stop_drain(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('trusted native renderer', 0)
        if failure == 'interrupted':
            raise KeyboardInterrupt('SIMULATED renderer interruption')
        raise RuntimeError('SIMULATED renderer pipe failure')
    monkeypatch.setattr(native_renderer, '_drain', stop_drain)
    expected = {'failed': RuntimeError, 'timeout': subprocess.TimeoutExpired, 'interrupted': KeyboardInterrupt}[failure]
    with pytest.raises(expected):
        run_check(p)
    row = saved(p)
    renderer = row['preparation_failure']['proof']['renderer']
    assert renderer['state'] == 'reaped' and renderer['outcome'] == failure
    assert len(p.renderers) == 1 and p.renderers[0].poll() == renderer['exit_code']
    assert row['preparation_failure']['proof']['probe'] == {'state': 'not_started'}
    assert row['phase'] == 'clean' and p.calls == []
    assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'


def test_ambiguous_renderer_launch_never_becomes_a_reaped_probe_receipt(prepared_native, monkeypatch):
    p = prepared_native
    def ambiguous(args, **kwargs):
        assert native_renderer.is_launcher(args)
        raise OSError('SIMULATED renderer launch uncertainty')
    monkeypatch.setattr(subprocess, 'Popen', ambiguous)
    with pytest.raises(Rejected, match='check_cleanup_unknown'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'preparation_failure' not in row and 'cleanup' not in row
    assert p.calls == [] and p.renderers == []


def test_coherent_renderer_receipt_cannot_extend_original_deadline(prepared_native, monkeypatch):
    p = prepared_native
    def fail_drain(*args, **kwargs):
        raise RuntimeError('SIMULATED renderer read failure')
    monkeypatch.setattr(native_renderer, '_drain', fail_drain)
    with pytest.raises(RuntimeError):
        run_check(p)
    row = saved(p)
    assert 'native_sandbox_boundary' not in row
    renderer = row['preparation_failure']['proof']['renderer']
    renderer['execution_deadline'] = (datetime.fromisoformat(row['execution_deadline']) + timedelta(seconds=60)).isoformat()
    native_renderer.verify_receipt(renderer)
    row['preparation_failure']['receipt_digest'] = digest([row['preparation_failure']['proof'], row['cleanup']])
    p.runtime._native_save(row)
    with pytest.raises(Rejected, match='original native deadline'):
        p.runtime._native_stop(row['check_id'])
    assert saved(p)['phase'] == 'cleanup_unknown'


@pytest.mark.parametrize('damage', ['source', 'recipe', 'snapshot', 'profile'])
def test_renderer_source_input_and_profile_bindings_survive_cached_review(prepared_native, monkeypatch, damage):
    p = prepared_native
    probe(p, monkeypatch)
    with pytest.raises(Rejected, match='runtime_sandbox'):
        run_check(p)
    row = saved(p)
    renderer = row['preparation_failure']['proof']['renderer']
    key = {'source': 'component_source_digest', 'recipe': 'recipe_contract_digest',
           'snapshot': 'preexisting_digest', 'profile': 'profile_sha256'}[damage]
    renderer[key] = 'f' * 64 if damage == 'profile' else 'sha256:' + 'f' * 64
    # Keep the outer checksum coherent to exercise the renderer/input binding.
    row['preparation_failure']['receipt_digest'] = digest([
        row['preparation_failure']['proof'], row['cleanup']])
    p.runtime._native_save(row)
    with pytest.raises(Rejected):
        p.runtime._native_stop(row['check_id'])
    assert saved(p)['phase'] == 'cleanup_unknown'
    assert p.calls == ['probe'] and len(p.renderers) == 1


def test_preparation_baseline_failure_has_no_admitted_intent_or_output(prepared_native):
    p = prepared_native
    target = p.state / 'unrelated-file'
    target.write_text('preserve unrelated state')
    (p.state / 'worker-forbidden-canary').symlink_to(target)
    with pytest.raises(Rejected, match='canary is an alias'):
        run_check(p)
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 0
    assert not (p.outputs / 'check').exists()
    assert target.read_text() == 'preserve unrelated state'
    assert p.calls == []


def test_resource_payload_rejection_retains_preparation_failure_without_launch(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch, 'pass')
    p.recipe['resource_limits'] = {'simulated_bound_guard': True}
    calls = []
    def reject_payload(recipe):
        if p.calls != ['probe']:
            return None
        calls.append('resource-payload')
        raise Rejected('resource_limits', 'SIMULATED retained resource allowance unavailable')
    monkeypatch.setattr(p.runtime, '_resource_native_payload', reject_payload)
    with pytest.raises(Rejected, match='resource_limits'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'clean'
    assert row['preparation_failure']['proof']['stage'] == 'supervisor_preparation'
    assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
    assert p.runtime._native_stop(row['check_id'])['cleanup']['effects_reconciled'] is True
    with pytest.raises(Rejected, match='native_preparation_failed'):
        run_check(p)
    assert p.calls == ['probe'] and calls == ['resource-payload']


def test_stderr_open_failure_closes_stdout_and_never_launches(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch, 'pass')
    original = Path.open
    opened = []
    def open_output(path, mode='r', *args, **kwargs):
        if mode == 'x' and path == p.outputs / 'check' / 'stderr.txt':
            raise OSError('SIMULATED stderr creation failure')
        stream = original(path, mode, *args, **kwargs)
        if mode == 'x' and path == p.outputs / 'check' / 'stdout.txt':
            opened.append(stream)
        return stream
    monkeypatch.setattr(Path, 'open', open_output)
    with pytest.raises(OSError, match='stderr creation failure'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'clean'
    assert row['preparation_failure']['proof']['stage'] == 'supervisor_preparation'
    assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
    assert row['cleanup']['outputs_after']['check/stdout.txt'] == hashlib.sha256(b'').hexdigest()
    assert len(opened) == 1 and opened[0].closed
    assert p.runtime._native_stop(row['check_id'])['cleanup']['effects_reconciled'] is True
    with pytest.raises(Rejected, match='native_preparation_failed'):
        run_check(p)
    assert p.calls == ['probe']


@pytest.mark.parametrize('outcome', ['ambiguous', 'interrupt', 'unreconciled', 'pass'])
def test_ambiguous_probe_effects_or_supervisor_launch_remain_unresolved(prepared_native, monkeypatch, outcome):
    p = prepared_native
    probe(p, monkeypatch, outcome)
    with pytest.raises((Rejected, OSError, KeyboardInterrupt)):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'preparation_failure' not in row and 'cleanup' not in row
    with pytest.raises(Rejected, match='check_cleanup_unknown'):
        native_preparation.verify(row)
    assert p.calls == (['probe', 'supervisor'] if outcome == 'pass' else ['probe'])


@pytest.mark.parametrize('damage', ['owner', 'status', 'proof', 'effects', 'baseline', 'profile'])
def test_contradictory_saved_failure_holds_again(prepared_native, monkeypatch, damage):
    p = prepared_native
    probe(p, monkeypatch)
    with pytest.raises(Rejected):
        run_check(p)
    row = saved(p)
    if damage == 'owner':
        row['owner'] = {'pid': 123, 'started': 'ambiguous'}
    elif damage == 'status':
        Path(row['status']).write_text('{}')
    elif damage == 'proof':
        row['preparation_failure']['proof']['supervisor_popen'] = 'possibly_called'
    elif damage == 'baseline':
        row['before_source'] = {}
    elif damage == 'profile':
        row['native_sandbox_boundary']['contract'] = {}
    else:
        row['cleanup']['source_after'] = {}
    p.runtime._native_save(row)
    with pytest.raises(Rejected, match='check_cleanup_unknown'):
        p.runtime._native_stop(row['check_id'])
    held = saved(p)
    assert held['phase'] == 'cleanup_unknown' and 'cleanup' not in held
    assert held['preparation_failure']['effects']


def test_failure_receipt_must_be_durable_before_restart_can_use_it(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch)
    original = p.runtime._native_save
    def fail_receipt(row):
        if row.get('preparation_failure'):
            raise OSError('SIMULATED receipt save failure')
        original(row)
    monkeypatch.setattr(p.runtime, '_native_save', fail_receipt)
    with pytest.raises(OSError, match='receipt save failure'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'preparation_failure' not in row
    with pytest.raises(Rejected):
        native_preparation.verify(row)


def test_ambiguous_native_observer_is_not_covered_by_reaped_probe(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch, 'pass')
    def ambiguous(*args):
        try:
            raise OSError('SIMULATED native observer launch/wait uncertainty')
        except OSError as error:
            raise Rejected('native_network', 'observation unavailable') from error
    monkeypatch.setattr(native_network, 'verify_observation', ambiguous)
    with pytest.raises(Rejected, match='check_cleanup_unknown'):
        run_check(p)
    assert 'preparation_failure' not in saved(p)
    assert p.calls == ['probe']


def test_fixed_native_observer_timeout_keeps_exact_reaped_evidence(prepared_native, monkeypatch):
    p = prepared_native
    probe(p, monkeypatch, 'pass')
    def timeout(*args):
        try:
            raise subprocess.TimeoutExpired(native_preparation.OBSERVERS[1], 10)
        except subprocess.TimeoutExpired as error:
            raise Rejected('native_network', 'observer exceeded bounded wait') from error
    monkeypatch.setattr(native_network, 'verify_observation', timeout)
    with pytest.raises(Rejected, match='native_network'):
        run_check(p)
    row = saved(p)
    assert row['preparation_failure']['proof']['native_observer_timeouts'] == [
        {'argv': native_preparation.OBSERVERS[1], 'state': 'reaped', 'outcome': 'timeout'}]
    assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'
    assert p.calls == ['probe']


@pytest.mark.skipif(sys.platform != 'darwin', reason='qualified macOS permission-probe route')
def test_real_denied_probe_is_reaped_without_running_product_command(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child')
    p.recipe.update(native_process_ownership=native_processes.BACKEND, probe_interpreter=sys.executable,
                    native_sandbox={'version': native_sandbox.VERSION})
    launched = []
    original = subprocess.Popen
    def popen(args, **kwargs):
        assert args[0] == '/usr/bin/sandbox-exec', 'A product supervisor must never be launched'
        child = original(args, **kwargs)
        launched.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', popen)
    def denied_profile(recipe, row, *rest):
        path = p.state / 'native-status' / (row['check_id'] + '.sb')
        path.write_text('(version 1)\n(deny default)\n')
        row['native_sandbox_boundary'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                          'temporary_before': {}, 'contract': recipe['native_sandbox']}
        p.runtime._native_save(row)
        return ['/usr/bin/sandbox-exec', '-f', str(path)]
    monkeypatch.setattr(p.runtime, '_native_profile_prepare', denied_profile)
    try:
        with pytest.raises(Rejected, match='runtime_sandbox'):
            run_check(p)
        row = saved(p)
        assert len(launched) == 1 and launched[0].poll() is not None
        assert row['preparation_failure']['proof']['probe']['exit_code'] == launched[0].returncode
        assert row['cleanup']['native_admission']['launched'] is False
        assert p.runtime._native_stop(row['check_id'])['phase'] == 'clean'
    finally:
        p.runtime.close()
