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

from .lifecycle import Lifecycle, stop_conflicts_in
from .store import Rejected, canonical, identifier, now


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
    def __init__(self, lifecycle: Lifecycle, runtime: Runtime | None = None, *, work_id: str | None = None,
                 reconcile_on_enter: bool = True):
        self.lifecycle = lifecycle
        self.runtime = runtime
        self.work_id = identifier(work_id) if work_id is not None else None
        self.reconcile_on_enter = reconcile_on_enter
        self.epoch: int | None = None
        self._lock = None
        self._fault = lambda stage: None

    def __enter__(self) -> 'Worker':
        if self.lifecycle.store.recovery_hold():
            raise Rejected('recovery_copy_held', 'A restored recovery copy cannot run workers or reset runtime/shared-budget ownership')
        path = self.lifecycle.store.directory / 'worker.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock = os.fdopen(fd, 'a+')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock.close()
            self._lock = None
            raise Rejected('worker_owned', 'A foreground worker already owns this service') from error
        try:
            if self.work_id is not None:
                # A narrow owner never adopts another work's unfinished runtime.
                # Historical terminal assignments and retained claims are left alone.
                with self.lifecycle.store._connection() as db:
                    foreign = db.execute("SELECT 1 FROM assignment WHERE work_id<>? "
                        "AND state NOT IN ('completed','failed','cancelled') LIMIT 1", (self.work_id,)).fetchone()
                if foreign:
                    raise Rejected('worker_scope', 'Another work has unresolved ownership; this scoped worker cannot adopt it')
            with self.lifecycle.store._transaction() as db:
                prior = db.execute('SELECT epoch FROM worker_status WHERE singleton=1').fetchone()
                self.epoch = prior[0] + 1 if prior else 1
                db.execute('INSERT OR REPLACE INTO worker_status VALUES(1,?,?,?,?)',
                           (self.epoch, str(uuid.uuid4()), now(), 'running'))
            # Recovery is compulsory before any saved intent can dispatch.
            if self.reconcile_on_enter:
                self.reconcile_sources()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args) -> None:
        try:
            if self.epoch is not None:
                with self.lifecycle.store._transaction() as db:
                    db.execute("UPDATE worker_status SET state='stopped',heartbeat=? WHERE singleton=1 AND epoch=?", (now(), self.epoch))
        finally:
            if self._lock:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None

    def attach_runtime(self, runtime: Runtime) -> None:
        """Construct/attach the product adapter only while this owner holds its lock."""
        if self._lock is None or self.epoch is None:
            raise Rejected('worker_not_owned', 'Acquire worker ownership before attaching a runtime')
        if self.runtime is not None:
            raise Rejected('runtime_attached', 'This worker already has its one runtime')
        if runtime is None:
            raise Rejected('runtime_missing', 'A runtime adapter is required')
        self.runtime = runtime

    def heartbeat(self) -> None:
        if self._lock is None or self.epoch is None:
            raise Rejected('worker_not_owned', 'Worker must hold exclusive ownership')
        with self.lifecycle.store._transaction() as db:
            db.execute('UPDATE worker_status SET heartbeat=? WHERE singleton=1 AND epoch=?', (now(), self.epoch))

    def assignments(self, *, unresolved: bool = True) -> list[dict]:
        clauses, args = [], []
        if unresolved:
            clauses.append("state NOT IN ('completed','failed','cancelled')")
        if self.work_id is not None:
            clauses.append('work_id=?')
            args.append(self.work_id)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.lifecycle.store._connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM assignment' + where + ' ORDER BY created_at,assignment_id', args)]

    def reconcile_sources(self) -> None:
        with self.lifecycle.store._connection() as db:
            query = 'SELECT work_id FROM lifecycle' + (' WHERE work_id=?' if self.work_id is not None else '')
            work_ids = [r[0] for r in db.execute(query, (self.work_id,) if self.work_id is not None else ())]
        for work_id in work_ids:
            self.lifecycle.reconcile(work_id)

    def _expired(self, assignment: dict) -> bool:
        """Keep the original sent-at clock and narrower approved recipe ceiling."""
        if assignment.get('sent_at') is None:
            return False
        with self.lifecycle.store._connection() as db:
            _, contract = self.lifecycle._run(db, assignment['work_id'])
        seconds = contract['max_seconds']
        if type(seconds) is not int or seconds < 1:
            raise Rejected('assignment_deadline', 'Original assignment limit is invalid')
        recipe = contract.get('runtime_recipe')
        recipe_seconds = recipe.get('max_seconds') if isinstance(recipe, dict) else None
        if type(recipe_seconds) is int and recipe_seconds > 0:
            seconds = min(seconds, recipe_seconds)
        try:
            started = datetime.fromisoformat(assignment['sent_at'])
            if started.tzinfo is None:
                raise ValueError('Missing original deadline timezone')
            return (datetime.now(timezone.utc) - started).total_seconds() >= seconds
        except (ValueError, TypeError) as error:
            raise Rejected('assignment_deadline', 'Original assignment deadline is invalid') from error

    def _unlaunched_intent(self, assignment: dict) -> bool:
        """Observe one exact paused sent intent; absence never excuses uncertainty."""
        if self._lock is None or self.runtime is None or assignment.get('state') != 'sent':
            return False
        def current():
            with self.lifecycle.store._connection() as db:
                saved = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment['assignment_id'],)).fetchone()
                run, _ = self.lifecycle._run(db, assignment['work_id'])
                return (saved is not None and dict(saved) == assignment
                        and run['execution'] == 'paused' and run['control'] == 'none'
                        and assignment.get('sent_at') is not None
                        and not db.execute('SELECT 1 FROM owned_stop WHERE attempt_id=?', (assignment['attempt_id'],)).fetchone()
                        and not db.execute('SELECT 1 FROM technical_blocker WHERE work_id=?', (assignment['work_id'],)).fetchone()
                        and not stop_conflicts_in(db, assignment['work_id']))
        try:
            if not current() or self._expired(assignment):
                return False
            observation = self.runtime.lookup(assignment['attempt_id'])
            return (isinstance(observation, dict) and observation.get('state') == 'absent'
                    and observation.get('definitive') is True
                    and observation.get('attempt_id') == assignment['attempt_id']
                    and current() and not self._expired(assignment))
        except (Rejected, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return False

    def stop_owned(self, reason: str = 'supervisor_stopped', detail: str = 'Foreground owner stopped; exact owned cleanup required',
                   *, preserve_absent_intents: bool = False) -> dict:
        """Stop only launched selected assignments, retaining uncertainty and charges."""
        if self._lock is None or self.epoch is None:
            raise Rejected('worker_not_owned', 'Owned stop must finish before ownership is released')
        preserved = {}
        for assignment in self.assignments():
            if assignment['state'] != 'proposed':
                if preserve_absent_intents and self._unlaunched_intent(assignment):
                    preserved[assignment['attempt_id']] = assignment
                else:
                    self._stop_owned(assignment, reason, detail)
        # Another stop or control change may invalidate an earlier observation.
        # The final cleanup receipt names only freshly verified absent intents.
        for attempt_id, assignment in list(preserved.items()):
            if not self._unlaunched_intent(assignment):
                self._stop_owned(assignment, reason, detail)
                del preserved[attempt_id]
        selected = self.assignments()
        with self.lifecycle.store._connection() as db:
            query = 'SELECT state FROM owned_stop' + (' WHERE work_id=?' if self.work_id is not None else '')
            stops = [r[0] for r in db.execute(query, (self.work_id,) if self.work_id is not None else ())]
            work_ids = [self.work_id] if self.work_id is not None else [r[0] for r in db.execute('SELECT work_id FROM lifecycle')]
            conflicts = [item for work_id in work_ids for item in stop_conflicts_in(db, work_id)]
        return {'known_stopped': not conflicts and all(state == 'stopped' for state in stops)
                and all(a['state'] == 'proposed' or a['state'] == 'sent' and a['attempt_id'] in preserved for a in selected),
                'unresolved_attempts': [a['attempt_id'] for a in selected
                                        if a['state'] != 'proposed' and a['attempt_id'] not in preserved],
                'unlaunched_intents': sorted(preserved),
                'stop_conflicts': [item['attempt_id'] for item in conflicts]}

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
        if state == 'startup_failed':
            result = observation.get('result', {})
            proof = result.get('startup_outcome', {})
            acknowledged_failure = (proof.get('kind') == 'acknowledged_no_turn_failure'
                                    and proof.get('method') == 'thread/start' and proof.get('turn_started') is False
                                    and isinstance(observation.get('thread_id'), str) and bool(observation['thread_id'].strip())
                                    and proof.get('thread_id') == observation['thread_id'])
            if (observation.get('attempt_id') != assignment['attempt_id']
                    or observation.get('model_calls_attempted') != 0
                    or (observation.get('thread_id') and not acknowledged_failure) or observation.get('turn_id')
                    or proof.get('attempt_id') != assignment['attempt_id']
                    or (proof.get('kind') not in ('rejected', 'before_thread_request') and not acknowledged_failure)
                    or (proof.get('kind') == 'rejected' and
                        (proof.get('method') != 'thread/start' or not isinstance(proof.get('request_id'), str)
                         or not proof['request_id'] or type(proof.get('code')) is not int))
                    or result.get('status') != 'failed' or not result.get('stopped')
                    or result.get('owned_processes') != [] or result.get('effects_reconciled') is not True):
                self.lifecycle.unknown(assignment['assignment_id'], 'Startup failure lacks authoritative rejection and cleanup evidence')
                return
            # No model work is accepted. The failed startup and any observed
            # session settings remain in evidence; reservation and hold survive.
            self.lifecycle.complete(observation['receipt_id'], assignment['assignment_id'], result,
                                    startup_observation=observation)
            return
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
            if self._expired(assignment):
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
        if self.runtime is None:
            raise Rejected('runtime_missing', 'Attach the runtime before dispatch or reconciliation')
        with self.lifecycle.store._transaction() as db:
            db.execute('UPDATE worker_status SET heartbeat=? WHERE singleton=1 AND epoch=?', (now(), self.epoch))
            query = "SELECT work_id FROM lifecycle WHERE control IN ('cancel_requested','reconciling','blocked_unknown_effects')"
            if self.work_id is not None:
                query += ' AND work_id=?'
            cancelling = [r[0] for r in db.execute(query, (self.work_id,) if self.work_id is not None else ())]
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
            for assignment in self.assignments():
                if assignment['work_id'] in cancelling:
                    continue
                try:
                    if assignment['state'] != 'proposed':
                        if assignment['attempt_id'] in stopped_this_tick:
                            continue
                        expired = self._expired(assignment)
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
