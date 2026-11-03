"""Simulated initial census failures; no OS observer, process or payload runs."""
import json
import os
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_processes


@pytest.fixture
def initial_census(monkeypatch):
    own_pid, ancestor_pid, outside_pid, unexpected_pid = os.getpid(), 910001, 910002, 910003
    parent = {own_pid: ancestor_pid, ancestor_pid: outside_pid, unexpected_pid: outside_pid}
    rows = {pid: {'pid': pid, 'uid': os.getuid(), 'birth': [1, pid],
                  'parent': parent[pid], 'group': own_pid, 'status': 2}
            for pid in parent}
    clock = SimpleNamespace(now=100.0, sleeps=[])
    signals = []
    def signal(pid, number):
        signals.append((pid, number))
        assert number == 0 and pid in (own_pid, ancestor_pid, outside_pid)
        if pid == outside_pid:
            raise PermissionError('simulated outside boundary')
    def sleep(seconds):
        assert seconds == .025
        clock.sleeps.append(seconds)
        clock.now = round(clock.now + seconds, 3)
    def forbidden(*args, **kwargs):
        pytest.fail('Initial refusal reached payload setup or execution')
    monkeypatch.setattr(native_processes, 'identity', lambda pid: rows[pid])
    monkeypatch.setattr(native_processes.os, 'kill', signal)
    monkeypatch.setattr(native_processes.os, 'set_blocking', forbidden)
    monkeypatch.setattr(native_processes.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(native_processes, 'open', forbidden, raising=False)
    monkeypatch.setattr(native_processes.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(native_processes.time, 'sleep', sleep)
    return SimpleNamespace(own=rows[own_pid], ancestor=rows[ancestor_pid],
        unexpected=rows[unexpected_pid], outside_pid=outside_pid, clock=clock, signals=signals,
        payload={'outside': {'pid': outside_pid}})


def assert_refusal(proof, fixture):
    assert proof['backend'] == native_processes.BACKEND
    assert proof['reconciled'] is False and proof['payload_started'] is False
    assert proof['observer'] == fixture.own
    assert proof['errors'] == ['RuntimeError: Native sandbox was not empty before command admission']
    assert proof['initial_census']['ancestors'] == [fixture.ancestor]
    assert json.loads(json.dumps(proof)) == proof
    with pytest.raises(RuntimeError, match='complete kernel sandbox cleanup receipt'):
        native_processes.verify_absent(proof)
    assert fixture.signals == [(fixture.own['pid'], 0), (fixture.outside_pid, 0),
                               (fixture.ancestor['pid'], 0)]


@pytest.mark.parametrize('complete', [True, False])
def test_initial_census_nonempty_retains_exact_rows_without_starting_payload(initial_census, monkeypatch, complete):
    fixture = initial_census
    monkeypatch.setattr(native_processes, 'census', lambda: ([fixture.ancestor, fixture.unexpected], complete))
    proof = native_processes._inner(fixture.payload)
    assert_refusal(proof, fixture)
    assert proof['initial_census'] == {
        'classification': 'unexpected_members', 'ancestors': [fixture.ancestor],
        'last_rows': [fixture.unexpected], 'complete': complete, 'scan_count': 1,
        'elapsed_seconds': 0.0}
    assert fixture.clock.sleeps == []


def test_initial_census_incomplete_retains_original_half_second_limit(initial_census, monkeypatch):
    fixture = initial_census
    scans = []
    def incomplete():
        scans.append(fixture.clock.now)
        return [fixture.ancestor], False
    monkeypatch.setattr(native_processes, 'census', incomplete)
    proof = native_processes._inner(fixture.payload)
    assert_refusal(proof, fixture)
    assert proof['initial_census'] == {
        'classification': 'incomplete_census', 'ancestors': [fixture.ancestor],
        'last_rows': [], 'complete': False, 'scan_count': 21, 'elapsed_seconds': .5}
    assert len(scans) == 21 and fixture.clock.sleeps == [.025] * 20


def test_initial_census_transient_incomplete_still_allows_existing_admission(initial_census, monkeypatch):
    fixture = initial_census
    observations = iter([([fixture.ancestor], False), ([fixture.ancestor], True)])
    monkeypatch.setattr(native_processes, 'census', lambda: next(observations))
    class Admitted(Exception):
        pass
    def admitted(*args):
        raise Admitted
    monkeypatch.setattr(native_processes.os, 'set_blocking', admitted)
    monkeypatch.setattr(native_processes.sys, 'stdin', SimpleNamespace(fileno=lambda: 0))
    with pytest.raises(Admitted):
        native_processes._inner(fixture.payload)
    assert fixture.clock.sleeps == [.025]
