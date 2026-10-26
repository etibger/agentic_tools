"""Supervised loopback HTTPS decision page backed by the existing AOS ledger.

The page is an interaction surface, not proof of human identity by itself. Assigned
workers must be denied its endpoint, browser control, and authoritative state.
"""

from __future__ import annotations

from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import secrets
import ssl
import sqlite3
import stat
from threading import Lock
from urllib.parse import parse_qs, urlsplit, urlencode
from pathlib import Path

from .store import Rejected, Store, canonical



def document(content: str) -> bytes:
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AOS decision</title><style>
body{margin:0;padding:24px;background:#0f171f;color:#e0e7ee;font:18px/1.5 system-ui}
main{max-width:850px;margin:auto}h1{color:#efbd64}section{background:#192530;border:1px solid #445a6c;border-radius:12px;padding:22px}
.note{border-left:4px solid #efbd64;padding:10px 14px;background:#283442}
button{font:inherit;border:0;border-radius:8px;padding:10px 18px;margin:8px 8px 0 0;cursor:pointer}
.approve{background:#81d4b5}.decline{background:#ffabab}.cancel{background:#c7d2dc}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}small{color:#a4b4c3}
li{margin:4px 0}details{margin:18px 0;border-top:1px solid #445a6c;padding-top:12px}
summary{cursor:pointer;color:#b9cbd9}h2{white-space:pre-wrap}
</style></head><body><main>''' + content + '</main></body></html>').encode()


class DecisionController:
    def __init__(self, store: Store, *, origin: str, qualification_reference: str):
        self.store = store
        self.origin = origin
        self.capability = store.host_capability("Codex local HTTPS decision tab", qualification_reference)
        self.sessions: dict[str, tuple[str, str, int, str | None]] = {}
        self.lock = Lock()

    def current(self, challenge_id: str | None = None) -> dict | None:
        current = [row for row in self.store.pending_approvals() if row["current"]
                   and (challenge_id is None or row["challenge_id"] == challenge_id)]
        if len(current) > 1:
            raise Rejected("multiple_current_decisions", "More than one current decision awaits an operator")
        return current[0] if current else None

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
                                      for row in controller.store.pending_approvals() if row["current"])
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
                states = controller.store.decision_states()
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
            body = f"""<h1>Needs your decision</h1><p class="note">Review this proposal before choosing. Closing this tab leaves work held.</p>
<section><small>Proposal revision {package['revision']}</small>
<h2>{escape(package['body'])}</h2>
<p><strong>Recommendation:</strong> {escape(package.get('recommendation', 'Approve only if the complete package matches your intent.'))}</p>
<p><strong>Alternative:</strong> {escape(package.get('alternative', 'Decline or cancel; work stays held.'))}</p>
<p><strong>Consequence:</strong> {escape(package.get('consequence', 'Approval records this exact concept; implementation dispatch remains gated.'))}</p>
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
                if (set(values) != {"challenge", "csrf", "choice"} or values["choice"] not in {"approve", "decline", "cancel"}
                        or not token):
                    self.reject(HTTPStatus.FORBIDDEN)
                    return
                session_use = controller.use_session(token.value, values["challenge"], values["csrf"], values["choice"])
                if session_use == "invalid":
                    self.reject(HTTPStatus.FORBIDDEN)
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
            body = document(f"<h1>Decision recorded</h1><section><p>Outcome: <strong>{escape(result['outcome'])}</strong></p>"
                            f"<p>Work: {escape(result['work_id'])} · revision: {result['concept_revision']}</p>"
                            f"<p>Digest: {escape(result['digest'])}</p><p>Decision ID: {escape(result['decision_id'])}</p>"
                            "<p>Implementation dispatch is still gated by the remaining AOS qualifications.</p></section>"
                            '<p><a href="/">Back to decisions</a></p>')
            self.response(HTTPStatus.OK, body, cookie="aos_decision=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/")

        def log_message(self, format: str, *args: object) -> None:
            # Never log the browser's form body, cookies, or challenge information.
            pass

    return Handler


def serve_https(store: Store, *, certificate: str, key: str, port: int, qualification_reference: str) -> None:
    if not 1 <= port <= 65535:
        raise Rejected("invalid_port", "HTTPS port must be in range 1-65535")
    key_path = Path(key)
    key_info = key_path.lstat()
    if (not stat.S_ISREG(key_info.st_mode) or key_info.st_uid != os.getuid()
            or stat.S_IMODE(key_info.st_mode) & 0o077):
        raise Rejected("unsafe_tls_key", "TLS key must be a private user-owned regular file")
    origin = f"https://127.0.0.1:{port}"
    controller = DecisionController(store, origin=origin, qualification_reference=qualification_reference)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(controller))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except (OSError, ssl.SSLError) as error:
        server.server_close()
        raise Rejected("tls_unavailable", "Local HTTPS certificate or key could not be loaded; decisions remain held") from error
    print(origin + "/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
