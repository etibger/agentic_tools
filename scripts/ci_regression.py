"""Bound a CI command and preserve attributable evidence, including partial pytest results."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def source_identity():
    def git(*arguments):
        return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', *arguments], stderr=subprocess.PIPE, timeout=10)
    try:
        changes = git('diff', '--binary', 'HEAD')
        untracked = {}
        for raw in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if raw:
                path = Path(os.fsdecode(raw))
                content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                untracked[str(path)] = hashlib.sha256(content).hexdigest()
        return {'head': git('rev-parse', 'HEAD').decode().strip(),
                'tracked_diff_sha256': hashlib.sha256(changes).hexdigest(),
                'dirty': bool(changes or untracked), 'untracked_sha256': untracked}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {'error': str(exc), 'head': None}


def tool_versions():
    versions = {}
    for name in ('uv', 'node'):
        executable = shutil.which(name)
        if executable is None:
            versions[name] = 'unavailable'
            continue
        try:
            versions[name] = subprocess.check_output([executable, '--version'],
                stderr=subprocess.STDOUT, text=True, timeout=2).strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            versions[name] = {'error': str(exc)}
    return versions


def summarize_tests(directory: Path, command_status: str):
    """A completed teardown is required; interrupted and absent work never becomes pass."""
    inventory_path = directory / 'inventory.json'
    if not inventory_path.exists():
        return {'inventory': 'missing', 'counts': {}, 'tests': {}, 'events_truncated': False}
    try:
        inventory = json.loads(inventory_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {'inventory': 'invalid', 'counts': {}, 'tests': {}, 'events_truncated': False,
                'evidence_errors': [str(exc)]}
    reports, started, finished = {}, set(), False
    truncated = False
    events_path = directory / 'events.jsonl'
    for line in events_path.read_text().splitlines() if events_path.exists() else []:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            truncated = True
            continue
        if event['event'] == 'start':
            started.add(event['nodeid'])
        elif event['event'] == 'report':
            reports.setdefault(event['nodeid'], []).append(event)
        elif event['event'] == 'session_finish':
            finished = True
    errors = []
    if len(set(inventory['all'])) != len(inventory['all']) or len(set(inventory['selected'])) != len(inventory['selected']):
        errors.append('duplicate collected nodeids')
    if not set(inventory['selected']).issubset(inventory['all']):
        errors.append('selected nodeids absent from full inventory')
    tests = {}
    for nodeid in inventory['selected']:
        phases = reports.get(nodeid, [])
        if len({p['phase'] for p in phases}) != len(phases):
            errors.append('duplicate test phase: ' + nodeid)
        if any(p['outcome'] == 'failed' for p in phases):
            status = 'failed'
        elif not any(p['phase'] == 'teardown' for p in phases):
            status = ('cancelled' if command_status in ('cancelled', 'timed_out') else 'missing') if nodeid in started else 'missing'
        elif any(p['outcome'] == 'skipped' for p in phases):
            status = 'skipped'
        elif any(p['phase'] == 'call' and p['outcome'] == 'passed' for p in phases):
            status = 'passed'
        else:
            status = 'missing'
        tests[nodeid] = {'status': status, 'started': nodeid in started,
                         'duration': sum(p['duration'] for p in phases),
                         'expected_failure': any('wasxfail' in p for p in phases)}
    return {'inventory': 'present', 'shard_index': inventory['shard_index'],
            'shard_count': inventory['shard_count'], 'collected': len(inventory['all']),
            'selected': len(inventory['selected']), 'counts': dict(Counter(t['status'] for t in tests.values())),
            'collection_errors': inventory['collection_errors'], 'session_finished': finished,
            'events_truncated': truncated, 'tests': tests, 'evidence_errors': errors}


def junit_status(path: Path | None):
    if path is None:
        return {'state': 'not_requested'}
    if not path.exists():
        return {'state': 'missing', 'path': str(path)}
    try:
        root = ET.parse(path).getroot()
        if root.tag not in ('testsuite', 'testsuites'):
            raise ValueError('not a JUnit testsuite document')
        return {'state': 'present', 'path': str(path), 'testcases': len(root.findall('.//testcase'))}
    except (OSError, ET.ParseError, ValueError) as exc:
        return {'state': 'invalid', 'path': str(path), 'error': str(exc)}


def run(command: list[str], directory: Path, timeout: float, *, junit: Path | None = None,
        grace: float = 5, pytest_required: bool = False) -> int:
    # Refuse reuse: a stale successful artifact must never qualify this invocation.
    directory.mkdir(parents=True, exist_ok=False)
    if junit is not None and junit.exists():
        raise ValueError('JUnit destination already exists')
    started = time.monotonic()
    result = {'status': 'running', 'command': command, 'cwd': str(Path.cwd()),
              'source': source_identity(), 'platform': platform.platform(),
              'python': sys.version, 'python_executable': sys.executable, 'tool_versions': tool_versions(),
              'versions': {name: importlib.metadata.version(name) for name in ('pytest', 'mkdocs', 'mcp')
                           if importlib.util.find_spec(name) is not None},
              'started_at': datetime.now(timezone.utc).isoformat(), 'timeout_seconds': timeout,
              'cleanup_signals': [], 'junit': junit_status(junit), 'pytest_required': pytest_required,
              'phase': 'starting'}
    write_json(directory / 'result.json', result)
    received = []
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda number, frame: received.append(number))
    child = None
    status, code = 'failed', 1
    try:
        environment = {**os.environ, 'AOS_CI_EVIDENCE_DIR': str(directory.resolve()), 'PYTHONUNBUFFERED': '1'}
        with (directory / 'command.log').open('wb', buffering=0) as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     env=environment, start_new_session=True)
            result.update(phase='command', child_pid=child.pid)
            write_json(directory / 'result.json', result)
            while child.poll() is None:
                if received or time.monotonic() - started >= timeout:
                    status = 'cancelled' if received else 'timed_out'
                    code = 128 + received[0] if received else 124
                    break
                time.sleep(.05)
            else:
                code = child.returncode
                status = 'passed' if code == 0 else 'cancelled' if code < 0 else 'failed'
                if code < 0:
                    code = 128 - code
            result.update(phase='cleanup', command_returncode=child.returncode, termination_reason=status)
            write_json(directory / 'result.json', result)
            # The session belongs solely to this invocation; terminate remaining
            # descendants even when the command leader already exited.
            for signum, delay in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    break
                result['cleanup_signals'].append(signal.Signals(signum).name)
                if delay:
                    # Keep the leader unreaped during the grace period so its
                    # PID/group cannot be recycled before escalation. Signal 0
                    # probes can be denied for an exiting macOS process.
                    until = time.monotonic() + delay
                    while time.monotonic() < until:
                        time.sleep(min(.05, max(0, until - time.monotonic())))
            child.wait(timeout=grace + 1)
            result['child_returncode'] = child.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
        if status == 'passed':
            status, code = 'failed', 1
        if child is not None:
            try:
                child.wait(timeout=grace + 1)
                result['child_returncode'] = child.returncode
            except subprocess.TimeoutExpired:
                result['cleanup_error'] = 'command leader did not exit after bounded cleanup'
    finally:
        if received:
            status, code = 'cancelled', 128 + received[0]
        result['source_after'] = source_identity()
        result['source_unchanged'] = result['source_after'] == result['source']
        result.update(status=status, exit_code=code, received_signals=received,
                      completed_at=datetime.now(timezone.utc).isoformat(),
                      duration_seconds=time.monotonic() - started, junit=junit_status(junit))
        try:
            summary = summarize_tests(directory, status)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summary = {'inventory': 'invalid', 'counts': {}, 'tests': {},
                       'evidence_errors': ['cannot read partial evidence: ' + str(exc)]}
        write_json(directory / 'tests.json', summary)
        # Missing/partial requested evidence is a failure even if a command
        # incorrectly returned success. Preserve the actual child status above.
        if status == 'passed' and (not result['source_unchanged'] or not result['source'].get('head') or
                result['junit']['state'] in ('missing', 'invalid') or
                (pytest_required and (summary['inventory'] != 'present' or not summary.get('selected'))) or
                summary.get('evidence_errors') or
                (summary['inventory'] == 'present' and
                 (summary['collection_errors'] or summary['events_truncated'] or
                  not summary['session_finished'] or summary['counts'].get('missing') or
                  summary['counts'].get('failed') or summary['counts'].get('cancelled')))):
            result.update(status='failed', exit_code=1, evidence_error='source identity changed/unavailable or required evidence incomplete/failed')
            code = 1
        if received:
            result.update(status='cancelled', exit_code=128 + received[0])
            code = 128 + received[0]
        result['phase'] = 'complete'
        write_json(directory / 'result.json', result)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        print(f"{result['status']}: {directory / 'result.json'}", flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--timeout', required=True, type=float)
    parser.add_argument('--junit', type=Path)
    parser.add_argument('--grace', type=float, default=5)
    parser.add_argument('--pytest-required', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not math.isfinite(args.timeout) or not math.isfinite(args.grace) or args.timeout <= 0 or args.grace < 0:
        parser.error('provide a command, positive timeout and nonnegative grace')
    return run(command, args.output, args.timeout, junit=args.junit, grace=args.grace, pytest_required=args.pytest_required)


if __name__ == '__main__':
    raise SystemExit(main())
