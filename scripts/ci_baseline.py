"""Select current failures for an attributable baseline replay; never waive regression failures."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


TERMINAL = {'passed', 'failed', 'skipped'}
EMPTY_DIFF = hashlib.sha256(b'').hexdigest()


def load(path):
    return json.loads(path.read_text())


def clean_source(result, expected=None):
    source = result['source']
    if (not re.fullmatch('[0-9a-f]{40}', source.get('head', '')) or source.get('dirty') is not False
            or source.get('tracked_diff_sha256') != EMPTY_DIFF or source.get('untracked_sha256') != {}
            or source.get('error') or result.get('source_unchanged') is not True
            or result.get('source_after') != source):
        raise ValueError('source identity missing, dirty or changed')
    if expected is not None and source['head'] != expected:
        raise ValueError('source does not match expected revision')
    return source


def completed(folder, expected=None):
    result, inventory, summary = [load(folder / name) for name in ('result.json', 'inventory.json', 'tests.json')]
    source = clean_source(result, expected)
    code = result.get('child_returncode')
    if (type(code) is not int or code not in (0, 1) or result.get('exit_code') != code
            or result.get('status') != ('passed' if code == 0 else 'failed')
            or result.get('phase') != 'complete' or result.get('pytest_required') is not True):
        raise ValueError('command did not complete normally as a pytest pass/failure')
    if (summary.get('inventory') != 'present' or summary.get('session_finished') is not True
            or summary.get('events_truncated') or summary.get('evidence_errors')
            or summary.get('collection_errors') != 0 or inventory.get('collection_errors') != 0):
        raise ValueError('incomplete pytest session or collection')
    all_ids, selected, outcomes = inventory['all'], inventory['selected'], summary['tests']
    for values in (all_ids, selected):
        if (not values or not all(isinstance(n, str) and n.startswith('tests/') and '::' in n for n in values)
                or len(values) != len(set(values))):
            raise ValueError('empty, duplicate or invalid node IDs')
    if set(selected) != set(outcomes) or not set(selected).issubset(all_ids):
        raise ValueError('result and selection inventories disagree')
    if any(t.get('status') not in TERMINAL for t in outcomes.values()):
        raise ValueError('nonterminal or missing test outcomes')
    counts = Counter(t['status'] for t in outcomes.values())
    if dict(counts) != summary.get('counts'):
        raise ValueError('reported outcome counts disagree')
    if (code == 0) != (counts['failed'] == 0):
        raise ValueError('command exit and test failure outcomes disagree')
    if result.get('junit', {}).get('state') != 'present':
        raise ValueError('missing JUnit receipt')
    tree = ET.parse(folder / 'junit.xml').getroot()
    if tree.tag not in ('testsuite', 'testsuites') or len(tree.findall('.//testcase')) < len(selected):
        raise ValueError('missing or incomplete JUnit cases')
    return result, inventory, summary, source


def bundle_identity(folder):
    """An evidence-index copy can repeat a bundle, but cannot change any file."""
    identity = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            value = json.dumps(load(path), sort_keys=True).encode() if path.suffix == '.json' else path.read_bytes()
            identity[path.name] = hashlib.sha256(value).hexdigest()
    return identity


def failure_details(folder):
    details = {}
    path = folder / 'events.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get('event') == 'report' and event.get('outcome') == 'failed':
                details.setdefault(event['nodeid'], str(event.get('detail', '')).splitlines()[0][:500]
                                   if event.get('detail') else '')
    return details


def select(artifacts, shards=8, candidate_ref=None):
    if shards < 1:
        raise ValueError('shard count must be positive')
    folders = {}
    for path in sorted(artifacts.rglob('result.json')):
        match = re.fullmatch(r'regression-(\d+)', path.parent.name)
        if match:
            index = int(match.group(1))
            if index >= shards or path.parent.name != f'regression-{index}':
                raise ValueError('unexpected regression shard')
            identity = bundle_identity(path.parent)
            if index in folders:
                if identity != folders[index][1]:
                    raise ValueError(f'conflicting duplicate artifact for shard {index}')
                continue
            folders[index] = (path.parent, identity)
    if set(folders) != set(range(shards)):
        raise ValueError('missing required regression shards')
    source, complete, seen, failures = None, None, set(), {}
    for index, (folder, _) in sorted(folders.items()):
        _, inventory, summary, current = completed(folder, candidate_ref)
        if source is None:
            source = current
        elif source != current:
            raise ValueError('inconsistent current source identities')
        all_ids, selected = set(inventory['all']), set(inventory['selected'])
        if complete is None:
            complete = all_ids
        elif complete != all_ids:
            raise ValueError('full regression inventories disagree')
        expected = {n for n in all_ids if int(hashlib.sha256(n.encode()).hexdigest(), 16) % shards == index}
        if selected != expected or seen.intersection(selected):
            raise ValueError('shard coverage is not the exact disjoint partition')
        for evidence in (inventory, summary):
            if evidence.get('shard_index') != index or evidence.get('shard_count') != shards:
                raise ValueError('wrong shard identity')
        seen.update(selected)
        details = failure_details(folder)
        failures.update({n: {'current_status': 'failed', 'current_detail': details.get(n)}
                         for n, outcome in summary['tests'].items() if outcome['status'] == 'failed'})
    if seen != complete:
        raise ValueError('executed union differs from complete regression inventory')
    return {'status': 'selected', 'current_source': source, 'shards': shards,
            'complete_regression_unique_tests': len(seen), 'failed_count': len(failures),
            'targets': sorted(failures), 'failures': failures,
            'selection_time': datetime.now(timezone.utc).isoformat(),
            'current_regression_status': 'failed' if failures else 'no_test_failures',
            'waiver': False}


def compare(targets, baseline, baseline_ref):
    report = {'status': 'incomplete', 'baseline_ref': baseline_ref,
              'current_source': targets.get('current_source'), 'current_regression_status': 'failed',
              'waiver': False, 'classifications': {}, 'errors': [],
              'limitation': 'Matching failed node IDs do not establish equivalent causes. Baseline replay counts are not added to complete regression counts.'}
    try:
        selected = targets['targets']
        if (targets.get('status') != 'selected' or not selected or len(selected) != len(set(selected))
                or targets.get('failed_count') != len(selected) or set(targets['failures']) != set(selected)):
            raise ValueError('invalid or empty current failed-target inventory')
        result, inventory, summary, source = completed(baseline, baseline_ref)
        report.update(baseline_source=source, baseline_execution_status=result['status'],
                      baseline_command=result.get('command'), baseline_duration_seconds=result.get('duration_seconds'))
        if set(inventory['selected']) != set(selected) or set(inventory['all']) != set(selected):
            raise ValueError('baseline selection does not exactly equal current failed targets')
        if any(e.get('shard_index') != 0 or e.get('shard_count') != 1 for e in (inventory, summary)):
            raise ValueError('baseline replay must run all selected cases without sharding')
        details = failure_details(baseline)
        names = {'failed': 'reproduced_failure', 'passed': 'baseline_passed', 'skipped': 'baseline_skipped'}
        for nodeid in selected:
            status = summary['tests'][nodeid]['status']
            report['classifications'][nodeid] = {'classification': names[status],
                'current_detail': targets['failures'][nodeid].get('current_detail'),
                'baseline_detail': details.get(nodeid)}
        report['counts'] = dict(Counter(r['classification'] for r in report['classifications'].values()))
        report['status'] = 'all_reproduced' if report['counts'].get('reproduced_failure') == len(selected) else 'not_all_reproduced'
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        report['errors'].append(str(exc))
        for nodeid in targets.get('targets', []):
            report['classifications'].setdefault(nodeid, {'classification': 'missing'})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    selection = commands.add_parser('select')
    selection.add_argument('--artifacts', type=Path, required=True)
    selection.add_argument('--shards', type=int, default=8)
    selection.add_argument('--candidate-ref')
    comparison = commands.add_parser('compare')
    comparison.add_argument('--targets', type=Path, required=True)
    comparison.add_argument('--baseline', type=Path, required=True)
    comparison.add_argument('--baseline-ref', required=True)
    for command in (selection, comparison):
        command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; choose a fresh evidence path')
    try:
        report = (select(args.artifacts, args.shards, args.candidate_ref) if args.operation == 'select'
                  else compare(load(args.targets), args.baseline, args.baseline_ref))
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        report = {'status': 'incomplete', 'errors': [str(exc)], 'waiver': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(args.output)
    print(json.dumps(report, indent=2))
    return int(report['status'] not in ('selected', 'all_reproduced'))


if __name__ == '__main__':
    raise SystemExit(main())
