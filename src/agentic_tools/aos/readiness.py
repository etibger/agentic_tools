"""Private, bounded host readiness before any product approval or reservation.

This is a technical receipt, not a decision or assignment. It reuses the model
transport and native check adapter without opening the product runtime journal.
Incomplete ownership is held across calls, even when the requested inputs change.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from codex_cli_bin import bundled_codex_path

from . import checkout_boundary, instructions, native_paths, native_processes
from .checks import adapter_for, source_digest
from .codex_runtime import CodexRuntime, PIN, StdioRPC, inventory, process_groups
from .store import Rejected, canonical, digest, identifier, now
from agentic_tools.check_adapters import identities as adapter_identities

FORMAT = 'aos-readiness-v1'
_CLEANUP_SECONDS = 30


def _save(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise Rejected('readiness_scope', 'Readiness control file became a link')
    temporary = path.with_suffix(path.suffix + '.writing')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # A partial control write is retained; it is not a successful receipt.
        raise


def _load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise Rejected('readiness_evidence', 'Readiness control record is missing, linked or too large')
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise Rejected('readiness_evidence', 'Readiness control record is unreadable') from error
    if not isinstance(value, dict):
        raise Rejected('readiness_evidence', 'Readiness control record must be an object')
    return value


def _canonical_directory(path: Path, *, existing: bool = True) -> Path:
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path
            or any(p.is_symlink() for p in (path, *path.parents))
            or existing and not path.is_dir()):
        raise Rejected('readiness_scope', 'Readiness requires exact canonical directories')
    return path


def _runtime_identity() -> dict:
    packages = {name: version(name) for name in ('openai-codex', 'openai-codex-cli-bin')}
    if set(packages.values()) != {PIN}:
        raise Rejected('runtime_version', 'Readiness requires the pinned SDK and bundled CLI')
    binary = bundled_codex_path().resolve(strict=True)
    return {'packages': packages, 'binary': str(binary),
            'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'runtime_source_digest': source_digest()}


def _normalise(recipe: dict, state: Path) -> dict:
    result = copy.deepcopy(recipe)
    if not checkout_boundary.enabled(result):
        raise Rejected('readiness_policy', 'Private readiness requires isolated-checkout-v1')
    if any(key.startswith('_') for key in result):
        raise Rejected('readiness_authority', 'Product/technical internal execution fields are not readiness inputs')
    manifest = result.get('instruction_manifest')
    # Only the already-supported, explicitly declared project registration may
    # be absorbed. An arbitrary config/source change is never normalized away.
    actual = instructions.readback(manifest, Path(result['source']), after_startup=True)
    if actual['configuration_transition'] is not None or 'startup_project_registration' not in manifest:
        result['instruction_manifest'] = actual['manifest']
    instructions.validate(result['instruction_manifest'], Path(result['source']), state)
    checkout_boundary.validate(result, state)
    return result


def _scoped(recipe: dict, output: Path, controls: tuple[Path, ...]) -> dict:
    result = copy.deepcopy(recipe)
    policy = result['checkout_policy']
    # Protect both journals before preparing any remapped owned XDG directories.
    policy['protected_roots'] = sorted(set(policy.get('protected_roots', [])) | {str(p) for p in controls})
    return checkout_boundary.scoped_outputs(result, output, prepare=True)


def _remaining(deadline: float, *, reserve: float = 0) -> float:
    remaining = deadline - time.monotonic() - reserve
    if remaining <= 0:
        raise Rejected('readiness_deadline', 'The original readiness deadline has expired')
    return remaining



def _source(recipe: dict, deadline: float) -> dict:
    files = inventory(Path(recipe['source']), recipe=recipe, check_deadline=lambda: _remaining(deadline))
    return {'identity': digest(files), 'source_identity': digest(files), 'inventory': files}


class _MetadataRPC(StdioRPC):
    """Expose reader completion for positively observed metadata-only cleanup."""
    def __init__(self, *args, **kwargs):
        self.read_finished = threading.Event()
        self.drain_finished = threading.Event()
        super().__init__(*args, **kwargs)

    def _read(self):
        try:
            return super()._read()
        finally:
            self.read_finished.set()

    def _drain(self):
        try:
            return super()._drain()
        finally:
            self.drain_finished.set()

    def readers_complete(self, timeout: float) -> bool:
        until = time.monotonic() + max(0, timeout)
        return (self.read_finished.wait(max(0, until - time.monotonic()))
                and self.drain_finished.wait(max(0, until - time.monotonic())))


class _MetadataOwner:
    """Current birth-checked lineage only; historical PIDs never seed ownership."""
    def __init__(self, client):
        self.client = client
        self.proc = client.proc
        self.owner = native_processes.identity(self.proc.pid)
        if (self.owner is None or self.owner['uid'] != os.getuid()
                or self.owner['group'] != self.proc.pid):
            raise Rejected('readiness_owner', 'Cannot establish the metadata server owner')
        self.members = {}
        self.errors = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._watch, name='aos-readiness-owner', daemon=True)
        self.thread.start()

    def _observe(self) -> dict:
        from .checkout_runtime import _owned
        with self.lock:
            retained = dict(self.members)
        members = _owned(self.owner, retained, allow_lost=True)
        with self.lock:
            self.members.update(members)
        return members

    def _watch(self):
        while not self.stop.is_set():
            try:
                self._observe()
            except Exception as error:
                with self.lock:
                    self.errors.append(type(error).__name__ + ': ' + str(error))
                return
            self.stop.wait(.1)

    def close(self, deadline: float) -> dict:
        """EOF, then exact owned SIGINT if necessary; no TERM/KILL substitution."""
        errors = []
        try:
            self._observe()
        except Exception as error:
            errors.append(type(error).__name__ + ': ' + str(error))
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            until = min(deadline, time.monotonic() + 2)
            while self.proc.poll() is None and time.monotonic() < until:
                time.sleep(.025)
            if self.proc.poll() is None and native_processes.same(
                    self.owner, native_processes.identity(self.owner['pid'])):
                self.proc.send_signal(signal.SIGINT)
            # The server may have reaped while its known descendants remain.
            # Fresh identities, not cached group membership, authorize signals.
            live = self._observe()
            for pid, saved in live.items():
                if native_processes.same(saved, native_processes.identity(pid)):
                    try:
                        os.kill(pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
            until = min(deadline, time.monotonic() + 5)
            while time.monotonic() < until:
                if self.proc.poll() is not None and not self._observe():
                    break
                time.sleep(.025)
        except Exception as error:
            errors.append(type(error).__name__ + ': ' + str(error))
        finally:
            self.stop.set()
            self.thread.join(timeout=max(0, min(2, deadline - time.monotonic())))
        with self.lock:
            errors.extend(self.errors)
            members = list(self.members.values())
        remaining = []
        absent = False
        group_empty = False
        try:
            absent = not native_processes.same(self.owner, native_processes.identity(self.owner['pid']))
            remaining = [saved for saved in members if native_processes.same(saved, native_processes.identity(saved['pid']))]
            group_empty = not any(row['group'] == self.owner['pid'] for row in process_groups(timeout=2).values())
        except Exception as error:
            errors.append(type(error).__name__ + ': ' + str(error))
        readers = self.client.readers_complete(max(0, min(1, deadline - time.monotonic())))
        reaped = self.proc.poll() is not None
        if readers:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream and not stream.closed:
                    stream.close()
        return {'verified': bool(reaped and absent and group_empty and not remaining and readers
                                 and not self.thread.is_alive() and not errors),
                'owner': self.owner, 'members': members, 'remaining': remaining,
                'server_reaped': reaped, 'owner_absent': absent, 'group_empty': group_empty,
                'readers_complete': readers, 'observer_joined': not self.thread.is_alive(),
                'exit_code': self.proc.poll(), 'errors': errors,
                'scope': 'Observed metadata owner/descendants; excludes no unobserved detached process.'}


def _probe(recipe, **kwargs):
    from .checkout_runtime import probe
    return probe(recipe, **kwargs)


def _resources(recipe, deadline):
    from .checkout_runtime import observe_resources
    return observe_resources(recipe, deadline=deadline)


def _enforce_resources(recipe, before, after):
    from .checkout_runtime import enforce_endpoints
    return enforce_endpoints(recipe, before, after)


def _role(recipe, role, *, state, output, control, deadline, model, effort,
          fingerprint, save_phase, transport_factory, owner_factory, role_probe):
    scoped = _scoped(recipe, output, (state, control.parent, control.parent.parent / 'runtime'))
    runtime = object.__new__(CodexRuntime)
    runtime.directory = state
    overrides, profile = runtime._profile(scoped, assessment=role == 'assessment')
    client = owner = None
    launch_called = False
    probe_inflight = False
    result = {'role': role, 'model_turns': 0, 'cleanup': {'verified': False}, 'phase': 'launch_intent'}
    save_phase(result)
    try:
        timeout = min(15, _remaining(deadline, reserve=5))
        launch_called = True
        client = transport_factory(Path(fingerprint['binary']), overrides, Path(recipe['source']), timeout=timeout)
        owner = owner_factory(client)
        result.update(phase='connected', owner=owner.owner)
        save_phase(result)
        client.initialize()
        client.timeout = min(15, _remaining(deadline, reserve=5))
        config = client.request('config/read', {'cwd': recipe['source'], 'includeLayers': False})['config']
        runtime._verify_config(config, profile)
        instructions.verify_settings(scoped['instruction_manifest'], config)
        result.update(phase='permission_probe_before')
        save_phase(result)
        probe_inflight = True
        result['before_probe'] = role_probe(scoped, state_dir=state, output=output, control=control,
                                          role='writer' if role == 'implementation' else 'assessor', after_startup=False, deadline=deadline - 5)
        probe_inflight = False
        result.update(phase='thread_request', configuration_verified=True)
        save_phase(result)
        client.timeout = min(15, _remaining(deadline, reserve=5))
        reply = client.request('thread/start', {'model': model, 'cwd': recipe['source'],
                               'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                               'config': {'model_reasoning_effort': effort}})
        if (not isinstance(reply.get('thread'), dict) or not isinstance(reply['thread'].get('id'), str)
                or not reply['thread']['id'].strip() or reply.get('model') != model
                or reply.get('reasoningEffort') != effort or reply.get('approvalPolicy') != 'never'
                or reply.get('cwd') != recipe['source']):
            raise Rejected('readiness_configuration', 'Metadata thread settings differ from the requested role')
        result.update(phase='thread_acknowledged', thread_id=reply['thread']['id'])
        save_phase(result)
        actual = instructions.readback(scoped['instruction_manifest'], Path(recipe['source']), after_startup=True)
        recipe['instruction_manifest'] = actual['manifest']
        scoped['instruction_manifest'] = actual['manifest']
        overrides, profile = runtime._profile(scoped, assessment=role == 'assessment', after_startup=True)
        client.timeout = min(15, _remaining(deadline, reserve=5))
        config = client.request('config/read', {'cwd': recipe['source'], 'includeLayers': False})['config']
        runtime._verify_config(config, profile)
        instructions.verify_settings(scoped['instruction_manifest'], config)
        result.update(phase='permission_probe_after')
        save_phase(result)
        probe_inflight = True
        result['after_probe'] = role_probe(scoped, state_dir=state, output=output, control=control,
                                         role='writer' if role == 'implementation' else 'assessor', after_startup=True, deadline=deadline - 5)
        probe_inflight = False
        result.update(phase='metadata_verified', profile_digest=digest(profile),
                      instruction_transition=actual['configuration_transition'])
        save_phase(result)
    except BaseException as error:
        result['error'] = {'code': getattr(error, 'code', 'readiness_failure'), 'detail': str(error)[:2000]}
        if probe_inflight:
            result['failed_probe_cleanup'] = {'verified': getattr(error, 'cleanup_verified', False) is True,
                                              'receipt': str(getattr(error, 'receipt_path', '') or '')}
            path = result['failed_probe_cleanup']['receipt']
            if path:
                try:
                    record = _load(Path(path))
                    result['failed_probe_cleanup'].update(
                        sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                        owner=record.get('owner'), members=record.get('observed', []))
                except Exception:
                    result['failed_probe_cleanup']['verified'] = False
    finally:
        if owner is not None:
            result['cleanup'] = owner.close(deadline)
        elif client is not None:
            # The direct process handle is ours, but missing identity proof cannot
            # be converted into positive descendant cleanup. Keep the hold.
            try:
                if client.proc.stdin and not client.proc.stdin.closed:
                    client.proc.stdin.close()
                if client.proc.poll() is None:
                    client.proc.send_signal(signal.SIGINT)
                    client.proc.wait(timeout=max(.001, min(3, deadline - time.monotonic())))
            except Exception:
                pass
            result['cleanup']['direct_exit'] = client.proc.poll()
        elif not launch_called:
            result['cleanup'] = {'verified': True, 'no_launch': True}
        probes_clean = (result.get('failed_probe_cleanup', {}).get('verified', not probe_inflight)
                        and all(probe.get('cleanup', {}).get('cleanup_verified', probe.get('cleanup', {}).get('verified')) is True
                                for probe in (result[key] for key in ('before_probe', 'after_probe') if key in result)))
        result['cleanup']['metadata_verified'] = result['cleanup'].get('verified') is True
        result['cleanup']['verified'] = result['cleanup']['metadata_verified'] and probes_clean
        result['passed'] = 'error' not in result and result['cleanup'].get('verified') is True
        save_phase(result)
    return result


def _native(recipe, checks, *, state, private_runtime, output, control, deadline, save_phase):
    """Reuse trusted native ownership in a private journal, with zero assignments."""
    if not checks:
        return {'checks': [], 'cleanup': {'verified': True}, 'model_turns': 0}
    if checkout_boundary.reviewed_host(recipe):
        raise Rejected('readiness_checks', 'reviewed-host-v1 readiness cannot execute project checks')
    private_runtime.mkdir(mode=0o700, exist_ok=False)
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=private_runtime)), {})
    result = {'checks': [], 'cleanup': {'verified': False}, 'model_turns': 0}
    try:
        scoped = _scoped(recipe, output, (state, control.parent, private_runtime.parent))
        scoped['_readiness_deadline_monotonic'] = deadline
        scoped['_adapter_bindings'] = {key: {**identity, 'binding': 'host-readiness'}
                                      for key, identity in adapter_identities().items()}
        frozen = _source(scoped, deadline)
        for check in checks:
            remaining = int(_remaining(deadline, reserve=_CLEANUP_SECONDS))
            scoped['max_seconds'] = remaining
            selected = {**check, 'timeout_seconds': min(check['timeout_seconds'], remaining)}
            save_phase({'phase': 'native_check', 'name': check['name']})
            completed = runtime._check(scoped, selected, output / check['name'], 'host-readiness', frozen)
            result['checks'].append({key: completed[key] for key in
                ('name', 'result', 'output', 'output_sha256', 'raw_outputs', 'native_check_id', 'scope') if key in completed})
            if result['checks'][-1].get('result') != 'pass':
                raise Rejected('readiness_native', 'Readiness check failed: ' + check['name'])
    except BaseException as error:
        result['error'] = {'code': getattr(error, 'code', 'readiness_native'), 'detail': str(error)[:2000]}
    finally:
        # NULL attempt IDs cannot be selected through _native_for_attempt(None).
        # This directory belongs solely to this exact readiness run.
        with runtime._db() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM native_check')]
            attempts = db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0]
        cleanup = []
        for check_id in ids:
            try:
                row = runtime._native_stop(check_id, cancel=bool(result.get('error')))
                _verify_native(row)
                cleanup.append({'check_id': check_id, 'verified': True, 'record_digest': digest(row)})
            except Exception as error:
                cleanup.append({'check_id': check_id, 'verified': False, 'error': str(error)[:2000]})
        result['cleanup'] = {'verified': attempts == 0 and all(row['verified'] for row in cleanup),
                             'checks': cleanup, 'private_model_attempts': attempts, 'journal': str(runtime.path)}
        # close() is housekeeping after explicit reconciliation, not its proof.
        runtime.close()
    result['passed'] = 'error' not in result and result['cleanup']['verified']
    return result



def _verify_native(row):
    if row.get('preparation_failure'):
        from .native_preparation import verify
        return verify(row)
    return CodexRuntime.verify_completed_native(row)


def _verify_cached_cleanup(receipt, runtime_root):
    if not receipt.get('cleanup_verified'):
        raise Rejected('readiness_cleanup_unknown', 'A readiness cache requires positive owned cleanup')
    if receipt.get('simulated'):
        return  # Explicitly simulated tests never become real readiness evidence.
    for role in receipt.get('roles', []):
        cleanup = role['cleanup']
        if not cleanup.get('verified'):
            raise Rejected('readiness_cleanup_unknown', 'Retained role cleanup is incomplete')
        observed = [] if cleanup.get('no_launch') else [cleanup['owner'], *cleanup.get('members', [])]
        for key in ('before_probe', 'after_probe'):
            probe = role.get(key, {}).get('cleanup', {})
            if probe.get('owner'):
                observed.extend([probe['owner'], *probe.get('observed', [])])
        failed = role.get('failed_probe_cleanup', {})
        if failed.get('receipt'):
            path = Path(failed['receipt'])
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != failed.get('sha256'):
                raise Rejected('readiness_evidence', 'Retained failed probe cleanup changed')
            if failed.get('owner'):
                observed.extend([failed['owner'], *failed.get('members', [])])
        for saved in observed:
            if native_processes.same(saved, native_processes.identity(saved['pid'])):
                raise Rejected('readiness_cleanup_unknown', 'An observed metadata owner is still present')
    native = receipt.get('native') or {}
    cleanup = native.get('cleanup', {})
    rows = cleanup.get('checks', [])
    if rows:
        path = Path(cleanup['journal'])
        if (path.is_symlink() or not path.is_file() or not path.is_relative_to(runtime_root)
                or any(p.is_symlink() for p in (path, *path.parents))):
            raise Rejected('readiness_evidence', 'Private native readiness journal is outside its exact owned root or linked')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            for saved in rows:
                row = db.execute('SELECT record FROM native_check WHERE id=?', (saved['check_id'],)).fetchone()
                if row is None or digest(json.loads(row[0])) != saved['record_digest']:
                    raise Rejected('readiness_evidence', 'Retained native readiness identity changed')
                _verify_native(json.loads(row[0]))
    for check in native.get('checks', []):
        CodexRuntime._verify_check_receipt(check)


def run_readiness(recipe: dict, *, state_dir: Path, readiness_dir: Path, model: str, effort: str,
                  max_seconds: int = 300, max_attempts: int = 2, native_checks=(),
                  transport_factory=_MetadataRPC, owner_factory=_MetadataOwner, role_probe=None,
                  native_runner=None) -> dict:
    """Return host facts; never approve/admit work or reserve product attempts.

    Dependencies are injectable only by trusted Python tests, not through project
    configuration. Such receipts are explicitly marked simulated. The same
    completed input returns its exact retained result after fresh validation;
    changed inputs may use the remaining finite technical allowance. Reviewed
    host validation uses metadata-only readiness; project checks are forbidden
    until the separately approved validation stage.
    """
    if (type(max_seconds) is not int or not 30 <= max_seconds <= 3600
            or type(max_attempts) is not int or not 1 <= max_attempts <= 10
            or not isinstance(model, str) or not model.strip()
            or not isinstance(effort, str) or not effort.strip()):
        raise Rejected('readiness_bounds', 'Explicit finite readiness time, attempts, model and effort required')
    if not isinstance(native_checks, (tuple, list)) or any(not isinstance(check, dict) for check in native_checks):
        raise Rejected('readiness_checks', 'Readiness checks must be explicit trusted native descriptors')
    metadata_only = checkout_boundary.reviewed_host(recipe)
    if metadata_only and native_checks:
        raise Rejected('readiness_checks', 'reviewed-host-v1 readiness is metadata-only; project checks require the approved validation stage')
    if metadata_only:
        interpreter = recipe.get('probe_interpreter')
        if (not isinstance(interpreter, str) or not Path(interpreter).is_absolute()
                or not Path(interpreter).is_file() or Path(interpreter).resolve() != Path(sys.executable).resolve()):
            raise Rejected('readiness_interpreter', 'Metadata-only readiness requires the exact worker Python interpreter')
    started = time.monotonic()
    deadline = started + max_seconds
    state = _canonical_directory(state_dir)
    directory = _canonical_directory(readiness_dir, existing=False)
    base = _normalise(recipe, state)
    roots = [Path(base['source']), Path(base['outputs']), *native_paths.writable_roots(base), state]
    if any(native_paths.overlaps(directory, root) for root in roots):
        raise Rejected('readiness_scope', 'Private readiness controls must be outside product state and writable roots')
    if len({check.get('name') for check in native_checks}) != len(native_checks):
        raise Rejected('readiness_checks', 'Readiness check names must be unique')
    for check in native_checks:
        if not isinstance(check.get('name'), str):
            raise Rejected('readiness_checks', 'Every readiness check needs a simple name')
        identifier(check['name'])
        if (type(check.get('timeout_seconds')) is not int or check['timeout_seconds'] <= 0
                or check['timeout_seconds'] > max_seconds or not isinstance(check.get('argv'), list)
                or not check['argv'] or not all(isinstance(part, str) and part for part in check['argv'])
                or not isinstance(check.get('scope'), str) or not check['scope'].strip()):
            raise Rejected('readiness_checks', 'Readiness requires bounded commands with explicit scope')
        adapter_for(check)
    checkout_boundary.validate_tree(base, deadline=deadline)
    baseline = _source(base, deadline)
    fingerprint = _runtime_identity()
    simulated = (transport_factory is not _MetadataRPC or owner_factory is not _MetadataOwner
                 or role_probe is not None or native_runner is not None)
    input_key = digest([base, baseline, fingerprint, list(native_checks), model, effort, simulated])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    control = directory / 'control'
    control.mkdir(mode=0o700, exist_ok=True)
    if control.is_symlink():
        raise Rejected('readiness_scope', 'Readiness controls must not be linked')
    fd = os.open(control / 'readiness.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('readiness_owned', 'Another host readiness check is active') from error
        journal_path = control / 'readiness.json'
        journal = _load(journal_path) if journal_path.exists() else {
            'format': FORMAT, 'state_dir': str(state), 'source': base['source'],
            'max_attempts': max_attempts, 'max_seconds': max_seconds, 'runs': []}
        if (journal.get('format') != FORMAT or journal.get('state_dir') != str(state)
                or journal.get('source') != base['source'] or journal.get('max_attempts') != max_attempts
                or journal.get('max_seconds') != max_seconds or not isinstance(journal.get('runs'), list)):
            raise Rejected('readiness_identity', 'Retain the original readiness scope and finite allowance')
        if any(prior.get('state') not in ('passed', 'failed') for prior in journal['runs']):
            raise Rejected('readiness_cleanup_unknown', 'Prior readiness ownership is unresolved; no automatic replay')
        for prior in journal['runs']:
            if prior.get('input_key') == input_key:
                path = Path(prior['receipt'])
                if (path.parent != control or path.is_symlink() or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != prior['sha256']):
                    raise Rejected('readiness_evidence', 'Retained readiness receipt is missing or changed')
                cached = _load(path)
                retained_native = cached.get('native')
                if metadata_only and (cached.get('readiness_scope') != 'model-metadata-only'
                        or cached.get('requested_native_checks') != []
                        or (retained_native is None and cached.get('ready') is True)
                        or (retained_native is not None and (not isinstance(retained_native, dict)
                            or retained_native.get('checks') != []
                            or retained_native.get('execution') != 'not_requested'))):
                    raise Rejected('readiness_evidence', 'Cached readiness does not prove the requested metadata-only scope')
                _verify_cached_cleanup(cached, directory / 'runtime')
                if 'resources_before' not in cached:
                    return {**cached, 'cached': True, 'revalidated_at': now()}
                resource_output = _canonical_directory(Path(cached['resource_output']))
                if resource_output != directory / 'outputs' / prior['id']:
                    raise Rejected('readiness_evidence', 'Cached resource output scope differs from its readiness run')
                current = _resources({**base, 'outputs': str(resource_output)}, deadline)
                product_current = _resources(base, deadline)
                cached = {**cached, 'cached': True, 'revalidated_at': now(), 'current_resources': current,
                          'current_product_resources': product_current}
                try:
                    _enforce_resources(base, cached['resources_before'], current)
                    _enforce_resources(base, cached['product_resources_before'], product_current)
                except Rejected as error:
                    cached.update(ready=False, status='held', blockers=[{'code': error.code, 'detail': str(error)}])
                _remaining(deadline)
                return cached
        if len(journal['runs']) >= max_attempts:
            raise Rejected('readiness_allowance', 'Finite readiness attempts exhausted; retained failures are not reset')
        run_id = 'readiness-' + str(len(journal['runs']) + 1)
        for parent in (directory / 'outputs', directory / 'runtime'):
            _canonical_directory(parent, existing=False)
        output = directory / 'outputs' / run_id
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        private_runtime = directory / 'runtime' / run_id
        private_runtime.parent.mkdir(mode=0o700, exist_ok=True)
        entry = {'id': run_id, 'input_key': input_key, 'state': 'running', 'started_at': now()}
        journal['runs'].append(entry)
        _save(journal_path, journal)
        receipt_path = control / (run_id + '.json')
        run_control = control / run_id
        run_control.mkdir(mode=0o700, exist_ok=False)
        receipt = {'format': FORMAT, 'id': run_id, 'ready': False, 'status': 'running',
                   'started_at': entry['started_at'], 'input_key': input_key, 'source_before': baseline,
                   'resource_output': str(output),
                   'runtime_identity': fingerprint, 'roles': [], 'native': None,
                   'model_turns': 0, 'product_attempts': 0, 'product_reservations': 0,
                   'simulated': simulated, 'blockers': [], 'cleanup_verified': False,
                   'requested_native_checks': copy.deepcopy(list(native_checks)),
                   'limits': {'max_seconds': max_seconds, 'max_attempts': max_attempts,
                              'claim': 'Endpoint resources and positively observed ownership; no transient-peak or detached-escape proof.'}}
        if metadata_only:
            receipt['readiness_scope'] = 'model-metadata-only'
        def save_phase(phase):
            receipt['phase'] = copy.deepcopy(phase)
            _save(receipt_path, receipt)
        try:
            receipt['product_resources_before'] = _resources(base, deadline)
            _enforce_resources(base, receipt['product_resources_before'], receipt['product_resources_before'])
            resource_recipe = {**base, 'outputs': str(output)}
            receipt['resources_before'] = _resources(resource_recipe, deadline)
            _enforce_resources(base, receipt['resources_before'], receipt['resources_before'])
            for role in ('implementation', 'assessment'):
                _remaining(deadline, reserve=5)
                role_output = output / role
                role_output.mkdir(mode=0o700)
                result = _role(base, role, state=state, output=role_output, control=run_control,
                               deadline=deadline, model=model, effort=effort, fingerprint=fingerprint,
                               save_phase=save_phase, transport_factory=transport_factory,
                               owner_factory=owner_factory, role_probe=role_probe or _probe)
                receipt['roles'].append(result)
                if not result['passed']:
                    error = result.get('error', {'code': 'readiness_cleanup_unknown', 'detail': 'Role cleanup is unverified'})
                    raise Rejected(error['code'], error['detail'])
            if metadata_only:
                # No native callback is dispatched, including an injected test
                # seam. Role probes and all endpoint checks remain unchanged.
                receipt['native'] = {'passed': True, 'checks': [], 'cleanup': {'verified': True},
                                     'model_turns': 0, 'execution': 'not_requested',
                                     'reason': 'Reviewed host project checks are excluded from readiness'}
            else:
                native_output = output / 'native'
                native_output.mkdir(mode=0o700)
                native = native_runner or _native
                receipt['native'] = native(base, list(native_checks), state=state, private_runtime=private_runtime,
                                           output=native_output, control=run_control, deadline=deadline, save_phase=save_phase)
            if receipt['native'].get('passed', not native_checks) is not True:
                error = receipt['native'].get('error', {'code': 'readiness_native', 'detail': 'Native readiness failed'})
                raise Rejected(error['code'], error['detail'])
            receipt['source_after'] = _source(base, deadline)
            if receipt['source_after'] != baseline:
                raise Rejected('readiness_source_drift', 'Source changed during technical readiness')
            base = _normalise(base, state)
            if _runtime_identity() != fingerprint:
                raise Rejected('readiness_runtime_drift', 'Runtime code or bundled CLI changed during readiness')
            receipt['resources_after'] = _resources(resource_recipe, deadline)
            receipt['product_resources_after'] = _resources(base, deadline)
            receipt['product_resource_check'] = _enforce_resources(
                base, receipt['product_resources_before'], receipt['product_resources_after'])
            receipt['resource_check'] = _enforce_resources(base, receipt['resources_before'], receipt['resources_after'])
            _remaining(deadline)
            receipt['ready'] = True
        except BaseException as error:
            receipt['blockers'].append({'code': getattr(error, 'code', 'readiness_failure'), 'detail': str(error)[:2000]})
        finally:
            roles_clean = all(row.get('cleanup', {}).get('verified') is True for row in receipt['roles'])
            phase = receipt.get('phase', {})
            # Failure before a role result is appended retains its latest exact
            # cleanup fact, or remains unknown after a launch intent.
            if isinstance(phase, dict) and phase.get('role') and phase not in receipt['roles']:
                roles_clean = roles_clean and phase.get('cleanup', {}).get('verified') is True
            native_clean = (phase.get('phase') != 'native_check' if receipt['native'] is None
                            else receipt['native'].get('cleanup', {}).get('verified') is True)
            receipt['cleanup_verified'] = roles_clean and native_clean
            receipt['ready'] = receipt['ready'] and receipt['cleanup_verified']
            receipt['status'] = ('passed' if receipt['ready'] else 'failed' if receipt['cleanup_verified'] else 'cleanup_unknown')
            receipt.update(finished_at=now(), elapsed_seconds=time.monotonic() - started,
                           prepared_recipe=base, recipe_digest=digest(base))
            entry.update(state=receipt['status'], input_key=digest([base, baseline, fingerprint, list(native_checks), model, effort, simulated]),
                         receipt=str(receipt_path))
            _save(receipt_path, receipt)
            entry['sha256'] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            _save(journal_path, journal)
        return receipt
