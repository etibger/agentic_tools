"""Validate exact retained evidence for a host-reviewed, post-workload gap.

This is not a general resource-error override. A disposition never changes the
historical guard, blocked phase, deadline, or command result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import native_preparation, resource_limits
from .store import Rejected, digest


def _reject(message):
    raise Rejected('technical_quiescence', message)


def evidence(path, sha256, *, prelaunch=False):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_nlink != 1):
        _reject('The reviewed evidence must be an exact owned non-aliased file')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        _reject('The reviewed evidence hash changed')
    packet = json.loads(raw)
    expected = 'aos-phase-prelaunch-observation-forensics-v1' if prelaunch else 'aos-phase-gap-forensics-v1'
    if packet.get('format') != expected or packet.get('read_only') is not True:
        _reject('The exact sealed forensic packet is required')
    return packet


def _artifact(saved):
    path = Path(saved['path'])
    if saved.get('exists') is False:
        if path.exists() or path.is_symlink():
            _reject('A no-launch artifact appeared after review')
        return
    if path.is_symlink() or not path.exists():
        _reject('A reviewed native artifact disappeared or became an alias')
    info = path.stat()
    if any(saved[key] != value for key, value in
           (('device', info.st_dev), ('inode', info.st_ino), ('bytes', info.st_size), ('mtime', info.st_mtime))):
        _reject('A reviewed native artifact changed')
    if path.is_file() and saved.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
        _reject('A reviewed native artifact hash changed')


def validate(q, record, phase, guard, packet):
    return _validate(q, record, phase, guard, packet, prelaunch=False)


def validate_prelaunch(q, record, phase, guard, packet):
    return _validate(q, record, phase, guard, packet, prelaunch=True)


def _validate(q, record, phase, guard, packet, *, prelaunch):
    from .native_qualification import _acknowledged, _dispositions
    breach = guard['breach']
    if prelaunch:
        failures = [record['breach'], *record.get('unresolved_failures', [])]
        if (not breach or breach.get('reason') != 'observation_failed' or breach.get('error') != 'Rejected'
                or breach not in failures or any(failure and failure != breach and not _acknowledged(record, failure)
                                                 for failure in failures)
                or any(item['phase_id'] == phase['id'] for item in _dispositions(record))):
            _reject('Only the exact unacknowledged prelaunch observation failure can be reviewed')
        prior = [{'operation_id': item['operation_id'], 'phase_id': item['phase_id'],
                  'acknowledgment_digest': digest(item['acknowledgment']), 'resource_id': item['resource_id'],
                  'resource_receipt': item['resource_receipt']} for item in _dispositions(record)]
        with q.runtime._db() as db:
            raw_guard = db.execute('SELECT record FROM resource_limit WHERE work_id=?',
                                   (phase['resource_id'],)).fetchone()
        if (not raw_guard or packet.get('resource_guard_raw_sha256') != hashlib.sha256(raw_guard[0].encode()).hexdigest()
                or packet.get('resource_guard_digest') != digest(guard)
                or packet.get('guard_baseline_digest') != digest(guard['baseline'])
                or packet.get('guard_scope_digest') != guard['scope_digest']
                or packet.get('prior_dispositions') != prior
                or packet.get('prior_charged_growth_bytes') != record['charged_growth_bytes']):
            _reject('The reviewed original guard, baseline or prior accounting evidence changed')
    elif (not breach or breach.get('reason') != 'active_observation_gap' or record['breach'] != breach
          or record.get('unresolved_failures') or _dispositions(record)):
        _reject('Only the exact latest stopped observation gap can be acknowledged once')
    if (record.get('export') or record['active_phase'] is not None
            or record['phases'][-1]['id'] != phase['id'] or guard['active_attempt'] is not None):
        _reject('Only the exact latest stopped observation gap can be acknowledged once')
    if (packet.get('qualification_id') != q.qualification_id or packet.get('phase_id') != phase['id']
            or packet.get('source_baseline_digest') != digest(record['source_baseline'])
            or packet.get('phase') != {key: phase.get(key) for key in packet.get('phase', {})}
            or packet.get('phase_result') != {key: phase['receipt'].get(key) for key in packet.get('phase_result', {})}
            or packet.get('phase', {}).get('recipe_digest') != phase['recipe_digest']
            or packet.get('phase_result', {}).get('resource_limits') != phase['resource_receipt']):
        _reject('The reviewed packet is stale or belongs to another phase')
    if any(p['state'] != 'stopped' or p.get('receipt', {}).get('stopped') is not True
           or p['receipt'].get('source_unchanged') is not True or p['receipt'].get('product_budget_unchanged') is not True
           for p in record['phases']):
        _reject('Every historical phase must be positively stopped with unchanged protected baselines')
    if phase['receipt']['status'] != 'blocked' or phase['receipt'].get('model_calls') != 0:
        _reject('The historical blocked technical-only receipt must remain intact')
    for group, cap in (('output_roots', 'max_output_growth_bytes'),
                       ('source_evidence_roots', 'max_source_evidence_growth_bytes')):
        if guard['high_water_growth_bytes'][group] > guard['scope'][cap]:
            _reject('A retained byte high-water violation cannot be acknowledged as quiescence')
    if min(guard['last_observation']['available_bytes'].values(), default=0) < guard['scope']['min_free_bytes']:
        _reject('A retained free-space violation cannot be acknowledged as quiescence')
    _artifact(packet['phase_receipt'])
    historical = packet.get('historical_execution_artifacts', {})
    if not isinstance(historical, dict) or len(historical) < 2:
        _reject('Historical execution source and verification evidence is required')
    artifacts = []
    for name, saved in historical.items():
        raw = Path(name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != saved['sha256'] or json.loads(raw) != saved['artifact']:
            _reject('Historical execution source evidence changed')
        artifacts.append(saved['artifact'])
    required = ('src/agentic_tools/aos/native_processes.py', 'src/agentic_tools/aos/codex_runtime.py',
                'src/agentic_tools/aos/native_preparation.py')
    if (not any(all(isinstance(item.get('files', {}).get(name), str)
                    and len(item['files'][name]) == 64 for name in required) for item in artifacts)
            or not any(item.get('source_unchanged') is True and item.get('pytest', {}).get('exit_code') == 0
                       for item in artifacts)):
        _reject('Exact reviewed execution-source hashes and passing source-stable verification are required')
    source_digests = {digest({name.removeprefix('src/agentic_tools/'): value
                              for name, value in item['files'].items()
                              if name.startswith(('src/agentic_tools/aos/', 'src/agentic_tools/check_adapters/'))})
                      for item in artifacts if isinstance(item.get('files'), dict)}
    if not any(item.get('source_digest') in source_digests and item.get('source_unchanged') is True
               and item.get('pytest', {}).get('exit_code') == 0 for item in artifacts):
        _reject('The historical manifest and passing verification do not bind the same exact source')
    rows = q._rows(phase)
    reviewed = {item['id']: item for item in packet['native_rows']}
    if not rows or {row['check_id'] for row in rows} != set(reviewed):
        _reject('The full phase native-row inventory differs from the reviewed packet')
    final_samples, prepared = [], []
    with q.runtime._db() as db:
        if db.execute("SELECT 1 FROM native_check WHERE json_type(record,'$.phase') IS NOT 'text' "
                      "OR json_extract(record,'$.phase') IS NOT 'clean' LIMIT 1").fetchone():
            _reject('Unresolved native ownership remains in this private journal')
        for prior in record['phases']:
            previous = resource_limits._read(db, prior['resource_id'])
            if (not previous or previous['active_attempt'] is not None
                    or previous['breach'] and prior['id'] != phase['id']
                    and (not prelaunch or not _acknowledged(record, previous['breach'], prior['id']))):
                _reject('Another unresolved resource observation remains')
        for disposition in _dispositions(record):
            previous = resource_limits._read(db, disposition['resource_id'])
            if (not previous or previous['active_attempt'] is not None or previous['breach']
                    or disposition['resource_receipt']['breach']):
                _reject('An earlier accounting continuation has an unresolved observation')
        for row in rows:
            saved = reviewed[row['check_id']]
            raw = db.execute('SELECT record FROM native_check WHERE id=?', (row['check_id'],)).fetchone()[0]
            if hashlib.sha256(raw.encode()).hexdigest() != saved['raw_record_sha256']:
                _reject('A reviewed native row changed')
            if row.get('candidate') != phase['recipe_digest'] or row['phase'] != 'clean':
                _reject('Native evidence belongs to another candidate or has unresolved effects')
            if row['cleanup'].get('stopped') is not True or row['cleanup'].get('effects_reconciled') is not True:
                _reject('Native cleanup is not positively reconciled')
            for name in ('status', 'release', 'cancel', 'profile'):
                if saved.get(name):
                    _artifact(saved[name])
            if row.get('preparation_failure'):
                native_preparation.verify(row)
                proof = row['preparation_failure']['proof']
                if prelaunch and proof['probe']['state'] != 'not_started':
                    _reject('A permission probe ran; the narrow prelaunch observation disposition does not apply')
                if not prelaunch and proof['probe']['state'] != 'not_started':
                    profile = Path(proof['profile']['path'])
                    if (getattr(profile.stat(), 'st_birthtime', 0) <= breach['observed_at']
                            or proof['probe'].get('profile_sha256') != proof['profile']['sha256']):
                        _reject('The permission canary is not proven to follow the observed gap')
                prepared.append(row)
            else:
                if prelaunch:
                    _reject('A native workload was admitted; the prelaunch disposition does not apply')
                status = json.loads(Path(row['status']).read_text())
                census, sample = status.get('census', {}), status.get('resource_guard', {})
                if (census.get('boundary_verified') is not True or census.get('reconciled') is not True
                        or census.get('remaining') != [] or census.get('errors') != []
                        or sample.get('breach') is not None or not sample.get('observed_at')
                        or sample['observed_at'] >= breach['observed_at']
                        or sample.get('scope_digest') != guard['scope_digest']):
                    _reject('A workload is not proven stopped before a healthy final observation')
                q.runtime._verify_check_receipt(row['receipt'])
                final_samples.append((sample['observed_at'], row))
    if prelaunch:
        if len(prepared) != len(rows):
            _reject('Every native row must retain exact no-launch and no-probe evidence')
        for row in prepared:
            if (row.get('before_native_paths') != row['cleanup']['native_path_effects']['after']
                    or row['before_source'] != row['cleanup']['source_after']):
                _reject('Prelaunch source/native endpoint inventories changed')
        return {'kind': 'prelaunch-observation-failure', 'failure_observed_at': breach['observed_at'],
                'native_rows': {key: value['raw_record_sha256'] for key, value in reviewed.items()},
                'scope_digest': guard['scope_digest'], 'original_guard_digest': digest(guard),
                'historical_phase_digest': digest(phase),
                'limitations': 'No owned workload or permission probe launched; the original observation failure remains historical evidence'}
    if not final_samples or not prepared:
        _reject('Exact completed-workload and subsequent no-launch evidence is required')
    anchor_time, anchor = max(final_samples, key=lambda item: item[0])
    for row in prepared:
        if (anchor['cleanup']['native_path_effects']['after'] != row.get('before_native_paths')
                or row['before_native_paths'] != row['cleanup']['native_path_effects']['after']
                or anchor['cleanup']['source_after'] != row['before_source']
                or row['before_source'] != row['cleanup']['source_after']):
            _reject('Observed source/native endpoint inventories do not establish the reviewed interval')
    return {'healthy_final_observation': anchor_time, 'gap_observed_at': breach['observed_at'],
            'native_rows': {key: value['raw_record_sha256'] for key, value in reviewed.items()},
            'scope_digest': guard['scope_digest'], 'original_guard_digest': digest(guard),
            'historical_phase_digest': digest(phase),
            'limitations': 'No owned workload in the reviewed interval; this does not assert continuous global filesystem observation'}
