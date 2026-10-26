"""Durable allowance identities; work/revision/restart never reset reservations."""
from __future__ import annotations

import json
from pathlib import Path

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


def reserve(db, budget: dict, recipe: dict) -> None:
    """Called in the same IMMEDIATE transaction that inserts the attempt."""
    budget_id = budget['budget_id']
    if recipe['max_seconds'] > budget['max_job_seconds']:
        raise Rejected('runtime_budget', 'Job exceeds the exact approved per-job allowance')
    old = db.execute('SELECT record FROM runtime_budget WHERE id=?', (budget_id,)).fetchone()
    identity = {k: budget[k] for k in ('budget_id', 'kind', 'max_assignments', 'max_seconds', 'max_job_seconds')}
    if budget['kind'] == 'product':
        identity.update(product_id=budget['product_id'], source=str(Path(recipe['source']).resolve()))
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
    if (len(attempts) >= budget['max_assignments']
            or sum(r.get('budget_seconds', 300) for r in attempts) + recipe['max_seconds'] > budget['max_seconds']):
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
