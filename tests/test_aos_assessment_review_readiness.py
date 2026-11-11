"""Synthetic technical journals; these tests never run readiness or product work."""
import copy
import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import assessment_continuation as prior
from agentic_tools.aos import assessment_review_readiness as extension
from agentic_tools.aos import readiness
from agentic_tools.aos.store import Rejected, canonical, digest


def _checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_immutable(path, value):
    path.chmod(0o600)
    path.write_text(canonical(value) + '\n')
    path.chmod(0o400)


@pytest.fixture
def history(tmp_path):
    source, state, directory = (tmp_path / name for name in ('source', 'state', 'readiness'))
    for path in (source, state, directory):
        path.mkdir(mode=0o700)
    control = directory / 'control'
    control.mkdir(mode=0o700)
    (control / 'readiness.lock').touch(mode=0o600)
    config = {'state_dir': str(state), 'readiness_dir': str(directory), 'recipe': {'source': str(source)},
              'readiness': {'max_attempts': 2, 'max_seconds': 600, 'checks': []}}
    journal_path = control / 'readiness.json'
    readiness._save(journal_path, {'format': readiness.FORMAT, 'state_dir': str(state), 'source': str(source),
                                  'max_attempts': 2, 'max_seconds': 600, 'runs': []})

    def append(index, limit):
        receipt = {'format': readiness.FORMAT, 'id': f'readiness-{index}', 'status': 'passed', 'ready': True,
                   'simulated': False, 'readiness_scope': 'model-metadata-only', 'model_turns': 0,
                   'product_attempts': 0, 'product_reservations': 0, 'cleanup_verified': True,
                   'roles': [], 'requested_native_checks': [], 'native': None,
                   'limits': {'max_attempts': limit, 'max_seconds': 600,
                              'claim': 'Retained endpoint observations and positive owned cleanup.'}}
        path = control / f'readiness-{index}.json'
        readiness._save(path, receipt)
        journal = readiness._load(journal_path)
        journal['runs'].append({'id': receipt['id'], 'receipt': str(path), 'state': 'passed', 'sha256': _checksum(path)})
        readiness._save(journal_path, journal)
        return receipt

    append(1, 2)
    append(2, 2)
    audit = {'work_id': 'FIXTURE', 'operation_id': 'recovery'}
    old_reference = prior._extend_readiness(config, audit, 'previous-continuation')
    third = append(3, 3)
    previous_packet = {'ready': True, 'project': copy.deepcopy(config), 'readiness': third,
                       'assessment_continuation': {'format': prior.FORMAT, 'operation_id': 'previous-continuation',
                           'work_id': 'FIXTURE', 'recovery_operation_id': 'recovery',
                           'recovery_audit_digest': digest(audit), 'readiness_extension': old_reference}}
    p = SimpleNamespace(config=config, packet=previous_packet, control=control, journal=journal_path,
                        old_reference=old_reference, append=append)
    p.extend = lambda operation='review-continuation': extension.extend(config, previous_packet, operation, 'FIXTURE')
    return p


def test_one_cumulative_extension_preserves_exact_history_and_is_idempotent(history):
    p = history
    original = p.journal.read_bytes()
    old_bytes = Path(p.old_reference['path']).read_bytes()
    before_receipts = [path.read_bytes() for path in sorted(p.control.glob('readiness-*.json'))]
    reference = p.extend()
    value = extension.validate(reference, p.config, p.packet)
    current = readiness._load(p.journal)
    assert current['max_attempts'] == 4 and current['max_seconds'] == 600
    assert current['runs'] == json.loads(original)['runs']
    assert value['original_journal_raw'].encode() == original
    assert value['original_journal_sha256'] == hashlib.sha256(original).hexdigest()
    assert value['previous_extension'] == p.old_reference
    assert value['previous_packet_digest'] == digest(p.packet)
    assert value['product_budget_changed'] is False
    assert value['readiness_scope'] == 'model-metadata-only'
    assert Path(p.old_reference['path']).read_bytes() == old_bytes
    assert [path.read_bytes() for path in sorted(p.control.glob('readiness-*.json'))] == before_receipts
    expanded = p.journal.read_bytes()
    assert p.extend() == reference
    assert p.journal.read_bytes() == expanded
    with pytest.raises(Rejected, match='exhausted three-run'):
        p.extend('another-extension')
    assert not (p.control / 'assessment-review-readiness-extension-another-extension.json').exists()
    assert p.journal.read_bytes() == expanded


def test_fourth_receipt_must_be_retained_successful_and_exact(history):
    p = history
    reference = p.extend()
    with pytest.raises(Rejected, match='has not produced'):
        extension.validate_receipt({}, reference, p.config, p.packet)
    fourth = p.append(4, 4)
    extension.validate_receipt(fourth, reference, p.config, p.packet)
    extension.validate_receipt({**fourth, 'cached': True, 'current_resources': {}}, reference, p.config, p.packet)
    assert p.extend() == reference
    with pytest.raises(Rejected, match='retained successful'):
        extension.validate_receipt({**fourth, 'ready': False}, reference, p.config, p.packet)
    with pytest.raises(Rejected, match='retained successful'):
        extension.validate_receipt({**fourth, 'limits': {'max_attempts': 5, 'max_seconds': 600}}, reference, p.config, p.packet)


def test_crash_after_immutable_claim_can_complete_only_same_extension(history):
    p = history
    reference = p.extend()
    value = extension.validate(reference, p.config, p.packet)
    p.journal.write_text(value['original_journal_raw'])
    assert extension.validate(reference, p.config, p.packet, allow_pending=True) == value
    with pytest.raises(Rejected, match='four-attempt ceiling'):
        extension.validate(reference, p.config, p.packet)
    assert p.extend() == reference
    assert readiness._load(p.journal)['max_attempts'] == 4


@pytest.mark.parametrize('field,value', [
    ('format', 'other'), ('operation_id', 'other'), ('work_id', 'other'),
    ('state_dir', '/other'), ('source', '/other'), ('readiness_scope', 'native'),
    ('configured_max_attempts', 3), ('original_max_attempts', 2), ('max_attempts', 5),
    ('max_seconds', 601), ('product_budget_changed', True), ('previous_extension', None),
    ('previous_packet_digest', 'sha256:' + '0' * 64), ('original_journal_sha256', '0' * 64),
    ('prior_receipts', []),
])
def test_invalid_crash_replay_preserves_journal_bytes(history, field, value):
    p = history
    reference = p.extend()
    path = Path(reference['path'])
    saved = json.loads(path.read_text())
    p.journal.write_text(saved['original_journal_raw'])
    before = p.journal.read_bytes()
    saved[field] = value
    _rewrite_immutable(path, saved)
    with pytest.raises(Rejected):
        p.extend()
    assert p.journal.read_bytes() == before


def test_wrong_operation_in_already_expanded_receipt_is_rejected_without_write(history):
    p = history
    reference = p.extend()
    path = Path(reference['path'])
    saved = json.loads(path.read_text())
    saved['operation_id'] = 'wrong-operation'
    _rewrite_immutable(path, saved)
    before = p.journal.read_bytes()
    with pytest.raises(Rejected, match='different operation'):
        p.extend()
    assert p.journal.read_bytes() == before


def test_pending_replay_requires_original_journal_bytes_not_only_equal_json(history):
    p = history
    reference = p.extend()
    saved = extension.validate(reference, p.config, p.packet)
    p.journal.write_text(json.dumps(saved['original_journal'], indent=2))
    before = p.journal.read_bytes()
    with pytest.raises(Rejected, match='history bytes changed'):
        p.extend()
    assert p.journal.read_bytes() == before


@pytest.mark.parametrize('change', ['running', 'unknown-cleanup', 'truthy-cleanup', 'native', 'native-cleanup', 'product-turn', 'history-hash'])
def test_unproved_third_run_cannot_increase_allowance(history, change):
    p = history
    journal = readiness._load(p.journal)
    path = p.control / 'readiness-3.json'
    receipt = readiness._load(path)
    if change == 'running':
        journal['runs'][2]['state'] = 'running'
    elif change == 'history-hash':
        journal['runs'][1]['sha256'] = '0' * 64
    else:
        if change == 'unknown-cleanup':
            receipt['cleanup_verified'] = False
        elif change == 'truthy-cleanup':
            receipt['cleanup_verified'] = 'unproved'
        elif change == 'native-cleanup':
            receipt['native'] = {'checks': [], 'execution': 'not_requested', 'cleanup': {'checks': [{}]}}
        elif change == 'native':
            receipt['requested_native_checks'] = [{'name': 'unapproved'}]
        else:
            receipt['model_turns'] = 1
        readiness._save(path, receipt)
        journal['runs'][2]['sha256'] = _checksum(path)
        p.packet['readiness'] = receipt
    readiness._save(p.journal, journal)
    before = p.journal.read_bytes()
    with pytest.raises(Rejected):
        p.extend()
    assert p.journal.read_bytes() == before
    assert not (p.control / 'assessment-review-readiness-extension-review-continuation.json').exists()


def test_previous_immutable_extension_and_packet_are_bound_after_fourth_run(history):
    p = history
    reference = p.extend()
    p.append(4, 4)
    changed = copy.deepcopy(p.packet)
    changed['other'] = True
    with pytest.raises(Rejected, match='extension changed'):
        extension.validate(reference, p.config, changed)
    path = Path(p.old_reference['path'])
    value = json.loads(path.read_text())
    value['original_journal_sha256'] = '0' * 64
    _rewrite_immutable(path, value)
    with pytest.raises(Rejected, match='previous immutable'):
        extension.validate(reference, p.config, p.packet)


def test_changed_fourth_history_or_cleanup_is_rejected(history):
    p = history
    reference = p.extend()
    fourth = p.append(4, 4)
    fourth['cleanup_verified'] = False
    path = p.control / 'readiness-4.json'
    readiness._save(path, fourth)
    journal = readiness._load(p.journal)
    journal['runs'][3]['sha256'] = _checksum(path)
    readiness._save(p.journal, journal)
    with pytest.raises(Rejected, match='cleanup'):
        extension.validate_receipt(fourth, reference, p.config, p.packet)


def test_existing_readiness_owner_prevents_extension(history):
    p = history
    before = p.journal.read_bytes()
    with (p.control / 'readiness.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Rejected, match='Another readiness owner'):
            p.extend()
    assert p.journal.read_bytes() == before


@pytest.mark.parametrize('invalid', ['../escape', '', 'has/slash'])
def test_invalid_operation_does_not_change_journal(history, invalid):
    p = history
    before = p.journal.read_bytes()
    with pytest.raises(Rejected):
        p.extend(invalid)
    assert p.journal.read_bytes() == before
