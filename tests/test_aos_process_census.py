"""Argument classification uses only synthetic observations; no process calls."""
from copy import deepcopy
from pathlib import Path

import pytest

from agentic_tools.aos.process_census import (
    MAX_ARGUMENT_BYTES, MAX_KERNEL_ARGV_BYTES,
    ProcessObservationError, decode_kernel_argv, observe_marked_process,
)


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
    assert f.calls == ['identity', 'arguments', 'identity', 'arguments', 'identity']


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
    ['ok'] * 129, ['ok', 'x' * 65535], ['ok', 'ø' * 32768], ['ok'] + ['x' * 4096] * 16,
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


def kernel_block(args, *, executable=b'/qualified/bin/python', padding=None,
                 environment=b'', byteorder='little', count=None):
    """XNU's 64-bit exec string region, with its16byte prefix stripped."""
    region = b'executable_path=' + executable + b'\0'
    region += b'\0' * ((-len(region)) % 8) if padding is None else padding
    return ((len(args) if count is None else count).to_bytes(4, byteorder, signed=True)
            + region[16:] + b''.join(arg.encode('utf-8') + b'\0' for arg in args) + environment)


@pytest.mark.parametrize('size', [4096, 4097, 21145, MAX_ARGUMENT_BYTES])
def test_complete_large_argument_uses_the_same_aggregate_budget(size):
    args = ['x' * size]
    f = Census(decode_kernel_argv(kernel_block(args)))
    f.executable = OSError('unavailable protected executable')
    assert f.observe() is None
    assert f.calls == ['identity', 'arguments', 'identity', 'arguments', 'identity']


@pytest.mark.parametrize('total,accepted', [(65535, True), (65536, True), (65537, False)])
def test_raw_and_list_argument_data_share_exact_byte_budget(total, accepted):
    args = ['py', 'x' * (total - 2)]
    if accepted:
        assert decode_kernel_argv(kernel_block(args)) == args
        assert Census(args).observe() is None
    else:
        with pytest.raises(ProcessObservationError, match='bounds'):
            decode_kernel_argv(kernel_block(args))
        with pytest.raises(ProcessObservationError, match='bounds'):
            Census(args).observe()


@pytest.mark.parametrize('extra,accepted', [('', True), ('x', False)])
def test_multibyte_boundary_counts_utf8_bytes_not_characters(extra, accepted):
    args = ['py', 'ø' * 32767 + extra]
    if accepted:
        assert decode_kernel_argv(kernel_block(args)) == args
    else:
        with pytest.raises(ProcessObservationError, match='bounds'):
            decode_kernel_argv(kernel_block(args))


@pytest.mark.parametrize('argc,accepted', [(128, True), (129, False)])
def test_raw_argument_count_bound(argc, accepted):
    args = ['program'] + [''] * (argc - 1)
    if accepted:
        assert decode_kernel_argv(kernel_block(args)) == args
    else:
        with pytest.raises(ProcessObservationError, match='count'):
            decode_kernel_argv(kernel_block(args))


@pytest.mark.parametrize('length', range(1, 17))
def test_executable_alignment_all_remainders_and_empty_later_args(length):
    args = ['rewritten argv0', '', 'argument']
    assert decode_kernel_argv(kernel_block(args, executable=b'x' * length)) == args


@pytest.mark.parametrize('byteorder', ['little', 'big'])
def test_native_int_header_can_be_decoded_without_host_calls(byteorder):
    args = ['program', '--argument']
    assert decode_kernel_argv(kernel_block(args, byteorder=byteorder), byteorder=byteorder) == args


def test_environment_is_never_decoded_returned_or_classified():
    args = ['program', 'last argument']
    environment = b'\xffSECRET=' + MARKER.encode() + b'\0\x80\0'
    decoded = decode_kernel_argv(kernel_block(args, environment=environment))
    assert decoded == args
    f = Census(decoded); f.executable = AssertionError('environment marker reached executable lookup')
    assert f.observe() is None


def test_exact_raw_block_limit_ignores_opaque_trailing_bytes_but_never_overflows():
    raw = kernel_block(['program'])
    full = raw + b'\xff' * (MAX_KERNEL_ARGV_BYTES - len(raw))
    assert decode_kernel_argv(full) == ['program']
    with pytest.raises(ProcessObservationError, match='block'):
        decode_kernel_argv(full + b'\0')


@pytest.mark.parametrize('count', [-1, 0, 129, 2**31 - 1])
def test_invalid_kernel_argc_never_decodes_trailing_data(count):
    with pytest.raises(ProcessObservationError, match='count'):
        decode_kernel_argv(kernel_block(['program'], count=count, environment=b'\xffSECRET'))


@pytest.mark.parametrize('args', [[''], ['', 'second']])
def test_empty_argv0_cannot_shift_argv_into_environment(args):
    with pytest.raises(ProcessObservationError, match=r'argv\[0\]'):
        decode_kernel_argv(kernel_block(args, environment=b'SECRET=' + MARKER.encode() + b'\0'))


@pytest.mark.parametrize('raw', [b'', b'\x01\0\0\0',
    b'\x01\0\0\0unterminated-executable',
    kernel_block(['program'])[:-1],
    kernel_block(['program'], count=2),
    kernel_block(['program'], executable=b''),
    kernel_block(['program'], executable=b'x', padding=b'!\0\0\0\0\0'),
    kernel_block(['program'])[:-8] + b'\xff\0',
])
def test_malformed_or_truncated_kernel_block_holds(raw):
    with pytest.raises(ProcessObservationError):
        decode_kernel_argv(raw)


def test_long_argument_marker_keeps_strict_executable_attestation():
    f = Census(decode_kernel_argv(kernel_block(['program', 'x' * 21145 + MARKER])))
    f.executable = Path('/unqualified/bin/limactl')
    with pytest.raises(ProcessObservationError, match='executable'):
        f.observe()
    assert 'executable' in f.calls


@pytest.mark.parametrize('before,after', [
    (['unrelated'], ['program', MARKER]),
    (['unrelated', 'first'], ['unrelated', 'second']),
    (['program', MARKER], ['unrelated']),
])
def test_same_birth_argument_drift_cannot_establish_exclusion_or_ownership(before, after):
    f = Census(before)
    f.after_arguments = lambda: setattr(f, 'args', after)
    with pytest.raises(ProcessObservationError, match='arguments changed'):
        f.observe()
    assert ('executable' in f.calls) == any(MARKER in arg for arg in before)


@pytest.mark.parametrize('field,value', [('pid', 456), ('uid', 502), ('birth', [1000, 8]), ('status', 5)])
def test_identity_drift_after_second_unmarked_argument_read_holds(field, value):
    f = Census()
    identity = f.read_identity
    def changed(pid):
        if f.reads == 2:
            f.identity.update({field: value})
        return identity(pid)
    f.read_identity = changed
    with pytest.raises(ProcessObservationError):
        f.observe()
    assert 'executable' not in f.calls
