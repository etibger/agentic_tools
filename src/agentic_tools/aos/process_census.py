"""Bounded argument classification before strict service executable attestation.

An argument marker selects a *candidate*, never an owner. Callers must still
verify their service role and reconcile PID files, sockets and listeners. A
live process can be excluded only after readable complete arguments and a
stable kernel PID/UID/birth observation. No executable lookup is needed for
that exclusion, including for protected macOS processes.
"""
from __future__ import annotations

from pathlib import Path


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
    _require(type(value) is list and 1 <= len(value) <= 128 and value[0] and
             all(type(x) is str and '\x00' not in x for x in value),
             'Process census arguments are unavailable or malformed')
    try:
        sizes = [len(x.encode('utf-8')) for x in value]
    except UnicodeError as error:
        raise ProcessObservationError('Process census arguments are not UTF-8') from error
    _require(max(sizes) <= 4096 and sum(sizes) <= 65536,
             'Process census arguments exceed their bounds')
    return list(value)


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
        return None
    executable = executable_reader(pid)
    _require(isinstance(executable, Path) and executable in allowed_executables,
             'Marked process executable is not an approved service executable')
    _require(_arguments(argv_reader(pid)) == args,
             'Marked process arguments changed during executable observation')
    final = _identity(identity_reader(pid), pid, expected_uid)
    _require(before['birth'] == final['birth'], 'Process identity changed during executable observation')
    return {'identity': before, 'executable': str(executable), 'argv': args}
