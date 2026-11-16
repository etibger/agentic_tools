"""Coverage must be exhaustive, disjoint and backed by actual job conclusions."""
import json
from pathlib import Path
import tempfile
import unittest

from ci_aggregate import aggregate, shard_for


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ids = [f'tests/test_fixture.py::test_case[{i}]' for i in range(30)]
        self.needs = {n: {'result': 'success'} for n in ('regression', 'dashboard')}
        for index in range(2):
            selected = [n for n in self.ids if shard_for(n, 2) == index]
            self.write(f'regression-{index}', 'inventory.json', {'all': self.ids, 'selected': selected,
                'shard_index': index, 'shard_count': 2, 'collection_errors': 0})
            self.write(f'regression-{index}', 'tests.json', {'inventory': 'present',
                'shard_index': index, 'shard_count': 2, 'collection_errors': 0,
                'session_finished': True, 'events_truncated': False,
                'tests': {n: {'status': 'passed'} for n in selected}})
        for name in ('regression-0', 'regression-1', 'dashboard'):
            self.write(name, 'result.json', {'source': {'head': 'candidate', 'dirty': False},
                'status': 'passed', 'junit': {'state': 'present'},
                'source_after': {'head': 'candidate', 'dirty': False}, 'source_unchanged': True})

    def write(self, name, file, value):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        (folder / file).write_text(json.dumps(value))

    def change(self, name, file, mutate):
        path = self.root / name / file
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value))

    def check_failed(self, text):
        result = aggregate(self.root, 2, self.needs)
        self.assertEqual(result['status'], 'failed')
        self.assertIn(text, '\n'.join(result['errors']))

    def test_complete_unique_inventory(self):
        result = aggregate(self.root, 2, self.needs)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['unique_tests'], 30)
        self.assertEqual(result['counts']['passed'], 30)

    def test_missing_artifact_is_not_zero_failures(self):
        (self.root / 'regression-1/result.json').unlink()
        self.check_failed('missing result')

    def test_upload_does_not_override_cancelled_job(self):
        self.needs['regression']['result'] = 'cancelled'
        self.check_failed('GitHub job regression: cancelled')

    def test_skipped_job_is_distinct(self):
        self.needs['dashboard']['result'] = 'skipped'
        self.check_failed('GitHub job dashboard: skipped')

    def test_missing_test_result_fails(self):
        self.change('regression-0', 'tests.json', lambda t: t['tests'].pop(next(iter(t['tests']))))
        self.check_failed('result inventory differs')

    def test_duplicate_selection_fails(self):
        self.change('regression-0', 'inventory.json', lambda t: t['selected'].extend(t['selected'][:1]))
        self.check_failed('duplicate inventory')

    def test_changed_collection_fails(self):
        self.change('regression-0', 'inventory.json', lambda t: t['all'].append('tests/new.py::test_new'))
        self.check_failed('inventories are missing or disagree')

    def test_missing_junit_fails(self):
        self.change('regression-0', 'result.json', lambda t: t['junit'].update(state='missing'))
        self.check_failed('missing or invalid JUnit')

    def test_source_drift_fails(self):
        self.change('dashboard', 'result.json', lambda t: t['source'].update(head='other'))
        self.check_failed('inconsistent source')

    def test_changed_source_cannot_use_pass_receipt(self):
        self.change('dashboard', 'result.json', lambda t: t.update(source_unchanged=False))
        self.check_failed('source changed or final source identity missing')

    def test_partial_session_fails(self):
        self.change('regression-0', 'tests.json', lambda t: t.update(session_finished=False))
        self.check_failed('incomplete pytest session')

    def test_missing_authoritative_conclusion_fails(self):
        self.needs.clear()
        self.check_failed('conclusions are missing')

    def test_failed_tests_not_hidden_by_pass_receipt(self):
        self.change('regression-0', 'tests.json', lambda t: next(iter(t['tests'].values())).update(status='failed'))
        self.check_failed('1 tests failed')


if __name__ == '__main__':
    unittest.main()
