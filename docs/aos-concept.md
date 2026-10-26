# AOS: Detailed Investigation and Concept Approval

Work ID: AOS-001. Package: AOS-DI-001, revision 3. Date: 24 October 2026.

**Status: approved; M0 and the supervised HTTPS decision route qualified; Q1 simulated lifecycle independently accepted.** The original approved text and its digest remain separate from this maintained status. Following the user's approved extra cancellation repair, a non-author ran 169 native/retained checks and four additional recovery probes; all 173 passed with no material Q1 findings. The coordinator accepted the unchanged candidate, AOS component 0.2.3, on 25 October 2026. Earlier failures and approvals remain retained. The bounded Q2 real-adapter milestone, including sequential actual bug-fix and feature workflows, is now accepted locally. Q3 connected-desktop qualification and Herdr execution remain held. Source acceptance does not mean committed, installed or released. The [toolbox contracts](toolbox.md) remain manually operated; no existing project has been migrated.

## Decision in one minute

Build one local Autonomous Orchestration Service (AOS) in this repository. Keep ChatGPT macOS for conversation and decisions, the dashboard as the primary live overview, and Confluence for durable project history. Share project records, team planning, decisions, dependency transitions, evidence and recovery; implement the bug-fix workflow first. Initially admit one active project and one source writer. Connect a small AOS prototype through the app's existing tool mechanism before expanding the engine. Use a later, separately selected Tide & Tiles defect as the first real pilot.

Before investigation, show the project structure and initial/provisional team. Before implementation, require a versioned DI and the user's Concept Approval. After approval, confirm the actual team and coordinate within the approved boundaries. A generic instruction to fix a bug does not waive this gate.

Recommended implementation: Python, a transactional SQLite store, a small independently running worker, a thin MCP/plugin bridge and a replaceable Codex runtime adapter. Agent completion events drive handoffs; a cheap reconciliation timer catches missed events. Model calls supply investigation, implementation and assessment where judgment is required. Routine dispatch does not depend on a periodic prompt to this chat. This is a design choice, not an installed stack or a claim of qualified integration.

Approval authorizes local implementation and bounded validation of milestones M0-M3 below, including their proposed delegation, a reusable dashboard with usage/cost reporting, and Confluence archive preparation. It does not select a real defect, alter the running game/dashboard, authorize Git delivery, install a persistent startup service or publish anything externally. Actual Confluence writes require a named destination, audience and publication authority; local preview and simulated publisher checks can precede that selection. Connection setup remains subject to the client's actual permission prompts. If M0 fails, stop at its decision packet.

## Project introduction and team

The investigation and design review for this package were performed by the current assistant as coordinator/investigator. The user reported selecting Astra / Extra High for that review; this is a user-reported setting, not a runtime receipt. Following approval, one technical lead/implementer was launched with requested settings `gpt-6.1-sol` / `high` as the sole runtime source writer. The coordinator owns integration qualification and maintained documentation. A separate assessor with the same requested model/effort independently verified and reviewed the M0 candidate and the retained runtime evidence. Returning this chat to GPT-6.1 Sol / High was recommended at the implementation boundary; its current model is not independently observed.

| Proposed member | Responsibility level | Deliverable and boundary | Model / reasoning effort | Activation |
| --- | --- | --- | --- | --- |
| Coordinator / project lead | AOS-G5 | Contract, task graph, staffing, decision consumption, resource ownership and next action; no self-acceptance of authored changes | `gpt-6.1-sol` / `high` | M0-M3 |
| Investigator / technical lead | AOS-G5 | Interface qualification, design corrections, dependency and failure analysis; later becomes the sole Implementer | `gpt-6.1-sol` / `high` | M0; then implementation after its exit |
| Independent Verification and Reviewer | AOS-G4 | Independently execute affected checks and assess source, evidence and limits; never author the candidate being accepted | `gpt-6.1-sol` / `high` | M0-M3, with separate execution/review records |
| Challenger | AOS-G5 | Independent failure-boundary assessment if shared live resources or other elevated effects are introduced | `gpt-6.1-sol` / `high` | Only after elevated assurance is selected and staffed |

Standard assurance is proposed for the local service because it combines durable state, integration and process execution. One non-author assessor may cover Verification and Reviewer responsibilities, with both reports explicit. Native game interaction requires a fresh effect assessment; elevated assurance adds separate Verification, Reviewer and Challenger. A materially expanded crew or changed assurance/authority needs a focused decision.

These are responsibilities for developing AOS-001. They do not imply continuously running agents or that the unbuilt service coordinates its own implementation. The prototype's fixture runs have separate bounded assignments and evidence. For later projects the service owns dispatch; its coordinator agent is invoked for bounded planning or unresolved findings, rather than kept alive to poll other agents.

The AOS responsibility rubric is independently written: G3 owns well-defined tasks and escalates ambiguity; G4 owns a bounded change and assesses relevant interactions; G5 owns a complex outcome or coordinates delivery across assignments; G6 owns architecture or capability spanning several projects. These labels describe assigned expectations. They do not certify human-equivalent competence, determine model strength or grant permissions. Grade, model, effort, domain experience and observed performance are separate fields.

Every team plan records assignment scope, dependencies, access/resources, deliverable, stop trigger, independence constraints and status. Show proposed, launched, acknowledged, observed-active, waiting and finished distinctly. Record requested and runtime-reported model/effort separately; use unknown when the runtime cannot expose a value. A replacement member gets the approved contract, factual evidence and outstanding obligations. Ordinary reassignment inside the approved staffing/budget envelope is recorded without reopening settled approval.

## Investigation observations and remaining uncertainty

The table records the pre-implementation investigation. The subsequent M0 results are recorded below; installation alone does not qualify the client, runtime or human-decision path.

| Evidence inspected | Actual observation | Consequence |
| --- | --- | --- |
| `pyproject.toml`, repository contents | Python 3.12+ documentation project; no AOS package, worker, MCP service or scheduler | Add a bounded runtime rather than assume one exists |
| `scripts/check_gate.py` | Extended-format structural validator; it neither dispatches agents nor authenticates evidence | Preserve compatibility; implement separate profile-aware transitions |
| `scripts/docs_hooks.py`, MkDocs navigation | Canonical templates become downloadable text; generated downloads/site are ignored | Keep guide and template sources aligned |
| Local CLI help/version | Codex CLI 0.163.0 is installed; `exec` and experimental app-server interfaces are exposed | A runtime is available for qualification; no agent invocation was tested |
| Project Python environment | `openai_codex` and `mcp` are not installed | Dependency pinning and native bootstrap belong to implementation |
| Existing coordination audit | Completed-report handoffs, misleading activity state and shared native resources need explicit handling | Prioritize completion consumption, honest status and ownership |
| Live reference dashboard, read-only browser inspection | Dependency graphs, decision panel, recorded-state age and document links are visible; usage/cost is reported unavailable | Preserve the useful graph interaction, make cost coverage visible and derive current views from service records |
| Current interface and proposed service | This conversation already runs in ChatGPT macOS with tool access. The proposed AOS service and its tools do not exist yet, so their round trip, worker lifetime and human-decision handling are untested | M0 implements and checks our AOS integration; the app's existing chat/tool functionality is established |

CLI help emitted a sandbox warning about creating PATH aliases. The read-only commands completed; this does not qualify an SDK environment or an agent launch. Account/model access, SDK/runtime compatibility, approval callbacks, computer-use availability and usage metering remain untested.

Official documentation describes a Python Codex SDK with a pinned CLI runtime and local thread control. It also describes MCP tools and local-source plugin testing. These establish candidate interfaces, not availability in this account or successful use in the exact macOS client. See [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk), [MCP server development](https://developers.openai.com/plugins/build/mcp-server) and [plugin connection/testing](https://developers.openai.com/plugins/deploy/connect-chatgpt).

The [app-server reference](https://learn.chatgpt.com/docs/app-server) documents terminal turn events and stored thread/turn reads. They provide candidate mechanisms for event-driven dispatch and reconciliation. A terminal event can report success, interruption or failure; it does not establish that the assigned outcome passed assessment. M0 must establish which of these operations the pinned SDK/runtime actually exposes.

## M0 prototype and qualification result

The first candidate adds the `aos` CLI and a small package under `src/agentic_tools/aos/`. It stores introductions, versioned concepts, their canonical digests, approval challenges and decisions in SQLite. Stable operation IDs support retry; stale revisions and worker-supplied decisions are rejected. The stdio MCP bridge exposes short commands and status. It has no dispatcher, independent worker, dashboard connection or archive publisher yet.

Bootstrap and inspect through the repository's native environment:

```sh
uv sync --locked --dev
uv run aos runtime-capabilities
uv run pytest
```

The maintained [coordinator command guide](aos-operator.md) covers the CLI route for exact concept/envelope registration, HTTPS decision preparation, guarded admission, status/version inspection and pause/resume/cancel through the existing engine. Its disposable checks use simulated outputs; actual-host qualification and Herdr admission remain separately held. Workflow mutations are not added to the MCP bridge.

Start the prototype with `aos serve --state-dir <private-directory> --worker-writable-root <fixture-directory>`. Declare each worker-writable root. The authoritative directory must be outside those roots, owned by the user and private; permission bits alone do not isolate processes running as that same user. Actual worker sandbox denial must also be qualified. The `status` command reads an existing work record; `--audience worker` exposes only read tools and opens the database read-only.

A local, ignored `.codex/config.toml` configures the `aos` server for a synthetic fixture. Project-scoped MCP configuration is a [supported connection mechanism](https://learn.chatgpt.com/docs/extend/mcp). The tools became available in this chat without an app restart or an invoked reload. Actual tool calls introduced synthetic work, presented its exact concept and read the same durable record. A fresh separate stdio process also read that work's revision and digest with no decision recorded. No persistent startup service was installed, and the active game chat was not altered.

The default approval path rejects requests with `human_confirmation_unqualified`, including the actual synthetic request from this chat. The separate `probe_confirmation_host` form only tests transport and never records approval. Its first actual-chat call returned unavailable; the user explicitly reported “No form appeared.” Investigation found that the probe accessed an accepted-response field on decline/cancel responses, masking the underlying action. Diagnostics version 2 fixes that defect and distinguishes decline, cancellation, timeout and protocol errors while exposing only bounded client/capability metadata. The corrected actual-host probe subsequently returned `action=cancel`, `response=null` and no recorded decision; the user again reported that no form appeared. This does not establish a visible human choice.

An elicitation response may be generated by a client automatically: acceptance, a client name or an operator flag alone cannot establish human provenance. Qualify the actual visible human response before configuring a trusted host; the real approval form must bind the exact current package. This remaining test does not reopen the already approved AOS-001 implementation decision.

The documented [app-server MCP reload](https://learn.chatgpt.com/docs/app-server) queues a refresh for loaded threads, and the pinned request schema has no server/thread selector. It was not invoked because the user required the running game chat to remain undisturbed. A temporary second project-only diagnostic entry did not appear in this active chat and was removed. No safe, exposed control for reconnecting only this chat's AOS server was established.

Results observed on 24 October 2026:

| Check | Observed result and limit |
| --- | --- |
| Internal prototype | 33 tests pass, including real stdio process restart/reconnect, durable records, idempotency, stale/racing decisions, forged arguments, worker restrictions and corrected host diagnostics. Independent verification and source review found no actionable defect. One upstream MCP/Pydantic warning remains. |
| Installed runtime | SDK and its bundled CLI are pinned to 0.163.0. Actual initialization and model catalogue reads succeed. |
| Worker scope | A named permission profile permits the fixture and required runtime/policy reads, denies private state and credential directories, and disables inherited MCP servers, plugins, hooks, additional agents and tool network access. Both a deterministic sandbox command and the second real agent fixture permit the intended write and reject private-state read/write and network binding. |
| Real completion, identity and usage | Four turns report `gpt-6.1-sol` / `high`, deliver terminal events and token observations, and remain readable through a new runtime connection. The first stopped before its fixture because the restricted environment lacked `PATH`; the second uses an explicit command path environment and passes all four fixture checks. |
| Active status and interruption | The original probe missed interruption because the startup marker arrived only in final output. The corrected observer detects the named in-progress command, reads active status and interrupts the turn. Both later turns retain `interrupted` after reconnect. Earlier unsuccessful attempts remain in the evidence. |
| Command cleanup | Turn interruption alone left the fixture process observable. The fourth turn lists the same thread-owned command before and after interruption, terminates its exact process ID, observes an empty registry and verifies OS-process absence before and after client closure. This qualifies one simple child fixture, not detached trees or independent worker lifetime. |
| Chat record access | Actual introduce/present/read succeeded for `AOS-M0-CHAT-001`; a separate fresh stdio connection retrieves the same work revision 2 and concept digest. No app restart or global reload was used. |
| Human confirmation | The original MCP elicitation probe remained unqualified. The user selected a supervised local HTTPS decision tab inside Codex. The disposable Q0-HTTPS-001 decision was answered by the user, durably read back in a fresh process, checked against TLS/worker-denial evidence and independently assessed. This qualifies that supervised route only; autonomous dispatch remains unavailable. |

The user explicitly expanded the original two-assignment allowance for additional and longer validation. Two further smoke turns were used, each with a five-minute timeout. Across all four turns, reported execution is 69.168 seconds and probe wall time is 76.249 seconds. Counting only the last cumulative observation for each unique turn gives 142,545 input tokens and 1,262 output tokens (143,807 total). Cached input (83,814), cache-write input (58,701) and reasoning output (224) are reported categories/subsets, not additional tokens to add to that total. These observations exclude the main chat and development/assessment agents. API-equivalent cost is unpriced and actual billing is unavailable. A failed metadata helper attempt exited before starting its runtime; the corrected metadata-only probe succeeded and started no model turn.

The future runtime adapter must distinguish turn interruption from command cleanup. The pinned runtime accepts experimental `thread/backgroundTerminals/list` and `/terminate` through raw RPC even though its Python generated models omit those methods. Use the loaded assignment thread and listed process ID, preserve unresolved ownership and verify effects before resource reuse. `command/exec/terminate` has a different process-ID contract; closing the SDK client alone does not prove descendant cleanup. These are qualified interface findings, not an implemented production adapter.

**M0 exit and next step:** the corrected MCP probe showed no visible human form. The user subsequently chose a local HTTPS decision tab inside Codex; D-HOST-01 revisions 1 and 2 were withdrawn, and the user's direct side-tab implementation instruction is retained against revision 3. The tab uses the existing SQLite challenge/decision ledger and a loopback TLS controller. Local tests cover exact-package display, wrong-token/cross-origin rejection, replay, expiry and restart without approval. The disposable Q0-HTTPS-001 request recorded Approve, and a separate process plus independent assessor read back its exact digest and decision ID. The user directly confirmed “Yes, I pressed Approve.” The actual worker sandbox denied the endpoint, ledger and TLS key; account trust and invalid-certificate failure were checked. The independent assessor qualified the supervised human route with the explicit limitation that it has no independent identity credential. The retained private Q0 qualification receipt grants no Herdr or dispatch authority. M1–M3 now require the approved disposable lifecycle and recovery tests.

**User attention:** D-Q1-EXT-01 revision 2 is approved, with its exact HTTPS decision independently read back. It permits one additional cancellation repair and one non-author reassessment within the existing limits. An earlier rejected click remains retained as capture-recovery evidence; it was not treated as approval. The Q0 fixture does not approve Herdr or enable dispatch. The approved AOS design, pilot amendment revisions 1–2, validation extension and side-tab direction remain recorded. The read-only dashboard shows the approved decision separately from technical lifecycle blockers; refreshing it does not run agents or accept approvals. Chat B's Herdr v0.3 concept is approved, with execution held for the required disposable workflow demonstration and the user's later instruction there to continue. Proposed AOS-PILOT-AMENDMENT revisions 3–4 are not adopted by this status update.

**Later pilot progress, 25 October 2026:** a focused decision-capture correction passed independent assessment (313 tests, 23 rejection checks and native worker-access probes). An unchanged proposal can now be answered on the first click after a two-hour absence; browser credentials remain bound to the exact proposal and server session. A narrowly authorized historical startup approval was recovered with its true chat provenance after backup and disposable rehearsal, then read back in a new process. The subsequent isolated counting-bug workflow completed with two actual agents, an automatic handoff, independent source review and three fixed native checks. This is local source/workflow acceptance, not installation or release. The subsequent feature test also passed with two actual agents, three new-behavior and two compatibility checks, separate source review and released resources. Q2 exit is accepted; Q3 connected lifecycle, dashboard/archive and safe desktop acceptance remain outstanding, with Herdr admission held. The capture-governance confirmation remains an external chat record; its old ledger request is not covered by the narrow recovery and must not be treated as a new decision.

## Outcome and scope

The user can submit one bug-fix request in ChatGPT macOS, inspect the structure/team, receive a DI, explicitly approve its exact revision, follow coordinated execution and usage in the dashboard, and inspect the final result with its historical record. Closing/reopening the conversation or dashboard retrieves durable state. Worker operation while the app is closed must be demonstrated on an awake Mac; sleep, logout or process termination are interruption/recovery cases.

First supported delivery is a local candidate with assessment and evidence. Include pause/resume/cancel controls, exact waits, preserved failures and a receiving owner. Source review, simulated tests, actual integration, physical interaction, commit and installation remain separately reported claims.

Feature-change/new-product execution, concurrent project scheduling, a separate project-management application, public hosting, cross-host scheduling, automatic deployment, unattended native desktop actions, public plugin submission and a permanent startup installation are deferred. Their extension points may exist without claiming those capabilities are implemented. Existing extended projects keep their contracts.

## Three connected user surfaces

The selected AOS setup includes all three surfaces. The dashboard is a first-release requirement, rather than an optional future enhancement. Keep each surface useful on its own and link it to the same work ID, concept revision, decision IDs, task IDs and candidate identity.

| Surface | Primary purpose | Update rule |
| --- | --- | --- |
| ChatGPT macOS | Request work, discuss trade-offs, inspect the team/DI, answer decisions, pause/resume or redirect work | Read current AOS state when answering status questions; record accepted commands and exact human decisions once |
| Local dashboard | Main view of current outcome, dependencies, owners, waits, evidence, decisions, usage/cost and service health | Render a consistent service snapshot with observation times; ordinary refresh never invokes a model or drives dispatch |
| Confluence | Retain approved DI versions, material decisions, milestone outcomes, final delivery and proportionate lessons | Publish bounded snapshots at meaningful milestones to the selected project location; show source revision and publication status |

AOS owns execution and approval state. Git owns product source; retained artifacts hold execution evidence. Chat responses, dashboard views and Confluence publications refer to those records. A later page edit or old chat summary cannot silently alter an approved plan. Discuss a proposed amendment in chat and record the new decision before changing affected work.

The dashboard should preserve the current dependency-graph approach: an overall stage graph, expandable detail for the current milestone, task owners, wait reasons and evidence links. Keep zoom/pan and the inspected task stable across refreshes. Put current stage, next action, pending decisions and a compact cost summary above the graph; provide team and cost breakdowns underneath. Keep historical detail collapsible. Select a task to see its assignment, model/effort, observed state, evidence and attributable usage.

Initially, dashboard decisions link back to the corresponding chat request; the dashboard remains read-only. A future approve/pause control would use the same authenticated command and revision checks, with no second approval ledger. Confluence links back to the relevant project/dashboard where reachable and clearly identifies snapshots as historical. Do not expect a localhost dashboard link to work for readers on another computer.

### Usage and cost

Always show the cost section, including when measurement is unavailable. Begin with the fields below and reuse the [usage glossary](dashboard.md#usage-column-glossary).

| Field | Meaning and limits |
| --- | --- |
| Project budget and remaining allowance | Approved units and limits; distinguish enforceable time/turn limits from any financial target |
| Recorded model usage | Provider-reported input, cached input and output categories, with source and observation time; reasoning/cached subsets must not be counted twice |
| Estimated API-equivalent cost | A labelled estimate from compatible usage and a versioned model/rate table, including currency, effective date and service-tier assumptions; it is not a subscription bill |
| Actual billed amount or credits | Display only when an authoritative, attributable billing source supplies them; otherwise unavailable |
| Breakdown | Per task, stage, role and actual model; include coordination, investigation, verification and failed/retried work rather than only the final implementer |
| Coverage and freshness | Which assignments/time ranges are captured, excluded or unpriced, collector health and last observation; partial totals remain visibly partial |
| Forecast to finish | Initially unavailable; later a range with assumptions and confidence based on representative work, never a task-count percentage presented as cost |

Usage collection belongs at the runtime adapter, keyed by assignment and runtime turn. Preserve raw observations and normalize cumulative updates/resets before aggregation so replay or restart cannot double charge an estimate. Keep historical rate assumptions with each estimate. Usage for this interactive chat or other processes is included only if a scoped collector can actually attribute it; otherwise disclose the exclusion. Responsibility grades do not imply billing rates.

Missing data displays unavailable or unpriced, not zero. Collecting partial usage must not claim a hard monetary cap. Enforce the approved observable limits; if an explicit hard financial limit is a prerequisite that the runtime cannot enforce, hold new spending work for a decision. Ordinary telemetry outages show a visible gap and do not erase evidence or falsely fail product behavior.

### Confluence history

Configure the project parent, audience, permitted content and publication boundary once for the selected work. Prefer one project page with concise milestone sections; create child pages only when the DI or final report warrants them. Prepare the introduction/team, approved concept, material changes and final delivery/lessons from the same records. Do not publish every agent message or raw private log.

Publication uses a separate queued obligation with a stable source revision. Retry transient failures without duplicating pages, verify the saved content/version, and preserve human edits by using designated generated sections or explicit version-conflict handling. Publication failure leaves local work available and is shown as pending/failed; it blocks only a milestone whose contract requires the archive. Full workflow closure retains any selected publication obligation. Confluence availability does not control routine dispatch.

## Proposed structure

```text
ChatGPT macOS <-> MCP/plugin bridge <-> shared AOS core and project store
                                          -> local dashboard snapshots
                                          -> Confluence archive snapshots
                                          -> dispatcher and resource ownership
                                              -> runtime adapter -> agents
                                          -> bug-fix workflow + project adapter
```

Approved source layout; components are added as their milestones are reached:

```text
src/agentic_tools/
    core/                 contracts, team plans, decisions, transitions
    store/                SQLite records, events, dispatch outbox
    worker/               scheduling, reconciliation, resource ownership
    workflows/            bug_fix first; common lifecycle interface
    adapters/             Codex runtime, usage, project recipes, archive publisher
    interface/            MCP tools, dashboard and chat-facing projections
tests/                    transition, recovery, adapter and integration cases
docs/ and templates/      maintained contracts and user guidance
```

Keep runtime data in a configured private state directory outside tracked/public documentation; validate its exclusion before recording any project data. Use ordinary current-state records plus a small event/dispatch journal; a general event-sourcing framework, broker and workflow language are unnecessary for this release. Project recipes identify the actual repository, native commands, effects, prerequisites and resources. They cannot weaken acceptance or grant authority. Discover a Tide & Tiles adapter only when its real pilot is selected.

The Python core follows the existing project language; SQLite supplies local transactions without a separate database service. Pin qualified runtime/MCP dependencies in the native project environment and record the effective CLI/SDK versions. Prefer the SDK's pinned runtime; choosing the existing CLI explicitly requires a compatibility check. Keep that choice behind the adapter. A mock adapter qualifies engine behavior, not real agents.

The bridge handles short commands/status requests. The worker owns ongoing execution independently of the bridge process and the current chat turn. Prefer a local-source plugin connection if supported in this client; a private tunnel is an alternative requiring its own confirmed setup. A public endpoint or remote hosting would change the approved architecture and requires a new decision. Do not infer background service lifetime from a stdio MCP process.

Qualify the service's agent tools and permissions separately from this conversation's tool catalogue. The desktop and CLI can [share MCP configuration](https://learn.chatgpt.com/docs/extend/mcp), so inherited tools must be inspected and restricted before launching a worker. Access to the current chat's browser, connected services or human-decision capability is not presumed. The first fixture needs only scoped local source/check access and the selected model runtime.

## Lifecycle and approval

`introduced -> investigating -> awaiting_concept_approval -> ready -> executing -> assessing -> delivered -> closed`

Waiting, failure, pause, cancellation and recovery are explicit conditions with an owner and next action. Changes requiring a revised concept return affected work to the approval gate; retain the original decision and evidence. A small approved repair iteration returns to execution without repeating unchanged concept approval.

1. Publish the introduction/team before any agent assignment. An investigation request authorizes only its bounded investigation; native effects and exploratory code require their stated scope.
2. The DI contains observed behavior, alternatives, recommendation, compatibility, test strategy, ordered tasks, resource ownership, team/model settings, effort/budget limits and stop criteria. Unknown cause or unavailable prerequisites remain visible.
3. Bind Concept Approval to work ID, DI revision/digest, acceptance, team envelope and authority. Record the actual human answer and provenance once. A generated summary or worker assertion cannot approve it. Silence, timeout, stale approvals and broad past Git permission do not authorize execution.
4. Before dispatch, validate the exact approved revision, dependencies, candidate, permissions, available independence, budget and resource ownership. Confirm launched model/effort or show the observability gap; never silently substitute a configuration outside the approved envelope.
5. Ingest a completion once and transactionally record its next required action. If assessment is needed, that action is an assessment assignment. Record the resulting assessment and enqueue the eligible follow-on work in a subsequent transaction. Never hold a database transaction open across a model call or external command. A reported completion is not automatically accepted behavior.

The human-decision path is an M0 qualification requirement: the client must show the exact package and preserve an explicit user confirmation through an appropriate host confirmation or interactive approval mechanism. Worker credentials have no decision capability. A general tool-use permission or an authenticated model call is not itself Concept Approval. The server must bind the affirmative response to the current package. If this cannot be demonstrated, report the gap and hold implementation dispatch; choose a concrete supervised mechanism with the user before proceeding. Text claiming "the user approved" is insufficient. Tool annotations assist the host but [do not replace server-side authorization and confirmation](https://developers.openai.com/plugins/build/mcp-server).

Enforce the boundary in the runtime as well as the dispatcher: investigation/review assignments have read-only product source and scoped output/cache locations for permitted checks; only the approved product writer receives source mutation access. Keep the authoritative store outside worker-writable roots, deny worker state-file mutation and withhold decision credentials and capabilities. A worker must not recover those credentials through filesystem or tool access. Verify effective permissions; a prompt instructing an agent to obey approval is not an enforcement mechanism.

Proposed chat-facing operations: introduce work, start investigation, read status/team/evidence, present concept, record a human decision, pause, resume and cancel. Mutations require stable operation IDs and expected revisions. Repeated requests return the existing result or a conflict. Status shows the latest factual observations, their age, next action and pending user decision. Optional push notifications require separate qualification and selection; status retrieval is sufficient initially.

## Handoffs without a chat watchdog

The user's current five-minute watchdog wakes an orchestrator chat to discover missed handoffs. Replace that dependency in the service with completion-driven dispatch and a separate recovery check. Keep an existing watchdog as a temporary fallback during qualification; this proposal does not change any running automation.

| Part | Responsibility | When a model is needed |
| --- | --- | --- |
| Runtime event adapter | Correlate start, terminal result and failure with the assignment and runtime turn; persist receipts | No model call to receive an event |
| Durable dispatcher | Validate current approval, prerequisites, ownership and limits; enqueue the next permitted assignment or record its blocker | The selected assignment may require an agent; deciding that an already approved verification task is ready does not |
| Reconciler | Compare unresolved assignments, saved results and process/resource observations; recover missed completions and stranded dispatch intents | No model call for a healthy check; unresolved semantic findings can create a bounded coordinator assignment |
| Coordinator agent | Resolve conflicting findings, propose a changed plan or prepare a focused human decision | Only for such a concrete deliverable, within its assignment limit |

For a normal repair the sequence is:

```text
Investigation result -> present DI -> wait for Concept Approval
Approved implementation -> candidate ready -> independent assessment
Assessment accepted -> delivery record
Confirmed in-scope defect -> bounded repair -> reassess affected evidence
Ambiguous finding or changed scope -> coordinator decision packet
```

Every arrow runs from durable state and current authority. The worker starts or resumes the required runtime turn; merely sending a message to an idle agent is not dispatch. Assessment verdicts identify the candidate and evidence. Missing or contradictory required fields hold acceptance and trigger a specific follow-up, without converting a textual "done" into a pass. A pilot contract should allow at most two corrective repair cycles after the first candidate; reaching that limit produces a decision packet with the retained findings.

Proposed initial reconciliation interval: 30 seconds while work is nonterminal. It checks local state and only the runtime assignments whose state needs reconciliation, with bounded queries and backoff on errors. A healthy idle check makes zero model calls and emits no user update. Intentional user/resource waits retain their reason; no timeout manufactures approval or source ownership. No recent output alone does not establish that an agent is dead.

Record completion receipt, transition eligibility, dispatch request and runtime acknowledgement separately. Initial qualification targets on an awake, available Mac are an eligible dispatch request within five seconds of consuming a completion, and recovery or an explicit fault within two reconciliation intervals when a dropped event is discoverable from runtime state. These are test targets, not measured performance or guarantees about model startup time. A slow or inaccessible runtime gets an explicit fault rather than a false completion.

Deduplicate terminal receipts and pending next actions. If a launch reply is lost, reconcile the saved launch intent with the runtime before another launch; an unknown live mutation stays held. Reconcile after restart before releasing resources or dispatching more work. Late results from an obsolete assignment/revision remain historical and cannot advance current work.

The timer is part of the independent worker, not a recurring chat prompt. If the worker itself dies, its timer cannot recover it: the bridge must report a stale service heartbeat/unavailable service on the next request, and recovery begins on an explicit restart. Automatic process supervision and proactive chat notifications are separate optional capabilities. When notifications are later selected, send only a meaningful decision, failure or completion; do not send healthy timer ticks. The initial service can continue authorized work without the chat being active, but it does not promise to wake this chat.

Before retiring a fallback watchdog, demonstrate normal handoff while the chat is idle, a dropped event, a duplicate event, lost launch acknowledgement, worker restart and an intentional approval wait. Retain timing and model-call counts. Reducing the watchdog interval alone would retain the original dependency on repeated chat turns.

## Evidence, recovery and resource rules

Each assignment/check records work/contract/DI revisions, source identity, executor, actual command/environment, selected scope, outputs, result and limits. Protect frozen source from the product writer until assessment ends. Source drift invalidates affected evidence; zero selected tests cannot pass a suite. Independent initial reports are preserved before exchanging conclusions.

Use durable events and an outbox for recoverable dispatch, with idempotent completion handling. This does not promise exactly-once external execution. On restart reconcile queued and in-flight assignments against runtime and actual resource state. An interrupted mutation is unknown until reconciled; never blindly retry it. Expired resource timestamps alone do not prove the process has stopped or the desktop is free.

One source writer and one owner per shared resource. Separate worktrees do not isolate a desktop or service. Pause prevents new dispatch and reconciles current work; cancel requests interruption and records acknowledged stops, released resources and unresolved effects before reporting cancelled. Restarting a deliberately paused run never resumes it implicitly. A second worker must fail ownership admission.

Use approved assignment time/turn limits and available token/cost telemetry. If monetary usage cannot be observed, show unknown and enforce observable turn/time bounds; do not claim a hard spend cap. Budget exhaustion holds further dispatch for a decision. Never reduce required checks automatically to fit a budget.

## Ordered implementation plan

The following are planning ranges for focused engineering sessions, not measured completion forecasts. A session is approximately 1-2 hours of active implementation/assessment; external setup and human waits are additional. Re-estimate after M0. Begin with a two-assignment/20-minute agent-execution cap for M0 smoke checks; subsequent live-agent limits must be recorded before dispatch.

| Milestone | Depends on | Owner / proposed effort | Exit evidence |
| --- | --- | --- | --- |
| M0: qualify the actual interface/runtime | Concept Approval for this package | Technical Lead + independent assessor; 1-2 sessions | In ChatGPT macOS, create/read synthetic work, present exact concept, bind explicit human response, reject worker/stale approval and retrieve state after reconnect; bounded read-only real-agent completion/status/interrupt checks, model/effort, usage availability and effective permission receipts |
| M1: smallest durable bug-fix path with simulated workers | M0 passes | Implementer + assessor; 2-3 sessions | One project traverses investigation, approval, candidate, assessment and delivery; completion/outbox, ownership, repair limit, pause/cancel and fault tests; consistent chat/dashboard snapshot and usage aggregation; no general task-graph editor or project mutation |
| M2: real bounded bug-fix execution | M1 passes | Implementer + assessor; 1-2 sessions | Synthetic fixture defect reproduces, gets its own DI/Concept Approval, is repaired by one writer, independently checked and delivered; live graph, honest cost coverage, milestone archive preview and failed/interrupted paths retained |
| M3: integrated desktop acceptance | M2 passes | Coordinator + assessor + user for decisions; re-estimate after M0 | Actual chat/dashboard journey, handoff with no new chat turn, app close/reopen, event loss/duplication, recovery, archive retry/conflict tests, limits and maintenance instructions; real Confluence publication/readback only after destination and authority are selected |
| P1: first real project pilot | M3 passes; user selects a defect and approves its own concept | Team/profile chosen from that defect | Tide & Tiles or another bounded real repair demonstrates correction and affected regressions at the agreed delivery boundary |

M0 is a feasibility checkpoint, not an invitation to build the full engine around an unavailable integration. A failed prerequisite yields observed facts and continue/change/defer options. Plan a supervised pilot before claiming unattended operation. The earlier 5-9-session total excluded the now-selected dashboard and archive integration; it is superseded and needs re-estimation after M0. Record time to first meaningful feedback and the effort to reuse the graph/usage components. No real-project date is promised before selecting the defect. An archive preview qualifies preparation only; live Confluence publishing remains unqualified until a real authorized write/readback passes.

## Acceptance and failure tests

| ID | Required distinguishing observation |
| --- | --- |
| A1: introduction first | User-visible structure, team, responsibilities, grades and proposed model/effort precede investigation dispatch |
| A2: human approval | Implementation request before approval, a worker-supplied approval and approval for an obsolete DI are rejected; exact current human approval admits eligible work |
| A3: honest staffing | Proposed/sent/acknowledged/observed-active differ; configuration mismatch is visible and cannot silently violate the approved envelope |
| A4: completion continuity | With no new chat prompt, a completion leads to one next permitted action or a named blocker; duplicate and dropped terminal events are handled, with eligibility/dispatch timing retained |
| A5: resource integrity | Competing writers/workers are rejected; interruption and expired ownership require reconciliation before reuse |
| A6: restart and controls | Crash after dispatch or completion, reconnect, user pause/resume and cancellation preserve decisions, unknown effects, ownership and next actions |
| A7: evidence and independence | Wrong candidate, failed/blocked/unrun required check, zero selected tests, missing output and unavailable required assessor prevent acceptance; corrected evidence can resolve the hold |
| A8: actual user journey | The fixture fails before repair and passes after; actual ChatGPT macOS flow and one real agent are exercised; model-only tests cannot satisfy this row |
| A9: scoped delivery | Final identity, actual checks, limits, receiving owner and accepted/committed/installed states are accurate; no implicit Git, publication or game mutation |
| A10: bounded coordination | Healthy reconciliation makes zero model calls; lost launch acknowledgement cannot cause blind relaunch; stale results cannot advance a revised project; repair-limit exhaustion and service death are explicit holds |
| A11: coherent dashboard and cost | Chat and dashboard agree on the same recorded revision/decisions; graph inspection survives refresh; stale facts and failed collectors stay visible; duplicate/reset usage observations do not inflate totals; partial/unpriced data never appears as zero or actual billing |
| A12: durable archive boundary | Milestone preview identifies its exact source revision; simulated retry and conflicting human edit cannot duplicate or silently overwrite content; authorized live publication requires saved-version/readback evidence, otherwise it remains explicitly pending/unqualified |

Run deterministic transition/recovery tests before live adapters, then recorded adapter integration tests and the actual client journey. The independent assessor judges whether evidence supports the behavior; passing schema checks alone cannot establish it. Native graphical game testing is a P1 obligation only if selected, with explicit ownership and recovery.

For the current documentation package, run the existing strict MkDocs build, template-download consistency, whitespace/link and bounded publication checks. Those checks validate this proposal's presentation and consistency, not the unimplemented AOS requirements above.

## Alternatives and principal trade-offs

| Option | Assessment |
| --- | --- |
| Continue manual chat coordination | Useful baseline and fallback; lacks a separately durable dispatch/recovery service |
| One shared core with a bug-fix path | Recommended; exercises common state/approval/resource problems with one bounded workflow |
| A separate service for every entry point | Repeats state, permission, recovery and integration work before evidence justifies divergence |
| Build a separate project-management application or general scheduler | Adds another interface and delays the requested chat/dashboard/history journey; reuse the graph pattern with a small service-backed view |
| Direct app-server protocol | Adds integration surface; use required documented methods behind the adapter if the SDK is insufficient, qualifying any experimental dependency explicitly |

The largest uncertainties are client setup/approval provenance, runtime capability exposure, actual process recovery and native resource effects. Early integration qualification contains them. Responsibility labels and stronger models do not remove these risks. Claims of faster delivery require pilot measurements; this design makes no measured productivity claim.

## Concept decision

Decision owner: user. Decision: **approved** on 24 October 2026, by the direct message “ok, start the work now!” Approved package: **AOS-DI-001 revision 3**. Original approved-text SHA-256: `5cb289a5cf5e6215ae5dc5ae871ab62af6a7a4730f05a88dac7b2c688068446d`. This digest identifies the retained approval snapshot, not this subsequently updated status page. Execution team: coordinator, sole technical lead/implementer and separate independent assessor have completed the current M0 candidate work. Remaining integration checks are held as described above.

The accepted decision covers local M0-M3 implementation with the standard-assurance team, model/effort settings, M0 bounds and selected chat/dashboard/archive surfaces. M1-M3 remain conditional on prior exits and recorded assignment limits. Confluence destination/authority and any revised effort or spending commitment must be settled before the affected work. A new project pilot receives its own introduction, DI and Concept Approval. Material changes receive a focused decision; routine work within the approved envelope continues without duplicate approval.
