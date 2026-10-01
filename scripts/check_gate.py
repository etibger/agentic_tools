"""Check a bounded gate record; this does not authenticate evidence or run agents."""
import argparse
import json
import re
from pathlib import Path

REQUIRED = {"verification", "reviewer", "devils_advocate"}

def check(state, evidence_root=None):
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    def reference(value, label):
        require(isinstance(value, str) and bool(value.strip()), label + " reference missing")
        if evidence_root is not None and isinstance(value, str) and value:
            root = Path(evidence_root).resolve()
            resolved = (root / value).resolve()
            require(resolved.is_relative_to(root) and resolved.is_file(), label + " artifact missing/outside evidence root")
    require(state.get("gate") in {"hold", "accepted"}, "unknown gate state")
    candidate = state.get("candidate", {})
    identity = candidate.get("patch_sha256", "")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", identity)), "candidate patch SHA-256 missing/invalid")
    require(candidate.get("frozen") is True, "candidate source is not frozen")
    for key in ("base", "inventory_ref", "own_report_ref"):
        if key != "base":
            reference(candidate.get(key), key)
        else:
            require(bool(candidate.get(key)), "exact base missing")
    reports = state.get("reports", {})
    require(REQUIRED.issubset(reports), "required independent role missing")
    if state.get("gate") == "accepted":
        for role in set(reports) | REQUIRED:
            report = reports.get(role, {})
            require(report.get("status") == "complete", role + " initial assessment incomplete")
            require(report.get("candidate_sha256") == identity, role + " candidate mismatch")
            reference(report.get("initial_ref"), role + " initial")
            reference(report.get("exchange_ref"), role + " post-initial exchange")
        for finding in state.get("findings", []):
            require(finding.get("disposition") in {"resolved", "rejected_with_evidence", "approved_exception"},
                    "unresolved finding: " + str(finding.get("id")))
            reference(finding.get("resolution_ref"), "finding resolution")
        require(bool(state.get("checks")), "executed check obligations missing")
        for item in state.get("checks", []):
            require(item.get("candidate_sha256") == identity, "check candidate mismatch")
            require(bool(item.get("command")) and bool(item.get("executor")) and bool(item.get("host")),
                    "check execution context missing")
            if item.get("required") is not False and item.get("result") != "passed":
                reference(item.get("approved_exception_ref"), "required check exception")
            require(item.get("result") in {"passed", "failed", "blocked", "skipped", "unrun"},
                    "invalid check result")
            if item.get("result") == "passed":
                reference(item.get("log_ref"), "executed check log")
                if item.get("kind") == "suite":
                    count = item.get("selected_count")
                    require(isinstance(count, int) and not isinstance(count, bool) and count > 0,
                            "passing suite selected zero/unknown tests")
        resources = state.get("native_resource", {})
        require(resources.get("released") is True, "native resources not released")
        reference(resources.get("release_ref"), "native resource release")
        require(bool(resources.get("last_command")), "last required native command missing")
        remaining = state.get("remaining")
        require(isinstance(remaining, int) and not isinstance(remaining, bool) and remaining >= 0,
                "remaining approved task count invalid")
        if isinstance(remaining, int) and remaining > 0:
            transition = state.get("next", {})
            require(bool(transition.get("owner")) and bool(transition.get("assignment")),
                    "next owner/action missing")
            require(bool(transition.get("acknowledged_at")), "next assignment acknowledgment missing")
        elif remaining == 0:
            require(bool(state.get("completed_at")), "final task completion timestamp missing")
        require(bool(state.get("accepted_at")), "gate acceptance timestamp missing")
    return errors

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()
    try:
        errors = check(json.loads(args.state.read_text()), args.evidence_root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, str(exc) + "\n")
    if errors:
        parser.exit(1, "\n".join("- " + e for e in errors) + "\n")
    print("Gate record structure accepted; inspect actual source and execution evidence separately.")

if __name__ == "__main__":
    main()
