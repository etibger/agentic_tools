"""Simulated owner/decision routing; real native acceptance has separate tests."""
import copy
import json

from agentic_tools.aos import supervisor
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import canonical
from agentic_tools.aos.worker import FakeRuntime
from test_aos_assessment_continuation import recovered, project_setup
from test_aos_supervisor import SimulatedService, answer


def test_continuation_owner_waits_for_new_decision_then_assesses_once(recovered, monkeypatch):
    p = recovered
    packet = p.prepare()
    retained = p.state / 'prepared-assessment-FIXTURE-fixture-continuation.json'
    supervisor._retain(retained, packet)
    old_bytes = p.original_path.read_bytes()
    answers, created, progress = [], [], []
    def emit(value):
        progress.append(copy.deepcopy(value))
        if value['state'] == 'pending':
            before = p.store.read('FIXTURE')
            assert len(before['decisions']) == 1
            assert len(before['lifecycle']['assignments']) == 2
            assert created == []
            answers.append(answer(p.store, value['decision_url'].split('challenge=')[1]))
    def runtime(lifecycle, recipe, work_id):
        assert len(answers) == 1
        assignments = p.store.read('FIXTURE')['lifecycle']['assignments']
        assert [a['kind'] for a in assignments] == ['implementation', 'assessment', 'assessment']
        latest = assignments[-1]
        assert latest['candidate'] == p.frozen
        assert lifecycle.assessment_context(latest['assignment_id']) == {'purpose': 'assessment'}
        selected = FakeRuntime(p.state / 'simulated-successor.sqlite3', [
            {'kind': 'assessment', 'result': {'status': 'completed', 'executor': 'assessor'}}])
        selected.close = lambda: None
        created.append(selected)
        return selected
    # This suite verifies owner/decision/dispatch mechanics. Native receipt and
    # independent-review gates run in the unchanged reviewed-host test suites.
    def accepted(lifecycle, db, run, contract, assignment, result, receipt_id):
        db.execute("UPDATE assignment SET state='completed',result=? WHERE assignment_id=?",
                   (canonical(result), assignment['assignment_id']))
        db.execute("UPDATE lifecycle SET execution='delivered',admitted=0 WHERE work_id=?", (assignment['work_id'],))
        db.execute('DELETE FROM resource WHERE work_id=?', (assignment['work_id'],))
        return 'delivered'
    monkeypatch.setattr(Lifecycle, '_reviewed_host_complete_in', accepted)
    options = dict(continuation_path=retained, service_factory=SimulatedService,
                   runtime_factory=runtime, emit=emit, interval=.01)
    first = supervisor.start(p.project_file, p.request_file, **options)
    assert first['state'] == 'delivered', first
    assert first['cleanup']['owned_stop']['known_stopped']
    assert supervisor.start(p.project_file, p.request_file, **options)['state'] == 'delivered'
    assert len(created) == len(answers) == 1
    assert p.original_path.read_bytes() == old_bytes
    state = p.store.read('FIXTURE')
    assert len(state['decisions']) == 2 and len(state['lifecycle']['assignments']) == 3
    assert p.lifecycle.startup_retries_used('FIXTURE') == 1
