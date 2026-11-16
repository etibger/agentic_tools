"""Host-attested human instructions, separate from implementation approval.

The embedding trusted host must attest original human evidence, including scope
and explicit supersession. Neither coordinator text nor a timestamp is evidence.
This is the same process/OS trust boundary as Store.host_capability, not NLP or
an authentication mechanism. No ingestion capability is exposed through MCP.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .store import Rejected, canonical, digest, now


POLICY = 'host-attested-v1'
SCHEMA = """
CREATE TABLE IF NOT EXISTS human_intent_scope (
 work_id TEXT PRIMARY KEY REFERENCES work(work_id), owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_instruction (
 instruction_id TEXT PRIMARY KEY, evidence TEXT NOT NULL, received_at TEXT NOT NULL,
 host TEXT NOT NULL, qualification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_instruction_conflict (
 fingerprint TEXT PRIMARY KEY, instruction_id TEXT NOT NULL, evidence TEXT NOT NULL,
 received_at TEXT NOT NULL, host TEXT NOT NULL, qualification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_instruction_verification (
 instruction_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, host TEXT NOT NULL,
 qualification TEXT NOT NULL);
"""


class Instruction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    instruction_id: str = Field(min_length=1, max_length=128)
    human_reference: str = Field(min_length=1, max_length=512)
    source_kind: str  # direct_human or authorized_relay; never quote/history
    relay_authorization: str | None = Field(default=None, max_length=512)
    scope: list[str] = Field(min_length=1, max_length=128)  # exact work IDs, or ['*']
    action: str  # stop/pause/cancel/resume; ownership is never inferred
    supersedes: list[str] = Field(max_length=128)
    provenance_available: bool
    context: str  # original or reconstructed; original references survive both

    @model_validator(mode='after')
    def evidence_shape(self):
        if self.source_kind not in {'direct_human', 'authorized_relay'}:
            raise ValueError('Quotes, status questions and chat assertions are not human authority')
        if self.source_kind == 'authorized_relay' and not (self.relay_authorization or '').strip():
            raise ValueError('An authorized relay needs original human relay authorization')
        if self.action not in {'stop', 'pause', 'cancel', 'resume'} or self.context not in {'original', 'reconstructed'}:
            raise ValueError('Explicit lifecycle action and evidence context required')
        if (not self.instruction_id.strip() or not self.human_reference.strip()
                or any(not value.strip() for value in self.scope + self.supersedes)
                or len(set(self.scope)) != len(self.scope) or len(set(self.supersedes)) != len(self.supersedes)
                or ('*' in self.scope and self.scope != ['*']) or self.instruction_id in self.supersedes):
            raise ValueError('Exact nonblank identities, unique scope and acyclic supersession required')
        return self


def current_in(db, work_id: str) -> dict:
    """Resolve only applicable evidence; arrival order and clocks have no authority."""
    # Read-only access to a pre-upgrade ledger must not migrate it.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='human_intent_scope'").fetchone():
        return {'policy': 'legacy', 'disposition': 'unenrolled'}
    owner = db.execute('SELECT owner FROM human_intent_scope WHERE work_id=?', (work_id,)).fetchone()
    if not owner:
        return {'policy': 'legacy', 'disposition': 'unenrolled'}
    records = {r[0]: json.loads(r[1]) for r in db.execute('SELECT instruction_id,evidence FROM human_instruction')}
    applicable = {key: r for key, r in records.items() if '*' in r['scope'] or work_id in r['scope']}
    conflicts = [{**json.loads(r[1]), 'conflict_id': 'conflict-' + r[0][7:]}
                 for r in db.execute('SELECT fingerprint,evidence FROM human_instruction_conflict')]
    conflicts = [r for r in conflicts if '*' in r['scope'] or work_id in r['scope']
                 or '*' in r.get('related_scope', []) or work_id in r.get('related_scope', [])
                 or r['instruction_id'] in applicable]
    conflict_ids = {r['conflict_id'] for r in conflicts}
    problems = []
    superseded = set()
    for key, record in applicable.items():
        for prior in record['supersedes']:
            if prior in conflict_ids:
                superseded.add(prior)
            elif prior not in records:
                problems.append('Missing superseded evidence: ' + prior)
            elif prior not in applicable and prior not in conflict_ids:
                problems.append('Supersession does not apply to this work: ' + prior)
            else:
                superseded.add(prior)
    heads = sorted(set(applicable) - superseded)
    verified = {r[0] for r in db.execute('SELECT instruction_id FROM human_instruction_verification')}
    for key in heads:
        if not applicable[key]['provenance_available'] and key not in verified:
            problems.append('Original human provenance unavailable: ' + key)
    if conflict_ids - superseded:
        problems.append('Conflicting original instruction evidence: ' + ', '.join(sorted(conflict_ids - superseded)))
    if len(heads) != 1:
        problems.append('Current human instruction is missing or conflicting')
    controlling = applicable[heads[0]] if len(heads) == 1 else None
    disposition = 'ambiguous' if problems else ('running' if controlling['action'] == 'resume' else controlling['action'])
    return {'policy': POLICY, 'owner': owner[0], 'disposition': disposition,
            'controlling_instruction': controlling['instruction_id'] if controlling else None,
            'current_heads': heads, 'problems': problems,
            'conflicts': conflicts, 'corroborated_instructions': sorted(verified & set(applicable)),
            'history': [{**r, 'disposition': 'current' if key in heads else 'superseded'}
                        for key, r in sorted(applicable.items())],
            'next_action': ('Resolve original human evidence with the responsible owner' if problems else
                            'Continue within existing approvals and limits' if disposition == 'running' else
                            'Keep work held until an applicable explicit human resume')}


def errors_in(db, work_id: str) -> list[str]:
    current = current_in(db, work_id)
    if current['disposition'] in {'unenrolled', 'running'}:
        if current['disposition'] == 'running':
            row = db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
            if not row or json.loads(row[0])['receiving_owner'] != current['owner']:
                return ['Human-agreed responsibility differs from the current contract owner']
        return []
    return ['Human intent fences execution: ' + current['disposition']]


def enable(lifecycle, work_id: str, owner: str, *, capability: object) -> dict:
    """Sticky enrollment for an existing attended route; never changes its owner."""
    lifecycle.store._authorize(capability)
    with lifecycle.store._transaction() as db:
        _, contract = lifecycle._run(db, work_id)
        if owner != contract['receiving_owner']:
            raise Rejected('handoff_owner', 'Enrollment must retain the registered receiving owner')
        prior = db.execute('SELECT owner FROM human_intent_scope WHERE work_id=?', (work_id,)).fetchone()
        if prior and prior[0] != owner:
            raise Rejected('handoff_owner', 'Existing intent ownership cannot be reassigned')
        if not prior:
            db.execute('INSERT INTO human_intent_scope VALUES(?,?)', (work_id, owner))
            lifecycle._touch(db, work_id, 'human_intent_enabled', {'owner': owner, 'policy': POLICY})
        return current_in(db, work_id)


def require_control_in(db, work_id: str, action: str, instruction_id: str | None) -> None:
    current = current_in(db, work_id)
    if current['disposition'] == 'unenrolled':
        return
    if (current['disposition'] == 'ambiguous' or not instruction_id
            or current['controlling_instruction'] != instruction_id):
        raise Rejected('human_intent', 'Read current human evidence; missing, conflicting or superseded instruction cannot control lifecycle')
    expected = {'pause': {'pause', 'stop'}, 'cancel': {'cancel'}, 'resume': {'running'}}
    if current['disposition'] not in expected[action]:
        raise Rejected('human_intent', 'Lifecycle action conflicts with the current human instruction')


def record(lifecycle, evidence: dict, *, capability: object, expected_current: dict | None = None) -> dict:
    """Called only by a trusted host bridge after inspecting original evidence.

    Unknown provenance may be retained to hold work, never to grant permission.
    Duplicates use original IDs/content, even when received by a different relay.
    Conflicting content is retained as ambiguity, never silently overwritten.
    """
    host = lifecycle.store._authorize(capability)
    instruction = Instruction.model_validate(evidence, strict=True).model_dump()
    with lifecycle.store._transaction() as db:
        for work_id, expected in (expected_current or {}).items():
            if digest(current_in(db, work_id)) != expected:
                raise Rejected('stale_human_intent', 'Instruction evidence changed; refresh the operator form before giving a new instruction')
        before = {r[0]: current_in(db, r[0]) for r in db.execute('SELECT work_id FROM human_intent_scope').fetchall()}
        prior = db.execute('SELECT evidence FROM human_instruction WHERE instruction_id=?',
                           (instruction['instruction_id'],)).fetchone()
        if prior:
            if prior[0] != canonical(instruction):
                original = json.loads(prior[0])
                if not original['provenance_available'] and {**original, 'provenance_available': True} == instruction:
                    added = db.execute('INSERT OR IGNORE INTO human_instruction_verification VALUES(?,?,?,?)',
                                       (instruction['instruction_id'], now(), host.host, host.qualification)).rowcount
                    if added:
                        _touch_scopes(lifecycle, db, instruction['instruction_id'], before)
                    return {'instruction_id': instruction['instruction_id'], 'corroborated': True, 'duplicate': not bool(added)}
                return _conflict(lifecycle, db, instruction, original, host, before)
            return {'instruction_id': instruction['instruction_id'], 'duplicate': True}
        # Reject cycles even for reordered delivery (missing predecessors hold).
        records = {r[0]: json.loads(r[1]) for r in db.execute('SELECT instruction_id,evidence FROM human_instruction')}
        alias = next((r for r in records.values() if r['human_reference'] == instruction['human_reference']), None)
        if alias:
            return _conflict(lifecycle, db, instruction, alias, host, before)
        records[instruction['instruction_id']] = instruction
        pending = list(instruction['supersedes'])
        seen = set()
        while pending:
            key = pending.pop()
            if key == instruction['instruction_id']:
                raise Rejected('instruction_cycle', 'Supersession cannot be cyclic')
            if key not in seen and key in records:
                seen.add(key)
                pending.extend(records[key]['supersedes'])
        db.execute('INSERT INTO human_instruction VALUES(?,?,?,?,?)',
                   (instruction['instruction_id'], canonical(instruction), now(), host.host, host.qualification))
        # Every affected work gets a new source revision, including late arrival
        # of a predecessor, so projections cannot retain a stale authority view.
        _touch_scopes(lifecycle, db, instruction['instruction_id'], before)
        return {'instruction_id': instruction['instruction_id'], 'duplicate': False}


def _conflict(lifecycle, db, instruction, original, host, before):
    observation = {**instruction, 'related_scope': original['scope']}
    fingerprint = digest(observation)
    added = db.execute('INSERT OR IGNORE INTO human_instruction_conflict VALUES(?,?,?,?,?,?)',
                       (fingerprint, instruction['instruction_id'], canonical(observation), now(), host.host, host.qualification)).rowcount
    if added:
        _touch_scopes(lifecycle, db, instruction['instruction_id'], before)
    return {'instruction_id': instruction['instruction_id'], 'conflict': True,
            'conflict_id': 'conflict-' + fingerprint[7:], 'duplicate': not bool(added)}


def _touch_scopes(lifecycle, db, instruction_id, before):
    for row in db.execute('SELECT work_id FROM human_intent_scope').fetchall():
        if current_in(db, row[0]) != before[row[0]]:
            lifecycle._touch(db, row[0], 'human_instruction_evidence_recorded', {'instruction_id': instruction_id})


def require_handoff(lifecycle, work_id: str, instruction_id: str, receiving_owner: str) -> dict:
    """P2/P3 consume this check; it does not transfer ownership or dispatch."""
    with lifecycle.store._connection() as db:
        db.execute('BEGIN')
        require_control_in(db, work_id, 'resume', instruction_id)
        current = current_in(db, work_id)
        if current['disposition'] == 'unenrolled' or receiving_owner != current['owner'] or errors_in(db, work_id):
            raise Rejected('handoff_owner', 'Handoff must retain the agreed responsible owner; role changes require a separate human agreement')
        return current
