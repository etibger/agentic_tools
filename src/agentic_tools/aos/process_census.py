"""Bounded argument classification before strict service executable attestation.

An argument marker selects a *candidate*, never an owner. Callers must still
verify their service role and reconcile PID files, sockets and listeners. A
live process can be excluded only after readable complete arguments and a
stable kernel PID/UID/birth observation. No executable lookup is needed for
that exclusion, including for protected macOS processes.
"""
from __future__ import annotations

from pathlib import Path
import sys


MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 65536
MAX_KERNEL_ARGV_BYTES = 131072


class ProcessObservationError(RuntimeError):
    """An unknown or contradictory process observation must hold execution."""


def _require(condition, message):
    if not condition:
        raise ProcessObservationError(message)


def _identity(value, pid, uid):
    _require(isinstance(value, dict) and type(value.get('pid')) is int and
             value['pid'] == pid and type(value.get('uid')) is int and
             value['uid'] == uid and type(value.get('birth')) is list and
             len(value['birth']) == 2 and
             all(type(x) is int and x >= 0 for x in value['birth']) and
             type(value.get('status')) is int and value['status'] != 5,
             'Process census identity is unavailable or contradictory')
    return {**value, 'birth': list(value['birth'])}


def _arguments(value):
    _require(type(value) is list and 1 <= len(value) <= MAX_ARGUMENTS and value[0] and
             all(type(x) is str and '\x00' not in x for x in value),
             'Process census arguments are unavailable or malformed')
    try:
        sizes = [len(x.encode('utf-8')) for x in value]
    except UnicodeError as error:
        raise ProcessObservationError('Process census arguments are not UTF-8') from error
    _require(sum(sizes) <= MAX_ARGUMENT_BYTES,
             'Process census arguments exceed their bounds')
    return list(value)


def decode_kernel_argv(data: bytes, *, byteorder: str = sys.byteorder) -> list[str]:
    """Decode complete argv from a bounded 64-bit macOS KERN_PROCARGS2 block.

    The caller must separately establish successful, complete kernel capture.
    The block contains a native int argc, an executable string and NUL padding,
    then argc NUL-terminated arguments. Trailing environment bytes are opaque:
    they are never decoded, returned or searched for ownership markers.
    """
    _require(type(data) is bytes and 5 <= len(data) <= MAX_KERNEL_ARGV_BYTES,
             'Process kernel argument block exceeds its bounds or is malformed')
    _require(byteorder in ('little', 'big'), 'Invalid kernel argument byte order')
    argc = int.from_bytes(data[:4], byteorder, signed=True)
    _require(1 <= argc <= MAX_ARGUMENTS, 'Process kernel argument count is invalid')
    executable_end = data.find(b'\0', 4)
    _require(executable_end > 4, 'Process kernel executable string is incomplete')
    # XNU exec_extract_strings aligns the executable string (including its NUL
    # and the stripped 16-byte executable_path= prefix) to target pointer size.
    # This reader supports the qualified 64-bit macOS layout only. Skipping an
    # arbitrary NUL run would confuse empty argv[0] with padding and read env.
    # https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.121.6/bsd/kern/kern_exec.c#L6290
    path_bytes = executable_end - 4 + 1
    position = 4 + (path_bytes + 7) // 8 * 8
    _require(position < len(data) and not any(data[executable_end + 1:position]),
             'Process kernel executable padding is incomplete or malformed')
    _require(data[position] != 0, 'Process kernel argv[0] is empty')
    arguments, total = [], 0
    for _ in range(argc):
        end = data.find(b'\0', position)
        _require(end >= position, 'Process kernel arguments are incomplete')
        total += end - position
        _require(total <= MAX_ARGUMENT_BYTES, 'Process kernel arguments exceed their bounds')
        try:
            arguments.append(data[position:end].decode('utf-8'))
        except UnicodeError:
            raise ProcessObservationError('Process kernel arguments are not UTF-8') from None
        position = end + 1
    return _arguments(arguments)


def observe_marked_process(pid, census_uid, *, expected_uid, marker,
                           identity_reader, argv_reader, executable_reader,
                           allowed_executables):
    """Return a strictly attested candidate, or exclude a proven non-target.

    Readers must return kernel observations rather than process display names.
    A PID already absent before classification is not a live candidate. Once
    observed alive, lost identity, changed arguments, unavailable observations
    and executable mismatch all fail closed. The marker is searched throughout
    every argument, including argv[0], option values and rewritten SSH titles;
    conservative false-positive candidates still require the caller's role
    verification. This function supplies no signal or lifecycle authority.
    """
    _require(type(pid) is int and 0 < pid < 10**9 and
             type(census_uid) is int and census_uid >= 0 and
             type(expected_uid) is int and expected_uid >= 0,
             'Invalid process census identity selector')
    _require(type(marker) is str and marker and '\x00' not in marker,
             'Invalid process census marker')
    _require(allowed_executables and all(isinstance(x, Path) and x.is_absolute()
                                        for x in allowed_executables),
             'Exact executable paths are required')
    if census_uid != expected_uid:
        return None
    before = identity_reader(pid)
    if before is None:
        return None
    before = _identity(before, pid, expected_uid)
    args = _arguments(argv_reader(pid))
    after = _identity(identity_reader(pid), pid, expected_uid)
    _require(before['birth'] == after['birth'], 'Process identity changed during argument observation')
    if not any(marker in value for value in args):
        _require(_arguments(argv_reader(pid)) == args,
                 'Process arguments changed before unrelated exclusion')
        final = _identity(identity_reader(pid), pid, expected_uid)
        _require(before['birth'] == final['birth'], 'Process identity changed before unrelated exclusion')
        return None
    executable = executable_reader(pid)
    _require(isinstance(executable, Path) and executable in allowed_executables,
             'Marked process executable is not an approved service executable')
    _require(_arguments(argv_reader(pid)) == args,
             'Marked process arguments changed during executable observation')
    final = _identity(identity_reader(pid), pid, expected_uid)
    _require(before['birth'] == final['birth'], 'Process identity changed during executable observation')
    return {'identity': before, 'executable': str(executable), 'argv': args}
