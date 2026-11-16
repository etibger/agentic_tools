"""Actual macOS reviewed-check consumer with synthetic assessment authority.

No model or human decision is simulated into an approval claim: the completed
assessment record is a test fixture. Native dispatch, environment, census,
command, journals, typed adapter, cleanup and replay use production functions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from agentic_tools.aos import reviewed_host as host
from agentic_tools.aos.store import digest
from test_aos_reviewed_host import case

_RUN = host.owned_host_command.run_owned
_VERIFY = host.owned_host_command.verify_completed
_RECONCILE = host.owned_host_command.reconcile_owned
_ENVIRONMENT = host.checkout_boundary.environment
_SCOPED = host.checkout_boundary.scoped_outputs

pytestmark = [pytest.mark.skipif(sys.platform != 'darwin', reason='macOS reviewed-host ownership route'),
              pytest.mark.parametrize('case', [False], indirect=True)]


@pytest.fixture
def native(case, monkeypatch):
    p = case
    for name, function in [('run_owned', _RUN), ('verify_completed', _VERIFY),
                           ('reconcile_owned', _RECONCILE)]:
        monkeypatch.setattr(host.owned_host_command, name, function)
    monkeypatch.setattr(host.checkout_boundary, 'environment', _ENVIRONMENT)
    monkeypatch.setattr(host.checkout_boundary, 'scoped_outputs', _SCOPED)
    for recipe in (p.base, p.recipe):
        recipe['checkout_policy'].update(change_roots=['main.rs'], endpoint_limits={
            'minimum_free_bytes': 1, 'max_owned_growth_bytes': 1024 * 1024,
            'max_output_bytes': 1024 * 1024})
    yield p
    p.runtime.close()


def bind_command(p, argv):
    p.descriptor['argv'] = list(argv)
    record = p.runtime._get(p.record['attempt_id'])
    record['recipe_digest'] = digest(p.base)
    record['reviewed_host_gate'] = {
        'mode': 'reviewed-host-v1', 'candidate': p.frozen, 'recipe_digest': digest(p.base),
        'checks': copy.deepcopy(p.base['checks']),
        'review': {'path': str(p.report), 'sha256': hashlib.sha256(p.report.read_bytes()).hexdigest()},
        'adapters': p.runtime._bindings(record['assignment'], p.base)}
    p.runtime._save(record)
    scoped = copy.deepcopy(p.base)
    scoped['checkout_policy']['protected_roots'] = [str(p.runtime.directory.resolve())]
    p.recipe = _SCOPED(scoped, Path(record['run_outputs']))
    p.recipe.update(_attempt_id=record['attempt_id'],
                    _adapter_bindings=p.runtime._bindings(record['assignment'], p.base))


def check(p):
    return p.runtime._check(p.recipe, p.descriptor, p.output, 'reviewer', p.frozen)


def assert_owned_cleanup(p, result):
    row = p.runtime._native_get(result['native_check_id'])
    assert row['ownership_backend'] == 'host-owned-v1' and row['phase'] == 'clean'
    proof = row['host_owner_receipt']
    assert proof['launch_authorized'] and proof['cleanup_verified']
    assert proof['owner']['pid'] != os.getpid()
    assert result['cleanup']['owned_processes'] == []
    assert result['cleanup']['effects_reconciled']
    assert result['cleanup']['partial_mutations']['changes'] == []
    assert host.owned_host_command.verify_completed(proof) == proof
    return row


def test_actual_reviewed_check_preserves_normal_temp_and_reuses_receipt(native):
    p = native
    body = ('import json,os,tempfile\n'
            'from pathlib import Path\n'
            'with tempfile.TemporaryDirectory() as root:\n'
            ' item=Path(root)/"owned.txt";item.write_text("synthetic")\n'
            ' print(json.dumps({"temp":str(Path(root).parent.resolve()),'
            '"tmpdir":os.environ["TMPDIR"],"payload":item.read_text()}))\n')
    bind_command(p, [sys.executable, '-I', '-c', body])
    result = check(p)
    assert result['result'] == 'pass'
    row = assert_owned_cleanup(p, result)
    output = json.loads(Path(row['host_control'], 'stdout.log').read_text())
    assert output['tmpdir'] == (os.environ.get('TMPDIR') or tempfile.gettempdir())
    assert output['temp'] == str(Path(output['tmpdir']).resolve())
    assert output['payload'] == 'synthetic'
    before = Path(row['host_control'], 'progress.json').read_bytes()
    assert check(p) == result
    assert Path(row['host_control'], 'progress.json').read_bytes() == before
    with p.runtime._db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_check').fetchone()[0] == 1


def test_actual_nonzero_exit_remains_failed_with_verified_cleanup(native):
    p = native
    bind_command(p, [sys.executable, '-I', '-c', 'print("synthetic failed build");raise SystemExit(7)'])
    result = check(p)
    assert result['result'] == 'fail' and result['exit_code'] == 7, result
    assert_owned_cleanup(p, result)
    assert check(p) == result


def test_actual_cancel_preserves_unrelated_owned_peer(native):
    p = native
    marker = p.output.parent / 'payload-started'
    body = ('import time\nfrom pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("started")\n'
            'time.sleep(20)\n')
    bind_command(p, [sys.executable, '-I', '-c', body])
    peer = subprocess.Popen([sys.executable, '-I', '-c', 'import time;time.sleep(30)'],
                            start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results, errors = [], []
    def execute():
        try:
            results.append(check(p))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=execute)
    try:
        thread.start()
        end = time.monotonic() + 10
        while not marker.exists() and thread.is_alive() and time.monotonic() < end:
            time.sleep(.02)
        assert marker.exists(), errors
        record = p.runtime._get(p.record['attempt_id'])
        record['native_cancel_requested'] = True
        p.runtime._save(record)
        thread.join(timeout=20)
        assert not thread.is_alive() and not errors
        assert results[0]['result'] == 'cancelled'
        row = assert_owned_cleanup(p, results[0])
        assert peer.poll() is None
        assert str(peer.pid) not in row['host_owner_receipt']['owned_remaining']
    finally:
        if peer.poll() is None:
            peer.terminate()
        peer.wait(timeout=3)
        thread.join(timeout=20)
