"""Rendering cannot authorize later effects after a saved hold or deadline."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from agentic_tools.aos import native_processes, native_sandbox, native_temporary
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import native_fixture


@pytest.mark.parametrize('reason', ['resource', 'deadline', 'cancel'])
def test_postrender_gate_precedes_owned_directory_probe_and_supervisor(tmp_path, monkeypatch, reason):
    p = native_fixture(tmp_path, 'no-child')
    parent = tmp_path / 'native-temporary'
    parent.mkdir()
    owned = parent / 'owned-root'
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    native_sandbox={'version': native_sandbox.VERSION,
                                    'temporary_paths': [{'parent': str(parent), 'name': owned.name, 'kind': 'tree'}],
                                    'owned_temporary_roots': [str(owned)]})
    rendered = []

    def profile(*args):
        rendered.append(True)
        return ['/usr/bin/sandbox-exec', '-f', 'fixture-never-executed.sb']

    monkeypatch.setattr(p.runtime, '_native_profile_prepare', profile)
    monkeypatch.setattr(native_temporary, 'prepare', lambda *a, **k: pytest.fail('temporary creation after render hold'))
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *a, **k: pytest.fail('permission probe after render hold'))
    if reason == 'resource':
        def payload(recipe):
            if rendered:
                raise Rejected('resource_limits', 'Retained resource breach observed during rendering')
            return None
        monkeypatch.setattr(p.runtime, '_resource_native_payload', payload)
        code = 'resource_limits'
    elif reason == 'deadline':
        original = p.runtime._native_deadline
        monkeypatch.setattr(p.runtime, '_native_deadline', lambda *a, **k:
                            datetime.now(timezone.utc) - timedelta(seconds=1) if rendered else original(*a, **k))
        code = 'assignment_deadline'
    else:
        p.recipe['_attempt_id'] = 'cancelled-during-render'
        monkeypatch.setattr(p.runtime, '_get', lambda _: {'native_cancel_requested': True} if rendered else None)
        code = 'check_cancelled'
    try:
        with pytest.raises(Rejected, match=code):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', p.frozen)
        assert rendered == [True]
        assert not owned.exists() and not p.runtime._native_processes
        with p.runtime._db() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT record FROM native_check')]
        assert len(rows) == 1
        row = rows[0]
        assert row['phase'] == 'clean'
        assert 'owned_temporary' not in row and 'owner' not in row
        assert row['preparation_failure']['proof']['stage'] == 'profile'
        assert row['cleanup']['native_admission'] == {'launched': False, 'reason': 'preparation_failed'}
    finally:
        p.runtime.close()
