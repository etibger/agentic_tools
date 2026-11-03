"""Detached native children are reconciled by a kernel sandbox census."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_processes
from agentic_tools.aos import native_sandbox
from agentic_tools.aos.codex_runtime import candidate, inventory, process_groups
from agentic_tools.aos.store import canonical
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import native_fixture


def detached_fixture(tmp_path, mode='complete', attempt_id=None):
    p = native_fixture(tmp_path, 'no-child', attempt_id=attempt_id)
    # The grandchild reparents before the test returns and no longer belongs to
    # the outer supervisor's process group. No marker or binary-name match is
    # needed to find it inside the same kernel sandbox instance.
    grandchild = ('import os,time\nfrom pathlib import Path\n'
                  f'Path({str(p.outputs / "detached.pid")!r}).write_text(str(os.getpid()))\n'
                  'time.sleep(30)\n')
    launcher = ('import subprocess,sys\n'
                f'subprocess.Popen([sys.executable,"-c",{grandchild!r}],start_new_session=True,'
                'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n')
    body = ('import subprocess,sys,time\nfrom pathlib import Path\n'
            'def test_detached():\n'
            f' subprocess.run([sys.executable,"-c",{launcher!r}],check=True)\n'
            f' target=Path({str(p.outputs / "detached.pid")!r})\n'
            ' until=time.monotonic()+3\n'
            ' while not target.exists() and time.monotonic()<until:time.sleep(.01)\n'
            ' assert target.exists()\n')
    if mode == 'timeout':
        body += ' time.sleep(30)\n'
        p.check['timeout_seconds'] = 1
    (p.source / 'test_native.py').write_text(body)
    p.recipe['immutable'] = inventory(p.source)
    p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.recipe['probe_interpreter'] = sys.executable
    p.frozen = candidate(p.source)
    return p


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
@pytest.mark.parametrize('mode,expected', [('complete', 'pass'), ('timeout', 'timeout')])
def test_native_census_stops_reparented_new_session_child(tmp_path, mode, expected):
    p = detached_fixture(tmp_path, mode)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == expected
        assert result['cleanup']['effects_reconciled']
        row = p.runtime._native_get(result['native_check_id'])
        proof = row['native_process_receipt']
        pid = int((p.outputs / 'detached.pid').read_text())
        assert any(m['pid'] == pid for m in proof['members'])
        assert proof['reconciled'] and proof['remaining'] == []
        native_processes.verify_absent(proof)
        assert row['owner']['pid'] not in process_groups()
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_native_census_worker_restart_reuses_cleanup_without_rerun(tmp_path):
    p = detached_fixture(tmp_path, attempt_id='census-restart')
    script = '''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from agentic_tools.aos.codex_runtime import CodexRuntime,candidate
r=CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=Path(sys.argv[1]))),{})
recipe=json.loads(sys.argv[2])
r._fault=lambda stage: os._exit(73) if stage=='native_finished_before_cleanup' else None
r._check(recipe,recipe['checks'][0],Path(recipe['outputs'])/'check','native-test',candidate(Path(recipe['source'])))
'''
    try:
        run = subprocess.run([sys.executable, '-c', script, str(p.state), canonical(p.recipe)],
                             capture_output=True, text=True, timeout=20)
        assert run.returncode == 73, run.stderr
        pid = int((p.outputs / 'detached.pid').read_text())
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass'
        assert int((p.outputs / 'detached.pid').read_text()) == pid
        with p.runtime._db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 1
        assert result['cleanup']['effects_reconciled']
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_native_census_cancel_preserves_unrelated_same_uid_process(tmp_path):
    p = detached_fixture(tmp_path, 'timeout')
    p.check['timeout_seconds'] = p.recipe['max_seconds'] = 20
    peer = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
    results, failures = [], []
    def check():
        try:
            results.append(p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen))
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=check)
    try:
        thread.start()
        deadline = time.monotonic() + 6
        while not (p.outputs / 'detached.pid').exists() and time.monotonic() < deadline:
            time.sleep(.025)
        assert (p.outputs / 'detached.pid').exists()
        with p.runtime._db() as db:
            check_id = db.execute('SELECT id FROM native_check').fetchone()[0]
        stopped = p.runtime._native_stop(check_id, cancel=True)
        thread.join(timeout=10)
        assert not thread.is_alive() and not failures
        assert stopped['cleanup']['effects_reconciled']
        assert results[0]['result'] == 'cancelled'
        assert peer.poll() is None
        assert all(m['pid'] != peer.pid for m in stopped['native_process_receipt']['members'])
    finally:
        peer.terminate()
        peer.wait(timeout=3)
        p.runtime.close()
        thread.join(timeout=10)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_native_census_cached_receipt_cannot_change_ownership_backend(tmp_path):
    p = detached_fixture(tmp_path)
    try:
        p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        del p.recipe['native_process_ownership']
        with pytest.raises(Rejected, match='different process ownership'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
def test_native_census_interruption_cannot_pass_with_successful_test_report(tmp_path):
    p = detached_fixture(tmp_path)
    report = p.outputs / 'check' / 'junit.xml'
    script = ('import os,signal\nfrom pathlib import Path\n'
              f'Path({str(report)!r}).write_text(\'<testsuite><testcase name="fake"/></testsuite>\')\n'
              'os.kill(os.getppid(),signal.SIGTERM)\nos._exit(0)\n')
    p.check['argv'] = [sys.executable, '-c', script]
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['passed'] == 1
        assert result['result'] == 'blocked'
        assert result['cleanup']['process_ownership']['interrupted']
    finally:
        p.runtime.close()


def test_receipt_rejects_live_exact_identity_and_incomplete_evidence(monkeypatch):
    saved = {'pid': 123, 'uid': 501, 'birth': [1, 2], 'status': 2}
    proof = {'backend': native_processes.BACKEND, 'observer': saved, 'members': [],
             'reconciled': True, 'boundary_verified': True, 'remaining': [], 'errors': []}
    monkeypatch.setattr(native_processes, 'identity', lambda pid: dict(saved))
    with pytest.raises(RuntimeError, match='remains alive'):
        native_processes.verify_absent(proof)
    monkeypatch.setattr(native_processes, 'identity', lambda pid: {**saved, 'birth': [3, 4]})
    native_processes.verify_absent(proof)  # reused unrelated PID is never signalled
    with pytest.raises(RuntimeError, match='complete'):
        native_processes.verify_absent({**proof, 'reconciled': False})


@pytest.mark.parametrize('reused', [True, False])
def test_native_census_never_signals_reused_or_unproved_wrapper_pid(tmp_path, monkeypatch, reused):
    p = native_fixture(tmp_path, 'no-child')
    status = p.state / 'status.json'
    status.write_text('{"census":{}}')
    saved = {'pid': 123, 'uid': 501, 'birth': [1, 1], 'group': 123}
    row = {'check_id': 'test-census-reuse', 'status': str(status), 'ownership_backend': native_processes.BACKEND,
           'owner': {'pid': 123, 'started': 'same-second', 'native_identity': saved}, 'members': {}}
    monkeypatch.setattr(native_processes, 'verify_absent', lambda receipt: None)
    monkeypatch.setattr(native_processes, 'identity', lambda pid: {**saved, 'birth': [1, 2] if reused else [1, 1]})
    snapshot = {123: {'parent': 1, 'group': 123, 'started': 'same-second'}}
    if not reused:
        snapshot[456] = {'parent': 1, 'group': 123, 'started': 'same-second'}
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', lambda: snapshot)
    signals = []
    monkeypatch.setattr('agentic_tools.aos.codex_runtime.os.kill', lambda *args: signals.append(args))
    try:
        with pytest.raises(Rejected, match='identity changed|Unexpected wrapper'):
            p.runtime._native_members(row)
        assert signals == []
    finally:
        p.runtime.close()


def test_native_census_child_permission_denial_cannot_claim_empty_cleanup(tmp_path, monkeypatch):
    own_pid, outside_pid, child_pid = os.getpid(), 919191, 919192
    rows = {pid: {'pid': pid, 'uid': os.getuid(), 'birth': [1, pid], 'status': 2,
                  'parent': outside_pid if pid == own_pid else own_pid, 'group': own_pid}
            for pid in (own_pid, outside_pid, child_pid)}
    monkeypatch.setattr(native_processes, 'identity', lambda pid: rows.get(pid))
    monkeypatch.setattr(native_processes, 'all_pids', lambda: list(rows))
    def signal_zero(pid, sig):
        if pid != own_pid:
            raise PermissionError('deliberate child-denying profile')
    monkeypatch.setattr(native_processes.os, 'kill', signal_zero)
    monkeypatch.setattr(native_processes.os, 'set_blocking', lambda *args: None)
    monkeypatch.setattr(native_processes.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(native_processes.sys, 'stdin', SimpleNamespace(fileno=lambda: 0))
    class FakeChild:
        pid = child_pid
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(native_processes.subprocess, 'Popen', lambda *args, **kwargs: FakeChild())
    payload = {'outside': rows[outside_pid], 'command': ['unused'], 'cwd': str(tmp_path),
               'stdout': str(tmp_path / 'stdout'), 'stderr': str(tmp_path / 'stderr'), 'timeout_seconds': 1}
    proof = native_processes._inner(payload)
    assert proof['remaining'] == []
    assert not proof['boundary_verified'] and not proof['reconciled']
    assert any('PermissionError' in item for item in proof['errors'])


@pytest.mark.parametrize('stage', ['signal', 'identity', 'identity-error', 'zombie'])
def test_native_census_disappearing_parent_is_incomplete_not_empty(monkeypatch, stage):
    pid = 919191
    monkeypatch.setattr(native_processes, 'all_pids', lambda: [pid])
    def check(pid, sig):
        if stage == 'signal':
            raise ProcessLookupError('parent forked then exited during census')
    monkeypatch.setattr(native_processes.os, 'kill', check)
    def lookup(pid):
        if stage == 'identity-error':
            raise PermissionError('transient exec metadata transition')
        return None if stage == 'identity' else {'pid': pid, 'uid': os.getuid(), 'birth': [1, 2], 'status': 5}
    monkeypatch.setattr(native_processes, 'identity', lookup)
    members, complete = native_processes.census()
    assert members == [] and not complete


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
@pytest.mark.parametrize('census_backend', [False, True])
def test_trusted_supervisors_ignore_product_python_module_shadows(tmp_path, census_backend):
    p = native_fixture(tmp_path, 'no-child')
    for name in ('json', 'subprocess', 'ctypes', 'pathlib', 'selectors'):
        (p.source / (name + '.py')).write_text('raise RuntimeError("product code hijacked trusted supervisor")\n')
    report = p.outputs / 'check' / 'junit.xml'
    p.check['argv'] = [sys.executable, '-I', '-c',
                       f'from pathlib import Path;Path({str(report)!r}).write_text(\'<testsuite><testcase name="isolated"/></testsuite>\')']
    p.recipe['immutable'] = inventory(p.source)
    p.recipe['probe_interpreter'] = sys.executable
    if census_backend:
        p.recipe['native_process_ownership'] = native_processes.BACKEND
    p.frozen = candidate(p.source)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass' and result['cleanup']['effects_reconciled']
    finally:
        p.runtime.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel sandbox route')
@pytest.mark.parametrize('ephemeral_tcp', [False, True])
def test_dedicated_native_profile_has_private_permission_receipt_and_census(tmp_path, ephemeral_tcp):
    p = detached_fixture(tmp_path)
    p.recipe['native_sandbox'] = {
        'version': native_sandbox.VERSION, 'unix_socket_roots': [str(p.outputs)],
        'temporary_paths': [{'parent': '/private/tmp', 'name': name, 'kind': 'file'}
                            for name in ('{token8}', 'tmp{token8}')]}
    if ephemeral_tcp:
        observed = subprocess.run(['/usr/sbin/sysctl', '-n', 'net.inet.ip.portrange.first',
                                   'net.inet.ip.portrange.last'], check=True, capture_output=True, text=True)
        first, last = map(int, observed.stdout.split())
        p.recipe['native_sandbox']['ephemeral_tcp'] = {'first': first, 'last': last}
        p.recipe['max_seconds'] = p.check['timeout_seconds'] = 30
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert result['result'] == 'pass' and result['cleanup']['effects_reconciled']
        row = p.runtime._native_get(result['native_check_id'])
        assert row['native_sandbox_boundary']['sha256']
        assert row['native_path_probe']['checks']['ephemeral_tcp' if ephemeral_tcp else 'network_denied']
        if ephemeral_tcp:
            observed = row['native_sandbox_boundary']['network_observation']
            assert observed['first'] == first and observed['last'] == last
            assert observed['allowed_port_count'] > 0
        assert row['native_path_probe']['checks']['state_read_denied']
        assert row['native_path_probe']['checks']['immutable_hardlink_denied']
        assert row['cleanup']['native_temporary_effects']['preexisting_preserved']
        profile = Path(row['native_sandbox_boundary']['path'])
        profile.write_text(profile.read_text() + '\n')
        with pytest.raises(Rejected, match='profile changed'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('current_kind, prior_kind', [('directory', 'directory'), ('directory', 'template'), ('template', 'directory')])
def test_native_directory_create_scope_conflicts_with_unresolved_namespace(tmp_path, monkeypatch, current_kind, prior_kind):
    p = detached_fixture(tmp_path)
    namespace = tmp_path / 'native-temporary'
    namespace.mkdir()
    def contract(kind):
        return {'version': native_sandbox.VERSION, **(
            {'directory_create_paths': [str(namespace / 'native-owned')]} if kind == 'directory'
            else {'temporary_paths': [{'parent': str(namespace), 'name': 'native-{pid}', 'kind': 'tree'}]})}
    p.recipe['native_sandbox'] = contract(current_kind)
    row = {'check_id': 'nc-unresolved-directory', 'attempt_id': None, 'phase': 'cleanup_unknown',
           'source': str(p.source), 'outputs_root': str(p.outputs), 'output': str(p.outputs / 'prior-check'),
           'native_sandbox_boundary': {'contract': contract(prior_kind)}}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *_a, **_k: pytest.fail('probe before ownership'))
    try:
        with pytest.raises(Rejected, match='temporary namespace'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert not (p.outputs / 'check').exists()
    finally:
        p.runtime.close()
