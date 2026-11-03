"""Bounded native preparation diagnostics do not grant launch authority."""
import base64
import copy
import hashlib
import json
import subprocess

import pytest

from agentic_tools.aos import native_preparation
from agentic_tools.aos.store import Rejected, digest
from test_aos_native_preparation import prepared_native, run_check, saved


def recorded_probe(stdout, stderr, *, partial=False):
    return {'state': 'reaped', 'outcome': 'timeout' if partial else 'returned',
            'diagnostics': native_preparation._probe_diagnostics(stdout, stderr, partial=partial)}


@pytest.mark.parametrize(('exit_code', 'stdout'), [(65, 'not JSON'), (1, '{"access": false}')])
def test_failed_native_probe_diagnostics_survive_exact_saved_replay(prepared_native, monkeypatch, exit_code, stdout):
    p = prepared_native
    stderr = 'sandbox-exec: diagnostic fixture failed\n'

    def failed_probe(args, **kwargs):
        p.calls.append('probe')
        return subprocess.CompletedProcess(args, exit_code, stdout, stderr)

    monkeypatch.setattr(subprocess, 'run', failed_probe)
    with pytest.raises(Rejected, match='runtime_sandbox'):
        run_check(p)
    row = saved(p)
    proof = row['preparation_failure']['proof']
    assert proof['probe']['exit_code'] == exit_code
    assert proof['probe']['diagnostics'] == native_preparation._probe_diagnostics(stdout, stderr, partial=False)
    assert base64.b64decode(proof['probe']['diagnostics']['stderr']['tail_base64']) == stderr.encode()
    assert proof['supervisor_popen'] == 'not_called'
    assert row['phase'] == 'clean' and row['cleanup']['effects_reconciled'] is True
    with p.runtime._db() as db:
        assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
    assert native_preparation.verify(copy.deepcopy(row)) == row
    assert p.runtime._native_stop(row['check_id']) == row
    with pytest.raises(Rejected, match='native_preparation_failed'):
        run_check(p)
    assert p.calls == ['probe']

    # Historical absence remains valid; coherently rehashed malformed optional
    # data must be rejected independently of the outer receipt checksum.
    legacy = copy.deepcopy(row)
    legacy['preparation_failure']['proof']['probe'].pop('diagnostics')
    legacy['preparation_failure']['receipt_digest'] = digest([
        legacy['preparation_failure']['proof'], legacy['cleanup']])
    legacy_before = copy.deepcopy(legacy)
    assert native_preparation.verify(legacy) == legacy_before
    assert legacy == legacy_before
    damaged = copy.deepcopy(row)
    damaged['preparation_failure']['proof']['probe']['diagnostics']['stdout']['byte_count'] = True
    damaged['preparation_failure']['receipt_digest'] = digest([
        damaged['preparation_failure']['proof'], damaged['cleanup']])
    with pytest.raises(Rejected, match='diagnostics are malformed'):
        native_preparation.verify(damaged)


def test_timeout_retains_only_available_partial_capture(prepared_native, monkeypatch):
    p = prepared_native
    stdout, stderr = b'partial\xff output', b'compiler\xe2'

    def timeout(args, **kwargs):
        p.calls.append('probe')
        raise subprocess.TimeoutExpired(args, kwargs['timeout'], output=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        run_check(p)
    row = saved(p)
    probe = row['preparation_failure']['proof']['probe']
    assert probe['state'] == 'reaped' and probe['outcome'] == 'timeout' and probe['exit_code'] is None
    assert probe['diagnostics'] == native_preparation._probe_diagnostics(stdout, stderr, partial=True)
    assert probe['diagnostics']['stdout']['capture_type'] == 'bytes'
    assert native_preparation.verify(copy.deepcopy(row)) == row
    assert p.calls == ['probe'] and row['cleanup']['native_admission']['launched'] is False


def test_bounded_unicode_and_bytes_keep_full_capture_hashes():
    stdout, stderr = 'é\n' * 10000, bytes(range(256)) * 100
    probe = recorded_probe(stdout, stderr)
    native_preparation._verify_probe_diagnostics(probe)
    for key, data, kind in (('stdout', stdout.encode('utf-8'), 'text-utf8'), ('stderr', stderr, 'bytes')):
        stream = probe['diagnostics'][key]
        assert stream['capture_type'] == kind and stream['byte_count'] == len(data)
        assert stream['sha256'] == hashlib.sha256(data).hexdigest()
        assert base64.b64decode(stream['tail_base64']) == data[-4096:]
        assert stream['truncated'] is True
    assert len(json.dumps(probe)) < 12000


def test_empty_capture_and_unavailable_capture_are_distinct():
    probe = recorded_probe('', None, partial=True)
    native_preparation._verify_probe_diagnostics(probe)
    empty, absent = (probe['diagnostics'][key] for key in ('stdout', 'stderr'))
    assert empty['byte_count'] == 0 and empty['sha256'] == hashlib.sha256(b'').hexdigest()
    assert empty['capture_type'] == 'text-utf8'
    assert absent == {'capture_type': 'unavailable', 'byte_count': None, 'sha256': None,
                      'tail_base64': '', 'truncated': False}


@pytest.mark.parametrize('damage', [
    'format', 'partial_type', 'partial_outcome', 'extra', 'missing_stream', 'stream_extra',
    'capture_type', 'count_bool', 'count_negative', 'count_mismatch', 'hash', 'short_hash',
    'base64_invalid', 'base64_noncanonical', 'oversize_tail', 'truncated_type', 'truncated_value',
    'unavailable_count', 'ambiguous_state', 'not_started_state',
])
def test_present_diagnostics_must_be_well_formed_and_bound_to_reaped_outcome(damage):
    probe = recorded_probe('a', None)
    details, stream = probe['diagnostics'], probe['diagnostics']['stdout']
    if damage == 'format': details['format'] = 'unknown'
    elif damage == 'partial_type': details['partial'] = 0
    elif damage == 'partial_outcome': details['partial'] = True
    elif damage == 'extra': details['extra'] = True
    elif damage == 'missing_stream': details.pop('stderr')
    elif damage == 'stream_extra': stream['extra'] = True
    elif damage == 'capture_type': stream['capture_type'] = 'raw-os-bytes'
    elif damage == 'count_bool': stream['byte_count'] = True
    elif damage == 'count_negative': stream['byte_count'] = -1
    elif damage == 'count_mismatch': stream['byte_count'] = 2
    elif damage == 'hash': stream['sha256'] = 'f' * 64
    elif damage == 'short_hash': stream['sha256'] = 'a'
    elif damage == 'base64_invalid': stream['tail_base64'] = '!'
    elif damage == 'base64_noncanonical': stream['tail_base64'] = 'YR=='
    elif damage == 'oversize_tail':
        stream.update(byte_count=4097, tail_base64=base64.b64encode(b'a' * 4097).decode(), truncated=True)
    elif damage == 'truncated_type': stream['truncated'] = 0
    elif damage == 'truncated_value': stream['truncated'] = True
    elif damage == 'unavailable_count': details['stderr']['byte_count'] = 0
    elif damage == 'ambiguous_state': probe['state'] = 'launch_ambiguous'
    elif damage == 'not_started_state': probe['state'] = 'not_started'
    with pytest.raises(Rejected, match='diagnostics are malformed'):
        native_preparation._verify_probe_diagnostics(probe)


def test_optional_legacy_absence_does_not_change_exact_not_started_witness():
    witness = {'state': 'not_started'}
    native_preparation._verify_probe_diagnostics(witness)
    assert witness == {'state': 'not_started'}
    native_preparation._verify_probe_diagnostics({'state': 'reaped', 'outcome': 'returned'})


def test_probe_launch_uncertainty_still_has_no_completed_failure(prepared_native, monkeypatch):
    p = prepared_native

    def ambiguous(args, **kwargs):
        p.calls.append('probe')
        raise OSError('SIMULATED unknown permission-probe launch')

    monkeypatch.setattr(subprocess, 'run', ambiguous)
    with pytest.raises(Rejected, match='check_cleanup_unknown'):
        run_check(p)
    row = saved(p)
    assert row['phase'] == 'intent' and 'preparation_failure' not in row and 'cleanup' not in row
    assert p.calls == ['probe']
