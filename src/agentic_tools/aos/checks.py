"""Generic evidence boundary. Interpreters never grant lifecycle authority."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentic_tools import check_adapters
from .store import Rejected, digest


def source_manifest() -> dict[str, str]:
    root = Path(__file__).parent.parent
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for package in ('aos', 'check_adapters') for p in sorted((root / package).rglob('*.py'))}


def source_digest() -> str:
    return digest(source_manifest())


def adapter_for(check: dict):
    try:
        return check_adapters.select(check)
    except (ValueError, TypeError) as error:
        raise Rejected('check_adapter', 'Unknown or inconsistent trusted check adapter') from error


def validate_normalized(evidence: dict) -> None:
    if (not isinstance(evidence, dict) or evidence.get('kind') not in ('build', 'tests')
            or type(evidence.get('recognized')) is not bool
            or any(type(evidence.get(k)) is not int or evidence[k] < 0
                   for k in ('selected', 'passed', 'failed', 'skipped', 'errors'))):
        raise Rejected('check_evidence', 'Adapter returned invalid normalized evidence')
    if evidence['kind'] == 'build':
        valid = all(evidence[k] == 0 for k in ('selected', 'passed', 'failed', 'skipped', 'errors'))
    else:
        valid = evidence['selected'] == sum(evidence[k] for k in ('passed', 'failed', 'skipped', 'errors'))
    if not valid:
        raise Rejected('check_evidence', 'Normalized result counts disagree')
    if (not isinstance(evidence.get('artifacts', []), list) or
            not all(isinstance(p, str) and p for p in evidence.get('artifacts', []))):
        raise Rejected('check_evidence', 'Adapter artifact references must be paths')


def passed(evidence: dict, exit_code: int | None) -> bool:
    validate_normalized(evidence)
    return (evidence['recognized'] and type(exit_code) is int and exit_code == 0 and
            (evidence['kind'] == 'build' or evidence['selected'] > 0 and
             evidence['passed'] == evidence['selected']))


def _protected(path: Path, writable_roots: tuple[Path, ...]) -> bool:
    return not any(path.resolve().is_relative_to(root.resolve()) for root in writable_roots)


def bindings(store, work_id: str, package: dict, recipe: dict) -> dict[str, dict]:
    """Bind code to exact approval, or its explicitly delegated technical gate.

    Historical untyped recipes keep their single original interpreter. Typed
    recipes require approved adapter identities, or a pinned independent receipt
    recorded through an already approved prerequisite. Reading a file/flag alone
    never creates that prerequisite or approves work.
    """
    roots = (*store.worker_writable_roots, Path(recipe['source']), Path(recipe['outputs']))
    if any(p.is_symlink() or not _protected(p, roots) for p in check_adapters.code_paths()):
        raise Rejected('check_adapter', 'Trusted adapter code overlaps a worker-writable root')
    selected = {adapter_for(check).ADAPTER_ID for check in recipe['checks']}
    current = {name: check_adapters.identities()[name] for name in sorted(selected)}
    bounds = package['execution_envelope']
    if all('result_kind' not in c and 'adapter_id' not in c for c in recipe['checks']):
        return {name: {**value, 'binding': 'historical-untyped-recipe', 'concept_digest': digest(package)}
                for name, value in current.items()}
    approved = bounds.get('check_adapters')
    if approved is not None:
        if approved != current:
            raise Rejected('check_adapter', 'Adapter identity/version differs from the exact approved package')
        return {name: {**value, 'binding': 'approved-package', 'concept_digest': digest(package)}
                for name, value in current.items()}
    allowed = bounds.get('adapter_architecture', {}).get('minimum_adapters', [])
    if not selected.issubset(set(allowed)):
        raise Rejected('check_adapter', 'Approved package does not delegate these adapter identities')
    # A receipt is meaningful only when its digest was recorded with a satisfied
    # prerequisite already named by the approved package. The trusted coordinator
    # owns this operation; workers cannot write the event or receipt.
    with store._connection() as db:
        row = db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
        contract = json.loads(row[0]) if row else {}
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='prerequisites_reconciled' ORDER BY event_id DESC", (work_id,))]
    latest = {}
    for event in events:
        for key, observation in event['evidence'].items():
            latest.setdefault(key, observation)
    for key, observation in latest.items():
        reference = observation.get('reference', '')
        if not reference.startswith('aos-check-adapters-v1:'):
            continue
        if (key not in bounds.get('prerequisites', {}) or contract.get('prerequisites', {}).get(key) is not True
                or observation.get('satisfied') is not True):
            continue
        try:
            _, expected_hash, filename = reference.split(':', 2)
            path = Path(filename)
            if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
                    or not _protected(path, roots) or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024):
                raise ValueError('Unprotected qualification receipt')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError('Qualification receipt bytes changed')
            receipt = json.loads(raw)
            team = {m['name'] for m in package['team_envelope']}
            if (receipt.get('format') != 'aos-check-adapters-v1' or receipt.get('accepted') is not True
                    or receipt.get('independent') is not True or receipt.get('work_id') != work_id
                    or receipt.get('concept_digest') != digest(package) or receipt.get('runtime_recipe_digest') != digest(recipe)
                    or receipt.get('source_digest') != source_digest() or receipt.get('check_adapters') != current
                    or receipt.get('assessor') not in team or receipt.get('source_author') not in team
                    or receipt['assessor'] == receipt['source_author'] or receipt['assessor'] == bounds['writer']):
                raise ValueError('Qualification is not exact and independent')
            return {name: {**value, 'binding': reference, 'concept_digest': digest(package)} for name, value in current.items()}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Rejected('check_adapter', 'Independent adapter qualification receipt is missing, changed or mismatched') from error
    raise Rejected('check_adapter', 'Typed checks require exact approved or independently qualified adapter identities')


def check_kind(contract: dict, name: str) -> str:
    """An approved build obligation is distinct from a test obligation."""
    descriptors = contract.get('runtime_recipe', {}).get('checks', [])
    descriptor = next((check for check in descriptors if check.get('name') == name), None)
    return adapter_for(descriptor).KIND if descriptor else contract.get('check_kinds', {}).get(name, 'tests')
