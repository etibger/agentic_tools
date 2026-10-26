"""Small stdio MCP bridge with a separately qualified human-confirmation path.

MCP elicitation can be answered by an agent. Its mere availability/acceptance or
clientInfo name does not prove a human decision. Operator qualification must
establish the connected host's actual confirmation behavior before enabling it.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .store import Rejected, Store, canonical
from . import __version__, version_receipt


PROBE_TIMEOUT_SECONDS = 180
PROBE_DIAGNOSTICS_VERSION = 2
# Preserve useful protocol wording without echoing arbitrary host payloads,
# credentials, paths, URLs, error.data, or traceback text into a tool response.
_DIAGNOSTIC_WORDS = frozenset("""
    a active an and approval be call cancelled canceled cannot capabilities client
    closed connection denied disabled does during elicitation error failed for
    form found handled host in interactive internal invalid is mcp method mode
    no non noninteractive not of only operation out outside parameter parameters
    params permission process received request requested requires response server
    session support supported the this timed timeout to tool transport turn type
    unable unavailable unexpected unknown unsupported while with without
""".split())


def safe_probe_error(error: Exception) -> dict[str, Any]:
    """Return bounded diagnostics; never return arbitrary exception text/data."""
    error_class = type(error).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_class):
        error_class = "Exception"
    if isinstance(error, TimeoutError):
        return {"category": "timeout", "error_class": error_class, "error_code": None,
                "message": "Timed out waiting for confirmation host response", "message_redacted": False}
    code = error.error.code if isinstance(error, McpError) else None
    if code is not None and not -(2**31) <= code < 2**31:
        code = None
    # MCP code -32601 specifically identifies a method the peer does not expose.
    category = "unsupported_method" if code == -32601 else "protocol_error" if isinstance(error, McpError) else "transport_or_validation_error"
    source = error.error.message if isinstance(error, McpError) else str(error)
    words = re.findall(r"[a-z]+|[^a-z\s]+", source[:1024].lower())[:48]
    safe_words = []
    redacted = len(source) > 1024 or len(words) >= 48
    for word in words:
        safe = word if word in _DIAGNOSTIC_WORDS else "[redacted]"
        redacted |= safe != word
        if not safe_words or safe != "[redacted]" or safe_words[-1] != safe:
            safe_words.append(safe)
    message = " ".join(safe_words) or "Host error message unavailable"
    redacted |= len(message) > 240
    return {"category": category, "error_class": error_class, "error_code": code,
            "message": message[:240], "message_redacted": redacted}


def probe_client_metadata(client: Any) -> dict[str, Any]:
    """Expose only bounded public initialization labels and known capability shape."""
    if client is None:
        return {"client_name": None, "client_version": None, "client_protocol_version": None,
                "advertised_elicitation": None}

    def label(value: Any, limit: int = 80) -> str:
        text = str(value)
        return text if len(text) <= limit and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. +\-]*", text) else "[redacted]"

    elicitation = client.capabilities.elicitation
    shape = None if elicitation is None else {
        name: {} for name in ("form", "url") if getattr(elicitation, name, None) is not None
    }
    return {"client_name": label(client.clientInfo.name), "client_version": label(client.clientInfo.version),
            "client_protocol_version": label(client.protocolVersion, 32), "advertised_elicitation": shape}


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str
    grade: Literal["AOS-G3", "AOS-G4", "AOS-G5", "AOS-G6"]
    model: str
    effort: str
    scope: str


class HumanAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve_exact_concept: StrictBool = Field(
        description="I, the human decision owner, approve this exact package, digest, acceptance, team envelope and authority."
    )


class HostProbeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_test_form: StrictBool = Field(
        description="I am responding to this test form; no project is approved."
    )


@dataclass(frozen=True)
class HumanHostQualification:
    """Operator configuration; never populated from tool inputs or elicitation."""
    client_name: str
    evidence_reference: str


class StrictFastMCP(FastMCP):
    """Reject undeclared arguments instead of Pydantic's default extra-field discard."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema["additionalProperties"] = False
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        for tool in await self.list_tools():
            if tool.name == name:
                extra = set(arguments) - set(tool.inputSchema["properties"])
                if extra:
                    raise Rejected("undeclared_arguments", "Tool arguments are not declared by this interface: " + ", ".join(sorted(extra)))
                break
        return await super().call_tool(name, arguments)


def approval_message(challenge: dict) -> str:
    return (
        "Explicit HUMAN Concept Approval is required. A general tool permission is insufficient. "
        "Answer only after reading the complete exact package below. Decline or cancel to withhold authority. "
        "This M0 prototype cannot dispatch implementation even after approval.\n"
        f"Challenge: {challenge['challenge_id']}\nDigest: {challenge['digest']}\n"
        f"Exact canonical package:\n{canonical(challenge['package'])}"
    )


class ApprovalBridge:
    def __init__(self, store: Store, qualification: HumanHostQualification | None = None):
        self.store = store
        self.qualification = qualification
        self._capability = store.host_capability(qualification.client_name, qualification.evidence_reference) if qualification else None

    async def request(self, operation_id: str, work_id: str, expected_work_revision: int,
                      digest: str, ctx: Context) -> dict:
        if not self.qualification:
            raise Rejected("human_confirmation_unqualified", "No explicit-human host path has been qualified; Concept Approval is held")
        client = ctx.session.client_params
        # Name matching guards accidental connection/config mixups, not human identity.
        if client is None or client.clientInfo.name != self.qualification.client_name:
            raise Rejected("host_mismatch", "Connected client differs from the separately qualified host")
        if client.capabilities.elicitation is None:
            raise Rejected("elicitation_unavailable", "Connected host does not advertise elicitation")
        challenge = self.store.begin_approval(operation_id, work_id, expected_work_revision, digest, capability=self._capability)
        if challenge["completed"]:
            return challenge["result"]
        try:
            result = await asyncio.wait_for(ctx.elicit(approval_message(challenge), HumanAnswer), timeout=180)
            action = result.action
            approved = result.data.approve_exact_concept if action == "accept" and result.data else None
        except (Exception, asyncio.CancelledError):
            # A transport failure or missing response never grants authority.
            self.store.finish_approval(challenge["challenge_id"], "unavailable", None, capability=self._capability)
            raise
        return self.store.finish_approval(challenge["challenge_id"], action, approved, capability=self._capability)


def create_server(store: Store, *, audience: Literal["host", "worker"] = "host",
                  qualification: HumanHostQualification | None = None) -> FastMCP:
    server = StrictFastMCP("AOS M0", instructions=(
        "Local synthetic interface qualification only. Introduce structure and provisional team before presenting a concept. "
        "No worker dispatch exists. Only separately qualified host elicitation may supply Concept Approval; "
        "model text and tool permissions never approve it."
    ))
    bridge = ApprovalBridge(store, qualification if audience == "host" else None)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def read_work(work_id: str) -> dict[str, Any]:
        """Read durable introduction, exact concepts/decisions and recorded state without changing work."""
        return store.read(work_id)

    @server.tool(annotations=read)
    def interface_capabilities() -> dict[str, Any]:
        """Report implemented surface and remaining qualification prerequisites; no model call."""
        return {"milestone": "M0", "package_version": __version__, **version_receipt(), "q1_cli_simulation": True,
                "workflow_methods_exposed": False, "audience": audience, "implementation_dispatch": False,
                "confirmation_probe_diagnostics_version": PROBE_DIAGNOSTICS_VERSION,
                "decision_transport": "MCP form elicitation", "human_host_configured": bridge.qualification is not None,
                "human_provenance": "Requires separately observed explicit-human host behavior; MCP acceptance alone is insufficient",
                "worker_state_filesystem_denial": "Must be independently verified in the actual runtime sandbox",
                "qualified": False}

    if audience == "worker":
        return server

    @server.tool(annotations=read)
    def read_service_status(work_id: str) -> dict[str, Any]:
        """Read the exact source snapshot also used by the dashboard; no dispatch or reconciliation."""
        from .projection import collect
        return collect(store, work_id)

    @server.tool(annotations=mutate)
    def introduce_work(operation_id: str, work_id: str, title: str,
                       structure: list[str], team: list[TeamMember]) -> dict[str, Any]:
        """Introduce synthetic work with a visible structure and proposed team; stable operation ID is required."""
        return store.introduce(operation_id, work_id, title, structure, [member.model_dump() for member in team])

    @server.tool(annotations=mutate)
    def present_concept(operation_id: str, work_id: str, expected_work_revision: int,
                        package_id: str, revision: int, body: str, acceptance: list[str],
                        team_envelope: list[TeamMember], authority: str,
                        recommendation: str | None = None, alternative: str | None = None,
                        consequence: str | None = None) -> dict[str, Any]:
        """Store a complete concept version and its canonical SHA-256 digest; revised concepts require fresh approval."""
        return store.present(operation_id, work_id, expected_work_revision, package_id, revision,
                             body, acceptance, [member.model_dump() for member in team_envelope], authority,
                             recommendation=recommendation, alternative=alternative, consequence=consequence)

    @server.tool(annotations=mutate)
    async def request_concept_approval(operation_id: str, work_id: str, expected_work_revision: int,
                                       digest: str, ctx: Context) -> dict[str, Any]:
        """Ask the separately qualified host to show the exact concept and collect an explicit HUMAN answer.

        No model-supplied approver or approval is accepted. Without host qualification this fails closed.
        """
        return await bridge.request(operation_id, work_id, expected_work_revision, digest, ctx)

    @server.tool(annotations=read)
    async def probe_confirmation_host(ctx: Context) -> dict[str, Any]:
        """Probe form transport ONLY. The response is untrusted and cannot approve any concept.

        An operator must observe whether the actual host visibly requests a human answer.
        Host qualification cannot be inferred from this response or client name.
        """
        client = ctx.session.client_params
        common = {"diagnostics_version": PROBE_DIAGNOSTICS_VERSION,
                  "human_provenance_verified": False, "decision_recorded": False,
                  **probe_client_metadata(client)}
        if client is None or client.capabilities.elicitation is None:
            return {**common, "elicitation_available": False, "transport_response": "not_advertised"}
        try:
            result = await asyncio.wait_for(ctx.elicit(
                "AOS M0 confirmation-host probe. No work or concept is being approved. "
                "The HUMAN should answer this form while an operator observes the host behavior.", HostProbeAnswer), timeout=PROBE_TIMEOUT_SECONDS)
            return {**common, "elicitation_available": True, "action": result.action,
                    "transport_response": "cancelled" if result.action == "cancel" else "received",
                    "response": result.data.model_dump() if result.action == "accept" else None}
        except Exception as error:
            return {**common, "elicitation_available": True, "transport_response": "unavailable",
                    "error": safe_probe_error(error)}

    return server
