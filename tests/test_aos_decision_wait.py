"""Exact saved-answer observation; synthetic host answers are never human qualification."""
import asyncio
import json
import sqlite3
import sys
import time

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from agentic_tools.aos.coordinator import prepare_decision
from agentic_tools.aos.coordinator_mcp import create_coordinator_server
from agentic_tools.aos.decision_wait import wait_for_decision
from agentic_tools.aos.store import Rejected, Store
from test_aos_lifecycle import setup


def pending(tmp_path):
    store, lifecycle, concept = setup(tmp_path, approved=False, admitted=False)
    request = prepare_decision(store, "decision-wait", "Q1", store.read("Q1")["revision"], concept["digest"])
    return store, lifecycle, {"work_id": "Q1", "challenge_id": request["challenge_id"], "digest": concept["digest"]}


def answer(store, request, action="accept", approved=True):
    cap = store.host_capability("test-host", "SIMULATED:decision-wait")
    return store.finish_approval(request["challenge_id"], action, approved, capability=cap,
                                 exact_answer="Synthetic test answer", source_channel="simulated-test",
                                 interaction_reference="SIMULATED:wait-test")


def inventory(store):
    with store._connection() as db:
        return Store._ledger_inventory(db)


@pytest.mark.parametrize("action,approved,outcome", [
    ("accept", True, "approved"), ("decline", None, "declined"),
    ("cancel", None, "cancelled"), ("unavailable", None, "unavailable"),
])
def test_pending_call_observes_separate_writer_without_dispatch(tmp_path, action, approved, outcome):
    store, _, request = pending(tmp_path)

    async def run():
        waiter = asyncio.create_task(wait_for_decision(store, **request, timeout_seconds=2))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        writer = Store(store.directory, store.worker_writable_roots)
        saved = answer(writer, request, action, approved)
        before = inventory(store)
        result = await waiter
        assert result["status"] == "decision_recorded"
        assert result["decision"]["decision_id"] == saved["decision_id"]
        assert result["decision"]["outcome"] == outcome
        assert result["decision"]["readback_verified"] is True
        assert result["work_revision"] == store.read("Q1")["revision"]
        assert result["dispatch_performed"] is False and result["automatic_chat_wake"] is False
        assert inventory(store) == before
        assert store.read("Q1")["lifecycle"]["assignments"] == []

    asyncio.run(run())


def test_two_observers_and_reconnect_retain_one_decision_and_all_ledger_rows(tmp_path):
    store, _, request = pending(tmp_path)

    async def run():
        tasks = [asyncio.create_task(wait_for_decision(store, **request, timeout_seconds=2)) for _ in range(2)]
        await asyncio.sleep(0.02)
        saved = answer(store, request)
        before = inventory(store)
        results = await asyncio.gather(*tasks)
        reopened = Store(store.directory, store.worker_writable_roots, read_only=True)
        results.append(await wait_for_decision(reopened, **request, timeout_seconds=0))
        assert {r["decision"]["decision_id"] for r in results} == {saved["decision_id"]}
        assert all(r["status"] == "decision_recorded" for r in results)
        assert inventory(store) == before

    asyncio.run(run())


def test_timeout_and_cancellation_only_end_observation(tmp_path):
    store, _, request = pending(tmp_path)
    before = inventory(store)

    async def run():
        start = time.monotonic()
        result = await wait_for_decision(store, **request, timeout_seconds=1)
        assert 0.95 <= time.monotonic() - start < 2
        assert result["status"] == "timeout" and result["decision"] is None
        waiter = asyncio.create_task(wait_for_decision(store, **request, timeout_seconds=30))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
        assert inventory(store) == before
        assert len(store.read("Q1")["pending_decisions"]) == 1

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("work_id", "OTHER"), ("challenge_id", "wrong"), ("digest", "sha256:wrong")])
def test_wrong_identity_cannot_observe_or_mutate(tmp_path, field, value):
    store, _, request = pending(tmp_path)
    before = inventory(store)
    with pytest.raises(Rejected):
        asyncio.run(wait_for_decision(store, **{**request, field: value}, timeout_seconds=0))
    assert inventory(store) == before


def test_revised_proposal_does_not_wait_for_or_reuse_old_answer(tmp_path):
    store, _, request = pending(tmp_path)
    old = store.read("Q1")["concepts"][-1]["package"]
    saved = answer(store, request)
    store.present("revise", "Q1", store.read("Q1")["revision"], old["package_id"], 2,
                  "Revised scope", old["acceptance"], old["team_envelope"], old["authority"])
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=30))
    assert result["status"] == "superseded" and not result["current_proposal"]
    assert result["decision"]["decision_id"] == saved["decision_id"]
    assert store.read("Q1")["concept_decision"] == "pending"


def test_stale_unanswered_challenge_returns_immediately(tmp_path):
    store, _, request = pending(tmp_path)
    old = store.read("Q1")["concepts"][-1]["package"]
    store.present("revise-pending", "Q1", store.read("Q1")["revision"], old["package_id"], 2,
                  "Revised scope", old["acceptance"], old["team_envelope"], old["authority"])
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=30))
    assert result["status"] == "superseded"
    assert result["decision"] is None


@pytest.mark.parametrize("stage", ["after_decision_commit", "decision_readback"])
def test_interrupted_receipt_holds_then_recovers_same_decision(tmp_path, stage):
    store, _, request = pending(tmp_path)

    def fail(where):
        if where == stage:
            raise sqlite3.OperationalError("injected interruption")

    store._fault = fail
    with pytest.raises((sqlite3.Error, Rejected)):
        answer(store, request)
    before = inventory(store)
    held = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert held["status"] == "reconciliation_required"
    assert held["decision"]["outcome"] == "approved"
    assert held["decision"]["readback_verified"] is False
    assert inventory(store) == before
    store._fault = lambda _: None
    saved = answer(store, request)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["status"] == "decision_recorded"
    assert result["decision"]["decision_id"] == saved["decision_id"] == held["decision"]["decision_id"]


def test_previously_verified_but_changed_receipt_fails_fresh_readback(tmp_path):
    store, _, request = pending(tmp_path)
    answer(store, request)
    with store._transaction() as db:
        db.execute("UPDATE decision SET exact_answer='altered'")
    before = inventory(store)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["status"] == "reconciliation_required"
    assert result["reason"] == "decision_readback_failed"
    assert inventory(store) == before


@pytest.mark.parametrize("value", [[], None, "not-an-object", True])
def test_non_object_operation_response_is_a_read_only_reconciliation_hold(tmp_path, value):
    store, _, request = pending(tmp_path)
    answer(store, request)
    with store._transaction() as db:
        db.execute("UPDATE operation SET response=? WHERE operation_id='decision-wait'", (json.dumps(value),))
    before = inventory(store)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["status"] == "reconciliation_required"
    assert result["reason"] == "ledger_unavailable_or_invalid"
    assert inventory(store) == before


def test_missing_database_returns_hold_without_recreating_it(tmp_path):
    store, _, request = pending(tmp_path)
    saved = store.path.with_suffix(".saved")
    store.path.rename(saved)
    original = saved.read_bytes()
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["status"] == "reconciliation_required"
    assert not store.path.exists() and saved.read_bytes() == original


def test_failed_commit_remains_pending_and_original_request_can_recover(tmp_path):
    store, _, request = pending(tmp_path)

    def fail(stage):
        if stage == "before_decision_commit":
            raise sqlite3.OperationalError("injected write failure")

    store._fault = fail
    with pytest.raises(Rejected, match="approval_recording_failed"):
        answer(store, request)
    before = inventory(store)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["status"] == "timeout" and result["decision"] is None
    assert inventory(store) == before
    store._fault = lambda _: None
    saved = answer(store, request)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert result["decision"]["decision_id"] == saved["decision_id"]
    assert len(store.read("Q1")["decisions"]) == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_ends_pending_wait_without_a_human_answer(tmp_path, action):
    store, lifecycle, request = pending(tmp_path)
    lifecycle.control("control-pending", "Q1", action)
    before = inventory(store)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=30))
    assert result["status"] == "held" and result["decision"] is None
    assert inventory(store) == before


def test_paused_work_stays_held_and_late_pause_rejects_stale_admission(tmp_path):
    store, lifecycle, request = pending(tmp_path)
    answer(store, request)
    result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    lifecycle.control("pause", "Q1", "pause")
    with pytest.raises(Rejected, match="stale_revision"):
        lifecycle.admit("late-admit", "Q1", expected_work_revision=result["work_revision"], expected_digest=request["digest"])
    held = asyncio.run(wait_for_decision(store, **request, timeout_seconds=0))
    assert held["status"] == "held" and held["execution"] == "paused"
    assert store.read("Q1")["lifecycle"]["assignments"] == []


def test_contended_database_is_bounded_and_does_not_start_background_work(tmp_path):
    store, _, request = pending(tmp_path)
    before = inventory(store)
    with sqlite3.connect(store.path) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        result = asyncio.run(wait_for_decision(store, **request, timeout_seconds=30))
        assert time.monotonic() - start < 1
        assert result["status"] == "reconciliation_required"
        lock.rollback()
    assert inventory(store) == before


def test_protocol_strict_arguments_and_worker_boundary(tmp_path):
    store, _, request = pending(tmp_path)

    async def run():
        server = create_coordinator_server(store, decision_origin="https://127.0.0.1:8443")
        async with create_connected_server_and_client_session(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["wait_for_decision"].annotations.readOnlyHint
            for invalid in ({"timeout_seconds": True}, {"timeout_seconds": "1"}, {"timeout_seconds": 31},
                            {"timeout_seconds": -1}, {"digest": 12}, {"answer": "approve"}):
                result = await client.call_tool("wait_for_decision", {**request, "timeout_seconds": 0, **invalid})
                assert result.isError
            waiting = asyncio.create_task(client.call_tool("wait_for_decision", {**request, "timeout_seconds": 2}))
            await asyncio.sleep(0.02)
            # The pending wait does not prevent other coordinator reads.
            read = await client.call_tool("read_work", {"work_id": "Q1"})
            assert not read.isError and not waiting.done()
            answer(store, request)
            result = await waiting
            assert not result.isError and result.structuredContent["status"] == "decision_recorded"
        worker = create_coordinator_server(store, decision_origin="https://127.0.0.1:8443", audience="worker")
        async with create_connected_server_and_client_session(worker) as client:
            assert "wait_for_decision" not in {t.name for t in (await client.list_tools()).tools}
            assert (await client.call_tool("wait_for_decision", request)).isError

    asyncio.run(run())


def test_real_stdio_pending_tool_and_new_connection_read_same_decision(tmp_path):
    store, _, request = pending(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentic_tools.aos.cli", "serve-coordinator",
        "--state-dir", str(store.directory), "--worker-writable-root", str(store.worker_writable_roots[0]),
        "--decision-origin", "https://127.0.0.1:8443"])

    async def run():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                waiting = asyncio.create_task(client.call_tool("wait_for_decision", {**request, "timeout_seconds": 3}))
                await asyncio.sleep(0.1)
                assert not waiting.done()
                saved = answer(store, request)
                result = await waiting
                assert not result.isError and result.structuredContent["status"] == "decision_recorded"
        before = inventory(store)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                result = await client.call_tool("wait_for_decision", {**request, "timeout_seconds": 0})
                assert result.structuredContent["decision"]["decision_id"] == saved["decision_id"]
        assert inventory(store) == before

    asyncio.run(run())
