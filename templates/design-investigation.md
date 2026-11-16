# Detailed investigation — [feature]

Status: proposed / revised / approved. Version: [ID]. Source: [exact identity]. Reporting cutoff: [UTC timestamp].
Prepared by: [actual role]. Decision owner: [user].
Confluence parent: [authorized ID/URL]. Published DI: [verified URL/ID/version]. Lessons learned child: [pending / verified URL].
Publish and read back this complete package before requesting approval and before dependent implementation/testing.

## Decision summary
Desired behavior, recommendation, alternatives, practical consequences and exact approval needed.
Use the compact pattern in `decision-brief.md`: include observed problem, example, additional effort, dependency impact, evidence and stopping point. Distinguish estimates from caps and real observations from simulations. Keep material limits above technical details.

## Scope and requirements
Before/after example; requirement IDs; exclusions; delivery/authority/platform boundaries.

## Observed behavior
Actual files/symbols, input-to-consumer trace, persisted/shared/client ownership and reused contracts. Label evidence separately from proposed behavior.

## Proposed design
Interfaces/data flow; editable figure; schema/examples/defaults/errors/lifecycle; compatibility; alternatives/tradeoffs.

## Failure boundaries and acceptance contract
| Requirement | Consumer/input/error boundary | Distinguishing observable | False positive rejected | Evidence level / limit |
| --- | --- | --- | --- | --- |
| [R1] | [boundary] | [observable] | [counterexample] | [source/native/live/limit] |

## Native environment and validation
| Check | Exact command | Host / prerequisite | Purpose | Shared resource owner | Baseline / required status |
| --- | --- | --- | --- | --- | --- |
| [check] | [discovered command] | [host] | [purpose] | [owner] | [actual status] |

## Ordered bring-up and exit gates
Include required specification sources/rendering/semantic checks and serialized checkpoint ownership per affected stage. Name the retrospective and post-retrospective actions and their acceptance boundaries. Bind supported internal gates to the serial execution profile below. For gates outside that bounded profile, prepare separate approvable work rather than adding a display-only gate.

| Task | Objective / boundary | Dependencies | Implementer work | Independent assessment | Required evidence / exit |
| --- | --- | --- | --- | --- | --- |
| [T0] | [objective] | [prerequisite] | [bounded work] | [trio/checks] | [pass/limits] |

## Executable AOS plan and estimates
For an executable AOS concept, attach the authoritative `di_plan` (`aos-di-plan-v1`) to the same versioned request as its execution envelope. Obtain the maintained `planning.DIPlan`/`ConceptRequest` schema through coordinator `operation_schema` for `present-concept`; prose or a dashboard diagram does not replace it. Keep this explanation, the exact plan and the displayed approval estimates consistent.

Omitting `di_plan.execution_profile` retains two aggregate tasks: writer-owned `implementation` and independently owned `assessment`, each with its own milestone. For internal checkpoints, set `execution_profile: "serial-milestones-v1"` and list 2, 4 or 6 tasks, up to three alternating writer/assessor pairs. Each task has one milestone in matching order; the first has no dependencies and every later task and milestone depends on exactly its immediate predecessor. Use the same approved writer and distinct assessor for every pair. Give every task stable IDs, concrete outputs and task-level definitions of done. State which semantic definitions of done the independent assessor must verify.

Every implementation milestone requires `candidate-frozen`. Every assessment milestone requires all envelope required and compatibility checks plus `independent-assessment`; every checkpoint executes the same complete native recipe. Explain how those checks remain meaningful at each stage. Intermediate success is `milestone_passed`, allowing only the next writer task; only final independent assessment accepts delivery. The first writer uses the original approved baseline and later writers use the exact preceding accepted checkpoint candidate. Retries and repairs retain the same task and milestone; repairs remain within confirmed findings and correction limits. Independent source review covers the cumulative original-baseline-to-candidate change. Unsupported branches, parallel stages and additional task kinds require separately approved work.

For each task, retain the original low/base/high active agent effort and elapsed hours, assumptions (including dependencies and human/technical waits), confidence, exclusions and material cost/delivery risks. Cover remaining investigation, implementation, tests and independent assessment. A four-task route requires at least four worker launches and the full native suite at both assessment checkpoints; include those costs and allowed recovery work rather than assuming later checkpoints are free. Estimate agent/API and infrastructure spend with currency and a supported basis, or explicitly record unknown values and the reason; unknown is not zero. State plan-level replan triggers. Show approved execution caps separately from forecasts and identify which limits are enforced. Readiness, admission and launch must retain capacity for remaining tasks and approved recovery; later stages do not reset charges or allowances. This contract adds no currency cap.

Keep original estimates distinguishable from later forecasts and actuals. Use coordinator `record-forecast` for a reasoned revision against the current package and stable task ID, preserving the original estimate. Record observed active, human-wait and technical-wait hours separately; use explicit unknowns for unmeasured time and monetary actuals. A forecast increase changes no cap. Explain material deviations and obtain a focused decision when the approved scope or authority must change. The dashboard must project the same package's task/milestone evidence, with attempts kept separately; completed-task counts are not spend percentages.

## Open questions, risks and decisions
One concise question per consequential unresolved choice. Record recommendation and impact. Register each pending request with a stable ID, requesting role, affected gate, timestamp and evidence/request link in workflow-state.json; show it in the dashboard. Record answers once and clear pending state promptly.

## Approval
User, date, exact approved version/choices/limits. Material revisions require a focused updated record. Silence is not approval.
