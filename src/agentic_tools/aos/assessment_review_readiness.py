"""One cumulative metadata-only retry after the assessment review-access repair."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from . import assessment_continuation as previous
from . import readiness
from .store import Rejected, canonical, digest, identifier, now

FORMAT = 'aos-assessment-review-readiness-extension-v1'
_CACHE_FIELDS = {'cached', 'revalidated_at', 'current_resources', 'current_product_resources'}
_FIELDS = {'format', 'operation_id', 'work_id', 'state_dir', 'readiness_dir', 'source',
           'readiness_scope', 'configured_max_attempts', 'original_max_attempts', 'max_attempts',
           'max_seconds', 'previous_extension', 'previous_packet_digest', 'original_journal',
           'original_journal_raw', 'original_journal_sha256', 'prior_receipts',
           'product_budget_changed', 'created_at'}


def _reject(message):
    raise Rejected('assessment_review_readiness', message)


def _id(value):
    if not isinstance(value, str):
        _reject('The exact continuation identifiers are required')
    return identifier(value)


def _reference(value):
    if (not isinstance(value, dict) or set(value) != {'path', 'sha256'}
            or not isinstance(value['path'], str) or not isinstance(value['sha256'], str)
            or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])):
        _reject('An exact immutable extension reference is required')
    return value


def _directories(config):
    directory = readiness._canonical_directory(Path(config['readiness_dir']))
    control = readiness._canonical_directory(directory / 'control')
    for path in (directory, control):
        status = path.stat()
        if status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o077:
            _reject('Readiness controls must remain private to their owner')
    readiness._canonical_directory(Path(config['state_dir']))
    readiness._canonical_directory(Path(config['recipe']['source']))
    if (config['readiness'].get('checks') != []
            or type(config['readiness'].get('max_attempts')) is not int
            or config['readiness']['max_attempts'] != 2
            or type(config['readiness'].get('max_seconds')) is not int
            or not 30 <= config['readiness']['max_seconds'] <= 3600):
        _reject('This extension requires the original two metadata-only attempts and unchanged time limit')
    return directory, control


def _journal(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 8 * 1024 * 1024):
            _reject('The readiness journal must remain an exact private regular file')
        raw = stream.read()
        after = path.stat()
    if any(getattr(before, k) != getattr(after, k) for k in
           ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
        _reject('Readiness history changed during readback')
    try:
        value = json.loads(raw)
        text = raw.decode('utf-8')
    except (ValueError, UnicodeError) as error:
        raise Rejected('assessment_review_readiness', 'Readiness history is unreadable') from error
    if not isinstance(value, dict):
        _reject('Readiness history must be an object')
    return value, text


def _archived(extension):
    original, raw = extension.get('original_journal'), extension.get('original_journal_raw')
    if not isinstance(original, dict) or not isinstance(raw, str):
        _reject('The original readiness journal and exact bytes must be retained')
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise Rejected('assessment_review_readiness', 'Archived readiness history is unreadable') from error
    if parsed != original or hashlib.sha256(raw.encode()).hexdigest() != extension.get('original_journal_sha256'):
        _reject('The original readiness journal byte identity changed')
    return original



def _receipts(journal, directory):
    # A truthy placeholder is not positive cleanup evidence for another retry.
    for index, run in enumerate(journal['runs'], 1):
        path = directory / 'control' / f'readiness-{index}.json'
        if not isinstance(run, dict) or run.get('receipt') != str(path):
            _reject('Prior readiness receipt paths changed')
        receipt = readiness._load(path)
        native = receipt.get('native') or {}
        roles = receipt.get('roles')
        if (receipt.get('cleanup_verified') is not True or not isinstance(roles, list)
                or any(role.get('cleanup', {}).get('verified') is not True for role in roles)
                or native.get('cleanup', {}).get('checks', [])):
            _reject('Only positively verified cleanup without native execution permits another attempt')
    return previous._prior_readiness_receipts(journal, directory)


def _saved_receipt(receipt, journal, limit, seconds):
    if not isinstance(receipt, dict):
        _reject('An exact successful metadata receipt is required')
    saved = readiness._load(Path(journal['runs'][-1]['receipt']))
    if (saved != {k: v for k, v in receipt.items() if k not in _CACHE_FIELDS}
            or saved.get('ready') is not True or saved.get('status') != 'passed'
            or type(saved.get('limits', {}).get('max_attempts')) is not int
            or saved['limits']['max_attempts'] != limit
            or type(saved.get('limits', {}).get('max_seconds')) is not int
            or saved['limits']['max_seconds'] != seconds):
        _reject('The readiness receipt differs from its retained successful run and limits')


def _previous(config, packet):
    """Verify the old immutable 2→3 chain without requiring the live header to be 3."""
    directory, control = _directories(config)
    metadata = packet.get('assessment_continuation', {})
    if (packet.get('project') != config or packet.get('ready') is not True
            or metadata.get('format') != previous.FORMAT):
        _reject('The exact previous assessment-continuation preparation is required')
    operation, work_id = _id(metadata.get('operation_id')), _id(metadata.get('work_id'))
    reference = _reference(metadata.get('readiness_extension'))
    old, checksum = previous._immutable_value(reference['path'])
    if (not isinstance(old, dict) or checksum != reference['sha256']
            or Path(reference['path']) != control / ('assessment-readiness-extension-' + operation + '.json')
            or old.get('format') != previous.FORMAT + '-readiness-extension'
            or old.get('operation_id') != operation or old.get('work_id') != work_id
            or old.get('recovery_operation_id') != metadata.get('recovery_operation_id')
            or old.get('recovery_audit_digest') != metadata.get('recovery_audit_digest')
            or old.get('state_dir') != config['state_dir'] or old.get('readiness_dir') != str(directory)
            or old.get('source') != config['recipe']['source']
            or type(old.get('original_max_attempts')) is not int or old['original_max_attempts'] != 2
            or type(old.get('max_attempts')) is not int or old['max_attempts'] != 3
            or type(old.get('max_seconds')) is not int or old['max_seconds'] != config['readiness']['max_seconds']
            or old.get('product_budget_changed') is not False):
        _reject('The previous immutable two-to-three extension changed')
    original = _archived(old)
    expected_keys = {'format', 'state_dir', 'source', 'max_attempts', 'max_seconds', 'runs'}
    if (set(original) != expected_keys or original.get('format') != readiness.FORMAT
            or original.get('state_dir') != config['state_dir'] or original.get('source') != config['recipe']['source']
            or original.get('max_attempts') != 2 or original.get('max_seconds') != old['max_seconds']
            or not isinstance(original.get('runs'), list) or len(original['runs']) != 2
            or _receipts(original, directory) != old.get('prior_receipts')):
        _reject('The previous original readiness history changed')
    return directory, control, metadata, reference, old


def _three_run_journal(journal, config, packet, prior):
    directory, _, _, reference, old = prior
    expected = {**old['original_journal'], 'max_attempts': 3, 'allowance_extension': reference}
    if (set(journal) != set(expected)
            or any(journal.get(k) != v for k, v in expected.items() if k != 'runs')
            or not isinstance(journal.get('runs'), list) or len(journal['runs']) != 3
            or journal['runs'][:2] != old['original_journal']['runs']):
        _reject('Extend only the exact exhausted three-run cumulative history once')
    references = _receipts(journal, directory)
    _saved_receipt(packet.get('readiness'), journal, 3, config['readiness']['max_seconds'])
    return references


def _record(extension, reference, config, packet, prior):
    directory, control, metadata, previous_reference, _ = prior
    operation = _id(extension.get('operation_id'))
    if (set(extension) != _FIELDS or extension.get('format') != FORMAT
            or Path(reference['path']) != control / ('assessment-review-readiness-extension-' + operation + '.json')
            or extension.get('work_id') != metadata['work_id']
            or extension.get('state_dir') != config['state_dir'] or extension.get('readiness_dir') != str(directory)
            or extension.get('source') != config['recipe']['source']
            or extension.get('readiness_scope') != 'model-metadata-only'
            or type(extension.get('configured_max_attempts')) is not int or extension['configured_max_attempts'] != 2
            or type(extension.get('original_max_attempts')) is not int or extension['original_max_attempts'] != 3
            or type(extension.get('max_attempts')) is not int or extension['max_attempts'] != 4
            or type(extension.get('max_seconds')) is not int or extension['max_seconds'] != config['readiness']['max_seconds']
            or extension.get('previous_extension') != previous_reference
            or extension.get('previous_packet_digest') != digest(packet)
            or extension.get('product_budget_changed') is not False
            or not isinstance(extension.get('created_at'), str) or not extension['created_at']):
        _reject('The exact one-attempt assessment-review readiness extension changed')
    original = _archived(extension)
    if _three_run_journal(original, config, packet, prior) != extension.get('prior_receipts'):
        _reject('The archived prior readiness receipts changed')
    return original


def validate(reference, config, previous_packet, *, allow_pending=False):
    """Check the complete immutable chain and current cumulative journal read-only."""
    _reference(reference)
    prior = _previous(config, previous_packet)
    extension, checksum = previous._immutable_value(reference['path'])
    if not isinstance(extension, dict) or checksum != reference['sha256']:
        _reject('The assessment-review extension byte identity changed')
    original = _record(extension, reference, config, previous_packet, prior)
    directory, control, _, _, _ = prior
    current, raw = _journal(control / 'readiness.json')
    if allow_pending and current == original:
        if raw != extension['original_journal_raw']:
            _reject('Pending readiness history bytes changed before replay')
        return extension
    expected = {**original, 'max_attempts': 4, 'allowance_extension': reference}
    if (set(current) != set(expected)
            or any(current.get(k) != v for k, v in expected.items() if k != 'runs')
            or not isinstance(current.get('runs'), list) or not 3 <= len(current['runs']) <= 4
            or current['runs'][:3] != original['runs']):
        _reject('Cumulative readiness history or the four-attempt ceiling changed')
    _receipts(current, directory)
    return extension


def extend(config, previous_packet, operation_id, work_id):
    """Retain one explicit 3→4 claim, preserving all original runs and limits."""
    _id(operation_id)
    _id(work_id)
    prior = _previous(config, previous_packet)
    directory, control, metadata, previous_reference, _ = prior
    if metadata['work_id'] != work_id:
        _reject('The readiness extension belongs to a different work item')
    journal_path = control / 'readiness.json'
    path = control / ('assessment-review-readiness-extension-' + operation_id + '.json')
    fd = os.open(control / 'readiness.lock', os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, 'a+') as lock:
        status = os.fstat(lock.fileno())
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600 or status.st_nlink != 1):
            _reject('Retain the exact private readiness owner lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('readiness_owned', 'Another readiness owner prevents allowance extension') from error
        journal, raw = _journal(journal_path)
        if path.exists() or path.is_symlink():
            extension, checksum = previous._immutable_value(path)
            if not isinstance(extension, dict) or extension.get('operation_id') != operation_id:
                _reject('Retained readiness extension belongs to a different operation')
            reference = {'path': str(path), 'sha256': checksum}
            validate(reference, config, previous_packet, allow_pending=True)
        else:
            references = _three_run_journal(journal, config, previous_packet, prior)
            extension = {'format': FORMAT, 'operation_id': operation_id, 'work_id': work_id,
                         'state_dir': config['state_dir'], 'readiness_dir': str(directory),
                         'source': config['recipe']['source'], 'readiness_scope': 'model-metadata-only',
                         'configured_max_attempts': 2, 'original_max_attempts': 3, 'max_attempts': 4,
                         'max_seconds': config['readiness']['max_seconds'], 'previous_extension': previous_reference,
                         'previous_packet_digest': digest(previous_packet), 'original_journal': journal,
                         'original_journal_raw': raw, 'original_journal_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                         'prior_receipts': references, 'product_budget_changed': False, 'created_at': now()}
            encoded = (canonical(extension) + '\n').encode()
            reference = {'path': str(path), 'sha256': hashlib.sha256(encoded).hexdigest()}
            _record(extension, reference, config, previous_packet, prior)
            out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(out, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(control, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            validate(reference, config, previous_packet, allow_pending=True)
        if journal == extension['original_journal']:
            readiness._save(journal_path, {**journal, 'max_attempts': 4, 'allowance_extension': reference})
        validate(reference, config, previous_packet)
        return reference


def validate_receipt(receipt, reference, config, previous_packet):
    """Bind the successful fourth metadata-only receipt to the exact retained run."""
    extension = validate(reference, config, previous_packet)
    journal, _ = _journal(Path(config['readiness_dir']) / 'control' / 'readiness.json')
    if len(journal['runs']) != 4:
        _reject('The additional readiness attempt has not produced its retained receipt')
    _saved_receipt(receipt, journal, extension['max_attempts'], extension['max_seconds'])
