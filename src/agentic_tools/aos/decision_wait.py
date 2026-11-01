"""Observe an exact HTTPS decision during one bounded coordinator tool call.

The existing ledger is the durable notification source. This reader neither
answers a challenge nor dispatches work, and it cannot wake an idle chat.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from .store import Rejected, Store, digest as package_digest, identifier


MAX_WAIT_SECONDS = 30
POLL_SECONDS = 0.25


class _DecisionReader(Store):
    def _connection(self):
        # Bound contention too, including the existing fresh verification path.
        # No database transaction or background task survives an async sleep.
        return super()._connection(timeout=0.05)

    def observe(self, work_id: str, challenge_id: str, digest: str) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            work = self._work(db, work_id)
            challenge = db.execute("SELECT * FROM challenge WHERE challenge_id=?", (challenge_id,)).fetchone()
            if not challenge or challenge["work_id"] != work_id or challenge["digest"] != digest:
                raise Rejected("decision_identity", "Challenge must belong to this exact work and digest")
            concept = db.execute("SELECT * FROM concept WHERE work_id=? AND revision=?",
                                 (work_id, challenge["concept_revision"])).fetchone()
            if (not concept or concept["digest"] != digest
                    or package_digest(json.loads(concept["package"])) != digest):
                raise Rejected("decision_identity", "Saved challenge and immutable proposal do not match")
            decision = db.execute("SELECT * FROM decision WHERE challenge_id=?", (challenge_id,)).fetchone()
            current = work["current_concept"] == challenge["concept_revision"]
            pending_current = (current and work["revision"] == challenge["work_revision"]
                               and work["state"] == "awaiting_concept_approval"
                               and challenge["status"] == "pending")
            run = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle'").fetchone():
                run = db.execute("SELECT execution,control FROM lifecycle WHERE work_id=?", (work_id,)).fetchone()
            result = {"work_id": work_id, "challenge_id": challenge_id, "digest": digest,
                      "concept_revision": challenge["concept_revision"], "work_revision": work["revision"],
                      "current_concept_revision": work["current_concept"], "current_proposal": current,
                      "execution": run["execution"] if run else "held",
                      "control": run["control"] if run else "none", "decision": None,
                      "status": "pending" if pending_current else "superseded"}
            if decision:
                result["decision"] = {key: decision[key] for key in
                                      ("decision_id", "outcome", "created_at")}
                result["decision"]["readback_verified"] = False
                audit = db.execute("SELECT status FROM decision_readback WHERE decision_id=?",
                                   (decision["decision_id"],)).fetchone()
                operation = db.execute("SELECT response FROM operation WHERE operation_id=?",
                                       (challenge["operation_id"],)).fetchone()
                saved = json.loads(operation["response"]) if operation else {}
                if not isinstance(saved, dict):
                    raise ValueError("Decision operation response must be an object")
                if (not audit or audit["status"] != "verified" or not saved.get("receipt")
                        or any(saved.get(key) != decision[key] for key in
                               ("decision_id", "outcome", "work_id", "digest", "concept_revision"))):
                    result.update(status="reconciliation_required", reason="decision_readback_required")
                else:
                    result["status"] = "decision_recorded" if current else "superseded"
            if self.recovery_hold() is not None:
                result.update(status="held", reason="recovery_copy_held")
            elif result["control"] != "none":
                result.update(status="held", reason="work_control")
            elif result["execution"] in {"paused", "cancelled", "delivered"}:
                result.update(status="held", reason="execution_paused_or_terminal")
            return result


async def wait_for_decision(store: Store, work_id: str, challenge_id: str, digest: str,
                            timeout_seconds: int = MAX_WAIT_SECONDS) -> dict:
    """Retryable observation only; timeout/cancellation never records an answer."""
    if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= MAX_WAIT_SECONDS:
        raise Rejected("invalid_wait", "Wait must be an integer from 0 to 30 seconds")
    identifier(work_id)
    identifier(challenge_id)
    started = time.monotonic()
    deadline = started + timeout_seconds
    reader = None
    while True:
        try:
            if reader is None:
                reader = _DecisionReader(store.directory, store.worker_writable_roots, read_only=True)
            result = reader.observe(work_id, challenge_id, digest)
            if result["decision"] and result["status"] not in {"held", "reconciliation_required"}:
                try:
                    reader.verify_decision(result["decision"]["decision_id"])
                except Rejected:
                    result.update(status="reconciliation_required", reason="decision_readback_failed")
                else:
                    result["decision"]["readback_verified"] = True
        except Rejected:
            raise
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            result = {"work_id": work_id, "challenge_id": challenge_id, "digest": digest,
                      "status": "reconciliation_required", "reason": "ledger_unavailable_or_invalid",
                      "decision": None}
        remaining = deadline - time.monotonic()
        if result["status"] != "pending" or remaining <= 0:
            if result["status"] == "pending":
                result["status"] = "timeout"
            return {**result, "waited_seconds": time.monotonic() - started,
                    "automatic_chat_wake": False, "dispatch_performed": False,
                    "next_action": "Read current work; reconcile holds and use existing guarded admission. "
                                   "Timeout ends only this wait. Do not request the same approval again."}
        await asyncio.sleep(min(POLL_SECONDS, remaining))
