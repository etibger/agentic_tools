# Candidates and acceptance gates

## Freeze an exact source state

A branch name is mutable. Use an exact commit when authorized, or identify the uncommitted state with its base, complete scoped patch, new-file inventory and relevant file hashes.

Include untracked product files. Preserve unrelated changes. Confirm identity before and after assessment. Source identity proves which state was inspected; it does not prove a check ran.

When authorized, the Implementer creates scoped intermediate stage commits and Verification confirms the exact reachable tree. Verification may create a checkpoint through an explicit single-writer handoff. The Coordinator consumes both receipts before dependent implementation begins. Follow repository rules for separate documentation commits; missing authority or execution is an explicit checkpoint blocker. See [the checkpoint contract](implementation.md#create-intermediate-checkpoints).

## Parallel assessment and the join

At a frozen stage, dispatch the same candidate to:

- Verification for executed behavior and regressions;
- Reviewer for independent source/design assessment;
- Devil's Advocate for independent challenge of boundaries and assumptions.

Record each initial report before sharing conclusions. Then exchange findings and executed-evidence questions. Keep initial reports and later supplements distinct.

A fix changes identity. Freeze the corrected candidate, independently recheck the affected behavior and explain any reuse of unchanged earlier evidence.

## Accept only a complete gate

| Obligation | Required record |
| --- | --- |
| Identity | Base, patch/commit, complete inventory and confirmed hashes |
| Checkpoints | Authorized intermediate SHA(s), parent/scope/owner/time and independent tree receipt, or explicit pending blocker |
| Documentation | Required specification sources, readable generated output and independent semantic checks |
| Initial assessments | All required role reports against the same candidate |
| Executed checks | Command, environment, executor, selected tests, result and retained output |
| Coverage | Requirement obligations and explicit skipped/blocked/unrun limits |
| Findings | Concrete trigger, owner, disposition, independent recheck and dissent |
| Discussion | Cross-role exchange after initial reports, with resolving evidence |
| Resources | Last required native command and resource-release acknowledgment |
| Continuation | Next owner/action and dispatch acknowledgment, or precise dependency |
| Dashboard | Actual stage, gate state and next action match the accepted facts |

A required blocked check is not silently waived. Seek a concise scoped exception if it changes the approved delivery claim.

## Validate the example record

~~~sh
uv run python scripts/check_gate.py templates/gate-state.example.json
uv run python -m unittest discover -s scripts -p 'test_*.py'
~~~

The checker catches structural mistakes such as missing initial reports, candidate mismatch, zero selected passing suites, unresolved findings, unreleased resources and missing next-owner acknowledgment.

It is not a runtime watcher or proof that a report is truthful. For a real accepted gate, pass an evidence root to require referenced artifacts to exist, and still inspect their content and execution evidence.

~~~sh
uv run python scripts/check_gate.py /path/to/state.json --evidence-root /path/to/evidence
~~~

## Prevent the idle-team stall

When no agents are active and work remains, diagnose the exact reason immediately. Dispatch the next authorized assignment if its prerequisites are satisfied. If held, display the dependency and owner. The submitted kickoff authorizes a recurring watchdog every five minutes by default. Audit every actual agent without relying on completion/blocker messages. Create/reuse it through the runtime scheduler and record the actual ID/status; distinguish scheduled runs from manual audits. It complements prompt completion consumption rather than replacing it. Quiet unchanged audits and pause only at full delivery, including the verified lessons-learned child page and accepted post-retrospective actions milestone, or explicit user pause.
