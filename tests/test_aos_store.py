from pathlib import Path

import pytest

from agentic_tools.aos.store import Rejected, Store, digest


TEAM = [{"name": "investigator", "role": "Technical Lead", "grade": "AOS-G5",
         "model": "gpt-6.1-sol", "effort": "high", "scope": "synthetic M0 only"}]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state", (tmp_path / "worker",))


def concept(store, work="M0-TEST", op="concept-1", expected=1, revision=1, body="synthetic no-op"):
    if expected == 1:
        store.introduce("intro-" + work, work, "M0 fixture", ["fixture/"], TEAM)
    return store.present(op, work, expected, "DI-001", revision, body,
                         ["No project writes"], TEAM, "Synthetic confirmation only; no dispatch")


def test_exact_current_human_confirmation_is_durable_and_idempotent(store):
    presented = concept(store)
    capability = store.host_capability("observed-test-host", "SIMULATED:test-receipt")
    challenge = store.begin_approval("approve-1", "M0-TEST", 2, presented["digest"], capability=capability)
    result = store.finish_approval(challenge["challenge_id"], "accept", True, capability=capability)
    assert result["outcome"] == "approved"
    assert result["digest"] == digest(challenge["package"])
    assert result["dispatch_available"] is False
    assert store.finish_approval(challenge["challenge_id"], "accept", True, capability=capability) == result
    with pytest.raises(Rejected, match="already answered differently"):
        store.finish_approval(challenge["challenge_id"], "decline", None, capability=capability)
    assert store.begin_approval("approve-1", "M0-TEST", 2, presented["digest"], capability=capability) == {"completed": True, "result": result}
    reopened = Store(store.directory, (store.directory.parent / "worker",), read_only=True)
    record = reopened.read("M0-TEST")
    assert record["state"] == "ready"
    assert len(record["decisions"]) == 1
    assert record["decisions"][0]["response"] == {"action": "accept", "approved": True}
    assert record["decisions"][0]["qualification"] == "SIMULATED:test-receipt"


@pytest.mark.parametrize(("action", "answer", "outcome"), [
    ("decline", None, "declined"), ("accept", False, "declined"),
    ("cancel", None, "cancelled"), ("unavailable", None, "unavailable"),
    ("accept", None, "unavailable"),
])
def test_nonaffirmative_response_never_approves(store, action, answer, outcome):
    presented = concept(store)
    capability = store.host_capability("test-host", "SIMULATED:nonaffirmative")
    challenge = store.begin_approval("decision-1", "M0-TEST", 2, presented["digest"], capability=capability)
    assert store.finish_approval(challenge["challenge_id"], action, answer, capability=capability)["outcome"] == outcome
    assert store.read("M0-TEST")["state"] == "awaiting_concept_approval"


def test_revision_changing_during_confirmation_is_stale(store):
    first = concept(store)
    capability = store.host_capability("test-host", "SIMULATED:stale")
    challenge = store.begin_approval("approval", "M0-TEST", 2, first["digest"], capability=capability)
    latest = concept(store, op="concept-2", expected=2, revision=2, body="changed authority")
    result = store.finish_approval(challenge["challenge_id"], "accept", True, capability=capability)
    assert result["outcome"] == "stale"
    record = store.read("M0-TEST")
    assert record["revision"] == latest["work_revision"]
    assert record["state"] == "awaiting_concept_approval"
    assert record["decisions"][0]["digest"] == first["digest"]


def test_old_digest_and_wrong_expected_revision_rejected(store):
    first = concept(store)
    latest = concept(store, op="concept-2", expected=2, revision=2)
    capability = store.host_capability("test-host", "SIMULATED:reject")
    with pytest.raises(Rejected, match="Only the current"):
        store.begin_approval("old-digest", "M0-TEST", latest["work_revision"], first["digest"], capability=capability)
    with pytest.raises(Rejected, match="Expected current"):
        store.begin_approval("old-revision", "M0-TEST", 2, latest["digest"], capability=capability)


@pytest.mark.parametrize("forged", [None, "human", "worker", {"approver": "user"}, object()])
def test_worker_or_model_cannot_supply_human_capability(store, forged):
    current = concept(store)
    with pytest.raises(Rejected) as error:
        store.begin_approval("forged", "M0-TEST", 2, current["digest"], capability=forged)
    assert error.value.code == "forbidden_decision"
    assert not store.read("M0-TEST")["decisions"]


def test_capability_cannot_be_reused_on_another_connection(store):
    current = concept(store)
    bearer = store.host_capability("test-host", "SIMULATED:local")
    another = Store(store.directory, (store.directory.parent / "worker",))
    with pytest.raises(Rejected, match="Only the configured"):
        another.begin_approval("forged", "M0-TEST", 2, current["digest"], capability=bearer)


def test_idempotency_conflict_and_concurrent_confirmation_hold(store):
    current = concept(store)
    assert concept(store) == current
    with pytest.raises(Rejected, match="different request"):
        concept(store, body="changed")
    bearer = store.host_capability("test-host", "SIMULATED:pending")
    store.begin_approval("pending", "M0-TEST", 2, current["digest"], capability=bearer)
    with pytest.raises(Rejected, match="unresolved confirmation"):
        store.begin_approval("pending", "M0-TEST", 2, current["digest"], capability=bearer)


def test_competing_current_challenge_is_rejected(store):
    current = concept(store)
    bearer = store.host_capability("test-host", "SIMULATED:competing")
    first = store.begin_approval("first", "M0-TEST", 2, current["digest"], capability=bearer)
    with pytest.raises(Rejected, match="already pending"):
        store.begin_approval("second", "M0-TEST", 2, current["digest"], capability=bearer)
    assert store.finish_approval(first["challenge_id"], "decline", None, capability=bearer)["outcome"] == "declined"
    assert store.read("M0-TEST")["state"] == "awaiting_concept_approval"


def test_status_opens_read_only_and_cannot_mutate(store):
    concept(store)
    reader = Store(store.directory, (store.directory.parent / "worker",), read_only=True)
    assert reader.read("M0-TEST")["revision"] == 2
    with pytest.raises(Rejected) as error:
        reader.introduce("mutate", "NEW", "bad", ["source"], TEAM)
    assert error.value.code == "read_only"


def test_state_in_worker_root_or_symlink_is_rejected(tmp_path):
    with pytest.raises(Rejected, match="outside every"):
        Store(tmp_path / "worker" / "state", (tmp_path / "worker",))
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path)
    with pytest.raises(Rejected, match="symlinks"):
        Store(alias / "state", (tmp_path / "worker",))


def test_state_permissions_and_database_symlink_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    with pytest.raises(Rejected, match="0700"):
        Store(state, (tmp_path / "worker",))
    state.chmod(0o700)
    (state / "aos.sqlite3").symlink_to(tmp_path / "worker.sqlite3")
    with pytest.raises(OSError):
        Store(state, (tmp_path / "worker",))
