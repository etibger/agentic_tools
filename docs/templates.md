# Download the workflow templates

Canonical files live in the repository's templates/ directory. The build generates the downloads below from those files, so the prompt and the guide do not carry competing copies.

## Toolbox entry points

Provide the completed shared contract alongside exactly one kickoff. See the [quick start](quick-start.md), [specification](toolbox.md) and [worked examples](worked-examples.md). These pilot contracts require manual evidence assessment; the existing gate checker does not validate their profiles.

| Template | Purpose |
| --- | --- |
| [Shared work contract](downloads/work-contract.txt) | Scope, authority, assurance, decisions, evidence and handoff rules used by all three workflows |
| [Bug-fix kickoff](downloads/bug-fix-kickoff.txt) | Reproduce, investigate, repair and verify a violation of intended behavior |
| [Feature-change kickoff](downloads/feature-change-kickoff.txt) | Change an existing product through useful integrated increments |
| [New-product kickoff](downloads/new-product-kickoff.txt) | Discover need and feasibility, then deliver a first usable slice through feature changes |

Publication and monitoring are explicit choices for these entry points. Blank authority fields add no permissions.

Toolbox v0.2 adds the project/team introduction, responsibility grades and proposed model/effort settings before investigation, followed by DI and explicit Concept Approval before implementation. The approved [AOS concept](aos-concept.md) records the first prototype and its remaining qualification holds; the templates remain manually operated.

## Extended feature package

The original full-package recipe retains its defaults. Use it explicitly through the [extended quick start](extended-feature-start.md); it is not automatically selected by elevated assurance.

| Template | Purpose |
| --- | --- |
| [Extended feature kickoff](downloads/feature-kickoff.txt) | Define required specification/checkpoints, five-minute audits and a post-retrospective actions milestone |
| [Detailed investigation](downloads/design-investigation.txt) | Prepare observed behavior, proposed choices, traceability and ordered exit gates |
| [Bounded assignment](downloads/assignment.txt) | Specify owner, candidate, resources, evidence and stop condition |
| [Gate and transition](downloads/gate-transition.txt) | Consume reports, decide the gate and acknowledge the next action |
| [Retrospective](downloads/retrospective.txt) | Facilitate actual discussion, a checked record and tracked follow-up actions |
| [Live dashboard](downloads/dashboard.html) | Serve with canonical workflow-state.json; includes approvals, next action and bounded burndown |
| [Workflow state example](downloads/workflow-state.example.json) | Synthetic starting shape for live state, decision register and output links |
| [Five-minute watchdog](downloads/watchdog.txt) | Scheduler prompt and audit contract; requires actual runtime scheduling |
| [Example gate state](downloads/gate-state.example.json) | Synthetic accepted-stage record for the checker |

These are instructions for a future, explicitly started workflow. Reading or downloading them does not authorize execution. Fill required fields, remove inapplicable optional lanes and obey the target repository's policies.
