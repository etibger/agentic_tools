"""Carry-forward denial must precede product attempt and process ownership."""
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_processes, resource_limits
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.native_qualification import TechnicalQualification
from agentic_tools.aos.store import Rejected
from agentic_tools.aos.worker import Worker
from test_aos_codex_runtime import claim, prepared
from test_native_qualification_independent import synthetic_private_receipts


def test_known_capsule_overrun_before_start_keeps_no_attempt_or_reservation(
        prepared, tmp_path, monkeypatch):
    p = prepared
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    resource_limits={
                        'output_roots': [],
                        'source_evidence_roots': [str(p.source), str(p.outputs)],
                        'max_output_growth_bytes': 100,
                        'max_source_evidence_growth_bytes': 100,
                        'min_free_bytes': 1,
                        'sample_interval_seconds': 1})
    qualification = TechnicalQualification(tmp_path / 'private-qualification', 'startup-carry',
                                           budget_paths=[p.runtime.path])
    try:
        phase = qualification.begin('technical-phase', p.recipe, max_seconds=60)
        # Synthetic private check receipts isolate real export/import accounting;
        # this test neither runs nor claims native product qualification.
        synthetic_private_receipts(SimpleNamespace(q=qualification), phase)
        assert qualification.reconcile('technical-phase')['status'] == 'passed'
        exported = qualification.export(p.recipe, qualification.directory / 'carry.json', work_id='Q2')
        final_recipe = exported['recipe']
        assert exported['accepted'] is False
        growth = p.outputs / 'approval-wait-overrun'
        growth.write_bytes(b'x' * 101)

        # The fixture already models approved admission. Isolate start's resource
        # gate without changing any real approval or executing a child process.
        monkeypatch.setattr(p.runtime, '_recipe', lambda _: final_recipe)
        launched = []
        def unexpected_client(*args, **kwargs):
            launched.append(True)
            raise AssertionError('Known pre-start denial must not create a client')
        monkeypatch.setattr(p.runtime, '_client', unexpected_client)
        assignment = claim(p)
        with p.runtime._db() as db:
            budget_before = list(db.execute('SELECT id,record FROM runtime_budget ORDER BY id'))
            scopes_before = list(db.execute('SELECT scope,budget_id FROM product_budget_scope ORDER BY scope'))

        denied = None
        for remove_growth in (False, True):
            if remove_growth:
                growth.unlink()
            observation = p.runtime.start(assignment)
            assert observation['state'] == 'startup_failed'
            assert observation['model_calls_attempted'] == 0
            assert observation['result']['startup_outcome']['admission_denial'] is True
            assert observation['result']['startup_outcome']['budget_reserved'] is False
            assert observation['result']['stopped'] is True
            assert observation['result']['effects_reconciled'] is True
            assert observation['result']['owned_processes'] == []
            if denied is not None:
                assert observation == denied
            denied = observation
            with p.runtime._db() as db:
                guard = resource_limits._read(db, 'Q2')
                assert guard['breach']['reason'] == 'max_source_evidence_growth_bytes'
                assert guard['active_attempt'] is None
                assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM native_check').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM admission_denial').fetchone()[0] == 1
                assert list(db.execute('SELECT id,record FROM runtime_budget ORDER BY id')) == budget_before
                assert list(db.execute('SELECT scope,budget_id FROM product_budget_scope ORDER BY scope')) == scopes_before
            assert not (p.outputs / assignment['attempt_id']).exists()
            assert not launched and not p.backend.clients and p.backend.calls == []
            assert p.runtime.model_calls == 0

        # A fresh runtime replays only this exact durable denial. An ordinary
        # absent attempt still cannot manufacture a clean cancellation receipt.
        reopened = CodexRuntime(p.lifecycle, {'Q2': final_recipe}, transport_factory=p.backend.factory)
        try:
            assert reopened.lookup(assignment['attempt_id']) == denied
            assert reopened.poll(assignment['attempt_id']) == denied
            assert reopened.start(assignment) == denied
            with pytest.raises(Rejected, match='runtime_identity'):
                reopened.start({**assignment, 'executor': 'different-executor'})
            stopped = reopened.cancel(assignment['attempt_id'])
            assert stopped['stopped'] is True and stopped['effects_reconciled'] is True
            assert reopened.cancel('ordinary-absent-attempt')['effects_reconciled'] is False
            with Worker(p.lifecycle, reopened) as worker:
                worker.tick()
                snapshot = p.lifecycle.snapshot('Q2')
                assert snapshot['assignments'][0]['state'] == 'failed'
                assert 'unknown_effects' not in {b['code'] for b in snapshot['technical_blockers']}
                p.lifecycle.control('cancel-known-admission-denial', 'Q2', 'cancel')
                worker.tick()
            snapshot = p.lifecycle.snapshot('Q2')
            assert snapshot['execution'] == 'cancelled' and snapshot['resources'] == []
            with reopened._db() as db:
                assert db.execute('SELECT count(*) FROM attempt').fetchone()[0] == 0
                assert list(db.execute('SELECT id,record FROM runtime_budget ORDER BY id')) == budget_before
            assert not launched and not p.backend.clients and p.backend.calls == []
        finally:
            reopened.close()
    finally:
        qualification.close()
