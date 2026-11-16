"""Real disposable native commands; model turns and approval remain scripted."""
import json

import pytest

from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.worker import Worker
import test_aos_codex_runtime as adapter_fixture
from test_aos_codex_runtime import prepared
from test_aos_internal_milestones import serial_plan


@pytest.mark.parametrize('passing', [False, True])
def test_native_checkpoint_must_finish_before_next_writer(request, monkeypatch, passing):
    monkeypatch.setattr(adapter_fixture, 'synthetic_plan', serial_plan)
    p = request.getfixturevalue('prepared')
    real_check = p.runtime._check
    observations = []

    def observed_check(*args, **kwargs):
        # There is no second writer before, or during, actual native execution.
        assert len(p.lifecycle.assignments()) == 2
        result = real_check(*args, **kwargs)
        assert len(p.lifecycle.assignments()) == 2
        observations.append(result)
        return result

    monkeypatch.setattr(p.runtime, '_check', observed_check)
    with Worker(p.lifecycle, p.runtime) as worker:
        worker.tick()
        (p.source / 'example.py').write_text('def add(a,b):\n return a' + ('+b' if passing else '*b') + '\n')
        p.backend.finish('thread-0')
        worker.tick()
        assessment = p.lifecycle.assignments()[-1]
        report = p.outputs / assessment['attempt_id'] / ('review-' + assessment['attempt_id'] + '.txt')
        report.write_text(json.dumps({'candidate_identity': candidate(p.source)['identity'],
            'verdict': 'pass', 'findings': [], 'limits': 'Scripted independent source review; actual native pytest.'}))
        p.backend.finish('thread-1')
        worker.tick()
        assert len(observations) == 1
        assert observations[0]['result'] == ('pass' if passing else 'fail'), json.dumps(observations[0], indent=2)
        assert observations[0]['selected'] == 1
        assert observations[0]['cleanup']['owned_processes'] == []
        assert observations[0]['cleanup']['effects_reconciled'] is True
        assignments = p.lifecycle.assignments()
        assert len(assignments) == (3 if passing else 2)
        assert p.lifecycle.snapshot('Q2')['delivery'] is None
        assert p.backend.count('turn/start') == (3 if passing else 2)
        if passing:
            assert json.loads(assignments[-1]['scope'])['di_task_id'] == 'task-2'
            # Startup of writer two exercises its checkpoint baseline in the
            # actual runtime preflight and atomic launch reservation.
            assert assignments[-1]['state'] == 'observed_active'
