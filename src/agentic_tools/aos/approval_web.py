"""Supervised loopback HTTPS decision page backed by the existing AOS ledger.

The page is an interaction surface, not proof of human identity by itself. Assigned
workers must be denied its endpoint, browser control, and authoritative state.
"""

from __future__ import annotations

from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import hmac
import json
import os
import secrets
import socket
import ssl
import sqlite3
import stat
from threading import Event, Lock, Thread, current_thread
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit, urlencode
from pathlib import Path

from .store import DECISION_CONTEXT_LABELS, Rejected, Store, canonical, identifier



def document(content: str) -> bytes:
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AOS decision</title><style>
body{margin:0;padding:24px;background:#0f171f;color:#e0e7ee;font:18px/1.5 system-ui}
a,a:visited{color:#9bd5ff;text-underline-offset:3px}a:focus-visible{outline:2px solid #efbd64;outline-offset:4px}
main{max-width:850px;margin:auto}h1{color:#efbd64}section{background:#192530;border:1px solid #445a6c;border-radius:12px;padding:22px}
.note{border-left:4px solid #efbd64;padding:10px 14px;background:#283442}
button{font:inherit;border:0;border-radius:8px;padding:10px 18px;margin:8px 8px 0 0;cursor:pointer}
.approve{background:#81d4b5}.decline{background:#ffabab}.cancel{background:#c7d2dc}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}small{color:#a4b4c3}
li{margin:4px 0}details{margin:18px 0;border-top:1px solid #445a6c;padding-top:12px}
summary{cursor:pointer;color:#b9cbd9}h2{white-space:pre-wrap}
</style></head><body><main>''' + content + '</main></body></html>').encode()


class DecisionController:
    def __init__(self, store: Store, *, origin: str, qualification_reference: str,
                 work_id: str | None = None, digest: str | None = None,
                 status: Callable[[], dict] | None = None):
        if (work_id is None) != (digest is None):
            raise Rejected("decision_scope", "An owned decision page needs both exact work and concept identity")
        if work_id is not None:
            identifier(work_id)
            if (not isinstance(digest, str) or len(digest) != 71 or not digest.startswith('sha256:')
                    or any(char not in '0123456789abcdef' for char in digest[7:])):
                raise Rejected("decision_scope", "Decision scope requires an exact concept SHA-256")
        self.store = store
        self.origin = origin
        self.work_id, self.digest, self.status = work_id, digest, status
        self.capability = store.host_capability("Codex local HTTPS decision tab", qualification_reference)
        self.sessions: dict[str, tuple[str, str, int, str | None]] = {}
        self.lock = Lock()

    def _in_scope(self, row: dict) -> bool:
        return self.work_id is None or (row["work_id"] == self.work_id and row["digest"] == self.digest)

    def pending(self) -> list[dict]:
        return [row for row in self.store.pending_approvals() if row["current"] and self._in_scope(row)]

    def current(self, challenge_id: str | None = None) -> dict | None:
        current = [row for row in self.pending()
                   if challenge_id is None or row["challenge_id"] == challenge_id]
        if len(current) > 1:
            raise Rejected("multiple_current_decisions", "More than one current decision awaits an operator")
        return current[0] if current else None

    def recorded(self, challenge_id: str | None = None) -> dict | None:
        """Read and verify the exact durable answer without creating new readback writes."""
        if challenge_id is None and self.work_id is None:
            return None
        with self.store._connection() as db:
            db.execute("BEGIN")
            if challenge_id is not None:
                row = db.execute("SELECT * FROM decision WHERE challenge_id=?", (challenge_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM decision WHERE work_id=? AND digest=? ORDER BY created_at DESC LIMIT 1",
                                 (self.work_id, self.digest)).fetchone()
            if row is None or not self._in_scope(row):
                return None
            saved = dict(row)
        reader = Store(self.store.directory, self.store.worker_writable_roots, read_only=True)
        receipt = reader.verify_decision(saved["decision_id"])
        if any(receipt[key] != saved[key] for key in ("work_id", "digest", "challenge_id", "outcome")):
            raise Rejected("decision_readback_failed", "Saved answer changed during readback")
        record = reader.read(saved["work_id"])
        return {"decision": receipt, "record": record,
                "current": record["current_concept"] == receipt["concept_revision"]}

    def progress(self) -> str:
        """The optional display callback cannot record an answer or grant execution."""
        if self.status is None:
            return ''
        try:
            value = self.status()
            if not isinstance(value, dict):
                return ''
            pieces = []
            for key in ("stage", "detail"):
                if isinstance(value.get(key), str) and value[key].strip():
                    pieces.append(escape(value[key][:2048]))
            return '<p>' + ' — '.join(pieces) + '</p>' if pieces else ''
        except Exception:
            return '<p>Live progress is temporarily unavailable. The saved decision below remains authoritative.</p>'

    def issue_session(self, challenge_id: str) -> str:
        current = self.current(challenge_id)
        if current is None:
            raise Rejected("stale_session", "Only an exact current pending proposal may receive browser credentials")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = (challenge_id, current["digest"], current["work_revision"], None)
        return token

    def use_session(self, token: str, challenge_id: str, csrf: str, choice: str) -> str:
        """Bind credentials to one exact proposal for this server lifetime.

        Origin, same-origin fetch metadata, unpredictable cookie/CSRF equality and
        worker isolation protect the browser route. Arbitrary elapsed time does
        not invalidate an unchanged proposal. Answered/revised/withdrawn work
        invalidates unused tokens; same-answer retries only read the durable result.
        """
        with self.lock:
            session = self.sessions.get(token)
            if not session or session[0] != challenge_id or not hmac.compare_digest(token, csrf):
                return "invalid"
            if session[3] is not None:
                return "retry" if session[3] == choice else "conflict"
            current = self.current(challenge_id)
            if current is None or current["digest"] != session[1] or current["work_revision"] != session[2]:
                return "stale"
            self.sessions[token] = (*session[:3], choice)
            return "new"


def handler_for(controller: DecisionController) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(2)

        def response(self, status: HTTPStatus, body: bytes, *, cookie: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def reject(self, status: HTTPStatus) -> None:
            self.response(status, document("<h1>Decision unavailable</h1><p>This submission made no new decision. Saved decisions remain recorded. Return to the AOS dashboard for the current status.</p>"))

        def recover_session(self, challenge_id: str) -> None:
            current = controller.current(challenge_id)
            route = '/?' + urlencode({'challenge': challenge_id}) if current or controller.recorded(challenge_id) else '/'
            self.response(HTTPStatus.FORBIDDEN, document(
                '<h1>Browser session unavailable</h1>'
                '<p>This tab no longer has valid session credentials. This can happen after the decision service restarts.</p>'
                '<p>This submission made no new decision. Any previously saved answer remains recorded.</p>'
                '<p><a href="' + escape(route) + '">Reopen the proposal and check its status</a></p>'
                '<p>Opening that link only reads the current status and refreshes the browser session. '
                'If the proposal still needs an answer, review it and choose an answer explicitly. '
                'A changed proposal will show its current scope.</p>'))

        def valid_host(self) -> bool:
            return self.headers.get("Host") == controller.origin.removeprefix("https://")

        def do_GET(self) -> None:
            try:
                self.get_page()
            except (OSError, sqlite3.Error, Rejected, ValueError):
                self.response(HTTPStatus.SERVICE_UNAVAILABLE, document(
                    '<h1>Decision storage unavailable</h1><p>Work is held for coordinator reconciliation. Saved decisions remain recorded.</p>'))

        def get_page(self) -> None:
            location = urlsplit(self.path)
            query = parse_qs(location.query, keep_blank_values=True)
            if (location.path != "/" or not self.valid_host() or location.fragment
                    or (query and (set(query) != {"challenge"} or len(query["challenge"]) != 1
                                   or not query["challenge"][0]))):
                self.reject(HTTPStatus.FORBIDDEN)
                return
            try:
                current = controller.current(query.get("challenge", [None])[0])
            except Rejected as error:
                if error.code == "multiple_current_decisions":
                    choices = "".join('<li><a href="/?' + escape(urlencode({"challenge": row["challenge_id"]}))
                                      + '">' + escape(row["package"]["body"]) + '</a></li>'
                                      for row in controller.pending())
                    self.response(HTTPStatus.OK, document('<h1>Pending decisions</h1><p>Choose the exact proposal to review. '
                        'Closing the tab supplies no answer.</p><ul>' + choices + '</ul>'))
                else:
                    self.reject(HTTPStatus.CONFLICT)
                return
            except (OSError, sqlite3.Error):
                self.response(HTTPStatus.SERVICE_UNAVAILABLE, document(
                    '<h1>Decision storage unavailable</h1><p>Work is held. The coordinator must restore recording access.</p>'))
                return
            if current is None:
                recorded = controller.recorded(query.get("challenge", [None])[0])
                if recorded is not None:
                    decision, record = recorded["decision"], recorded["record"]
                    effective = decision['outcome'] in {'approved', 'declined', 'cancelled'}
                    title = 'Decision recorded' if effective else 'Decision not applied'
                    message = ('This proposal is already answered. The exact answer is saved and verified. No further answer is needed.'
                               if effective else
                               'This interaction is recorded, but it did not supply an effective decision. '
                               'Work remains held for coordinator attention. No answer has been inferred.')
                    execution = ('<p>Execution: <strong>' + escape(record['execution']) + '</strong></p>'
                                 if recorded['current'] else
                                 '<p>This answer belongs to an earlier proposal. It does not approve the current proposal.</p>')
                    self.response(HTTPStatus.OK, document(
                        '<h1>' + title + '</h1><section><p>Outcome: <strong>' + escape(decision['outcome'])
                        + '</strong></p><p>Work: ' + escape(decision['work_id']) + '</p>' + execution
                        + (controller.progress() if effective and recorded['current'] else '')
                        + '<p>' + message + '</p>'
                        + '<details><summary>Exact record</summary><p>Decision ID: ' + escape(decision['decision_id'])
                        + '</p><p>Digest: ' + escape(decision['digest']) + '</p></details></section>'
                        + '<p><a href="/?' + escape(urlencode({'challenge': decision['challenge_id']}))
                        + '">Refresh this proposal status</a></p>'))
                    return
                # A scoped listener never exposes another work, even for an unrelated URL.
                states = controller.store.decision_states() if controller.work_id is None else []
                summaries = "".join(
                    "<section><p>Work: " + escape(row["work_id"]) + "</p><p>Decision: <strong>"
                    + escape(row["concept_decision"]) + "</strong></p><p>Execution: <strong>"
                    + escape(row["execution"]) + "</strong></p></section>" for row in states
                    if row["concept_decision"] != "pending")
                message = ("This request changed, was withdrawn or already answered. Read the recorded status below."
                           if query else "No current approval form is selected. Stale or withdrawn requests cannot approve work.")
                self.response(HTTPStatus.OK, document("<h1>No active decision form</h1><p>" + message + "</p>"
                    + summaries + '<p>Saved decisions remain recorded while execution is held by the remaining gates.</p>'
                    + '<p><a href="/">Current proposals and status</a></p>'))
                return
            package = current["package"]
            token = controller.issue_session(current["challenge_id"])
            acceptance = "".join(f"<li>{escape(str(item))}</li>" for item in package["acceptance"])
            team = "".join(
                "<li>" + escape(str(member["name"])) + " — " + escape(str(member["role"]))
                + ". " + escape(str(member["scope"])) + "</li>"
                for member in package["team_envelope"]
            )
            context = package.get("decision_context", {})
            brief = "".join(f"<p><strong>{label}:</strong> {escape(context[key])}</p>"
                            for key, label in DECISION_CONTEXT_LABELS.items() if key in context)
            from .planning import approval_summary_html
            plan_summary = (approval_summary_html(package['di_plan'], package['execution_envelope'])
                            if 'di_plan' in package and 'execution_envelope' in package else '')
            body = f"""<h1>Needs your decision</h1><p class="note">Review this proposal before choosing. Closing this tab leaves work held.</p>
<section><small>Proposal revision {package['revision']}</small>
<h2>{escape(package['body'])}</h2>
<p><strong>Recommendation:</strong> {escape(package.get('recommendation', 'Approve only if the complete package matches your intent.'))}</p>
<p><strong>Alternative:</strong> {escape(package.get('alternative', 'Decline or cancel; work stays held.'))}</p>
<p><strong>Consequence:</strong> {escape(package.get('consequence', 'Approval records this exact concept; implementation dispatch remains gated.'))}</p>
{brief}
{plan_summary}
<h3>What this approval covers</h3><ul>{acceptance}</ul>
<p><strong>Authority:</strong> {escape(package['authority'])}</p>
<h3>Team and limits</h3><ul>{team}</ul>
<details><summary>Technical details and exact record</summary>
<p><small>Work: {escape(current['work_id'])} · package: {escape(package['package_id'])}<br>
SHA-256: {escape(current['digest'])}</small></p>
<pre>{escape(json.dumps(package, indent=2, ensure_ascii=False))}</pre></details>
<form method="post" action="/decide"><input type="hidden" name="challenge" value="{escape(current['challenge_id'])}">
<input type="hidden" name="csrf" value="{escape(token)}">
<button class="approve" name="choice" value="approve">Approve this proposal</button>
<button class="decline" name="choice" value="decline">Decline</button>
<button class="cancel" name="choice" value="cancel">Cancel</button></form></section>"""
            self.response(HTTPStatus.OK, document(body), cookie=f"aos_decision={token}; HttpOnly; Secure; SameSite=Strict; Path=/")

        def do_POST(self) -> None:
            # Codex's in-app browser sends Origin:null for its same-origin form.
            # Fetch metadata, the server-lifetime cookie, and answer-bound CSRF remain mandatory.
            if (self.path != "/decide" or not self.valid_host()
                    or self.headers.get("Origin") not in {controller.origin, "null"}
                    or self.headers.get("Sec-Fetch-Site") != "same-origin"
                    or not self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded")):
                self.reject(HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.reject(HTTPStatus.BAD_REQUEST)
                return
            if not 1 <= length <= 4096:
                self.reject(HTTPStatus.BAD_REQUEST)
                return
            try:
                form = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie.get("aos_decision")
                values = {key: items[0] for key, items in form.items() if len(items) == 1}
                if (set(values) != {"challenge", "csrf", "choice"} or values["choice"] not in {"approve", "decline", "cancel"}):
                    self.reject(HTTPStatus.FORBIDDEN)
                    return
                if not token:
                    self.recover_session(values['challenge'])
                    return
                session_use = controller.use_session(token.value, values["challenge"], values["csrf"], values["choice"])
                if session_use == "invalid":
                    self.recover_session(values['challenge'])
                    return
                if session_use == "stale":
                    self.response(HTTPStatus.CONFLICT, document('<h1>Decision held</h1>'
                        '<p>This request changed, was withdrawn or already answered. Saved decisions remain recorded.</p>'
                        '<p><a href="/?' + escape(urlencode({'challenge': values['challenge']}))
                        + '">Read this proposal status</a></p>'))
                    return
                if session_use == "conflict":
                    self.reject(HTTPStatus.CONFLICT)
                    return
                action, approved = {"approve": ("accept", True), "decline": ("decline", None), "cancel": ("cancel", None)}[values["choice"]]
                result = controller.store.finish_approval(values["challenge"], action, approved, capability=controller.capability,
                    exact_answer={"approve": "Approve this exact package", "decline": "Decline", "cancel": "Cancel"}[values["choice"]],
                    source_channel="Codex local HTTPS decision tab", interaction_reference=values["challenge"])
            except Rejected as error:
                if error.code in {"approval_recording_failed", "decision_readback_failed"}:
                    self.response(HTTPStatus.SERVICE_UNAVAILABLE, document(
                        "<h1>Approval received; recording needs reconciliation</h1><p>" + escape(str(error)) +
                        "</p><p>Execution is held. The coordinator must reconcile this same interaction; do not submit another approval request.</p>"))
                else:
                    self.response(HTTPStatus.CONFLICT, document('<h1>Decision held</h1><p>' + escape(str(error))
                        + '</p><p>Saved decisions remain recorded. Read the current proposal status before proceeding.</p>'))
                return
            except (OSError, sqlite3.Error):
                self.response(HTTPStatus.SERVICE_UNAVAILABLE, document(
                    '<h1>Recording unavailable</h1><p>Execution is held for coordinator reconciliation of this same interaction.</p>'))
                return
            except (UnicodeDecodeError, ValueError):
                self.reject(HTTPStatus.CONFLICT)
                return
            effective = result['outcome'] in {'approved', 'declined', 'cancelled'}
            title = 'Decision recorded' if effective else 'Decision not applied'
            message = ('This exact answer is saved. Execution follows the approved limits and current controls.'
                       if effective else 'This interaction was recorded, but no effective decision was applied. '
                       'Work remains held for coordinator attention.')
            body = document(f"<h1>{title}</h1><section><p>Outcome: <strong>{escape(result['outcome'])}</strong></p>"
                            f"<p>Work: {escape(result['work_id'])} · revision: {result['concept_revision']}</p>"
                            f"<p>Digest: {escape(result['digest'])}</p><p>Decision ID: {escape(result['decision_id'])}</p>"
                            f"<p>{message}</p>"
                            + (controller.progress() if effective else '') + '</section>'
                            '<p><a href="/?' + escape(urlencode({'challenge': values['challenge']}))
                            + '">Check this proposal status</a></p><p><a href="/">Back to decisions</a></p>')
            self.response(HTTPStatus.OK, body, cookie="aos_decision=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/")

        def log_message(self, format: str, *args: object) -> None:
            # Never log the browser's form body, cookies, or challenge information.
            pass

    return Handler


class _DecisionHTTPServer(ThreadingHTTPServer):
    """Track only this listener's sockets and handlers, including incomplete TLS clients."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler, context):
        self.context = context
        self.owned_lock = Lock()
        self.connections: set[socket.socket] = set()
        self.handlers: set[Thread] = set()
        super().__init__(address, handler)
        self.socket.settimeout(.2)
        self.timeout = .1

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        try:
            # Handshake belongs to the bounded request thread, not the accept loop.
            request = self.context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
        except BaseException:
            connection.close()
            raise
        with self.owned_lock:
            self.connections.add(request)
        return request, address

    def process_request(self, request, client_address):
        thread = Thread(target=self.process_request_thread, args=(request, client_address),
                        name='aos-decision-request', daemon=True)
        with self.owned_lock:
            self.handlers = {prior for prior in self.handlers if prior.is_alive()}
            self.handlers.add(thread)
        try:
            thread.start()
        except BaseException:
            with self.owned_lock:
                self.handlers.discard(thread)
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self.owned_lock:
                self.connections.discard(request)

    def close_requests(self, deadline: float) -> bool:
        with self.owned_lock:
            connections = tuple(self.connections)
            handlers = tuple(self.handlers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The exact accepted socket may already have closed.
        for thread in handlers:
            thread.join(max(0, deadline - time.monotonic()))
        with self.owned_lock:
            return not self.connections and not any(thread.is_alive() for thread in self.handlers)


class DecisionService:
    """One owned HTTPS listener; authoritative progress remains in Store/Lifecycle.

    Enter binds and starts the listener before a supervisor presents a challenge.
    Closing interrupts only this service's accepted sockets and verifies its own
    listener and handlers have stopped. No service is installed or adopted.
    """
    def __init__(self, store: Store, *, certificate: str, key: str, port: int,
                 qualification_reference: str, work_id: str | None = None,
                 digest: str | None = None, status: Callable[[], dict] | None = None):
        if type(port) is not int or not 1 <= port <= 65535:
            raise Rejected("invalid_port", "HTTPS port must be in range 1-65535")
        self.store, self.certificate, self.key, self.port = store, certificate, key, port
        self.origin = f"https://127.0.0.1:{port}"
        self.url = self.origin + '/'
        self.controller = DecisionController(store, origin=self.origin,
            qualification_reference=qualification_reference, work_id=work_id, digest=digest, status=status)
        self.server = None
        self.thread = None
        self.started = Event()
        self.stop_requested = Event()
        self.failure = None
        self.closed = False
        self.cleanup = {"listener_stopped": False, "requests_stopped": False}

    def __enter__(self):
        if self.server is not None or self.closed:
            raise Rejected("decision_service_state", "A decision service instance cannot be started twice")
        key_info = Path(self.key).lstat()
        if (not stat.S_ISREG(key_info.st_mode) or key_info.st_uid != os.getuid()
                or stat.S_IMODE(key_info.st_mode) & 0o077):
            raise Rejected("unsafe_tls_key", "TLS key must be a private user-owned regular file")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(self.certificate, self.key, password=lambda: '')
        except (OSError, ssl.SSLError) as error:
            raise Rejected("tls_unavailable", "Local HTTPS certificate or key could not be loaded; decisions remain held") from error
        try:
            self.server = _DecisionHTTPServer(("127.0.0.1", self.port), self.request_handler(), context)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise Rejected("decision_port_in_use", "The decision port is occupied; the existing listener was not changed") from error
            raise
        self.thread = Thread(target=self._serve, name='aos-decision-listener', daemon=True)
        try:
            self.thread.start()
            if not self.started.wait(2):
                raise Rejected("decision_service_unavailable", "Decision listener did not start")
            self.check()
        except BaseException:
            self.close()
            raise
        return self

    def request_handler(self):
        """Trusted subclasses may reuse the owned TLS listener for another surface."""
        return handler_for(self.controller)

    def _serve(self):
        self.started.set()
        try:
            while not self.stop_requested.is_set():
                self.server.handle_request()
        except BaseException as error:
            if not self.stop_requested.is_set():
                self.failure = error

    def check(self) -> None:
        if self.closed or self.failure is not None or self.thread is None or not self.thread.is_alive():
            raise Rejected("decision_service_unavailable", "Decision listener is not running; saved answers remain recorded")

    def close(self) -> dict:
        if self.closed:
            return dict(self.cleanup)
        if self.thread is current_thread():
            raise Rejected("decision_service_cleanup", "The listener cannot join itself")
        deadline = time.monotonic() + 6
        self.stop_requested.set()
        if self.server is not None:
            # Closing the listener is nonblocking; BaseServer.shutdown() would
            # wait forever if its serve_forever loop failed before starting.
            self.server.server_close()
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(max(0, deadline - time.monotonic()))
            requests_stopped = self.server.close_requests(deadline)
        else:
            requests_stopped = True
        self.cleanup = {"listener_stopped": self.thread is None or not self.thread.is_alive(),
                        "requests_stopped": requests_stopped}
        self.closed = all(self.cleanup.values())
        if not self.closed:
            raise Rejected("decision_service_cleanup", "Decision listener or an owned request has not stopped; cleanup is unverified")
        return dict(self.cleanup)

    def __exit__(self, *_args):
        self.close()

    def wait(self) -> None:
        while True:
            self.check()
            self.thread.join(.25)


def serve_https(store: Store, *, certificate: str, key: str, port: int, qualification_reference: str) -> None:
    with DecisionService(store, certificate=certificate, key=key, port=port,
                         qualification_reference=qualification_reference) as service:
        print(service.url, flush=True)
        service.wait()
