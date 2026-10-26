"""Input boundary for the trusted coordinator CLI, using the existing engine.

These schemas validate files, not human authority. Approval is still collected by
the existing qualified decision route, and assigned agents need OS-level denial
of the authoritative state and decision endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import version_receipt
from .interface import TeamMember
from .lifecycle import Lifecycle
from .store import Rejected, Store, digest
from .checks import source_digest
from agentic_tools.check_adapters import identities as adapter_identities


class ExecutionEnvelope(BaseModel):
    # Adapter-specific bounds stay in the exact package; the adapter validates
    # them again before launch. Do not discard these fields when registering.
    model_config = ConfigDict(extra="allow", strict=True)
    revision: int = Field(ge=1)
    workflow: str
    baseline: str = Field(min_length=1)
    behavior_delta: str = Field(min_length=1)
    required_checks: list[str] = Field(min_length=1)
    compatibility_checks: list[str]
    writer: str = Field(min_length=1)
    assessor: str = Field(min_length=1)
    receiving_owner: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    resources: list[str] = Field(min_length=1)
    prerequisites: dict[str, bool] = Field(min_length=1)
    qualified_route: str = Field(min_length=1)
    max_assignments: int = Field(ge=1)
    max_repairs: int = Field(ge=0, le=2)
    max_seconds: int = Field(ge=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)


class IntroductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    work_id: str
    title: str
    structure: list[str] = Field(min_length=1)
    team: list[TeamMember] = Field(min_length=1)


class ConceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    package_id: str
    revision: int = Field(ge=1)
    body: str
    acceptance: list[str] = Field(min_length=1)
    team_envelope: list[TeamMember] = Field(min_length=1)
    authority: str
    recommendation: str | None = None
    alternative: str | None = None
    consequence: str | None = None
    execution_envelope: ExecutionEnvelope


class PrerequisiteObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    satisfied: bool
    reference: str = Field(min_length=1, max_length=4096)
    observed_at: str = Field(min_length=1)


class PrerequisiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    work_id: str
    expected_work_revision: int = Field(ge=1)
    digest: str
    evidence: dict[str, PrerequisiteObservation] = Field(min_length=1)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("invalid_request", "JSON object contains a duplicate field: " + key)
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


def request_file(path: Path, schema: type[BaseModel]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object, parse_constant=_nonfinite)
        # Keep input fields and adapter bounds byte-for-byte in value terms;
        # validation must not normalize or silently add authority-bearing fields.
        schema.model_validate(value, strict=True)
    except ValidationError as error:
        fields = [".".join(str(part) for part in item["loc"]) or "request" for item in error.errors()]
        raise Rejected("invalid_request", "Invalid or undeclared request fields: " + ", ".join(fields)) from error
    except (ValueError, UnicodeError) as error:
        raise Rejected("invalid_request", "Request must be a valid UTF-8 JSON object") from error
    return value


def register_contract(lifecycle: Lifecycle, operation_id: str, work_id: str,
                      expected_work_revision: int, concept_digest: str) -> dict:
    """Register exactly the saved envelope, with no independent scope copy."""
    record = lifecycle.store.read(work_id)
    concept = next((item for item in record["concepts"] if item["digest"] == concept_digest), None)
    if concept is None:
        raise Rejected("stale_concept", "The intended exact concept is not saved for this work")
    envelope = concept["package"].get("execution_envelope")
    try:
        ExecutionEnvelope.model_validate(envelope, strict=True)
    except ValidationError as error:
        raise Rejected("incomplete_contract", "Saved concept needs a complete bounded execution envelope") from error
    if "concept_revision" in envelope or "concept_digest" in envelope:
        raise Rejected("invalid_execution_envelope", "Concept identity is bound by the saved package, not envelope fields")
    contract = {**envelope, "concept_revision": concept["revision"], "concept_digest": concept_digest}
    return lifecycle.register(operation_id, work_id, contract,
                              expected_work_revision=expected_work_revision, expected_digest=concept_digest)


def prepare_decision(store: Store, operation_id: str, work_id: str,
                     expected_work_revision: int, concept_digest: str) -> dict:
    """Retry preparation by reading the same pending request, never answering it."""
    capability = store.host_capability("Codex local HTTPS decision tab", "operator-preparation-only")
    try:
        return store.begin_approval(operation_id, work_id, expected_work_revision, concept_digest, capability=capability)
    except Rejected as error:
        if error.code != "approval_in_progress":
            raise
        for row in store.pending_approvals():
            if (row["operation_id"] == operation_id and row["current"]
                    and row["work_id"] == work_id and row["work_revision"] == expected_work_revision
                    and row["digest"] == concept_digest
                    and row["fingerprint"] == digest(["approve", work_id, expected_work_revision, concept_digest])):
                return {"completed": False, "challenge_id": row["challenge_id"],
                        "digest": row["digest"], "package": row["package"]}
        raise


def capabilities() -> dict:
    return {**version_receipt(), "coordinator_commands_version": 1,
            "source_digest": source_digest(), "check_adapters": adapter_identities(),
            "check_adapter_contract_version": 1,
            "source_scope": "AOS and reusable check-adapter Python package files; excludes state, configuration, dependencies and documentation",
            "coordinator_commands": ["introduce-work", "present-concept", "register-contract", "prepare-decision", "decide-serve",
                                     "admit", "status", "pause", "resume", "cancel", "worker", "version",
                                     "dashboard-serve", "dashboard-inspect", "dashboard-stop", "reconcile-prerequisites",
                                     "backup-ledger", "restore-ledger", "serve-coordinator"],
            "dashboard_service_version": 1,
            "workflow_control_transport": "trusted coordinator CLI and MCP using the same Store/Lifecycle; explicit Worker process",
            "coordinator_accepts_approval_answer": False, "mcp_workflow_control": True,
            "qualification": "Command availability is not actual-host, desktop or product qualification"}
