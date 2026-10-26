"""Transactional M0 records. Only the trusted bridge receives decision authority.

This module's Python objects are not an OS security boundary. The deployment must
deny workers filesystem mutation of the authoritative state directory. No worker
gets a bridge connection or a host decision capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class Rejected(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise Rejected("invalid_identifier", "Identifiers must be 1-128 letters, digits, or ._:-")
    return value


def private_state_dir(path: Path, worker_writable_roots: tuple[Path, ...]) -> Path:
    """Check the configured separation, with actual sandbox denial still required."""
    path = path.expanduser().absolute()
    if not worker_writable_roots:
        raise Rejected("missing_worker_roots", "Declare all worker-writable roots before opening state")
    # Refuse symlink aliases so an apparent private path cannot resolve into source.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Rejected("unsafe_state_path", "State directory components must not be symlinks")
    for root in worker_writable_roots:
        if path.resolve().is_relative_to(root.expanduser().resolve()):
            raise Rejected("worker_writable_state", "Authoritative state must be outside every worker-writable root")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise Rejected("unsafe_state_permissions", "State directory must be owned by this user with mode 0700")
    return path


class _DecisionCapability:
    """Process-local bearer, created for one host-bound bridge only; never serialized."""

    def __init__(self, host: str, qualification: str):
        self.host = host
        self.qualification = qualification


class _RecoveryCapability:
    """Trusted administrator only; never issued by a host/worker MCP interface."""


class Store:
    def __init__(self, state_dir: Path, worker_writable_roots: tuple[Path, ...], *, read_only: bool = False):
        self.read_only = read_only
        self.worker_writable_roots = worker_writable_roots
        if read_only and not state_dir.expanduser().absolute().is_dir():
            raise Rejected("state_not_found", "State directory does not exist")
        self.directory = private_state_dir(state_dir, worker_writable_roots)
        self.path = self.directory / "aos.sqlite3"
        fd = os.open(self.path, (os.O_RDONLY if read_only else os.O_CREAT | os.O_RDWR) | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise Rejected("unsafe_state_permissions", "State database must be a private user-owned regular file")
        finally:
            os.close(fd)
        self._capabilities: set[_DecisionCapability] = set()
        self._recovery_capabilities: set[_RecoveryCapability] = set()
        self._fault = lambda stage: None
        if read_only:
            return
        self._backup_before_q1()
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work (
                    work_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    title TEXT NOT NULL, introduction TEXT NOT NULL,
                    state TEXT NOT NULL, current_concept INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS concept (
                    work_id TEXT NOT NULL REFERENCES work(work_id),
                    revision INTEGER NOT NULL, digest TEXT NOT NULL, package TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(work_id, revision)
                );
                CREATE TABLE IF NOT EXISTS operation (
                    operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenge (
                    challenge_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, work_id TEXT NOT NULL REFERENCES work(work_id),
                    work_revision INTEGER NOT NULL, concept_revision INTEGER NOT NULL,
                    digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision (
                    decision_id TEXT PRIMARY KEY, challenge_id TEXT UNIQUE NOT NULL,
                    work_id TEXT NOT NULL, concept_revision INTEGER NOT NULL,
                    digest TEXT NOT NULL, outcome TEXT NOT NULL, host TEXT NOT NULL,
                    qualification TEXT NOT NULL, response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(decision)")}
            for name in ("package_snapshot", "package_id", "exact_answer", "source_channel",
                         "interaction_reference", "requested_at", "provenance_kind"):
                if name not in columns:
                    db.execute(f"ALTER TABLE decision ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS decision_readback (
                decision_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL,
                observed_at TEXT NOT NULL)""")
            # Preserve the original structured answer/provenance of pre-Q1 receipts.
            db.execute("""UPDATE decision SET
                package_snapshot=(SELECT package FROM concept c WHERE c.work_id=decision.work_id AND c.revision=decision.concept_revision),
                package_id=json_extract((SELECT package FROM concept c WHERE c.work_id=decision.work_id AND c.revision=decision.concept_revision), '$.package_id'),
                exact_answer=response, source_channel=host, interaction_reference=challenge_id,
                requested_at=(SELECT created_at FROM challenge c WHERE c.challenge_id=decision.challenge_id),
                provenance_kind='legacy-structured-host-response' WHERE package_snapshot IS NULL""")

    def _backup_before_q1(self) -> None:
        """One private, SQLite-consistent rollback copy before the additive migration."""
        with self._connection() as source:
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='decision'").fetchone()
            if not exists or "package_snapshot" in {r[1] for r in source.execute("PRAGMA table_info(decision)")}:
                return
            target = self.directory / "aos.pre-q1.sqlite3"
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return
            os.close(fd)
            with sqlite3.connect(target) as backup:
                source.backup(backup)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5) if self.read_only else sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise Rejected("read_only", "This connection only reads current records")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _prior(self, db: sqlite3.Connection, op: str, payload: Any) -> dict | None:
        identifier(op)
        fingerprint = digest(payload)
        old = db.execute("SELECT * FROM operation WHERE operation_id=?", (op,)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise Rejected("idempotency_conflict", "Operation ID was already used for a different request")
            return json.loads(old["response"])
        pending = db.execute("SELECT fingerprint FROM challenge WHERE operation_id=?", (op,)).fetchone()
        if pending and pending["fingerprint"] != fingerprint:
            raise Rejected("idempotency_conflict", "Operation ID belongs to a different approval request")
        return None

    def _record(self, db: sqlite3.Connection, op: str, payload: Any, response: dict) -> dict:
        db.execute("INSERT INTO operation VALUES(?,?,?)", (op, digest(payload), canonical(response)))
        return response

    def _work(self, db: sqlite3.Connection, work_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM work WHERE work_id=?", (identifier(work_id),)).fetchone()
        if not row:
            raise Rejected("work_not_found", "Unknown work ID")
        return row

    def _expected(self, row: sqlite3.Row, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise Rejected("invalid_revision", "Work revision must be a positive integer")
        if row["revision"] != revision:
            raise Rejected("stale_revision", f"Expected current work revision {row['revision']}")

    def introduce(self, operation_id: str, work_id: str, title: str, structure: list[str], team: list[dict]) -> dict:
        identifier(work_id)
        if not title.strip() or not structure or not team:
            raise Rejected("missing_introduction", "Title, project structure, and provisional team are required")
        for member in team:
            if not all(member.get(field) for field in ("name", "role", "grade", "model", "effort", "scope")):
                raise Rejected("missing_team_fields", "Each proposed member needs name, role, grade, model, effort, scope")
        payload = ["introduce", work_id, title, structure, team]
        with self._transaction() as db:
            if prior := self._prior(db, operation_id, payload):
                return prior
            if db.execute("SELECT 1 FROM work WHERE work_id=?", (work_id,)).fetchone():
                raise Rejected("work_exists", "Work ID already exists")
            timestamp = now()
            introduction = {"structure": structure, "team": team, "team_status": "proposed"}
            db.execute("INSERT INTO work VALUES(?,?,?,?,?,?,?,?)", (
                work_id, 1, title, canonical(introduction), "introduced", None, timestamp, timestamp,
            ))
            return self._record(db, operation_id, payload, {"work_id": work_id, "work_revision": 1, "state": "introduced"})

    def present(self, operation_id: str, work_id: str, expected_work_revision: int,
                package_id: str, revision: int, body: str, acceptance: list[str],
                team_envelope: list[dict], authority: str, *,
                recommendation: str | None = None, alternative: str | None = None,
                consequence: str | None = None, execution_envelope: dict | None = None) -> dict:
        identifier(package_id)
        if type(revision) is not int or revision < 1:
            raise Rejected("invalid_revision", "Concept revision must be a positive integer")
        if not body.strip() or not acceptance or not team_envelope or not authority.strip():
            raise Rejected("incomplete_concept", "Body, acceptance, team envelope, and authority are required")
        package = {"format": "aos-concept-v1", "work_id": work_id, "package_id": package_id,
                   "revision": revision, "body": body, "acceptance": acceptance,
                   "team_envelope": team_envelope, "authority": authority}
        if any(value is not None for value in (recommendation, alternative, consequence)):
            if not all(value and value.strip() for value in (recommendation, alternative, consequence)):
                raise Rejected("incomplete_decision_context", "Recommendation, alternative, and consequence are required together")
            package.update(recommendation=recommendation, alternative=alternative, consequence=consequence)
        if execution_envelope is not None:
            if not isinstance(execution_envelope, dict):
                raise Rejected("invalid_execution_envelope", "Approved execution bounds must be an explicit object")
            package["execution_envelope"] = execution_envelope
        payload = ["present", expected_work_revision, package]
        with self._transaction() as db:
            if prior := self._prior(db, operation_id, payload):
                return prior
            work = self._work(db, work_id)
            self._expected(work, expected_work_revision)
            if revision != (work["current_concept"] or 0) + 1:
                raise Rejected("invalid_concept_revision", "Concept revision must increase by exactly one")
            timestamp = now()
            package_digest = digest(package)
            db.execute("INSERT INTO concept VALUES(?,?,?,?,?)", (work_id, revision, package_digest, canonical(package), timestamp))
            db.execute("UPDATE work SET revision=revision+1,current_concept=?,state='awaiting_concept_approval',updated_at=? WHERE work_id=?", (revision, timestamp, work_id))
            return self._record(db, operation_id, payload, {"work_id": work_id,
                "work_revision": work["revision"] + 1, "concept_revision": revision,
                "digest": package_digest, "state": "awaiting_concept_approval"})

    def read(self, work_id: str) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            work = self._work(db, work_id)
            result = dict(work)
            result["introduction"] = json.loads(result.pop("introduction"))
            concepts = db.execute("SELECT * FROM concept WHERE work_id=? ORDER BY revision", (work_id,)).fetchall()
            result["concepts"] = [{**dict(row), "package": json.loads(row["package"])} for row in concepts]
            result["decisions"] = [{**dict(row), "response": json.loads(row["response"])} for row in db.execute(
                "SELECT * FROM decision WHERE work_id=? ORDER BY created_at", (work_id,))]
            has_readbacks = db.execute("SELECT 1 FROM sqlite_master WHERE name='decision_readback'").fetchone()
            result["decision_readbacks"] = [dict(row) for row in db.execute(
                "SELECT r.* FROM decision_readback r JOIN decision d ON d.decision_id=r.decision_id WHERE d.work_id=?", (work_id,))] if has_readbacks else []
            result["pending_decisions"] = [dict(row) for row in db.execute(
                "SELECT challenge_id,concept_revision,digest,status,created_at FROM challenge WHERE work_id=? AND status='pending'", (work_id,))]
            current_decisions = [d for d in result["decisions"] if d["concept_revision"] == work["current_concept"]]
            answered = [d for d in current_decisions if d["outcome"] in {"approved", "declined", "cancelled"}]
            result["concept_decision"] = answered[-1]["outcome"] if answered else "pending"
            result["execution"] = "held"
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle'").fetchone():
                from .lifecycle import view_in
                result["lifecycle"] = view_in(db, work_id)
                result["execution"] = result["lifecycle"]["execution"]
            result["observed_at"] = now()
            result["dispatch_available"] = False
            result["next_action"] = "Q1 simulation only; inspect lifecycle blockers. Live implementation dispatch is unavailable" if "lifecycle" in result else "M0 qualification only; implementation dispatch is unavailable"
            hold = self.recovery_hold()
            if hold is not None:
                result['recovery_hold'] = hold
            return result

    def recovery_hold(self) -> dict | None:
        """A recovery copy is never an adopted second execution ledger."""
        path = self.directory / 'restore-hold.json'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise Rejected('recovery_copy_held', 'Recovery-copy marker is unsafe; execution remains held')
            value = json.load(stream)
        if not isinstance(value, dict) or value.get('format') != 'aos-recovery-copy-hold-v1' or value.get('adoption_authorized') is not False:
            raise Rejected('recovery_copy_held', 'Recovery-copy marker is invalid; execution remains held')
        return value

    def pending_approvals(self) -> list[dict]:
        """Read current, unresolved challenges for the local human decision page."""
        with self._connection() as db:
            rows = db.execute("""
                SELECT c.*, w.revision AS current_work_revision, w.current_concept,
                       w.state AS work_state, p.package
                FROM challenge c JOIN work w ON w.work_id=c.work_id
                JOIN concept p ON p.work_id=c.work_id AND p.revision=c.concept_revision
                WHERE c.status='pending' ORDER BY c.created_at
            """).fetchall()
            return [{**dict(row), "package": json.loads(row["package"]),
                     "current": row["work_revision"] == row["current_work_revision"]
                     and row["concept_revision"] == row["current_concept"]
                     and row["work_state"] == "awaiting_concept_approval"
                     and digest(json.loads(row["package"])) == row["digest"]}
                    for row in rows]

    def decision_states(self) -> list[dict]:
        """The same reconciled decision/execution fields used by ordinary status."""
        with self._connection() as db:
            work_ids = [r[0] for r in db.execute("SELECT work_id FROM work ORDER BY updated_at DESC")]
        return [self.read(work_id) for work_id in work_ids]

    def host_capability(self, host: str, qualification: str) -> _DecisionCapability:
        if not host.strip() or not qualification.strip():
            raise Rejected("unqualified_human_host", "Host and qualification receipt must be configured by the operator")
        capability = _DecisionCapability(host, qualification)
        self._capabilities.add(capability)
        return capability

    def _authorize(self, capability: object) -> _DecisionCapability:
        if not any(capability is known for known in self._capabilities):
            raise Rejected("forbidden_decision", "Only the configured human-confirmation bridge can record decisions")
        return capability  # type: ignore[return-value]

    def begin_approval(self, operation_id: str, work_id: str, expected_work_revision: int,
                       package_digest: str, *, capability: object) -> dict:
        result = self._begin_approval(operation_id, work_id, expected_work_revision, package_digest, capability=capability)
        if result["completed"]:
            prior = result["result"]
            receipt = self.verify_decision(prior["decision_id"], expected=prior.get("receipt"))
            result["result"] = {**prior, "decision_recorded": True, "approval_recorded": prior["outcome"] == "approved", "readback": receipt}
        return result

    def _begin_approval(self, operation_id: str, work_id: str, expected_work_revision: int,
                        package_digest: str, *, capability: object) -> dict:
        self._authorize(capability)
        payload = ["approve", work_id, expected_work_revision, package_digest]
        with self._transaction() as db:
            if prior := self._prior(db, operation_id, payload):
                return {"completed": True, "result": prior}
            work = self._work(db, work_id)
            self._expected(work, expected_work_revision)
            concept = db.execute("SELECT * FROM concept WHERE work_id=? AND revision=?", (work_id, work["current_concept"])).fetchone()
            if not concept or concept["digest"] != package_digest or work["state"] != "awaiting_concept_approval":
                raise Rejected("stale_concept", "Only the current pending concept can be approved")
            if db.execute("SELECT 1 FROM decision WHERE work_id=? AND concept_revision=? AND outcome IN ('approved','declined','cancelled')",
                          (work_id, work['current_concept'])).fetchone():
                raise Rejected("already_answered", "This exact proposal already has a terminal decision")
            if db.execute("SELECT 1 FROM challenge WHERE operation_id=?", (operation_id,)).fetchone():
                raise Rejected("approval_in_progress", "This request already has an unresolved confirmation; read status before retrying")
            active = db.execute("SELECT created_at FROM challenge WHERE work_id=? AND work_revision=? AND status='pending'",
                                (work_id, work["revision"])).fetchall()
            if active:
                raise Rejected("approval_in_progress", "A current confirmation is already pending for this work")
            challenge_id = str(uuid.uuid4())
            db.execute("INSERT INTO challenge VALUES(?,?,?,?,?,?,?,?,?)", (challenge_id, operation_id, digest(payload), work_id, work["revision"], concept["revision"], package_digest, "pending", now()))
            return {"completed": False, "challenge_id": challenge_id, "package": json.loads(concept["package"]), "digest": package_digest}

    def finish_approval(self, challenge_id: str, action: str, approved: bool | None,
                        *, capability: object, exact_answer: str | None = None,
                        source_channel: str | None = None, interaction_reference: str | None = None) -> dict:
        """Commit first, then independently read/compare before saying recorded.

        A received answer is not a receipt. Failed readback never requests approval
        again: retry the same challenge to read its original durable decision.
        """
        host = self._authorize(capability)
        try:
            result = self._finish_approval(challenge_id, action, approved, host=host,
                exact_answer=exact_answer, source_channel=source_channel,
                interaction_reference=interaction_reference)
        except sqlite3.Error as error:
            raise Rejected("approval_recording_failed", "Approval received, recording failed; coordinator must reconcile the ledger") from error
        self._fault("after_decision_commit")
        receipt = self.verify_decision(result["decision_id"], expected=result.get("receipt"))
        verified = {**result, "decision_recorded": True, "approval_recorded": result["outcome"] == "approved", "readback": receipt}
        try:
            with self._transaction() as db:
                db.execute("UPDATE operation SET response=? WHERE operation_id=(SELECT operation_id FROM challenge WHERE challenge_id=?)",
                           (canonical(verified), challenge_id))
        except sqlite3.Error as error:
            raise Rejected("decision_readback_failed", "Decision committed; receipt publication failed. Coordinator must read back the same decision, without requesting approval again") from error
        return verified

    def _finish_approval(self, challenge_id: str, action: str, approved: bool | None, *,
                         host: _DecisionCapability, exact_answer: str | None,
                         source_channel: str | None, interaction_reference: str | None) -> dict:
        if action not in ("accept", "decline", "cancel", "unavailable") or type(approved) not in (bool, type(None)):
            raise Rejected("invalid_host_response", "Invalid human host response")
        with self._transaction() as db:
            challenge = db.execute("SELECT * FROM challenge WHERE challenge_id=?", (challenge_id,)).fetchone()
            if not challenge:
                raise Rejected("unknown_challenge", "Unknown human confirmation challenge")
            existing = db.execute("SELECT response FROM operation WHERE operation_id=?", (challenge["operation_id"],)).fetchone()
            if existing:
                recorded = db.execute("SELECT * FROM decision WHERE challenge_id=?", (challenge_id,)).fetchone()
                if recorded is None or json.loads(recorded["response"]) != {"action": action, "approved": approved}:
                    raise Rejected("idempotency_conflict", "Approval challenge was already answered differently")
                for key, intended in (("exact_answer", exact_answer), ("source_channel", source_channel),
                                      ("interaction_reference", interaction_reference)):
                    if intended is not None and recorded[key] != intended:
                        raise Rejected("idempotency_conflict", "Approval challenge was already bound to different answer provenance")
                return json.loads(existing["response"])
            work = self._work(db, challenge["work_id"])
            stale = work["revision"] != challenge["work_revision"] or work["current_concept"] != challenge["concept_revision"]
            stale = stale or challenge["status"] != "pending" or work["state"] != "awaiting_concept_approval"
            if db.execute("SELECT 1 FROM decision WHERE work_id=? AND concept_revision=? AND outcome IN ('approved','declined','cancelled')",
                          (work["work_id"], challenge["concept_revision"])).fetchone():
                raise Rejected("already_answered", "This exact proposal already has a terminal decision")
            outcome = "stale" if stale else ("approved" if action == "accept" and approved is True else "declined" if action == "decline" or (action == "accept" and approved is False) else "cancelled" if action == "cancel" else "unavailable")
            decision_id = str(uuid.uuid4())
            timestamp = now()
            host_response = {"action": action, "approved": approved}
            concept = db.execute("SELECT package FROM concept WHERE work_id=? AND revision=?",
                                 (work["work_id"], challenge["concept_revision"])).fetchone()
            package = json.loads(concept["package"])
            if digest(package) != challenge["digest"]:
                raise Rejected("stale_digest", "Challenge no longer matches the exact immutable package; execution held")
            db.execute("""INSERT INTO decision
                (decision_id,challenge_id,work_id,concept_revision,digest,outcome,host,qualification,response,created_at,
                 package_snapshot,package_id,exact_answer,source_channel,interaction_reference,requested_at,provenance_kind)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (decision_id, challenge_id, work["work_id"], challenge["concept_revision"],
                challenge["digest"], outcome, host.host, host.qualification, canonical(host_response), timestamp,
                concept["package"], package["package_id"], exact_answer if exact_answer is not None else canonical(host_response),
                source_channel or host.host, interaction_reference or challenge_id, challenge["created_at"], "qualified-host-response"))
            db.execute("UPDATE challenge SET status=? WHERE challenge_id=?", (outcome, challenge_id))
            if outcome == "approved":
                db.execute("UPDATE work SET revision=revision+1,state='ready',updated_at=? WHERE work_id=?", (timestamp, work["work_id"]))
            elif not stale:
                db.execute("UPDATE work SET revision=revision+1,updated_at=? WHERE work_id=?", (timestamp, work["work_id"]))
            response = {"decision_id": decision_id, "outcome": outcome, "work_id": work["work_id"],
                        "concept_revision": challenge["concept_revision"], "digest": challenge["digest"],
                        "work_revision": work["revision"] + (0 if stale else 1), "dispatch_available": False}
            expected_receipt = dict(db.execute("SELECT * FROM decision WHERE decision_id=?", (decision_id,)).fetchone())
            expected_receipt["response"] = json.loads(expected_receipt["response"])
            response["receipt"] = expected_receipt
            db.execute("INSERT INTO operation VALUES(?,?,?)", (challenge["operation_id"], challenge["fingerprint"], canonical(response)))
            self._fault("before_decision_commit")
            return response

    @staticmethod
    def _ledger_inventory(db: sqlite3.Connection) -> dict:
        """Logical snapshot, including budgets/holds, independent of SQLite page layout."""
        result = {}
        for name, schema in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted([list(row) for row in db.execute(f'SELECT * FROM {quoted}')], key=canonical)
            result[name] = digest({'schema': schema, 'rows': rows})
        return result

    def consistent_backup(self, target: Path) -> dict:
        """Exclusive private SQLite backup. Open this store read-only before live use."""
        target = target.expanduser().absolute()
        private_state_dir(target.parent, self.worker_writable_roots)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        # A failed/partial backup is deliberately retained and cannot be reused.
        with self._connection() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Rejected('backup_failed', 'Backup integrity failed; partial evidence retained')
            inventory = self._ledger_inventory(destination)
        return {'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'tables': inventory, 'created_at': now(), 'source_opened_read_only': self.read_only}

    @classmethod
    def restore_backup(cls, backup: Path, state_dir: Path, worker_writable_roots: tuple[Path, ...],
                       expected_sha256: str) -> dict:
        """Create-only quiescent ledger restore; runtime/budget sidecars are not copied."""
        if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
            raise Rejected('invalid_backup_identity', 'Use the exact SHA-256 returned by backup-ledger')
        backup = backup.expanduser().absolute()
        state_dir = state_dir.expanduser().absolute()
        if os.path.lexists(state_dir):
            raise Rejected('restore_destination_exists', 'Restore requires a fresh private destination; never overwrite current state')
        if not backup.parent.is_dir():
            raise Rejected('backup_not_found', 'The retained backup directory is unavailable')
        private_state_dir(backup.parent, worker_writable_roots)
        fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise Rejected('unsafe_backup_permissions', 'Backup must be a private user-owned regular file')
            if hashlib.sha256(stream.read()).hexdigest() != expected_sha256:
                raise Rejected('backup_identity_mismatch', 'Backup bytes differ from the retained backup identity')
        with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as source:
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Rejected('backup_failed', 'Retained backup integrity failed')
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'work', 'concept', 'decision', 'challenge', 'operation'}.issubset(tables):
                raise Rejected('invalid_backup', 'Backup must contain the existing AOS ledger schema')
            if 'lifecycle' in tables and (source.execute('SELECT 1 FROM lifecycle WHERE admitted=1').fetchone()
                    or source.execute('SELECT 1 FROM resource').fetchone()
                    or source.execute("SELECT 1 FROM assignment WHERE state NOT IN ('completed','failed','cancelled')").fetchone()
                    or source.execute("SELECT 1 FROM owned_stop WHERE state<>'stopped'").fetchone()
                    or source.execute("SELECT 1 FROM worker_status WHERE state='running'").fetchone()):
                raise Rejected('restore_ownership_held', 'Ledger-only restore requires quiescent work without admitted assignments, resources or unresolved runtime ownership')
            inventory = cls._ledger_inventory(source)
            directory = private_state_dir(state_dir, worker_writable_roots)
            hold = {'format': 'aos-recovery-copy-hold-v1', 'status': 'recovery-copy-held',
                    'backup_sha256': expected_sha256, 'recorded_at': now(), 'adoption_authorized': False,
                    'reason': 'Ledger recovery copy only; runtime/shared-budget sidecars and live adoption are not restored'}
            hold_fd = os.open(directory / 'restore-hold.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(hold_fd, 'w', encoding='utf-8') as stream:
                json.dump(hold, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            # Persist the dispatch fence before the copied ledger can exist.
            # A failed sync leaves the partial private destination for inspection.
            for parent in (directory, directory.parent):
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            target = directory / 'aos.sqlite3'
            target_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(target_fd)
            # Preserve any failed/partial destination as evidence; never overwrite it.
            with sqlite3.connect(target) as destination:
                source.backup(destination)
                if (destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                        or cls._ledger_inventory(destination) != inventory):
                    raise Rejected('restore_readback_failed', 'Restored ledger differs from the exact backup table identities')
        restored = cls(directory, worker_writable_roots, read_only=True)
        with restored._connection() as db:
            decision_ids = [row[0] for row in db.execute('SELECT decision_id FROM decision')]
        for decision_id in decision_ids:
            restored.verify_decision(decision_id)
        if hashlib.sha256(backup.read_bytes()).hexdigest() != expected_sha256:
            raise Rejected('backup_identity_mismatch', 'Backup changed during restore; retain the failed destination')
        return {'state_dir': str(directory), 'backup_sha256': expected_sha256,
                'restored_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'tables': inventory,
                'verified_decisions': decision_ids, 'readback': 'verified',
                'recovery_hold': hold,
                'runtime_sidecars_restored': False, 'shared_budget_sidecars_restored': False,
                'runtime_dispatch_restored': False, 'live_migration': False}

    @staticmethod
    def _startup_recovery_evidence(bundle: dict) -> dict:
        """One approved historical message only, not a general approval import API.

        The operator must verify these retained bytes against the original human
        chat. File hashes bind that reviewed evidence, not an identity assertion
        supplied by a fixture or by an MCP client.
        """
        pins = {
            'authority_record': '9cb4aafdc228ba0cc752c618fc74f5556909c33c00006711baf5be61b1a77faa',
            'source_readback': 'de7c28ffb008e8716c978d2fccafc27d7f03acc3ad6626f445f7ae1a5743dca4',
            'document': '65a50fcb8a43b393c41fc15153d2b11474e3b2288e62ce1660345e6e1353d80a',
            'recovery_authorization': 'fdfeaece0245ecdc659b6b92d5fc601a557940a8622db131b159fa5367b1f86c',
            'failed_capture': '461f3da4bb7205a06beb11a56dadd5c47f9df156c0337f71286840297a29d9d3',
        }
        if (not isinstance(bundle, dict) or set(bundle) != {'format', *pins}
                or bundle['format'] != 'aos-startup-chat-recovery-v1'):
            raise Rejected('invalid_recovery_evidence', 'Exact startup recovery evidence bundle required')
        for key, expected in pins.items():
            value = bundle[key]
            if (not isinstance(value, str) or len(value) > 200000
                    or hashlib.sha256(value.encode()).hexdigest() != expected):
                raise Rejected('invalid_recovery_evidence', f'Unverified or changed {key}; technical hold')
        authority = json.loads(bundle['authority_record'])
        source = json.loads(bundle['source_readback'])
        if (source['role'] != 'user' or source['verified_via'] != 'Codex read_thread'
                or source['message_id'] != authority['source_user_message_id']
                or source['thread_id'] != authority['source_thread_id']
                or source['turn_id'] != authority['source_turn_id']
                or source['exact_answer'] != authority['exact_answer']
                or authority['outcome'] != 'approved' or authority['web_submission_recorded'] is not False
                or digest(authority['package_snapshot']) != authority['package_digest']
                or authority['work_id'] != 'AOS-Q2-BUG-002'
                or authority['package_id'] != 'AOS-Q2-STARTUP-EXTENSION'
                or authority['package_revision'] != 1
                or authority['package_digest'] != 'sha256:76eb3aba40de2107a4951e1ec3e4457404dd70ae3f95a855c9a9dc0cb068f30f'):
            raise Rejected('invalid_recovery_evidence', 'Original human source and exact startup package required')
        return authority

    def recovery_capability(self) -> _RecoveryCapability:
        """Trusted local administration only. Never expose this via MCP or workers."""
        if self.read_only:
            raise Rejected('read_only', 'Read-only connections cannot receive recovery authority')
        capability = _RecoveryCapability()
        self._recovery_capabilities.add(capability)
        return capability

    def recover_startup_approval(self, bundle: dict, backup: Path, *, capability: object) -> dict:
        """Add the one evidenced chat approval, preserving the failed web request.

        Requires an exact consistent pre-recovery backup. Writes decision, recovery
        challenge, provenance and operation atomically. No lifecycle, assignment,
        outbox, budget or prior decision/challenge/operation is changed. Retrying
        after a lost reply reads the same original recovery decision.
        """
        if not any(capability is known for known in self._recovery_capabilities):
            raise Rejected('forbidden_recovery', 'Only the trusted local recovery administrator may recover this approval')
        authority = self._startup_recovery_evidence(bundle)
        backup = backup.expanduser().absolute()
        # Reject aliases and unsafe files before reading the retained backup.
        private_state_dir(backup.parent, self.worker_writable_roots)
        info = backup.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or os.path.samefile(backup, self.path)):
            raise Rejected('unsafe_backup', 'An independent private regular backup is required')
        with sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True) as saved:
            if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Rejected('backup_failed', 'Backup is incomplete; execution held')
            before = self._ledger_inventory(saved)
        backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        evidence = canonical(bundle)
        evidence_digest = digest(bundle)
        operation_id = 'recover-startup-chat-01a17354-12fe-7390-9e60-39f0c3643aec'
        payload = ['recover-startup-chat', evidence_digest, backup_digest]
        try:
            with self._transaction() as db:
                prior = self._prior(db, operation_id, payload)
                if prior:
                    result = prior
                else:
                    if self._ledger_inventory(db) != before:
                        raise Rejected('backup_mismatch', 'Ledger changed after backup; take a new consistent backup before recovery')
                    work = self._work(db, authority['work_id'])
                    concept = db.execute('SELECT * FROM concept WHERE work_id=? AND revision=?',
                                         (work['work_id'], work['current_concept'])).fetchone()
                    original = db.execute('SELECT * FROM challenge WHERE challenge_id=?',
                                          (authority['original_web_challenge_id'],)).fetchone()
                    if (not concept or concept['revision'] != 1 or concept['digest'] != authority['package_digest']
                            or concept['package'] != canonical(authority['package_snapshot'])
                            or work['state'] != 'awaiting_concept_approval' or not original
                            or original['work_id'] != work['work_id'] or original['work_revision'] != work['revision']
                            or original['concept_revision'] != 1
                            or original['digest'] != concept['digest'] or original['status'] not in {'pending', 'unavailable'}
                            or db.execute("SELECT 1 FROM decision WHERE work_id=? AND concept_revision=1 AND outcome IN ('approved','declined','cancelled')",
                                          (work['work_id'],)).fetchone()):
                        raise Rejected('recovery_conflict', 'Startup request changed, was withdrawn or already answered; execution held')
                    timestamp = now()
                    challenge_id = str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id))
                    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ':decision'))
                    db.execute('INSERT INTO challenge VALUES(?,?,?,?,?,?,?,?,?)',
                               (challenge_id, operation_id, digest(payload), work['work_id'], work['revision'],
                                1, concept['digest'], 'approved', timestamp))
                    response = {'action': 'recover-original-human-chat-approval', 'approved': True,
                                'source_user_message_id': authority['source_user_message_id'],
                                'web_submission_recorded': False}
                    db.execute('''INSERT INTO decision
                        (decision_id,challenge_id,work_id,concept_revision,digest,outcome,host,qualification,response,created_at,
                         package_snapshot,package_id,exact_answer,source_channel,interaction_reference,requested_at,provenance_kind)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                               (decision_id, challenge_id, work['work_id'], 1, concept['digest'], 'approved',
                                'AOS coordinator historical chat recovery', 'approved recovery evidence ' + evidence_digest,
                                canonical(response), timestamp, concept['package'], authority['package_id'],
                                authority['exact_answer'], authority['source_channel'], authority['source_user_message_id'],
                                timestamp, 'historical-chat-recovery'))
                    db.execute('''CREATE TABLE IF NOT EXISTS decision_recovery (
                        decision_id TEXT PRIMARY KEY REFERENCES decision(decision_id), original_challenge_id TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL, evidence TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
                        before_inventory TEXT NOT NULL, recovered_at TEXT NOT NULL)''')
                    db.execute('INSERT INTO decision_recovery VALUES(?,?,?,?,?,?,?)',
                               (decision_id, original['challenge_id'], evidence_digest, evidence, backup_digest,
                                canonical(before), timestamp))
                    db.execute("UPDATE work SET revision=revision+1,state='ready',updated_at=? WHERE work_id=?",
                               (timestamp, work['work_id']))
                    receipt = dict(db.execute('SELECT * FROM decision WHERE decision_id=?', (decision_id,)).fetchone())
                    receipt['response'] = json.loads(receipt['response'])
                    result = {'decision_id': decision_id, 'work_id': work['work_id'], 'outcome': 'approved',
                              'concept_revision': 1, 'digest': concept['digest'], 'receipt': receipt,
                              'evidence_digest': evidence_digest, 'backup_sha256': backup_digest,
                              'original_challenge_id': original['challenge_id'], 'dispatch_available': False,
                              'execution': 'held', 'web_submission_recorded': False}
                    self._record(db, operation_id, payload, result)
                    self._fault('before_recovery_commit')
        except sqlite3.Error as error:
            raise Rejected('approval_recording_failed', 'Recovery recording failed; preserve evidence and reconcile the same recovery') from error
        self._fault('after_recovery_commit')
        receipt = self.verify_decision(result['decision_id'], expected=result['receipt'])
        return {**result, 'decision_recorded': True, 'approval_recorded': True, 'readback': receipt}

    def verify_decision(self, decision_id: str, *, expected: dict | None = None) -> dict:
        """New read transaction, including immutable snapshot and original provenance."""
        try:
            self._fault("decision_readback")
            with self._connection() as db:
                db.execute("BEGIN")
                row = db.execute("SELECT * FROM decision WHERE decision_id=?", (decision_id,)).fetchone()
                if row is None:
                    raise ValueError("Missing decision")
                receipt = dict(row)
                concept = db.execute("SELECT package,digest FROM concept WHERE work_id=? AND revision=?",
                                     (row["work_id"], row["concept_revision"])).fetchone()
                challenge = db.execute("SELECT * FROM challenge WHERE challenge_id=?", (row["challenge_id"],)).fetchone()
                package = json.loads(row["package_snapshot"])
                if (not concept or concept["package"] != row["package_snapshot"] or concept['digest'] != row['digest']
                        or digest(package) != row["digest"]
                        or package["work_id"] != row["work_id"] or package["revision"] != row["concept_revision"]
                        or package["package_id"] != row["package_id"] or not challenge
                        or challenge["work_id"] != row["work_id"] or challenge["concept_revision"] != row["concept_revision"]
                        or challenge["digest"] != row["digest"] or challenge["created_at"] != row["requested_at"]
                        or not all(row[k] for k in ("exact_answer", "source_channel", "interaction_reference", "created_at"))):
                    raise ValueError("Decision snapshot/provenance mismatch")
                receipt["response"] = json.loads(receipt["response"])
                if row['provenance_kind'] == 'historical-chat-recovery':
                    recovery = db.execute('SELECT * FROM decision_recovery WHERE decision_id=?', (decision_id,)).fetchone()
                    if not recovery:
                        raise ValueError('Missing historical recovery provenance')
                    bundle = json.loads(recovery['evidence'])
                    authority = self._startup_recovery_evidence(bundle)
                    if (digest(bundle) != recovery['evidence_digest']
                            or row['exact_answer'] != authority['exact_answer']
                            or row['source_channel'] != authority['source_channel']
                            or row['interaction_reference'] != authority['source_user_message_id']
                            or recovery['original_challenge_id'] != authority['original_web_challenge_id']
                            or recovery['recovered_at'] != row['created_at']
                            or row['qualification'] != 'approved recovery evidence ' + recovery['evidence_digest']):
                        raise ValueError('Historical recovery provenance mismatch')
                if expected is None:
                    operation = db.execute("SELECT response FROM operation WHERE operation_id=?", (challenge["operation_id"],)).fetchone()
                    expected = json.loads(operation["response"]).get("receipt") if operation else None
                if row['provenance_kind'] == 'historical-chat-recovery':
                    operation = db.execute('SELECT response FROM operation WHERE operation_id=?', (challenge['operation_id'],)).fetchone()
                    saved_result = json.loads(operation['response']) if operation else {}
                    original = db.execute('SELECT * FROM challenge WHERE challenge_id=?', (recovery['original_challenge_id'],)).fetchone()
                    failure = json.loads(bundle['failed_capture'])['pending_challenges'][0]
                    if (not expected or saved_result.get('receipt') != expected
                            or saved_result.get('evidence_digest') != recovery['evidence_digest']
                            or saved_result.get('backup_sha256') != recovery['backup_sha256']
                            or not original or original['work_id'] != row['work_id']
                            or original['digest'] != row['digest'] or original['created_at'] != failure['created_at']):
                        raise ValueError('Historical recovery operation/original request mismatch')
                if expected is not None and receipt != expected:
                    raise ValueError("Readback differs from the intended decision fields")
            self._record_readback(decision_id, "verified", "Exact decision and snapshot read in a new transaction")
            return receipt
        except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
            try:
                self._record_readback(decision_id, "blocked", str(error))
            except sqlite3.Error:
                pass
            raise Rejected("decision_readback_failed", "Approval received; durable readback failed. Execution held for coordinator repair; do not request approval again") from error

    def _record_readback(self, decision_id: str, status: str, detail: str) -> None:
        if self.read_only:
            return
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO decision_readback VALUES(?,?,?,?)", (decision_id, status, detail, now()))
