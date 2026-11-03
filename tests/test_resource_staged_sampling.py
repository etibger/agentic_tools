"""Slow observations never monopolize the journal or erase a peer's evidence."""
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agentic_tools.aos import resource_limits as limits
from agentic_tools.aos.store import Rejected
from test_aos_resource_limits import guarded


def activate(g):
    with g.db:
        return limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True)


@pytest.mark.parametrize('stage', ['observe', 'encode'])
@pytest.mark.parametrize('api', ['connection', 'file'])
def test_other_writers_can_commit_during_observation_and_encoding(guarded, monkeypatch, stage, api):
    g = guarded
    activate(g)
    reached = []
    def write_independent_record():
        with sqlite3.connect(g.path, timeout=0) as peer:
            peer.execute('BEGIN IMMEDIATE')
            peer.execute('CREATE TABLE IF NOT EXISTS independent_writes (value INTEGER)')
            peer.execute('INSERT INTO independent_writes VALUES(1)')
        reached.append(True)
    if stage == 'observe':
        original = limits.observe
        def observed(bound):
            write_independent_record()
            return original(bound)
        monkeypatch.setattr(limits, 'observe', observed)
    else:
        original = limits.canonical
        def encoded(record):
            if isinstance(record, dict) and 'baseline' in record and 'high_water_growth_bytes' in record:
                write_independent_record()
            return original(record)
        monkeypatch.setattr(limits, 'canonical', encoded)
    if api == 'file':
        report = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    else:
        with g.db:
            report = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
    assert reached and report['breach'] is None


@pytest.mark.parametrize('peer_breach', [False, True])
def test_cas_merge_keeps_transient_overrun_and_peer_latest_observation(guarded, monkeypatch, peer_breach):
    g = guarded
    activate(g)
    oversized = g.target / 'transient-overrun'
    oversized.write_bytes(b'x' * 11)
    original, observations, peer_records = limits.observe, [], []
    def observed(bound):
        observations.append(True)
        pending = original(bound)
        oversized.unlink()
        with sqlite3.connect(g.path) as peer:
            current = limits._read(peer, 'WORK')
            current['last_observation'] = original(bound)
            current['high_water_growth_bytes']['output_roots'] = 7
            if peer_breach:
                current['breach'] = {'reason': 'peer-first-breach',
                                     'observed_at': current['last_observation']['observed_at']}
            limits._save(peer, 'WORK', current)
            peer_records.append(current)
        return pending
    monkeypatch.setattr(limits, 'observe', observed)
    report = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    retained = limits._read(g.db, 'WORK')
    assert observations == [True]
    assert report['high_water_growth_bytes']['output_roots'] == 11
    assert report['breach']['reason'] == ('peer-first-breach' if peer_breach else 'max_output_growth_bytes')
    assert retained['last_observation'] == peer_records[0]['last_observation']
    assert retained['baseline'] == g.baseline['baseline']


@pytest.mark.parametrize('change', ['owner-aba', 'baseline', 'scope'])
def test_changed_observation_origin_cannot_be_overwritten(guarded, monkeypatch, change):
    g = guarded
    activate(g)
    (g.target / 'transient-overrun').write_bytes(b'x' * 11)
    original, peer_records = limits.observe, []
    def observed(bound):
        pending = original(bound)
        with sqlite3.connect(g.path) as peer:
            current = limits._read(peer, 'WORK')
            if change == 'owner-aba':
                # A completed finish/reactivation has the same final owner.
                current['owner_generation'] += 2
            elif change == 'baseline':
                current['baseline']['files']['output_roots']['unexpected-baseline-entry'] = 0
            else:
                current['scope']['min_free_bytes'] += 1
            limits._save(peer, 'WORK', current)
            peer_records.append(copy.deepcopy(current))
        return pending
    monkeypatch.setattr(limits, 'observe', observed)
    with pytest.raises(Rejected, match='baseline|scope|ownership'):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    assert limits._read(g.db, 'WORK') == peer_records[0]
    failure = limits._read_failure(g.db, 'WORK', g.bound)
    assert failure['high_water_growth_bytes']['output_roots'] == 11
    assert failure['breach']['reason'] == 'max_output_growth_bytes'


def test_connection_api_preserves_caller_owned_transaction_rollback(guarded):
    g = guarded
    before = limits._read(g.db, 'WORK')
    g.db.execute('BEGIN IMMEDIATE')
    pending = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True)
    assert g.db.in_transaction and pending['active_attempt'] == 'a1'
    with sqlite3.connect(g.path) as peer:
        assert limits._read(peer, 'WORK') == before
    g.db.rollback()
    assert limits._read(g.db, 'WORK') == before


def test_legacy_generation_counts_both_owner_transitions(guarded):
    g = guarded
    with g.db:
        old = limits._read(g.db, 'WORK')
        old.pop('owner_generation')
        limits._save(g.db, 'WORK', old)
    with g.db:
        finished = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True, finish=True)
    assert finished['owner_generation'] == 2 and finished['active_attempt'] is None
    with g.db:
        active = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True)
    with g.db:
        repeated = limits.sample(g.db, 'WORK', g.bound, 'a1', force=True, activate=True)
    assert active['owner_generation'] == repeated['owner_generation'] == 3


@pytest.mark.parametrize('generation', [None, True, -1, '1'])
def test_invalid_owner_generation_cannot_be_sampled(guarded, generation):
    g = guarded
    with g.db:
        record = limits._read(g.db, 'WORK')
        record['owner_generation'] = generation
        limits._save(g.db, 'WORK', record)
    with pytest.raises(Rejected, match='generation'):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)


@pytest.mark.parametrize('api', ['file', 'caller-transaction'])
def test_encoding_bound_retains_the_observed_lower_bound(guarded, monkeypatch, api):
    g = guarded
    original_raw = g.db.execute('SELECT record FROM resource_limit WHERE work_id=?', ('WORK',)).fetchone()[0]
    (g.target / ('new-' + 'x' * 180)).write_bytes(b'x')
    monkeypatch.setattr(limits, 'MAX_RECORD_BYTES', len(original_raw.encode()) + 80)
    if api == 'caller-transaction':
        g.db.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(Rejected, match='bounded size'):
            if api == 'file':
                limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
            else:
                limits.sample(g.db, 'WORK', g.bound, 'a1', force=True)
    finally:
        if api == 'caller-transaction':
            assert g.db.in_transaction
            g.db.rollback()
    failure = json.loads(limits._failure_path(g.path, 'WORK').read_text())
    assert failure['high_water_growth_bytes']['output_roots'] == 1
    assert g.db.execute('SELECT record FROM resource_limit WHERE work_id=?', ('WORK',)).fetchone()[0] == original_raw


def test_wrong_observer_does_not_latch_failure_before_any_observation(guarded):
    g = guarded
    activate(g)
    with pytest.raises(Rejected, match='Another unresolved'):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'wrong-owner', force=True)
    assert not limits._failure_path(g.path, 'WORK').exists()


@pytest.mark.parametrize('failure', ['expired-after-rebase', 'expired-after-busy-read', 'sidecar-read-error'])
def test_rebase_delay_or_error_retains_pending_observation(guarded, monkeypatch, failure):
    g = guarded
    activate(g)
    (g.target / 'observed-overrun').write_bytes(b'x' * 11)
    clock = [time.time()]
    deadline = clock[0] + 1
    original, rebased, failure_reads = limits.observe, limits._rebase_sample, []
    snapshot, snapshots = limits._snapshot, []
    read_failure = limits._read_failure
    def observed(bound):
        result = original(bound)
        with sqlite3.connect(g.path) as peer:
            current = limits._read(peer, 'WORK')
            current['high_water_growth_bytes']['source_evidence_roots'] = 1
            limits._save(peer, 'WORK', current)
        if failure == 'expired-after-busy-read':
            clock[0] += 2
        return result
    def busy_read(*args, **kwargs):
        snapshots.append(True)
        if len(snapshots) == 1:
            error = sqlite3.OperationalError('synthetic initial read contention')
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        return snapshot(*args, **kwargs)
    def rebase(*args, **kwargs):
        rebased(*args, **kwargs)
        clock[0] += 2
    def sidecar(*args, **kwargs):
        failure_reads.append(True)
        if len(failure_reads) == 2:
            raise OSError('synthetic failure reading durable failure evidence')
        return read_failure(*args, **kwargs)
    monkeypatch.setattr(limits, 'observe', observed)
    monkeypatch.setattr(limits.time, 'time', lambda: clock[0])
    if failure == 'expired-after-rebase':
        monkeypatch.setattr(limits, '_rebase_sample', rebase)
    elif failure == 'sidecar-read-error':
        monkeypatch.setattr(limits, '_read_failure', sidecar)
    else:
        monkeypatch.setattr(limits, '_snapshot', busy_read)
    with pytest.raises((Rejected, OSError)):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True, deadline=deadline,
                           last_observed_at=clock[0])
    assert limits._read(g.db, 'WORK')['high_water_growth_bytes']['output_roots'] == 0
    retained = json.loads(limits._failure_path(g.path, 'WORK').read_text())
    assert retained['high_water_growth_bytes']['output_roots'] == 11
    assert retained['breach']['reason'] == 'max_output_growth_bytes'


@pytest.fixture
def staged_peer_clock(tmp_path, monkeypatch):
    """Synthetic observations and real local SQLite; no resource tree is scanned."""
    clock = [109.0]
    monkeypatch.setattr(limits.time, 'time', lambda: clock[0])
    monkeypatch.setattr(limits.time, 'monotonic', lambda: clock[0])
    source, output = str(tmp_path / 'source'), str(tmp_path / 'output')
    bound = {'source': source, 'output_roots': [output], 'source_evidence_roots': [source],
             'generated_roots': [], 'temporary_paths': [], 'sample_interval_seconds': 2,
             'scan_seconds': 10, 'max_observation_gap_seconds': 14,
             'max_output_growth_bytes': 10, 'max_source_evidence_growth_bytes': 10,
             'min_free_bytes': 1}
    baseline = {'identities': {source: {'device': 1, 'inode': 2, 'uid': 3},
                              output: {'device': 1, 'inode': 4, 'uid': 3}},
                'files': {'output_roots': {}, 'source_evidence_roots': {}},
                'available_bytes': {'1': 1000}, 'observed_at': 100.0}
    initial = {'scope': bound, 'scope_digest': limits.digest(bound), 'baseline': baseline,
               'last_observation': copy.deepcopy(baseline), 'active_attempt': 'a1',
               'owner_generation': 1, 'breach': None,
               'high_water_growth_bytes': {'output_roots': 0, 'source_evidence_roots': 0}}
    path = tmp_path / 'staged-peer.sqlite3'
    db = sqlite3.connect(path)
    limits.initialize(db)
    with db:
        limits._save(db, 'WORK', initial)
    case = SimpleNamespace(path=path, db=db, bound=bound, initial=copy.deepcopy(initial),
                           clock=clock, peer=True, peer_breach=None, observations=[],
                           persisted=[], rebases=[], persist_at=115.0, rebase_at=None)

    def observe(_bound):
        case.observations.append(clock[0])
        observed = copy.deepcopy(baseline)
        observed.update(observed_at=clock[0], files={
            'output_roots': {output + '/pending': 3},
            'source_evidence_roots': {source + '/pending': 5}})
        if case.peer:
            # The peer's healthy committed observation follows A's scan and is
            # newer than A's retained timestamp100. Its preceding gap is10s.
            clock[0] = 110.0
            peer = sqlite3.connect(path)
            try:
                with peer:
                    current = limits._read(peer, 'WORK')
                    current['last_observation'] = copy.deepcopy(observed)
                    current['last_observation'].update(observed_at=110.0, files={
                        'output_roots': {output + '/peer': 7},
                        'source_evidence_roots': {source + '/peer': 2}})
                    current['high_water_growth_bytes'] = {
                        'output_roots': 7, 'source_evidence_roots': 2}
                    current['breach'] = copy.deepcopy(case.peer_breach)
                    limits._save(peer, 'WORK', current)
            finally:
                peer.close()
        return observed

    persist, rebase = limits._persist_sample, limits._rebase_sample

    def timed_persist(*args, **kwargs):
        # Advance only time: both the failed first CAS and successful second
        # CAS use the real implementation and exact serialized row comparison.
        if case.persisted:
            clock[0] = case.persist_at
        matched = persist(*args, **kwargs)
        case.persisted.append(matched)
        return matched

    def timed_rebase(*args, **kwargs):
        rebase(*args, **kwargs)
        case.rebases.append(True)
        if case.rebase_at is not None:
            clock[0] = case.rebase_at

    monkeypatch.setattr(limits, 'observe', observe)
    monkeypatch.setattr(limits, '_persist_sample', timed_persist)
    monkeypatch.setattr(limits, '_rebase_sample', timed_rebase)
    try:
        yield case
    finally:
        db.close()


def test_healthy_committed_peer_prevents_stale_snapshot_gap(staged_peer_clock):
    """A generic concurrency regression, not evidence of a particular product failure."""
    g = staged_peer_clock
    started = g.clock[0]
    report = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    retained = limits._read(g.db, 'WORK')
    assert g.clock[0] - started == 6 < g.bound['max_observation_gap_seconds']
    assert g.persisted == [False, True] and g.rebases == [True]
    assert g.observations == [109.0]  # Rebase must not repeat the observation.
    assert report['breach'] is None
    assert retained['last_observation']['observed_at'] == 110.0
    assert retained['high_water_growth_bytes'] == {'output_roots': 7, 'source_evidence_roots': 5}
    assert retained['baseline'] == g.initial['baseline']
    assert retained['scope'] == g.bound
    assert retained['active_attempt'] == 'a1' and retained['owner_generation'] == 1
    assert not limits._failure_path(g.path, 'WORK').exists()


def test_true_active_gap_still_fails_with_fresh_operation_budget(staged_peer_clock):
    g = staged_peer_clock
    g.peer = False
    g.clock[0] = 115.0
    report = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    assert g.persisted == [True] and g.rebases == []
    assert report['breach'] == {'reason': 'active_observation_gap', 'observed_at': 115.0}
    assert limits._read(g.db, 'WORK')['breach'] == report['breach']
    with pytest.raises(Rejected, match='active_observation_gap'):
        limits.enforce(report)


def test_committed_peer_breach_and_both_high_waters_survive_rebase(staged_peer_clock):
    g = staged_peer_clock
    g.peer_breach = {'reason': 'peer-retained-failure', 'observed_at': 110.0}
    report = limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True)
    assert g.persisted == [False, True] and g.rebases == [True]
    assert report['breach'] == g.peer_breach
    assert report['high_water_growth_bytes'] == {'output_roots': 7, 'source_evidence_roots': 5}
    assert limits._read(g.db, 'WORK')['breach'] == g.peer_breach
    with pytest.raises(Rejected, match='peer-retained-failure'):
        limits.enforce(report)


@pytest.mark.parametrize('deadline_kind', ['observation-operation', 'job'])
def test_healthy_peer_cannot_extend_original_deadline(staged_peer_clock, deadline_kind):
    g = staged_peer_clock
    # At124 the operation started at109 has exhausted its own14s window,
    # although the peer observation at110 is still exactly14s old. The job
    # case expires at112 and resumes at113: even the old100 timestamp is
    # within14s, so only the explicit job deadline can reject that case.
    g.rebase_at = 124.0 if deadline_kind == 'observation-operation' else 113.0
    deadline = None if deadline_kind == 'observation-operation' else 112.0
    with pytest.raises(Rejected, match='original observation window'):
        limits.sample_file(str(g.path), 'WORK', g.bound, 'a1', force=True, deadline=deadline)
    retained = limits._read(g.db, 'WORK')
    assert g.persisted == [False] and g.rebases == [True]
    assert g.observations == [109.0]
    assert retained['breach'] is None and retained['last_observation']['observed_at'] == 110.0
    assert retained['high_water_growth_bytes'] == {'output_roots': 7, 'source_evidence_roots': 2}
    failure = json.loads(limits._failure_path(g.path, 'WORK').read_text())
    assert failure['high_water_growth_bytes'] == {'output_roots': 7, 'source_evidence_roots': 5}
    assert failure['breach']['reason'] == 'resource_observation_unavailable'
