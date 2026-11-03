"""Complete native test reports with an exact, recipe-bound suite composition.

The command's exit status and immutable command definition still matter. These
parsers establish test selection/results, never build success or authority.
Unknown runner formats fail closed instead of guessing from a passing substring.
"""
from dataclasses import dataclass
from pathlib import Path
import re

ADAPTER_ID = 'native-tests-v1'
VERSION = 1
KIND = 'tests'

_FAMILIES = {'nextest', 'unittest', 'bun'}
_MAX_SUITES = 128
_NUM = r'[0-9]{1,10}'
_TIME = r'[0-9]+(?:\.[0-9]+)?'
_SGR = re.compile(r'\x1b\[[0-9;]*m')
_NEXT_HEADER = re.compile(r'\s*Nextest run ID [a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12} with nextest profile: \S+')
_NEXT_START = re.compile(rf'\s*Starting ({_NUM}) tests? across ({_NUM}) binar(?:y|ies)(?: \(({_NUM}) tests? skipped\))?')
_NEXT_END = re.compile(rf'\s*Summary \[\s*{_TIME}s\] ({_NUM}) tests? run: ({_NUM}) passed(?: \(({_NUM}) slow\))?(?:, ({_NUM}) failed)?(?:, ({_NUM}) skipped)?')
_UNIT_RUN = re.compile(rf'Ran ({_NUM}) tests? in {_TIME}s')
_UNIT_END = re.compile(r'(OK|FAILED)(?: \(([^()]+)\))?')
_BUN_START = re.compile(r'bun test v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][^\s]+)? \([a-zA-Z0-9]+\)')
_BUN_END = re.compile(rf'Ran ({_NUM}) tests? across ({_NUM}) files?\. \[{_TIME}(?:ms|s)\]')
_BUN_COUNT = re.compile(rf'[ \t]+({_NUM}) (pass|fail|skip|todo|expect\(\) calls)')
_BUN_FRAME = re.compile(r'\s*bun[ \t]+test(?:[ \t]|$)')
_BUN_COMMAND = re.compile(r'bun test (?:\./)?[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*')


def _number(value: str | None) -> int:
    number = int(value or '0')
    if not 0 <= number <= 2 ** 31 - 1:
        raise ValueError('Native result count outside supported range')
    return number


def _report(family, selected, passed, failed=0, skipped=0, errors=0, *, unselected=0):
    if selected != passed + failed + skipped + errors or min(selected, passed, failed, skipped, errors, unselected) < 0:
        raise ValueError('Native test totals disagree')
    return {'family': family, 'selected': selected, 'passed': passed, 'failed': failed,
            'skipped': skipped, 'errors': errors, 'unselected': unselected}


def _nextest(lines):
    reports, pending, header = [], None, False
    for line in lines:
        # Treat malformed/repeated framing as a failed interpretation too.
        if re.match(r'\s*Nextest run ID ', line):
            if header or pending is not None or not _NEXT_HEADER.fullmatch(line):
                raise ValueError('Unpaired or unknown nextest invocation')
            header = True
        elif re.match(r'\s*Starting .*tests? across ', line):
            match = _NEXT_START.fullmatch(line)
            if pending is not None or not match:
                raise ValueError('Incomplete nextest start')
            pending = tuple(_number(v) for v in match.groups())
        elif re.match(r'\s*Summary \[', line):
            match = _NEXT_END.fullmatch(line)
            if pending is None or not match:
                raise ValueError('Unpaired or unknown nextest summary')
            selected, binaries, unselected = pending
            run, passed, slow, failed, skipped = map(_number, match.groups())
            if run != selected or skipped != unselected or slow > passed or selected and not binaries:
                raise ValueError('Nextest start and summary disagree')
            # Nextest's skipped total is selection filtering, not executed
            # skips. Only the matching start+end establish that distinction.
            reports.append(_report('nextest', selected, passed, failed, unselected=unselected))
            pending, header = None, False
    if pending is not None or header:
        raise ValueError('Missing nextest summary')
    return reports


def _unittest(lines):
    reports, pending = [], None
    for line in lines:
        if line.startswith('Ran ') and ' across ' not in line:
            match = _UNIT_RUN.fullmatch(line)
            if pending is not None or not match:
                raise ValueError('Incomplete unittest run report')
            pending = _number(match[1])
        elif re.match(r'^(?:OK|FAILED)(?:\b|\()', line):
            match = _UNIT_END.fullmatch(line)
            if pending is None or not match:
                raise ValueError('Unpaired or unknown unittest result')
            counts = {}
            for item in match[2].split(', ') if match[2] else []:
                field = re.fullmatch(rf'(failures|errors|skipped|expected failures|unexpected successes)=({_NUM})', item)
                if not field or field[1] in counts:
                    raise ValueError('Unknown or duplicated unittest result field')
                counts[field[1]] = _number(field[2])
            failed = counts.get('failures', 0) + counts.get('unexpected successes', 0)
            errors = counts.get('errors', 0)
            skipped = counts.get('skipped', 0) + counts.get('expected failures', 0)
            if (match[1] == 'FAILED') != bool(failed + errors):
                raise ValueError('Unittest status contradicts its counts')
            reports.append(_report('unittest', pending, pending - failed - skipped - errors, failed, skipped, errors))
            pending = None
        elif pending is not None and line.strip():
            raise ValueError('Unittest terminal status missing')
    if pending is not None:
        raise ValueError('Unittest terminal status missing')
    return reports


def _bun(lines, commands=()):
    reports, pending, counts = [], False, {}
    starts = 0
    for line in lines:
        if commands and _BUN_FRAME.match(line):
            if pending or starts >= len(commands) or line != commands[starts]:
                raise ValueError('Unpaired, reordered or unknown Bun command frame')
            pending, counts = True, {}
            starts += 1
        elif line.startswith('bun test v'):
            if pending or not _BUN_START.fullmatch(line):
                raise ValueError('Unpaired or unknown Bun invocation')
            pending, counts = True, {}
        elif match := _BUN_COUNT.fullmatch(line):
            if not pending or match[2] in counts:
                raise ValueError('Unpaired or duplicated Bun count')
            counts[match[2]] = _number(match[1])
        elif (re.fullmatch(r'[ \t]+\S+ (?:pass|fail|skip|todo|expect\(\) calls)', line)
              or (pending or commands) and re.fullmatch(r'[ \t]+[-+]?[0-9][0-9.eE+-]* [A-Za-z][A-Za-z ()_-]*', line)):
            raise ValueError('Unknown or malformed Bun result count')
        elif line.startswith('Ran ') and ' across ' in line:
            match = _BUN_END.fullmatch(line)
            if not pending or not match or not {'pass', 'fail'}.issubset(counts):
                raise ValueError('Incomplete Bun summary')
            selected, files = map(_number, match.groups())
            if selected and not files:
                raise ValueError('Bun file selection missing')
            report = _report('bun', selected, counts['pass'], counts['fail'],
                             counts.get('skip', 0) + counts.get('todo', 0))
            if commands:
                report.update(command=commands[len(reports)], invocation=len(reports) + 1)
            reports.append(report)
            pending, counts = False, {}
        elif counts and line.strip():
            raise ValueError('Bun terminal summary is not contiguous')
    if pending or counts:
        raise ValueError('Bun terminal summary missing')
    if commands and len(reports) != len(commands):
        raise ValueError('Configured Bun command frame missing')
    return reports


def _split_bun(stdout_lines, stderr_lines, commands):
    """Observe headers separately; bind summaries to exact stderr command frames.

    This supports the declared serial command order, not timing correlation
    across pipes. No arbitrary stdout/stderr fragments are concatenated.
    """
    headers = []
    for line in stdout_lines:
        if _BUN_FRAME.match(line):
            if not _BUN_START.fullmatch(line):
                raise ValueError('Unknown or mixed Bun stdout framing')
            headers.append(line)
        elif (_BUN_COUNT.fullmatch(line)
              or re.fullmatch(r'[ \t]+\S+ (?:pass|fail|skip|todo|expect\(\) calls)', line)
              or re.fullmatch(r'[ \t]+[-+]?[0-9][0-9.eE+-]* [A-Za-z][A-Za-z ()_-]*', line)
              or line.startswith('Ran ') and ' across ' in line):
            raise ValueError('Bun results appeared in the declared header-only stream')
    if len(headers) != len(commands) or len(set(headers)) != 1:
        raise ValueError('Missing, extra or inconsistent Bun stdout headers')
    return _bun(stderr_lines, commands)


@dataclass(frozen=True)
class BoundNativeTests:
    """Bind expected invocations at selection time, before any process starts."""
    suite_counts: tuple[tuple[str, int], ...]
    bun_commands: tuple[str, ...] = ()
    ADAPTER_ID = ADAPTER_ID
    VERSION = VERSION
    KIND = KIND

    def command(self, argv: list[str], output: Path) -> list[str]:
        return list(argv)

    def interpret(self, output: Path, stdout: str, stderr: str, exit_code: int | None) -> dict:
        reports, valid = [], True
        streams = {name: _SGR.sub('', text).replace('\r\n', '\n').split('\n')
                   for name, text in (('stdout', stdout), ('stderr', stderr))}
        # Default parsing still requires each report in one stream. The opt-in
        # Bun mode uses its own exact command-framing contract below.
        for stream, lines in streams.items():
            parsers = (_nextest, _unittest) if self.bun_commands else (_nextest, _unittest, _bun)
            for parser in parsers:
                try:
                    reports.extend({**report, 'stream': stream} for report in parser(lines))
                except (ValueError, OverflowError):
                    valid = False
        if self.bun_commands:
            try:
                reports.extend({**report, 'stream': 'split', 'header_stream': 'stdout',
                                'summary_stream': 'stderr'}
                    for report in _split_bun(streams['stdout'], streams['stderr'], self.bun_commands))
            except (ValueError, OverflowError):
                valid = False
        actual = {family: sum(r['family'] == family for r in reports) for family in _FAMILIES}
        expected = dict(self.suite_counts)
        valid = valid and actual == {family: expected.get(family, 0) for family in _FAMILIES}
        # Every expected suite must select tests; a positive sibling suite must
        # not hide a zero-test invocation in a mixed command.
        valid = valid and all(r['selected'] > 0 for r in reports)
        return {'kind': KIND, 'recognized': bool(valid),
                **{key: sum(r[key] for r in reports) for key in ('selected', 'passed', 'failed', 'skipped', 'errors')},
                'suite_counts': expected, 'suites': reports}


def bind(check: dict) -> BoundNativeTests:
    counts = check.get('suite_counts')
    if (not isinstance(counts, dict) or not counts or not set(counts).issubset(_FAMILIES)
            or any(type(value) is not int or not 1 <= value <= _MAX_SUITES for value in counts.values())
            or sum(counts.values()) > _MAX_SUITES):
        raise ValueError('Native test checks require exact positive suite_counts for supported runners')
    commands = check.get('bun_commands', ())
    if 'bun_commands' in check and (
            not isinstance(commands, list) or not commands or len(commands) != counts.get('bun', 0)
            or any(not isinstance(command, str) or len(command) > 4096
                   or not _BUN_COMMAND.fullmatch(command) for command in commands)
            or len(set(commands)) != len(commands)):
        raise ValueError('Bun split framing requires exact ordered bun_commands matching suite_counts')
    return BoundNativeTests(tuple(sorted(counts.items())), tuple(commands))
