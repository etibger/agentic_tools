"""Read-only service snapshots shared by chat/CLI, dashboard and local history.

Collection never reconciles, dispatches or writes the authoritative store. The
source identity excludes fetch/collection age; those observations are separate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
from threading import Lock

from .store import Rejected, Store, digest, now


def _age(timestamp: str, observed: str) -> float:
    return max(0, (datetime.fromisoformat(observed) - datetime.fromisoformat(timestamp)).total_seconds())


def _source(record: dict) -> dict:
    source = deepcopy(record)
    for key in ('observed_at', 'next_action', 'dispatch_available'):
        source.pop(key, None)
    lifecycle = source.get('lifecycle', {})
    lifecycle.pop('projection', None)
    # These legacy component labels are not evidence of current runtime
    # admission/qualification, which is owned by exact contracts and receipts.
    lifecycle.pop('live_dispatch_available', None)
    lifecycle.pop('qualification', None)
    worker = lifecycle.get('worker', {})
    worker.pop('heartbeat_age_seconds', None)
    worker.pop('health', None)
    return source


def validate_snapshot(snapshot: dict) -> None:
    source = snapshot['source']
    if (snapshot.get('format') != 'aos-service-snapshot-v1'
            or snapshot['source_digest'] != digest(source)
            or snapshot['source_revision'] != source['revision']
            or snapshot['work_id'] != source['work_id']
            or snapshot['source_timestamp'] != source['updated_at']):
        raise Rejected('snapshot_mismatch', 'Service snapshot source identity differs; retain approvals and hold the view')


def collect(store: Store, work_id: str) -> dict:
    """Store.read performs one SQLite read transaction, including lifecycle data."""
    record = store.read(work_id)
    source = _source(record)
    observed = record['observed_at']
    lifecycle = record.get('lifecycle', {})
    projection = lifecycle.get('projection', {'health': 'uncollected'})
    worker = lifecycle.get('worker', {'health': 'unavailable'})
    return {'format': 'aos-service-snapshot-v1', 'work_id': work_id,
            'source_revision': source['revision'], 'source_timestamp': source['updated_at'],
            'source_digest': digest(source), 'source': source,
            'observed_at': observed,
            'collector': {'health': 'current', 'last_success_at': observed, 'last_attempt_at': observed,
                          'failure': None, 'provenance': 'single read-only SQLite transaction'},
            'freshness': {'source_age_seconds': _age(source['updated_at'], observed),
                          'source_stale': _age(source['updated_at'], observed) > 300,
                          'projection': projection, 'worker': worker}}


class SnapshotCollector:
    """Retain the last good source on failure without renewing its observation."""
    def __init__(self, store: Store, work_id: str):
        self.store, self.work_id = store, work_id
        self.last_good = None
        self._lock = Lock()

    def collect(self) -> dict:
        with self._lock:
            return self._collect()

    def _collect(self) -> dict:
        try:
            snapshot = collect(self.store, self.work_id)
            if self.last_good and snapshot['source_revision'] < self.last_good['source_revision']:
                raise Rejected('source_revision_regressed', 'Retain newer source observation; coordinator reconciles rollback')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            if self.last_good is None:
                raise
            snapshot = deepcopy(self.last_good)
            snapshot['collector'].update(health='failed', last_attempt_at=now(),
                                         failure=type(error).__name__ + ': source collection failed')
            snapshot['freshness']['source_age_seconds'] = _age(snapshot['source_timestamp'], now())
            snapshot['freshness']['source_stale'] = snapshot['freshness']['source_age_seconds'] > 300
            return snapshot
        self.last_good = deepcopy(snapshot)
        return snapshot


def _usage(source: dict, observed: str) -> dict:
    usage = source.get('lifecycle', {}).get('usage', {})
    observations = source.get('lifecycle', {}).get('runtime_observations', [])
    recorded = [o.get('usage') or {} for o in observations]
    keys = ('totalTokens', 'inputTokens', 'outputTokens', 'cachedInputTokens')
    counters = {}
    counts = {}
    for key in keys:
        values = [u.get('tokens', {}).get(key) for u in recorded]
        known = [v for v in values if type(v) is int and v >= 0]
        counts[key] = len(known)
        counters[key] = sum(known) if known else None
    # Runtime adapter already deduplicates cumulative observations/resets. Sum
    # only the latest saved observation per attempt; never infer unknown = zero.
    return {'total_tokens': counters.get('totalTokens'), 'input_tokens': counters.get('inputTokens'),
            'output_tokens': counters.get('outputTokens'), 'cached_input_tokens': counters.get('cachedInputTokens'),
            'model_calls': usage.get('model_calls'),
            'partial': not recorded or any(u.get('partial', True) for u in recorded)
                       or any(n < len(recorded) for n in counts.values()),
            'counter_coverage': counts, 'observed_attempts': len(recorded),
            'coverage': usage.get('coverage', 'No runtime usage observations; engineering/main chat excluded')
                        + '; displayed counters sum known values only; missing counters are excluded, never measured zero',
            'source': 'Saved per-attempt runtime observations; token subsets are not additive',
            'observed_at': max((u['at'] for u in recorded if u.get('at')), default=None),
            'api_equivalent': None, 'actual_billed': None}


def dashboard(snapshot: dict) -> dict:
    """Pure renderer mapping. Both surfaces retain the same exact source identity."""
    validate_snapshot(snapshot)
    source = snapshot['source']
    run = source.get('lifecycle', {})
    observed = snapshot['observed_at']
    assignments = run.get('assignments', [])
    pending = run.get('pending_human_decisions', source.get('pending_decisions', []))
    if source['concept_decision'] != 'pending':
        pending = []
    concepts = {c['revision']: c for c in source['concepts']}
    decisions = []
    for answer in source['decisions']:
        if answer['outcome'] not in ('approved', 'declined', 'cancelled'):
            continue
        package = concepts[answer['concept_revision']]['package']
        decisions.append({'id': answer['decision_id'], 'status': {'cancelled': 'withdrawn'}.get(answer['outcome'], answer['outcome']),
                          'question': package['body'], 'recommendation': package.get('recommendation', 'See exact package'),
                          'alternatives': package.get('alternative', 'See exact package'),
                          'consequence': package.get('consequence', package['authority']),
                          'gate': 'Concept', 'requested_by': 'coordinator', 'requested_at': answer['requested_at'],
                          'answer': answer['exact_answer'], 'answered_at': answer['created_at'],
                          'package_revision': answer['concept_revision'], 'package_sha256': answer['digest']})
    for challenge in pending:
        package = concepts[challenge['concept_revision']]['package']
        decisions.append({'id': challenge['challenge_id'], 'status': 'pending', 'question': package['body'],
                          'recommendation': package.get('recommendation', 'Inspect the exact package'),
                          'alternatives': package.get('alternative', 'Decline or cancel'),
                          'consequence': package.get('consequence', package['authority']), 'gate': 'Concept',
                          'requested_by': 'coordinator', 'requested_at': challenge['created_at'],
                          'package_revision': challenge['concept_revision'], 'package_sha256': challenge['digest'],
                          'answer_destination': 'Use the separately qualified human decision surface; this dashboard is read-only.'})
    blockers = [{'title': b['code'], **b} for b in run.get('technical_blockers', [])]
    projection = snapshot['freshness']['projection']
    if projection['health'] == 'stale-or-conflicting':
        blockers.append({'title': 'Recorded projection differs from source', 'owner': 'coordinator',
                         'detail': 'The read-only view uses current source; saved projection requires reconciliation. Approval history is retained.',
                         'next_action': 'Reconcile the same ledger records; do not request approval again.'})
    if snapshot['collector']['health'] != 'current':
        blockers.append({'title': 'Source collection failed', 'owner': 'coordinator',
                         'detail': 'Retaining the last good source observation.', 'next_action': 'Restore collection and read back the source.'})
    if snapshot['freshness']['worker']['health'] == 'stale':
        blockers.append({'title': 'Worker heartbeat is stale', 'owner': 'coordinator',
                         'detail': 'Service activity is unobserved; the recorded execution state does not prove a live worker.',
                         'next_action': 'Explicitly restart/reconcile exact saved ownership before further dispatch.'})
    terminal = {'completed', 'failed', 'cancelled'}
    current = [a for a in assignments if a['state'] not in terminal]
    def node(a: dict, index: int) -> dict:
        status = {'proposed': 'ready', 'sent': 'waiting', 'acknowledged': 'waiting', 'observed_active': 'active',
                  'completed': 'review', 'failed': 'failed', 'cancelled': 'held'}.get(a['state'], 'held')
        if a['kind'] == 'assessment' and a['state'] in ('proposed', 'sent', 'acknowledged'):
            status = 'review'
        return {'id': a['assignment_id'], 'title': a['kind'].replace('_', ' ').title(), 'owner': a['executor'],
                'status': status, 'summary': 'Assignment ' + a['state'] + '; completion does not independently accept the workflow.',
                'wait_reason': ('Observed activity: ' + str(a.get('observed_active_at')) if a.get('observed_active_at') else 'No observed-active receipt'),
                'observed_at': a.get('terminal_at') or a.get('observed_active_at') or a['created_at'],
                'depends_on': [], 'column': index, 'row': 0, 'kind': 'action'}
    history_nodes = [node(a, i) for i, a in enumerate(assignments)]
    current_nodes = [node(a, i) for i, a in enumerate(current)]
    if not current_nodes:
        current_nodes = [{'id': 'current-disposition', 'title': 'Current disposition', 'owner': 'coordinator',
                          'status': 'accepted' if source['execution'] == 'delivered' else 'held',
                          'summary': 'Concept ' + source['concept_decision'] + '; execution ' + source['execution'],
                          'wait_reason': 'See named technical blockers and separate human decisions.',
                          'observed_at': source['updated_at'], 'depends_on': [], 'column': 0, 'row': 0, 'kind': 'action'}]
    entry = {'id': 'concept-entry', 'title': 'Concept decision', 'owner': 'human decision owner',
             'status': 'accepted' if source['concept_decision'] == 'approved' else 'held',
             'summary': 'Concept ' + source['concept_decision'] + '; execution admission is separate.',
             'wait_reason': 'Retain the original answer and provenance; technical holds do not reopen approval.',
             'observed_at': source['updated_at'], 'depends_on': [], 'column': 0, 'row': 0, 'kind': 'decision'}
    for n in current_nodes:
        n['column'] += 1
        n['depends_on'] = ['concept-entry']
    current_nodes.insert(0, entry)
    def graph(nodes, suffix):
        return {'id': source['work_id'].lower().replace('_', '-') + '-' + suffix,
                'title': 'Current work' if suffix == 'current' else 'Attempt history',
                'summary': 'Future independent review is a handoff; it is not a current review queue.',
                'next_action': 'Inspect recorded assignment owners and blockers.', 'source_revision': source['revision'],
                'observed_at': observed, 'provenance': snapshot['source_digest'], 'nodes': nodes}
    delivery = run.get('delivery')
    envelope = concepts.get(source.get('current_concept'), {}).get('package', {}).get('execution_envelope', {})
    budget = {'assignment_limit': envelope.get('max_assignments'), 'assignments_recorded': len(assignments),
              'per_assignment_seconds': envelope.get('max_seconds'), 'repair_limit': envelope.get('max_repairs'),
              'shared_runtime_reservations': 'Not collected by this read-only work view; remaining shared budget is unavailable'}
    return {'schema_version': 1, 'synthetic': False, 'project': source['title'], 'build_id': 'aos-service-snapshot-v1',
            'source_revision': source['revision'], 'source_digest': snapshot['source_digest'],
            'updated_at': source['updated_at'], 'observed_at': observed, 'collector': deepcopy(snapshot['collector']),
            'baseline_at': source['created_at'], 'completed_at': None,
            'current_stage': source['work_id'], 'stage_status': 'Concept ' + source['concept_decision'] + ' · Execution ' + source['execution'],
            'attention_note': 'Approvals and execution holds are separate. Current data is a read-only service snapshot.',
            'next': {'owner': current[0]['executor'] if current else 'coordinator',
                     'action': 'Observe the saved assignment; independent review follows a frozen result.' if current else 'Reconcile disposition and remaining qualification.',
                     'dependency': 'Current authority, checks, ownership and shared limits still apply.'},
            'candidate': (run.get('candidate') or {}).get('identity'),
            'tasks': [{'id': 'workflow', 'title': 'Independent workflow acceptance',
                       'status': 'accepted' if delivery else 'active' if current else 'held',
                       **({'accepted_at': source['updated_at']} if delivery else {})}],
            'history': [], 'decisions': decisions, 'blockers': blockers,
            'phase_flow': graph(current_nodes, 'current'), 'phase_graph': graph(history_nodes or current_nodes, 'history'),
            'agents': [{'role': a['executor'] + ' · ' + a['kind'], 'status': a['state'],
                        'assignment': a['assignment_id'] + ' · requested ' + a['requested_model'] + '/' + a['requested_effort']
                        + ' · runtime ' + str(a.get('runtime_model') or 'unavailable') + '/' + str(a.get('runtime_effort') or 'unavailable')
                        + ' · observed active ' + str(a.get('observed_active_at') or 'unavailable')} for a in current],
            'watchdog': {'id': None, 'status': 'Read-only collector; refresh invokes no models', 'last_audit_at': observed},
            'outputs': {'di': {'status': 'Exact package retained in source ledger'}, 'lessons': {'status': 'Local archive qualification only; live publication unqualified'}},
            'usage': _usage(source, observed), 'evidence': [snapshot['source_digest']],
            'budget': budget,
            'findings': ['Worker health: ' + snapshot['freshness']['worker']['health'],
                         'Recorded assignments: ' + str(len(assignments)) + ' / approved limit ' + str(envelope.get('max_assignments', 'unavailable')),
                         budget['shared_runtime_reservations']]}
