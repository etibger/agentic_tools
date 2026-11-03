"""Real disposable CLI preview/apply paths; all host observations are simulated."""
import copy
import json

import pytest

from agentic_tools.aos import cli
from agentic_tools.aos import startup_failure_recovery as recovery
from agentic_tools.aos import startup_resource_continuation as continuation
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Rejected, Store
from test_aos_startup_failure_recovery import failed_startup, retain, sql
from test_startup_resource_continuation import (
    acknowledge, approved_continuation, continuation_proposal,
)


ROUTES = [('recover-startup-failure', 'failed_startup', 'request'),
          ('continue-recovered-startup', 'approved_continuation', 'continuation_request')]


def arguments(c, command, request, *, apply=False):
    path = c.evidence_root / ('cli-' + command + '.json')
    retain(path, request)
    result = [command, '--state-dir', str(c.store.directory), '--request', str(path)]
    for root in c.store.worker_writable_roots:
        result += ['--worker-writable-root', str(root)]
    return result + (['--apply'] if apply else [])


def journals(c):
    return c.store.path.read_bytes(), c.runtime.read_bytes()


@pytest.mark.parametrize('command,fixture_name,request_name', ROUTES)
def test_default_cli_preview_uses_read_only_lifecycle_and_preserves_journals(
        request, monkeypatch, capsys, command, fixture_name, request_name):
    c = request.getfixturevalue(fixture_name)
    argv = arguments(c, command, getattr(c, request_name))
    before = journals(c)
    seen = []
    original_factory = Lifecycle.for_reading.__func__
    def for_reading(cls, store):
        assert store.read_only is True
        seen.append(store.path)
        return original_factory(cls, store)
    def no_mutating_constructor(*args, **kwargs):
        pytest.fail('Default preview must not create or migrate lifecycle schema')
    monkeypatch.setattr(Lifecycle, 'for_reading', classmethod(for_reading))
    monkeypatch.setattr(Lifecycle, '__init__', no_mutating_constructor)
    assert cli.main(argv) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == '' and result['ready'] is True and result['applied'] is False
    assert seen == [c.store.path]
    assert journals(c) == before
    table = recovery.TABLE if command == 'recover-startup-failure' else continuation.TABLE
    assert not sql(c.store.path, 'SELECT 1 FROM sqlite_schema WHERE name=?', (table,))


@pytest.mark.parametrize('command,fixture_name,request_name', ROUTES)
def test_cli_explicit_apply_uses_existing_mutation_route_only_when_requested(
        request, monkeypatch, capsys, command, fixture_name, request_name):
    c = request.getfixturevalue(fixture_name)
    argv = arguments(c, command, getattr(c, request_name), apply=True)
    before_attempts = sql(c.runtime, 'SELECT id,record FROM attempt ORDER BY id')
    before_budgets = sql(c.runtime, 'SELECT id,record FROM runtime_budget ORDER BY id')
    seen = []
    original_constructor = Lifecycle.__init__
    def construct(self, store):
        assert store.read_only is False
        seen.append(store.path)
        original_constructor(self, store)
    def no_preview_factory(*args, **kwargs):
        pytest.fail('Explicit apply must use the existing mutable lifecycle context')
    monkeypatch.setattr(Lifecycle, '__init__', construct)
    monkeypatch.setattr(Lifecycle, 'for_reading', classmethod(no_preview_factory))
    assert cli.main(argv) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == '' and result['applied'] is True
    assert seen == [c.store.path]
    table = recovery.TABLE if command == 'recover-startup-failure' else continuation.TABLE
    assert sql(c.store.path, f'SELECT COUNT(*) FROM {table}')[0][0] == 1
    assert sql(c.runtime, 'SELECT id,record FROM attempt ORDER BY id') == before_attempts
    assert sql(c.runtime, 'SELECT id,record FROM runtime_budget ORDER BY id') == before_budgets
    assert sql(c.store.path, "SELECT execution,admitted FROM lifecycle WHERE work_id='Q1'") == [('held', 0)]


@pytest.mark.parametrize('command,fixture_name,request_name', ROUTES)
@pytest.mark.parametrize('apply', [False, True])
def test_cli_request_cannot_supply_an_approval_answer(
        request, capsys, command, fixture_name, request_name, apply):
    c = request.getfixturevalue(fixture_name)
    supplied = {**copy.deepcopy(getattr(c, request_name)), 'approved': True}
    argv = arguments(c, command, supplied, apply=apply)
    before = journals(c)
    assert cli.main(argv) == 2
    output = capsys.readouterr()
    assert output.out == '' and json.loads(output.err)['error']
    assert journals(c) == before


@pytest.mark.parametrize('apply', [False, True])
def test_cli_continuation_still_requires_the_new_saved_human_decision(continuation_proposal, capsys, apply):
    c = continuation_proposal
    argv = arguments(c, 'continue-recovered-startup', c.continuation_request, apply=apply)
    before = journals(c)
    assert cli.main(argv) == 2
    output = capsys.readouterr()
    assert output.out == '' and json.loads(output.err)['error'] == 'startup_resource_continuation'
    assert journals(c) == before
    assert not sql(c.store.path, 'SELECT 1 FROM sqlite_schema WHERE name=?', (continuation.TABLE,))


def test_read_only_lifecycle_factory_does_not_relax_default_constructor(failed_startup):
    c = failed_startup
    before = journals(c)
    with pytest.raises(Rejected, match='read-only store'):
        Lifecycle.for_reading(c.store)
    read_only = Store(c.store.directory, c.store.worker_writable_roots, read_only=True)
    with pytest.raises(Rejected, match='trusted service store'):
        Lifecycle(read_only)
    assert Lifecycle.for_reading(read_only).store is read_only
    assert journals(c) == before


def test_existing_projection_advances_atomically_with_continuation_before_admit(approved_continuation):
    c = approved_continuation
    with c.store._transaction() as db:
        assert db.execute("DELETE FROM technical_blocker WHERE work_id='Q1' AND code='unrelated_obligation'").rowcount == 1
        c.lifecycle._touch(db, 'Q1', 'fixture_obligation_satisfied', {'simulated': True})
    c.lifecycle.reconcile('Q1')
    revision = c.store.read('Q1')['revision']
    c.continuation_request['expected_work_revision'] = revision
    assert sql(c.store.path, "SELECT source_revision FROM projection WHERE work_id='Q1'") == [(revision,)]
    before_attempts = sql(c.runtime, 'SELECT id,record FROM attempt ORDER BY id')
    outcome = acknowledge(c, apply=True)
    assert outcome['status'] == 'success' and outcome['work_revision'] == revision + 1
    assert c.store.read('Q1')['revision'] == revision + 1
    admitted = c.lifecycle.admit('admit-after-current-projection', 'Q1')
    assert admitted['execution'] == 'eligible'
    assert not sql(c.store.path, "SELECT 1 FROM technical_blocker WHERE work_id='Q1' AND code='projection_mismatch'")
    assert sql(c.store.path, 'SELECT COUNT(*) FROM assignment WHERE predecessor=?',
               ('startup-continuation:' + c.continuation_operation,)) == [(1,)]
    assert sql(c.runtime, 'SELECT id,record FROM attempt ORDER BY id') == before_attempts
