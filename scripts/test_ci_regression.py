"""Exercise command failures, real interruption, sharding and partial evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from ci_regression import summarize_tests

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/ci_regression.py'


class RegressionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = {**os.environ, 'PYTHONPATH': str(ROOT)}
        # Nested fixture runs must not append to the enclosing CI invocation.
        self.env.pop('AOS_CI_EVIDENCE_DIR', None)
        self.env.pop('AOS_CI_SHARD_INDEX', None)
        self.env.pop('AOS_CI_SHARD_COUNT', None)

    def command(self, name, command, timeout=10, extra=()):
        output = self.root / name
        argv = [sys.executable, str(RUNNER), '--output', str(output), '--timeout', str(timeout),
                '--grace', '.3', *extra, '--', *command]
        return output, argv

    def execute(self, name, code, **kwargs):
        output, argv = self.command(name, [sys.executable, '-c', code], **kwargs)
        process = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=15)
        return process, output, json.loads((output / 'result.json').read_text())

    def pytest_run(self, name, source, *, index=0, count=1, timeout=10):
        source_path = self.root / 'test_synthetic.py'
        source_path.write_text(source)
        output = self.root / name
        junit = output / 'junit.xml'
        command = [sys.executable, '-m', 'pytest', '-p', 'scripts.ci_pytest',
                   '-o', 'addopts=', '--junitxml=' + str(junit), str(source_path)]
        output, argv = self.command(name, command, timeout=timeout,
            extra=['--junit', str(junit), '--pytest-required'])
        env = {**self.env, 'AOS_CI_SHARD_INDEX': str(index), 'AOS_CI_SHARD_COUNT': str(count)}
        process = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
        return process, output, json.loads((output / 'result.json').read_text())

    def test_success_failure_and_no_stale_reuse(self):
        process, output, result = self.execute('pass', 'print("retained output", flush=True)')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result['status'], 'passed')
        self.assertIn('retained output', (output / 'command.log').read_text())
        self.assertEqual(len(result['source']['head']), 40)
        before = (output / 'result.json').read_bytes()
        _, argv = self.command('pass', [sys.executable, '-c', 'raise SystemExit(0)'])
        repeat = subprocess.run(argv, env=self.env, capture_output=True, timeout=15)
        self.assertNotEqual(repeat.returncode, 0)
        self.assertEqual((output / 'result.json').read_bytes(), before)
        process, _, result = self.execute('fail', 'raise SystemExit(7)')
        self.assertEqual(process.returncode, 7)
        self.assertEqual(result['status'], 'failed')

    def test_success_requires_requested_junit_and_inventory(self):
        output = self.root / 'missing-junit'
        process, _, result = self.execute('missing-junit', 'print("done")',
            extra=['--junit', str(output / 'junit.xml')])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result['junit']['state'], 'missing')
        self.assertEqual(result['child_returncode'], 0)
        process, _, result = self.execute('missing-inventory', 'print("done")', extra=['--pytest-required'])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result['status'], 'failed')

    def test_timeout_has_partial_log_and_bounded_exit(self):
        process, output, result = self.execute('timeout', 'import time; print("started", flush=True); time.sleep(30)', timeout=.3)
        self.assertEqual(process.returncode, 124)
        self.assertEqual(result['status'], 'timed_out')
        self.assertIn('started', (output / 'command.log').read_text())
        self.assertIn('SIGTERM', result['cleanup_signals'])
        self.assertLess(result['duration_seconds'], 4)

    def test_signal_cancels_only_own_group_and_reaps_child(self):
        outside = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: outside.poll() is None and (outside.terminate(), outside.wait(timeout=3)))
        output, argv = self.command('cancel', [sys.executable, '-c', 'import time; print("started",flush=True); time.sleep(30)'])
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                if (output / 'command.log').exists() and 'started' in (output / 'command.log').read_text():
                    break
                time.sleep(.02)
            else:
                self.fail('runner child never started')
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(process.returncode, 143)
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(result['received_signals'], [signal.SIGTERM])
            self.assertIsNotNone(result['child_returncode'])
            self.assertIsNone(outside.poll(), 'unrelated process was signalled')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_signal_during_post_success_cleanup_cannot_report_pass(self):
        outside = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: outside.poll() is None and (outside.terminate(), outside.wait(timeout=3)))
        descendant = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
        leader = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(descendant) + '])')
        output, argv = self.command('cancel-cleanup', [sys.executable, '-c', leader],
                                   extra=['--grace', '1'])
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                try:
                    receipt = json.loads((output / 'result.json').read_text())
                except FileNotFoundError:
                    receipt = {}
                if receipt.get('phase') == 'cleanup' and receipt.get('command_returncode') == 0:
                    break
                time.sleep(.01)
            else:
                self.fail('success-to-cleanup checkpoint was not reached')
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['child_returncode'], 0, 'leader succeeded before cancellation')
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(process.returncode, 143)
            self.assertIsNone(outside.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_uncooperative_child_is_killed_after_grace(self):
        code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'
        process, _, result = self.execute('kill', code, timeout=.4)
        self.assertEqual(process.returncode, 124)
        self.assertEqual(result['cleanup_signals'], ['SIGTERM', 'SIGKILL'])
        self.assertEqual(result['child_returncode'], -signal.SIGKILL)
        self.assertLess(result['duration_seconds'], 4)

    def test_pytest_reports_fail_skip_xfail_and_teardown_failure(self):
        source = '''import pytest
@pytest.fixture
def broken_teardown():
    yield
    raise AssertionError('teardown failure')
def test_pass(): pass
def test_fail(): assert False
@pytest.mark.skip(reason='synthetic skip')
def test_skip(): pass
@pytest.mark.xfail(reason='known expectation')
def test_xfail(): assert False
def test_teardown(broken_teardown): pass
'''
        process, output, result = self.pytest_run('outcomes', source)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['junit']['state'], 'present')
        tests = json.loads((output / 'tests.json').read_text())
        self.assertEqual(tests['counts'], {'passed': 1, 'failed': 2, 'skipped': 2})
        self.assertEqual(len(tests['tests']), 5, 'setup/call/teardown are one test, not three')
        self.assertTrue(tests['session_finished'])
        self.assertEqual(sum(t['expected_failure'] for t in tests['tests'].values()), 1)

    def test_timeout_distinguishes_finished_active_and_unstarted(self):
        source = '''import time
def test_a_pass(): pass
def test_b_hang(): time.sleep(30)
def test_c_unstarted(): pass
'''
        process, output, result = self.pytest_run('partial', source, timeout=1.5)
        self.assertEqual(process.returncode, 124)
        self.assertEqual(result['junit']['state'], 'missing')
        tests = json.loads((output / 'tests.json').read_text())
        self.assertEqual(tests['counts'], {'passed': 1, 'cancelled': 1, 'missing': 1})
        self.assertFalse(tests['session_finished'])
        self.assertEqual(tests['selected'], 3)

    def test_full_nodeid_shards_cover_exactly_once(self):
        source = 'import pytest\n@pytest.mark.parametrize("number",range(18))\ndef test_number(number): assert number >= 0\n'
        inventories = []
        selected = []
        for index in range(3):
            process, output, result = self.pytest_run('shard-' + str(index), source, index=index, count=3)
            self.assertEqual(process.returncode, 0, (process.stderr, result, (output / 'command.log').read_text()))
            inventory = json.loads((output / 'inventory.json').read_text())
            inventories.append(inventory['all'])
            selected.extend(inventory['selected'])
        self.assertTrue(all(inventory == inventories[0] for inventory in inventories))
        self.assertEqual(set(selected), set(inventories[0]))
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(len(selected), 18)

    def test_collection_error_and_empty_collection_do_not_pass(self):
        process, output, result = self.pytest_run('collection', 'raise RuntimeError("collection failed")\n')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('collection failed', (output / 'events.jsonl').read_text())
        process, _, result = self.pytest_run('empty', '')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result['status'], 'failed')

    def test_truncated_event_retains_completed_test_and_marks_evidence(self):
        process, output, _ = self.pytest_run('truncated', 'def test_pass(): pass\n')
        self.assertEqual(process.returncode, 0)
        with (output / 'events.jsonl').open('a') as events:
            events.write('{"partial":')
        summary = summarize_tests(output, 'cancelled')
        self.assertEqual(summary['counts'], {'passed': 1})
        self.assertTrue(summary['events_truncated'])


if __name__ == '__main__':
    unittest.main()
