"""Attended human-input host. Never expose this endpoint or key to agents.

An authenticated direct form submission is the original instruction; it is not
evidence about a chat transcript. Operator isolation is a deployment obligation,
just as for the existing decision host. Tests submit synthetic human inputs.
"""
from __future__ import annotations

from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from threading import Lock
from urllib.parse import parse_qs, urlencode

from .approval_web import DecisionService, document
from . import human_intent
from .lifecycle import Lifecycle
from .store import Rejected, canonical, digest, identifier, now


SCHEMA = """CREATE TABLE IF NOT EXISTS human_operator_input (
 input_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, payload TEXT NOT NULL,
 received_at TEXT NOT NULL, result TEXT);
"""


def private_file(path: Path, store) -> Path:
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Rejected('operator_file', 'Operator files must not use symbolic links')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or any(path.resolve().is_relative_to(p.resolve()) for p in store.worker_writable_roots)):
        raise Rejected('operator_file', 'Operator credentials must be private and outside worker-writable roots')
    return path


class IntentController:
    def __init__(self, store, *, origin: str, work_id: str, operator_key: Path, qualification_reference: str):
        self.store, self.origin, self.work_id = store, origin, identifier(work_id)
        self.lifecycle = Lifecycle(store)
        # Bind only an existing registered work; never introduce or approve work.
        with store._connection() as db:
            self.lifecycle._run(db, work_id)
            db.executescript(SCHEMA)
        path = private_file(operator_key, store)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise Rejected('operator_key', 'Operator key changed or is not private')
            self.secret = stream.read(257).strip()
        if len(self.secret) < 32 or len(self.secret) > 256 or not self.secret.isascii():
            raise Rejected('operator_key', 'Use a private random operator key of 32-256 ASCII characters')
        self.capability = store.host_capability('Attended HTTPS instruction controls', qualification_reference)
        self.sessions = {}
        self.forms = {}
        self.lock = Lock()

    def status(self):
        return self.store.read(self.work_id)['lifecycle']['human_intent']

    def login(self, supplied):
        if not hmac.compare_digest(supplied.encode(), self.secret.encode()):
            raise Rejected('operator_access', 'Operator access required')
        with self.lock:
            # Bound sessions per foreground service. Restart revokes all sessions.
            if len(self.sessions) >= 32:
                raise Rejected('operator_access', 'Restart this foreground listener to renew operator sessions')
            token = secrets.token_urlsafe(32)
            self.sessions[token] = secrets.token_urlsafe(32)
            return token, self.sessions[token]

    def csrf(self, token):
        with self.lock:
            return self.sessions.get(token)

    def issue_form(self, csrf, current):
        with self.lock:
            if len(self.forms) >= 512:
                raise Rejected('operator_access', 'Restart this foreground listener to renew forms')
            input_id = 'human-' + secrets.token_hex(16)
            self.forms[input_id] = (csrf, digest(current))
            return input_id

    def submit(self, fields):
        """Only the authenticated handler calls this method, never coordinator MCP."""
        input_id = identifier(fields['input_id'])
        binding = self.forms.get(input_id)
        if not binding or not hmac.compare_digest(binding[0], fields['csrf']):
            raise Rejected('operator_access', 'Read a form from this operator session first')
        action = fields['action']
        if action not in {'enroll', 'stop', 'pause', 'resume', 'cancel', 'unresolved', 'replay', 'handoff'}:
            raise Rejected('operator_action', 'Unknown operator action')
        payload = {k: v for k, v in fields.items() if k != 'csrf'}
        with self.store._transaction() as db:
            prior = db.execute('SELECT * FROM human_operator_input WHERE input_id=?', (input_id,)).fetchone()
            if prior and prior['payload'] != canonical(payload):
                raise Rejected('operator_replay', 'This form identity already names different original input')
            if prior and prior['result']:
                return json.loads(prior['result'])
            db.execute('INSERT OR IGNORE INTO human_operator_input VALUES(?,?,?,?,NULL)',
                       (input_id, self.work_id, canonical(payload), now()))
        if prior:
            return self.recover(prior)
        if action == 'enroll':
            human_intent.enable(self.lifecycle, self.work_id, fields['owner'], capability=self.capability)
            result = {'message': 'Instruction controls enabled; supply a current instruction to release the human hold.'}
        elif action == 'handoff':
            human_intent.require_handoff(self.lifecycle, self.work_id, fields['reference'], fields['owner'])
            result = {'message': 'Same-owner handoff checked. Responsibilities remain unchanged; this does not dispatch work.'}
        elif action == 'replay':
            with self.store._connection() as db:
                row = db.execute('SELECT evidence FROM human_instruction WHERE instruction_id=?', (fields['reference'],)).fetchone()
                pending = db.execute('SELECT * FROM human_operator_input WHERE input_id=? AND work_id=? AND result IS NULL',
                                     (fields['reference'], self.work_id)).fetchone()
            if pending:
                original = self.recover(pending)
                return self.finish(input_id, {'message': 'Interrupted original input read back; no new lifecycle action was applied.',
                                             'original_receipt': original})
            if not row:
                raise Rejected('operator_replay', 'Original instruction is unavailable; no history was invented')
            evidence = json.loads(row[0])
            if evidence['scope'] != [self.work_id]:
                raise Rejected('operator_scope', 'This listener replays only its exact work-scoped inputs')
            result = human_intent.record(self.lifecycle, evidence, capability=self.capability)
            result['message'] = 'Original evidence replayed with the same identity. No new instruction or lifecycle action was created.'
        else:
            before = self.status()
            if before['disposition'] == 'unenrolled':
                raise Rejected('human_intent', 'Enable instruction controls first')
            if fields['owner'] != before['owner']:
                raise Rejected('handoff_owner', 'A control form cannot change the agreed owner')
            evidence = {'instruction_id': input_id, 'human_reference': 'operator-input:' + input_id,
                        'source_kind': 'direct_human', 'scope': [self.work_id],
                        'action': 'resume' if action == 'unresolved' else action,
                        'supersedes': fields['supersedes'].split(),
                        'provenance_available': action != 'unresolved',
                        'context': 'reconstructed' if action == 'unresolved' else 'original'}
            result = human_intent.record(self.lifecycle, evidence, capability=self.capability,
                                         expected_current={self.work_id: binding[1]})
            result['message'] = 'Original operator input saved.'
            current = self.status()
            if current['disposition'] != 'ambiguous' and current['controlling_instruction'] == input_id:
                control = 'pause' if action == 'stop' else action
                run = self.store.read(self.work_id)
                if control != 'resume' or run['execution'] == 'paused':
                    result['control'] = self.lifecycle.control('operator-control:' + input_id, self.work_id, control,
                                                              instruction_id=input_id)
                    if result['control'].get('blockers'):
                        result['message'] += ' Execution remains held by existing approval, cleanup or technical requirements.'
            else:
                result['message'] += ' Work remains held for evidence reconciliation.'
        return self.finish(input_id, result)

    def recover(self, prior):
        """A missing result is uncertainty, never permission to repeat an effect."""
        with self.store._connection() as db:
            saved = db.execute('SELECT 1 FROM human_instruction WHERE instruction_id=?', (prior['input_id'],)).fetchone()
        return self.finish(prior['input_id'], {
            'message': ('Original instruction is retained; lifecycle application was interrupted or is unverified. '
                        'Read current state; no lifecycle action was repeated.' if saved else
                        'Original form was retained but its effect is unverified. No instruction was inferred or repeated. Read current state.'),
            'interrupted': True, 'instruction_retained': bool(saved)})

    def finish(self, input_id, result):
        result['status'] = self.status()
        with self.store._transaction() as db:
            db.execute('UPDATE human_operator_input SET result=? WHERE input_id=? AND result IS NULL', (canonical(result), input_id))
        # Verify the saved receipt rather than claim success on an in-memory result.
        with self.store._connection() as db:
            return json.loads(db.execute('SELECT result FROM human_operator_input WHERE input_id=?', (input_id,)).fetchone()[0])


def handler_for(controller):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *_args):
            pass  # Never log form inputs, cookies or operator credentials.

        def response(self, status, body, cookie=None):
            data = document(body)
            self.send_response(status)
            for key, value in {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(data)),
                               'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                               'X-Content-Type-Options': 'nosniff',
                               'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}.items():
                self.send_header(key, value)
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get('Host') == controller.origin.removeprefix('https://')

        def session(self):
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['aos_intent'].value if 'aos_intent' in cookie else ''
            return controller.csrf(token)

        def do_GET(self):
            try:
                if self.path != '/' or not self.valid_host():
                    raise Rejected('operator_access', 'Open the exact operator page')
                csrf = self.session()
                if not csrf:
                    self.response(HTTPStatus.OK, '<h1>Human instruction controls</h1><p>Operator access required. '
                                  'This page does not read chat instructions.</p><form method="post" action="/unlock">'
                                  '<label>Private operator key <input type="password" name="operator_key" autocomplete="off" required></label>'
                                  '<button>Unlock</button></form>')
                    return
                current = controller.status()
                run = controller.store.read(controller.work_id)
                owner = current.get('owner') or json.loads(self.contract())['receiving_owner']
                controls = '<h1>Human instruction controls</h1><section><p>Work: ' + escape(controller.work_id)
                controls += '</p><p>Responsible owner: ' + escape(owner) + '</p><p>Current instruction: <strong>'
                controls += escape(current['disposition']) + '</strong></p><p>Original controlling identity: '
                controls += escape(current.get('controlling_instruction') or 'No verified current instruction')
                controls += '</p><p>Execution: ' + escape(run['execution']) + '</p><p>Next action: '
                controls += escape(current.get('next_action', 'Enable instruction controls with the registered owner.')) + '</p></section>'
                if run['execution'] in {'held', 'paused'} or run['lifecycle']['technical_blockers']:
                    controls += '<p>Execution remains ' + escape(run['execution']) + '. A saved resume does not grant approval, '
                    controls += 'clear cleanup obligations or admit work. The coordinator must reconcile the existing execution gates.</p>'
                refs = current.get('current_heads', []) + [r['conflict_id'] for r in current.get('conflicts', [])]
                controls += '<p>Only a deliberate submission below gives a new instruction. Quoted/history text and status views give no authority. '
                controls += 'This page cannot approve work, transfer roles or start a worker.</p>'
                controls += self.form(csrf, owner, refs, current.get('controlling_instruction') or '', current)
                controls += '<details><summary>Original instruction history and evidence problems</summary><pre>'
                controls += escape(json.dumps(current, indent=2)) + '</pre></details><p><a href="/">Refresh saved status</a></p>'
                self.response(HTTPStatus.OK, controls)
            except (Rejected, OSError, ValueError, KeyError, sqlite3.Error, CookieError):
                self.response(HTTPStatus.SERVICE_UNAVAILABLE, '<h1>Controls unavailable</h1><p>Read current evidence before acting. No instruction was inferred.</p>')

        def contract(self):
            with controller.store._connection() as db:
                return db.execute('SELECT contract FROM lifecycle WHERE work_id=?', (controller.work_id,)).fetchone()[0]

        def form(self, csrf, owner, refs, reference, current):
            values = {'csrf': csrf, 'input_id': controller.issue_form(csrf, current)}
            hidden = ''.join('<input type="hidden" name="' + k + '" value="' + escape(v, quote=True) + '">' for k, v in values.items())
            return ('<form method="post" action="/instruction">' + hidden
                    + '<label>Instruction replaces these original identities (space separated)<textarea name="supersedes">'
                    + escape(' '.join(refs)) + '</textarea></label><p>Confirm these references before submitting a new instruction.</p>'
                    + '<label>Agreed owner <input name="owner" value="' + escape(owner, quote=True) + '"></label>'
                    + '<label>Original identity for replay / current identity for handoff <input name="reference" value="'
                    + escape(reference, quote=True) + '"></label><p>'
                    + ''.join('<button name="action" value="' + action + '">' + label + '</button>' for action, label in [
                        ('enroll', 'Enable controls'), ('stop', 'Stop this work'), ('pause', 'Pause this work'),
                        ('resume', 'Explicitly resume this work'), ('cancel', 'Cancel this execution'),
                        ('unresolved', 'Record unresolved history and hold'), ('replay', 'Replay original input'),
                        ('handoff', 'Check same-owner handoff')]) + '</p></form>')

        def do_POST(self):
            try:
                if (self.path not in {'/unlock', '/instruction'} or not self.valid_host()
                        or any(len(self.headers.get_all(name, [])) != 1 for name in
                               ('Host', 'Origin', 'Sec-Fetch-Site', 'Content-Type', 'Content-Length'))
                        or self.headers.get('Origin') != controller.origin
                        or self.headers.get('Sec-Fetch-Site') != 'same-origin'
                        or self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'
                        or self.headers.get('Transfer-Encoding')):
                    raise Rejected('operator_access', 'Same-origin operator submission required')
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 16384:
                    raise Rejected('operator_access', 'Bounded form required')
                body = self.rfile.read(size)
                if len(body) != size:
                    raise Rejected('operator_access', 'Complete form required')
                pairs = parse_qs(body.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=8)
                if any(len(v) != 1 for v in pairs.values()):
                    raise Rejected('operator_access', 'Unique form fields required')
                fields = {k: v[0] for k, v in pairs.items()}
                if self.path == '/unlock':
                    if set(fields) != {'operator_key'}:
                        raise Rejected('operator_access', 'Operator access required')
                    token, _ = controller.login(fields['operator_key'])
                    self.response(HTTPStatus.OK, '<h1>Operator access opened</h1><p><a href="/">Read current controls</a></p>',
                                  cookie='aos_intent=' + token + '; Secure; HttpOnly; SameSite=Strict; Path=/')
                    return
                csrf = self.session()
                if (not csrf or set(fields) != {'csrf', 'input_id', 'action', 'owner', 'reference', 'supersedes'}
                        or not hmac.compare_digest(csrf.encode(), fields['csrf'].encode())):
                    raise Rejected('operator_access', 'Current operator session required')
                result = controller.submit(fields)
                self.response(HTTPStatus.OK, '<h1>Saved operator result</h1><p>' + escape(result['message'])
                              + '</p><pre>' + escape(json.dumps(result, indent=2)) + '</pre><p><a href="/">Read current controls</a></p>')
            except (Rejected, OSError, ValueError, KeyError, sqlite3.Error, CookieError):
                self.response(HTTPStatus.CONFLICT, '<h1>Submission held</h1><p>No new authority was inferred. '
                              'A submitted instruction may already be retained; read current status before retrying. '
                              'Owner changes are not allowed here.</p><p><a href="/">Read saved status</a></p>')
    return Handler


class IntentService(DecisionService):
    def __init__(self, store, *, operator_key: Path, work_id: str, **options):
        private_file(Path(options['key']), store)
        super().__init__(store, **options)
        self.controller = IntentController(store, origin=self.origin, work_id=work_id, operator_key=operator_key,
                                           qualification_reference=options['qualification_reference'])

    def request_handler(self):
        return handler_for(self.controller)


def serve(store, **options):
    with IntentService(store, **options) as service:
        print(service.url, flush=True)  # The URL contains no operator credential.
        service.wait()
