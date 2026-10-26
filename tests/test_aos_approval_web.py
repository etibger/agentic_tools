"""Bounded local HTTP tests for the HTTPS handler; TLS is checked in host integration."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import sqlite3
from threading import Thread
import time
from urllib.parse import urlencode

from agentic_tools.aos.approval_web import DecisionController, handler_for
from agentic_tools.aos.store import Store
from test_aos_store import TEAM


@contextmanager
def running_page(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    origin = f"https://127.0.0.1:{port}"
    controller = DecisionController(store, origin=origin, qualification_reference="SIMULATED:web-test")
    server.RequestHandlerClass = handler_for(controller)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fixture(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "worker",))
    store.introduce("intro", "WEB-TEST", "Synthetic web decision", ["fixture/"], TEAM)
    current = store.present("concept", "WEB-TEST", 1, "WEB-DI", 1, "Exact test package", ["No real work"], TEAM,
                            "Test only", recommendation="Approve the test package", alternative="Decline the test package",
                            consequence="No implementation dispatch")
    capability = store.host_capability("preparer", "SIMULATED:prepare")
    store.begin_approval("challenge", "WEB-TEST", 2, current["digest"], capability=capability)
    return store, current


def get_form(port):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", "/")
    response = conn.getresponse()
    body = response.read().decode()
    assert response.status == 200
    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
    challenge = re.search(r'name="challenge" value="([^"]+)"', body).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
    return conn, body, cookie, challenge, csrf


def post(conn, cookie, challenge, csrf, choice, origin, *, site="same-origin"):
    data = urlencode({"challenge": challenge, "csrf": csrf, "choice": choice})
    conn.request("POST", "/decide", data, {"Content-Type": "application/x-www-form-urlencoded",
                 "Cookie": cookie, "Origin": origin, "Sec-Fetch-Site": site})
    response = conn.getresponse()
    response.read()
    return response.status


def test_exact_page_negative_paths_and_durable_approval(tmp_path):
    store, current = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, body, cookie, challenge, csrf = get_form(port)
        assert current["digest"] in body
        assert "Approve the test package" in body
        assert "Decline the test package" in body
        assert "No implementation dispatch" in body
        assert "What this approval covers" in body
        assert "No real work" in body
        assert "Team and limits" in body
        assert "Technical details and exact record" in body
        assert current["digest"] not in body.split("<details>", 1)[0]
        assert not store.read("WEB-TEST")["decisions"]
        assert post(conn, cookie, challenge, csrf, "approve", "https://foreign.example", site="cross-site") == 403
        assert post(conn, cookie, challenge, csrf, "approve", origin, site="none") == 403
        assert post(conn, cookie, challenge, "wrong", "approve", "null") == 403
        assert post(conn, cookie, challenge, csrf, "approve", "null") == 200
        assert post(conn, cookie, challenge, csrf, "approve", "null") == 200
        assert post(conn, cookie, challenge, csrf, "decline", "null") == 409
        conn.close()
    reopened = Store(store.directory, (tmp_path / "worker",), read_only=True)
    record = reopened.read("WEB-TEST")
    assert record["state"] == "ready"
    assert record["dispatch_available"] is False
    assert len(record["decisions"]) == 1
    assert record["decisions"][0]["digest"] == current["digest"]


def test_interrupted_page_session_can_reopen_without_approving(tmp_path):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, _):
        conn, _, _, _, _ = get_form(port)
        conn.close()
    assert not store.read("WEB-TEST")["decisions"]
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        assert post(conn, cookie, challenge, csrf, "cancel", origin) == 200
        conn.close()
    assert store.read("WEB-TEST")["decisions"][0]["outcome"] == "cancelled"


def test_old_unchanged_challenge_remains_answerable(tmp_path):
    store, _ = fixture(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE challenge SET created_at=?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    capability = store.host_capability("test", "SIMULATED:expired")
    challenge = store.pending_approvals()[0]
    with running_page(store) as (port, _):
        conn = HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        assert "Needs your decision" in response.read().decode()
        conn.close()
    result = store.finish_approval(challenge["challenge_id"], "accept", True, capability=capability)
    assert result["outcome"] == "approved"
    assert store.read("WEB-TEST")["state"] == "ready"


def test_lost_http_reply_is_recovered_from_fresh_store(tmp_path):
    store, _ = fixture(tmp_path)
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        data = urlencode({"challenge": challenge, "csrf": csrf, "choice": "cancel"})
        conn.request("POST", "/decide", data, {"Content-Type": "application/x-www-form-urlencoded",
                     "Cookie": cookie, "Origin": origin, "Sec-Fetch-Site": "same-origin"})
        conn.close()  # The client never reads the response.
        for _ in range(40):
            if store.read("WEB-TEST")["decisions"]:
                break
            time.sleep(0.025)
    reopened = Store(store.directory, (tmp_path / "worker",), read_only=True)
    assert reopened.read("WEB-TEST")["decisions"][0]["outcome"] == "cancelled"


def test_recording_failure_page_distinguishes_received_answer_from_recorded(tmp_path):
    store, _ = fixture(tmp_path)
    def fail(stage):
        if stage == 'before_decision_commit':
            raise sqlite3.OperationalError('injected persistence failure')
    store._fault = fail
    with running_page(store) as (port, origin):
        conn, _, cookie, challenge, csrf = get_form(port)
        data = urlencode({'challenge': challenge, 'csrf': csrf, 'choice': 'approve'})
        conn.request('POST', '/decide', data, {'Content-Type': 'application/x-www-form-urlencoded',
                     'Cookie': cookie, 'Origin': origin, 'Sec-Fetch-Site': 'same-origin'})
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 503
        assert 'Approval received, recording failed' in body
        assert 'Decision recorded' not in body
        assert 'do not submit another approval request' in body
        conn.close()
    assert not store.read('WEB-TEST')['decisions']
