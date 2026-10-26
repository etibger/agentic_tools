"""Owned disposable loopback servers and fresh-wheel dashboard bring-up."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import subprocess
import sys
from threading import Thread
from zipfile import ZipFile

import pytest

from agentic_tools.aos.dashboard_service import RECEIPT_NAME, renderer
from agentic_tools.aos.projection import collect
from agentic_tools.aos.store import Store
from test_aos_lifecycle import setup


@pytest.fixture
def source(tmp_path):
    store, _, _ = setup(tmp_path, admitted=False)
    return store


def common(source):
    return ["--state-dir", str(source.directory), "--worker-writable-root", str(source.directory.parent / "fixture")]


def command(source, name, *args, code=0, env=None, cwd=None):
    result = subprocess.run([sys.executable, "-m", "agentic_tools.aos.cli", name, *common(source), *map(str, args)],
                            capture_output=True, text=True, timeout=15, env=env, cwd=cwd)
    assert result.returncode == code, (result.args, result.stdout, result.stderr)
    return json.loads(result.stderr if code == 2 else result.stdout)


@contextmanager
def running(source, *, env=None, cwd=None, port=0, alias="fixture"):
    process = subprocess.Popen([sys.executable, "-m", "agentic_tools.aos.cli", "dashboard-serve", *common(source),
                                "--work", alias + "=Q1", "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "Dashboard did not start"
        line = process.stdout.readline()
        assert line, process.communicate(timeout=5)
        receipt = json.loads(line)
        assert receipt["state"] == "listening" and "stop_token" not in receipt
        yield process, receipt
    finally:
        if process.poll() is None:
            process.terminate()  # Exact owned disposable process; SIGTERM reconciles its receipt.
        process.communicate(timeout=10)


def request(receipt, path, *, method="GET", headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", receipt["port"], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_service_reads_exact_source_renderer_and_strict_resources_without_ledger_writes(source):
    before = source.path.read_bytes()
    with running(source) as (_, receipt):
        assert request(receipt, "/")[0] == 200
        status, headers, html = request(receipt, "/work/fixture/dashboard.html")
        assert status == 200 and html == renderer()
        assert b"fetch('workflow-state.json'" in html
        assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        snapshot = json.loads(request(receipt, "/work/fixture/snapshot.json")[2])
        view = json.loads(request(receipt, "/work/fixture/workflow-state.json")[2])
        native = collect(Store(source.directory, source.worker_writable_roots, read_only=True), "Q1")
        assert snapshot["source_digest"] == native["source_digest"] == view["source_digest"]
        assert snapshot["source_revision"] == view["source_revision"]
        assert view["stage_status"] == "Concept approved · Execution held"
        secret = json.loads((source.directory / RECEIPT_NAME).read_text())["stop_token"]
        health = request(receipt, "/health.json")[2]
        assert secret.encode() not in health and str(source.directory).encode() not in health
        assert json.loads(health)["renderer_digest"].startswith("sha256:")
        for path in ("/aos.sqlite3", "/dashboard-service.json", "/evidence/private", "/work/unknown/snapshot.json",
                     "/work/fixture/../../aos.sqlite3", "/work/fixture/%2e%2e/aos.sqlite3",
                     "/work/fixture/snapshot.json?work=OTHER", "/work/fixture/", "/favicon.ico"):
            status, _, body = request(receipt, path)
            assert status == 404 and secret.encode() not in body and str(source.directory).encode() not in body
        assert request(receipt, "/work/fixture/snapshot.json", method="POST")[0] == 404
        assert request(receipt, "/", method="OPTIONS")[0] == 405
    assert source.path.read_bytes() == before


@pytest.mark.parametrize("headers", [{"Host": "foreign.example"}, {"Host": "127.0.0.1"},
    {"Origin": "https://foreign.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}])
def test_loopback_reads_reject_foreign_host_origin_and_fetch_site(source, headers):
    with running(source) as (_, receipt):
        status, response_headers, _ = request(receipt, "/health.json", headers=headers)
        assert status == 403 and "Access-Control-Allow-Origin" not in response_headers
        assert request(receipt, "/health.json", headers={"Origin": receipt["origin"], "Sec-Fetch-Site": "same-origin"})[0] == 200


def test_shutdown_requires_private_instance_credential_and_is_not_a_browser_action(source):
    with running(source) as (process, receipt):
        path = source.directory / RECEIPT_NAME
        saved = json.loads(path.read_text())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        auth = {"Authorization": "Bearer " + saved["stop_token"]}
        assert request(receipt, "/control/stop", method="POST", body=b"")[0] == 403
        assert request(receipt, "/control/stop", method="POST", body=b"", headers={"Authorization": "Bearer wrong"})[0] == 403
        assert request(receipt, "/control/stop", method="POST", body=b"", headers={**auth, "Origin": receipt["origin"]})[0] == 403
        assert request(receipt, "/control/stop", method="POST", body=b"", headers={**auth, "Sec-Fetch-Site": "same-origin"})[0] == 403
        assert process.poll() is None
        inspected = command(source, "dashboard-inspect")
        assert inspected["instance_id"] == receipt["instance_id"] and inspected["state"] == "listening"
        assert "stop_token" not in inspected
        stopped = command(source, "dashboard-stop")
        assert stopped["state"] == "stopped" and stopped["instance_id"] == receipt["instance_id"]
        process.wait(timeout=5)
        assert process.returncode == 0
        assert command(source, "dashboard-stop") == stopped
        assert command(source, "dashboard-inspect") == stopped
        assert "stop_token" not in json.loads(path.read_text())


def test_duplicate_start_does_not_replace_owner_or_touch_existing_listener(source):
    with running(source) as (process, receipt):
        before = (source.directory / RECEIPT_NAME).read_bytes()
        error = command(source, "dashboard-serve", "--work", "other=Q1", "--port", "0", code=2)
        assert error["error"] == "dashboard_owned"
        assert (source.directory / RECEIPT_NAME).read_bytes() == before
        assert process.poll() is None and request(receipt, "/health.json")[0] == 200


def test_occupied_port_fails_clearly_and_releases_service_lock(source):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        error = command(source, "dashboard-serve", "--work", "fixture=Q1", "--port", port, code=2)
        assert error["error"] == "dashboard_port_in_use"
        assert not (source.directory / RECEIPT_NAME).exists()
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    with running(source) as (_, receipt):
        assert receipt["state"] == "listening"


@pytest.mark.parametrize("views", [["fixture=UNKNOWN"], ["bad/alias=Q1"], ["duplicate=Q1", "duplicate=Q1"]])
def test_only_explicit_valid_existing_work_views_can_start(source, views):
    args = [part for view in views for part in ("--work", view)]
    result = command(source, "dashboard-serve", *args, "--port", "0", code=2)
    assert result["error"] in ("work_not_found", "invalid_dashboard_views")
    assert not (source.directory / RECEIPT_NAME).exists()


def test_stale_process_receipt_restart_preserves_ledger_and_changes_instance(source):
    assert command(source, "dashboard-inspect")["state"] == "absent"
    assert command(source, "dashboard-stop")["state"] == "absent"
    before = source.path.read_bytes()
    with running(source) as (process, receipt):
        old_token = json.loads((source.directory / RECEIPT_NAME).read_text())["stop_token"]
        process.kill()  # Simulated abrupt failure of this exact disposable process.
        process.wait(timeout=5)
        assert command(source, "dashboard-inspect", code=3)["state"] == "unavailable"
    with running(source) as (_, next_receipt):
        assert next_receipt["instance_id"] != receipt["instance_id"]
        assert request(next_receipt, "/control/stop", method="POST", body=b"",
                       headers={"Authorization": "Bearer " + old_token})[0] == 403
    assert source.path.read_bytes() == before


def test_private_receipt_permissions_and_symlinks_fail_closed(source):
    with running(source) as (_, receipt):
        path = source.directory / RECEIPT_NAME
        path.chmod(0o644)
        try:
            assert command(source, "dashboard-inspect", code=2)["error"] == "unsafe_dashboard_receipt"
        finally:
            path.chmod(0o600)
        assert request(receipt, "/health.json")[0] == 200
    path.unlink()
    target = source.directory / "private-evidence.json"
    target.write_text('{"private":"must not be opened"}')
    path.symlink_to(target.name)
    assert command(source, "dashboard-inspect", code=2)["error"] == "state_unavailable"
    assert target.read_text() == '{"private":"must not be opened"}'


def test_stop_never_sends_credential_or_mutation_to_an_unrelated_listener(source):
    observed = []

    class Unrelated(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(("GET", self.headers.get("Authorization")))
            payload = b'{"instance_id":"unrelated"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            observed.append(("POST", self.headers.get("Authorization")))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Unrelated)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receipt = {"format": "aos-dashboard-service-v1", "instance_id": "a" * 32, "state": "listening",
               "port": server.server_port, "origin": f"http://127.0.0.1:{server.server_port}", "stop_token": "s" * 43}
    path = source.directory / RECEIPT_NAME
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    try:
        assert command(source, "dashboard-stop", code=2)["error"] == "dashboard_instance_mismatch"
        assert observed == [("GET", None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fresh_source_export_and_wheel_serve_without_checkout_or_private_helpers(source, tmp_path):
    repository = Path(__file__).resolve().parents[1]
    exported = tmp_path / "exported"
    exported.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(repository / name, exported / name)
    for name in ("src", "templates"):
        shutil.copytree(repository / name, exported / name, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    assert (exported / "templates/dashboard.html").is_symlink()
    assert (exported / "templates/dashboard.html").read_bytes() == renderer()
    assert not (exported / "private").exists()
    build = subprocess.run(["uv", "build", "--wheel", "--offline"], cwd=exported,
                           capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stdout + build.stderr
    wheel = next((exported / "dist").glob("*.whl"))
    unpacked = tmp_path / "unpacked-wheel"
    with ZipFile(wheel) as archive:
        assert archive.read("agentic_tools/aos/assets/dashboard.html") == renderer()
        assert not any("private/" in name or "templates/" in name for name in archive.namelist())
        archive.extractall(unpacked)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    environment = {**os.environ, "PYTHONPATH": str(unpacked)}
    module_path = subprocess.run([sys.executable, "-c", "import agentic_tools.aos; print(agentic_tools.aos.__file__)"],
                                 cwd=elsewhere, env=environment, check=True, capture_output=True, text=True, timeout=10)
    assert Path(module_path.stdout.strip()).is_relative_to(unpacked)
    before = source.path.read_bytes()
    with running(source, env=environment, cwd=elsewhere) as (_, receipt):
        assert request(receipt, "/work/fixture/dashboard.html")[2] == renderer()
        assert json.loads(request(receipt, "/work/fixture/workflow-state.json")[2])["current_stage"] == "Q1"
        assert command(source, "dashboard-stop", env=environment, cwd=elsewhere)["state"] == "stopped"
    assert source.path.read_bytes() == before
