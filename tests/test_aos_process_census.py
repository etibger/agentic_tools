"""Argument classification uses only synthetic observations; no process calls."""
from copy import deepcopy
from pathlib import Path

import pytest

from agentic_tools.aos.process_census import ProcessObservationError, observe_marked_process


MARKER = '/owned/service'
LIMA = Path('/qualified/bin/limactl')
SSH = Path('/usr/bin/ssh')


class Census:
    def __init__(self, args=None):
        self.identity = {'pid': 123, 'uid': 501, 'birth': [1000, 7], 'status': 2}
        self.args = args if args is not None else ['/protected/application', '--ordinary']
        self.calls = []
        self.reads = 0
        self.after_arguments = None
        self.after_executable = None
        self.executable = LIMA

    def read_identity(self, pid):
        assert pid == 123
        self.calls.append('identity')
        self.reads += 1
        if self.reads == 2 and self.after_arguments:
            self.after_arguments()
        return deepcopy(self.identity)

    def read_args(self, pid):
        assert pid == 123
        self.calls.append('arguments')
        if isinstance(self.args, Exception):
            raise self.args
        return deepcopy(self.args)

    def read_executable(self, pid):
        assert pid == 123
        self.calls.append('executable')
        if isinstance(self.executable, Exception):
            raise self.executable
        if self.after_executable:
            self.after_executable()
        return self.executable

    def observe(self, **kw):
        return observe_marked_process(123, kw.pop('census_uid', 501), expected_uid=501,
            marker=MARKER, identity_reader=self.read_identity, argv_reader=self.read_args,
            executable_reader=self.read_executable, allowed_executables=(LIMA, SSH), **kw)


def test_protected_unrelated_process_never_queries_unreadable_executable():
    f = Census()
    f.executable = OSError('protected kernel path')
    assert f.observe() is None
    assert f.calls == ['identity', 'arguments', 'identity']


def test_readable_empty_argument_is_not_unknown_arguments():
    f = Census(['/qualified/python', '-c', ''])
    assert f.observe() is None
    assert 'executable' not in f.calls


@pytest.mark.parametrize('args', [
    ['/protected/application', '--config=' + MARKER + '/file'],
    ['ssh: ' + MARKER + '/ssh.sock [mux]'],
    [MARKER + '/spoofed-application'],
    ['/protected/application', '--target=' + MARKER],
    ['/protected/application', 'prefix:' + MARKER + ':suffix'],
])
def test_markers_hidden_in_any_argument_cannot_hide_an_unqualified_binary(args):
    f = Census(args)
    f.executable = Path('/unqualified/bin/limactl')
    with pytest.raises(ProcessObservationError, match='executable'):
        f.observe()
    assert 'executable' in f.calls


def test_qualified_candidate_preserves_exact_identity_args_and_executable():
    f = Census([str(LIMA), 'hostagent', MARKER + '/ha.pid'])
    result = f.observe()
    assert result == {'identity': f.identity, 'argv': f.args, 'executable': str(LIMA)}
    assert f.calls == ['identity', 'arguments', 'identity', 'executable', 'arguments', 'identity']


def test_marked_unavailable_executable_holds_even_if_it_disappears():
    f = Census(['/spoofed/name', MARKER])
    f.executable = OSError('kernel path unavailable')
    with pytest.raises(OSError):
        f.observe()


@pytest.mark.parametrize('args', [None, [], [''], ['ok', '\x00hidden'], ['ok', 7],
    ['ok'] * 129, ['ok', 'x' * 4097], ['ok', 'ø' * 2049], ['ok'] + ['x' * 4096] * 16,
    ['ok', '\ud800']])
def test_unavailable_invalid_or_unbounded_arguments_hold_before_binary(args):
    f = Census(); f.args = args
    with pytest.raises(ProcessObservationError, match='arguments'):
        f.observe()
    assert 'executable' not in f.calls


def test_unreadable_args_holds_even_for_an_unrelated_protected_process():
    f = Census(); f.args = OSError('arguments unavailable')
    with pytest.raises(OSError):
        f.observe()
    assert 'executable' not in f.calls


@pytest.mark.parametrize('args', [['/protected/application'], [str(LIMA), MARKER]])
@pytest.mark.parametrize('field,value', [('pid', 456), ('uid', 502), ('birth', [1000, 8]),
                                       ('status', 5)])
def test_identity_change_after_args_holds_even_for_excluded_process(args, field, value):
    f = Census(args)
    f.after_arguments = lambda: f.identity.update({field: value})
    with pytest.raises(ProcessObservationError):
        f.observe()
    assert 'executable' not in f.calls


def test_disappearance_after_args_is_not_a_proven_unrelated_process():
    f = Census()
    f.after_arguments = lambda: setattr(f, 'identity', None)
    with pytest.raises(ProcessObservationError):
        f.observe()


def test_census_uid_disagreement_holds_before_reading_args():
    f = Census(); f.identity['uid'] = 502
    with pytest.raises(ProcessObservationError):
        f.observe()
    assert f.calls == ['identity']


@pytest.mark.parametrize('field,value', [('pid', 456), ('uid', 502), ('birth', [1000, 8]),
                                       ('status', 5)])
def test_identity_change_after_executable_holds(field, value):
    f = Census([str(LIMA), MARKER])
    f.after_executable = lambda: f.identity.update({field: value})
    with pytest.raises(ProcessObservationError):
        f.observe()


def test_exec_or_title_change_without_birth_change_holds():
    f = Census([str(LIMA), MARKER])
    f.after_executable = lambda: setattr(f, 'args', [str(LIMA), 'different'])
    with pytest.raises(ProcessObservationError, match='arguments changed'):
        f.observe()


def test_departed_census_child_is_not_a_live_candidate():
    f = Census(); f.identity = None
    assert f.observe() is None
    assert f.calls == ['identity']


def test_other_user_does_not_need_process_inspection():
    f = Census()
    assert f.observe(census_uid=0) is None
    assert f.calls == []
