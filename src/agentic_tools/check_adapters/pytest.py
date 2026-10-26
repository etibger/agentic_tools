"""The historical pytest/JUnit interpretation, outside orchestration code."""
from pathlib import Path
import xml.etree.ElementTree as ET

ADAPTER_ID = 'pytest-v1'
VERSION = 1
KIND = 'tests'


def command(argv: list[str], output: Path) -> list[str]:
    return argv + ['-p', 'no:cacheprovider', '--junitxml=' + str(output / 'junit.xml')]


def interpret(output: Path, stdout: str, stderr: str, exit_code: int | None) -> dict:
    selected = passed = failed = skipped = errors = 0
    report = output / 'junit.xml'
    recognized = False
    if report.is_file() and not report.is_symlink():
        try:
            cases = list(ET.parse(report).getroot().iter('testcase'))
            selected = len(cases)
            recognized = True
            for case in cases:
                tags = {child.tag for child in case}
                if 'error' in tags:
                    errors += 1
                elif 'failure' in tags:
                    failed += 1
                elif 'skipped' in tags:
                    skipped += 1
                elif list(case):
                    # Preserve the previous conservative treatment of any child.
                    errors += 1
                else:
                    passed += 1
        except (ET.ParseError, OSError, ValueError):
            recognized = False
    return {'kind': KIND, 'recognized': recognized, 'selected': selected,
            'passed': passed, 'failed': failed, 'skipped': skipped, 'errors': errors,
            'artifacts': [str(report)] if recognized else []}
