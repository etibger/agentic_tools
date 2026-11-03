"""Shutdown preserves completed evidence; replay observes without reconciling it.

All process observations are simulated. These tests launch no native command,
permission probe, renderer, app-server, model, or qualification phase.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_journal, native_paths, native_processes, native_sandbox, native_temporary
from agentic_tools.aos import codex_runtime as runtime_module
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected, canonical


def identity(pid, *, group=None):
    return {'pid': pid, 'uid': os.getuid(), 'birth': [1700000000, pid],
            'parent': 1, 'group': group or pid, 'status': 2}


@pytest.fixture
def completed(tmp_path, monkeypatch):
    source, outputs, state, cache, temporary = [tmp_path / name for name in
                                              ('source', 'outputs', 'state', 'cache', 'temporary')]
    for path in (source, outputs, state, cache, temporary):
        path.mkdir(mode=0o700)
    (source / 'source.txt').write_text('original source')
    (outputs / 'result.txt').write_text('retained result')
    (cache / 'artifact').write_text('native cache artifact')
    retired_root = temporary / 'owned-fixture'
    recipe = {'source': str(source), 'outputs': str(outputs), 'native_write_roots': [
        {'path': str(cache), 'kind': 'cache'}], 'native_sandbox': {'version': native_sandbox.VERSION,
        'temporary_paths': [{'parent': str(temporary), 'name': retired_root.name, 'kind': 'tree'}],
        'owned_temporary_roots': [str(retired_root)]}}
    check_id, receipts = 'nc-completed-history', []
    # The disposable directory has no process owner. Exercise the actual
    # exclusive creation/removal helper to obtain a structurally exact receipt.
    owned = native_temporary.prepare(recipe, check_id, {}, receipts.append)
    retired = native_temporary.remove(recipe, check_id, owned, receipts.append, quiescent=True)
    later = temporary / 'later-owner'
    later.mkdir()
    (later / 'do-not-touch').write_text('unrelated later contents')
    retired_root.symlink_to(later, target_is_directory=True)
    runtime = CodexRuntime(SimpleNamespace(store=SimpleNamespace(directory=state)), {})
    profile = state / 'completed-profile.sb'
    profile.write_text('(version 1)\n(deny default)\n')
    census = {'backend': native_processes.BACKEND, 'reconciled': True, 'boundary_verified': True,
              'remaining': [], 'errors': [], 'observer': identity(990001), 'members': [identity(990002)],
              'exit_code': 0, 'interrupted': False, 'stop_reason': None}
    status = state / 'completed-status.json'
    status.write_text(json.dumps({'exit_code': 0, 'census': census}))
    native_after = native_paths.snapshot(recipe)
    row = {'check_id': check_id, 'attempt_id': None, 'phase': 'clean', 'source': str(source),
           'outputs_root': str(outputs), 'output': str(outputs / 'check'),
           'status': str(status), 'release': str(state / 'completed.release'),
           'cancel_path': str(state / 'completed.cancel'), 'ownership_backend': native_processes.BACKEND,
           'owner': {'pid': 990003, 'started': 'synthetic-owner-birth', 'native_identity': identity(990003)},
           'members': {}, 'native_process_receipt': copy.deepcopy(census),
           'native_path_recipe': recipe, 'native_path_contract': recipe, 'owned_temporary': retired,
           'native_sandbox_boundary': {'path': str(profile), 'sha256': hashlib.sha256(profile.read_bytes()).hexdigest(),
                                       'contract': recipe['native_sandbox'], 'temporary_before': {}},
           'cleanup': {'stopped': True, 'owned_processes': [], 'effects_reconciled': True,
                       'partial_mutations': {'status': 'none', 'changes': []},
                       'output_mutations': {'status': 'recorded', 'changes': ['historical-only.txt']},
                       'source_after': {'historical-source': 'retained-source-hash'},
                       'outputs_after': {'historical-output': 'retained-output-hash'},
                       'observed_at': '2026-01-01T00:00:00+00:00',
                       'native_path_effects': {'after': native_after, 'changes': {str(cache): []}},
                       'owned_temporary': copy.deepcopy(retired), 'process_ownership': copy.deepcopy(census),
                       'native_temporary_effects': {'observed': True, 'created': {}, 'preexisting_preserved': True}}}
    with runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (check_id, None, canonical(row)))
    observed = []
    def absent(pid):
        observed.append(pid)
        return None
    monkeypatch.setattr(native_processes, 'identity', absent)
    monkeypatch.setattr(runtime_module, 'process_groups', lambda **kwargs: {})
    return SimpleNamespace(runtime=runtime, row=row, source=source, outputs=outputs, state=state,
                           cache=cache, temporary=temporary, retired_root=retired_root, later=later,
                           profile=profile, status=status, census=census, observed=observed)


def journal_bytes(p):
    with p.runtime._db() as db:
        return db.execute('SELECT id,attempt_id,record FROM native_check ORDER BY id').fetchall()


def prohibit_reconciliation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Completed verification attempted mutation or a fresh effects scan')
    for owner, names in ((CodexRuntime, ('_native_save', '_native_temporary_remove')),
                         (runtime_module, ('inventory',)),
                         (native_paths, ('snapshot', 'reconcile')),
                         (native_sandbox, ('temporary_snapshot',)),
                         (native_temporary, ('prepare', 'remove')),
                         (os, ('kill', 'unlink', 'rmdir', 'mkdir', 'rename', 'replace')),
                         (subprocess, ('Popen',))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


@pytest.mark.parametrize('denial', ['libproc', 'ps'])
def test_close_does_not_revalidate_or_rewrite_completed_history_under_observation_denial(completed, monkeypatch, denial):
    p = completed
    before = journal_bytes(p)
    with p.runtime._db() as db:
        revision = native_journal.revision(db)
    def denied(*args, **kwargs):
        if denial == 'ps':
            raise subprocess.CalledProcessError(1, ['/bin/ps'], stderr='Operation not permitted')
        raise PermissionError('simulated restricted libproc observation')
    monkeypatch.setattr(native_processes, 'identity', denied)
    monkeypatch.setattr(runtime_module, 'process_groups', denied)
    def never_stop(*args, **kwargs):
        pytest.fail('Shutdown revisited a historical completed check')
    monkeypatch.setattr(p.runtime, '_native_stop', never_stop)
    p.runtime.close()
    assert journal_bytes(p) == before
    with p.runtime._db() as db:
        assert native_journal.revision(db) == revision
    assert not Path(p.row['cancel_path']).exists()


@pytest.mark.parametrize('phase', ['intent', 'running', 'cleanup_unknown'])
def test_close_still_requests_cancellation_for_unresolved_rows(completed, monkeypatch, phase):
    p = completed
    unresolved = {'check_id': 'nc-unresolved', 'attempt_id': None, 'output': str(p.outputs / 'unresolved'), 'phase': phase}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-unresolved', None, canonical(unresolved)))
    calls = []
    monkeypatch.setattr(p.runtime, '_native_stop', lambda check_id, **kw: calls.append((check_id, kw)))
    before = journal_bytes(p)
    p.runtime.close()
    assert calls == [('nc-unresolved', {'cancel': True})]
    assert journal_bytes(p) == before


@pytest.mark.parametrize('raw', ['{',
    '{"check_id":"nc-invalid","attempt_id":null,"output":"/fixture/invalid","phase":"clean","phase":"running"}',
    '{"check_id":"wrong-id","attempt_id":null,"output":"/fixture/invalid","phase":"clean"}'])
def test_close_keeps_invalid_indexed_rows_selected_for_explicit_cleanup(completed, monkeypatch, raw):
    p = completed
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-invalid', None, raw))
    calls = []
    monkeypatch.setattr(p.runtime, '_native_stop', lambda check_id, **kw: calls.append((check_id, kw)))
    before = journal_bytes(p)
    p.runtime.close()
    assert calls == [('nc-invalid', {'cancel': True})]
    assert journal_bytes(p) == before


@pytest.mark.parametrize('field,value', [('check_id', '-invalid-id'), ('output', 'relative/check')])
def test_close_selects_invalid_clean_identity_syntax_as_unresolved(completed, monkeypatch, field, value):
    p = completed
    invalid = {'check_id': 'nc-invalid-clean', 'attempt_id': None, 'output': str(p.outputs / 'invalid'), 'phase': 'clean'}
    invalid[field] = value
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (invalid['check_id'], None, canonical(invalid)))
    calls = []
    monkeypatch.setattr(p.runtime, '_native_stop', lambda check_id, **kw: calls.append((check_id, kw)))
    before = journal_bytes(p)
    p.runtime.close()
    assert calls == [(invalid['check_id'], {'cancel': True})]
    assert journal_bytes(p) == before


def test_close_legacy_without_index_keeps_cleanup_only_selection(completed, monkeypatch):
    p = completed
    unresolved = {'check_id': 'nc-legacy', 'attempt_id': None, 'output': str(p.outputs / 'legacy'), 'phase': 'running'}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', ('nc-legacy', None, canonical(unresolved)))
        # Simulate an entirely older journal, never a partial-schema fallback.
        for name, (kind, _) in list(native_journal.SCHEMA.items())[::-1]:
            db.execute('DROP ' + kind + ' ' + name)
        assert native_journal.verify(db, allow_absent=True) is False
    calls = []
    monkeypatch.setattr(p.runtime, '_native_stop', lambda check_id, **kw: calls.append((check_id, kw)))
    before = journal_bytes(p)
    p.runtime.close()
    assert calls == [('nc-legacy', {'cancel': True})]
    assert journal_bytes(p) == before
    with p.runtime._db() as db:
        assert native_journal.verify(db, allow_absent=True) is False


def test_close_partial_index_rejects_without_silently_omitting_owned_rows(completed, monkeypatch):
    p = completed
    with p.runtime._db() as db:
        db.execute('DROP TRIGGER native_check_control_update')
    calls = []
    monkeypatch.setattr(p.runtime, '_native_stop', lambda *args, **kw: calls.append((args, kw)))
    before = journal_bytes(p)
    with pytest.raises(Rejected, match='schema or revision triggers'):
        p.runtime.close()
    assert calls == [] and journal_bytes(p) == before


def test_completed_verifier_preserves_exact_history_and_reused_temp_path(completed, monkeypatch):
    p = completed
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    prohibit_reconciliation(monkeypatch)
    verified = CodexRuntime.verify_completed_native(p.row)
    assert verified == original and p.row == original
    assert verified is not p.row and verified['cleanup'] is not p.row['cleanup']
    assert journal_bytes(p) == before
    assert set(p.observed) == {990001, 990002, 990003}
    assert p.retired_root.is_symlink()
    assert (p.later / 'do-not-touch').read_text() == 'unrelated later contents'
    assert not Path(p.row['cancel_path']).exists() and not Path(p.row['release']).exists()


@pytest.mark.parametrize('target', ['observer', 'member', 'owner', 'owner-reused', 'wrapper-child'])
def test_completed_verifier_rejects_live_or_changed_process_identity_without_mutation(completed, monkeypatch, target):
    p = completed
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    selected = {'observer': 990001, 'member': 990002, 'owner': 990003, 'owner-reused': 990003}
    def current(pid):
        if pid != selected.get(target):
            return None
        value = identity(pid)
        if target == 'owner-reused':
            value['birth'][1] += 1
        return value
    monkeypatch.setattr(native_processes, 'identity', current)
    groups = {}
    if target == 'owner':
        groups[990003] = {'parent': 1, 'group': 990003, 'started': p.row['owner']['started']}
    if target == 'wrapper-child':
        groups[990004] = {'parent': 1, 'group': 990003, 'started': 'unexpected-wrapper'}
    monkeypatch.setattr(runtime_module, 'process_groups', lambda **kwargs: groups)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises((Rejected, RuntimeError), match='identity|census|wrapper'):
        CodexRuntime.verify_completed_native(p.row)
    assert p.row == original and journal_bytes(p) == before


@pytest.mark.parametrize('damage', ['profile', 'status-json', 'status-census', 'status-valid-census',
                                  'cached-census', 'cleanup-census', 'cache-root', 'retired-temp', 'cleanup-temp'])
def test_completed_verifier_rejects_binding_mismatch_without_new_effects(completed, monkeypatch, damage):
    p = completed
    if damage == 'profile':
        p.profile.write_text('changed profile')
    elif damage == 'status-json':
        p.status.write_text('{')
    elif damage == 'status-census':
        status = json.loads(p.status.read_text())
        status['census']['boundary_verified'] = False
        p.status.write_text(json.dumps(status))
    elif damage == 'status-valid-census':
        status = json.loads(p.status.read_text())
        status['census']['exit_code'] = 7
        p.status.write_text(json.dumps(status))
    elif damage == 'cached-census':
        p.row['native_process_receipt']['exit_code'] = 7
    elif damage == 'cleanup-census':
        p.row['cleanup']['process_ownership']['exit_code'] = 7
    elif damage == 'cache-root':
        p.cache.rename(p.cache.with_name('old-cache'))
        p.cache.mkdir()
    elif damage == 'retired-temp':
        p.row['owned_temporary']['roots'][0]['state'] = 'owned'
    else:
        p.row['cleanup']['owned_temporary']['roots'][0]['removed_at'] = 'changed receipt'
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises((Rejected, RuntimeError)):
        CodexRuntime.verify_completed_native(p.row)
    assert p.row == original and journal_bytes(p) == before


@pytest.mark.parametrize('denial', ['libproc', 'ps'])
def test_completed_verifier_observation_failure_is_not_absence_or_mutation(completed, monkeypatch, denial):
    p = completed
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    def denied(*args, **kwargs):
        if denial == 'ps':
            raise subprocess.CalledProcessError(1, ['/bin/ps'], stderr='Operation not permitted')
        raise PermissionError('simulated libproc denial')
    monkeypatch.setattr(native_processes if denial == 'libproc' else runtime_module,
                        'identity' if denial == 'libproc' else 'process_groups', denied)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises((Rejected, OSError, subprocess.SubprocessError)):
        CodexRuntime.verify_completed_native(p.row)
    assert p.row == original and journal_bytes(p) == before


@pytest.mark.parametrize('field,value', [('phase', 'running'), ('cleanup', None)])
def test_noncompleted_row_cannot_pass_completed_verifier(completed, monkeypatch, field, value):
    p = completed
    p.row[field] = value
    original = copy.deepcopy(p.row)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises(Rejected):
        CodexRuntime.verify_completed_native(p.row)
    assert p.row == original


@pytest.mark.parametrize('damage', [
    'original-no-launch', 'status-no-launch', 'status-mixed',
    'mixed-disposition', 'null-disposition', 'mixed-admission', 'null-admission',
    'missing-census', 'empty-census', 'missing-cleanup-census', 'empty-cleanup-census',
    'missing-backend', 'wrong-backend',
])
def test_completed_census_verifier_rejects_no_launch_or_missing_proof_variants(completed, monkeypatch, damage):
    p = completed
    no_launch = {'launched': False, 'backend': native_processes.BACKEND}
    if damage == 'original-no-launch':
        p.row.pop('native_process_receipt')
        p.row['cleanup'].pop('process_ownership')
        p.row['native_launch_disposition'] = no_launch
        p.row['cleanup']['native_admission'] = copy.deepcopy(no_launch)
        # The current status still contains the valid original census. It
        # cannot convert a retained no-launch record into completed execution.
    elif damage in ('status-no-launch', 'status-mixed'):
        status = {'exit_code': None, 'native_admission': no_launch,
                  'resource_failure': {'reason': 'synthetic admission refusal'}}
        if damage == 'status-mixed':
            status['census'] = copy.deepcopy(p.census)
        p.status.write_text(json.dumps(status))
    elif damage in ('mixed-disposition', 'null-disposition'):
        p.row['native_launch_disposition'] = no_launch if damage == 'mixed-disposition' else None
    elif damage in ('mixed-admission', 'null-admission'):
        p.row['cleanup']['native_admission'] = no_launch if damage == 'mixed-admission' else None
    elif damage == 'missing-census':
        p.row.pop('native_process_receipt')
    elif damage == 'empty-census':
        p.row['native_process_receipt'] = {}
    elif damage == 'missing-cleanup-census':
        p.row['cleanup'].pop('process_ownership')
    elif damage == 'empty-cleanup-census':
        p.row['cleanup']['process_ownership'] = {}
    elif damage == 'missing-backend':
        p.row.pop('ownership_backend')
    else:
        p.row['ownership_backend'] = 'unsupported-backend'
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises(Rejected):
        CodexRuntime.verify_completed_native(p.row)
    assert p.row == original and journal_bytes(p) == before
    if damage not in ('status-no-launch', 'status-mixed'):
        assert p.observed == []  # Retained proof is checked before observations.


@pytest.mark.parametrize('admission', [None, {'launched': False, 'backend': native_processes.BACKEND}],
                         ids=['null-admission', 'no-launch-admission'])
@pytest.mark.parametrize('resource_failure', ['missing', False], ids=['missing-resource-failure', 'false-resource-failure'])
def test_status_admission_key_cannot_hide_behind_a_valid_census(completed, monkeypatch, admission, resource_failure):
    p = completed
    status = json.loads(p.status.read_text())
    status['native_admission'] = admission
    if resource_failure != 'missing':
        status['resource_failure'] = resource_failure
    p.status.write_text(json.dumps(status))
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises(Rejected, match='Native status differs'):
        CodexRuntime.verify_completed_native(p.row)
    assert p.observed == []
    assert p.row == original and journal_bytes(p) == before


def test_completed_status_symlink_is_rejected_before_process_observation(completed, monkeypatch):
    p = completed
    retained = p.status.with_name('retained-original-status.json')
    p.status.rename(retained)
    p.status.symlink_to(retained)
    status_bytes = retained.read_bytes()
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    prohibit_reconciliation(monkeypatch)
    with pytest.raises(Rejected, match='Native status differs'):
        CodexRuntime.verify_completed_native(p.row)
    assert p.observed == [] and p.status.is_symlink()
    assert retained.read_bytes() == status_bytes
    assert p.row == original and journal_bytes(p) == before


def test_status_byte_change_during_observation_is_rejected_even_with_equal_census(completed, monkeypatch):
    p = completed
    status_bytes = p.status.read_bytes()
    original = copy.deepcopy(p.row)
    before = journal_bytes(p)
    observe = CodexRuntime._native_observe
    def change_status(row):
        result = observe(row)
        # Simulate a concurrent external write, leaving parsed JSON unchanged.
        p.status.write_bytes(status_bytes + b'\n')
        return result
    monkeypatch.setattr(CodexRuntime, '_native_observe', staticmethod(change_status))
    prohibit_reconciliation(monkeypatch)
    with pytest.raises(Rejected, match='Native status changed during'):
        CodexRuntime.verify_completed_native(p.row)
    assert p.observed
    assert p.status.read_bytes() == status_bytes + b'\n'
    assert json.loads(p.status.read_bytes()) == json.loads(status_bytes)
    assert p.row == original and journal_bytes(p) == before
