"""Explicit loopback dashboard process; no workflow mutation or file server.

The authoritative ledger is opened read-only. A separate private process receipt
and lock support inspection and instance-bound shutdown; workers must be denied
the same private directory and loopback access by their existing runtime sandbox.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
from threading import Thread
import time
from urllib.parse import urlsplit
import uuid

from .coordinator import capabilities
from .projection import SnapshotCollector, dashboard
from .store import Rejected, Store, identifier, now


RECEIPT_NAME = "dashboard-service.json"
LOCK_NAME = "dashboard-service.lock"


def renderer() -> bytes:
    return files("agentic_tools.aos").joinpath("assets", "dashboard.html").read_bytes()


def work_views(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        alias, separator, work_id = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", alias) or alias in result:
            raise Rejected("invalid_dashboard_views", "Use unique kebab-case aliases: --work alias=WORK_ID")
        result[alias] = identifier(work_id)
    if not 1 <= len(result) <= 32:
        raise Rejected("invalid_dashboard_views", "Configure 1-32 explicit dashboard work views")
    return result


def _private_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise Rejected("unsafe_dashboard_receipt", "Dashboard process files must be private user-owned regular files")


def _write_receipt(directory: Path, value: dict) -> None:
    temporary = directory / (".dashboard-" + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / RECEIPT_NAME)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_receipt(store: Store) -> dict | None:
    try:
        fd = os.open(store.directory / RECEIPT_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        _private_file(stream.fileno())
        value = json.load(stream)
    if (not isinstance(value, dict) or value.get("format") != "aos-dashboard-service-v1"
            or not isinstance(value.get("instance_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["instance_id"])
            or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535
            or value.get("origin") != f"http://127.0.0.1:{value['port']}"
            or value.get("state") not in ("listening", "stopped")
            or (value["state"] == "listening" and (not isinstance(value.get("stop_token"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", value["stop_token"])) )):
        raise Rejected("invalid_dashboard_receipt", "Dashboard instance receipt is invalid; do not contact an arbitrary endpoint")
    return value


def _public(receipt: dict) -> dict:
    return {key: value for key, value in receipt.items() if key != "stop_token"}


class DashboardService:
    def __init__(self, store: Store, views: dict[str, str], port: int):
        if not store.read_only:
            raise Rejected("read_only_required", "Dashboard collection requires a read-only authoritative store")
        if type(port) is not int or not 0 <= port <= 65535:
            raise Rejected("invalid_port", "Dashboard port must be in range 0-65535; 0 selects a disposable port")
        # Apply the same strict mapping rules to direct callers as to the CLI.
        work_views([alias + "=" + work_id for alias, work_id in views.items()])
        self.store, self.views, self.port = store, views, port
        self.collectors = {alias: SnapshotCollector(store, work_id) for alias, work_id in views.items()}
        self.html = renderer()
        self.receipt = None
        self.server = None
        self._lock = None

    def __enter__(self):
        fd = os.open(self.store.directory / LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _private_file(fd)
            self._lock = os.fdopen(fd, "a+")
        except BaseException:
            os.close(fd)
            raise
        try:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Rejected("dashboard_owned", "A dashboard process already owns this state; inspect it before starting another") from error
            for collector in self.collectors.values():
                dashboard(collector.collect())  # Fail closed before opening a listener.
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler_for(self))
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    raise Rejected("dashboard_port_in_use", "Configured dashboard port is occupied; no existing process was changed") from error
                raise
            self.server.daemon_threads = True
            self.port = self.server.server_port
            self.receipt = {"format": "aos-dashboard-service-v1", "instance_id": uuid.uuid4().hex,
                            "state": "listening", "pid": os.getpid(), "started_at": now(), "port": self.port,
                            "origin": f"http://127.0.0.1:{self.port}", "views": self.views,
                            "version": capabilities(), "renderer_digest": "sha256:" + hashlib.sha256(self.html).hexdigest(),
                            "stop_token": secrets.token_urlsafe(32)}
            _write_receipt(self.store.directory, self.receipt)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        try:
            if self.server:
                self.server.server_close()
            if self.receipt:
                stopped = {**self.receipt, "state": "stopped", "stopped_at": now()}
                stopped.pop("stop_token", None)
                _write_receipt(self.store.directory, stopped)
        finally:
            if self._lock:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None

    def request_shutdown(self) -> None:
        # HTTP and signal callbacks cannot call shutdown on the serve thread.
        Thread(target=self.server.shutdown, daemon=True).start()

    def run(self) -> None:
        prior = signal.signal(signal.SIGTERM, lambda *_args: self.request_shutdown())
        try:
            print(json.dumps(_public(self.receipt)), flush=True)
            self.server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, prior)


def handler_for(service: DashboardService):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def response(self, status: HTTPStatus, value: dict | bytes, kind="application/json; charset=utf-8"):
            content = json.dumps(value).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

        def error(self, status, code):
            self.response(status, {"error": code})

        def host_valid(self):
            return self.headers.get_all("Host", []) == [f"127.0.0.1:{service.port}"]

        def read_origin_valid(self):
            origins = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            return (not origins or origins == [service.receipt["origin"]]) and (not sites or sites in (["same-origin"], ["none"]))

        def do_GET(self):
            if not self.host_valid() or not self.read_origin_valid():
                self.error(HTTPStatus.FORBIDDEN, "foreign_dashboard_origin")
                return
            location = urlsplit(self.path)
            if location.scheme or location.netloc or location.query or location.fragment:
                self.error(HTTPStatus.NOT_FOUND, "dashboard_resource_not_found")
                return
            try:
                if location.path == "/health.json":
                    self.response(HTTPStatus.OK, _public(service.receipt))
                    return
                if location.path == "/":
                    from html import escape
                    links = "".join(f'<li><a href="/work/{alias}/dashboard.html">{escape(work)}</a></li>' for alias, work in service.views.items())
                    self.response(HTTPStatus.OK, ("<!doctype html><meta charset=utf-8><title>AOS dashboards</title><h1>AOS dashboards</h1><ul>" + links + "</ul>").encode(), "text/html; charset=utf-8")
                    return
                routes = {f"/work/{alias}/{resource}": (alias, resource) for alias in service.views
                          for resource in ("dashboard.html", "workflow-state.json", "snapshot.json")}
                if location.path not in routes:
                    self.error(HTTPStatus.NOT_FOUND, "dashboard_resource_not_found")
                    return
                alias, resource = routes[location.path]
                if resource == "dashboard.html":
                    self.response(HTTPStatus.OK, service.html, "text/html; charset=utf-8")
                else:
                    snapshot = service.collectors[alias].collect()
                    self.response(HTTPStatus.OK, snapshot if resource == "snapshot.json" else dashboard(snapshot))
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                self.error(HTTPStatus.SERVICE_UNAVAILABLE, "dashboard_collection_unavailable")

        def do_POST(self):
            if self.path != "/control/stop":
                self.error(HTTPStatus.NOT_FOUND, "dashboard_resource_not_found")
                return
            supplied = self.headers.get("Authorization", "")
            if (not self.host_valid() or self.headers.get_all("Origin") or self.headers.get_all("Sec-Fetch-Site")
                    or self.headers.get_all("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0"
                    or not hmac.compare_digest(supplied, "Bearer " + service.receipt["stop_token"])):
                self.error(HTTPStatus.FORBIDDEN, "dashboard_stop_forbidden")
                return
            self.response(HTTPStatus.OK, {"instance_id": service.receipt["instance_id"], "state": "stopping"})
            service.request_shutdown()

        def do_HEAD(self):
            self.error(HTTPStatus.METHOD_NOT_ALLOWED, "dashboard_method_not_allowed")

        do_OPTIONS = do_HEAD
        do_PUT = do_HEAD
        do_DELETE = do_HEAD

        def log_message(self, *_args):
            pass  # Never log request paths, authorization or private source data.

    return Handler


def _request(receipt: dict, method: str, path: str) -> dict:
    connection = HTTPConnection("127.0.0.1", receipt["port"], timeout=3)
    try:
        headers = {"Authorization": "Bearer " + receipt["stop_token"]} if method == "POST" else {}
        connection.request(method, path, body=b"" if method == "POST" else None, headers=headers)
        response = connection.getresponse()
        payload = response.read(65537)
        if len(payload) > 65536:
            raise Rejected("dashboard_instance_mismatch", "Dashboard health response is not a bounded instance receipt")
        value = json.loads(payload)
        if response.status != 200 or not isinstance(value, dict) or value.get("instance_id") != receipt["instance_id"]:
            raise Rejected("dashboard_instance_mismatch", "Endpoint is not the recorded dashboard instance; no other process will be stopped")
        return value
    except HTTPException as error:
        raise Rejected("dashboard_service_unavailable", "Recorded dashboard endpoint did not return a valid HTTP response") from error
    finally:
        connection.close()


def inspect_service(store: Store) -> dict:
    receipt = _read_receipt(store)
    if receipt is None:
        return {"state": "absent"}
    if receipt["state"] == "stopped":
        return _public(receipt)
    try:
        health = _request(receipt, "GET", "/health.json")
    except (OSError, ValueError) as error:
        return {**_public(receipt), "state": "unavailable", "observation": type(error).__name__,
                "next_action": "Inspect the owned process; an unavailable listener does not prove process cleanup"}
    return {**_public(health), "state": "listening"}


def stop_service(store: Store, *, timeout: float = 5) -> dict:
    receipt = _read_receipt(store)
    if receipt is None or receipt["state"] == "stopped":
        return {"state": "absent"} if receipt is None else _public(receipt)
    _request(receipt, "GET", "/health.json")  # Never send credentials to a mismatched listener.
    _request(receipt, "POST", "/control/stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _read_receipt(store)
        if current and current["instance_id"] != receipt["instance_id"]:
            raise Rejected("dashboard_instance_changed", "Dashboard ownership changed while stopping; retain both observations")
        if current and current["state"] == "stopped":
            return _public(current)
        time.sleep(0.05)
    raise Rejected("dashboard_stop_unconfirmed", "Stop requested but private receipt is not reconciled; no process was killed")
