# Toolbox specification

Version: 0.2. Status: pilot workflow contracts, 24 October 2026.

Agentic Tools is one repository containing reusable workflows for bug fixes, feature changes and new products. Select a workflow by the question being answered, then select assurance by uncertainty and consequences. Project-specific commands and integrations supply the execution environment.

These contracts guide a coordinator; they do not launch agents or enforce transitions automatically. The existing gate checker still supports the extended feature format only. The new workflows require an explicit evidence review; profile-aware validation and dispatch automation remain future work.

The approved [AOS investigation and Concept Approval package](aos-concept.md) defines a shared runtime with bug-fix execution first, ChatGPT macOS for conversation, a live dependency/cost dashboard and Confluence project history. The [M0 prototype](aos-concept.md#m0-prototype-and-qualification-result) is implemented, with remaining qualification holds. The introduction, team and approval rules below also apply when a coordinator operates these contracts manually.

## Choose the work

| Entry point | Use when | Main path | Exit |
| --- | --- | --- | --- |
| [Bug fix](downloads/bug-fix-kickoff.txt) | Existing behavior violates an intended contract | Introduce → reproduce/investigate → Concept Approval → repair → verify | Demonstrated correction and affected regression evidence |
| [Feature change](downloads/feature-change-kickoff.txt) | An existing product needs agreed new behavior | Introduce → investigate/design → Concept Approval → implement useful increments → accept | Integrated capability meets the agreed contract |
| [New product](downloads/new-product-kickoff.txt) | Users, solution or feasibility need discovery | Introduce → discover/probe feasibility → Concept Approval → deliver first usable slice | Agreed first release is usable and has a receiving owner |

A bug report with ambiguous intended behavior starts with investigation. If resolution changes the agreed behavior, record that decision and continue as a feature change. Product discovery may stop, defer or narrow the proposal before implementation. A new-product workflow reuses the feature workflow for delivery increments; it does not create a competing development process.

## Choose assurance independently

| Profile | Suitable context | Minimum assessment |
| --- | --- | --- |
| Lightweight | Bounded, understood, readily reversible change with limited consequences | Author runs meaningful affected checks; one non-author assessor reviews the candidate, behavior evidence and limits |
| Standard | Changed interfaces, compatibility, multiple consumers or meaningful integration uncertainty | Independent execution and source review responsibilities, separate from authorship; one assessor may cover both if capacity and competence permit and the arrangement is recorded |
| Elevated | Sensitive mutations, shared live resources, difficult recovery or substantial uncertainty | Separate Verification, Reviewer and Challenger initials before exchange; explicit execution admission, recovery and resource ownership |

The coordinator recommends and records the profile with its rationale. Patch size and product age do not determine it. Prefer a small number of people or agents with clear responsibilities. A coordinator may also investigate or implement; an author cannot supply their own independent acceptance. When an independent assessor is unavailable, show the gap and the delivery boundary rather than claim independent signoff.

Reassess the profile when effects, environment or uncertainty change. Strengthen affected checks immediately within existing authority; obtain a focused decision if commitments or authority must change. A profile never waives target-repository requirements.

## Shared core

Before investigation, present the project outcome, phases/dependencies, investigation bounds, initial team and provisional execution team. Each member has a title, task, responsibility grade and rationale, proposed model/reasoning effort, deliverable, access/resources and stop condition. Responsibility grades express expected scope and judgment; they do not certify capability or grant permissions. Distinguish proposed staffing from actual launches and runtime-reported configuration. After Concept Approval, confirm the actual execution team and record changes within the approved staffing/budget envelope.

Use one [work contract](downloads/work-contract.txt), versioned when commitments change. It records:

1. Work ID, contract version, workflow/profile, source baseline and accountable owner.
2. User outcome, committed acceptance, exclusions, compatibility and receiving delivery boundary.
3. Authority, allowed mutations and publication audience; applicable repository instructions.
4. Target environment, native check recipes, relevant source versions, dependencies and shared-resource owner.
5. First useful increment, affected consumers, dependency-ordered tasks and stop/replan triggers.
6. Decisions, candidate identity, actual evidence, unresolved limits and next owner/action.

Keep the record in the task's approved working location. A small task can use a compact message and evidence links; a larger project can use maintained files. Preserve prior revisions and answers. A new Git commit is useful only when authorized; an exact base plus complete scoped patch, new-file inventory and relevant hashes can identify an uncommitted candidate.

## Decisions and handoffs

Every new project starts with a proportionate Detailed Investigation and explicit user Concept Approval before implementation. Present observed behavior, the proposed solution and alternatives, test strategy and acceptance, ordered plan, team/model settings, resource boundaries, estimates/uncertainty and stop conditions. Bind the decision to the exact package revision and record the user's answer once. A broad repair request is not a waiver; only an explicit task-specific waiver can replace this gate. An existing approval for that exact package need not be repeated. Silence and elapsed time never count as approval.

Investigation and explicitly bounded feasibility probes may proceed within their stated authority before Concept Approval. Routine implementation and corrections within an approved concept continue autonomously. Material changes to behavior, acceptance, authority, assurance, staffing envelope or budget receive a focused decision; retain settled approvals and unaffected work.

Each task has an owner, deliverable, prerequisites, mutation/resource boundary and exit evidence. States distinguish ready, assigned, acknowledged, active, waiting, done and failed; unknown observations remain unknown. A sent assignment is not observed activity. A completed assessment is not accepted behavior.

On completion, the coordinator reads the result, checks candidate/scope, records disposition and resource release, then dispatches ready authorized work. A wait names its prerequisite and owner. Documentation can proceed beside implementation when inputs are stable; it blocks only transitions whose contract requires it. One owner writes product source and one executor owns shared outputs at a time, unless actual isolation has been demonstrated. Separate source trees do not necessarily isolate a desktop, service or database.

## Evidence and acceptance

Run cheap normal builds and safe affected checks early within the agreed execution boundary. Treat bring-up as part of each development task: verify its build, intended behavior, relevant failures and immediate integration before calling the candidate ready. Code written with required checks unrun or failing remains in progress or blocked. Independent milestone assessment checks the tested candidate and composed behavior; it is not a substitute for unfinished author bring-up. Tests that mutate live resources still require their applicable admission. Freeze a candidate for acceptance; record command, environment, executor, result, selected scope, retained output and limits. Changed source invalidates affected evidence; justify reuse by dependency impact, including unchanged consumers.

Check the smallest observation that distinguishes the behavior and a plausible false positive. Separate source reasoning, model/helper tests, actual integration, physical interaction and delivery. Missing logs and explicitly unrun checks are different limits. Keep original failures and corrections. A confirmed shared-contract defect triggers a bounded sweep of direct consumers and immediate error/ordering boundaries.

Independent reviewers receive source, contracts and factual execution evidence. Referenced helpers must not reveal another assessor's current conclusions before initials are complete. Then exchange findings and resolving evidence. A record checker can establish consistency, not behavioral truth.

Accept only the agreed scope: required evidence and findings are resolved, selected outputs are delivered and remaining obligations are visible. An unavailable mandatory check holds that claim until resolved or explicitly changed by the decision owner. Do not rename a narrower local result as full release readiness.

## Optional integrations

| Integration | Default for these pilot workflows | When selected |
| --- | --- | --- |
| Documentation | Update affected maintained sources; generate/check using native project recipes when applicable | Record audience, sources, commands and semantic assessment; a separate new specification is optional |
| Dashboard | Off; concise task status is sufficient | Show the current milestone graph, evidence-backed states and separate observation/update/fetch ages |
| Recurring monitoring | Off | Explicit authority, actual schedule identity, meaningful-change notifications and termination condition |
| External publication | Off | Named destination, audience, authority and verified readback |
| Separate Journalist | Off | Useful narrative work with bounded updates and responsibility distinct from acceptance |
| Commit/install/deploy | Only existing explicit authority | Named boundary, owner and actual receipt; no inherited permissions |

Elevated assurance does not automatically enable these integrations. The [extended feature recipe](extended-feature-start.md) is a separate, explicit selection with its existing publication, monitoring, specification and retrospective obligations.

The proposed AOS setup explicitly selects the dashboard and Confluence history because the user wants those surfaces. The generic defaults above still suit projects that do not select them. Selected views share work/decision/candidate identities; dashboard cost distinguishes measured usage, estimates, actual billing and unavailable coverage. A Confluence destination and publication authority belong to the project contract.

## Delivery and learning

Demonstrate the agreed user journey, identify the final candidate and delivery state, disclose limits and name the receiving maintenance owner. Keep accepted, committed, installed and released states distinct. A brief learning note is sufficient for a small fix; select a fuller retrospective when the work warrants it or the user requires it. Any selected immediate actions must be executed and checked before their milestone closes. Deferred experiments have an owner, trigger and success criterion; they do not keep a finite feature open indefinitely.

## Repository and rollout

Keep one repository with common contracts in `templates/`, the guide and worked examples in `docs/`, and existing bounded checks in `scripts/`. Environment adapters describe commands, prerequisite discovery and resource effects without redefining acceptance. Add adapters only when a real project needs them. Keep private project data and source archives outside published examples.

First walk through the [worked examples](worked-examples.md). A manually coordinated bounded bug fix can exercise the contracts immediately. For service development, review the [AOS concept](aos-concept.md) before implementation: qualify a small integration, implement common transitions and recovery, then a bounded bug-fix path before selecting the real-project pilot. Record time to first meaningful feedback, handoffs, rework, useful findings and delivery gaps. At closure, decide which fields and automation helped before expanding to other workflows or a general scheduler. Existing running projects retain their agreed contracts until an explicit migration; this specification does not restart them or alter their acceptance.
