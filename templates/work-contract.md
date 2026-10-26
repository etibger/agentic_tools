# Shared work contract — toolbox v0.2

Provide this contract together with exactly one kickoff: bug-fix, feature-change or new-product. The user supplies the problem, repository, expected outcome, constraints and authority. The coordinator discovers routine source/check details and fills the relevant fields; omit irrelevant optional fields. Do not send the user this entire inventory as a questionnaire. Reading the templates grants no execution authority. Existing user instructions and repository rules govern the work.

## Brief

- Work ID / contract revision:
- Workflow: [bug-fix / feature-change / new-product]
- Assurance: [lightweight / standard / elevated; coordinator records rationale]
- Accountable coordinator / feature owner:
- Repository / source baseline / unrelated work to preserve:
- User problem and one before/after example:
- Committed acceptance / exclusions / compatibility:
- Delivery boundary / receiving maintainer:
- Existing approved decisions / unresolved decisions:
- Authority: local edits [scope]; commits [yes/no]; branch [name/none]; push/PR [yes/no]; install/deploy [scope/none]; external publication [destination/audience/none]; recurring monitoring [scope/none]. Unset fields grant no additional authority; continue unaffected authorized work.
- Environment / native check recipes / relevant requirement-source versions:
- Shared resources / exclusive executor / recovery and release condition:
- Affected maintained documentation / native generation/checks:
- Optional dashboard, Journalist, full retrospective or additional outputs: [selected scope/none]
- First useful increment / prerequisite / stop or replan trigger:
- Introduction shown before investigation / phases and dependencies:
- Initial team / provisional execution team: [title, responsibility grade/rationale, task, proposed model/effort, deliverable, access/resources, independence and stop condition per member]
- Investigation bounds / estimate and uncertainty:
- DI package revision / solution, alternatives, test strategy and ordered plan:
- Concept Approval: [pending / user's exact answer, approved revision and scope / explicit task-specific waiver]
- Approved staffing/model/budget envelope / actual launched configuration and later changes:

## Common operating rules

1. Present the introduction and initial/provisional team before investigation. Read applicable instructions, investigate within stated bounds and prepare a proportionate DI. Obtain explicit user Concept Approval for its exact revision before implementation; a broad repair request is not a waiver. Record existing exact-package approval or an explicit task-specific waiver without asking again. Confirm the actual team after approval. Grades describe expected responsibility; permissions, model/effort and observed capability remain separate.
2. Lightweight: meaningful author checks plus one non-author assessor. Standard: independent execution and source-review responsibilities, which one non-author assessor may cover if recorded. Elevated: separate Verification, Reviewer and Challenger, with independent initials before exchange and explicit execution/recovery boundaries. Show unavailable independence as a gap.
3. Use one product writer and one owner of shared outputs. Delegate only useful bounded work. Every assignment names owner, contract revision, deliverable, prerequisites, authority, resources and return condition. Separate workspaces require proof of actual resource isolation.
4. Run cheap builds and safe affected checks early within authority. Completion of a development assignment includes appropriate bring-up: build, intended behavior, relevant failure cases and immediate integration checks. Code written alone is not complete; missing or failing required bring-up leaves the task active or blocked. A tested candidate ready for independent assessment is still distinct from milestone acceptance. Freeze an exact candidate for acceptance: authorized commit or base + scoped patch + new-file inventory + relevant hashes. Recheck changed obligations and direct consumers; explain evidence reuse.
5. Retain executed command, environment, executor, candidate, result, selected scope, output reference and coverage limit. Zero selected tests is not a passing suite. Separate model, integration and physical-input evidence. Preserve failed attempts and corrections.
6. Keep factual review packets and reachable helpers free of peer conclusions before independent initials. Exchange concrete findings afterward. Accepting a shared-contract defect triggers a bounded direct-consumer sweep.
7. Consume each completion, record disposition/release and dispatch the next ready authorized assignment. Record sent, acknowledged and observed-active separately. A wait names its dependency/owner; unknown remains unknown. Documentation blocks only named dependent transitions.
8. Keep a versioned decision record. Material behavior, acceptance, environment, authority, assurance, staffing envelope or budget changes receive a focused decision when needed. Routine corrections and reassignments within the approved envelope continue with recorded changes. Do not reopen settled approvals or infer approval from silence. Retain original evidence as historical when its conditions change.
9. Close only the agreed delivery: real user outcome, final identity, affected documentation, required findings/checks and selected outputs. State accepted/committed/installed/released separately, name limitations and receiving ownership, and record proportionate learning plus any selected follow-up actions.

## Compact task and evidence records

| Task | Outcome / exit | Prerequisites | Owner / resource | State / observed time | Evidence / next action |
| --- | --- | --- | --- | --- | --- |
| [ID] | [observable behavior] | [IDs/none] | [owner/boundary] | [ready/assigned/acknowledged/active/waiting/done/failed/unknown] | [result or exact wait] |

For each accepted candidate, record identity, contract revision, required checks/reports and results, findings/dispositions, limits, acceptance owner/time and next action. For each human decision, record question, recommendation, impact, exact answer and affected revision. Keep detailed logs behind references rather than duplicating them in every status update.

This is a pilot manual contract. `scripts/check_gate.py` accepts the extended feature format only; it does not validate these profiles or prove execution.
