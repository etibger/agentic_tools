"""Reusable native command build evidence; never claims to run tests."""
from pathlib import Path

ADAPTER_ID = 'native-build-v1'
VERSION = 1
KIND = 'build'


def command(argv: list[str], output: Path) -> list[str]:
    return list(argv)


def interpret(output: Path, stdout: str, stderr: str, exit_code: int | None) -> dict:
    # The exact approved command defines the build obligation. A terminal exit
    # status and retained nonempty raw output are required; no test count exists.
    return {'kind': KIND, 'recognized': type(exit_code) is int and bool((stdout + stderr).strip()),
            'selected': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0}
