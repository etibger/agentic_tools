"""Coordinator transport tests; synthetic decisions never qualify a human host."""
import asyncio
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from agentic_tools.aos.coordinator_mcp import create_coordinator_server
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store
from agentic_tools.aos.worker import Worker
from test_aos_lifecycle import contract, runtime
from test_aos_store import TEAM


def requests():
    team = [{**TEAM[0], "name": name} for name in ("writer", "assessor")]
    bounds = contract({"digest": "placeholder"})
    bounds.pop("concept_digest")
    bounds.pop("concept_revision")
    intro = dict(operation_id="intro", work_id="MCP", title="Disposable MCP transport",
                 structure=["fixture/"], team=team)
    concept = dict(operation_id="present", work_id="MCP", expected_work_revision=1,
                   package_id="MCP-DI", revision=1, body="Synthetic transport only",
                   acceptance=["Same lifecycle and independent check"], team_envelope=team,
                   authority=bounds["authority"], execution_envelope=bounds)
    return intro, concept


def test_protocol_authority_replay_and_existing_worker_handoff(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "fixture",))
    intro, concept = requests()

    async def run():
        server = create_coordinator_server(store, decision_origin="https://127.0.0.1:8443")
        async with create_connected_server_and_client_session(server) as client:
            async def operation(name, request, error=False):
                result = await client.call_tool("coordinator_operation", {"operation": name, "request": request})
                assert result.isError == error, result
                return result.structuredContent if not error else result

            async def bound(name, op):
                saved = store.read("MCP")
                return await operation(name, dict(operation_id=op, work_id="MCP",
                    expected_work_revision=saved["revision"], digest=saved["concepts"][-1]["digest"]))

            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"interface_capabilities", "read_work", "read_service_status", "read_dashboard",
                             "operation_schema", "coordinator_operation"}
            assert not {"finish_approval", "request_concept_approval", "run_worker"} & tools
            assert not (await client.call_tool("operation_schema", {"operation": "present-concept"})).isError
            first = await operation("introduce-work", intro)
            assert await operation("introduce-work", intro) == first
            await operation("introduce-work", {**intro, "title": "conflicting retry"}, error=True)
            current = await operation("present-concept", concept)
            assert await operation("present-concept", concept) == current
            await operation("present-concept", {**concept, "operation_id": "bad", "approved": True}, error=True)
            await operation("present-concept", {**concept, "operation_id": "bad", "expected_work_revision": "2"}, error=True)
            await bound("register-contract", "register")
            held = await bound("admit", "unapproved")
            assert held["blockers"] and store.read("MCP")["decisions"] == []
            saved = store.read("MCP")
            request = dict(operation_id="decision", work_id="MCP", expected_work_revision=saved["revision"], digest=current["digest"])
            pending = await operation("prepare-decision", request)
            assert pending == await operation("prepare-decision", request)
            assert pending["decision_url"].startswith("https://127.0.0.1:8443/?challenge=")
            await operation("prepare-decision", {**request, "answer": "approve"}, error=True)
            assert store.read("MCP")["decisions"] == []
            # Explicit simulated host seam outside MCP, never a live human decision.
            cap = store.host_capability("test", "SIMULATED:MCP-transport")
            store.finish_approval(pending["challenge_id"], "accept", True, capability=cap,
                                  exact_answer="Synthetic test approval", source_channel="simulated-host",
                                  interaction_reference="SIMULATED:MCP-test")
            decision = await operation("prepare-decision", request)
            assert decision["completed"] and len(store.read("MCP")["decisions"]) == 1
            admitted = await bound("admit", "approved")
            assert admitted["execution"] == "eligible"
            await bound("pause", "pause")
            assert store.read("MCP")["concept_decision"] == "approved"
            await bound("resume", "resume")
            # Worker remains the original engine; the MCP transport creates no jobs.
            with Worker(Lifecycle(store), runtime(tmp_path)) as worker:
                worker.tick()
            service = await client.call_tool("read_service_status", {"work_id": "MCP"})
            assert store.read("MCP")["execution"] == "delivered"
            assert store.read("MCP")["lifecycle"]["resources"] == []
            projected = await client.call_tool("read_dashboard", {"work_id": "MCP"})
            assert projected.structuredContent["source_revision"] == service.structuredContent["source_revision"]
            await operation("pause", dict(operation_id="stale", work_id="MCP", expected_work_revision=1,
                                           digest=current["digest"]), error=True)
        reopened = Store(store.directory, store.worker_writable_roots, read_only=True)
        async with create_connected_server_and_client_session(create_coordinator_server(reopened, decision_origin="https://localhost:8443")) as client:
            read = await client.call_tool("read_work", {"work_id": "MCP"})
            assert read.structuredContent["execution"] == "delivered"
            assert len(read.structuredContent["decisions"]) == 1
    asyncio.run(run())


def test_worker_connection_cannot_mutate(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "fixture",))
    async def run():
        server = create_coordinator_server(store, audience="worker", decision_origin="https://127.0.0.1:8443")
        async with create_connected_server_and_client_session(server) as client:
            assert {t.name for t in (await client.list_tools()).tools} == {"read_work", "interface_capabilities"}
            denied = await client.call_tool("coordinator_operation", {"operation": "introduce-work", "request": requests()[0]})
            assert denied.isError
    asyncio.run(run())


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8443", "https://example.com", "https://user:pass@localhost", "https://localhost/path", "https://localhost?other=1"])
def test_only_configured_loopback_https_decision_origin(tmp_path, origin):
    store = Store(tmp_path / "state", (tmp_path / "fixture",))
    with pytest.raises(Rejected, match="decision_origin"):
        create_coordinator_server(store, decision_origin=origin)


def test_actual_stdio_connection_reads_same_state_after_reconnect(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "fixture",))
    store.introduce(**requests()[0])
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentic_tools.aos.cli", "serve-coordinator",
        "--state-dir", str(store.directory), "--worker-writable-root", str(tmp_path / "fixture"),
        "--decision-origin", "https://127.0.0.1:8443"])
    async def run():
        for _ in range(2):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    caps = await client.call_tool("interface_capabilities", {})
                    assert caps.structuredContent["mcp_coordinator_version"] == 1
                    assert caps.structuredContent["coordinator_accepts_approval_answer"] is False
                    read = await client.call_tool("read_work", {"work_id": "MCP"})
                    expected = store.read("MCP")
                    assert read.structuredContent["operator"]["mcp_workflow_control"] is True
                    assert {k:v for k,v in read.structuredContent.items() if k not in {"observed_at", "operator"}} == {
                        k:v for k,v in expected.items() if k != "observed_at"}
    asyncio.run(run())
