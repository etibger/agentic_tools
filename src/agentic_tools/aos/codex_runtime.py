"""Bounded local adapter for the pinned 0.163.0 app-server protocol.

This module grants no approval. Recipes are trusted coordinator inputs, bound to
the canonical approved execution envelope. Workers never open this journal.
An uncertain request is never repeated. Detached processes remain unsupported.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

from codex_cli_bin import bundled_codex_path

from .store import Rejected, canonical, digest, identifier, now


PIN = '0.163.0'
PROFILE = 'aos-bounded'
FEATURES = ('hooks', 'plugins', 'apps', 'multi_agent', 'remote_plugin', 'memories', 'remote_control')


def clean_environment() -> dict[str, str]:
    """Use existing account files, never inherited bearer tokens or shell secrets."""
    return {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}


def inventory(root: Path, *, allow_empty: bool = False) -> dict[str, str]:
    """All regular source files, including new files; links/special files fail closed."""
    if root.is_symlink() or not root.is_dir():
        raise Rejected('source_scope', 'An existing real isolated source directory is required')
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise Rejected('source_scope', 'Source contains a link or special file')
        if stat.S_ISREG(mode):
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result and not allow_empty:
        raise Rejected('source_scope', 'Empty source cannot identify a candidate')
    return result


def candidate(root: Path) -> dict:
    files = inventory(root)
    return {'identity': digest(files), 'source_identity': digest(files), 'inventory': files}


def changed(before: dict, after: dict) -> dict:
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    return {'status': 'recorded', 'changes': paths} if paths else {'status': 'none', 'changes': []}


class StdioRPC:
    """Small timeout-bound protocol transport; no SDK inherited-env overlay.

    Server requests are denied, never automatically approved. Reasoning content
    and stderr are drained but not retained. Only factual lifecycle events enter
    the private adapter journal.
    """
    def __init__(self, binary: Path, overrides: list[str], cwd: Path, *, timeout: float = 15):
        args = [str(binary)]
        for item in overrides:
            args.extend(['-c', item])
        args.extend(['app-server', '--listen', 'stdio://'])
        self.timeout = timeout
        self.proc = subprocess.Popen(args, cwd=cwd, env=clean_environment(), text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True, bufsize=1)
        self.waiters = {}
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.closed = False
        self.diagnostics = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for line in self.proc.stderr:
            self.diagnostics.append(line[-1000:])
            self.diagnostics[:] = self.diagnostics[-10:]

    def _send(self, value: dict):
        with self.write_lock:
            self.proc.stdin.write(canonical(value) + '\n')
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                message = json.loads(line)
                if 'method' in message:
                    if 'id' in message:
                        self._send({'id': message['id'], 'error': {'code': -32601, 'message': 'AOS denies server-initiated requests'}})
                    elif message['method'] in ('turn/completed', 'thread/tokenUsage/updated', 'item/started', 'item/completed'):
                        payload = message.get('params', {})
                        item = payload.get('item', {})
                        if message['method'].startswith('item/') and item.get('type') != 'commandExecution':
                            continue
                        self.events.put({'method': message['method'], 'params': payload})
                elif 'id' in message:
                    with self.lock:
                        waiter = self.waiters.get(str(message['id']))
                    if waiter:
                        waiter.put(message)
        except Exception:
            pass
        finally:
            self.closed = True
            with self.lock:
                for waiter in self.waiters.values():
                    waiter.put({'error': {'message': 'Runtime transport closed'}})

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = str(uuid.uuid4())
        waiter = queue.Queue()
        with self.lock:
            self.waiters[request_id] = waiter
        try:
            self._send({'id': request_id, 'method': method, 'params': params or {}})
            message = waiter.get(timeout=self.timeout)
            if 'error' in message or not isinstance(message.get('result'), dict):
                detail = str(message.get('error', {}).get('message', 'unrecognized result'))[:500]
                raise Rejected('runtime_rpc', f'{method} failed: {detail}')
            return message['result']
        except queue.Empty as error:
            raise Rejected('runtime_timeout', f'{method} reply unavailable; request must not be repeated') from error
        finally:
            with self.lock:
                self.waiters.pop(request_id, None)

    def initialize(self):
        reply = self.request('initialize', {'clientInfo': {'name': 'aos_bounded_worker', 'title': 'AOS bounded worker', 'version': '0.3.0'},
                                            'capabilities': {'experimentalApi': True}})
        self._send({'method': 'initialized'})
        return reply

    def drain_events(self) -> list[dict]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.closed = True


def processes() -> dict[int, tuple[int, str]]:
    reply = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,lstart='], capture_output=True,
                           text=True, timeout=5, check=True, env=clean_environment())
    rows = {}
    for line in reply.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            rows[int(fields[0])] = (int(fields[1]), fields[2])
    return rows


def process_groups() -> dict[int, dict]:
    """Observe group membership as well as PID/start identity; never match names."""
    reply = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,lstart='], capture_output=True,
                           text=True, timeout=5, check=True, env=clean_environment())
    return {int(parts[0]): {'parent': int(parts[1]), 'group': int(parts[2]), 'started': parts[3].strip()}
            for line in reply.stdout.splitlines() if len(parts := line.split(None, 3)) == 4}


# The trusted session leader waits after the sandbox command exits. Keeping that
# exact leader alive prevents PGID reuse while ordinary children are discovered
# and stopped, including children reparented to PID 1 after pytest exits. Its
# private status/release files are denied to sandboxed commands. A worker death
# leaves a durable owned guard for explicit restart/cancel reconciliation.
NATIVE_SUPERVISOR = '''import json,os,subprocess,sys,time
from pathlib import Path
p=json.loads(sys.argv[1])
if sys.stdin.buffer.read(1)!=b'G':raise SystemExit(1)
child=subprocess.Popen(p['argv'],cwd=p['cwd'],stdin=subprocess.DEVNULL)
code=child.wait()
status=Path(p['status'])
temporary=status.with_suffix('.writing')
temporary.write_text(json.dumps({'exit_code':code}))
os.replace(temporary,status)
while not Path(p['release']).exists():time.sleep(.025)
'''


class CodexRuntime:
    """One bounded turn per durable attempt; completion requires native evidence.

    The lifecycle must validate the human decision and execution envelope first.
    An adapter recipe is admitted only when its digest is also present in that
    exact approved envelope. No CLI operator flag can manufacture this binding.
    """
    def __init__(self, lifecycle, recipes: dict, *, transport_factory=StdioRPC):
        self.lifecycle = lifecycle
        self.directory = lifecycle.store.directory
        self.path = self.directory / 'codex-runtime.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS attempt (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS observed_process (attempt_id TEXT, pid TEXT, started TEXT, PRIMARY KEY(attempt_id,pid,started))')
            db.execute('CREATE TABLE IF NOT EXISTS native_check (id TEXT PRIMARY KEY, attempt_id TEXT, record TEXT NOT NULL)')
        self.recipes = recipes
        self.factory = transport_factory
        self.clients = {}
        self.model_calls = 0
        self._fault = lambda stage: None
        self._monitors = {}
        self._timers = {}
        self._native_processes = {}
        self._native_lock = threading.RLock()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, attempt_id: str) -> dict | None:
        identifier(attempt_id)
        with self._db() as db:
            row = db.execute('SELECT record FROM attempt WHERE id=?', (attempt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, record: dict):
        with self._db() as db:
            db.execute('UPDATE attempt SET record=? WHERE id=?', (canonical(record), record['attempt_id']))

    def _recipe(self, assignment: dict) -> dict:
        errors = self.lifecycle.assignment_authority_errors(assignment)
        if errors:
            raise Rejected('runtime_authority', '; '.join(errors))
        recipe = self.recipes.get(assignment['work_id'])
        if not recipe:
            raise Rejected('runtime_recipe', 'No exact approved runtime recipe')
        with self.lifecycle.store._connection() as db:
            _, contract = self.lifecycle._run(db, assignment['work_id'])
            saved = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND attempt_id=?',
                               (assignment['assignment_id'], assignment['attempt_id'])).fetchone()
            if not saved or saved['state'] != 'sent' or any(saved[k] != assignment.get(k) for k in
                    ('work_id', 'kind', 'concept_revision', 'concept_digest', 'contract_revision', 'scope',
                     'executor', 'requested_model', 'requested_effort')):
                raise Rejected('runtime_identity', 'Only the exact saved sent assignment may launch')
            package = json.loads(db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?',
                                           (assignment['work_id'], assignment['concept_revision'])).fetchone()[0])
        if package['execution_envelope'].get('runtime_recipe_digest') != digest(recipe):
            raise Rejected('runtime_recipe', 'Recipe differs from the exact approved execution envelope')
        if recipe.get('requires_detached_processes') is not False or not 1 <= recipe.get('max_seconds', 0) <= min(300, contract['max_seconds']):
            raise Rejected('runtime_recipe', 'Bounded non-detached recipe required')
        source, outputs = Path(recipe['source']).resolve(), Path(recipe['outputs']).resolve()
        denied = (self.directory.resolve(), Path.home() / '.codex')
        roots = [source, outputs] + [Path(p).resolve() for p in recipe['read_roots']]
        if any(str(Path(p).resolve()) != p for p in [recipe['source'], recipe['outputs'], *recipe['read_roots']]):
            raise Rejected('runtime_scope', 'Recipe roots must be canonical absolute paths')
        if source == outputs or source.is_relative_to(outputs) or outputs.is_relative_to(source):
            raise Rejected('runtime_scope', 'Source and check outputs must be disjoint')
        for root in roots:
            if any(root == p or root.is_relative_to(p) or p.is_relative_to(root) for p in denied):
                raise Rejected('runtime_scope', 'Worker roots overlap authoritative state or credential scope')
        if not source.is_dir() or not outputs.is_dir():
            raise Rejected('runtime_scope', 'Coordinator must prepare real isolated source and output directories')
        if (not recipe.get('checks') or not recipe.get('immutable') or not recipe.get('mutable')
                or set(c['name'] for c in recipe['checks']) != set(contract['required_checks'] + contract.get('compatibility_checks', []))):
            raise Rejected('runtime_recipe', 'Exact native checks, immutable tests and mutable source allowlist required')
        if set(recipe['immutable']) & set(recipe['mutable']):
            raise Rejected('runtime_recipe', 'Immutable tests and mutable source must be disjoint')
        files = inventory(source)
        if assignment['kind'] == 'implementation' and digest(files) != contract['baseline']:
            raise Rejected('source_drift', 'Implementation source differs from the exact approved baseline')
        if any(files.get(path) != expected for path, expected in recipe['immutable'].items()):
            raise Rejected('source_drift', 'Immutable source/check hashes differ before launch')
        for relative in [*recipe['immutable'], *recipe['mutable']]:
            self._source_path(recipe, relative)
        for check in recipe['checks']:
            identifier(check['name'])
            if (not isinstance(check.get('argv'), list) or not check['argv']
                    or not all(isinstance(part, str) and part for part in check['argv'])
                    or type(check.get('timeout_seconds')) is not int
                    or not 1 <= check['timeout_seconds'] <= recipe['max_seconds'] or not check.get('scope')):
                raise Rejected('runtime_recipe', 'Bounded exact command, check scope and timeout required')
        for key in ('implementation_prompt', 'assessment_prompt'):
            if not isinstance(recipe.get(key), str) or not recipe[key].strip():
                raise Rejected('runtime_recipe', 'Bounded prompts required')
        return recipe

    def _budget(self, assignment: dict) -> dict:
        with self.lifecycle.store._connection() as db:
            package = json.loads(db.execute('SELECT package FROM concept WHERE work_id=? AND revision=?',
                                           (assignment['work_id'], assignment['concept_revision'])).fetchone()[0])
        budget = package['execution_envelope'].get('shared_qualification_budget', {})
        if (budget.get('budget_id') != 'aos-q2q3-runtime-v1'
                or budget.get('ledger_directory') != str(self.directory.resolve())
                or type(budget.get('max_assignments')) is not int or not 1 <= budget['max_assignments'] <= 8
                or type(budget.get('max_seconds')) is not int or not 1 <= budget['max_seconds'] <= 2400):
            raise Rejected('runtime_budget', 'Exact shared Q2/Q3 budget and authoritative ledger binding required')
        return budget

    def budget_receipt(self) -> dict:
        with self._db() as db:
            attempts = [json.loads(row[0]) for row in db.execute('SELECT record FROM attempt')]
        return {'budget_id': 'aos-q2q3-runtime-v1', 'ledger_directory': str(self.directory.resolve()),
                'attempts_reserved': len(attempts), 'seconds_reserved': sum(r.get('budget_seconds', 300) for r in attempts),
                'accounting': 'failed/ambiguous attempts retain one slot and full time reservation; never reset per work or stage',
                'actual_billing': 'unavailable'}

    def _profile(self, recipe: dict, assessment: bool = False) -> tuple[list[str], dict]:
        filesystem = {':root': 'deny', ':minimal': 'read', recipe['source']: 'read' if assessment else 'write',
                      recipe['outputs']: 'write'}
        filesystem.update({p: 'read' for p in recipe['read_roots']})
        filesystem.update({str(self.directory.resolve()): 'deny', str(Path.home() / '.codex'): 'deny'})
        for relative in recipe['immutable']:
            filesystem[str(self._source_path(recipe, relative))] = 'read'
        profile = {'filesystem': filesystem, 'network': {'enabled': False}}
        shell_set = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONDONTWRITEBYTECODE': '1'}
        entries = ','.join(f'{json.dumps(k)}={json.dumps(v)}' for k, v in filesystem.items())
        overrides = ['approval_policy="never"', 'web_search="disabled"', f'default_permissions="{PROFILE}"',
                     'shell_environment_policy.inherit="none"',
                     'shell_environment_policy.set={PATH="/usr/bin:/bin:/usr/sbin:/sbin",PYTHONDONTWRITEBYTECODE="1"}',
                     f'permissions.{PROFILE}={{filesystem={{{entries}}},network={{enabled=false}}}}']
        overrides.extend(f'features.{key}=false' for key in FEATURES)
        # Read config only to disable known inherited integrations. Unseen effective
        # integrations are rejected by _verify_config before thread/turn creation.
        source = Path(recipe['source']).resolve()
        paths = [Path.home() / '.codex/config.toml'] + [p / '.codex/config.toml' for p in reversed([source, *source.parents])]
        for path in dict.fromkeys(paths):
            if path.exists():
                config = tomllib.loads(path.read_text())
                for table in ('mcp_servers', 'plugins'):
                    for name in config.get(table, {}):
                        # 0.163.0's -c path parser treats quotes literally. Its
                        # dotted path syntax cannot safely address a dotted key.
                        if any(char in name for char in '.\"\n\r='):
                            raise Rejected('runtime_config_key', 'Inherited integration key cannot be safely overridden by the pinned CLI')
                        overrides.append(f'{table}.{name}.enabled=false')
                # CLI table overrides merge with configured shell values. Blank
                # every inherited explicit value as well as disabling inheritance.
                for name in config.get('shell_environment_policy', {}).get('set', {}):
                    if name not in shell_set:
                        if any(char in name for char in '.\"\n\r='):
                            raise Rejected('runtime_config_key', 'Configured shell key cannot be safely denied by the pinned CLI')
                        shell_set[name] = ''
                        overrides.append(f'shell_environment_policy.set.{name}=""')
        profile['shell_environment'] = shell_set
        return overrides, profile

    @staticmethod
    def _source_path(recipe: dict, relative: str) -> Path:
        path = Path(recipe['source']) / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
            raise Rejected('runtime_scope', 'Exact regular relative source paths required')
        return path

    @staticmethod
    def _verify_config(config: dict, profile: dict):
        if (config.get('approval_policy') != 'never' or config.get('web_search') != 'disabled'
                or config.get('default_permissions') != PROFILE
                or any(config.get('features', {}).get(key) is not False for key in FEATURES)
                or any(v.get('enabled', True) for table in ('mcp_servers', 'plugins') for v in config.get(table, {}).values())):
            raise Rejected('runtime_permissions', 'Inherited tools or approval/configuration restrictions are not verified')
        effective = config.get('permissions', {}).get(PROFILE, {})
        if (effective.get('extends') is not None or effective.get('workspace_roots')
                or {k: v for k, v in effective.get('filesystem', {}).items() if v is not None} != profile['filesystem']
                or effective.get('network', {}).get('enabled') is not False
                or config.get('shell_environment_policy', {}).get('inherit') != 'none'
                or config.get('shell_environment_policy', {}).get('set') != profile['shell_environment']):
            raise Rejected('runtime_permissions', 'Effective filesystem/network/environment differs from the approved restrictions')

    def _client(self, record: dict, recipe: dict):
        aid = record['attempt_id']
        if aid in self.clients and not self.clients[aid].closed:
            return self.clients[aid]
        rows = processes()
        for owner in record.get('server_history', []):
            if owner['pid'] in rows and rows[owner['pid']][1] == owner['started']:
                raise Rejected('runtime_owner_unavailable', 'Saved app-server is alive without its control transport; ownership remains held')
        if version('openai-codex') != PIN or version('openai-codex-cli-bin') != PIN:
            raise Rejected('runtime_version', 'Pinned SDK and bundled runtime must both be 0.163.0')
        binary = bundled_codex_path()
        runtime_version = subprocess.run([str(binary), '--version'], capture_output=True, text=True,
                                         timeout=10, check=True, env=clean_environment()).stdout.strip()
        if runtime_version != 'codex-cli ' + PIN:
            raise Rejected('runtime_version', 'Bundled CLI version mismatch')
        scoped = {**recipe, 'outputs': record['run_outputs']}
        overrides, profile = self._profile(scoped, record['assignment']['kind'] == 'assessment')
        client = self.factory(binary, overrides, Path(recipe['source']))
        try:
            row = processes().get(client.proc.pid)
            if row:
                record.setdefault('server_history', []).append({'pid': client.proc.pid, 'started': row[1]})
            elif self.factory is StdioRPC:
                raise Rejected('runtime_processes', 'Cannot establish spawned app-server OS identity')
            self._save(record)
            client.initialize()
            config = client.request('config/read', {'cwd': recipe['source'], 'includeLayers': False})['config']
            self._verify_config(config, profile)
        except Exception:
            client.close()
            raise
        self.clients[aid] = client
        record.update(runtime_version=runtime_version, effective_permissions=profile, configuration_verified_at=now())
        self._save(record)
        try:
            record['sandbox_probe'] = self._probe_sandbox(record, scoped)
            self._save(record)
        except Exception:
            client.close()
            raise
        self._watch(aid, client)
        return client

    def _probe_sandbox(self, record: dict, recipe: dict) -> dict:
        """Exercise the effective OS boundary before any model turn is sent."""
        assessment = record['assignment']['kind'] == 'assessment'
        overrides, _ = self._profile(recipe, assessment)
        code = '''import json,os,socket,sys
from pathlib import Path
source,outputs,state,credentials=map(Path,sys.argv[1:5])
reviewer=sys.argv[5]=='assessment'
answer={}
def denied(key,action):
 try: action(); answer[key]=False
 except PermissionError: answer[key]=True
denied('state_read_denied',lambda:(state/'codex-runtime.sqlite3').open('rb'))
denied('state_write_denied',lambda:(state/'worker-forbidden-canary').open('w'))
denied('credentials_denied',lambda:list(credentials.iterdir()))
denied('immutable_write_denied',lambda:os.close(os.open(sys.argv[7],os.O_WRONLY)))
p=outputs/('probe-'+sys.argv[6]+'.txt')
with p.open('x') as f:f.write('bounded permission probe')
answer['output_write']=p.read_text()=='bounded permission probe'
p.unlink()
p=source/('probe-'+sys.argv[6]+'.txt')
try:
 with p.open('x') as f:f.write('bounded permission probe')
 p.unlink(); answer['source_access']=not reviewer
except PermissionError:answer['source_access']=reviewer
with socket.socket() as s:denied('network_denied',lambda:s.bind(('127.0.0.1',0)))
print(json.dumps(answer))
raise SystemExit(0 if all(answer.values()) else 1)
'''
        args = [str(bundled_codex_path())]
        for item in overrides:
            args.extend(['-c', item])
        args.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', recipe['source'],
                     recipe['checks'][0]['argv'][0], '-c', code, recipe['source'], recipe['outputs'],
                     str(self.directory.resolve()), str(Path.home() / '.codex'),
                     record['assignment']['kind'], record['attempt_id'],
                     str(self._source_path(recipe, next(iter(recipe['immutable']))))])
        run = subprocess.run(args, capture_output=True, text=True, env=clean_environment(), timeout=15)
        try:
            answer = json.loads(run.stdout)
        except ValueError as error:
            raise Rejected('runtime_sandbox', 'OS permission probe returned no recognized evidence') from error
        if run.returncode != 0 or not answer or not all(value is True for value in answer.values()):
            raise Rejected('runtime_sandbox', 'OS permission probe did not demonstrate required denials/access')
        return {'observed_at': now(), 'checks': answer, 'model_calls': 0, 'exit_code': run.returncode}

    def _watch(self, aid, client):
        stop = threading.Event()
        observed = {}
        self._monitors[aid] = (stop, observed)
        def watch():
            while not stop.is_set():
                try:
                    rows = processes()
                    owned = {client.proc.pid}
                    while True:
                        new = {p for p, (parent, _) in rows.items() if parent in owned}
                        if new.issubset(owned):
                            break
                        owned.update(new)
                    observed.update({str(p): rows[p][1] for p in owned if p != client.proc.pid})
                    with self._db() as db:
                        db.executemany('INSERT OR IGNORE INTO observed_process VALUES(?,?,?)',
                                       [(aid, str(p), rows[p][1]) for p in owned if p != client.proc.pid])
                except Exception:
                    observed['unavailable'] = 'process observation failed'
                stop.wait(.1)
        threading.Thread(target=watch, daemon=True).start()

    def start(self, assignment: dict) -> dict:
        aid = assignment['attempt_id']
        identifier(aid)
        prior_record = self._get(aid)
        if prior_record:
            if any(prior_record['assignment'].get(k) != assignment.get(k) for k in
                   ('assignment_id', 'attempt_id', 'work_id', 'kind', 'concept_revision', 'concept_digest',
                    'contract_revision', 'scope', 'executor', 'requested_model', 'requested_effort')):
                raise Rejected('runtime_identity', 'Attempt ID already names another assignment')
            return self.lookup(aid)
        recipe = self._recipe(assignment)
        budget = self._budget(assignment)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT record FROM attempt WHERE id=?', (aid,)).fetchone()
            if not prior:
                attempts = [json.loads(row[0]) for row in db.execute('SELECT record FROM attempt')]
                if (len(attempts) >= budget['max_assignments']
                        or sum(r.get('budget_seconds', 300) for r in attempts) + recipe['max_seconds'] > budget['max_seconds']):
                    raise Rejected('runtime_budget', 'Shared Q2/Q3 attempt/time reservation exhausted; no reset or launch')
                record = {'attempt_id': aid, 'assignment': assignment, 'recipe_digest': digest(recipe),
                          'phase': 'intent', 'created_at': now(), 'baseline': inventory(Path(recipe['source'])),
                          'usage_raw': [], 'usage': None, 'observed_processes': {}, 'model_calls_attempted': 0,
                          'budget_id': budget['budget_id'], 'budget_seconds': recipe['max_seconds'],
                          'run_outputs': str(Path(recipe['outputs']) / aid)}
                if assignment['kind'] == 'assessment' and candidate(Path(recipe['source'])) != json.loads(assignment['candidate']):
                    raise Rejected('source_drift', 'Assessment source differs from its frozen candidate')
                db.execute('INSERT INTO attempt VALUES(?,?)', (aid, canonical(record)))
        if prior:
            return self.lookup(aid)
        try:
            Path(record['run_outputs']).mkdir(mode=0o700, exist_ok=False)
            client = self._client(record, recipe)
            record['phase'] = 'thread_request'
            self._save(record)
            self._fault('before_thread_request')
            reply = client.request('thread/start', {'model': assignment['requested_model'], 'cwd': recipe['source'],
                                   'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                                   'config': {'model_reasoning_effort': assignment['requested_effort']}})
            record.update(thread_id=reply['thread']['id'], model=reply.get('model'), effort=reply.get('reasoningEffort'))
            self._save(record)
            if (record['model'] != assignment['requested_model'] or record['effort'] != assignment['requested_effort']
                    or reply.get('approvalPolicy') != 'never' or reply.get('cwd') != recipe['source']):
                raise Rejected('runtime_configuration', 'Observed thread configuration is outside the approved envelope')
            record.update(phase='turn_request', model_calls_attempted=1)
            self._save(record)  # No retry is permitted beyond this durable boundary.
            self.model_calls += 1
            timer = threading.Timer(recipe['max_seconds'], self.cancel, args=(aid,))
            timer.daemon = True
            self._timers[aid] = timer
            timer.start()
            self._fault('before_turn_request')
            text = recipe['assessment_prompt' if assignment['kind'] == 'assessment' else 'implementation_prompt']
            text = text.replace(recipe['outputs'], record['run_outputs'])
            text += '\nAttempt: ' + aid
            if assignment['kind'] == 'assessment':
                text += '\nFrozen candidate: ' + assignment['candidate']
                text += ('\nYour review file must contain JSON with candidate_identity equal to the frozen identity, '
                         'verdict equal to pass, fail, or blocked, findings as a list, and limits as nonempty text. '
                         'A pass requires an empty findings list. Do not claim native check success; the trusted adapter runs them.')
            reply = client.request('turn/start', {'threadId': record['thread_id'], 'input': [{'type': 'text', 'text': text, 'text_elements': []}],
                                                  'effort': assignment['requested_effort'], 'approvalPolicy': 'never'})
            self._fault('after_turn_reply_before_save')
            record.update(turn_id=reply['turn']['id'], phase='running')
            self._save(record)
            return self._observation(record, 'active')
        except Exception as error:
            record['error'] = type(error).__name__ + ': ' + str(error)
            self._save(record)
            return self._observation(record, 'unknown')

    @staticmethod
    def _observation(record, state):
        return {'state': state, 'attempt_id': record['attempt_id'], 'model': record.get('model'), 'effort': record.get('effort'),
                'simulated': False, 'thread_id': record.get('thread_id'), 'turn_id': record.get('turn_id'),
                'usage': record.get('usage'), 'model_calls_attempted': record['model_calls_attempted'],
                'detail': record.get('error')}

    def lookup(self, attempt_id: str) -> dict:
        record = self._get(attempt_id)
        if record is None:
            return {'state': 'absent', 'definitive': True, 'attempt_id': attempt_id}
        if record.get('terminal_observation'):
            return self._cached_terminal(record)
        if not record.get('thread_id') or record['phase'] not in ('turn_request', 'running', 'checking'):
            return self._observation(record, 'unknown')
        return self.poll(attempt_id)

    def _cached_terminal(self, record: dict) -> dict:
        try:
            rows = self._native_for_attempt(record['attempt_id'])
            result = record['terminal_observation'].get('result', {})
            # Earlier adapter receipts predate native ownership. They cannot
            # retrospectively establish that an assessment left no children.
            if (record['assignment']['kind'] == 'assessment' and
                    (not record.get('model_stop_receipt') or
                     set(result.get('native_checks', [])) != {r['check_id'] for r in rows})):
                raise Rejected('check_cleanup_unknown', 'Cached assessment lacks current native ownership evidence')
            return record['terminal_observation']
        except Exception as error:
            record['error'] = type(error).__name__ + ': ' + str(error)
            self._save(record)
            return self._observation(record, 'unknown')

    def _events(self, record, client):
        for event in client.drain_events():
            payload = event['params']
            if payload.get('threadId') != record.get('thread_id'):
                continue
            if payload.get('turnId') and record.get('turn_id') and payload['turnId'] != record['turn_id']:
                continue
            if event['method'] == 'thread/tokenUsage/updated':
                value = payload.get('tokenUsage', {}).get('total')
                if isinstance(value, dict) and all(type(v) is int and v >= 0 for v in value.values() if v is not None):
                    fingerprint = digest(event)
                    if not any(v['fingerprint'] == fingerprint for v in record['usage_raw']):
                        record['usage_raw'].append({'fingerprint': fingerprint, 'at': now(), 'value': value})
                        old = record.get('usage')
                        reset = old and value.get('totalTokens', 0) < old['tokens'].get('totalTokens', 0)
                        tokens = {k: max(v or 0, old['tokens'].get(k, 0) if old else 0) for k, v in value.items()}
                        record['usage'] = {'tokens': tokens, 'partial': bool(reset or old and old['partial']),
                                           'aggregation': 'per-thread cumulative high-water; reset is partial, never added twice',
                                           'billing': 'unavailable', 'cost': None, 'at': now()}
            elif event['method'].startswith('item/'):
                record.setdefault('command_items', {})[payload['item']['id']] = payload['item']
        if record['attempt_id'] in self._monitors:
            record['observed_processes'].update(self._monitors[record['attempt_id']][1])
        self._save(record)

    def _remember_error(self, record, error, operation):
        detail = type(error).__name__ + ': ' + str(error)
        record['error'] = detail
        record.setdefault('error_history', []).append({'operation': operation, 'at': now(), 'detail': detail})
        self._save(record)

    def _startup_metadata_pending(self, record, recipe, error):
        """Only the observed empty saved rollout, after a durable turn ACK.

        This is a read retry, never permission to repeat Start. The original
        assignment clock also applies after a worker/transport reconnect.
        """
        if (getattr(error, 'code', None) != 'runtime_rpc' or not record.get('turn_id')
                or record['phase'] != 'running' or record.get('status_readable_at')):
            return False
        prefix = ('runtime_rpc: thread/read failed: failed to read thread: thread-store internal error: '
                  'failed to read session metadata ')
        message = str(error)
        if not message.startswith(prefix):
            return False
        path, separator, suffix = message[len(prefix):].partition(': rollout at ')
        if (not separator or suffix != path + ' is empty'
                or not Path(path).name.startswith('rollout-')
                or not Path(path).name.endswith('-' + record['thread_id'] + '.jsonl')):
            return False
        started = record['assignment'].get('sent_at') or record['created_at']
        deadline = datetime.fromisoformat(started) + timedelta(seconds=recipe['max_seconds'])
        if datetime.now(timezone.utc) >= deadline:
            return False
        prior = record.get('status_wait', {})
        record['status_wait'] = {'state': 'waiting', 'reason': 'acknowledged_empty_session_metadata',
                                 'thread_id': record['thread_id'], 'turn_id': record['turn_id'],
                                 'first_observed_at': prior.get('first_observed_at', now()),
                                 'last_observed_at': now(), 'deadline': deadline.isoformat()}
        self._save(record)
        return True

    def _read_turn(self, record, recipe, client):
        try:
            reply = client.request('thread/read', {'threadId': record['thread_id'], 'includeTurns': True})
        except Rejected as error:
            self._remember_error(record, error, 'thread/read')
            if self._startup_metadata_pending(record, recipe, error):
                raise Rejected('runtime_status_pending', 'Acknowledged saved turn metadata is not readable before its original deadline') from error
            raise
        thread = reply['thread']
        if thread['id'] != record['thread_id'] or thread.get('cwd') != recipe['source']:
            raise Rejected('runtime_identity', 'Read returned another thread or scope')
        turns = thread.get('turns', [])
        if len(turns) != 1 or record.get('turn_id') and turns[0]['id'] != record['turn_id']:
            raise Rejected('runtime_identity', 'Ambiguous turn identity; never relaunch')
        turn = turns[0]
        if turn.get('status') not in ('inProgress', 'completed', 'failed', 'interrupted'):
            raise Rejected('runtime_state', 'Unrecognized turn state')
        record.update(turn_id=turn['id'], phase='running')
        record.setdefault('status_readable_at', now())
        if record.get('status_wait'):
            record['status_wait'].update(state='readable', resolved_at=now())
        self._save(record)
        return turn

    def poll(self, attempt_id: str) -> dict:
        record = self._get(attempt_id)
        if not record:
            return {'state': 'absent', 'definitive': True, 'attempt_id': attempt_id}
        if record.get('terminal_observation'):
            return self._cached_terminal(record)
        try:
            if not record.get('thread_id') or record['phase'] not in ('turn_request', 'running', 'checking'):
                return self._observation(record, 'unknown')
            recipe = self.recipes[record['assignment']['work_id']]
            if digest(recipe) != record['recipe_digest']:
                raise Rejected('runtime_recipe', 'Saved recipe changed')
            if record['phase'] == 'checking':
                turn, stop = record['terminal_turn'], record['model_stop_receipt']
            else:
                client = self._client(record, recipe)
                self._events(record, client)
                turn = self._read_turn(record, recipe, client)
                if turn.get('status') == 'inProgress':
                    return self._observation(record, 'active')
                stop = self._reconcile(record, recipe, client, interrupt=False, expected_turn=turn)
            if not stop['effects_reconciled']:
                return self._observation(record, 'unknown')
            record.update(phase='checking', terminal_turn=turn)
            record.pop('stop_receipt', None)
            self._save(record)
            result = self._result(record, recipe, turn, stop)
            result.update(self._finish_stop(record, recipe, stop))
            observation = self._observation(record, 'completed' if result['status'] == 'completed' else 'failed')
            observation.update(receipt_id='codex-' + attempt_id, result=result)
            record['terminal_observation'] = observation
            record['phase'] = 'terminal'
            self._save(record)
            return observation
        except Exception as error:
            if getattr(error, 'code', '') == 'runtime_status_pending':
                observation = self._observation(record, 'waiting')
                observation['status_wait'] = record['status_wait']
                return observation
            self._remember_error(record, error, 'poll')
            stop = None
            if record.get('model_stop_receipt') and getattr(error, 'code', '') != 'check_cleanup_unknown':
                try:
                    stop = self._finish_stop(record, recipe, record['model_stop_receipt'])
                except Exception:
                    pass
            if stop:
                observation = self._observation(record, 'failed')
                observation.update(receipt_id='codex-' + attempt_id,
                                   result={'status': 'failed', 'executor': record['assignment']['executor'],
                                           'error': record['error'], **stop})
                record['terminal_observation'] = observation
                record['phase'] = 'terminal'
                self._save(record)
                return observation
            return self._observation(record, 'unknown')

    def _reconcile(self, record, recipe, client, *, interrupt, expected_turn=None):
        # Resume loads the saved thread for terminal administration; it starts no turn.
        resumed = client.request('thread/resume', {'threadId': record['thread_id']})
        if 'thread' in resumed and (resumed['thread'].get('id') != record['thread_id']
                                    or resumed['thread'].get('cwd') != recipe['source']):
            raise Rejected('runtime_identity', 'Resume returned another thread or scope')
        turn = self._read_turn(record, recipe, client)
        if expected_turn and turn['status'] != expected_turn['status']:
            raise Rejected('runtime_state', 'Exact saved terminal status changed during reconciliation')
        if interrupt and turn['status'] == 'inProgress':
            if not record.get('interrupt_requested_at'):
                record['interrupt_requested_at'] = now()
                self._save(record)  # An uncertain interrupt is not repeated.
                client.request('turn/interrupt', {'threadId': record['thread_id'], 'turnId': record['turn_id']})
            turn = self._read_turn(record, recipe, client)
        if turn['status'] == 'inProgress':
            raise Rejected('runtime_stop_pending', 'Exact saved turn is still active; ownership remains held')
        record['terminal_turn_observed'] = {'id': turn['id'], 'status': turn['status'], 'observed_at': now()}
        self._save(record)
        jobs = client.request('thread/backgroundTerminals/list', {'threadId': record['thread_id']}).get('data')
        if not isinstance(jobs, list):
            raise Rejected('runtime_processes', 'Unrecognized terminal registry')
        for job in jobs:
            if not isinstance(job.get('processId'), str) or not job['processId']:
                raise Rejected('runtime_processes', 'Owned terminal identity unavailable')
            client.request('thread/backgroundTerminals/terminate', {'threadId': record['thread_id'], 'processId': job['processId']})
        final = client.request('thread/backgroundTerminals/list', {'threadId': record['thread_id']}).get('data')
        self._events(record, client)
        # Capture descendants before closing the exact app-server. An unexpected
        # surviving identity is held; PID reuse does not target another process.
        rows = processes()
        observed = record['observed_processes']
        with self._db() as db:
            saved = list(db.execute('SELECT pid,started FROM observed_process WHERE attempt_id=?', (record['attempt_id'],)))
        observed.update(dict(saved))
        observed.update({str(owner['pid']): owner['started'] for owner in record.get('server_history', [])})
        descendants = {client.proc.pid}
        while True:
            new = {p for p, (parent, _) in rows.items() if parent in descendants}
            if new.issubset(descendants):
                break
            descendants.update(new)
        observed.update({str(p): rows[p][1] for p in descendants if p != client.proc.pid})
        client.close()
        if record['attempt_id'] in self._monitors:
            self._monitors[record['attempt_id']][0].set()
        until = time.monotonic() + 3
        while True:
            rows = processes()
            alive = [p for p, started in observed.items() if p == 'unavailable' or (int(p) in rows and rows[int(p)][1] == started)]
            if not alive or time.monotonic() >= until:
                break
            time.sleep(.05)
        after = inventory(Path(recipe['source']))
        effects = changed(record['baseline'], after)
        stop = {'attempt_id': record['attempt_id'], 'stopped': client.proc.poll() is not None,
                'owned_processes': alive if final == [] else final, 'effects_reconciled': final == [] and not alive,
                'partial_mutations': effects, 'observed_at': now()}
        record['model_stop_receipt'] = stop
        self._save(record)
        return stop

    def _native_get(self, check_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute('SELECT record FROM native_check WHERE id=?', (check_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _native_save(self, row: dict) -> None:
        with self._db() as db:
            db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(row), row['check_id']))

    def _native_members(self, row: dict) -> tuple[dict, list[int]]:
        snapshot = process_groups()
        owner = row.get('owner')
        if not owner:
            raise Rejected('check_cleanup_unknown', 'Native launch has no durable OS owner; do not retry or release')
        leader = snapshot.get(owner['pid'])
        group = {pid: value for pid, value in snapshot.items() if value['group'] == owner['pid']}
        if leader and (leader['started'] != owner['started'] or leader['group'] != owner['pid']):
            raise Rejected('check_cleanup_unknown', 'Native supervisor identity changed; no unrelated process may be signalled')
        if not leader and any(str(pid) not in row['members'] or row['members'][str(pid)] != value['started']
                              for pid, value in group.items()):
            raise Rejected('check_cleanup_unknown', 'Supervisor disappeared with unrecognized group members; ownership held')
        if leader:
            row['members'].update({str(pid): value['started'] for pid, value in group.items() if pid != owner['pid']})
        self._native_save(row)
        alive = [int(pid) for pid, started in row['members'].items()
                 if int(pid) in snapshot and snapshot[int(pid)]['started'] == started]
        return snapshot, alive

    def _native_stop(self, check_id: str, *, cancel: bool = False) -> dict:
        """Reconcile one exact private-journal owner, never a name or broad PID set."""
        with self._native_lock:
            row = self._native_get(check_id)
            if not row:
                raise Rejected('check_cleanup_unknown', 'Native ownership journal is missing')
            if cancel:
                row['cancelled'] = True
            try:
                if row.get('cleanup', {}).get('effects_reconciled'):
                    # Re-read exact identities even for cached success.
                    snapshot, alive = self._native_members(row)
                    owner = row['owner']
                    if alive or (owner['pid'] in snapshot and snapshot[owner['pid']]['started'] == owner['started']):
                        raise Rejected('check_cleanup_unknown', 'An owned native identity contradicted its clean receipt')
                    return row
                snapshot, alive = self._native_members(row)
                for selected_signal in (signal.SIGTERM, signal.SIGKILL):
                    for pid in alive:
                        current = process_groups().get(pid)
                        if current and current['started'] == row['members'][str(pid)]:
                            try:
                                os.kill(pid, selected_signal)
                            except ProcessLookupError:
                                pass
                    until = time.monotonic() + (0.5 if selected_signal == signal.SIGTERM else 3)
                    while True:
                        snapshot, alive = self._native_members(row)
                        if not alive or time.monotonic() >= until:
                            break
                        time.sleep(.025)
                    if not alive:
                        break
                if alive:
                    raise Rejected('check_cleanup_unknown', 'Native owned processes remain observable after exact cleanup')
                owner = row['owner']
                # The known session leader exits only after child reconciliation.
                release = Path(row['release'])
                if not release.exists():
                    fd = os.open(release, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                process = self._native_processes.get(check_id)
                if process:
                    process.wait(timeout=3)
                until = time.monotonic() + 3
                while True:
                    snapshot = process_groups()
                    current = snapshot.get(owner['pid'])
                    root_alive = current and current['started'] == owner['started']
                    members_alive = [pid for pid, started in row['members'].items()
                                     if int(pid) in snapshot and snapshot[int(pid)]['started'] == started]
                    if not root_alive and not members_alive:
                        break
                    if time.monotonic() >= until:
                        raise Rejected('check_cleanup_unknown', 'Native supervisor or recorded child exit is not verified')
                    time.sleep(.025)
                source_after = inventory(Path(row['source']))
                outputs_after = inventory(Path(row['outputs_root']), allow_empty=True)
                row['cleanup'] = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
                                  'partial_mutations': changed(row['before_source'], source_after),
                                  'output_mutations': changed(row['before_outputs'], outputs_after),
                                  'source_after': source_after, 'outputs_after': outputs_after, 'observed_at': now()}
                row['phase'] = 'clean'
                self._native_save(row)
                return row
            except Exception:
                row.pop('cleanup', None)
                row['phase'] = 'cleanup_unknown'
                self._native_save(row)
                raise

    def _native_for_attempt(self, attempt_id: str, *, cancel: bool = False) -> list[dict]:
        with self._db() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM native_check WHERE attempt_id=? ORDER BY id', (attempt_id,))]
        return [self._native_stop(check_id, cancel=cancel) for check_id in ids]

    def _finish_stop(self, record: dict, recipe: dict, model_stop: dict) -> dict:
        rows = self._native_for_attempt(record['attempt_id'])
        if not model_stop.get('effects_reconciled'):
            raise Rejected('check_cleanup_unknown', 'Model process/effect reconciliation remains unresolved')
        after = inventory(Path(recipe['source']))
        stop = {**model_stop, 'partial_mutations': changed(record['baseline'], after),
                'native_checks': [r['check_id'] for r in rows],
                'native_output_mutations': {r['check_id']: r['cleanup']['output_mutations'] for r in rows},
                'observed_at': now()}
        record['stop_receipt'] = stop
        if record['attempt_id'] in self._timers:
            self._timers[record['attempt_id']].cancel()
        self._save(record)
        return stop

    def _check(self, recipe: dict, check: dict, output: Path, executor: str, frozen: dict) -> dict:
        for parent in [output, *output.parents]:
            if parent.is_symlink():
                raise Rejected('runtime_output', 'Check output path contains a symlink')
        xml_path = output / 'junit.xml'
        overrides, _ = self._profile(recipe, assessment=True)
        args = [str(bundled_codex_path())]
        for item in overrides:
            args.extend(['-c', item])
        command = check['argv'] + ['-p', 'no:cacheprovider', '--junitxml=' + str(xml_path)]
        args.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', recipe['source'], *command])
        check_id = 'nc-' + digest([str(output.resolve()), command, frozen])[7:39]
        with self._native_lock:
            if recipe.get("_attempt_id"):
                attempt = self._get(recipe["_attempt_id"])
                if attempt and attempt.get("native_cancel_requested"):
                    raise Rejected("check_cancelled", "Native admission was durably cancelled")
            row = self._native_get(check_id)
            if not row:
                output.mkdir(mode=0o700, parents=True, exist_ok=False)
                status_dir = self.directory / 'native-status'
                status_dir.mkdir(mode=0o700, exist_ok=True)
                row = {'check_id': check_id, 'attempt_id': recipe.get('_attempt_id'), 'phase': 'intent',
                       'source': recipe['source'], 'outputs_root': recipe['outputs'], 'output': str(output),
                       'status': str(status_dir / (check_id + '.json')), 'release': str(status_dir / (check_id + '.release')),
                       'before_source': inventory(Path(recipe['source'])),
                       'before_outputs': inventory(Path(recipe['outputs']), allow_empty=True),
                       'members': {}, 'command': command, 'candidate': frozen, 'created_at': now()}
                with self._db() as db:
                    db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, row['attempt_id'], canonical(row)))
                stdout_path, stderr_path = output / 'stdout.txt', output / 'stderr.txt'
                payload = {'argv': args, 'cwd': recipe['source'], 'status': row['status'], 'release': row['release']}
                with stdout_path.open('x') as stdout_file, stderr_path.open('x') as stderr_file:
                    process = subprocess.Popen([sys.executable, '-c', NATIVE_SUPERVISOR, canonical(payload)],
                                               cwd=recipe['source'], env=clean_environment(), stdin=subprocess.PIPE,
                                               stdout=stdout_file, stderr=stderr_file, start_new_session=True)
                self._native_processes[check_id] = process
                identity = process_groups().get(process.pid)
                if not identity or identity['group'] != process.pid:
                    process.stdin.close()  # No G byte: no sandbox command can start.
                    raise Rejected('check_cleanup_unknown', 'Native supervisor identity unavailable before execution')
                row.update(owner={'pid': process.pid, 'started': identity['started']}, phase='running')
                self._native_save(row)  # Durable ownership precedes the execution barrier.
                try:
                    self._fault('native_owner_before_go')
                    process.stdin.write(b'G')
                    process.stdin.flush()
                finally:
                    process.stdin.close()
        if row["phase"] == "running" and not row.get("receipt"):
            until = time.monotonic() + min(check['timeout_seconds'], recipe['max_seconds'])
            while not Path(row['status']).is_file() and time.monotonic() < until:
                latest = self._native_get(check_id)
                if latest.get('cancelled') or latest.get('cleanup'):
                    break
                time.sleep(.025)
            if not Path(row['status']).is_file():
                row = self._native_get(check_id)
                row['timed_out'] = not row.get('cancelled')
                self._native_save(row)
            self._fault('native_finished_before_cleanup')
        row = self._native_stop(check_id)
        if row.get('receipt'):
            return row['receipt']
        status_path = Path(row['status'])
        status = json.loads(status_path.read_text()) if status_path.is_file() else {}
        exit_code = status.get('exit_code')
        stdout = (output / 'stdout.txt').read_text()
        stderr = (output / 'stderr.txt').read_text()
        transcript = output / 'output.txt'
        transcript.write_text(stdout + '\n' + stderr)
        selected = 0
        passed = False
        if xml_path.is_file() and not xml_path.is_symlink():
            suites = ET.parse(xml_path).getroot()
            cases = list(suites.iter('testcase'))
            selected = len(cases)
            passed = (selected > 0 and not any(list(case) for case in cases) and exit_code == 0)
        outcome = ('cancelled' if row.get('cancelled') else 'timeout' if row.get('timed_out') else
                   'pass' if passed else 'fail' if selected else 'blocked')
        row['cleanup']['outputs_after'] = inventory(Path(recipe['outputs']), allow_empty=True)
        row['cleanup']['output_mutations'] = changed(row['before_outputs'], row['cleanup']['outputs_after'])
        row['cleanup']['observed_at'] = now()
        result = {'name': check['name'], 'command': command, 'environment': 'pinned Codex sandbox; sanitized env; source read-only',
                'executor': executor, 'selected': selected, 'result': outcome,
                'output': str(transcript), 'output_sha256': hashlib.sha256(transcript.read_bytes()).hexdigest(),
                'candidate': frozen, 'scope': check['scope'], 'limits': 'isolated fixture only; no billing attribution',
                'exit_code': exit_code, 'native_check_id': check_id, 'cleanup': row['cleanup']}
        row['receipt'] = result
        self._native_save(row)
        return result

    def _result(self, record, recipe, turn, stop):
        source = Path(recipe['source'])
        frozen = candidate(source)
        assignment = record['assignment']
        baseline = record['baseline']
        changes = changed(baseline, frozen['inventory'])['changes']
        allowed = set(recipe['mutable'])
        for path, expected in recipe['immutable'].items():
            if frozen['inventory'].get(path) != expected:
                raise Rejected('source_drift', 'An immutable source/check changed')
        if any(path not in allowed for path in changes):
            raise Rejected('source_drift', 'Source changes exceed the exact mutable allowlist')
        result = {'status': 'completed' if turn['status'] == 'completed' else 'failed',
                  'executor': assignment['executor'], 'candidate': frozen, **stop,
                  'runtime_turn_status': turn['status'], 'runtime_thread_id': record['thread_id'],
                  'runtime_turn_id': record['turn_id'], 'usage': record['usage'],
                  'limits': 'bounded local fixture; detached processes and desktop lifetime unqualified'}
        if assignment['kind'] == 'assessment':
            if frozen != json.loads(assignment['candidate']):
                raise Rejected('source_drift', 'Assessment changed the frozen candidate')
            scoped = {**recipe, 'outputs': record['run_outputs'], '_attempt_id': record['attempt_id']}
            output = Path(record['run_outputs'])
            checks = [self._check(scoped, check, output / check['name'], assignment['executor'], frozen) for check in recipe['checks']]
            rows = self._native_for_attempt(record['attempt_id'])
            if {c.get('native_check_id') for c in checks} != {row['check_id'] for row in rows}:
                raise Rejected('check_cleanup_unknown', 'Check evidence does not match the durable native owners')
            if candidate(source) != frozen:
                raise Rejected('source_drift', 'Source changed during native assessment')
            report = output / ('review-' + record['attempt_id'] + '.txt')
            if report.is_symlink() or not report.is_file() or not 0 < report.stat().st_size <= 1024 * 1024 or not report.read_text().strip():
                raise Rejected('missing_review', 'Independent source review report is missing')
            review = json.loads(report.read_text())
            if (review.get('candidate_identity') != frozen['identity'] or review.get('verdict') not in ('pass', 'fail', 'blocked')
                    or not isinstance(review.get('findings'), list) or not isinstance(review.get('limits'), str) or not review['limits'].strip()):
                raise Rejected('invalid_review', 'Independent review must bind the candidate and an explicit verdict, findings and limits')
            result.update(observed_source_identity=frozen['source_identity'], checks=checks,
                          initial_reports={'execution': [c['output'] for c in checks],
                                           'review': {'path': str(report), 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()},
                                           'independent': True})
            if any(c['result'] != 'pass' for c in checks) or review['verdict'] != 'pass' or review['findings']:
                result['status'] = 'failed'
        return result

    def cancel(self, attempt_id: str) -> dict:
        record = self._get(attempt_id)
        unknown = {'attempt_id': attempt_id, 'stopped': False, 'owned_processes': None,
                   'effects_reconciled': False, 'partial_mutations': 'unknown'}
        if not record:
            return unknown
        try:
            # Native ownership always precedes any cached model-stop answer.
            with self._native_lock:
                record['native_cancel_requested'] = True
                self._save(record)
                rows = self._native_for_attempt(attempt_id, cancel=True)
            prior = record.get('stop_receipt')
            if record['assignment']['kind'] == 'assessment' and prior:
                checks = record.get('terminal_observation', {}).get('result', {}).get('checks', [])
                if (not record.get('model_stop_receipt') or
                        checks and {c.get('native_check_id') for c in checks} != {r['check_id'] for r in rows}):
                    return unknown  # Old pre-repair stop evidence cannot prove native cleanup.
            if not record.get('thread_id'):
                return unknown
            recipe = self.recipes[record['assignment']['work_id']]
            if digest(recipe) != record['recipe_digest']:
                return unknown
            if record.get('model_stop_receipt') or prior:
                stop = self._finish_stop(record, recipe, record.get('model_stop_receipt') or prior)
                record['phase'] = 'cancelled'
                self._save(record)
                return stop
            client = self._client(record, recipe)
            stop = self._reconcile(record, recipe, client, interrupt=True)
            if not stop['effects_reconciled']:
                return stop
            stop = self._finish_stop(record, recipe, stop)
            record['phase'] = 'cancelled'
            self._save(record)
            return stop
        except Exception as error:
            if getattr(error, 'code', '') != 'runtime_status_pending':
                self._remember_error(record, error, 'cancel')
            return {**unknown, 'detail': record.get('error')}

    def close(self):
        # Deliberate worker exit never reports cancellation or resource release.
        for stop, _ in self._monitors.values():
            stop.set()
        for timer in self._timers.values():
            timer.cancel()
        for client in self.clients.values():
            client.close()
        with self._db() as db:
            checks = [r[0] for r in db.execute('SELECT id FROM native_check')]
        for check_id in checks:
            try:
                self._native_stop(check_id, cancel=True)
            except Exception:
                pass  # Durable unknown ownership remains held for restart.
