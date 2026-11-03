"""Restore only exact previously completed cleanup evidence in a passive journal."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_cleanup_recovery as recovery
from agentic_tools.aos import native_journal, technical_control
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical, digest


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _connect(path):
    return sqlite3.connect(path)


def _rows(path):
    with _connect(path) as db:
        return dict(db.execute('SELECT id,record FROM native_check ORDER BY id'))


def _snapshot(path):
    with _connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {table: db.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid').fetchall()
                for table in tables}


def _update_row(path, key, change):
    with _connect(path) as db:
        row = json.loads(db.execute('SELECT record FROM native_check WHERE id=?', (key,)).fetchone()[0])
        change(row)
        db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(row), key))
    return row


def _repin(case):
    case.request['prior_journal_sha256'] = _sha(case.prior.read_bytes())
    current, prior = _rows(case.journal), _rows(case.prior)
    for item in case.request['checks']:
        item['expected_record_sha256'] = _sha(current[item['check_id']].encode())
        item['prior_record_sha256'] = _sha(prior[item['check_id']].encode())
        item['receipt_digest'] = digest(json.loads(prior[item['check_id']]).get('receipt'))


def _restore(case, *, apply=False):
    return recovery.restore_completed_cleanup(case.journal, case.request, apply=apply)


@pytest.fixture
def cleanup_case(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / 'private-journal'
    directory.mkdir(mode=0o700)
    journal = directory / 'runtime.sqlite3'
    prior = directory / 'retained-prior.sqlite3'
    source, outputs = (tmp_path.resolve() / name for name in ('source', 'outputs'))
    source.mkdir()
    outputs.mkdir()
    recipe = {'source': str(source), 'outputs': str(outputs), 'checks': [], 'max_seconds': 1800}
    phase = {'id': 'historical', 'input_digest': digest(recipe), 'recipe_digest': digest(recipe),
             'recipe': recipe, 'started_at': 100.0, 'max_seconds': 1800, 'deadline': 1900.0,
             'started_monotonic': 100.0, 'deadline_monotonic': 1900.0, 'resource_id': 'original-resources',
             'output': str(outputs / 'technical-historical'), 'state': 'stopped'}
    archive = {'active_phase': None, 'phases': [phase], 'charged_growth_bytes': {'output_roots': 999},
               'source_baseline': {'preserved': 'source'}, 'budget_before': {'preserved': 'budget'},
               'breach': {'reason': 'active_observation_gap', 'observed_at': 300.0}}
    guard = {'active_attempt': None, 'breach': {'reason': 'active_observation_gap', 'observed_at': 300.0},
             'baseline': {'preserved': 'baseline'}, 'high_water_growth_bytes': {'output_roots': 999}}
    originals = {}
    with _connect(journal) as db:
        db.execute('CREATE TABLE native_check (id TEXT PRIMARY KEY, attempt_id TEXT, record TEXT NOT NULL)')
        db.execute('CREATE TABLE resource_limit (work_id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        db.execute('CREATE TABLE technical_qualification (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        db.execute('CREATE TABLE budget_sentinel (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        technical_control.initialize(db)
        assert native_journal.initialize(db)
        db.execute('INSERT INTO technical_qualification VALUES(?,?)', ('qualification', canonical(archive)))
        technical_control.save(db, 'qualification', phase)
        db.execute('INSERT INTO resource_limit VALUES(?,?)', (phase['resource_id'], canonical(guard)))
        db.execute('INSERT INTO budget_sentinel VALUES(?,?)', ('budget', canonical({'charges': 17, 'held': True})))
        for index, key in enumerate(('nc-completed-one', 'nc-completed-two', 'nc-unrelated-clean')):
            output = outputs / key
            output.mkdir()
            transcript, stdout = output / 'output.txt', output / 'stdout.txt'
            transcript.write_bytes(b'fixture complete\n')
            stdout.write_bytes(b'fixture output\n')
            process_receipt = {'boundary_verified': True, 'reconciled': True, 'remaining': []}
            cleanup = {'stopped': True, 'effects_reconciled': True, 'owned_processes': [],
                       'process_ownership': process_receipt,
                       'source_after': {'input': 'unchanged'}, 'native_path_effects': {'after': {'cache': index}}}
            row = {'check_id': key, 'attempt_id': None, 'phase': 'clean',
                   'ownership_backend': 'sandbox-census-v1', 'output': str(outputs / key),
                   'native_sandbox_boundary': {'path': str(directory / (key + '.sb')), 'sha256': 'c' * 64},
                   'native_process_receipt': copy.deepcopy(process_receipt),
                   'source': str(source), 'command': ['/fixture/check', str(index)],
                   'candidate': 'sha256:' + 'a' * 64, 'cleanup': cleanup,
                   'receipt': {'native_check_id': key, 'result': 'pass', 'cleanup': copy.deepcopy(cleanup),
                               'output_sha256': _sha(transcript.read_bytes()), 'output': str(transcript),
                               'raw_outputs': {str(stdout): _sha(stdout.read_bytes())}}}
            if index == 0:
                row['cancelled'] = False
            originals[key] = copy.deepcopy(row)
            db.execute('INSERT INTO native_check VALUES(?,?,?)', (key, None, canonical(row)))
    shutil.copyfile(journal, prior)
    journal.chmod(0o600)
    prior.chmod(0o600)
    for key in ('nc-completed-one', 'nc-completed-two'):
        def downgrade(row):
            row['phase'] = 'cleanup_unknown'
            row.pop('cleanup')
        _update_row(journal, key, downgrade)
    request = {'operation_id': 'restore-historical-cleanup', 'prior_journal_path': str(prior),
               'prior_journal_sha256': '', 'checks': [
                   {'check_id': key, 'expected_record_sha256': '', 'prior_record_sha256': '',
                    'receipt_digest': ''} for key in ('nc-completed-one', 'nc-completed-two')]}
    verified = []

    def verify(row):
        verified.append(copy.deepcopy(row))
        return copy.deepcopy(row)

    monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(verify), raising=False)
    case = SimpleNamespace(directory=directory, journal=journal, prior=prior, request=request,
                           originals=originals, verified=verified, archive=archive, guard=guard,
                           phase=phase, source=source, outputs=outputs)
    _repin(case)
    return case


def test_preview_is_read_only_and_apply_restores_only_the_original_fields(cleanup_case):
    c = cleanup_case
    before = _snapshot(c.journal)
    prior_bytes = c.prior.read_bytes()
    preview = _restore(c)
    assert preview['format'] == 'aos-native-cleanup-restoration-v1'
    assert preview['applied'] is False
    assert _snapshot(c.journal) == before
    assert c.prior.read_bytes() == prior_bytes
    result = _restore(c, apply=True)
    assert result['applied'] is True
    assert result['operation_id'] == c.request['operation_id']
    assert result['request'] == c.request
    assert result['changed_fields'] == ['phase', 'cleanup']
    assert result['effects_reobserved'] is False and result['budget_or_resource_changes'] is False
    assert result['grants_work'] is False and result['grants_time'] is False
    assert result['model_budget_reservations'] == 0
    assert {item['check_id'] for item in result['checks']} == {item['check_id'] for item in c.request['checks']}
    for checked, requested in zip(result['checks'], c.request['checks']):
        assert checked['prior_record_sha256'] == requested['expected_record_sha256']
        assert checked['restored_record_sha256'] == requested['prior_record_sha256']
        assert checked['original_receipt_digest'] == requested['receipt_digest']
    assert {key: json.loads(raw) for key, raw in _rows(c.journal).items()} == c.originals
    after = _snapshot(c.journal)
    for table in ('resource_limit', 'technical_qualification', 'technical_archive_revision',
                  'technical_phase_control', 'budget_sentinel'):
        assert after[table] == before[table]
    assert after['native_check_revision'][0][1] == before['native_check_revision'][0][1] + 2
    assert c.prior.read_bytes() == prior_bytes
    with _connect(c.journal) as db:
        assert native_journal.verify(db)
        assert all(phase == 'clean' for _, _, phase in native_journal.identities(db))
    assert all(row['phase'] == 'clean' and row['cleanup'] == row['receipt']['cleanup'] for row in c.verified)


def test_exact_retry_returns_saved_audit_without_changing_any_record(cleanup_case):
    c = cleanup_case
    applied = _restore(c, apply=True)
    before, calls = _snapshot(c.journal), len(c.verified)
    assert _restore(c, apply=True) == applied
    assert _snapshot(c.journal) == before
    assert len(c.verified) == calls
    c.request['checks'][0]['receipt_digest'] = 'sha256:' + 'f' * 64
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _snapshot(c.journal) == before


@pytest.mark.parametrize('damage', ['empty', 'duplicate', 'omitted', 'clean-selected', 'unknown-selected',
                                   'oversized', 'extra-field', 'operation', 'current-hash', 'prior-hash', 'receipt-hash'])
def test_batch_request_must_exactly_select_every_unknown_row(cleanup_case, damage):
    c = cleanup_case
    if damage == 'empty':
        c.request['checks'] = []
    elif damage == 'duplicate':
        c.request['checks'].append(copy.deepcopy(c.request['checks'][0]))
    elif damage == 'omitted':
        c.request['checks'].pop()
    elif damage in ('clean-selected', 'unknown-selected'):
        extra = copy.deepcopy(c.request['checks'][0])
        extra['check_id'] = 'nc-unrelated-clean' if damage == 'clean-selected' else 'nc-nonexistent'
        c.request['checks'].append(extra)
    elif damage == 'oversized':
        c.request['checks'] *= 33
    elif damage == 'extra-field':
        c.request['ignore_unknown'] = True
    elif damage == 'operation':
        c.request['operation_id'] = 'invalid operation'
    else:
        key = {'current-hash': 'expected_record_sha256', 'prior-hash': 'prior_record_sha256',
               'receipt-hash': 'receipt_digest'}[damage]
        c.request['checks'][0][key] = '0' * 64
    before = _snapshot(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == dict((row[0], row[2]) for row in before['native_check'])
    assert _snapshot(c.journal).get('budget_sentinel') == before['budget_sentinel']


@pytest.mark.parametrize('damage', ['cancelled', 'command', 'candidate', 'receipt', 'owner', 'cleanup-present',
                                   'phase-running', 'phase-intent', 'attempt'])
def test_any_unexplained_current_row_change_rejects_the_whole_batch(cleanup_case, damage):
    c = cleanup_case
    key = c.request['checks'][1]['check_id']
    def change(row):
        if damage == 'cancelled':
            row['cancelled'] = True
        elif damage == 'command':
            row['command'].append('changed')
        elif damage == 'candidate':
            row['candidate'] = 'sha256:' + 'c' * 64
        elif damage == 'receipt':
            row['receipt']['result'] = 'fail'
        elif damage == 'owner':
            row['owner'] = {'pid': 99999, 'uid': os.getuid(), 'birth': [10, 1]}
        elif damage == 'cleanup-present':
            row['cleanup'] = copy.deepcopy(row['receipt']['cleanup'])
        elif damage in ('phase-running', 'phase-intent'):
            row['phase'] = damage.removeprefix('phase-')
        else:
            row['attempt_id'] = 'foreign-attempt'
    _update_row(c.journal, key, change)
    _repin(c)
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


@pytest.mark.parametrize('damage', ['not-clean', 'missing-cleanup', 'receipt-cleanup', 'missing-receipt', 'preparation'])
def test_prior_rows_must_contain_complete_matching_workload_cleanup(cleanup_case, damage):
    c = cleanup_case
    key = c.request['checks'][1]['check_id']
    def change(row):
        if damage == 'not-clean':
            row['phase'] = 'cleanup_unknown'
        elif damage == 'missing-cleanup':
            row.pop('cleanup')
        elif damage == 'receipt-cleanup':
            row['receipt']['cleanup']['stopped'] = False
        elif damage == 'missing-receipt':
            row.pop('receipt')
        else:
            row['preparation_failure'] = {'proof': {'supervisor_popen': 'not_called'}}
    _update_row(c.prior, key, change)
    if damage in ('receipt-cleanup', 'missing-receipt', 'preparation'):
        _update_row(c.journal, key, change)
    _repin(c)
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


@pytest.mark.parametrize('damage', ['boundary', 'process-proof', 'backend', 'launch-disposition'])
def test_only_completed_dedicated_native_workload_receipts_are_restorable(cleanup_case, damage):
    c = cleanup_case
    key = c.request['checks'][0]['check_id']
    def change(row):
        if damage == 'boundary':
            row.pop('native_sandbox_boundary')
        elif damage == 'process-proof':
            row.pop('native_process_receipt')
        elif damage == 'backend':
            row['ownership_backend'] = 'legacy-process-group'
        else:
            row['native_launch_disposition'] = {'launched': False}
    _update_row(c.prior, key, change)
    _update_row(c.journal, key, change)
    _repin(c)
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


@pytest.mark.parametrize('damage', ['receipt-null', 'receipt-list', 'cleanup-null', 'cleanup-list'])
def test_malformed_embedded_receipts_reject_without_partial_updates(cleanup_case, damage):
    c = cleanup_case
    key = c.request['checks'][1]['check_id']
    def change(row):
        value = None if damage.endswith('null') else []
        if damage.startswith('receipt'):
            row['receipt'] = value
        else:
            row['receipt']['cleanup'] = value
    _update_row(c.prior, key, change)
    _update_row(c.journal, key, change)
    _repin(c)
    before = _snapshot(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _snapshot(c.journal) == before


@pytest.mark.parametrize('artifact', ['transcript', 'stdout'])
def test_changed_retained_output_blocks_the_entire_restoration(cleanup_case, artifact):
    c = cleanup_case
    receipt = c.originals['nc-completed-two']['receipt']
    path = Path(receipt['output'] if artifact == 'transcript' else next(iter(receipt['raw_outputs'])))
    path.write_text('changed retained evidence')
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


@pytest.mark.parametrize('damage', ['backup-hash', 'backup-alias', 'backup-hardlink', 'backup-wal',
                                   'same-database', 'missing-index', 'changed-index'])
def test_only_the_exact_closed_backup_and_verified_live_index_are_accepted(cleanup_case, damage):
    c = cleanup_case
    if damage == 'backup-hash':
        c.request['prior_journal_sha256'] = '0' * 64
    elif damage == 'backup-alias':
        alias = c.directory / 'backup-alias.sqlite3'
        alias.symlink_to(c.prior)
        c.request['prior_journal_path'] = str(alias)
    elif damage == 'backup-hardlink':
        os.link(c.prior, c.directory / 'backup-link.sqlite3')
    elif damage == 'backup-wal':
        Path(str(c.prior) + '-wal').write_bytes(b'uncheckpointed state')
    elif damage == 'same-database':
        c.request['prior_journal_path'] = str(c.journal)
        c.request['prior_journal_sha256'] = _sha(c.journal.read_bytes())
    else:
        with _connect(c.journal) as db:
            if damage == 'changed-index':
                db.execute('DROP TRIGGER native_check_control_update')
            else:
                for name, (kind, _) in reversed(list(native_journal.SCHEMA.items())):
                    db.execute('DROP ' + kind.upper() + ' "' + name + '"')
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


@pytest.mark.parametrize('owner', ['resource', 'control', 'archive', 'other-native'])
def test_active_or_unselected_ownership_blocks_restoration(cleanup_case, owner):
    c = cleanup_case
    with _connect(c.journal) as db:
        if owner == 'resource':
            changed = copy.deepcopy(c.guard)
            changed['active_attempt'] = 'active-attempt'
            db.execute('UPDATE resource_limit SET record=?', (canonical(changed),))
        elif owner == 'control':
            db.execute("UPDATE technical_phase_control SET active=1, record=json_set(record,'$.active',json('true'),'$.phase.state','running')")
        elif owner == 'archive':
            changed = copy.deepcopy(c.archive)
            changed['active_phase'] = c.phase['id']
            db.execute('UPDATE technical_qualification SET record=?', (canonical(changed),))
        else:
            changed = copy.deepcopy(c.originals['nc-unrelated-clean'])
            changed['phase'] = 'cleanup_unknown'
            changed.pop('cleanup')
            db.execute('UPDATE native_check SET record=? WHERE id=?', (canonical(changed), changed['check_id']))
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


def test_failed_completed_receipt_verification_restores_no_rows(cleanup_case, monkeypatch):
    c = cleanup_case
    calls = []
    def verify(row):
        calls.append(row['check_id'])
        if len(calls) == 2:
            raise Rejected('fixture-proof', 'Prior native completion no longer verifies')
        return copy.deepcopy(row)
    monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(verify))
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert len(calls) == 2
    assert _rows(c.journal) == before


@pytest.mark.parametrize('result', ['none', 'cleanup-only', 'changed-row'])
def test_completed_verifier_cannot_silently_rewrite_prior_evidence(cleanup_case, monkeypatch, result):
    c = cleanup_case
    def verify(row):
        if result == 'none':
            return None
        if result == 'cleanup-only':
            return copy.deepcopy(row['cleanup'])
        changed = copy.deepcopy(row)
        changed['candidate'] = 'changed-by-verifier'
        return changed
    monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(verify))
    before = _rows(c.journal)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert _rows(c.journal) == before


def test_second_row_write_failure_rolls_back_the_first_row_revision_and_audit(cleanup_case, monkeypatch):
    c = cleanup_case
    before = _snapshot(c.journal)
    original_connect = sqlite3.connect
    attempted = []

    class InterruptedBatch(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('UPDATE native_check SET record='):
                attempted.append(parameters[1])
                if len(attempted) == 2:
                    raise sqlite3.OperationalError('fixture second-row interruption')
            return super().execute(sql, parameters)

    def connect(database, *args, **kwargs):
        if str(database) == c.journal.as_uri() + '?mode=rw':
            kwargs['factory'] = InterruptedBatch
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert len(attempted) == 2
    assert _snapshot(c.journal) == before


def test_current_path_replacement_during_writer_open_rejects_same_bytes_and_revision(cleanup_case, monkeypatch):
    c = cleanup_case
    before = _snapshot(c.journal)
    original_connect = sqlite3.connect
    old_inode = c.journal.stat().st_ino
    replaced = False

    def connect(database, *args, **kwargs):
        nonlocal replaced
        if str(database) == c.journal.as_uri() + '?mode=rw' and not replaced:
            replacement = c.directory / 'same-bytes-replacement.sqlite3'
            shutil.copyfile(c.journal, replacement)
            replacement.chmod(0o600)
            assert replacement.read_bytes() == c.journal.read_bytes()
            os.replace(replacement, c.journal)
            replaced = True
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert replaced and c.journal.stat().st_ino != old_inode
    assert _snapshot(c.journal) == before


@pytest.mark.parametrize('drift', ['row', 'new-row', 'resource', 'control', 'archive', 'backup'])
def test_compare_and_swap_rechecks_every_passive_snapshot_after_verification(cleanup_case, monkeypatch, drift):
    c = cleanup_case
    changed = False
    def verify(row):
        nonlocal changed
        if not changed:
            changed = True
            if drift == 'backup':
                _update_row(c.prior, 'nc-unrelated-clean', lambda value: value.update(candidate='changed'))
            else:
                with _connect(c.journal) as db:
                    if drift == 'row':
                        value = json.loads(db.execute('SELECT record FROM native_check WHERE id=?',
                                                      ('nc-completed-two',)).fetchone()[0])
                        value['candidate'] = 'changed'
                        db.execute('UPDATE native_check SET record=? WHERE id=?',
                                   (canonical(value), value['check_id']))
                    elif drift == 'new-row':
                        value = copy.deepcopy(c.originals['nc-unrelated-clean'])
                        value['check_id'] = 'nc-late-clean'
                        db.execute('INSERT INTO native_check VALUES(?,?,?)', (value['check_id'], None, canonical(value)))
                    elif drift == 'resource':
                        value = copy.deepcopy(c.guard)
                        value['high_water_growth_bytes']['output_roots'] += 1
                        db.execute('UPDATE resource_limit SET record=?', (canonical(value),))
                    elif drift == 'control':
                        db.execute("UPDATE technical_phase_control SET record=json_set(record,'$.phase.hold_receipt','changed')")
                    else:
                        value = copy.deepcopy(c.archive)
                        value['charged_growth_bytes']['output_roots'] += 1
                        db.execute('UPDATE technical_qualification SET record=?', (canonical(value),))
        return copy.deepcopy(row)
    monkeypatch.setattr(CodexRuntime, 'verify_completed_native', staticmethod(verify))
    with pytest.raises(Rejected):
        _restore(c, apply=True)
    assert changed
    rows = _rows(c.journal)
    assert all(json.loads(rows[item['check_id']])['phase'] == 'cleanup_unknown' for item in c.request['checks'])
