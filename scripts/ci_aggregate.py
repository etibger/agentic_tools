"""Reject incomplete regression evidence; job conclusions remain authoritative."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def shard_for(nodeid, count):
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), 'big') % count


def aggregate(root, shards, needs):
    errors, runs, inventories = [], {}, []
    totals = dict.fromkeys(('passed', 'failed', 'skipped', 'cancelled', 'missing'), 0)
    seen, source = set(), None
    for path in sorted(root.rglob('result.json')):
        try:
            result = json.loads(path.read_text())
            name = path.parent.name
            if name in runs:
                raise ValueError('duplicate result directory ' + name)
            runs[name] = result
        except (ValueError, OSError) as exc:
            errors.append(f'{path}: {exc}')
    for name in [*(f'regression-{i}' for i in range(shards)), 'dashboard']:
        result = runs.get(name)
        if result is None:
            errors.append(f'{name}: missing result')
            continue
        if result.get('source_unchanged') is not True or result.get('source_after') != result.get('source'):
            errors.append(f'{name}: source changed or final source identity missing')
        if result.get('status') != 'passed':
            errors.append(f'{name}: command {result.get("status", "missing")}')
        identity = result.get('source')
        if not identity or not identity.get('head'):
            errors.append(f'{name}: missing source identity')
        elif source is None:
            source = identity
        elif identity != source:
            errors.append(f'{name}: inconsistent source identity')
    for index in range(shards):
        name = f'regression-{index}'
        paths = [p for p in root.rglob('result.json') if p.parent.name == name]
        if len(paths) != 1:
            continue
        folder = paths[0].parent
        try:
            inventory = json.loads((folder / 'inventory.json').read_text())
            tests = json.loads((folder / 'tests.json').read_text())
            all_ids, selected = inventory['all'], inventory['selected']
            if len(set(all_ids)) != len(all_ids) or len(set(selected)) != len(selected):
                raise ValueError('duplicate inventory node IDs')
            if not all_ids or not selected:
                raise ValueError('empty inventory or selection')
            inventories.append(set(all_ids))
            expected = {n for n in all_ids if shard_for(n, shards) == index}
            if set(selected) != expected:
                errors.append(f'{name}: selection does not match deterministic partition')
            for evidence in (inventory, tests):
                if evidence.get('shard_index') != index or evidence.get('shard_count') != shards:
                    errors.append(f'{name}: wrong shard identity')
                if evidence.get('collection_errors') != 0:
                    errors.append(f'{name}: collection errors or missing collection result')
            if tests.get('inventory') != 'present' or not tests.get('session_finished') or tests.get('events_truncated') or tests.get('evidence_errors'):
                errors.append(f'{name}: incomplete pytest session')
            outcomes = tests['tests']
            if set(outcomes) != set(selected):
                errors.append(f'{name}: result inventory differs from selected inventory')
            for nodeid in selected:
                if nodeid in seen:
                    errors.append(f'{name}: duplicate execution of {nodeid}')
                seen.add(nodeid)
                status = outcomes.get(nodeid, {}).get('status', 'missing')
                if status not in totals:
                    errors.append(f'{name}: unknown test status {status}')
                    status = 'missing'
                totals[status] += 1
            if runs[name].get('junit', {}).get('state') != 'present':
                errors.append(f'{name}: missing or invalid JUnit')
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(f'{name}: invalid or missing evidence: {exc}')
    if len(inventories) != shards or any(i != inventories[0] for i in inventories[1:]):
        errors.append('full test inventories are missing or disagree')
    if inventories and seen != inventories[0]:
        errors.append('executed union does not equal the complete inventory')
    for status in ('failed', 'cancelled', 'missing'):
        if totals[status]:
            errors.append(f'{totals[status]} tests {status}')
    # The matrix job result comes from GitHub, separately from uploaded receipts.
    if set(needs) != {'regression', 'dashboard'}:
        errors.append('required GitHub job conclusions are missing or unexpected')
    for name, job in needs.items():
        if not isinstance(job, dict) or job.get('result') != 'success':
            errors.append(f'GitHub job {name}: {job.get("result", "missing") if isinstance(job, dict) else "invalid"}')
    expected_names = {*(f'regression-{i}' for i in range(shards)), 'dashboard'}
    if set(runs) != expected_names:
        errors.append('unexpected or missing command receipts')
    return {'status': 'failed' if errors else 'passed', 'source': source,
            'unique_tests': len(seen), 'counts': totals, 'jobs': needs,
            'commands': {n: r.get('status', 'missing') for n, r in runs.items()}, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--shards', type=int, required=True)
    args = parser.parse_args()
    try:
        needs = json.loads(os.environ.get('AOS_CI_NEEDS', '{}'))
        if not isinstance(needs, dict) or args.shards < 1:
            raise ValueError('invalid job conclusions or shard count')
        result = aggregate(args.artifacts, args.shards, needs)
    except (ValueError, TypeError) as exc:
        result = {'status': 'failed', 'errors': [str(exc)]}
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / 'aggregate.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as output:
            output.write('AOS evidence: ' + result['status'] + '\n\n')
            output.write('```json\n' + json.dumps(result, indent=2) + '\n```\n')
    return int(result['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
