"""Bounded observation failure causes survive without arbitrary exception text."""
import errno
import json
import sqlite3
import time

import pytest

from agentic_tools.aos import resource_limits as limits
from agentic_tools.aos.store import Rejected
from test_aos_resource_limits import guarded


def failed_observation(g, monkeypatch, error):
    def fail(bound):
        raise error
    monkeypatch.setattr(limits, 'observe', fail)
    with g.db:
        return limits.sample(g.db, 'WORK', g.bound, 'diagnostic', force=True)


def test_rejected_observation_retains_cause_across_database_and_failure_latch(guarded, monkeypatch):
    g = guarded
    result = failed_observation(g, monkeypatch,
                                Rejected('resource_observation', 'Resource observation exceeded its bounded scan'))
    failure = result['breach']
    assert failure['reason'] == 'observation_failed' and failure['error'] == 'Rejected'
    assert failure['error_code'] == 'resource_observation'
    assert failure['error_message'] == 'Resource observation exceeded its bounded scan'
    assert 'errno' not in failure
    with sqlite3.connect(g.path) as restarted:
        assert limits._read(restarted, 'WORK')['breach'] == failure
    limits._retain_failure(g.path, 'WORK', g.bound, result)
    assert limits._read_failure(g.db, 'WORK', g.bound)['breach'] == failure


def test_rejected_detail_is_bounded_using_the_full_original_prefix(guarded, monkeypatch):
    code, message = 'resource_' + 'x' * 100, 'details ' + '\U0001f4a1' * 1000
    result = failed_observation(guarded, monkeypatch, Rejected(code, message))
    assert result['breach']['error_code'] == code[:64]
    assert result['breach']['error_message'] == message[:500]
    limits._retain_failure(guarded.path, 'WORK', guarded.bound, result)
    assert limits._read_failure(guarded.db, 'WORK', guarded.bound)['breach'] == result['breach']


@pytest.mark.parametrize('error,expected', [
    (OSError(errno.EACCES, 'DO_NOT_CAPTURE_STRERROR', '/private/DO_NOT_CAPTURE_FILENAME'), {'errno': errno.EACCES}),
    (OSError('DO_NOT_CAPTURE_OSERROR_ARGS'), {}),
    (ValueError('DO_NOT_CAPTURE_VALUEERROR_ARGS'), {}),
])
def test_other_exception_messages_and_paths_are_not_retained(guarded, monkeypatch, error, expected):
    failure = failed_observation(guarded, monkeypatch, error)['breach']
    assert failure['reason'] == 'observation_failed' and failure['error'] == type(error).__name__
    assert {key: value for key, value in failure.items()
            if key not in ('reason', 'error', 'observed_at')} == expected
    assert 'DO_NOT_CAPTURE' not in json.dumps(failure)


def test_new_observation_details_do_not_replace_an_existing_breach(guarded, monkeypatch):
    g = guarded
    (g.target / 'over-limit').write_bytes(b'x' * 11)
    with g.db:
        initial = limits.sample(g.db, 'WORK', g.bound, 'diagnostic', force=True)
    assert initial['breach']['reason'] == 'max_output_growth_bytes'
    later = failed_observation(g, monkeypatch, Rejected('resource_observation', 'later scan failure'))
    assert later['breach'] == initial['breach']


@pytest.mark.parametrize('details', [{}, {'error_code': 'resource_observation', 'error_message': ''},
                                    {'error_code': 'x' * 64, 'error_message': 'x' * 500}, {'errno': errno.EIO}])
def test_failure_latch_accepts_legacy_and_bounded_optional_details(guarded, details):
    g = guarded
    pending = limits._read(g.db, 'WORK')
    pending['breach'] = {'reason': 'observation_failed', 'error': 'Rejected',
                         'observed_at': time.time(), **details}
    limits._retain_failure(g.path, 'WORK', g.bound, pending)
    assert limits._read_failure(g.db, 'WORK', g.bound)['breach'] == pending['breach']


@pytest.mark.parametrize('key,value', [
    ('error_code', None), ('error_code', 1), ('error_code', ''), ('error_code', 'x' * 65),
    ('error_message', None), ('error_message', 1), ('error_message', 'x' * 501),
    ('errno', None), ('errno', True), ('errno', '5'), ('errno', 5.0),
])
def test_failure_latch_rejects_malformed_or_oversize_optional_details(guarded, key, value):
    g = guarded
    pending = limits._read(g.db, 'WORK')
    pending['breach'] = {'reason': 'observation_failed', 'error': 'Rejected',
                         'observed_at': time.time(), key: value}
    limits._retain_failure(g.path, 'WORK', g.bound, pending)
    with pytest.raises(Rejected, match='malformed'):
        limits._read_failure(g.db, 'WORK', g.bound)
