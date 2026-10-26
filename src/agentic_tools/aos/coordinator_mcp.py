"""Trusted coordinator MCP transport over the existing CLI's contracts.

No decision-answer capability, alternate ledger, dispatcher or process manager
is introduced here. Assigned agents must not receive this connection.
"""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .coordinator import (
    ConceptRequest, IntroductionRequest, PrerequisiteRequest, capabilities,
    prepare_decision, register_contract,
)
from .interface import StrictFastMCP
from .lifecycle import Lifecycle
from .projection import collect, dashboard
from .store import Rejected, Store


class BoundOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    expected_work_revision: int = Field(ge=1)
    digest: str = Field(min_length=1)


Operation = Literal[
    "introduce-work", "present-concept", "register-contract", "prepare-decision",
    "reconcile-prerequisites", "admit", "pause", "resume", "cancel",
]


def create_coordinator_server(store: Store, *, decision_origin: str,
                              audience: Literal["host", "worker"] = "host") -> StrictFastMCP:
    parsed = urlsplit(decision_origin)
    if (parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise Rejected("decision_origin", "Configure the existing loopback HTTPS decision origin")
    origin = decision_origin.rstrip("/")
    server = StrictFastMCP("AOS coordinator", instructions=(
        "Trusted coordinator connection to the existing shared AOS. Introduce structure and team, "
        "then present the complete bounded concept. Read status before each mutation and retain stable "
        "operation IDs across retries. Only the human decision page records approval; tool permissions "
        "and model text do not approve work. Admission checks the existing approvals, prerequisites, "
        "resources and limits. A held result is not permission to dispatch. The existing separately "
        "started worker owns execution and cancellation cleanup. Do not give this connection to workers."
    ))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def interface_capabilities() -> dict[str, Any]:
        """Read exact tool version, coordinator schema, decision route and connection limits."""
        return {**capabilities(), "mcp_coordinator_version": 1, "audience": audience,
                "workflow_methods_exposed": audience == "host", "mcp_workflow_control": audience == "host",
                "decision_transport": "existing loopback HTTPS page", "decision_url": origin + "/",
                "coordinator_accepts_approval_answer": False, "worker_process_control": False,
                "state_directory": str(store.directory.resolve()),
                "worker_writable_roots": [str(p.resolve()) for p in store.worker_writable_roots]}

    @server.tool(annotations=read)
    def read_work(work_id: str) -> dict[str, Any]:
        """Read saved concepts, decisions and execution. Approval and execution holds are separate."""
        return {**store.read(work_id), "operator": capabilities()}

    if audience == "worker":
        return server

    @server.tool(annotations=read)
    def read_service_status(work_id: str) -> dict[str, Any]:
        """Read the same authoritative snapshot as the dashboard, without reconciliation or dispatch."""
        return collect(store, work_id)

    @server.tool(annotations=read)
    def read_dashboard(work_id: str) -> dict[str, Any]:
        """Read current dashboard facts including user decisions, technical blockers and assignments."""
        return dashboard(collect(store, work_id))

    @server.tool(annotations=read)
    def operation_schema(operation: Operation) -> dict[str, Any]:
        """Read the strict request schema for a coordinator operation before constructing its request."""
        return schema_for(operation).model_json_schema()

    @server.tool(annotations=mutate)
    def coordinator_operation(operation: Operation, request: dict[str, Any]) -> dict[str, Any]:
        """Use an existing coordinator operation; request must match operation_schema exactly.

        Introduce/present/reconcile use the existing CLI request schemas. All other
        operations require operation_id, work_id, expected_work_revision and digest.
        prepare-decision returns the human's HTTPS link; it never records an answer.
        admit makes approved work eligible for the existing worker, not a model call.
        pause/resume/cancel preserve owned-effect and approval gates. After any mutation
        read_work again; saved replay results may describe an earlier state.
        """
        # Validate without serializing through the model: preserve the exact supplied
        # package, including omitted optional fields, as the CLI does.
        schema_for(operation).model_validate(request, strict=True)
        if operation == "introduce-work":
            return store.introduce(**request)
        if operation == "present-concept":
            return store.present(**request)
        if operation == "prepare-decision":
            result = prepare_decision(store, request["operation_id"], request["work_id"],
                                      request["expected_work_revision"], request["digest"])
            challenge = result.get("challenge_id")
            return {"completed": result["completed"], "challenge_id": challenge,
                    "digest": result.get("digest"), "result": result.get("result"),
                    "decision_url": origin + ("/?challenge=" + challenge if challenge else "/")}
        lifecycle = Lifecycle(store)
        if operation == "reconcile-prerequisites":
            return lifecycle.reconcile_prerequisites(**request)
        if operation == "register-contract":
            return register_contract(lifecycle, request["operation_id"], request["work_id"],
                                     request["expected_work_revision"], request["digest"])
        args = {"expected_work_revision": request["expected_work_revision"],
                "expected_digest": request["digest"]}
        if operation == "admit":
            return lifecycle.admit(request["operation_id"], request["work_id"], **args)
        return lifecycle.control(request["operation_id"], request["work_id"], operation, **args)

    return server


def schema_for(operation: Operation) -> type[BaseModel]:
    return {"introduce-work": IntroductionRequest, "present-concept": ConceptRequest,
            "reconcile-prerequisites": PrerequisiteRequest}.get(operation, BoundOperation)
