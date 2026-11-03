"""Bounded macOS ``pgrep -af`` for the qualified native sandbox only.

The system pgrep uses sysmond, which can disclose arguments outside the native
process boundary. This helper instead lists PIDs, verifies kernel same-sandbox
signal permission, and reads argv only for an unchanged UID/birth identity.
It requires the process-info and sysctl rules in native_sandbox; a sandbox's
mere presence is not evidence that an arbitrary other profile is suitable.

Invoke the protected file with the trusted interpreter's ``-I -S`` flags. The
recipe must protect and hash its private PATH wrapper and both sibling modules.
Only ``-af pattern`` is supported; output is PID lines, as on macOS (where -a
includes ancestors). This is not a general replacement for the system tool.
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import shlex
import struct
import sys
from types import SimpleNamespace


def _processes():
    # -I -S deliberately excludes site packages, the current working directory,
    # and PYTHONPATH. Load only this protected, absolute sibling source.
    path = Path(__file__).resolve().with_name('native_processes.py')
    namespace = {'__name__': '_aos_native_processes', '__file__': str(path)}
    # Bind execution to the protected source bytes, not a mutable/stale pyc
    # beside them that is not part of the recipe's source-file hash manifest.
    exec(compile(path.read_bytes(), str(path), 'exec'), namespace)
    return SimpleNamespace(**namespace)


class _Regex(ctypes.Structure):
    # Darwin SDK _regex.h: regex_t (not the incompatible Linux representation).
    _fields_ = [('magic', ctypes.c_int), ('nsub', ctypes.c_size_t),
                ('endp', ctypes.c_void_p), ('guts', ctypes.c_void_p)]


def _libc():
    if sys.platform != 'darwin':
        raise RuntimeError('Native pgrep is qualified only on macOS')
    return ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)


class Pattern:
    """Use the platform's actual POSIX ERE, not Python regex semantics."""
    def __init__(self, pattern: str):
        encoded = os.fsencode(pattern)
        if not encoded or len(encoded) > 4096 or b'\0' in encoded:
            raise ValueError('Pattern must contain 1 to 4096 bytes and no NUL')
        self.library = _libc()
        self.compiled = _Regex()
        self.library.regcomp.argtypes = [ctypes.POINTER(_Regex), ctypes.c_char_p, ctypes.c_int]
        self.library.regcomp.restype = ctypes.c_int
        self.library.regexec.argtypes = [ctypes.POINTER(_Regex), ctypes.c_char_p,
                                        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        self.library.regexec.restype = ctypes.c_int
        self.library.regfree.argtypes = [ctypes.POINTER(_Regex)]
        self.library.regfree.restype = None
        # REG_EXTENDED | REG_NOSUB, from Darwin _regex.h.
        result = self.library.regcomp(ctypes.byref(self.compiled), encoded, 1 | 4)
        if result:
            raise ValueError('Invalid POSIX extended regular expression')

    def matches(self, command: bytes) -> bool:
        result = self.library.regexec(ctypes.byref(self.compiled), command, 0, None, 0)
        if result not in (0, 1):  # REG_NOMATCH = 1
            raise RuntimeError('Native regular expression execution failed')
        return result == 0

    def close(self):
        self.library.regfree(ctypes.byref(self.compiled))


def _require_sandbox():
    library = ctypes.CDLL('/usr/lib/libsandbox.1.dylib', use_errno=True)
    library.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    library.sandbox_check.restype = ctypes.c_int
    if library.sandbox_check(os.getpid(), None, 0) != 1:
        raise RuntimeError('Native pgrep requires the qualified native sandbox')


def _argv(raw: bytes) -> bytes:
    """Parse KERN_PROCARGS2 and exclude the environment after argc arguments."""
    if len(raw) < 5:
        raise RuntimeError('Incomplete native process arguments')
    count = struct.unpack_from('=i', raw)[0]
    if count <= 0 or count > len(raw):
        raise RuntimeError('Invalid native process argument count')
    end = raw.find(b'\0', 4)  # Executable path precedes argv, with NUL padding.
    if end < 0:
        raise RuntimeError('Incomplete native process executable')
    # Darwin exec_extract_strings aligns the executable PATH AREA to the
    # target pointer width before argv. The removed executable_path= prefix
    # is 16 bytes, so it preserves this alignment. Current macOS supports
    # 64-bit processes only. Skipping arbitrary NULs would swallow an empty
    # argv[0] and could incorrectly include an environment entry in argc.
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError('Native pgrep requires the qualified 64-bit ABI')
    offset = 4 + ((end + 1 - 4 + 7) // 8) * 8
    if offset >= len(raw) or any(raw[end + 1:offset]):
        raise RuntimeError('Unsupported native process argument alignment')
    arguments = []
    for _ in range(count):
        end = raw.find(b'\0', offset)
        if end < 0:
            raise RuntimeError('Incomplete native process argument vector')
        arguments.append(raw[offset:end])
        offset = end + 1
    return b' '.join(arguments)


def arguments(pid: int) -> bytes:
    library = _libc()
    library.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                              ctypes.c_void_p, ctypes.c_size_t]
    library.sysctl.restype = ctypes.c_int
    # Darwin CTL_KERN=1, KERN_PROCARGS2=49. Use the host's bounded ARG_MAX;
    # a truncated or unexpected buffer is an error, never a successful match.
    capacity = os.sysconf('SC_ARG_MAX')
    if not 4096 <= capacity <= 4 * 1024 * 1024:
        raise RuntimeError('Unsupported native argument buffer bound')
    # KERN_PROCARGS2 subtracts the argc prefix before copying up to ARG_MAX
    # argument bytes; omitting this space can produce silent tail truncation.
    capacity += ctypes.sizeof(ctypes.c_int)
    buffer = ctypes.create_string_buffer(capacity)
    size = ctypes.c_size_t(capacity)
    mib = (ctypes.c_int * 3)(1, 49, pid)
    ctypes.set_errno(0)
    if library.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'Native process arguments unavailable')
    if size.value > capacity:
        raise RuntimeError('Native process argument buffer overflow')
    return _argv(buffer.raw[:size.value])


def find(pattern: Pattern, processes=None) -> list[int]:
    processes = processes or _processes()
    matches = []
    for pid in sorted(set(processes.all_pids())):
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            # No metadata or argv request occurs before the kernel selects
            # an owned member. A process group, name or parent is insufficient.
            os.kill(pid, 0)
            before = processes.identity(pid)
            if before is None or before['status'] == 5:
                continue
            if before['uid'] != os.getuid():
                raise RuntimeError('Unexpected cross-UID native process')
            command = arguments(pid)
            after = processes.identity(pid)
            os.kill(pid, 0)
            if not processes.same(before, after) or after['status'] == 5:
                continue
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES, errno.ESRCH):
                continue
            raise
        if pattern.matches(command):
            matches.append(pid)
    return matches


def wrapper(interpreter: str) -> str:
    """Return a wrapper for the recipe's protected directory; install nothing."""
    python = Path(interpreter)
    if (not python.is_absolute() or not python.is_file()
            or any(c in interpreter for c in ('\0', '\n', '\r'))):
        raise ValueError('Native pgrep requires an existing absolute interpreter')
    helper = Path(__file__).resolve()
    return '#!/bin/sh\nexec ' + shlex.quote(str(python.resolve())) + ' -I -S ' + shlex.quote(str(helper)) + ' "$@"\n'


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != '-af':
        print('usage: native pgrep -af pattern', file=sys.stderr)
        return 2
    try:
        pattern = Pattern(argv[1])
    except ValueError as error:
        print('pgrep: ' + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('pgrep: ' + str(error), file=sys.stderr)
        return 3
    try:
        _require_sandbox()
        matches = find(pattern)
        for pid in matches:
            print(pid)
        return 0 if matches else 1
    except Exception as error:
        print('pgrep: ' + str(error), file=sys.stderr)
        return 3
    finally:
        pattern.close()


if __name__ == '__main__':
    raise SystemExit(main())
