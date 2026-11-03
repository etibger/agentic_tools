"""Fixed native renderer child: source binding, bounds, and exact reaping."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from agentic_tools.aos import native_renderer as renderer, native_sandbox
from agentic_tools.aos.store import Rejected


pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='exact macOS native renderer identities')


@pytest.fixture
def render_request(tmp_path):
    source, output, temporary, state = (tmp_path / name for name in ('source', 'output', 'temporary', 'state'))
    for path in (source, output, temporary, state):
        path.mkdir()
    recipe = {'source': str(source), 'outputs': str(output), 'native_sandbox': {
        'version': native_sandbox.VERSION,
        'temporary_paths': [{'parent': str(temporary), 'name': 'fixture-{pid}', 'kind': 'tree'}]}}
    return {'recipe': recipe, 'filesystem': {':root': 'deny', ':minimal': 'read', str(source): 'read',
            str(output): 'write', str(state): 'deny'}, 'preexisting': {}, 'protected': [str(state), str(source)],
            'network_observation': None}


def invoke(render_request, observed, seconds=5):
    return renderer.render(**render_request, deadline=datetime.now(timezone.utc) + timedelta(seconds=seconds),
                           observe=observed.append, expected_sources=renderer.source_hashes())


def child_control(monkeypatch, code):
    # A deliberately fixed test observer waits for parent identity capture before
    # executing its bounded fault. No product or source command is invoked.
    monkeypatch.setattr(renderer, 'BOOTSTRAP', 'import sys\nsys.stdin.buffer.read()\n' + code)


def test_exact_profile_bytes_and_historical_receipt(render_request, monkeypatch):
    observed = []
    expected = native_sandbox.render(**render_request).encode()
    actual = invoke(render_request, observed)
    assert actual == expected
    final = observed[-1]
    assert final['state'] == 'reaped' and final['outcome'] == 'success'
    assert final['profile_sha256'] == hashlib.sha256(expected).hexdigest()
    assert final['owner_identity']['pid'] == final['pid']
    assert final['owner_identity']['uid'] == os.getuid()
    assert final['outputs_complete'] and final['output_validated']
    assert final['source_hashes'] == renderer.source_hashes()
    assert [v['state'] for v in observed] == ['not_started', 'not_started', 'launch_ambiguous',
                                               'launch_ambiguous', 'reaped', 'reaped']
    monkeypatch.setattr(renderer, '_read', lambda *a: pytest.fail('historical validation reread code'))
    assert renderer.verify_receipt(final, require_success=True) == final


def test_fixed_isolated_launch_ignores_cwd_and_environment_shadow_modules(render_request, tmp_path, monkeypatch):
    marker = tmp_path / 'untrusted-import'
    for name in ('json', 'hashlib', 'pathlib', 'subprocess', 'types'):
        (tmp_path / (name + '.py')).write_text(f'open({str(marker)!r},"w").write("hijacked")\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('PYTHONSTARTUP', str(tmp_path / 'json.py'))
    observed = []
    assert invoke(render_request, observed).startswith(b'(version 1)\n')
    assert not marker.exists()
    assert all(observed[-1][k] is True for k in ('isolated', 'site_disabled', 'bytecode_disabled'))
    assert renderer.is_launcher([sys.executable, '-I', '-S', '-B', '-c', renderer.BOOTSTRAP])
    assert not renderer.is_launcher([sys.executable, '-I', '-S', '-c', renderer.BOOTSTRAP])


@pytest.mark.parametrize('fault', ['source', 'deadline', 'input'])
def test_prelaunch_validation_does_not_spawn(render_request, monkeypatch, fault):
    observed = []
    sources = renderer.source_hashes()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    if fault == 'source':
        sources['aos/native_sandbox.py'] = '0' * 64
    elif fault == 'deadline':
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        monkeypatch.setattr(renderer, 'MAX_INPUT', 1)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid preparation launched'))
    with pytest.raises(Rejected):
        renderer.render(**render_request, deadline=deadline, observe=observed.append, expected_sources=sources)
    assert observed == [{'format': renderer.FORMAT, 'state': 'not_started'}]


def test_rejected_profile_is_reaped_without_provisional_success(render_request):
    observed = []
    render_request['filesystem'][':root'] = 'write'
    with pytest.raises(Rejected, match='default-deny'):
        invoke(render_request, observed)
    assert observed[-1]['outcome'] == 'rejected'
    renderer.verify_receipt(observed[-1])
    with pytest.raises(Rejected):
        renderer.verify_receipt(observed[-1], require_success=True)


def test_original_deadline_kills_and_reaps_exact_child(render_request, monkeypatch):
    child_control(monkeypatch, 'import time\ntime.sleep(30)\n')
    observed = []
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(render_request, observed, seconds=.25)
    assert time.monotonic() - start < 2
    final = observed[-1]
    assert final['state'] == 'reaped' and final['outcome'] == 'timeout' and final['exit_code'] < 0
    assert not final['outputs_complete'] and 'profile_sha256' not in final
    renderer.verify_receipt(final)
    assert renderer.native_processes.identity(final['pid']) is None


def test_interruption_kills_and_reaps_before_propagating(render_request, monkeypatch):
    observed = []
    monkeypatch.setattr(renderer, '_drain', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt('fixture interrupt')))
    with pytest.raises(KeyboardInterrupt, match='fixture interrupt'):
        invoke(render_request, observed)
    assert observed[-1]['outcome'] == 'interrupted'
    renderer.verify_receipt(observed[-1])
    assert renderer.native_processes.identity(observed[-1]['pid']) is None


def test_missing_identity_never_releases_input_or_claims_reaped_receipt(render_request, monkeypatch):
    observed, children = [], []
    original = subprocess.Popen
    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    monkeypatch.setattr(renderer.native_processes, 'identity', lambda pid: None)
    monkeypatch.setattr(renderer, '_drain', lambda *a: pytest.fail('input released without exact identity'))
    with pytest.raises(Rejected, match='identity was not proved'):
        invoke(render_request, observed)
    assert len(children) == 1 and children[0].poll() is not None
    assert observed[-1]['state'] == 'launch_ambiguous'
    with pytest.raises(Rejected):
        renderer.verify_receipt(observed[-1])


def test_unproved_reap_remains_ambiguous(render_request, monkeypatch):
    observed, children = [], []
    original = subprocess.Popen
    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append((child, child.wait))
        child.wait = lambda *a, **k: (_ for _ in ()).throw(TimeoutError('SIMULATED missing exact wait'))
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    monkeypatch.setattr(renderer, '_drain', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        with pytest.raises(Rejected, match='exact exit/reaping is unproved'):
            invoke(render_request, observed)
        assert observed[-1]['state'] == 'launch_ambiguous'
        with pytest.raises(Rejected):
            renderer.verify_receipt(observed[-1])
    finally:
        for child, wait in children:
            if child.poll() is None:
                child.kill()
            wait(timeout=3)


def test_ambiguous_popen_error_is_never_no_launch_proof(render_request, monkeypatch):
    observed = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('SIMULATED launch unknown')))
    with pytest.raises(Rejected, match='launch/reaping is unproved'):
        invoke(render_request, observed)
    assert observed[-1]['state'] == 'launch_ambiguous'


@pytest.mark.parametrize('fault', ['output', 'stderr', 'utf8', 'incomplete'])
def test_no_partial_or_oversized_profile_can_pass(render_request, monkeypatch, fault):
    observed = []
    if fault == 'output':
        monkeypatch.setattr(renderer, 'MAX_OUTPUT', 512)
    elif fault == 'stderr':
        monkeypatch.setattr(renderer, 'MAX_STDERR', 128)
        child_control(monkeypatch, 'sys.stderr.write("x"*10000)\n')
    elif fault == 'utf8':
        child_control(monkeypatch, 'sys.stdout.buffer.write(b"\\xff")\n')
    else:
        child_control(monkeypatch, 'sys.stdout.write("(version 1)\\n(deny default)")\n')
    with pytest.raises(Rejected):
        invoke(render_request, observed)
    assert observed[-1]['state'] == 'reaped' and observed[-1]['outcome'] != 'success'
    assert 'profile_sha256' not in observed[-1]
    renderer.verify_receipt(observed[-1])


def test_source_drift_after_reaping_is_nonpassing_known_cleanup(render_request, monkeypatch):
    original, calls = renderer._sources, []
    def source(*args):
        calls.append(1)
        if len(calls) > 1:
            raise Rejected('native_renderer_source', 'SIMULATED source drift after wait')
        return original(*args)
    monkeypatch.setattr(renderer, '_sources', source)
    observed = []
    with pytest.raises(Rejected, match='source drift after wait'):
        invoke(render_request, observed)
    assert observed[-1]['state'] == 'reaped' and observed[-1]['outcome'] == 'returned'
    renderer.verify_receipt(observed[-1])


@pytest.mark.parametrize('operation', ['mkdir', 'write', 'fork', 'network'])
def test_fixed_child_audit_rejects_side_effect_regressions(render_request, tmp_path, monkeypatch, operation):
    original = renderer._sources
    target = tmp_path / 'forbidden-effect'
    expression = {'mkdir': f'os.mkdir({str(target)!r})', 'write': f'open({str(target)!r},"w")',
                  'fork': 'os.fork()', 'network': '__import__("socket").socket()'}[operation]
    def sources(*args):
        modules, hashes, component = original(None)
        modules['native_sandbox']['source'] += '\n' + expression + '\n'
        sha = hashlib.sha256(modules['native_sandbox']['source'].encode()).hexdigest()
        modules['native_sandbox']['sha256'] = hashes['aos/native_sandbox.py'] = component['aos/native_sandbox.py'] = sha
        return modules, hashes, component
    monkeypatch.setattr(renderer, '_sources', sources)
    observed = []
    with pytest.raises(Rejected, match='forbids'):
        invoke(render_request, observed)
    assert observed[-1]['outcome'] == 'rejected' and not target.exists()
    renderer.verify_receipt(observed[-1])


def test_wait_releases_parent_thread_for_observation(render_request, monkeypatch):
    child_control(monkeypatch, 'import time\nend=time.monotonic()+.4\nwhile time.monotonic()<end:sum(range(1000))\n'
                  'sys.stdout.write("(version 1)\\n(deny default)\\n")\n')
    stop, observations = threading.Event(), []
    def observe_thread():
        while not stop.wait(.02):
            observations.append(time.monotonic())
    observer = threading.Thread(target=observe_thread)
    observer.start()
    try:
        assert invoke(render_request, []).startswith(b'(version 1)\n')
    finally:
        stop.set(); observer.join(timeout=2)
    assert len(observations) >= 8
    assert max(b - a for a, b in zip(observations, observations[1:])) < .2


@pytest.mark.parametrize('field,value', [('owner_identity', {}), ('source_hashes', {}),
    ('component_source_digest', 'sha256:' + '0' * 64), ('output_validated', False), ('state', 'launch_ambiguous')])
def test_saved_success_requires_complete_bound_receipt(render_request, field, value):
    observed = []
    invoke(render_request, observed)
    damaged = copy.deepcopy(observed[-1]); damaged[field] = value
    with pytest.raises(Rejected):
        renderer.verify_receipt(damaged, require_success=True)
