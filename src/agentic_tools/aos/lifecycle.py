"""Q1 shared, synthetic-only lifecycle. This is not a live runtime adapter.

Only the trusted coordinator/worker opens this store. Assigned agents must never
receive its connection or decision capability. Runtime calls occur outside SQL
transactions; stable attempt IDs and reconciliation contain uncertain launches.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from datetime import datetime, timezone

from .store import Rejected, Store, canonical, digest, identifier, now
from .checks import adapter_for, bindings, check_kind, validate_normalized, passed as evidence_passed


SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle (
 work_id TEXT PRIMARY KEY REFERENCES work(work_id), contract TEXT NOT NULL,
 execution TEXT NOT NULL DEFAULT 'held', control TEXT NOT NULL DEFAULT 'none',
 admitted INTEGER NOT NULL DEFAULT 0, cycle INTEGER NOT NULL DEFAULT 0,
 candidate TEXT, delivery TEXT, resume_execution TEXT);
CREATE TABLE IF NOT EXISTS assignment (
 assignment_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(work_id),
 predecessor TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
 contract_revision INTEGER NOT NULL, concept_revision INTEGER NOT NULL,
 concept_digest TEXT NOT NULL, candidate TEXT, executor TEXT NOT NULL,
 requested_model TEXT NOT NULL, requested_effort TEXT NOT NULL, scope TEXT NOT NULL,
 runtime_model TEXT, runtime_effort TEXT, attempt_id TEXT UNIQUE NOT NULL,
 created_at TEXT NOT NULL, eligible_at TEXT NOT NULL, sent_at TEXT,
 acknowledged_at TEXT, observed_active_at TEXT, terminal_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS outbox (
 assignment_id TEXT PRIMARY KEY REFERENCES assignment(assignment_id),
 state TEXT NOT NULL, epoch INTEGER, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource (
 resource_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, assignment_id TEXT,
 state TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS completion (
 receipt_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 result TEXT NOT NULL, disposition TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lifecycle_event (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL,
 kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS technical_blocker (
 work_id TEXT NOT NULL, code TEXT NOT NULL, owner TEXT NOT NULL,
 dependency TEXT NOT NULL, detail TEXT NOT NULL, next_action TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(work_id,code));
CREATE TABLE IF NOT EXISTS projection (
 work_id TEXT PRIMARY KEY, source_revision INTEGER NOT NULL,
 source_digest TEXT NOT NULL, snapshot TEXT NOT NULL, collected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owned_stop (
 attempt_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, reason TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, observation TEXT, observed_at TEXT);
CREATE TABLE IF NOT EXISTS worker_status (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL,
 owner TEXT NOT NULL, heartbeat TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_observation (
 attempt_id TEXT PRIMARY KEY REFERENCES assignment(attempt_id), observation TEXT NOT NULL,
 observed_at TEXT NOT NULL);
"""


TERMINAL_EXECUTIONS = frozenset({'cancelled', 'delivered'})
CANCELLATION_CONTROLS = frozenset({'cancel_requested', 'reconciling', 'blocked_unknown_effects', 'cancelled'})


def partial_mutations_known(observation: dict) -> bool:
    """An explicit no-mutation answer is evidence; absent/unknown is not."""
    value = observation.get('partial_mutations')
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() not in {
            'unknown', 'unavailable', 'unreconciled', 'pending', 'not observed', 'not checked'}
    if isinstance(value, dict):
        if value.get('status') == 'none':
            return not value.get('changes')
        if value.get('status') == 'recorded':
            detail = value.get('detail')
            changes = value.get('changes')
            return ((isinstance(detail, str) and partial_mutations_known({'partial_mutations': detail}))
                    or (isinstance(changes, list) and bool(changes)
                        and all(isinstance(change, str) and partial_mutations_known({'partial_mutations': change}) for change in changes)))
        return False
    return False


def stopped_with_effects(observation: dict) -> bool:
    return (observation.get('stopped') is True and observation.get('owned_processes') == []
            and observation.get('effects_reconciled') is True and partial_mutations_known(observation))


def stop_conflicts_in(db: sqlite3.Connection, work_id: str) -> list[dict]:
    """Derive current obligations from the retained conflict/resolution journal.

    Reading the original events also protects stores created before conflicts
    participated in cancellation ownership. An assignment's terminal label and
    an earlier stopped receipt cannot discharge a later contradiction.
    """
    pending = {}
    for event in db.execute("""SELECT kind,detail FROM lifecycle_event WHERE work_id=?
            AND kind IN ('owned_stop_conflict','owned_stop_conflict_resolved') ORDER BY event_id""", (work_id,)):
        detail = json.loads(event['detail'])
        if event['kind'] == 'owned_stop_conflict':
            conflict_id = digest(detail)
            pending[conflict_id] = {'conflict_id': conflict_id,
                                    'attempt_id': detail['retained']['attempt_id'], **detail}
        else:
            for conflict_id in detail['conflict_ids']:
                pending.pop(conflict_id, None)
    return list(pending.values())


def cancellation_assignments_in(db: sqlite3.Connection, work_id: str) -> list[dict]:
    conflicted = {c['attempt_id'] for c in stop_conflicts_in(db, work_id)}
    return [dict(a) for a in db.execute('SELECT * FROM assignment WHERE work_id=? ORDER BY created_at,assignment_id', (work_id,))
            if a['state'] not in ('completed', 'failed', 'cancelled') or a['attempt_id'] in conflicted]


def assignment_scope(contract: dict, kind: str) -> dict:
    return {'authority': contract['authority'], 'baseline': contract['baseline'],
            'source_access': 'read-only' if kind == 'assessment' else 'write',
            'resources': contract['resources'], 'max_seconds': contract['max_seconds'],
            'deliverable': 'independent execution and review reports' if kind == 'assessment' else 'frozen candidate and factual outputs',
            'stop_condition': 'unmet gate, authority change, source drift, unknown effects or exhausted limit',
            'required_checks': contract['required_checks'] + contract.get('compatibility_checks', [])}


def _concept_decision(db: sqlite3.Connection, work: sqlite3.Row) -> str:
    row = db.execute("""SELECT outcome FROM decision WHERE work_id=? AND concept_revision=?
        AND outcome IN ('approved','declined','cancelled') ORDER BY created_at DESC LIMIT 1""",
                     (work['work_id'], work['current_concept'])).fetchone()
    return row[0] if row else 'pending'


def snapshot_in(db: sqlite3.Connection, work_id: str) -> dict:
    """One source snapshot used identically by status and the dashboard collector."""
    work = db.execute('SELECT * FROM work WHERE work_id=?', (work_id,)).fetchone()
    run = db.execute('SELECT * FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
    blockers = [dict(r) for r in db.execute('SELECT * FROM technical_blocker WHERE work_id=? ORDER BY code', (work_id,))]
    if not any(b['code'] == 'decision_readback' for b in blockers):
        for failed in db.execute("""SELECT r.* FROM decision_readback r JOIN decision d ON d.decision_id=r.decision_id
            WHERE d.work_id=? AND r.status='blocked'""", (work_id,)):
            blockers.append({'work_id': work_id, 'code': 'decision_readback', 'owner': 'coordinator',
                             'dependency': 'dispatch', 'detail': failed['detail'],
                             'next_action': 'Read back the same durable decision; do not request approval again',
                             'observed_at': failed['observed_at']})
    assignments = [dict(r) for r in db.execute('SELECT * FROM assignment WHERE work_id=? ORDER BY created_at,assignment_id', (work_id,))]
    for assignment in assignments:
        for key in ('candidate', 'result'):
            assignment[key] = json.loads(assignment[key]) if assignment[key] else None
    decision = _concept_decision(db, work)
    pending = [dict(r) for r in db.execute("""SELECT challenge_id,concept_revision,digest,created_at
        FROM challenge WHERE work_id=? AND concept_revision=? AND work_revision=? AND status='pending'""",
        (work_id, work['current_concept'], work['revision']))] if decision == 'pending' else []
    runtime_observations = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_observation'").fetchone():
        runtime_observations = [json.loads(r[0]) for r in db.execute('''SELECT o.observation FROM runtime_observation o
            JOIN assignment a ON a.attempt_id=o.attempt_id WHERE a.work_id=?''', (work_id,))]
    usage = {'model_calls': 0, 'cost': None, 'billing': 'unavailable', 'coverage': 'simulated runtime; no model usage'}
    if runtime_observations:
        observed = [o['usage'] for o in runtime_observations if o.get('usage')]
        usage = {'model_calls': sum(o.get('model_calls_attempted', 0) for o in runtime_observations),
                 'recorded_tokens': {k: sum(o['tokens'].get(k, 0) for o in observed)
                                     for k in {key for o in observed for key in o['tokens']}} if observed else None,
                 'cost': None, 'billing': 'unavailable',
                 'partial': len(observed) < len(runtime_observations) or any(o['partial'] for o in observed),
                 'coverage': 'attempted runtime requests and observed per-turn counters only; main chat/engineering agents excluded; token subsets are not additive'}
    return {'work_id': work_id, 'source_revision': work['revision'], 'source_timestamp': work['updated_at'],
            'concept_decision': decision, 'execution': run['execution'] if run else 'held',
            'control': run['control'] if run else 'none', 'technical_blockers': blockers,
            'pending_human_decisions': pending, 'assignments': assignments,
            'resources': [dict(r) for r in db.execute('SELECT * FROM resource WHERE work_id=? ORDER BY resource_id', (work_id,))],
            'owned_stops': [{**dict(r), 'observation': json.loads(r['observation']) if r['observation'] else None} for r in db.execute('SELECT * FROM owned_stop WHERE work_id=? ORDER BY requested_at,attempt_id', (work_id,))],
            'stop_conflicts': stop_conflicts_in(db, work_id),
            'candidate': json.loads(run['candidate']) if run and run['candidate'] else None,
            'delivery': json.loads(run['delivery']) if run and run['delivery'] else None,
            'live_dispatch_available': False, 'qualification': 'Q1 simulated lifecycle only',
            'usage': usage, 'runtime_observations': runtime_observations}


def view_in(db: sqlite3.Connection, work_id: str) -> dict:
    source = snapshot_in(db, work_id)
    projection = db.execute('SELECT * FROM projection WHERE work_id=?', (work_id,)).fetchone()
    source['projection'] = {'source_revision': projection['source_revision'], 'collected_at': projection['collected_at'],
                            'health': 'current' if projection['source_digest'] == digest(source) else 'stale-or-conflicting'} if projection else {'health': 'uncollected'}
    # Read-only additions are outside the existing canonical projection shape.
    # Otherwise an unchanged pre-upgrade revision would falsely look corrupt.
    run = db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
    source['technical_prerequisites'] = json.loads(run['contract'])['prerequisites'] if run else {}
    source['prerequisite_evidence'] = [json.loads(r[0]) for r in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='prerequisites_reconciled' ORDER BY event_id", (work_id,))]
    service = db.execute('SELECT * FROM worker_status WHERE singleton=1').fetchone()
    if service:
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(service['heartbeat'])).total_seconds())
        source['worker'] = {**dict(service), 'heartbeat_age_seconds': age,
                            'health': 'stale' if service['state'] == 'running' and age > 60 else service['state']}
    else:
        source['worker'] = {'state': 'unavailable', 'health': 'unavailable'}
    return source


class Lifecycle:
    def __init__(self, store: Store):
        self.store = store
        self._fault = lambda stage: None
        if store.read_only:
            raise Rejected('read_only', 'Lifecycle control requires the trusted service store')
        with store._connection() as db:
            db.executescript(SCHEMA)

    def _touch(self, db: sqlite3.Connection, work_id: str, kind: str, detail: Any) -> None:
        timestamp = now()
        db.execute('UPDATE work SET revision=revision+1,updated_at=? WHERE work_id=?', (timestamp, work_id))
        db.execute('INSERT INTO lifecycle_event(work_id,kind,detail,created_at) VALUES(?,?,?,?)',
                   (work_id, kind, canonical(detail), timestamp))

    def _block(self, db: sqlite3.Connection, work_id: str, code: str, detail: str,
               next_action: str = 'Coordinator reconciles evidence and explicitly retries', *, dependency: str = 'dispatch') -> None:
        db.execute('INSERT OR REPLACE INTO technical_blocker VALUES(?,?,?,?,?,?,?)',
                   (work_id, code, 'coordinator', dependency, detail, next_action, now()))
        db.execute("UPDATE lifecycle SET execution=CASE WHEN execution IN ('paused','cancelled','delivered') THEN execution ELSE 'held' END WHERE work_id=?", (work_id,))

    def _command_guard(self, db: sqlite3.Connection, work_id: str,
                       expected_work_revision: int | None, expected_digest: str | None) -> None:
        """Optional coordinator compare-and-set, checked inside the mutation transaction."""
        if expected_work_revision is None and expected_digest is None:
            return
        if expected_work_revision is None or expected_digest is None:
            raise Rejected('missing_command_identity', 'Expected work revision and exact concept digest are required together')
        work = self.store._work(db, work_id)
        self.store._expected(work, expected_work_revision)
        concept = db.execute('SELECT digest FROM concept WHERE work_id=? AND revision=?',
                             (work_id, work['current_concept'])).fetchone()
        if not concept or concept['digest'] != expected_digest:
            raise Rejected('stale_concept', 'Command must identify the current exact concept digest')

    @staticmethod
    def _command_payload(payload: list, expected_work_revision: int | None, expected_digest: str | None) -> list:
        return (payload if expected_work_revision is None and expected_digest is None else
                [*payload, {'expected_work_revision': expected_work_revision, 'expected_digest': expected_digest}])

    def register(self, operation_id: str, work_id: str, contract: dict, *,
                 expected_work_revision: int | None = None, expected_digest: str | None = None) -> dict:
        required = ('revision', 'workflow', 'concept_revision', 'concept_digest', 'baseline', 'behavior_delta',
                    'required_checks', 'writer', 'assessor', 'receiving_owner', 'authority', 'resources',
                    'prerequisites', 'qualified_route', 'max_assignments', 'max_repairs', 'max_seconds', 'model', 'effort')
        if any(key not in contract for key in required) or any(not contract[key] for key in required if key != 'max_repairs'):
            raise Rejected('incomplete_contract', 'Exact concept, baseline, delta, checks, independence, authority, resources and limits required')
        if contract.get('requires_detached_processes'):
            raise Rejected('detached_processes_unqualified', 'Assignments needing detached processes remain unqualified and cannot be admitted')
        if contract['workflow'] not in ('bug-fix', 'feature-change'):
            raise Rejected('unsupported_workflow', 'Only the approved bug-fix and feature-change paths exist')
        if contract['workflow'] == 'feature-change' and not contract.get('compatibility_checks'):
            raise Rejected('missing_feature_contract', 'Feature changes require compatibility/consumer checks')
        if (contract['writer'] == contract['assessor'] or not isinstance(contract['required_checks'], list)
                or len(set(contract['required_checks'])) != len(contract['required_checks'])):
            raise Rejected('independence_required', 'A distinct assessor and unique named checks are required')
        if (any(type(contract[k]) is not int or contract[k] < 1 for k in ('revision', 'concept_revision', 'max_assignments', 'max_seconds'))
                or type(contract['max_repairs']) is not int or not 0 <= contract['max_repairs'] <= 2):
            raise Rejected('invalid_limits', 'Positive bounded assignment/time limits and at most two repairs required')
        if (not isinstance(contract['prerequisites'], dict) or not isinstance(contract['resources'], list)
                or not all(isinstance(r, str) and r for r in contract['resources'])):
            raise Rejected('invalid_contract', 'Named prerequisites and resources required')
        payload = self._command_payload(['register', work_id, contract], expected_work_revision, expected_digest)
        with self.store._transaction() as db:
            if prior := self.store._prior(db, operation_id, payload):
                return prior
            self._command_guard(db, work_id, expected_work_revision, expected_digest)
            self.store._work(db, work_id)
            if db.execute('SELECT 1 FROM lifecycle WHERE work_id=?', (work_id,)).fetchone():
                raise Rejected('contract_exists', 'Retain the registered contract; revisions need explicit replan')
            db.execute('INSERT INTO lifecycle(work_id,contract) VALUES(?,?)', (work_id, canonical(contract)))
            self._touch(db, work_id, 'contract_registered', contract)
            return self.store._record(db, operation_id, payload, {'work_id': work_id, 'execution': 'held'})

    def _run(self, db: sqlite3.Connection, work_id: str) -> tuple[sqlite3.Row, dict]:
        row = db.execute('SELECT * FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
        if not row:
            raise Rejected('contract_not_found', 'Register a contract before execution')
        return row, json.loads(row['contract'])

    def reconcile_prerequisites(self, operation_id: str, work_id: str, expected_work_revision: int,
                                digest: str, evidence: dict) -> dict:
        """Record named technical facts before the first admission, never scope/approval."""
        if not isinstance(evidence, dict) or not evidence:
            raise Rejected('invalid_prerequisite_evidence', 'Named observations and durable evidence references are required')
        for item in evidence.values():
            try:
                valid = (isinstance(item, dict) and set(item) == {'satisfied', 'reference', 'observed_at'}
                         and type(item['satisfied']) is bool and isinstance(item['reference'], str)
                         and bool(item['reference'].strip()) and len(item['reference']) <= 4096
                         and isinstance(item['observed_at'], str)
                         and datetime.fromisoformat(item['observed_at']).tzinfo is not None)
            except ValueError:
                valid = False
            if not valid:
                raise Rejected('invalid_prerequisite_evidence', 'Each fact needs a boolean, durable reference and timezone-qualified observation time')
        self.reconcile(work_id)
        payload = self._command_payload(['reconcile_prerequisites', work_id, evidence], expected_work_revision, digest)
        with self.store._transaction() as db:
            if prior := self.store._prior(db, operation_id, payload):
                return prior
            self._command_guard(db, work_id, expected_work_revision, digest)
            run, contract = self._run(db, work_id)
            if (run['admitted'] or run['control'] != 'none' or run['execution'] not in ('held', 'paused')
                    or db.execute('SELECT 1 FROM assignment WHERE work_id=?', (work_id,)).fetchone()
                    or db.execute('SELECT 1 FROM resource WHERE work_id=?', (work_id,)).fetchone()
                    or db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (work_id,)).fetchone()):
                raise Rejected('prerequisite_ownership_held', 'Technical facts may change only before first admission, without assignment/resource ownership')
            if self._approved_envelope_errors(db, work_id, contract):
                raise Rejected('prerequisite_scope_mismatch', 'Retain the exact approved package and execution envelope')
            approved = db.execute("""SELECT 1 FROM decision d JOIN decision_readback r ON d.decision_id=r.decision_id
                WHERE d.work_id=? AND d.concept_revision=? AND d.digest=? AND d.outcome='approved' AND r.status='verified'""",
                (work_id, contract['concept_revision'], contract['concept_digest'])).fetchone()
            if not approved:
                raise Rejected('approval_required', 'Technical reconciliation cannot replace exact verified Concept Approval')
            if set(evidence) - set(contract['prerequisites']):
                raise Rejected('foreign_prerequisite', 'Only prerequisite names already fixed by the approved package may be reconciled')
            for key, item in evidence.items():
                contract['prerequisites'][key] = item['satisfied']
            db.execute('UPDATE lifecycle SET contract=? WHERE work_id=?', (canonical(contract), work_id))
            self._touch(db, work_id, 'prerequisites_reconciled', {'operation_id': operation_id, 'evidence': evidence})
            return self.store._record(db, operation_id, payload,
                {'work_id': work_id, 'work_revision': self.store._work(db, work_id)['revision'],
                 'technical_prerequisites': contract['prerequisites'], 'approval_preserved': True,
                 'execution': run['execution'], 'next_action': 'Read status and explicitly retry admission with a fresh operation ID'})

    def _approved_envelope_errors(self, db: sqlite3.Connection, work_id: str, contract: dict) -> list[str]:
        concept = db.execute('SELECT package FROM concept WHERE work_id=? AND revision=? AND digest=?',
                             (work_id, contract['concept_revision'], contract['concept_digest'])).fetchone()
        if not concept:
            return ['Exact approved execution package is missing']
        package = json.loads(concept['package'])
        bounds = package.get('execution_envelope')
        if not isinstance(bounds, dict):
            return ['Approved package contains no explicit execution envelope; concept remains approved, execution is held']
        errors = []
        exact = ('revision', 'workflow', 'baseline', 'behavior_delta', 'required_checks', 'compatibility_checks',
                 'writer', 'assessor', 'receiving_owner', 'authority', 'resources', 'qualified_route', 'model', 'effort')
        for field in exact:
            if field not in bounds or contract.get(field) != bounds[field]:
                errors.append('Execution contract differs from approved ' + field)
        for field in ('runtime_recipe', 'check_kinds', 'check_adapters', 'product_budget', 'adapter_architecture'):
            if field in bounds or field in contract:
                if contract.get(field) != bounds.get(field):
                    errors.append('Execution contract differs from approved ' + field)
        if package.get('authority') != bounds.get('authority'):
            errors.append('Execution authority disagrees with the approved package authority')
        for field in ('max_assignments', 'max_repairs', 'max_seconds'):
            if type(bounds.get(field)) is not int or not 0 <= contract[field] <= bounds[field]:
                errors.append('Execution contract exceeds approved ' + field)
        if (not isinstance(bounds.get('prerequisites'), dict)
                or set(contract['prerequisites']) != set(bounds['prerequisites'])):
            errors.append('Execution prerequisite set differs from the approved package')
        members = {m.get('name'): m for m in package.get('team_envelope', []) if isinstance(m, dict)}
        for role in ('writer', 'assessor'):
            member = members.get(contract[role])
            if not member or member.get('model') != contract['model'] or member.get('effort') != contract['effort']:
                errors.append('Execution ' + role + ' is outside the approved staffing/model envelope')
        return errors

    def assignment_authority_errors(self, assignment: dict) -> list[str]:
        """Rechecked for saved intents and already observed attempts on recovery."""
        with self.store._connection() as db:
            _, contract = self._run(db, assignment['work_id'])
            work = self.store._work(db, assignment['work_id'])
            errors = self._approved_envelope_errors(db, assignment['work_id'], contract)
            if (assignment['concept_revision'] != work['current_concept']
                    or assignment['contract_revision'] != contract['revision']
                    or assignment['concept_digest'] != contract['concept_digest']
                    or assignment['requested_model'] != contract['model']
                    or assignment['requested_effort'] != contract['effort']
                    or assignment['executor'] != contract['assessor' if assignment['kind'] == 'assessment' else 'writer']
                    or assignment['scope'] != canonical(assignment_scope(contract, assignment['kind']))):
                errors.append('Saved assignment no longer matches the approved current contract')
            if ((assignment.get('runtime_model') is not None and assignment['runtime_model'] != assignment['requested_model'])
                    or (assignment.get('runtime_effort') is not None and assignment['runtime_effort'] != assignment['requested_effort'])):
                errors.append('Observed runtime configuration is outside the approved envelope')
            decision = db.execute("""SELECT 1 FROM decision d JOIN decision_readback r ON r.decision_id=d.decision_id
                WHERE d.work_id=? AND d.concept_revision=? AND d.digest=? AND d.outcome='approved' AND r.status='verified'""",
                (assignment['work_id'], contract['concept_revision'], contract['concept_digest'])).fetchone()
            if not decision:
                errors.append('Current exact approval readback is not verified')
            return errors

    def _admission_errors(self, db: sqlite3.Connection, work_id: str) -> list[str]:
        run, contract = self._run(db, work_id)
        work = self.store._work(db, work_id)
        errors = self._approved_envelope_errors(db, work_id, contract)
        if self.store.recovery_hold():
            errors.append('Recovery copy is not adopted; dispatch remains held and runtime/shared-budget sidecars are not restored')
        decision = db.execute("""SELECT d.*,r.status AS readback_status FROM decision d
            LEFT JOIN decision_readback r ON r.decision_id=d.decision_id WHERE d.work_id=?
            AND d.concept_revision=? AND d.digest=? AND d.outcome='approved' ORDER BY d.created_at DESC LIMIT 1""",
            (work_id, contract['concept_revision'], contract['concept_digest'])).fetchone()
        if (not decision or decision['readback_status'] != 'verified' or work['current_concept'] != contract['concept_revision']):
            errors.append('Current exact concept approval and verified decision readback required')
        if not contract['authority'] or not contract['qualified_route'] or not all(value is True for value in contract['prerequisites'].values()):
            errors.append('Authority, qualified route or a named prerequisite is unmet')
        if run['control'] != 'none' or run['execution'] in ('cancelled', 'delivered'):
            errors.append('Execution control fences dispatch')
        if db.execute('SELECT 1 FROM lifecycle WHERE work_id<>? AND admitted=1', (work_id,)).fetchone():
            errors.append('Another execution project owns admission')
        if db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (work_id,)).fetchone():
            errors.append('Unresolved technical blocker')
        for resource in contract['resources']:
            owner = db.execute('SELECT * FROM resource WHERE resource_id=?', (resource,)).fetchone()
            if owner and owner['work_id'] != work_id:
                errors.append('Resource ownership conflict: ' + resource)
        return errors

    def admit(self, operation_id: str, work_id: str, *,
              expected_work_revision: int | None = None, expected_digest: str | None = None) -> dict:
        # Reconcile before eligibility, including the exact decision in a fresh transaction.
        self.reconcile(work_id)
        payload = self._command_payload(['admit', work_id], expected_work_revision, expected_digest)
        with self.store._transaction() as db:
            if prior := self.store._prior(db, operation_id, payload):
                return prior
            self._command_guard(db, work_id, expected_work_revision, expected_digest)
            run, contract = self._run(db, work_id)
            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='admission'", (work_id,))
            errors = self._admission_errors(db, work_id)
            if errors:
                self._block(db, work_id, 'admission', '; '.join(errors))
                self._touch(db, work_id, 'admission_held', errors)
                return self.store._record(db, operation_id, payload, {'work_id': work_id, 'execution': 'held', 'blockers': errors})
            for resource in contract['resources']:
                db.execute('INSERT OR IGNORE INTO resource VALUES(?,?,NULL,?,?)', (resource, work_id, 'reserved', now()))
            db.execute("UPDATE lifecycle SET admitted=1,execution='eligible' WHERE work_id=?", (work_id,))
            if not db.execute('SELECT 1 FROM assignment WHERE work_id=?', (work_id,)).fetchone():
                self._enqueue(db, work_id, 'implementation', 'admission:' + work_id)
            self._touch(db, work_id, 'admitted', {})
            return self.store._record(db, operation_id, payload, {'work_id': work_id, 'execution': 'eligible'})

    def _enqueue(self, db: sqlite3.Connection, work_id: str, kind: str, predecessor: str) -> str | None:
        run, contract = self._run(db, work_id)
        if run['control'] != 'none' or run['execution'] in ('cancelled', 'delivered'):
            return None
        if db.execute('SELECT COUNT(*) FROM assignment WHERE work_id=?', (work_id,)).fetchone()[0] >= contract['max_assignments']:
            self._block(db, work_id, 'assignment_limit', 'Approved assignment limit exhausted', 'Return a bounded decision packet')
            return None
        assignment_id = 'a-' + digest([work_id, predecessor])[7:39]
        timestamp = now()
        self._fault('before_outbox_enqueue')
        db.execute('''INSERT INTO assignment
            (assignment_id,work_id,predecessor,kind,state,contract_revision,concept_revision,concept_digest,candidate,
             executor,requested_model,requested_effort,scope,attempt_id,created_at,eligible_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (assignment_id, work_id, predecessor, kind, 'proposed', contract['revision'], contract['concept_revision'],
             contract['concept_digest'], run['candidate'], contract['assessor'] if kind == 'assessment' else contract['writer'],
             contract['model'], contract['effort'], canonical(assignment_scope(contract, kind)),
             assignment_id, timestamp, timestamp))
        db.execute('INSERT INTO outbox VALUES(?,?,NULL,?)', (assignment_id, 'pending', timestamp))
        return assignment_id

    def assignments(self, *, unresolved: bool = False) -> list[dict]:
        with self.store._connection() as db:
            where = "WHERE state NOT IN ('completed','failed','cancelled')" if unresolved else ''
            return [dict(r) for r in db.execute('SELECT * FROM assignment ' + where + ' ORDER BY created_at,assignment_id')]

    def claim(self, assignment_id: str, epoch: int) -> dict | None:
        with self.store._transaction() as db:
            assignment = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not assignment or assignment['state'] != 'proposed':
                return None
            run, contract = self._run(db, assignment['work_id'])
            owner = db.execute('SELECT * FROM worker_status WHERE singleton=1').fetchone()
            if not owner or owner['epoch'] != epoch or owner['state'] != 'running':
                raise Rejected('stale_worker', 'Only the current worker epoch can claim an outbox item')
            if run['execution'] == 'paused':
                return None
            errors = self._admission_errors(db, assignment['work_id'])
            if errors:
                if not db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (assignment['work_id'],)).fetchone():
                    self._block(db, assignment['work_id'], 'admission', '; '.join(errors))
                    self._touch(db, assignment['work_id'], 'dispatch_held', errors)
                return None
            if assignment['kind'] != 'assessment' and any(r['state'] == 'frozen' for r in db.execute('SELECT state FROM resource WHERE work_id=?', (assignment['work_id'],))):
                self._block(db, assignment['work_id'], 'candidate_frozen', 'Writer cannot mutate a frozen assessment candidate')
                return None
            timestamp = now()
            db.execute("UPDATE assignment SET state='sent',sent_at=? WHERE assignment_id=?", (timestamp, assignment_id))
            db.execute("UPDATE outbox SET state='sending',epoch=?,updated_at=? WHERE assignment_id=?", (epoch, timestamp, assignment_id))
            db.execute("UPDATE resource SET assignment_id=?,state=?,observed_at=? WHERE work_id=?",
                       (assignment_id, 'frozen' if assignment['kind'] == 'assessment' else 'owned', timestamp, assignment['work_id']))
            db.execute("UPDATE lifecycle SET execution='active' WHERE work_id=?", (assignment['work_id'],))
            self._touch(db, assignment['work_id'], 'launch_intent', {'assignment_id': assignment_id, 'epoch': epoch})
            return dict(db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone())

    def can_launch_saved(self, assignment: dict) -> bool:
        """A durable intent is not permission to launch through a later fence."""
        authority_errors = self.assignment_authority_errors(assignment)
        with self.store._transaction() as db:
            run, _ = self._run(db, assignment['work_id'])
            if run['admitted'] != 1 or run['execution'] == 'paused' or run['control'] != 'none':
                return False
            errors = authority_errors + self._admission_errors(db, assignment['work_id'])
            if errors:
                if not db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (assignment['work_id'],)).fetchone():
                    self._block(db, assignment['work_id'], 'approved_envelope', '; '.join(errors))
                    self._touch(db, assignment['work_id'], 'saved_launch_held', errors)
                return False
            return True

    def acknowledge(self, assignment_id: str, observation: dict) -> None:
        with self.store._transaction() as db:
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not a or a['state'] not in ('sent', 'acknowledged', 'observed_active'):
                return
            if observation.get('attempt_id') != a['attempt_id']:
                raise Rejected('wrong_attempt', 'Runtime acknowledgement must identify the saved attempt')
            if (observation.get('model') != a['requested_model'] or observation.get('effort') != a['requested_effort']):
                self._block(db, a['work_id'], 'configuration_mismatch', 'Runtime model/effort missing or outside requested envelope')
            active = observation.get('state') == 'active'
            db.execute('''UPDATE assignment SET state=?,acknowledged_at=COALESCE(acknowledged_at,?),
                observed_active_at=CASE WHEN ? THEN COALESCE(observed_active_at,?) ELSE observed_active_at END,
                runtime_model=?,runtime_effort=? WHERE assignment_id=?''',
                ('observed_active' if active else 'acknowledged', now(), active, now(), observation.get('model'), observation.get('effort'), assignment_id))
            db.execute("UPDATE outbox SET state='sent',updated_at=? WHERE assignment_id=?", (now(), assignment_id))
            self._touch(db, a['work_id'], 'acknowledged', observation)

    def unknown(self, assignment_id: str, detail: str) -> None:
        with self.store._transaction() as db:
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            run, _ = self._run(db, a['work_id'])
            if run['execution'] in TERMINAL_EXECUTIONS or run['control'] == 'cancelled' or a['state'] in ('completed', 'failed', 'cancelled'):
                return
            db.execute("UPDATE assignment SET state='blocked_unknown_effects' WHERE assignment_id=?", (assignment_id,))
            db.execute("UPDATE outbox SET state='blocked',updated_at=? WHERE assignment_id=?", (now(), assignment_id))
            self._block(db, a['work_id'], 'unknown_effects', detail, 'Locate the exact owned attempt and reconcile effects before reuse')
            self._touch(db, a['work_id'], 'unknown_effects', detail)

    def observe_runtime(self, assignment: dict, observation: dict) -> None:
        """Factual telemetry only; it grants neither decision nor completion authority."""
        if observation.get('simulated') is not False:
            return
        if observation.get('attempt_id') != assignment['attempt_id']:
            raise Rejected('wrong_attempt', 'Runtime telemetry names another saved attempt')
        selected = {key: observation.get(key) for key in ('attempt_id', 'state', 'model', 'effort', 'thread_id',
                    'turn_id', 'usage', 'model_calls_attempted', 'detail')}
        with self.store._transaction() as db:
            saved = db.execute('SELECT work_id FROM assignment WHERE assignment_id=? AND attempt_id=?',
                               (assignment['assignment_id'], assignment['attempt_id'])).fetchone()
            if not saved:
                raise Rejected('assignment_not_found', 'Runtime telemetry requires an exact saved assignment')
            prior = db.execute('SELECT observation FROM runtime_observation WHERE attempt_id=?', (assignment['attempt_id'],)).fetchone()
            if prior and prior[0] == canonical(selected):
                return
            db.execute('INSERT OR REPLACE INTO runtime_observation VALUES(?,?,?)',
                       (assignment['attempt_id'], canonical(selected), now()))
            self._touch(db, saved['work_id'], 'runtime_observation', selected)

    def _assessment_error(self, contract: dict, a: sqlite3.Row, result: dict) -> str | None:
        if (result.get('candidate') != json.loads(a['candidate'])
                or result.get('observed_source_identity') != json.loads(a['candidate'])['source_identity']):
            return 'Assessment identifies the wrong candidate'
        reports = result.get('initial_reports', {})
        if (result.get('executor') != contract['assessor'] or result.get('executor') == contract['writer']
                or not reports.get('execution') or not reports.get('review') or reports.get('independent') is not True):
            return 'Independent execution and source-review initial reports are required'
        checks = result.get('checks', [])
        required = set(contract['required_checks']) | set(contract.get('compatibility_checks', []))
        if len({c.get('name') for c in checks}) != len(checks) or not required.issubset({c.get('name') for c in checks}):
            return 'Required check evidence is missing or duplicated'
        qualified = None
        typed = any('result_kind' in c or 'adapter_id' in c for c in contract.get('runtime_recipe', {}).get('checks', []))
        if contract.get('runtime_recipe'):
            try:
                with self.store._connection() as db:
                    row = db.execute('SELECT package FROM concept WHERE work_id=? AND revision=? AND digest=?',
                        (a['work_id'], a['concept_revision'], a['concept_digest'])).fetchone()
                package = json.loads(row[0])
                if digest(package) != a['concept_digest']:
                    raise Rejected('check_adapter', 'Approved package changed')
                qualified = bindings(self.store, a['work_id'], package, package['execution_envelope']['runtime_recipe'])
            except (Rejected, KeyError, TypeError, ValueError):
                return 'Required check adapter is not exactly approved or independently qualified'
        for check in checks:
            expected_kind = check_kind(contract, check.get('name'))
            if check.get('kind') is not None:
                try:
                    validate_normalized(check)
                    valid_result = (check['kind'] == expected_kind and evidence_passed(check, check.get('exit_code'))
                                    and bool(check.get('adapter')) and bool(check.get('output_sha256'))
                                    and bool(check.get('raw_outputs')))
                    if qualified is not None:
                        descriptor = next((c for c in contract['runtime_recipe']['checks'] if c['name'] == check.get('name')), None)
                        valid_result = (valid_result and descriptor is not None and
                                        check.get('adapter') == qualified.get(adapter_for(descriptor).ADAPTER_ID))
                except Rejected:
                    valid_result = False
            else:
                valid_result = not typed and expected_kind == 'tests' and type(check.get('selected')) is int and check['selected'] > 0
            if (check.get('result') != 'pass' or not valid_result
                    or not all(check.get(k) for k in ('command', 'environment', 'executor', 'output', 'scope', 'limits'))
                    or check.get('candidate') != result.get('candidate') or check['executor'] != contract['assessor']):
                return 'A required check failed, was blocked/unrun, selected zero tests, lacked output or had wrong identity'
        return None

    def complete(self, receipt_id: str, assignment_id: str, result: dict) -> dict:
        identifier(receipt_id)
        fingerprint = digest([assignment_id, result])
        with self.store._transaction() as db:
            prior = db.execute('SELECT * FROM completion WHERE receipt_id=?', (receipt_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise Rejected('completion_conflict', 'Receipt ID already names different factual output')
                return {'disposition': prior['disposition'], 'duplicate': True}
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not a:
                raise Rejected('assignment_not_found', 'Unknown assignment')
            run, contract = self._run(db, a['work_id'])
            work = self.store._work(db, a['work_id'])
            disposition = 'historical'
            current = (a['state'] in ('sent', 'acknowledged', 'observed_active', 'waiting')
                       and a['contract_revision'] == contract['revision'] and a['concept_revision'] == work['current_concept']
                       and a['concept_digest'] == contract['concept_digest'] and run['control'] == 'none'
                       and run['execution'] not in ('cancelled', 'delivered'))
            expired = (a['sent_at'] is not None and
                       (datetime.now(timezone.utc) - datetime.fromisoformat(a['sent_at'])).total_seconds() > contract['max_seconds'])
            if current and expired:
                self._request_owned_stop_in(db, a, 'assignment_deadline', 'Terminal receipt arrived after the assignment deadline; exact stop reconciliation required')
                disposition = 'deadline_held'
                current = False
            if current:
                error = None
                if db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (a['work_id'],)).fetchone():
                    error = 'An unresolved technical blocker prevents candidate acceptance or successor admission'
                elif result.get('owned_processes') != [] or result.get('effects_reconciled') is not True or not partial_mutations_known(result):
                    error = 'Runtime terminal result does not prove owned-process exit and mutation reconciliation'
                elif result.get('executor') != a['executor'] or result.get('status') != 'completed':
                    error = 'Assignment failed or the terminal receipt has the wrong executor'
                elif a['kind'] == 'assessment':
                    error = self._assessment_error(contract, a, result)
                elif not result.get('candidate') or not all(result['candidate'].get(k) for k in ('identity', 'source_identity', 'inventory')):
                    error = 'Implementation must freeze an exact candidate and source/new-file inventory'
                effects_unknown = (result.get('owned_processes') != [] or result.get('effects_reconciled') is not True
                                   or not partial_mutations_known(result))
                db.execute('UPDATE assignment SET state=?,terminal_at=?,result=? WHERE assignment_id=?',
                           ('blocked_unknown_effects' if effects_unknown else 'failed' if error else 'completed', now(), canonical(result), assignment_id))
                if error:
                    disposition = 'held'
                    self._block(db, a['work_id'], 'unknown_effects' if effects_unknown else 'assessment_failed' if a['kind'] == 'assessment' else 'assignment_failed', error)
                    finding = result.get('finding', {})
                    reports = result.get('initial_reports', {})
                    if (a['kind'] == 'assessment' and result.get('executor') == contract['assessor']
                            and result.get('candidate') == json.loads(a['candidate'])
                            and result.get('observed_source_identity') == json.loads(a['candidate'])['source_identity']
                            and reports.get('execution') and reports.get('review') and reports.get('independent') is True
                            and result.get('owned_processes') == [] and result.get('effects_reconciled') is True
                            and partial_mutations_known(result) and finding.get('confirmed') is True and finding.get('in_scope') is True and finding.get('summary')):
                        if run['cycle'] < contract['max_repairs']:
                            db.execute('UPDATE lifecycle SET cycle=cycle+1,candidate=NULL WHERE work_id=?', (a['work_id'],))
                            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='assessment_failed'", (a['work_id'],))
                            db.execute("UPDATE resource SET state='reserved',assignment_id=NULL,observed_at=? WHERE work_id=?", (now(), a['work_id']))
                            self._enqueue(db, a['work_id'], 'repair', 'completion:' + receipt_id)
                            disposition = 'repair'
                        else:
                            self._block(db, a['work_id'], 'repair_limit', 'At most two corrective cycles are authorized', 'Return findings in a focused decision packet')
                elif a['kind'] in ('implementation', 'repair'):
                    db.execute('UPDATE lifecycle SET candidate=? WHERE work_id=?', (canonical(result['candidate']), a['work_id']))
                    db.execute("UPDATE resource SET state='frozen',assignment_id=NULL,observed_at=? WHERE work_id=?", (now(), a['work_id']))
                    self._enqueue(db, a['work_id'], 'assessment', 'completion:' + receipt_id)
                    disposition = 'assessment_required'
                else:
                    # Acceptance is exclusively the non-author assessor's qualified evidence.
                    delivery = {'candidate': result['candidate'], 'accepted': True, 'committed': False, 'installed': False,
                                'released': False, 'receiving_owner': contract['receiving_owner'], 'checks': result['checks'],
                                'limits': 'Q2 bounded runtime candidate; desktop/archive qualification remains separate' if result.get('runtime_turn_id') else 'Q1 simulated acceptance only', 'archive': 'local preparation pending; live publication unqualified',
                                'accepted_at': now(), 'assessor': result['executor']}
                    db.execute("UPDATE lifecycle SET execution='delivered',admitted=0,delivery=? WHERE work_id=?", (canonical(delivery), a['work_id']))
                    db.execute('DELETE FROM resource WHERE work_id=?', (a['work_id'],))
                    disposition = 'delivered'
                if disposition != 'delivered' and run['execution'] != 'paused':
                    blocked = db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (a['work_id'],)).fetchone()
                    db.execute('UPDATE lifecycle SET execution=? WHERE work_id=?', ('held' if blocked else 'eligible', a['work_id']))
            elif not expired and a['state'] not in ('completed', 'failed', 'cancelled') and run['control'] == 'none':
                effects_known = result.get('owned_processes') == [] and result.get('effects_reconciled') is True and partial_mutations_known(result)
                db.execute("UPDATE assignment SET state=?,terminal_at=?,result=? WHERE assignment_id=?",
                           ('completed' if effects_known else 'blocked_unknown_effects', now(), canonical(result), assignment_id))
                self._block(db, a['work_id'], 'stale_assignment' if effects_known else 'unknown_effects', 'Terminal result belongs to an obsolete contract/concept; effects and ownership remain held')
            db.execute('INSERT INTO completion VALUES(?,?,?,?,?,?)', (receipt_id, assignment_id, fingerprint, canonical(result), disposition, now()))
            self._touch(db, a['work_id'], 'completion_consumed', {'receipt_id': receipt_id, 'disposition': disposition})
        self._fault('after_completion_commit')
        return {'disposition': disposition, 'duplicate': False}

    def reassess(self, operation_id: str, work_id: str) -> dict:
        payload = ['reassess', work_id]
        with self.store._transaction() as db:
            if prior := self.store._prior(db, operation_id, payload):
                return prior
            run, _ = self._run(db, work_id)
            if not run['candidate'] or run['control'] != 'none':
                raise Rejected('no_frozen_candidate', 'Reassessment requires the retained frozen candidate')
            if db.execute("SELECT 1 FROM assignment WHERE work_id=? AND state NOT IN ('completed','failed','cancelled')", (work_id,)).fetchone():
                raise Rejected('assignment_unresolved', 'Resolve current assignment before reassessment')
            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='assessment_failed'", (work_id,))
            aid = self._enqueue(db, work_id, 'assessment', 'reassessment:' + operation_id)
            if run['execution'] != 'paused':
                db.execute("UPDATE lifecycle SET execution='eligible' WHERE work_id=? AND NOT EXISTS(SELECT 1 FROM technical_blocker WHERE work_id=?)", (work_id, work_id))
            self._touch(db, work_id, 'reassessment_requested', {'assignment_id': aid})
            return self.store._record(db, operation_id, payload, {'assignment_id': aid})

    def control(self, operation_id: str, work_id: str, action: str, *,
                expected_work_revision: int | None = None, expected_digest: str | None = None) -> dict:
        if action not in ('pause', 'resume', 'cancel'):
            raise Rejected('invalid_control', 'Use explicit pause, resume or cancel')
        if action == 'resume':
            self.reconcile(work_id)
        payload = self._command_payload(['control', work_id, action], expected_work_revision, expected_digest)
        with self.store._transaction() as db:
            if prior := self.store._prior(db, operation_id, payload):
                return prior
            self._command_guard(db, work_id, expected_work_revision, expected_digest)
            run, _ = self._run(db, work_id)
            if run['execution'] in ('cancelled', 'delivered'):
                raise Rejected('terminal_execution', 'Terminal execution cannot be resumed or changed')
            if action == 'pause':
                db.execute("UPDATE lifecycle SET resume_execution=execution,execution='paused' WHERE work_id=?", (work_id,))
            elif action == 'resume':
                if run['execution'] != 'paused':
                    raise Rejected('not_paused', 'Resume requires an explicit persisted pause')
                db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='resume_held'", (work_id,))
                errors = self._admission_errors(db, work_id)
                if errors:
                    self._block(db, work_id, 'resume_held', '; '.join(errors))
                    self._touch(db, work_id, 'resume_held', errors)
                    return self.store._record(db, operation_id, payload, {'execution': 'paused', 'blockers': errors})
                db.execute("UPDATE lifecycle SET execution='eligible',resume_execution=NULL WHERE work_id=?", (work_id,))
            else:
                db.execute("UPDATE lifecycle SET execution='held',control='cancel_requested' WHERE work_id=?", (work_id,))
                db.execute("UPDATE outbox SET state='cancelled',updated_at=? WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=? AND state='proposed')", (now(), work_id))
                db.execute("UPDATE assignment SET state='cancelled',terminal_at=? WHERE work_id=? AND state='proposed'", (now(), work_id))
            self._touch(db, work_id, action, {})
            updated = db.execute('SELECT execution,control FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
            return self.store._record(db, operation_id, payload, dict(updated))

    def _request_owned_stop_in(self, db: sqlite3.Connection, a: sqlite3.Row, reason: str, detail: str) -> bool:
        run, _ = self._run(db, a['work_id'])
        intent = db.execute('SELECT * FROM owned_stop WHERE attempt_id=?', (a['attempt_id'],)).fetchone()
        if (run['execution'] in TERMINAL_EXECUTIONS or run['control'] == 'cancelled'
                or a['state'] in ('completed', 'failed', 'cancelled') or (intent and intent['state'] == 'stopped')):
            return False
        if intent:
            # Retrying an unresolved stop does not replace newer user control or
            # erase the previous observation. The exact adapter stop may retry.
            return True
        control = run['control'] if run['control'] in CANCELLATION_CONTROLS else 'owned_stop_requested'
        db.execute("UPDATE lifecycle SET execution=CASE WHEN execution='paused' THEN 'paused' ELSE 'held' END,control=? WHERE work_id=?",
                   (control, a['work_id']))
        db.execute("UPDATE assignment SET state='stop_requested' WHERE assignment_id=?", (a['assignment_id'],))
        db.execute("UPDATE outbox SET state=CASE WHEN state='cancelled' THEN state ELSE 'blocked' END,updated_at=? WHERE assignment_id=?", (now(), a['assignment_id']))
        db.execute("INSERT INTO owned_stop VALUES(?,?,?,'requested',?,NULL,NULL)",
                   (a['attempt_id'], a['work_id'], reason, now()))
        self._block(db, a['work_id'], reason, detail, 'Reconcile exact owned stop and retained mutations; coordinator decides the next authorized action')
        self._touch(db, a['work_id'], 'owned_stop_requested', {'assignment_id': a['assignment_id'], 'reason': reason, 'detail': detail})
        return True

    def request_owned_stop(self, assignment_id: str, reason: str, detail: str) -> bool:
        """Persist a stop intent while preserving newer cancellation/terminal state."""
        with self.store._transaction() as db:
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not a:
                raise Rejected('assignment_not_found', 'Only an exact saved assignment can request a stop')
            return self._request_owned_stop_in(db, a, reason, detail)

    def owned_stop_result(self, assignment_id: str, observation: dict) -> dict:
        with self.store._transaction() as db:
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not a:
                raise Rejected('assignment_not_found', 'Only an exact saved assignment can receive a stop result')
            intent = db.execute('SELECT * FROM owned_stop WHERE attempt_id=?', (a['attempt_id'],)).fetchone()
            if not intent:
                raise Rejected('stop_not_requested', 'Persist exact stop intent before accepting stop evidence')
            encoded = canonical(observation)
            if (intent['observation'] == encoded or db.execute(
                    "SELECT 1 FROM lifecycle_event WHERE work_id=? AND kind IN ('owned_stop_reconciled','owned_stop_historical') AND detail=?",
                    (a['work_id'], encoded)).fetchone() or any(
                    json.loads(event[0])['received'] == observation for event in db.execute(
                        "SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='owned_stop_conflict'", (a['work_id'],)))):
                return {'disposition': 'duplicate'}
            run, _ = self._run(db, a['work_id'])
            if run['execution'] in TERMINAL_EXECUTIONS or run['control'] == 'cancelled':
                # A late receipt is historical evidence, never a transition out
                # of a terminal execution or a replacement of its saved proof.
                self._touch(db, a['work_id'], 'owned_stop_historical', observation)
                return {'disposition': 'historical'}
            if intent['state'] == 'stopped':
                conflict = {'retained': json.loads(intent['observation']), 'received': observation}
                if db.execute("SELECT 1 FROM lifecycle_event WHERE work_id=? AND kind='owned_stop_conflict' AND detail=?",
                              (a['work_id'], canonical(conflict))).fetchone():
                    return {'disposition': 'duplicate'}
                self._block(db, a['work_id'], 'stop_receipt_conflict', 'A changed receipt cannot replace the proved owned stop')
                self._touch(db, a['work_id'], 'owned_stop_conflict', conflict)
                return {'disposition': 'conflict'}
            safe = observation.get('attempt_id') == a['attempt_id'] and stopped_with_effects(observation)
            db.execute('UPDATE owned_stop SET state=?,observation=?,observed_at=? WHERE attempt_id=?',
                       ('stopped' if safe else 'blocked_unknown_effects', encoded, now(), a['attempt_id']))
            db.execute('UPDATE assignment SET state=?,terminal_at=? WHERE assignment_id=?',
                       ('cancelled' if safe else 'blocked_unknown_effects', now() if safe else None, assignment_id))
            if run['control'] not in CANCELLATION_CONTROLS:
                unresolved = db.execute("SELECT 1 FROM owned_stop WHERE work_id=? AND state<>'stopped'", (a['work_id'],)).fetchone()
                db.execute("UPDATE lifecycle SET execution=CASE WHEN execution='paused' THEN 'paused' ELSE 'held' END,control=? WHERE work_id=?",
                           ('owned_stop_requested' if unresolved else 'none', a['work_id']))
            if not safe:
                self._block(db, a['work_id'], 'unknown_effects', 'Owned stop lacks exact process-exit and explicit known partial-mutation evidence')
            # Preserve cancellation precedence, pause, resources and the original
            # deadline/envelope hold even after a proved stop.
            self._touch(db, a['work_id'], 'owned_stop_reconciled', observation)
            return {'disposition': 'reconciled'}

    def pending_owned_stop(self, attempt_id: str) -> bool:
        with self.store._connection() as db:
            return bool(db.execute("SELECT 1 FROM owned_stop WHERE attempt_id=? AND state<>'stopped'", (attempt_id,)).fetchone())

    def cancellation_assignments(self, work_id: str) -> list[dict]:
        with self.store._connection() as db:
            return cancellation_assignments_in(db, work_id)

    def resolve_owned_stop_conflict(self, assignment_id: str, conflict_ids: list[str],
                                    observation: dict, evidence: str) -> dict:
        """Trusted coordinator reconciles the current exact-attempt conflicts.

        Ordinary adapter receipt replay cannot resolve a contradiction. This
        explicit recovery action binds fresh process/effect evidence to every
        current conflict for the attempt, preserving both previous receipts.
        It grants no successor or resource release; cancellation checks again.
        """
        with self.store._transaction() as db:
            a = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment_id,)).fetchone()
            if not a:
                raise Rejected('assignment_not_found', 'Only an exact saved assignment can resolve a stop conflict')
            resolution = {'assignment_id': assignment_id, 'attempt_id': a['attempt_id'],
                          'conflict_ids': sorted(conflict_ids), 'observation': observation, 'evidence': evidence}
            if db.execute("SELECT 1 FROM lifecycle_event WHERE work_id=? AND kind='owned_stop_conflict_resolved' AND detail=?",
                          (a['work_id'], canonical(resolution))).fetchone():
                return {'disposition': 'duplicate'}
            run, _ = self._run(db, a['work_id'])
            if run['execution'] in TERMINAL_EXECUTIONS or run['control'] == 'cancelled':
                raise Rejected('terminal_execution', 'Conflict recovery cannot reopen terminal execution')
            conflicts = [c for c in stop_conflicts_in(db, a['work_id']) if c['attempt_id'] == a['attempt_id']]
            expected = {c['conflict_id'] for c in conflicts}
            if not expected or len(conflict_ids) != len(expected) or set(conflict_ids) != expected:
                raise Rejected('stop_conflict_changed', 'Reconcile every current conflict for this exact attempt')
            if (observation.get('attempt_id') != a['attempt_id'] or not stopped_with_effects(observation)
                    or not isinstance(evidence, str) or not evidence.strip()):
                raise Rejected('stop_conflict_unreconciled', 'Exact process-exit, known partial mutations and reconciliation evidence required')
            db.execute("UPDATE owned_stop SET state='stopped',observation=?,observed_at=? WHERE attempt_id=?",
                       (canonical(observation), now(), a['attempt_id']))
            self._touch(db, a['work_id'], 'owned_stop_conflict_resolved', resolution)
            if not stop_conflicts_in(db, a['work_id']):
                db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='stop_receipt_conflict'", (a['work_id'],))
            return {'disposition': 'resolved'}

    def cancellation_result(self, work_id: str, observations: list[dict]) -> None:
        with self.store._transaction() as db:
            run, _ = self._run(db, work_id)
            if run['control'] not in ('cancel_requested', 'reconciling', 'blocked_unknown_effects'):
                return
            conflicts = stop_conflicts_in(db, work_id)
            conflicted = {c['attempt_id'] for c in conflicts}
            owned = {a['attempt_id'] for a in cancellation_assignments_in(db, work_id)}
            conflict_blocker = db.execute("SELECT 1 FROM technical_blocker WHERE work_id=? AND code='stop_receipt_conflict'", (work_id,)).fetchone()
            safe = (not conflicts and not conflict_blocker
                    and len(observations) == len(owned) and owned == {o.get('attempt_id') for o in observations}
                    and all(stopped_with_effects(o) for o in observations))
            for observation in observations:
                attempt_id = observation.get('attempt_id')
                # A fresh cancel reply is retained in cancel_reconciled below,
                # but cannot silently replace either side of an open conflict.
                if attempt_id in owned and attempt_id not in conflicted:
                    db.execute("""INSERT INTO owned_stop VALUES(?,?,'cancel',?,?,?,?)
                        ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state,observation=excluded.observation,observed_at=excluded.observed_at""",
                        (attempt_id, work_id, 'stopped' if stopped_with_effects(observation) else 'blocked_unknown_effects',
                         now(), canonical(observation), now()))
            if safe:
                db.execute("UPDATE assignment SET state='cancelled',terminal_at=? WHERE work_id=? AND state NOT IN ('completed','failed','cancelled')", (now(), work_id))
                db.execute("UPDATE outbox SET state='cancelled',updated_at=? WHERE assignment_id IN (SELECT assignment_id FROM assignment WHERE work_id=?)", (now(), work_id))
                db.execute("UPDATE lifecycle SET control='cancelled',execution='cancelled',admitted=0 WHERE work_id=?", (work_id,))
                db.execute('DELETE FROM resource WHERE work_id=?', (work_id,))
                db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='unknown_effects'", (work_id,))
            else:
                db.execute("UPDATE lifecycle SET control='blocked_unknown_effects' WHERE work_id=?", (work_id,))
                self._block(db, work_id, 'unknown_effects', 'Cancellation does not prove exact owned processes stopped and an explicit known partial-mutation record')
            self._touch(db, work_id, 'cancel_reconciled', observations)

    def reconcile(self, work_id: str) -> dict:
        # No runtime calls/models. Validate receipt before projection/admission.
        with self.store._connection() as db:
            decisions = [dict(r) for r in db.execute("SELECT * FROM decision WHERE work_id=? AND outcome='approved'", (work_id,))]
        error = None
        for decision in decisions:
            try:
                self.store.verify_decision(decision['decision_id'])
            except Rejected as failure:
                error = str(failure)
        with self.store._transaction() as db:
            self.store._work(db, work_id)
            if error:
                self._block(db, work_id, 'decision_readback', error)
                self._touch(db, work_id, 'readback_blocked', error)
            elif db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='decision_readback'", (work_id,)).rowcount:
                self._touch(db, work_id, 'readback_recovered', {})
            # Retire obsolete challenges without altering approval history.
            db.execute("""UPDATE challenge SET status='stale' WHERE work_id=? AND status='pending' AND
                (concept_revision<>(SELECT current_concept FROM work WHERE work_id=?) OR EXISTS
                (SELECT 1 FROM decision d WHERE d.work_id=challenge.work_id AND d.concept_revision=challenge.concept_revision AND d.outcome='approved'))""", (work_id, work_id))
            source = snapshot_in(db, work_id)
            old = db.execute('SELECT * FROM projection WHERE work_id=?', (work_id,)).fetchone()
            try:
                projection_corrupt = old is not None and digest(json.loads(old['snapshot'])) != old['source_digest']
            except (ValueError, TypeError):
                projection_corrupt = True
            if old and (projection_corrupt
                        or old['source_revision'] > source['source_revision']
                        or (old['source_revision'] == source['source_revision'] and old['source_digest'] != digest(source))):
                if not db.execute("SELECT 1 FROM technical_blocker WHERE work_id=? AND code='projection_mismatch'", (work_id,)).fetchone():
                    self._block(db, work_id, 'projection_mismatch', 'Source/view identity mismatch; approval history is retained', 'Repair projection explicitly from the same ledger record')
                    self._touch(db, work_id, 'projection_mismatch', {})
                return snapshot_in(db, work_id)
            if not db.execute("SELECT 1 FROM technical_blocker WHERE work_id=? AND code='projection_mismatch'", (work_id,)).fetchone():
                self._project(db, source)
            return source

    def _project(self, db: sqlite3.Connection, source: dict) -> None:
        db.execute('INSERT OR REPLACE INTO projection VALUES(?,?,?,?,?)',
                   (source['work_id'], source['source_revision'], digest(source), canonical(source), now()))

    def repair_projection(self, work_id: str) -> dict:
        with self.store._transaction() as db:
            db.execute("DELETE FROM technical_blocker WHERE work_id=? AND code='projection_mismatch'", (work_id,))
            self._touch(db, work_id, 'projection_repaired', {'source': 'same durable ledger; no new decision'})
            source = snapshot_in(db, work_id)
            self._project(db, source)
            return source

    def snapshot(self, work_id: str) -> dict:
        with self.store._connection() as db:
            db.execute('BEGIN')
            return view_in(db, work_id)
