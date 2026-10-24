# Future workflow improvements

Status: proposals for prioritization. Research date: 24 October 2026.

The [toolbox specification](docs/toolbox.md), shared contract, three entry points and [worked examples](docs/worked-examples.md) now define a pilot application of these proposals. Profile-aware validation, automated dependency transitions and measured effectiveness remain future work. Existing running projects retain their own contracts until explicitly migrated.

The aim is predictable delivery of useful tool features with a process proportionate to the change. These are independently written software-workflow proposals. Company source documents, private discussions and evidence stay in their existing access-controlled locations.

The five primary recommendations from this review are:

| ID | Recommendation | Smallest useful change |
| --- | --- | --- |
| R1 | Select the workflow by risk and size. | Define lightweight, standard and elevated-risk profiles with explicit roles, evidence and publication needs. |
| R2 | Make the approved project contract explicit and versioned. | Connect the existing brief and DI to one record of outcomes, scope, authority, tolerances, acceptance and approved changes. |
| R3 | Plan increments around uncertainty and dependency readiness. | Prove the riskiest assumption and one real end-to-end path before expanding implementation. |
| R4 | Make project status follow validated evidence. | Reconcile the gate record, workflow state and dashboard against one approved contract and candidate. |
| R5 | Extend delivery through use, support and measured learning. | Record consumer acceptance, the delivery boundary, a maintenance handoff and the effectiveness of one selected improvement. |

Implementation work remains a separate prioritization decision. Start with R1 and R2 for new projects. For a running integration project, first make its critical dependency path and first missing real-world result explicit. Trial a small transition validator before building a larger orchestration service.

## Five primary changes to trial

### R1: Make process cost proportional to risk

Add a selected profile to the kickoff and gate schema. A lightweight profile can use a short local contract, affected checks and one independent assessor. A standard profile adds independent execution and review; an elevated-risk profile adds an explicit challenger, stronger execution admission and retained recovery evidence. Choose by effects and uncertainty, including interaction with shared user resources, rather than lines changed. Roles are responsibilities; create separate agents when the independence or work justifies them. Make dashboards, recurring monitoring and external publication explicit project choices. Required outputs remain obligations once selected.

First affected surfaces: `templates/feature-kickoff.md`, `docs/quick-start.md`, `docs/gates.md` and `scripts/check_gate.py`. Trial one local correction and one native integration. Observe setup effort, handoff count, useful findings and acceptance gaps. Do not declare an elevated-risk interaction lightweight merely because its patch is small.

### R2: Bind work to a versioned outcome contract

Extend the existing brief and DI with a compact authoritative record: user outcome, committed acceptance, exclusions, selected profile, target environment, source versions, authority, dependencies and change tolerances. Link every milestone and accepted candidate to its contract revision. Record a human answer once and propagate it to dependent work. Routine repairs within an approved design continue; a material change gets one focused decision with impact and alternatives. Silence or an expired decision timer does not grant approval.

First affected surfaces: kickoff, DI and gate-transition templates, plus the workflow-state example. Trial an approved design correction and a changed execution environment. Success means that each role receives the same current obligation, an old approval is not requested again, historical environment evidence remains correctly scoped and no mandatory acceptance item silently disappears.

### R3: Deliver a working slice before expanding supporting machinery

Plan the first increment around the hardest external assumption and a representative complete user journey. Demonstrate construction, interaction, observed completion and cleanup before extending to a large matrix or comprehensive evidence protocol. Probe maximum capacity and platform limits early when they can invalidate the promised outcome. Keep pure/model checks, real integration, physical interaction and final delivery as distinct claims. Preparatory checks do not count as actual acceptance runs.

Within existing authority, run a cheap normal build and safe affected tests before a combined independent candidate assessment. Reserve separate admission for actions with additional effects. A failed feasibility probe produces a concrete change/stop/defer decision packet; it does not authorize a reduced deliverable. First affected surfaces: DI stage planning and verification guidance. Trial one external integration and record time to first actual user journey, first compiler feedback, remaining acceptance and rework.

### R4: Turn coordinator rules into checkable dependency transitions

Use one contract-bound task graph and evidence record to derive milestone summaries, role cards, decision state and the next ready assignment. Show the current milestone's subgraph: owner, prerequisite, wait reason, resource and observed status for each node. A documentation obligation blocks only the transitions that actually require it. Consume completion and dispatch ready authorized work together; explicitly distinguish sent, acknowledged, active and completed assignments. Keep large history outside the current dashboard projection.

Extend the structural gate checker incrementally: verify referenced candidate identities and evidence digests, contract/profile consistency, required checkpoints and transition prerequisites. Missing or conflicting records produce an unknown/held state, never a pass. This checks record consistency; human assessment still establishes whether evidence supports the behavior. First trial: resolved approval, stale summary, duplicate completion, failed build and independent documentation branch. Measure unreasoned ready waits and contradictory state fields, while preserving one native resource owner.

### R5: Finish at useful delivery and make learning testable

Add a consumer acceptance step to the existing verification, retrospective and post-retrospective actions workflow. It should show the agreed real user journey, limitations, installation or local-use boundary, recovery and receiving maintenance owner. Display committed acceptance remaining beside engineering checks. Commits, agents, reports and passing helper checks describe activity rather than delivered usefulness.

Keep the existing verified immediate-action milestone. Extend deferred improvements with a baseline, later observation and retain/revise/retire decision when their trial is authorized. Distinguish finite feature closure from continuing product support; do not keep a project or watchdog alive indefinitely for hypothetical future learning. First affected surfaces: verification, retrospective/actions and delivery templates. Trial one feature handoff and one process experiment, recording actual acceptance and effectiveness separately.

## How to use this backlog

Select at most two additional experiments for one feature. Give each selected experiment an accountable owner, existing authority, a bounded trial, an observable success condition and a revisit trigger. Suggested roles below become assignments only when adopted. Compare benefits with the effort and defects introduced by the experiment. Record unknown measurements as unknown.

The existing [coordination audit](docs/future-improvements.md) contains more detailed evidence and trial proposals for handoff latency, role freshness, earlier compiler feedback, finite conditional releases and bounded receipts. Retain that evidence when considering F09 through F12 below. A later review of a running native application reinforced the need for milestone dependency graphs, early integration proof, consistent status projections and genuinely independent review packets. These are proposed reusable responses; local corrections in one project do not establish reusable adoption or measured speedup.

## Product value and planning

### F01: Separate the continuing tool from each finite feature project

Keep a product backlog and maintenance owner across feature projects. Each feature has its own approved outcome, delivery boundary and closure; its successor can reuse the accepted baseline and unresolved product obligations. Ongoing product work should not keep a completed feature project open indefinitely.

Suggested owner: Coordinator with the feature owner. Trial: deliver one feature and start another using the same tool baseline. Success signal: the first project closes cleanly while support ownership, known limitations and the next feature remain discoverable.

### F02: Prioritize outcomes and make stopping a normal result

Add a short statement of who benefits, the observed problem, expected benefit and evidence that would make the feature no longer worthwhile. Revisit this when a major assumption fails or scope changes substantially. Include stop, defer and reduce scope among legitimate decisions.

Suggested owner: feature owner. Trial: reassess one feature after its first useful increment. Success signal: the decision explains expected value and remaining cost, with a recorded reason to continue, shrink or stop. Ticket and agent counts provide no substitute for that assessment.

### F03: Distinguish minimum acceptance from stretch goals

For uncertain performance or usability improvements, record a minimum acceptable outcome and optional stretch results, including the conditions under which each is measured. Keep optional work outside the committed completion denominator until adopted.

Suggested owner: Investigator and feature owner. Trial: one feature with a measurable performance or usability target. Success signal: acceptance remains unambiguous when the minimum succeeds and the stretch goal does not.

### F04: Forecast with evidence and uncertainty

Record rough size, dependencies and confidence before producing a completion forecast. Refresh the forecast after a representative increment, separating tool runtime, active work, resource queues, review and human decision waits. Keep actual timestamps distinct from estimates.

Suggested owner: Coordinator. Trial: three comparable increments. Success signal: forecasts explain changes and expose missing data without inventing exact completion dates. Use the observations before setting timing targets.

### F05: Offer reusable feature recipes

Create small recipes for a local bug fix, connector addition, behavioral feature and cross-component change. Each recipe suggests common failure boundaries, native checks and affected documentation. Select and trim the recipe during investigation.

Suggested owner: workflow maintainer. Trial: reuse one recipe on a second feature. Success signal: less setup effort with explicit applicability decisions and no omitted feature-specific obligation. Avoid a growing checklist of irrelevant questions.

## Requirements, dependencies and knowledge

### F06: Track requirement disposition and reverse traceability

Give each requirement a stable identity and a state such as proposed, committed, accepted, deferred or rejected, with a reason for changes. Permit a result or task to link back to the requirement it serves. Flag committed requirements with no check and accepted checks with no clear obligation.

Suggested owner: Investigator and Verification. Trial: one small requirement set. Success signal: a removed requirement, an untested requirement and an unnecessary task can all be identified without reading the entire project history.

### F07: Separate delivery dependencies from behavioral constraints

A dependency may mean an unavailable service, credential, dataset or tool version; it may also mean ordering, synchronization or compatibility needed for correct behavior. Record its kind, owner, needed point, readiness evidence and consequence of failure. An entry covered elsewhere should link to that coverage and status.

Suggested owner: Investigator. Trial: one feature with an external integration. Success signal: the plan distinguishes a missing prerequisite from an interaction defect, and verifies both at the appropriate boundary.

### F08: Record source authority and notice relevant source changes

Keep source title, version, retrieval date and authority classification with the decisions they support. Distinguish a requirement, guidance, local example, draft and informal discussion. When a source changes, identify affected decisions and checks before deciding whether to adopt it.

Suggested owner: Investigator. Trial: revise one source used by an in-flight synthetic project. Success signal: the affected obligations become visible while unrelated accepted evidence remains valid. Source text cannot expand the user's granted authority.

## Execution and evidence

### F09: Reuse evidence by impact, including unchanged consumers

Describe which requirements, interfaces, configuration and assumptions a change can affect. Each assessor can justify reuse of prior evidence and expand checks where the impact crosses into unchanged code. Measure recheck effort and later reopened findings.

Suggested owner: Verification and Reviewer. Trial: three corrections to previously assessed candidates. Success signal: reused evidence has an explicit unchanged basis and changed obligations receive fresh assessment. A small textual diff alone does not establish low impact.

### F10: Use finite execution batches and idempotent handoffs

When already authorized, release a named build followed conditionally by a bounded test on the same identity. Stop on failure, drift or unexpected effects. Give each assignment an identity so duplicate completion events cannot trigger duplicate execution. Reconcile an interrupted assignment before retrying it.

Suggested owner: Coordinator and Verification. Trial: a build failure, duplicate completion and interrupted handoff. Success signal: appropriate stops, one native owner, preserved outputs and a recorded next action. Build on the existing audit's finite-release and queue proposals.

### F11: Keep the dashboard projection small and explain its freshness

Expose distinct times for a role observation, received report, state update, scheduler audit and browser fetch. Derive current stage, next action and pending decisions from canonical records. Keep large immutable history outside the live payload and provide a compact completion summary with links.

Suggested owner: Dashboard owner. Trial: fresh HTTP with stale role facts, a resolved approval and a completed assignment awaiting consumption. Success signal: each state is displayed accurately, including uncertainty, without a misleading active label.

### F12: Retain evidence according to the delivery claim

Define which source snapshots, logs, configuration and reports must survive task cleanup. Use manifests with content identity and retained artifact locations. Check that the people who will accept or maintain the feature can retrieve them. A transient CI URL needs an associated retained result when later reproduction matters.

Suggested owner: Verification and the receiving maintainer. Trial: reconstruct one acceptance decision after its temporary workspace is gone. Success signal: the claim, selected scope and result remain inspectable. Use existing storage before introducing a new evidence platform.

### F13: Strengthen one representative oracle before expanding coverage

For a critical user journey, maintain a known-good case and a deliberately wrong result that the check must reject. For agent-generated outputs, independently review expected behavior and distinguish factual accuracy, citation quality and execution success. Expand mutation or fault-injection checks only where the risk warrants them.

Suggested owner: Verification. Trial: one important workflow boundary. Success signal: the test fails for the named plausible defect and passes for the accepted behavior, with the actual scope stated. Extend the existing oracle-calibration contract rather than creating another parallel suite.

### F14: Make challenge reviews answer a specific uncertainty

Select a challenging reviewer when a feature has a significant assumption, difficult interface, security boundary or ambiguous acceptance criterion. Give that role a concrete question and counterexample to investigate. Preserve independent findings and resolve disagreement with evidence.

Suggested owner: Coordinator. Trial: compare a targeted challenge with an unrestricted review on one suitable increment. Success signal: useful distinct findings and their assessment effort are recorded. Participation or a long debate is not itself a quality outcome.

### F15: Add explicit execution budgets and replan triggers

Permit a project to define limits for exploration, repeated repair attempts, compute use or elapsed dependency waiting. At a reached limit, summarize results, uncertainty and concrete options for reducing scope, changing approach or obtaining the missing resource. Use available usage data and preserve unknown categories.

Suggested owner: Coordinator and feature owner. Trial: a bounded investigation that encounters an unavailable dependency. Success signal: it ends in a usable decision packet without silently repeating work or changing the promised outcome.

## Delivery and continuing ownership

### F16: Trial progressive delivery where distribution warrants it

For shared tools, consider one representative user or environment before wider rollout. Define the exact package, installation/configuration steps, success and failure observations, rollback procedure and required delivery authority. A local-only feature can finish at its agreed local boundary.

Suggested owner: Implementer and receiving maintainer. Trial: one authorized pilot delivery. Success signal: package identity, actual user journey and recovery are demonstrated; the pilot's scope is clear before expansion.

### F17: Treat compatibility and deprecation as product obligations

Record supported clients, configuration/schema versions and migration behavior when a feature changes an interface. Give obsolete behavior an explicit replacement and an owner for removal. Tie any future deprecation date or user communication to actual agreement.

Suggested owner: Implementer and feature owner. Trial: one schema or interface evolution. Success signal: existing consumers remain supported or have a verified migration path, and support obligations survive project closure.

### F18: Feed escaped defects back into the workflow

Link a defect found after delivery to the original requirement, assumption and evidence gap. Distinguish a product defect from deployment, fixture, source-data or process errors. Preserve the failing case and add the smallest useful regression or acceptance correction.

Suggested owner: receiving maintainer with Verification. Trial: one real escaped defect or an explicitly synthetic exercise. Success signal: the recovery is verified and the feedback reaches the next relevant project. Monitoring and human notifications require their own applicable authority.

## Workflow improvement and governance

### F19: Measure whether a process improvement earns its cost

Extend the existing retrospective experiment register with baseline, trial cohort, observed effect and retain/revise/retire decision. Useful measures include verified outcomes, rework, escaped findings, active human effort, decision waits and evidence-preparation effort. Preserve differences in scope and conditions.

Suggested owner: workflow maintainer. Trial: one experiment across three comparable feature increments. Success signal: the next retrospective makes an evidence-backed decision. A small sample supports a local choice rather than a general productivity claim.

### F20: Version the workflow and remove obsolete ceremony

Record the workflow/profile version a project uses. Review changes for their effect on active projects before adoption. Periodically ask whether each recurring role, artifact, review or automation still has a useful purpose and whether its evidence can be captured during normal work.

Suggested owner: workflow maintainer and Coordinator. Trial: one profile revision with an in-flight project. Success signal: that project retains a clear contract, and the review identifies a justified simplification or explains why existing controls remain valuable.

### F21: Keep publication adapters separate from project completion

Treat local files, a documentation site and approved collaboration systems as destinations for views of the project record. Configure the required audience and outputs per project. Track publication/readback as an obligation when selected, while keeping restricted evidence out of a public reusable kit.

Suggested owner: Coordinator and documentation owner. Trial: a local project and a separately authorized collaboration-system output. Success signal: both use the same approved facts, with correct audience and source links and no duplicated authoritative state.

### F22: Limit concurrent projects by real bottlenecks

If several features run together, make shared writers, test environments, external dependencies and human decisions visible across projects. Limit started work when one of these resources is saturated. Allow useful independent preparation without creating more unfinished implementation.

Suggested owner: Coordinator. Trial: two features sharing one native verification environment. Success signal: clear ownership, no concurrent output corruption and observed queue times. Add a scheduler only after a simpler explicit queue proves insufficient.

### F23: Give delegated work a bounded, useful contract

Each assignment states the question or deliverable, allowed mutations, inputs, acceptance evidence, resources, dependencies and return condition. An assessor or preparation worker should not create a parallel implementation or a new approval layer. Size the active crew to genuinely independent work and the bottleneck's capacity. A shared desktop may remain one resource even when source workspaces are isolated.

Suggested owner: Coordinator. Trial: one milestone with preparation workers and one exclusive executor. Success signal: every assignment maps to a committed outcome, waiting workers have an honest reason and prepared results are either consumed or explicitly retired. Count neither preparation coverage nor agent availability as completed product execution.

### F24: Check whether the full acceptance promise is feasible early

For a large acceptance matrix, qualify a representative real path and the most constraining boundary before elaborate preparation for every case. Observe target capacity, permissions and recovery on the actual intended environment. Recheck affected assumptions after environment changes. Estimate the execution cost from observed runs while preserving unknowns.

Suggested owner: Investigator and Verification. Trial: a native workflow with low and high capacity boundaries. Success signal: infeasibility is exposed while alternatives remain cheap, and the user receives a concrete decision if the committed outcome cannot be met. Existing mandatory counts and physical-input obligations remain in the denominator until the user approves a change.

### F25: Keep independent review inputs free of peer conclusions

Separate factual execution/integrity helpers from code that writes review judgments. Review-packet references, including reachable scripts, must not expose another assessor's initial conclusions before the independent phase closes. Share source, raw results, known prior failures and the agreed contract. Exchange current conclusions only after independent initials exist. If exposure occurs, disclose and retain the affected reports, then obtain a properly independent reassessment where required; extraction cannot undo what was read.

Suggested owner: Verification and Coordinator. Trial: inspect a packet whose referenced helper contains a synthetic peer conclusion. Success signal: the leak is detected before distribution, while factual tools remain reusable. Do not rerun unchanged product checks solely to repair a review-input error.

## Candidate next discussion

Choose one small feature and one integration feature as pilots. Agree their profiles and project contracts, then observe where waiting, rework and uncertainty actually occur. Select follow-up backlog items from those observations. Keep implementation changes, workflow experiments and measured benefits separately identifiable.
