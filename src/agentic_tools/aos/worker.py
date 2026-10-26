"""Explicit independent worker with deterministic and bounded Codex adapters.

Real recipes remain conditional on exact approval, prerequisites and separately
qualified runtime boundaries; adapter availability does not qualify live work.
No persistent startup service is installed by this module.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .lifecycle import Lifecycle
from .store import Rejected, canonical, now


class Runtime(Protocol):
    def lookup(self, attempt_id: str) -> dict: ...
    def start(self, assignment: dict) -> dict: ...
    def poll(self, attempt_id: str) -> dict: ...
    def cancel(self, attempt_id: str) -> dict: ...


class FakeRuntime:
    """Persistent simulation; each plan entry provides factual fixture outputs.

    start is idempotent by the durable attempt ID. Unknown lookup is distinct
    from authoritative absence, so losing a reply never implies a safe retry.
    """
    def __init__(self, path: Path, plan: list[dict]):
        self.path = path
        self.plan = plan
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS attempt (
                attempt_id TEXT PRIMARY KEY, observation TEXT NOT NULL, polls INTEGER NOT NULL,
                plan_index INTEGER NOT NULL UNIQUE, result TEXT NOT NULL)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def lookup(self, attempt_id: str) -> dict:
        with self._db() as db:
            row = db.execute('SELECT observation FROM attempt WHERE attempt_id=?', (attempt_id,)).fetchone()
            return json.loads(row[0]) if row else {'state': 'absent', 'definitive': True, 'attempt_id': attempt_id}

    def start(self, assignment: dict) -> dict:
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT observation FROM attempt WHERE attempt_id=?', (assignment['attempt_id'],)).fetchone()
            if prior:
                return json.loads(prior[0])
            index = db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0]
            if index >= len(self.plan):
                raise Rejected('fake_plan_exhausted', 'No approved simulated output for the next assignment')
            step = self.plan[index]
            if step['kind'] != assignment['kind']:
                raise Rejected('fake_plan_mismatch', 'Simulated plan does not match the next permitted action')
            observation = {'state': 'active', 'attempt_id': assignment['attempt_id'],
                           'model': step.get('model', assignment['requested_model']),
                           'effort': step.get('effort', assignment['requested_effort']), 'simulated': True}
            db.execute('INSERT INTO attempt VALUES(?,?,?,?,?)',
                       (assignment['attempt_id'], canonical(observation), step.get('polls', 0), index, canonical(step['result'])))
            return observation

    def poll(self, attempt_id: str) -> dict:
        with self._db() as db:
            row = db.execute('SELECT * FROM attempt WHERE attempt_id=?', (attempt_id,)).fetchone()
            if not row:
                return {'state': 'absent', 'definitive': True, 'attempt_id': attempt_id}
            observation = json.loads(row['observation'])
            if observation['state'] == 'active':
                if row['polls'] > 0:
                    db.execute('UPDATE attempt SET polls=polls-1 WHERE attempt_id=?', (attempt_id,))
                else:
                    observation.update(state='completed', receipt_id='r-' + attempt_id, result=json.loads(row['result']))
                    db.execute('UPDATE attempt SET observation=? WHERE attempt_id=?', (canonical(observation), attempt_id))
            return observation

    def cancel(self, attempt_id: str) -> dict:
        with self._db() as db:
            row = db.execute('SELECT observation,plan_index FROM attempt WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row:
                step = self.plan[row['plan_index']]
                if step.get('unknown_effects'):
                    return {'attempt_id': attempt_id, 'stopped': False, 'owned_processes': ['SIMULATED:unknown-owned-child'],
                            'effects_reconciled': False, 'partial_mutations': 'unknown'}
                observation = json.loads(row['observation'])
                observation['state'] = 'cancelled'
                db.execute('UPDATE attempt SET observation=? WHERE attempt_id=?', (canonical(observation), attempt_id))
            return {'attempt_id': attempt_id, 'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
                    'partial_mutations': 'simulation does not mutate product source'}


class Worker:
    def __init__(self, lifecycle: Lifecycle, runtime: Runtime):
        self.lifecycle = lifecycle
        self.runtime = runtime
        self.epoch: int | None = None
        self._lock = None
        self._fault = lambda stage: None

    def __enter__(self) -> 'Worker':
        path = self.lifecycle.store.directory / 'worker.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock = os.fdopen(fd, 'a+')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock.close()
            self._lock = None
            raise Rejected('worker_owned', 'A foreground worker already owns this service') from error
        with self.lifecycle.store._transaction() as db:
            prior = db.execute('SELECT epoch FROM worker_status WHERE singleton=1').fetchone()
            self.epoch = prior[0] + 1 if prior else 1
            db.execute('INSERT OR REPLACE INTO worker_status VALUES(1,?,?,?,?)',
                       (self.epoch, str(uuid.uuid4()), now(), 'running'))
        # Recovery is compulsory before any saved intent can dispatch.
        self.reconcile_sources()
        return self

    def __exit__(self, *_args) -> None:
        try:
            with self.lifecycle.store._transaction() as db:
                db.execute("UPDATE worker_status SET state='stopped',heartbeat=? WHERE singleton=1 AND epoch=?", (now(), self.epoch))
        finally:
            if self._lock:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None

    def reconcile_sources(self) -> None:
        with self.lifecycle.store._connection() as db:
            work_ids = [r[0] for r in db.execute('SELECT work_id FROM lifecycle')]
        for work_id in work_ids:
            self.lifecycle.reconcile(work_id)

    def _stop_owned(self, assignment: dict, reason: str, detail: str) -> None:
        if not self.lifecycle.request_owned_stop(assignment['assignment_id'], reason, detail):
            return
        try:
            observation = self.runtime.cancel(assignment['attempt_id'])
        except Exception:
            observation = {'attempt_id': assignment['attempt_id'], 'stopped': False,
                           'owned_processes': None, 'effects_reconciled': False, 'partial_mutations': 'unknown'}
        self.lifecycle.owned_stop_result(assignment['assignment_id'], observation)

    def _consume(self, assignment: dict, observation: dict) -> None:
        self.lifecycle.observe_runtime(assignment, observation)
        state = observation.get('state')
        if state in ('active', 'waiting', 'completed', 'failed'):
            if state == 'waiting':
                waiting = observation.get('status_wait', {})
                if (waiting.get('state') != 'waiting'
                        or waiting.get('reason') != 'acknowledged_empty_session_metadata'
                        or not observation.get('thread_id') or not observation.get('turn_id')
                        or waiting.get('thread_id') != observation['thread_id']
                        or waiting.get('turn_id') != observation['turn_id']):
                    self.lifecycle.unknown(assignment['assignment_id'], 'Waiting lacks an exact acknowledged startup identity')
                    return
            with self.lifecycle.store._connection() as db:
                _, contract = self.lifecycle._run(db, assignment['work_id'])
            if (assignment.get('sent_at') is not None and
                    (datetime.now(timezone.utc) - datetime.fromisoformat(assignment['sent_at'])).total_seconds() > contract['max_seconds']):
                self._stop_owned(assignment, 'assignment_deadline', 'Runtime observation arrived after the assignment deadline')
                return
            authority_errors = self.lifecycle.assignment_authority_errors(assignment)
            if authority_errors:
                self._stop_owned(assignment, 'approved_envelope', '; '.join(authority_errors))
                return
            self.lifecycle.acknowledge(assignment['assignment_id'], observation)
            self._fault('after_acknowledgment')
            if observation.get('model') != assignment['requested_model'] or observation.get('effort') != assignment['requested_effort']:
                self._stop_owned(assignment, 'approved_envelope', 'Observed runtime configuration is outside the approved envelope')
                return
            if state in ('completed', 'failed'):
                self.lifecycle.complete(observation['receipt_id'], assignment['assignment_id'], observation['result'])
        elif state not in ('absent',):
            self.lifecycle.unknown(assignment['assignment_id'], 'Runtime cannot establish the saved launch identity/effects')

    def tick(self) -> dict:
        if self._lock is None or self.epoch is None:
            raise Rejected('worker_not_owned', 'Worker must hold exclusive ownership before ticking')
        with self.lifecycle.store._transaction() as db:
            db.execute('UPDATE worker_status SET heartbeat=? WHERE singleton=1 AND epoch=?', (now(), self.epoch))
            cancelling = [r[0] for r in db.execute("SELECT work_id FROM lifecycle WHERE control IN ('cancel_requested','reconciling','blocked_unknown_effects')")]
            db.executemany("UPDATE lifecycle SET control='reconciling' WHERE work_id=?", [(w,) for w in cancelling])
        # Fence first. Only exact owned attempt IDs are given to cancellation.
        for work_id in cancelling:
            owned = self.lifecycle.cancellation_assignments(work_id)
            observations = []
            for assignment in owned:
                try:
                    observations.append(self.runtime.cancel(assignment['attempt_id']))
                except Exception:
                    observations.append({'attempt_id': assignment['attempt_id'], 'stopped': False,
                                         'owned_processes': None, 'effects_reconciled': False})
            self.lifecycle.cancellation_result(work_id, observations)
        self.reconcile_sources()
        # Bound each cycle; completion successors are picked up in this same tick.
        dispatched = 0
        calls_before = getattr(self.runtime, 'model_calls', 0)
        stopped_this_tick = set()
        for _ in range(32):
            progress = False
            for assignment in self.lifecycle.assignments(unresolved=True):
                if assignment['work_id'] in cancelling:
                    continue
                try:
                    if assignment['state'] != 'proposed':
                        if assignment['attempt_id'] in stopped_this_tick:
                            continue
                        with self.lifecycle.store._connection() as db:
                            _, contract = self.lifecycle._run(db, assignment['work_id'])
                        expired = (assignment.get('sent_at') is not None and
                                   (datetime.now(timezone.utc) - datetime.fromisoformat(assignment['sent_at'])).total_seconds() > contract['max_seconds'])
                        authority_errors = self.lifecycle.assignment_authority_errors(assignment)
                        if expired or authority_errors or self.lifecycle.pending_owned_stop(assignment['attempt_id']):
                            reason = 'assignment_deadline' if expired else 'approved_envelope'
                            detail = 'Assignment wall-time limit reached; exact owned stop required' if expired else '; '.join(authority_errors) or 'Saved owned stop remains unresolved'
                            self._stop_owned(assignment, reason, detail)
                            stopped_this_tick.add(assignment['attempt_id'])
                            continue
                    if assignment['state'] == 'proposed':
                        claimed = self.lifecycle.claim(assignment['assignment_id'], self.epoch)
                        if claimed is None:
                            continue
                        self._fault('after_intent_before_launch')
                        if not self.lifecycle.can_launch_saved(claimed):
                            continue
                        observation = self.runtime.start(claimed)
                        self._fault('after_launch_before_ack')
                        self._consume(claimed, observation)
                        dispatched += 1
                        progress = True
                    else:
                        # Find a saved attempt before considering another launch.
                        observation = self.runtime.lookup(assignment['attempt_id'])
                        if observation.get('state') == 'absent' and observation.get('definitive') is True:
                            if assignment['state'] == 'sent':
                                if not self.lifecycle.can_launch_saved(assignment):
                                    continue
                                observation = self.runtime.start(assignment)
                                dispatched += 1
                            else:
                                self.lifecycle.unknown(assignment['assignment_id'], 'Previously acknowledged attempt disappeared; mutation effects are unknown')
                                continue
                        elif observation.get('state') in ('unknown', 'absent'):
                            self.lifecycle.unknown(assignment['assignment_id'], 'Lost acknowledgement cannot be reconciled; no blind relaunch')
                            continue
                        elif observation.get('state') != 'waiting':
                            observation = self.runtime.poll(assignment['attempt_id'])
                        self._consume(assignment, observation)
                        if observation.get('state') in ('completed', 'failed'):
                            progress = True

                except (Rejected, OSError, sqlite3.Error) as error:
                    self.lifecycle.unknown(assignment['assignment_id'], str(error))
            if not progress:
                break
        self.reconcile_sources()
        return {'epoch': self.epoch, 'dispatched': dispatched,
                'model_calls': getattr(self.runtime, 'model_calls', 0) - calls_before,
                'qualification': 'Q1 fake runtime only' if isinstance(self.runtime, FakeRuntime)
                else 'Q2 adapter; real fixture and desktop qualification require independent evidence'}

    def run(self, *, max_cycles: int, interval: float = 30) -> None:
        if max_cycles < 1 or not 0 < interval <= 30:
            raise Rejected('invalid_worker_limit', 'Explicit positive cycle cap and interval up to 30 seconds required')
        for cycle in range(max_cycles):
            self.tick()
            if cycle + 1 < max_cycles:
                time.sleep(interval)
