"""Stable Rust libtest summaries emitted by an exact Cargo test command."""
from pathlib import Path
import re

ADAPTER_ID = 'cargo-test-v1'
VERSION = 1
KIND = 'tests'

SUMMARY = re.compile(r'^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; (\d+) filtered out(?:;.*)?$', re.MULTILINE)
RUNNING = re.compile(r'^running (\d+) tests?\s*$', re.MULTILINE)


def command(argv: list[str], output: Path) -> list[str]:
    return list(argv)


def interpret(output: Path, stdout: str, stderr: str, exit_code: int | None) -> dict:
    # Cargo writes build messages to stderr; only libtest stdout establishes
    # selection/results. A build-success line alone cannot satisfy tests.
    summaries = list(SUMMARY.finditer(stdout))
    runs = [int(m.group(1)) for m in RUNNING.finditer(stdout)]
    passed = failed = skipped = measured = 0
    valid = bool(summaries) and len(summaries) == len(runs)
    for index, match in enumerate(summaries):
        p, f, s, m, _filtered = map(int, match.groups()[1:])
        passed += p
        failed += f
        skipped += s
        measured += m
        valid = valid and index < len(runs) and runs[index] == p + f + s + m
        valid = valid and (match.group(1) == ('FAILED' if f else 'ok'))
    # Benchmarks are not tests. Refuse a mixed measured/test report rather than
    # quietly dropping cases from the normalized total.
    valid = valid and measured == 0
    return {'kind': KIND, 'recognized': valid, 'selected': passed + failed + skipped,
            'passed': passed, 'failed': failed, 'skipped': skipped, 'errors': 0}
