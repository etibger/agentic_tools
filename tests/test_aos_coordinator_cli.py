"""Disposable coordinator commands and simulated HTTPS decisions; no model calls.

The synthetic HTTP client below is a test seam, never human qualification. No
CLI command supplies a decision and the workflow uses no database edits.
"""
from contextlib import contextmanager
from http.client import HTTPSConnection
import json
from pathlib import Path
import re
import select
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
from urllib.parse import urlencode

import pytest

from agentic_tools.aos.coordinator import capabilities
from test_aos_lifecycle import assessment, contract, implementation
from test_aos_store import TEAM


class Coordinator:
    def __init__(self, root):
        self.root = root
        self.state = root / "state"
        self.source = root / "fixture"
        self.common = ["--state-dir", str(self.state), "--worker-writable-root", str(self.source)]
        self.number = 0

    def run(self, command, *args, code=0):
        result = subprocess.run([sys.executable, "-m", "agentic_tools.aos.cli", command,
                                 *self.common, *map(str, args)], capture_output=True, text=True, timeout=20)
        assert result.returncode == code, (result.args, result.stdout, result.stderr)
        output = result.stderr if code == 2 else result.stdout
        return json.loads(output) if output.strip() else None

    def request(self, command, value, *, code=0):
        self.number += 1
        path = self.root / f"request-{self.number}.json"
        path.write_text(json.dumps(value))
        return self.run(command, "--request", path, code=code)

    def status(self, work="CLI"):
        return self.run("status", work)

    def control(self, command, operation, current, *, work="CLI", revision=None, digest=None, code=0):
        args = ["--operation-id", operation, "--work-id", work,
                "--expected-work-revision", revision if revision is not None else self.status(work)["revision"],
                "--digest", digest if digest is not None else current["digest"]]
        return self.run(command, *args, code=code), args

    def prepare(self, workflow="bug-fix", work="CLI", **overrides):
        intro = {"operation_id": "intro-" + work, "work_id": work, "title": "Disposable command workflow",
                 "structure": ["fixture/"], "team": TEAM}
        assert self.request("introduce-work", intro) == self.request("introduce-work", intro)
        bounds = contract({"digest": "not-self-referential"}, workflow, **overrides)
        bounds.pop("concept_digest")
        bounds.pop("concept_revision")
        team = [{**TEAM[0], "name": name, "role": role} for name, role in
                [("writer", "Implementer"), ("assessor", "Independent assessor")]]
        request = {"operation_id": "concept-" + work, "work_id": work, "expected_work_revision": 1,
                   "package_id": "CLI-DI", "revision": 1, "body": "Synthetic command fixture only",
                   "acceptance": ["Independent simulated checks pass"], "team_envelope": team,
                   "authority": bounds["authority"], "execution_envelope": bounds}
        current = self.request("present-concept", request)
        assert current == self.request("present-concept", request)
        registered, args = self.control("register-contract", "register-" + work, current, work=work)
        assert registered == self.run("register-contract", *args)
        return current, request

    def worker(self, plan, cycles=1):
        path = self.root / "fake-plan.json"
        path.write_text(json.dumps(plan))
        return self.run("worker", "--fake-plan", path, "--max-cycles", cycles, "--interval", ".01")


@pytest.fixture
def cli(tmp_path):
    return Coordinator(tmp_path)


@contextmanager
def decision_page(cli):
    """Run the real CLI HTTPS route on a fresh owned socket and temporary key."""
    certificate, key = cli.root / "certificate.pem", cli.root / "key.pem"
    openssl = shutil.which("openssl")
    assert openssl, "OpenSSL is required to qualify the disposable HTTPS CLI route"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(certificate), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=15)
    key.chmod(0o600)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    process = subprocess.Popen([sys.executable, "-m", "agentic_tools.aos.cli", "decide-serve", *cli.common,
                                "--certificate", str(certificate), "--key", str(key), "--port", str(port),
                                "--qualification-reference", "SIMULATED:CLI-test-no-human-qualification"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 15)[0], "Disposable HTTPS server did not start"
        assert process.stdout.readline().strip() == origin + "/"
        context = ssl.create_default_context(cafile=str(certificate))
        connection = HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
        yield connection, origin
        connection.close()
    finally:
        process.terminate()
        process.communicate(timeout=10)


def answer(cli, current, *, work="CLI", choice="approve"):
    prepared, args = cli.control("prepare-decision", "decision-" + work, current, work=work)
    assert prepared == cli.run("prepare-decision", *args)  # Pending retries retain one challenge.
    assert prepared["completed"] is False
    with decision_page(cli) as (connection, origin):
        connection.request("GET", "/?" + urlencode({"challenge": prepared["challenge_id"]}))
        response = connection.getresponse()
        body = response.read().decode()
        assert response.status == 200 and current["digest"] in body
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        challenge = re.search(r'name="challenge" value="([^"]+)"', body)[1]
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]

        def post(token, selected=choice, supplied_origin=origin):
            connection.request("POST", "/decide", urlencode({"challenge": challenge, "csrf": token, "choice": selected}),
                               {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie,
                                "Origin": supplied_origin, "Sec-Fetch-Site": "same-origin"})
            posted = connection.getresponse()
            posted.read()
            return posted.status

        assert post("wrong-token") == 403
        assert post(csrf, supplied_origin="https://other.example") == 403
        assert cli.status(work)["decisions"] == []
        assert post(csrf) == 200
        assert post(csrf) == 200  # Durable same-answer retry, one decision.
        assert post(csrf, "decline" if choice == "approve" else "approve") == 409
    completed = cli.run("prepare-decision", *args)
    assert completed["completed"] and completed["result"]["readback"]["digest"] == current["digest"]
    assert len(cli.status(work)["decisions"]) == 1
    return completed


@pytest.mark.parametrize("workflow", ["bug-fix", "feature-change"])
def test_disposable_command_workflow_https_pause_restart_and_automatic_handoff(cli, workflow):
    current, _ = cli.prepare(workflow)
    preapproval, retry = cli.control("admit", "preapproval", current, code=3)
    assert preapproval["execution"] == "held" and preapproval["blockers"]
    assert cli.run("admit", *retry, code=3) == preapproval
    assert cli.status()["lifecycle"]["assignments"] == []
    answer(cli, current)
    admitted, args = cli.control("admit", "admit", current)
    assert admitted["execution"] == "eligible"
    assert cli.run("admit", *args) == admitted
    assert len(cli.status()["lifecycle"]["assignments"]) == 1
    paused, args = cli.control("pause", "pause-before-start", current)
    assert paused["execution"] == "paused" and cli.run("pause", *args) == paused
    plan = [{"kind": "implementation", "result": implementation(), "polls": 1},
            {"kind": "assessment", "result": assessment(workflow)}]
    cli.worker(plan)
    assert not (cli.state / "fake-runtime.sqlite3").exists() or count_attempts(cli) == 0
    resumed, resume_args = cli.control("resume", "resume-before-start", current)
    assert resumed["execution"] == "eligible" and cli.run("resume", *resume_args) == resumed
    cli.worker(plan)
    active = cli.status()["lifecycle"]
    assert active["worker"]["state"] == "stopped" and count_attempts(cli) == 1
    assert cli.control("pause", "pause-active", current)[0]["execution"] == "paused"
    cli.worker(plan)  # Reconnect, consume completion, preserve the explicit pause.
    paused = cli.status()["lifecycle"]
    assert paused["execution"] == "paused" and count_attempts(cli) == 1
    assert cli.control("resume", "resume-active", current)[0]["execution"] == "eligible"
    cli.worker(plan, cycles=2)
    delivered = cli.status()["lifecycle"]
    assert delivered["execution"] == "delivered" and delivered["delivery"]["accepted"] is True
    assert [item["kind"] for item in delivered["assignments"]] == ["implementation", "assessment"]
    assert delivered["resources"] == [] and count_attempts(cli) == 2
    assert delivered["worker"]["epoch"] >= 4
    cli.worker(plan)
    assert count_attempts(cli) == 2
    assert cli.control("pause", "terminal-pause", current, code=2)[0]["error"] == "terminal_execution"


def count_attempts(cli):
    with sqlite3.connect(cli.state / "fake-runtime.sqlite3") as db:
        return db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0]


@pytest.mark.parametrize("unknown", [False, True])
def test_cancel_uses_existing_owned_stop_and_retains_uncertain_resources(cli, unknown):
    current, _ = cli.prepare()
    answer(cli, current)
    cli.control("admit", "admit", current)
    plan = [{"kind": "implementation", "result": implementation(), "polls": 1000, "unknown_effects": unknown}]
    cli.worker(plan)
    assert count_attempts(cli) == 1
    result, args = cli.control("cancel", "cancel", current)
    assert result["control"] == "cancel_requested" and cli.run("cancel", *args) == result
    assert cli.status()["lifecycle"]["resources"]
    cli.worker(plan)
    status = cli.status()["lifecycle"]
    assert status["control"] == ("blocked_unknown_effects" if unknown else "cancelled")
    assert bool(status["resources"]) is unknown
    assert count_attempts(cli) == 1


def test_wrong_identity_stale_revision_and_operation_reuse_fail_closed(cli):
    current, _ = cli.prepare()
    before = cli.status()
    error, _ = cli.control("admit", "wrong-digest", current, digest="sha256:wrong", code=2)
    assert error["error"] == "stale_concept"
    error, _ = cli.control("pause", "stale-revision", current, revision=1, code=2)
    assert error["error"] == "stale_revision"
    assert cli.status()["revision"] == before["revision"]
    paused, args = cli.control("pause", "stable-operation", current)
    assert cli.run("pause", *args) == paused
    args[args.index("--expected-work-revision") + 1] = cli.status()["revision"]
    assert cli.run("pause", *args, code=2)["error"] == "idempotency_conflict"


def test_revised_concept_retains_approval_and_cannot_admit_old_contract(cli):
    current, request = cli.prepare()
    answer(cli, current)
    request.update(operation_id="revision-2", revision=2, expected_work_revision=cli.status()["revision"],
                   body="Different synthetic scope")
    updated = cli.request("present-concept", request)
    assert cli.status()["decisions"][0]["outcome"] == "approved"
    assert cli.control("admit", "old-digest", current, code=2)[0]["error"] == "stale_concept"
    held, _ = cli.control("admit", "new-digest-old-contract", updated, code=3)
    assert held["execution"] == "held"
    assert cli.status()["lifecycle"]["assignments"] == []
    assert len(cli.status()["decisions"]) == 1


@pytest.mark.parametrize("choice,qualified", [("decline", True), ("approve", False)])
def test_declined_package_and_false_prerequisite_never_dispatch(cli, choice, qualified):
    current, _ = cli.prepare(prerequisites={"q0": qualified, "isolated_fixture": True})
    answer(cli, current, choice=choice)
    assert cli.control("admit", "admit", current, code=3)[0]["execution"] == "held"
    cli.worker([])
    assert count_attempts(cli) == 0
    assert cli.status()["concept_decision"] == ("declined" if choice == "decline" else "approved")


def test_held_resume_records_one_revision_and_retries_without_projection_conflict(cli):
    current, _ = cli.prepare(prerequisites={"q0": False, "isolated_fixture": True})
    answer(cli, current)
    cli.control("pause", "pause-unqualified", current)
    before = cli.status()["revision"]
    held, args = cli.control("resume", "resume-held", current, code=3)
    assert held["execution"] == "paused" and held["blockers"]
    after = cli.status()["revision"]
    assert after == before + 1
    assert cli.run("resume", *args, code=3) == held
    status = cli.status()
    assert status["revision"] == after
    assert not any(item["code"] == "projection_mismatch" for item in status["lifecycle"]["technical_blockers"])
    assert status["lifecycle"]["assignments"] == []


def test_saved_adapter_bounds_are_preserved_by_contract_registration(cli):
    extras = {"runtime_recipe_digest": "sha256:synthetic-recipe",
              "shared_qualification_budget": {"budget_id": "synthetic-only", "max_assignments": 2, "max_seconds": 60}}
    cli.prepare(**extras)
    with sqlite3.connect(cli.state / "aos.sqlite3") as db:
        saved = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='CLI'").fetchone()[0])
    for key, value in extras.items():
        assert saved[key] == value


@pytest.mark.parametrize("payload", [[], {"approved": True}, {"operation_id": "bad", "work_id": "BAD",
    "title": "Invalid", "structure": ["fixture"], "team": TEAM, "approved": True}])
def test_untrusted_request_fields_are_rejected_before_state_creation(cli, payload):
    assert cli.request("introduce-work", payload, code=2)["error"] == "invalid_request"
    assert not cli.state.exists()


def test_duplicate_json_fields_cannot_replace_scope(cli):
    path = cli.root / "duplicate.json"
    path.write_text('{"work_id":"A","work_id":"B"}')
    assert cli.run("introduce-work", "--request", path, code=2)["error"] == "invalid_request"
    assert not cli.state.exists()


@pytest.mark.parametrize("field,value", [("max_seconds", True), ("required_checks", [{}]),
                                         ("prerequisites", {"q0": "true"}), ("resources", [False])])
def test_malformed_envelope_never_changes_saved_concept(cli, field, value):
    _, payload = cli.prepare()
    before = cli.status()
    payload.update(operation_id="invalid-revision", revision=2, expected_work_revision=before["revision"])
    payload["execution_envelope"][field] = value
    assert cli.request("present-concept", payload, code=2)["error"] == "invalid_request"
    assert cli.status()["revision"] == before["revision"]
    assert len(cli.status()["concepts"]) == 1


def test_version_and_status_identify_source_without_changing_work_or_qualifying_host(cli):
    cli.prepare()
    before = (cli.state / "aos.sqlite3").read_bytes()
    result = subprocess.run([sys.executable, "-m", "agentic_tools.aos.cli", "version"],
                            check=True, capture_output=True, text=True, timeout=10)
    receipt = json.loads(result.stdout)
    assert receipt == capabilities()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["source_digest"])
    assert receipt["coordinator_accepts_approval_answer"] is False and receipt["mcp_workflow_control"] is False
    assert "product qualification" in receipt["qualification"]
    assert cli.status()["operator"] == receipt
    assert (cli.state / "aos.sqlite3").read_bytes() == before


def test_no_cli_approval_or_worker_audience_can_grant_coordinator_powers(cli):
    result = subprocess.run([sys.executable, "-m", "agentic_tools.aos.cli", "admit", *cli.common,
                             "--operation-id", "forged", "--work-id", "CLI", "--expected-work-revision", "1",
                             "--digest", "forged", "--audience", "worker", "--approved"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr
    assert not cli.state.exists()


def test_declared_worker_writable_state_is_rejected(cli):
    request = {"operation_id": "intro", "work_id": "CLI", "title": "Fixture", "structure": ["fixture"], "team": TEAM}
    cli.state = cli.source / "state"
    cli.common = ["--state-dir", str(cli.state), "--worker-writable-root", str(cli.source)]
    assert cli.request("introduce-work", request, code=2)["error"] == "worker_writable_state"
    assert not cli.state.exists()
