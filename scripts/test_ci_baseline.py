"""Baseline attribution requires complete current coverage and an exact independent replay."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from ci_baseline import compare, select


CURRENT, BASELINE = 'a' * 40, 'b' * 40


def source(head):
    return {'head': head, 'dirty': False, 'tracked_diff_sha256': hashlib.sha256(b'').hexdigest(),
            'untracked_sha256': {}}


class BaselineEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / 'artifacts'
        self.baseline = self.root / 'baseline'
        self.ids = [f'tests/test_fixture.py::test_case[{i}]' for i in range(24)]
        self.failures = {self.ids[2], self.ids[9], self.ids[15]}
        for index in range(2):
            selected = [n for n in self.ids if int(hashlib.sha256(n.encode()).hexdigest(), 16) % 2 == index]
            self.bundle(self.artifacts / f'regression-{index}', self.ids, selected,
                        {n: 'failed' if n in self.failures else 'passed' for n in selected}, CURRENT, index, 2)

    def bundle(self, folder, all_ids, selected, statuses, head, index=0, shards=1):
        folder.mkdir(parents=True)
        failed = 'failed' in statuses.values()
        values = {
            'result.json': {'source': source(head), 'source_after': source(head), 'source_unchanged': True,
                'status': 'failed' if failed else 'passed', 'phase': 'complete', 'pytest_required': True,
                'child_returncode': int(failed), 'exit_code': int(failed), 'junit': {'state': 'present'},
                'command': ['python', '-m', 'pytest', *selected], 'duration_seconds': 1.0},
            'inventory.json': {'all': all_ids, 'selected': selected, 'shard_index': index,
                'shard_count': shards, 'collection_errors': 0},
            'tests.json': {'inventory': 'present', 'session_finished': True, 'events_truncated': False,
                'evidence_errors': [], 'collection_errors': 0, 'shard_index': index, 'shard_count': shards,
                'counts': dict(Counter(statuses.values())),
                'tests': {n: {'status': status} for n, status in statuses.items()}}}
        for name, value in values.items():
            (folder / name).write_text(json.dumps(value))
        (folder / 'junit.xml').write_text('<testsuites><testsuite>' + '<testcase/>' * len(selected) + '</testsuite></testsuites>')
        (folder / 'events.jsonl').write_text(''.join(json.dumps({'event': 'report', 'nodeid': n,
            'outcome': 'failed', 'detail': 'Synthetic assertion failure\nadditional detail'}) + '\n'
            for n, status in statuses.items() if status == 'failed'))

    def change(self, folder, name, mutate):
        path = folder / name
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value))

    def replay(self, statuses=None):
        selected = sorted(self.failures)
        self.bundle(self.baseline, selected, selected,
                    statuses or dict.fromkeys(selected, 'failed'), BASELINE)
        return compare(select(self.artifacts, 2, CURRENT), self.baseline, BASELINE)

    def test_exact_failed_union_and_identical_evidence_index_copy(self):
        expected = select(self.artifacts, 2, CURRENT)
        shutil.copytree(self.artifacts / 'regression-0', self.artifacts / 'index/aos-regression-0/regression-0')
        actual = select(self.artifacts, 2, CURRENT)
        self.assertEqual(actual['targets'], sorted(self.failures))
        self.assertEqual(actual['failed_count'], 3)
        self.assertEqual(actual['complete_regression_unique_tests'], 24)
        self.assertEqual(actual['targets'], expected['targets'])
        self.assertFalse(actual['waiver'])
        self.assertEqual(actual['current_source'], source(CURRENT))

    def test_conflicting_duplicate_artifact_is_rejected(self):
        duplicate = self.artifacts / 'index/regression-0'
        shutil.copytree(self.artifacts / 'regression-0', duplicate)
        self.change(duplicate, 'result.json', lambda r: r.update(duration_seconds=100))
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
            select(self.artifacts, 2)

    def test_missing_and_partial_shards_cannot_select_failures(self):
        self.change(self.artifacts / 'regression-0', 'tests.json', lambda s: s.update(session_finished=False))
        with self.assertRaisesRegex(ValueError, 'incomplete pytest'):
            select(self.artifacts, 2)
        shutil.rmtree(self.artifacts / 'regression-0')
        with self.assertRaisesRegex(ValueError, 'missing required'):
            select(self.artifacts, 2)

    def test_wrong_source_and_overlapping_coverage_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'expected revision'):
            select(self.artifacts, 2, BASELINE)
        folder = self.artifacts / 'regression-0'
        self.change(folder, 'result.json', lambda r: r['source'].update(dirty=True))
        with self.assertRaisesRegex(ValueError, 'dirty or changed'):
            select(self.artifacts, 2)
        self.change(folder, 'result.json', lambda r: r['source'].update(dirty=False))
        self.change(folder, 'inventory.json', lambda i: i['selected'].append(next(n for n in self.ids if n not in i['selected'])))
        with self.assertRaisesRegex(ValueError, 'inventories disagree'):
            select(self.artifacts, 2)

    def test_all_reproduced_retains_failed_regression_and_baseline_exit(self):
        report = self.replay()
        self.assertEqual(report['status'], 'all_reproduced')
        self.assertEqual(report['counts'], {'reproduced_failure': 3})
        self.assertEqual(report['baseline_execution_status'], 'failed')
        self.assertEqual(report['current_regression_status'], 'failed')
        self.assertFalse(report['waiver'])
        self.assertIn('do not establish equivalent causes', report['limitation'])
        self.assertTrue(all(r['baseline_detail'] == 'Synthetic assertion failure' for r in report['classifications'].values()))

    def test_baseline_passes_and_skips_remain_unreproduced(self):
        ids = sorted(self.failures)
        report = self.replay(dict(zip(ids, ['passed', 'skipped', 'failed'])))
        self.assertEqual(report['status'], 'not_all_reproduced')
        self.assertEqual(report['counts'], {'baseline_passed': 1, 'baseline_skipped': 1, 'reproduced_failure': 1})
        self.assertEqual(report['current_regression_status'], 'failed')

    def test_exact_replay_inventory_and_baseline_ref_required(self):
        self.replay()
        targets = select(self.artifacts, 2)
        report = compare(targets, self.baseline, CURRENT)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('expected revision', report['errors'][0])
        self.change(self.baseline, 'inventory.json', lambda i: i['all'].append(self.ids[0]))
        report = compare(targets, self.baseline, BASELINE)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('exactly equal', report['errors'][0])
        self.assertEqual({r['classification'] for r in report['classifications'].values()}, {'missing'})

    def test_partial_replay_and_missing_junit_cannot_qualify(self):
        self.replay()
        targets = select(self.artifacts, 2)
        self.change(self.baseline, 'tests.json', lambda s: s.update(events_truncated=True))
        report = compare(targets, self.baseline, BASELINE)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('incomplete pytest', report['errors'][0])
        self.change(self.baseline, 'tests.json', lambda s: s.update(events_truncated=False))
        (self.baseline / 'junit.xml').unlink()
        self.assertEqual(compare(targets, self.baseline, BASELINE)['status'], 'incomplete')
        (self.baseline / 'junit.xml').write_text('<testsuites><testsuite><testcase/></testsuite></testsuites>')
        report = compare(targets, self.baseline, BASELINE)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('incomplete JUnit', report['errors'][0])

    def test_cli_nonzero_for_baseline_pass_and_no_stale_output_reuse(self):
        self.replay(dict.fromkeys(self.failures, 'passed'))
        targets = self.root / 'targets.json'
        targets.write_text(json.dumps(select(self.artifacts, 2)))
        report = self.root / 'comparison.json'
        argv = [sys.executable, str(Path(__file__).with_name('ci_baseline.py')), 'compare',
                '--targets', str(targets), '--baseline', str(self.baseline), '--baseline-ref', BASELINE,
                '--output', str(report)]
        process = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(report.read_text())['status'], 'not_all_reproduced')
        saved = report.read_bytes()
        process = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(saved, report.read_bytes())


if __name__ == '__main__':
    unittest.main()
