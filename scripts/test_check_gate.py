import json
import unittest
from pathlib import Path
from check_gate import check

class GateContractTests(unittest.TestCase):
    def setUp(self):
        self.state = json.loads((Path(__file__).resolve().parents[1] / "templates/gate-state.example.json").read_text())
    def test_synthetic_example(self):
        self.assertEqual(check(self.state), [])
    def test_missing_independent_initial_report(self):
        self.state["reports"]["reviewer"]["initial_ref"] = ""
        self.assertTrue(any("reviewer initial" in x for x in check(self.state)))
    def test_mismatched_candidate(self):
        self.state["reports"]["verification"]["candidate_sha256"] = "2" * 64
        self.assertTrue(any("candidate mismatch" in x for x in check(self.state)))
    def test_zero_selected_suite(self):
        self.state["checks"][0]["selected_count"] = 0
        self.assertTrue(any("selected zero" in x for x in check(self.state)))
    def test_unconsumed_transition(self):
        self.state["next"]["acknowledged_at"] = None
        self.assertTrue(any("acknowledgment" in x for x in check(self.state)))
    def test_native_resources_not_released(self):
        self.state["native_resource"]["released"] = False
        self.assertTrue(any("not released" in x for x in check(self.state)))
    def test_unresolved_finding(self):
        self.state["findings"] = [{"id": "F1", "disposition": "open"}]
        self.assertTrue(any("unresolved" in x for x in check(self.state)))
    def test_completed_task_requires_frozen_timestamp(self):
        self.state["remaining"] = 0
        self.state["completed_at"] = None
        self.assertTrue(any("completion timestamp" in x for x in check(self.state)))
    def test_required_unrun_is_not_accepted(self):
        self.state["checks"][0]["result"] = "unrun"
        self.assertTrue(any("required check exception" in x for x in check(self.state)))
    def test_missing_actual_artifacts(self):
        self.assertTrue(any("artifact missing" in x for x in check(self.state, Path(__file__).parent)))
