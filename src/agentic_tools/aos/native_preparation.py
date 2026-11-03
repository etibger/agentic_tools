"""Same-invocation proof for failure before a native supervisor is created.

An unfinished journal intent supplies no such proof. Only the live preparation
scope may retain this receipt, after the fixed isolated nofork probe is reaped.
"""
from __future__ import annotations

from contextlib import closing
import base64
import binascii
import hashlib
import copy
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import native_paths, native_renderer, native_sandbox, native_temporary
from .store import Rejected, canonical, digest, identifier, now


FORMAT = 'aos-native-preparation-failure-v1'
PROBE_DIAGNOSTICS_FORMAT = 'aos-native-permission-probe-diagnostics-v1'
PROBE_TAIL_BYTES = 4096
OBSERVERS = (['/usr/sbin/sysctl', '-n', 'net.inet.ip.portrange.first', 'net.inet.ip.portrange.last'],
             ['/usr/sbin/netstat', '-an', '-p', 'tcp'])


def _probe_capture(value):
    # Normal text=True results have already been decoded/newline-normalized by
    # subprocess. Their UTF-8 hash is not a claim about the original OS bytes.
    # TimeoutExpired can instead expose the partial bytes captured by stdlib.
    if value is None:
        return {'capture_type': 'unavailable', 'byte_count': None, 'sha256': None,
                'tail_base64': '', 'truncated': False}
    capture_type = 'text-utf8' if isinstance(value, str) else 'bytes'
    data = value.encode('utf-8') if isinstance(value, str) else value
    if not isinstance(data, bytes):
        raise TypeError('Unexpected permission-probe capture type')
    return {'capture_type': capture_type, 'byte_count': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'tail_base64': base64.b64encode(data[-PROBE_TAIL_BYTES:]).decode('ascii'),
            'truncated': len(data) > PROBE_TAIL_BYTES}


def _probe_diagnostics(stdout, stderr, *, partial):
    return {'format': PROBE_DIAGNOSTICS_FORMAT, 'partial': partial,
            'stdout': _probe_capture(stdout), 'stderr': _probe_capture(stderr)}


def _verify_probe_diagnostics(probe):
    # The optional field adds diagnostics, never ownership or success authority.
    # Historical receipts that predate it retain their original meaning.
    if 'diagnostics' not in probe:
        return
    details = probe['diagnostics']
    invalid = Rejected('check_cleanup_unknown', 'Retained permission-probe diagnostics are malformed')
    if (probe.get('state') != 'reaped' or not isinstance(details, dict)
            or set(details) != {'format', 'partial', 'stdout', 'stderr'}
            or details['format'] != PROBE_DIAGNOSTICS_FORMAT
            or type(details['partial']) is not bool
            or details['partial'] != (probe.get('outcome') == 'timeout')):
        raise invalid
    for key in ('stdout', 'stderr'):
        stream = details[key]
        if (not isinstance(stream, dict)
                or set(stream) != {'capture_type', 'byte_count', 'sha256', 'tail_base64', 'truncated'}
                or type(stream['truncated']) is not bool):
            raise invalid
        if stream['capture_type'] == 'unavailable':
            if stream != {'capture_type': 'unavailable', 'byte_count': None, 'sha256': None,
                          'tail_base64': '', 'truncated': False}:
                raise invalid
            continue
        if (stream['capture_type'] not in ('text-utf8', 'bytes')
                or type(stream['byte_count']) is not int or stream['byte_count'] < 0
                or not isinstance(stream['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', stream['sha256'])
                or not isinstance(stream['tail_base64'], str)
                or len(stream['tail_base64']) > 4 * ((PROBE_TAIL_BYTES + 2) // 3)):
            raise invalid
        try:
            tail = base64.b64decode(stream['tail_base64'], validate=True)
        except (ValueError, binascii.Error) as error:
            raise invalid from error
        if (base64.b64encode(tail).decode('ascii') != stream['tail_base64']
                or len(tail) != min(stream['byte_count'], PROBE_TAIL_BYTES)
                or stream['truncated'] != (stream['byte_count'] > PROBE_TAIL_BYTES)
                or (not stream['truncated'] and hashlib.sha256(tail).hexdigest() != stream['sha256'])):
            raise invalid


def _identity(row):
    keys = ('check_id', 'attempt_id', 'source', 'outputs_root', 'output', 'status', 'release',
            'cancel_path', 'command', 'candidate', 'adapter', 'native_path_contract', 'execution_deadline',
            'before_source', 'before_outputs', 'before_native_paths', 'ownership_backend')
    value = {k: row.get(k) for k in keys}
    if 'native_temporary_before' in row:
        value['native_temporary_before'] = row['native_temporary_before']
    return digest(value)


def _no_launch_evidence(row):
    if (any(k in row for k in ('owner', 'receipt', 'native_process_receipt', 'native_launch_disposition'))
            or row.get('members') != {}
            or any(Path(row[k]).exists() or Path(row[k]).is_symlink() for k in ('status', 'release', 'cancel_path') if row.get(k))):
        raise Rejected('check_cleanup_unknown', 'Native launch evidence contradicts preparation-only failure')


def _canary(path):
    if path.is_symlink():
        raise Rejected('check_cleanup_unknown', 'Permission-probe canary is an alias')
    try:
        info = path.stat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Rejected('check_cleanup_unknown', 'Permission-probe canary is not an exact regular file')
    return {'kind': 'file', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size}


def _profile(row):
    boundary = row.get('native_sandbox_boundary')
    if boundary:
        path = Path(boundary['path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != boundary['sha256']:
            raise Rejected('check_cleanup_unknown', 'Retained preparation profile changed')
    return boundary


def _verify_preexisting(row, cleanup, boundary):
    # A completed observation describes that preparation, not the later life of
    # OS-owned temporary files. Live failure reconciliation still compares the
    # actual filesystem before this digest-bound receipt can first be saved.
    effects = cleanup.get('native_temporary_effects')
    if (not isinstance(effects, dict) or effects.get('observed') is not True
            or effects.get('preexisting_preserved') is not True
            or not isinstance(effects.get('before'), dict) or not isinstance(effects.get('after'), dict)):
        raise Rejected('check_cleanup_unknown', 'Retained temporary preservation observation is incomplete')
    before, after = effects['before'], effects['after']
    fields = {'device', 'inode', 'uid', 'mode', 'size', 'mtime_ns', 'link'}
    if any(not isinstance(name, str) or not Path(name).is_absolute()
           or not isinstance(saved, dict) or set(saved) != fields
           or any(type(saved[k]) is not int for k in fields - {'link'})
           or (saved['link'] is not None and not isinstance(saved['link'], str))
           for snapshot in (before, after) for name, saved in snapshot.items()):
        raise Rejected('check_cleanup_unknown', 'Retained temporary snapshot metadata is malformed')
    if any(name not in after or after[name] != saved for name, saved in before.items()):
        raise Rejected('check_cleanup_unknown', 'Retained protected temporary observations disagree')
    if (('native_temporary_before' in row and row['native_temporary_before'] != before)
            or (boundary and (boundary.get('temporary_before') != before
                              or effects.get('profile_sha256') != boundary['sha256']))):
        raise Rejected('check_cleanup_unknown', 'Retained temporary observations differ from preparation inputs')


def _verify_renderer(renderer, row, before, boundary):
    if not isinstance(renderer, dict) or renderer.get('state') not in ('not_started', 'reaped'):
        raise Rejected('check_cleanup_unknown', 'Renderer launch/reaping remains uncertain')
    native_renderer.verify_receipt(renderer)
    if renderer['state'] == 'reaped' or renderer.get('input_sha256') is not None:
        if (renderer.get('recipe_contract_digest') != digest(row['native_path_contract'])
                or renderer.get('preexisting_digest') != digest(before)):
            raise Rejected('check_cleanup_unknown', 'Renderer inputs differ from the retained native preparation')
        _renderer_deadline(row, datetime.fromisoformat(renderer['execution_deadline']))
    if renderer['state'] == 'not_started':
        if boundary and boundary.get('renderer_receipt') is not None:
            raise Rejected('check_cleanup_unknown', 'A retained rendered profile contradicts the no-renderer witness')
        return
    if boundary:
        native_renderer.verify_receipt(renderer, require_success=True)
        if (boundary.get('renderer_receipt') != renderer or renderer.get('profile_sha256') != boundary['sha256']
                or renderer.get('network_observation_digest') != digest(boundary.get('network_observation'))):
            raise Rejected('check_cleanup_unknown', 'Retained profile differs from its renderer input/output receipt')


def _renderer_deadline(row, deadline):
    try:
        original = datetime.fromisoformat(row['execution_deadline'])
        if deadline.tzinfo is None or original.tzinfo is None or deadline > original:
            raise ValueError('deadline')
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise Rejected('check_cleanup_unknown', 'Renderer deadline exceeds or loses the original native deadline') from error


class Preparation:
    """Ephemeral witness; never deserialize this from a saved intent."""

    def __init__(self, row, recipe, directory):
        _no_launch_evidence(row)
        self.row, self.recipe = row, recipe
        self.stage = 'profile'
        self.probe = {'state': 'not_started'}
        self.renderer = {'format': native_renderer.FORMAT, 'state': 'not_started'}
        self.canary = Path(directory) / 'worker-forbidden-canary'
        self.before_canary = _canary(self.canary)
        self.before_temporary = native_sandbox.temporary_snapshot(recipe)
        if (native_sandbox.config(recipe) or {}).get('owned_temporary_roots'):
            row['native_temporary_before'] = copy.deepcopy(self.before_temporary)
        self.identity = _identity(row)

    def record_render(self, receipt):
        if (not isinstance(receipt, dict) or receipt.get('format') != native_renderer.FORMAT
                or receipt.get('state') not in ('not_started', 'launch_ambiguous', 'reaped')):
            raise Rejected('check_cleanup_unknown', 'Unrecognized renderer lifecycle observation')
        if ((self.renderer['state'] == 'launch_ambiguous' and receipt['state'] == 'not_started')
                or (self.renderer['state'] == 'reaped' and receipt['state'] != 'reaped')
                or (self.renderer.get('input_sha256') is not None
                    and self.renderer['input_sha256'] != receipt.get('input_sha256'))):
            raise Rejected('check_cleanup_unknown', 'Renderer lifecycle or exact input identity regressed')
        self.renderer = copy.deepcopy(receipt)

    def run_render(self, recipe, filesystem, *, preexisting, protected, network_observation,
                   deadline, expected_sources):
        if (self.renderer['state'] != 'not_started' or recipe != self.recipe
                or preexisting != self.before_temporary or self.stage != 'profile'):
            raise Rejected('check_cleanup_unknown', 'Renderer requires this live exact preparation scope')
        _renderer_deadline(self.row, deadline)
        return native_renderer.render(recipe, filesystem, preexisting=preexisting, protected=protected,
                                      network_observation=network_observation, deadline=deadline,
                                      expected_sources=expected_sources, observe=self.record_render)

    def run_probe(self, args, *, code, **kwargs):
        # sandbox-exec replaces itself with the same trusted CPython image. -I
        # and -S remove user/site startup hooks; the fixed stdlib script cannot
        # fork. The legacy Codex sandbox wrapper is deliberately unqualified.
        boundary = _profile(self.row)
        if (not boundary or args[:3] != ['/usr/bin/sandbox-exec', '-f', boundary['path']]
                or Path(args[3]).resolve() != Path(sys.executable).resolve()
                or args[4:8] != ['-I', '-S', '-c', code]
                or kwargs.get('cwd') != self.row['source']):
            self.probe = {'state': 'unqualified'}
            raise Rejected('native_preparation', 'Probe does not use the qualified fixed isolated interpreter route')
        self.probe = {'state': 'launch_ambiguous', 'argv_digest': digest(args),
                      'code_sha256': hashlib.sha256(code.encode()).hexdigest(),
                      'interpreter': str(Path(args[3]).resolve()),
                      'interpreter_sha256': hashlib.sha256(Path(args[3]).resolve().read_bytes()).hexdigest(),
                      'profile_sha256': boundary['sha256'], 'isolated': True, 'site_disabled': True,
                      'nofork': True}
        try:
            run = subprocess.run(args, **kwargs)
        except subprocess.TimeoutExpired as error:
            # stdlib run kills and waits for its exact child before propagating
            # TimeoutExpired. Any other launch/wait error remains ambiguous.
            self.probe.update(state='reaped', outcome='timeout', exit_code=None)
            self.probe['diagnostics'] = _probe_diagnostics(error.output, error.stderr, partial=True)
            raise
        self.probe.update(state='reaped', outcome='returned', exit_code=run.returncode)
        self.probe['diagnostics'] = _probe_diagnostics(run.stdout, run.stderr, partial=False)
        return run

    def failure(self, row, error, *, inventory, changed, reconcile_owned=None):
        causes, current = [], error
        while current is not None and id(current) not in {id(e) for e in causes}:
            causes.append(current)
            current = current.__cause__ or current.__context__
        if (any(getattr(e, 'code', '') == 'native_network_launch_unknown' for e in causes)
                or (any(getattr(e, 'code', '') == 'native_network' for e in causes)
                    and any(isinstance(e, OSError) or (isinstance(e, subprocess.SubprocessError)
                            and not isinstance(e, subprocess.TimeoutExpired)) for e in causes))):
            raise Rejected('check_cleanup_unknown', 'Native observer launch/wait was not positively reconciled') from error
        observers = []
        if any(getattr(e, 'code', '') == 'native_network' for e in causes):
            for cause in causes:
                if isinstance(cause, subprocess.TimeoutExpired):
                    if cause.cmd not in OBSERVERS:
                        raise Rejected('check_cleanup_unknown', 'Timed-out helper is not a qualified nofork observer') from error
                    observers.append({'argv': cause.cmd, 'state': 'reaped', 'outcome': 'timeout'})
        if (row is not self.row or _identity(row) != self.identity or row['phase'] != 'intent'
                or self.stage not in ('profile', 'permission_probe', 'deadline', 'supervisor_preparation')
                or self.probe['state'] not in ('not_started', 'reaped')
                or self.renderer['state'] not in ('not_started', 'reaped')):
            raise Rejected('check_cleanup_unknown', 'Preparation failure has no exact live no-launch proof')
        _no_launch_evidence(row)
        boundary = _profile(row)
        _verify_renderer(self.renderer, row, self.before_temporary, boundary)
        owned = None
        if row.get('owned_temporary') is not None:
            if reconcile_owned is None:
                raise Rejected('check_cleanup_unknown', 'Owned temporary roots require exact no-launch reconciliation')
            owned = native_temporary.verify_removed(self.recipe, row['check_id'], reconcile_owned())
            if row.get('owned_temporary') != owned:
                raise Rejected('check_cleanup_unknown', 'Owned temporary removal was not durably reflected in the check')
        source_after = inventory(Path(row['source']), recipe=self.recipe)
        outputs_after = inventory(Path(row['outputs_root']), allow_empty=True)
        native = native_paths.reconcile(self.recipe, row.get('before_native_paths', {}))
        temporary = native_sandbox.temporary_snapshot(self.recipe)
        if any(temporary.get(p) != value for p, value in self.before_temporary.items()):
            raise Rejected('check_cleanup_unknown', 'A protected preexisting temporary resource changed during preparation')
        proof = {'format': FORMAT, 'check_id': row['check_id'], 'identity': self.identity,
                 'stage': self.stage, 'supervisor_popen': 'not_called', 'probe': dict(self.probe),
                 'renderer': copy.deepcopy(self.renderer), 'profile': boundary,
                 'native_observer_timeouts': observers, 'owned_temporary': owned}
        cleanup = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
                   'partial_mutations': changed(row['before_source'], source_after),
                   'output_mutations': changed(row['before_outputs'], outputs_after),
                   'source_after': source_after, 'outputs_after': outputs_after,
                   'native_path_effects': native,
                   'native_temporary_effects': {'observed': True, 'attribution': 'filesystem observations; no deletion',
                       'before': self.before_temporary, 'after': temporary, 'preexisting_preserved': True},
                   'probe_canary_effects': {'path': str(self.canary), 'before': self.before_canary, 'after': _canary(self.canary)},
                   'owned_temporary': owned,
                   'native_admission': {'launched': False, 'reason': 'preparation_failed'}, 'observed_at': now()}
        if boundary:
            cleanup['native_temporary_effects']['profile_sha256'] = boundary['sha256']
        failure = {'proof': proof, 'error': type(error).__name__ + ': ' + str(error), 'effects': cleanup,
                   'receipt_digest': digest([proof, cleanup])}
        row.update(phase='clean', preparation_failure=failure, cleanup=cleanup)
        return row


def verify(row):
    """Replay observed failure, never turn a restarted intent into a receipt."""
    failure = row.get('preparation_failure', {})
    proof, cleanup = failure.get('proof', {}), row.get('cleanup', {})
    probe = proof.get('probe', {})
    if (row.get('phase') != 'clean' or proof.get('format') != FORMAT
            or proof.get('check_id') != row['check_id'] or proof.get('identity') != _identity(row)
            or proof.get('supervisor_popen') != 'not_called'
            or proof.get('stage') not in ('profile', 'permission_probe', 'deadline', 'supervisor_preparation')
            or probe.get('state') not in ('not_started', 'reaped')
            or cleanup.get('native_admission') != {'launched': False, 'reason': 'preparation_failed'}
            or cleanup.get('stopped') is not True or cleanup.get('owned_processes') != []
            or cleanup.get('effects_reconciled') is not True
            or failure.get('effects') != cleanup
            or failure.get('receipt_digest') != digest([proof, cleanup])):
        raise Rejected('check_cleanup_unknown', 'Retained preparation failure proof is missing or inconsistent')
    if probe['state'] == 'reaped' and (probe.get('isolated') is not True or probe.get('site_disabled') is not True
                                     or probe.get('nofork') is not True or probe.get('outcome') not in ('returned', 'timeout')):
        raise Rejected('check_cleanup_unknown', 'Retained probe has no qualified reaping proof')
    _verify_probe_diagnostics(probe)
    if (not isinstance(proof.get('native_observer_timeouts'), list) or any(
            observed != {'argv': observed.get('argv'), 'state': 'reaped', 'outcome': 'timeout'}
            or observed.get('argv') not in OBSERVERS for observed in proof['native_observer_timeouts'])):
        raise Rejected('check_cleanup_unknown', 'Retained native observer timeout proof is unqualified')
    _no_launch_evidence(row)
    owned = proof.get('owned_temporary')
    if owned != row.get('owned_temporary') or owned != cleanup.get('owned_temporary'):
        raise Rejected('check_cleanup_unknown', 'Owned temporary cleanup differs from its no-launch receipt')
    if owned is not None:
        native_temporary.verify_removed(row['native_path_contract'], row['check_id'], owned)
    boundary = _profile(row)
    if boundary != proof.get('profile'):
        raise Rejected('check_cleanup_unknown', 'Retained preparation profile differs from its no-launch receipt')
    _verify_preexisting(row, cleanup, boundary)
    renderer = proof.get('renderer', {'format': native_renderer.FORMAT, 'state': 'not_started'})
    _verify_renderer(renderer, row, cleanup['native_temporary_effects']['before'], proof.get('profile'))
    native_paths.verify_identities(row['native_path_contract'], cleanup['native_path_effects']['after'])
    return row


def restore_completed_failure(journal_path, request, *, apply=False):
    """Restore a reviewed completed receipt downgraded by historical replay.

    Trusted coordinator only. This cannot complete an intent, observe new
    effects, recover a native launch, or clear resource/budget/phase holds.
    Preview is read-only; application and its audit commit in one transaction.
    Repeating an operation returns its historical audit, not a new observation
    of the check's current state.
    """
    from . import native_journal

    keys = {'operation_id', 'check_id', 'expected_record_sha256', 'receipt_digest'}
    if (not isinstance(request, dict) or set(request) != keys or type(apply) is not bool
            or not all(isinstance(request[k], str) for k in keys)):
        raise Rejected('preparation_restore', 'Exact restoration identities and hashes required')
    for key in ('operation_id', 'check_id'):
        identifier(request[key])
    if (not isinstance(request['expected_record_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', request['expected_record_sha256'])
            or not isinstance(request['receipt_digest'], str)
            or not re.fullmatch('sha256:[0-9a-f]{64}', request['receipt_digest'])):
        raise Rejected('preparation_restore', 'Exact original record and receipt digests required')
    path = Path(journal_path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file():
        raise Rejected('preparation_restore', 'Existing canonical native journal required')
    fingerprint = digest(['restore-completed-native-preparation', request])

    def prior(db):
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_preparation_restoration'").fetchone()
        if not exists:
            return None
        saved = db.execute('SELECT fingerprint,record FROM native_preparation_restoration WHERE operation_id=?',
                           (request['operation_id'],)).fetchone()
        if saved:
            if saved[0] != fingerprint:
                raise Rejected('preparation_restore_conflict', 'Restoration operation was used with different inputs')
            return json.loads(saved[1])
        return None

    # Filesystem/profile validation can be slow; never hold the writer lock
    # while reading historic effects or inspecting their protected identities.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        native_journal.verify(db, allow_absent=True)
        if saved := prior(db):
            return saved
        found = db.execute('SELECT attempt_id,record FROM native_check WHERE id=?', (request['check_id'],)).fetchone()
    if not found:
        raise Rejected('preparation_restore', 'Exact native check is missing')
    attempt_id, raw = found
    if hashlib.sha256(raw.encode()).hexdigest() != request['expected_record_sha256']:
        raise Rejected('preparation_restore_conflict', 'Native check differs from the reviewed raw record')
    row = json.loads(raw)
    failure = row.get('preparation_failure', {})
    if (row.get('check_id') != request['check_id'] or row.get('attempt_id') != attempt_id
            or row.get('phase') != 'cleanup_unknown' or 'cleanup' in row
            or failure.get('receipt_digest') != request['receipt_digest']
            or not isinstance(failure.get('effects'), dict)):
        raise Rejected('preparation_restore', 'Only an exact downgraded completed preparation receipt can be restored')
    restored = copy.deepcopy(row)
    restored.update(phase='clean', cleanup=copy.deepcopy(failure['effects']))
    verify(restored)
    restored_raw = canonical(restored)
    audit = {'format': 'aos-native-preparation-restoration-v1', 'operation_id': request['operation_id'],
             'check_id': request['check_id'], 'attempt_id': attempt_id, 'request': dict(request),
             'prior_state': {'phase': row['phase'], 'cleanup_present': False},
             'prior_record_sha256': request['expected_record_sha256'],
             'restored_record_sha256': hashlib.sha256(restored_raw.encode()).hexdigest(),
             'original_receipt_digest': request['receipt_digest'],
             'changed_fields': ['phase', 'cleanup'], 'effects_reobserved': False,
             'budget_or_resource_changes': False}
    if not apply:
        return {**audit, 'applied': False}
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        native_journal.verify(db, allow_absent=True)
        if saved := prior(db):
            return saved
        current = db.execute('SELECT attempt_id,record FROM native_check WHERE id=?', (request['check_id'],)).fetchone()
        if current != (attempt_id, raw):
            raise Rejected('preparation_restore_conflict', 'Native check changed before restoration commit')
        db.execute('CREATE TABLE IF NOT EXISTS native_preparation_restoration '
                   '(operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, record TEXT NOT NULL)')
        audit.update(applied=True, recorded_at=now())
        updated = db.execute('UPDATE native_check SET record=? WHERE id=? AND record=?',
                             (restored_raw, request['check_id'], raw))
        if updated.rowcount != 1:
            raise Rejected('preparation_restore_conflict', 'Native check changed during restoration')
        db.execute('INSERT INTO native_preparation_restoration VALUES(?,?,?)',
                   (request['operation_id'], fingerprint, canonical(audit)))
    return audit
