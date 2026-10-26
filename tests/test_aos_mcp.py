"""Real MCP protocol with simulated host callbacks; not desktop qualification."""

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.exceptions import McpError

from agentic_tools.aos import interface
from agentic_tools.aos.interface import HumanHostQualification, create_server, probe_client_metadata, safe_probe_error
from agentic_tools.aos.store import Store, canonical

from test_aos_store import TEAM, concept


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state", (tmp_path / "worker",))


def test_protocol_create_present_read_reconnect_and_default_gate(store):
    async def run():
        server = create_server(store)
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            schemas = {tool.name: tool.inputSchema for tool in tools}
            assert set(schemas["request_concept_approval"]["properties"]) == {"operation_id", "work_id", "expected_work_revision", "digest"}
            assert all("ctx" not in schema["properties"] for schema in schemas.values())
            created = await client.call_tool("introduce_work", {
                "operation_id": "intro", "work_id": "M0-PROTOCOL", "title": "protocol fixture",
                "structure": ["synthetic/"], "team": TEAM,
            })
            assert not created.isError
            presented = await client.call_tool("present_concept", {
                "operation_id": "concept", "work_id": "M0-PROTOCOL", "expected_work_revision": 1,
                "package_id": "M0-DI", "revision": 1, "body": "transport fixture only",
                "acceptance": ["No project mutation"], "team_envelope": TEAM, "authority": "No dispatcher",
            })
            assert not presented.isError
            package_digest = presented.structuredContent["digest"]
            denied = await client.call_tool("request_concept_approval", {
                "operation_id": "approve", "work_id": "M0-PROTOCOL", "expected_work_revision": 2,
                "digest": package_digest,
            })
            assert denied.isError
            assert "human_confirmation_unqualified" in denied.content[0].text
        # A new service/client connection reads the same persistent records.
        reopened = Store(store.directory, (store.directory.parent / "worker",))
        async with create_connected_server_and_client_session(create_server(reopened)) as client:
            record = await client.call_tool("read_work", {"work_id": "M0-PROTOCOL"})
            assert not record.isError
            assert record.structuredContent["state"] == "awaiting_concept_approval"
            assert record.structuredContent["decisions"] == []
            assert record.structuredContent["concepts"][0]["digest"] == package_digest
    asyncio.run(run())


@pytest.mark.parametrize(("action", "answer", "outcome"), [("accept", True, "approved"), ("decline", None, "declined"), ("cancel", None, "cancelled")])
def test_simulated_host_round_trip_binds_exact_challenge(store, action, answer, outcome):
    current = concept(store)
    messages = []

    async def elicitation_callback(_context, params):
        messages.append(params.message)
        assert current["digest"] in params.message
        assert canonical(store.read("M0-TEST")["concepts"][0]["package"]) in params.message
        assert "Challenge: " in params.message
        assert params.requestedSchema["properties"]["approve_exact_concept"]["type"] == "boolean"
        return types.ElicitResult(action=action, content={"approve_exact_concept": answer} if answer is not None else None)

    async def run():
        qualification = HumanHostQualification("simulated-test-host", "SIMULATED:callback-test-only")
        server = create_server(store, qualification=qualification)
        async with create_connected_server_and_client_session(
            server, client_info=types.Implementation(name="simulated-test-host", version="test"),
            elicitation_callback=elicitation_callback,
        ) as client:
            arguments = {"operation_id": "human", "work_id": "M0-TEST", "expected_work_revision": 2, "digest": current["digest"]}
            decision = await client.call_tool("request_concept_approval", arguments)
            assert not decision.isError
            assert decision.structuredContent["outcome"] == outcome
            duplicate = await client.call_tool("request_concept_approval", arguments)
            assert duplicate.structuredContent == decision.structuredContent
    asyncio.run(run())
    assert len(messages) == 1
    recorded = store.read("M0-TEST")["decisions"][0]
    assert recorded["qualification"] == "SIMULATED:callback-test-only"
    assert recorded["digest"] == current["digest"]
    assert recorded["challenge_id"] in messages[0]


def test_worker_cannot_list_or_call_decision_tools(store):
    current = concept(store)

    async def run():
        server = create_server(store, audience="worker", qualification=HumanHostQualification("forged", "forged"))
        async with create_connected_server_and_client_session(server) as client:
            assert {tool.name for tool in (await client.list_tools()).tools} == {"read_work", "interface_capabilities"}
            forged = await client.call_tool("request_concept_approval", {
                "operation_id": "forged", "work_id": "M0-TEST", "expected_work_revision": 2,
                "digest": current["digest"], "approver": "human", "approved": True,
            })
            assert forged.isError
    asyncio.run(run())
    assert not store.read("M0-TEST")["decisions"]


def test_matching_name_and_elicitation_does_not_auto_qualify(store):
    current = concept(store)
    calls = []

    async def auto_answer(_context, params):
        calls.append(params)
        assert set(params.requestedSchema["properties"]) == {"confirm_test_form"}
        probe_field = params.requestedSchema["properties"]["confirm_test_form"]
        assert probe_field["type"] == "boolean"
        assert probe_field["description"] == "I am responding to this test form; no project is approved."
        assert "No work or concept is being approved" in params.message
        return types.ElicitResult(action="accept", content={"confirm_test_form": True})

    async def run():
        async with create_connected_server_and_client_session(
            create_server(store), client_info=types.Implementation(name="looks-like-desktop", version="1"),
            elicitation_callback=auto_answer,
        ) as client:
            denied = await client.call_tool("request_concept_approval", {
                "operation_id": "forged", "work_id": "M0-TEST", "expected_work_revision": 2, "digest": current["digest"],
            })
            assert denied.isError
            probe = await client.call_tool("probe_confirmation_host", {})
            assert probe.structuredContent["human_provenance_verified"] is False
            assert probe.structuredContent["decision_recorded"] is False
            assert probe.structuredContent["response"] == {"confirm_test_form": True}
    asyncio.run(run())
    assert len(calls) == 1
    assert not store.read("M0-TEST")["decisions"]


def test_model_cannot_smuggle_an_approver_or_boolean(store):
    current = concept(store)

    async def run():
        async with create_connected_server_and_client_session(create_server(store)) as client:
            response = await client.call_tool("request_concept_approval", {
                "operation_id": "forged", "work_id": "M0-TEST", "expected_work_revision": 2,
                "digest": current["digest"], "approver": "human", "approved": True,
            })
            assert response.isError
            assert "undeclared_arguments" in response.content[0].text
    asyncio.run(run())
    assert not store.read("M0-TEST")["decisions"]


def test_native_stdio_process_roundtrip_and_reconnect(tmp_path):
    state = tmp_path / "state"
    roots = tmp_path / "worker"
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-m", "agentic_tools.aos.cli", "serve", "--state-dir", str(state),
        "--worker-writable-root", str(roots),
    ])

    async def run():
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                created = await client.call_tool("introduce_work", {
                    "operation_id": "native-intro", "work_id": "M0-STDIO", "title": "stdio reconnect fixture",
                    "structure": ["synthetic/"], "team": TEAM,
                })
                assert not created.isError
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                record = await client.call_tool("read_work", {"work_id": "M0-STDIO"})
                assert not record.isError
                assert record.structuredContent["state"] == "introduced"
                assert record.structuredContent["dispatch_available"] is False
    asyncio.run(run())


def test_probe_protocol_error_reports_safe_cause_without_claiming_human_or_approval(store):
    concept(store)

    async def unsupported(_context, _params):
        return types.ErrorData(code=-32601, message="Method not found", data={"credential": "must-never-appear"})

    async def run():
        async with create_connected_server_and_client_session(
            create_server(store), elicitation_callback=unsupported,
            client_info=types.Implementation(name="diagnostic-test-host", version="1.2.3"),
        ) as client:
            response = await client.call_tool("probe_confirmation_host", {})
            assert not response.isError
            record = response.structuredContent
            assert record["diagnostics_version"] == 2
            assert record["client_name"] == "diagnostic-test-host"
            assert record["client_version"] == "1.2.3"
            assert record["client_protocol_version"] == types.LATEST_PROTOCOL_VERSION
            assert record["advertised_elicitation"] == {"form": {}, "url": {}}
            assert record["transport_response"] == "unavailable"
            assert record["error"] == {"category": "unsupported_method", "error_class": "McpError", "error_code": -32601,
                                       "message": "method not found", "message_redacted": False}
            assert record["human_provenance_verified"] is False
            assert record["decision_recorded"] is False
            assert "must-never-appear" not in canonical(record)
    asyncio.run(run())
    assert not store.read("M0-TEST")["decisions"]


def test_probe_timeout_is_distinct_and_fail_closed(store, monkeypatch):
    concept(store)
    monkeypatch.setattr(interface, "PROBE_TIMEOUT_SECONDS", 0.01)

    async def slow_host(_context, _params):
        await asyncio.sleep(1)
        return types.ElicitResult(action="accept", content={"confirm_test_form": True})

    async def run():
        async with create_connected_server_and_client_session(create_server(store), elicitation_callback=slow_host) as client:
            response = await client.call_tool("probe_confirmation_host", {})
            assert not response.isError
            assert response.structuredContent["error"]["category"] == "timeout"
            assert response.structuredContent["decision_recorded"] is False
    asyncio.run(run())
    assert not store.read("M0-TEST")["decisions"]


@pytest.mark.parametrize(("action", "transport"), [("cancel", "cancelled"), ("decline", "received")])
def test_probe_nonaffirmative_answer_is_preserved(store, action, transport):
    concept(store)

    async def cancel(_context, _params):
        return types.ElicitResult(action=action, content=None)

    async def run():
        async with create_connected_server_and_client_session(create_server(store), elicitation_callback=cancel) as client:
            response = await client.call_tool("probe_confirmation_host", {})
            assert response.structuredContent["action"] == action
            assert response.structuredContent["transport_response"] == transport
            assert response.structuredContent["response"] is None
            assert response.structuredContent["human_provenance_verified"] is False
            assert response.structuredContent["decision_recorded"] is False
    asyncio.run(run())
    assert not store.read("M0-TEST")["decisions"]


def test_probe_diagnostics_redact_arbitrary_host_text_and_data():
    error = McpError(types.ErrorData(code=-32603, message=(
        "Elicitation failed: credential=sk-secret-value /Users/alice/auth.json "
        "https://private.example/?token=must-not-leak " + "untrusted " * 1000
    ), data={"token": "also-must-not-leak"}))
    record = safe_probe_error(error)
    assert record["category"] == "protocol_error"
    assert record["error_code"] == -32603
    assert "elicitation failed" in record["message"]
    assert record["message_redacted"] is True
    assert len(record["message"]) <= 240
    for sensitive in ("secret", "alice", "auth.json", "https", "private.example", "token", "must-not-leak", "untrusted"):
        assert sensitive not in canonical(record)


def test_probe_metadata_preserves_empty_capability_and_redacts_nonpublic_labels():
    client = types.InitializeRequestParams(
        protocolVersion="2025-11-25", capabilities=types.ClientCapabilities(elicitation=types.ElicitationCapability()),
        clientInfo=types.Implementation(name="/Users/alice/secret", version="https://private.invalid/token"),
    )
    record = probe_client_metadata(client)
    assert record == {"client_name": "[redacted]", "client_version": "[redacted]",
                      "client_protocol_version": "2025-11-25", "advertised_elicitation": {}}
    assert probe_client_metadata(None)["advertised_elicitation"] is None
