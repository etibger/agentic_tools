"""Exact-package and HTTP contracts; these do not substitute for owner review."""

import pytest
from pydantic import ValidationError

from agentic_tools.aos.coordinator import DecisionContext
from agentic_tools.aos.store import Rejected, Store, digest
from test_aos_approval_web import fixture, get_form, running_page
from test_aos_store import TEAM


CONTEXT = {
    "problem": "A saved browser session may expire; observed in the HTTP fixture.",
    "example": "After restarting the service, reopen the proposal before answering.",
    "effort": "Estimate: 10–20 minutes; hard cap: 30 minutes; model cost unknown.",
    "dependencies": "No other feature changes. Actual browser validation is still needed.",
    "evidence": "HTTP checks passed in simulation; a human must inspect the rendered page.",
    "stopping_point": "Return the tested local change; stop if authority boundaries would expand.",
}
CHOICE = dict(recommendation="Use a read-only recovery link", alternative="Keep the current message",
              consequence="Approval permits this bounded local change only")


def present(store, context, revision=1):
    return store.present(f"concept-{revision}", "BRIEF", revision, "DI", revision,
                         "Explain how to recover an expired decision session", ["No automatic answer"],
                         TEAM, "Synthetic fixture only", decision_context=context, **CHOICE)


def test_brief_bytes_bind_approval_and_old_challenge_becomes_stale(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "worker",))
    store.introduce("intro", "BRIEF", "Fixture", ["fixture/"], TEAM)
    first = present(store, CONTEXT)
    package = store.read("BRIEF")["concepts"][0]["package"]
    assert package["decision_context"] == CONTEXT
    assert first["digest"] == digest(package)
    capability = store.host_capability("test", "SIMULATED:brief")
    challenge = store.begin_approval("answer", "BRIEF", 2, first["digest"], capability=capability)
    second = present(store, {**CONTEXT, "effort": "Estimate unknown; hard cap remains 30 minutes."}, 2)
    assert second["digest"] != first["digest"]
    assert store.finish_approval(challenge["challenge_id"], "accept", True,
                                 capability=capability)["outcome"] == "stale"


@pytest.mark.parametrize("bad", [
    {}, {**CONTEXT, "effort": " "}, {**CONTEXT, "extra": "hidden"},
    {**CONTEXT, "example": 3}, {**CONTEXT, "evidence": "x" * 4097},
])
def test_incomplete_or_undeclared_brief_rejected_without_recording(tmp_path, bad):
    with pytest.raises(ValidationError):
        DecisionContext.model_validate(bad, strict=True)
    store = Store(tmp_path / "state", (tmp_path / "worker",))
    store.introduce("intro", "BRIEF", "Fixture", ["fixture/"], TEAM)
    with pytest.raises(Rejected, match="incomplete_decision_context"):
        present(store, bad)
    assert store.read("BRIEF")["concepts"] == []


def test_context_requires_choice_and_legacy_package_stays_unchanged(tmp_path):
    store, first = fixture(tmp_path)
    old = store.read("WEB-TEST")["concepts"][0]["package"]
    assert "decision_context" not in old
    assert first["digest"] == digest(old)
    with pytest.raises(Rejected, match="incomplete_decision_context"):
        store.present("incomplete", "WEB-TEST", 2, "DI", 2, "Missing choice", ["test"], TEAM,
                      "test only", decision_context=CONTEXT)
    assert store.read("WEB-TEST")["concepts"][0]["package"] == old


def test_brief_is_escaped_and_material_limits_visible_before_details(tmp_path):
    store = Store(tmp_path / "state", (tmp_path / "worker",))
    store.introduce("intro", "BRIEF", "Fixture", ["fixture/"], TEAM)
    context = {**CONTEXT, "problem": "<script>unknown</script> & observed failure"}
    current = present(store, context)
    capability = store.host_capability("test", "SIMULATED:brief")
    store.begin_approval("answer", "BRIEF", 2, current["digest"], capability=capability)
    with running_page(store) as (port, _):
        conn, body, *_ = get_form(port)
        conn.close()
    visible = body.split("<details>", 1)[0]
    assert "<script>" not in body
    assert "&lt;script&gt;unknown&lt;/script&gt; &amp; observed failure" in visible
    for key, value in context.items():
        if key != "problem":
            assert value in visible
    assert current["digest"] not in visible
    assert not store.read("BRIEF")["decisions"]
