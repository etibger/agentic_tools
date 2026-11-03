"""Durable allowance identities; work/revision/restart never reset reservations."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .store import Rejected, canonical

QUALIFICATION_ID = 'aos-q2q3-runtime-v1'


def approved_budget(bounds: dict, directory: Path) -> dict:
    qualification = bounds.get('shared_qualification_budget')
    product = bounds.get('product_budget')
    if bool(qualification) == bool(product):
        raise Rejected('runtime_budget', 'Exactly one explicit qualification or product allowance is required')
    budget = dict(qualification or product)
    if (budget.get('ledger_directory') != str(directory.resolve())
            or any(type(budget.get(k)) is not int or budget[k] < 1 for k in ('max_assignments', 'max_seconds'))):
        raise Rejected('runtime_budget', 'Exact approved budget limits and authoritative ledger binding required')
    if qualification:
        if (budget.get('budget_id') != QUALIFICATION_ID or budget['max_assignments'] > 10 or budget['max_seconds'] > 3000):
            raise Rejected('runtime_budget', 'Shared qualification allowance exceeds the supported ceiling')
        budget.update(kind='qualification', max_job_seconds=300)
    else:
        if (not isinstance(budget.get('budget_id'), str) or not budget['budget_id'].strip()
                or budget['budget_id'] == QUALIFICATION_ID
                or not isinstance(budget.get('product_id'), str) or not budget['product_id'].strip()
                or type(budget.get('max_job_seconds')) is not int or budget['max_job_seconds'] < 1
                or budget['max_job_seconds'] > budget['max_seconds']):
            raise Rejected('runtime_budget', 'Product work requires an explicit stable identity and per-job allowance')
        budget['kind'] = 'product'
    return budget


def initialize(db) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS runtime_budget (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS product_budget_scope (scope TEXT PRIMARY KEY, budget_id TEXT NOT NULL)')


def attempts_for(db, budget_id: str) -> list[dict]:
    # Pre-migration records have no budget_id. They always belong to the original
    # shared allowance, including failed intents and missing duration metadata.
    return [record for row in db.execute('SELECT record FROM attempt')
            if (record := json.loads(row[0])).get('budget_id', QUALIFICATION_ID) == budget_id]


def finish_capacity(budget: dict, *, max_repairs: int, max_seconds: int,
                    max_assignments: int, attempts=(), lifecycle_assignments: int = 0,
                    max_startup_retries: int = 0, reviewed_host: bool = False) -> dict:
    """Pure prospective admission check; no reservations or mutable state.

    Each correction needs a repair and its independent reassessment. Reviewed host
    validation also keeps one independent failure-triage slot per correction. Explicit
    startup retries add separate charged slots; they never consume review or triage capacity. Retained
    failed/ambiguous attempts consume their original full reservations. Missing
    legacy duration uses the same conservative 300-second charge as reserve().
    The caller passes all budget attempts, or an already budget-filtered sequence.
    Invalid/unavailable facts return ready=False rather than assuming capacity.
    """
    receipt = {'budget_id': budget.get('budget_id') if isinstance(budget, dict) else None,
               'ready': False, 'blockers': [], 'required_assignments': None,
               'required_seconds': None, 'remaining_assignments': None,
               'remaining_seconds': None, 'lifecycle_remaining_assignments': None,
               'attempts_reserved': None, 'seconds_reserved': None,
               'reservation_performed': False}
    if (not isinstance(budget, dict)
            or any(type(budget.get(k)) is not int or budget[k] < 1
                   for k in ('max_assignments', 'max_seconds', 'max_job_seconds'))
            or not isinstance(budget.get('budget_id'), str) or not budget['budget_id']
            or type(max_repairs) is not int or not 0 <= max_repairs <= 2
            or type(max_startup_retries) is not int or not 0 <= max_startup_retries <= 2
            or type(reviewed_host) is not bool
            or type(max_seconds) is not int or max_seconds < 1
            or type(max_assignments) is not int or max_assignments < 1
            or type(lifecycle_assignments) is not int or lifecycle_assignments < 0
            or not isinstance(attempts, (list, tuple))
            or any(not isinstance(item, dict) for item in attempts)):
        receipt['blockers'].append('Invalid finish-capacity limits or retained accounting')
        return receipt
    retained = [item for item in attempts
                if item.get('budget_id', QUALIFICATION_ID) == budget['budget_id']]
    if any(type(item.get('budget_seconds', 300)) is not int
           or item.get('budget_seconds', 300) < 1 for item in retained):
        receipt['blockers'].append('Retained attempt duration is invalid; capacity is unknown')
        return receipt
    required = 2 + (3 if reviewed_host else 2) * max_repairs + max_startup_retries
    seconds = sum(item.get('budget_seconds', 300) for item in retained)
    receipt.update(required_assignments=required, required_seconds=required * max_seconds,
                   remaining_assignments=budget['max_assignments'] - len(retained),
                   remaining_seconds=budget['max_seconds'] - seconds,
                   lifecycle_remaining_assignments=max_assignments - lifecycle_assignments,
                   attempts_reserved=len(retained), seconds_reserved=seconds)
    if max_seconds > budget['max_job_seconds']:
        receipt['blockers'].append('Job ceiling exceeds the approved per-job allowance')
    work = ('implementation, assessment, corrections'
            + (', independent failure triage' if reviewed_host else '') + ' and startup retries')
    if receipt['lifecycle_remaining_assignments'] < required:
        receipt['blockers'].append('Lifecycle assignment capacity cannot cover ' + work)
    if receipt['remaining_assignments'] < required:
        receipt['blockers'].append('Shared attempt capacity cannot cover ' + work)
    if receipt['remaining_seconds'] < receipt['required_seconds']:
        receipt['blockers'].append('Shared reserved-time capacity cannot cover ' + work)
    receipt['ready'] = not receipt['blockers']
    return receipt


def _budget_identity(budget: dict, source: str | None) -> dict:
    identity = {k: budget[k] for k in ('budget_id', 'kind', 'max_assignments', 'max_seconds', 'max_job_seconds')}
    if budget['kind'] == 'product':
        identity.update(product_id=budget['product_id'], source=str(Path(source).resolve()))
    return identity


def read_finish_capacity(path: Path, budget: dict, *, source: str, max_repairs: int,
                         max_seconds: int, max_assignments: int,
                         lifecycle_assignments: int = 0, max_startup_retries: int = 0,
                         reviewed_host: bool = False) -> dict:
    """Read existing accounting without constructing a product runtime or charging it.

    An absent runtime journal means no reservations there; it is never created.
    Existing product/source identities and limits must match before reporting ready.
    Unavailable/corrupt accounting returns a hold. Atomic reserve() remains the
    final concurrency check, so this receipt is evidence, not a reservation.
    """
    scope = dict(max_repairs=max_repairs, max_seconds=max_seconds,
                 max_assignments=max_assignments, lifecycle_assignments=lifecycle_assignments,
                 max_startup_retries=max_startup_retries, reviewed_host=reviewed_host)
    receipt = finish_capacity(budget, **scope)
    if receipt['required_assignments'] is None:
        return receipt
    path = Path(path)
    db = None
    try:
        expected = Path(budget['ledger_directory']).resolve() / 'codex-runtime.sqlite3'
        if not isinstance(source, str) or not Path(source).is_absolute():
            raise ValueError('Finish capacity requires an absolute product source')
        identity = _budget_identity(budget, source)
        if path.is_symlink() or path.resolve() != expected:
            raise ValueError('Runtime accounting path differs from the approved ledger')
        if not path.exists():
            return receipt
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.05)
        db.execute('BEGIN')
        saved = db.execute('SELECT record FROM runtime_budget WHERE id=?', (budget['budget_id'],)).fetchone()
        if saved and budget['kind'] == 'product' and json.loads(saved[0]) != identity:
            raise ValueError('Retain the original product identity and limits')
        if budget['kind'] == 'product':
            for key in ('product:' + identity['product_id'], 'source:' + identity['source']):
                row = db.execute('SELECT budget_id FROM product_budget_scope WHERE scope=?', (key,)).fetchone()
                if row and row[0] != budget['budget_id']:
                    raise ValueError('Product/source already has a durable allowance; renaming cannot reset it')
        attempts = attempts_for(db, budget['budget_id'])
        if attempts and not saved and budget['kind'] == 'product':
            raise ValueError('Retained product attempts have no durable allowance identity')
        receipt = finish_capacity(budget, attempts=attempts, **scope)
        return receipt
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as error:
        receipt['ready'] = False
        receipt['blockers'].append('Finish-capacity accounting unavailable: ' + str(error))
        return receipt
    finally:
        if db is not None:
            db.close()


def reserve(db, budget: dict, recipe: dict, *, following_jobs: int = 0) -> None:
    """Charge only this attempt, atomically retaining enough space for following jobs.

    The caller must hold the same IMMEDIATE transaction that inserts the attempt.
    following_jobs=0 preserves legacy charging. Headroom is checked, not reserved;
    no other attempt, budget extension or refund is created by this function.
    """
    if type(following_jobs) is not int or following_jobs < 0:
        raise Rejected('runtime_capacity', 'Following-job count must be a nonnegative integer')
    budget_id = budget['budget_id']
    if recipe['max_seconds'] > budget['max_job_seconds']:
        raise Rejected('runtime_budget', 'Job exceeds the exact approved per-job allowance')
    old = db.execute('SELECT record FROM runtime_budget WHERE id=?', (budget_id,)).fetchone()
    identity = _budget_identity(budget, recipe.get('source'))
    if budget['kind'] == 'product':
        for scope in ('product:' + identity['product_id'], 'source:' + identity['source']):
            saved = db.execute('SELECT budget_id FROM product_budget_scope WHERE scope=?', (scope,)).fetchone()
            if saved and saved[0] != budget_id:
                raise Rejected('runtime_budget', 'Product/source already has a durable allowance; renaming cannot reset it')
            db.execute('INSERT OR IGNORE INTO product_budget_scope VALUES(?,?)', (scope, budget_id))
    if old:
        previous = json.loads(old[0])
        # Product limits cannot reset through a new approval/revision. A future
        # explicit extension protocol is outside this small runtime interface.
        if previous != identity and budget['kind'] != 'qualification':
            raise Rejected('runtime_budget', 'Retain the original durable product identity and limits')
        if budget['kind'] == 'qualification':
            # An older approved package may still use lower limits. Keep the
            # largest exact-approved ceiling without altering any reservations.
            identity['max_assignments'] = max(previous['max_assignments'], identity['max_assignments'])
            identity['max_seconds'] = max(previous['max_seconds'], identity['max_seconds'])
    attempts = attempts_for(db, budget_id)
    if following_jobs and any(type(item.get('budget_seconds', 300)) is not int
                              or item.get('budget_seconds', 300) < 1 for item in attempts):
        raise Rejected('runtime_capacity', 'Retained duration is invalid; following-job capacity is unknown')
    if (len(attempts) + 1 + following_jobs > budget['max_assignments']
            or sum(r.get('budget_seconds', 300) for r in attempts)
            + (1 + following_jobs) * recipe['max_seconds'] > budget['max_seconds']):
        if following_jobs:
            raise Rejected('runtime_capacity', 'Current job and required following jobs exceed remaining attempt/time capacity')
        raise Rejected('runtime_budget', 'Approved attempt/time reservation exhausted; no reset or launch')
    db.execute('INSERT INTO runtime_budget VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
               (budget_id, canonical(identity)))


def receipt(db, directory: Path, budget_id: str = QUALIFICATION_ID) -> dict:
    attempts = attempts_for(db, budget_id)
    row = db.execute('SELECT record FROM runtime_budget WHERE id=?', (budget_id,)).fetchone()
    limits = json.loads(row[0]) if row else None
    return {'budget_id': budget_id, 'ledger_directory': str(directory.resolve()),
            'attempts_reserved': len(attempts), 'seconds_reserved': sum(r.get('budget_seconds', 300) for r in attempts),
            'limits': limits,
            'accounting': 'failed/ambiguous attempts retain one slot and full time reservation; never reset per work or stage',
            'actual_billing': 'unavailable'}
