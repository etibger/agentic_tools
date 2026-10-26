# Future workflow improvements

Status: proposals for prioritization. Research date: 24 October 2026.

The [toolbox specification](docs/toolbox.md), shared contract, three entry points and [worked examples](docs/worked-examples.md) now define a pilot application of these proposals. Profile-aware validation, automated dependency transitions and measured effectiveness remain future work. Existing running projects retain their own contracts until explicitly migrated.

The approved [AOS package](docs/aos-concept.md) has completed the bounded tool handoff for Herdr at component **0.2.4** / checked code `ce99e5d`: independent code assessment, actual two-agent Rust trial, preserved decisions/budgets, local dashboard/HTTPS adoption and receiving-chat connection all passed. Historical Python workflows and awake idle-worker desktop restart evidence retains its scope. Product-specific commands, permissions, tests and allowances remain with the owning project. Herdr’s approved v0.3 requirements and user execution hold are preserved; this handoff grants no product execution or budget. [Reproduction and host limits](docs/aos-validation.md) remain explicit. General install/update, automatic chat wake-up, backlog synchronization and live external archive publication remain future work.

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

### Issue and project index

Each R/F entry below is a maintained Markdown proposal with its original ID and history. Its corresponding issue is linked here; the [Agentic Tools project](https://github.com/users/etibger/projects/1) tracks triage. None of these proposals is complete solely because guidance or a partial implementation exists. The detailed audit remains supporting evidence rather than duplicate tickets. The AOS operational entries B01–B24 below supplement the proposals.

| Markdown IDs | Corresponding GitHub issues |
| --- | --- |
| R1, R2, R3, R4, R5 | [#1](https://github.com/etibger/agentic_tools/issues/1), [#2](https://github.com/etibger/agentic_tools/issues/2), [#3](https://github.com/etibger/agentic_tools/issues/3), [#4](https://github.com/etibger/agentic_tools/issues/4), [#5](https://github.com/etibger/agentic_tools/issues/5) |
| F01, F02, F03, F04, F05 | [#6](https://github.com/etibger/agentic_tools/issues/6), [#7](https://github.com/etibger/agentic_tools/issues/7), [#8](https://github.com/etibger/agentic_tools/issues/8), [#9](https://github.com/etibger/agentic_tools/issues/9), [#10](https://github.com/etibger/agentic_tools/issues/10) |
| F06, F07, F08, F09, F10 | [#11](https://github.com/etibger/agentic_tools/issues/11), [#12](https://github.com/etibger/agentic_tools/issues/12), [#13](https://github.com/etibger/agentic_tools/issues/13), [#14](https://github.com/etibger/agentic_tools/issues/14), [#15](https://github.com/etibger/agentic_tools/issues/15) |
| F11, F12, F13, F14, F15 | [#16](https://github.com/etibger/agentic_tools/issues/16), [#17](https://github.com/etibger/agentic_tools/issues/17), [#18](https://github.com/etibger/agentic_tools/issues/18), [#19](https://github.com/etibger/agentic_tools/issues/19), [#20](https://github.com/etibger/agentic_tools/issues/20) |
| F16, F17, F18, F19, F20 | [#21](https://github.com/etibger/agentic_tools/issues/21), [#22](https://github.com/etibger/agentic_tools/issues/22), [#23](https://github.com/etibger/agentic_tools/issues/23), [#24](https://github.com/etibger/agentic_tools/issues/24), [#25](https://github.com/etibger/agentic_tools/issues/25) |
| F21, F22, F23, F24, F25 | [#26](https://github.com/etibger/agentic_tools/issues/26), [#27](https://github.com/etibger/agentic_tools/issues/27), [#28](https://github.com/etibger/agentic_tools/issues/28), [#29](https://github.com/etibger/agentic_tools/issues/29), [#30](https://github.com/etibger/agentic_tools/issues/30) |

The earlier migration also created [AOS-STATE/#31](https://github.com/etibger/agentic_tools/issues/31), [AOS-INSTALL/#32](https://github.com/etibger/agentic_tools/issues/32) and [AOS-GITHUB/#33](https://github.com/etibger/agentic_tools/issues/33). Their Markdown entries are below. GitHub's Urgency and Importance fields are independent of project Status and of AOS execution authority. Unset priority fields mean untriaged, not low priority. This local Markdown update is awaiting Git publication; automated two-way reconciliation is not implemented. The manual baseline was read back on 25 October; the coordinator separately updated and verified B02/#35, B05/#38, B17/#50, B20/#53 and B22/#55 on 26 October, preserving their history and open state. The later native-completion code checkpoint was also manually updated and read back for B05/#38 and B24/#57; both remained open at that checkpoint. At final delivery, B05/#38 is accepted for the bounded tool handoff and closed / Done; B24/#57 is locally adopted and remains in review for observation during the next actual saved-answer interaction. Other entries are not implied to have a fresh remote readback.

### AOS operational backlog

| ID | Type and maintained outcome | GitHub issue | Current disposition and relations |
| --- | --- | --- | --- |
| AOS-STATE | Enhancement: put installed SQLite state outside the source checkout; migrate existing records and operation identities with safe backup/restore and worker-access boundaries. | [#31](https://github.com/etibger/agentic_tools/issues/31) | Open; source-checkout state remains a development default. Related to AOS-INSTALL and B03. |
| AOS-INSTALL | Documentation: a reproducible install/use guide including prerequisites, HTTPS certificate trust, version, state, restart and recovery. | [#32](https://github.com/etibger/agentic_tools/issues/32) | Open; guide is incomplete. B03 separately tracks an installable runtime. |
| AOS-GITHUB | Enhancement: track AOS bugs and improvements as public-safe GitHub issues with durable identity and truthful lifecycle links. | [#33](https://github.com/etibger/agentic_tools/issues/33) | Open; the initial 33-issue migration is only a manual baseline. B01 is the canonical two-way Markdown/GitHub synchronization outcome. |
| B01 | Enhancement: keep Markdown and GitHub backlog records synchronized and prioritized in both directions; preserve source IDs, conflict/failure receipts and visible freshness. | [#34](https://github.com/etibger/agentic_tools/issues/34) | **Highest priority: urgent and important.** Manual baseline only; depends on AOS-GITHUB. Priority never bypasses approval, qualification or resource gates. |
| B02 | Bug: cancellation can release a Q1 resource after a conflicting stop receipt without proving effects resolved. | [#35](https://github.com/etibger/agentic_tools/issues/35) | Repair accepted locally after independent assessment: 173 checks passed, including the original failure and additional recovery probes. Update 26 October: source is locally committed, not pushed, installed or released; current integrated-candidate assessment and actual desktop qualification remain separate. Issue remains open; original history is retained. Related to F10 and R4. |
| B03 | Enhancement: reproducible installable AOS runtime and HTTPS decision setup, with isolated clean-install/restart qualification. | [#36](https://github.com/etibger/agentic_tools/issues/36) | Open; depends on AOS-STATE and AOS-INSTALL. No installation authorized here. |
| B04 | Enhancement: qualify a real Codex runtime adapter and disposable bug/feature fixtures under Q2. | [#37](https://github.com/etibger/agentic_tools/issues/37) | Q2 milestone accepted locally after sequential real bug and feature workflows, automatic handoffs, separate source reviews, three bug checks and five feature/compatibility checks. Earlier failures remain counted. Issue stays open for the cross-cutting connected interruption/event-recovery evidence completed with B05; no installed/released or full-pilot readiness claim. Related to B20, B22, F10, F15 and F23. |
| B05 | Qualification: complete the connected AOS desktop journey and Herdr handoff. | [#38](https://github.com/etibger/agentic_tools/issues/38) | **Accepted locally; bounded tool handoff complete.** Earlier T1–T6 evidence is retained. At `ce99e5d` / AOS 0.2.4, 441 checks, the actual two-agent Rust build/three-test trial, automatic handoff, cleanup/replay, preserved original seven reservations, local service adoption and receiving-chat connection passed independent assessment. Exactly nine qualification jobs/45 reserved minutes are counted. Herdr requirements stay approved and execution held for user continuation and its separate product recipe/resources/allowance. No product implementation, general installation, push or external release is implied. |
| B06 | Enhancement: schedule selected work by priority, dependencies, authority, capacity and review dates. | [#39](https://github.com/etibger/agentic_tools/issues/39) | Open; related to F02, F04, F15, F22 and B01. No automatic scheduling is implemented. |
| B07 | Feature request: discuss an exact pending AOS decision within its HTTPS side tab without turning conversation into approval. | [#40](https://github.com/etibger/agentic_tools/issues/40) | Open for a later amendment; no chatbot integration authorized. Related to R2 and F11. |
| B08 | Deferred idea: explore separately designated AI decision authority in a future AOS workflow, with explicit scope, revocation and independent qualification. | [#41](https://github.com/etibger/agentic_tools/issues/41) | Open, unprioritized; today's exact-human approval remains mandatory. No agent may treat the current page as delegated authority. Related to B07 and R2. |
| B09 | External client request: heart/emoji reactions on assistant messages in Codex desktop chat. | [#42](https://github.com/etibger/agentic_tools/issues/42) | Open, unprioritized external dependency; native app feature is outside this repository. No AOS implementation or upstream feedback submission authorized. |
| B10 | Enhancement: explain decisions through the problem, practical consequence, options, effort and stopping point; retain technical identifiers and evidence in expandable details. | [#43](https://github.com/etibger/agentic_tools/issues/43) | Open; the current repair proposal and page presentation were corrected, but reusable template adoption and decision-owner qualification remain. Related to R2, F11 and B07. |
| B11 | Proposed enhancement: an editable private communication profile for the decision owner's preferred explanation level and examples. | [#44](https://github.com/etibger/agentic_tools/issues/44) | Open, unprioritized; presentation only, with current instructions taking precedence. No reusable profile loader or automatic personal-history collection is implemented. Related to B10. |
| B12 | Enhancement: refresh or replace an AOS chat connection and verify its effective version/capabilities and durable state without disturbing other sessions; qualify stale-version, failed-refresh and recovery cases. | [#45](https://github.com/etibger/agentic_tools/issues/45) | In progress, 26 October: AOS 0.2.5 adds a narrow coordinator MCP transport, independently reviewed and configured locally in both projects. Direct protocol readback preserves the original ledger. The receiving chat still has no AOS tools in its current inventory; actual desktop connection refresh and receiving-chat verification remain required. No completion claim from configuration alone. Related to AOS-INSTALL, B03, B05 and F17; no global reload authorized. |
| B13 | External client/integration request: open ordinary links in the selected system browser while retaining the explicitly chosen decision side tab; verify both routes and document actual client limits. | [#46](https://github.com/etibger/agentic_tools/issues/46) | Open, untriaged; first verify whether the installed client already supports the preference. No missing-feature claim, settings change or upstream submission. Related to B05, B09 and B10. |
| B14 | Enhancement: satisfy the existing AOS usage/cost obligation with attributable collection, replay/reset-safe aggregation, partial-coverage disclosure, versioned estimates and an always-visible dashboard breakdown. | [#47](https://github.com/etibger/agentic_tools/issues/47) | Open; existing acceptance requirement, not newly deferred scope. Smoke-test totals and simulated placeholders are partial evidence. Test duplicates, counter reset, interruption and unavailable prices; never imply unknown usage is zero or an enforced spending cap. Related to F11, F15 and B04. |
| B15 | Deferred enhancement: qualify a new-product workflow on the same shared AOS, with discovery/build-versus-adopt, a bounded first increment, independent assessment, recovery and a continuing product roadmap. | [#48](https://github.com/etibger/agentic_tools/issues/48) | Open, untriaged; the manual contract exists, but the simulated lifecycle accepts only bug-fix and feature-change. Requires later scope selection and a disposable end-to-end qualification; no product fork, silent migration or concurrency increase. Related to R1, F01, F05, F20, F22 and B04. |
| B16 | Enhancement: a verified decision durably triggers the owning coordinator/chat, which acknowledges it and resumes eligible work without another user message; queue safely if busy and reconcile after restart or lost delivery. | [#49](https://github.com/etibger/agentic_tools/issues/49) | Open, untriaged; no qualified wake-up bridge. Test actual side-tab-to-coordinator continuation, duplicates, lost acknowledgments, offline recovery and pause preservation. Show recorded/notified/queued/running separately; failed delivery is a technical blocker. Related to R4, F10, B05 and B12. |
| B17 | Bug: expired or invalid approval forms give an unhelpful generic rejection; show expiry and a safe recovery path for the same proposal, distinguishing rejected clicks from saved decisions with lost replies. | [#50](https://github.com/etibger/agentic_tools/issues/50) | In progress under the approved focused correction; a reported click was not saved after the ten-minute request window. Preserve the report and failure; never infer approval from timeout. Test expiry while reading, server restart, proposal changes and lost replies without weakening security. Related to B16, B10 and B05. Observed again: a delayed human click produced an unavailable page and no durable answer; the identical proposal was reopened only after readback confirmed no decision existed. Original failed interaction retained. A later repair approval was explicitly confirmed in chat after another form expiry; retain that human decision separately from the unresolved runtime capture, without impersonating a successful web response or asking for a repeat decision. The focused correction is independently accepted locally: 313 tests, 23 rejection checks and native worker-denial probes passed. First click after two hours is tested; the specified startup approval was recovered after a private backup and verified in a fresh process. Original failures and approvals are retained. General recovery is not implemented; the separate capture-governance chat confirmation still has a pending ledger entry. Update 26 October: the source is locally committed; no push, installation or release is claimed. Current integrated-candidate acceptance remains separate. |
| B18 | Enhancement: a plain-language glossary and abbreviation reference at the bottom of the shared dashboard, including project-specific codes. | [#51](https://github.com/etibger/agentic_tools/issues/51) | Local presentation implemented and served-page verified; in review, with broader responsive/integrated qualification outstanding. Not published or installed. Related to F11, B10 and B14. |
| B19 | Enhancement: include an effort/cost estimate with uncertainty and enforceable limits before Concept Approval. | [#52](https://github.com/etibger/agentic_tools/issues/52) | Open; existing GitHub request reconciled into Markdown. Estimate and execution authority remain distinct. Related to F04, F15 and B14; no new runtime scope authorized. |
| B20 | Bug: a passing native test can leave an ordinary child process alive after AOS records a clean stop. | [#53](https://github.com/etibger/agentic_tools/issues/53) | Correction independently accepted locally: 212 native/retained checks, eight new independent affected checks and 23 rejection cases passed. Original failure retained. Update 26 October: source is locally committed, not pushed, installed or released; bounded Q2 bug/feature workflows subsequently passed. The current integrated candidate and actual desktop/cutover remain separately held. Issue remains open. Distinct from B02; related to B04 and F22. |
| B21 | Research: assess current software-development approaches and evidence before selecting a philosophy for Agentic Tools/AOS. | [#54](https://github.com/etibger/agentic_tools/issues/54) | Deferred until Amendment 1 is complete and the backlog is consolidated/prioritized. Important, not urgent. Deliver a dated critical evidence synthesis, plain-language recommendation and bounded trial proposal; no methodology or runtime change is adopted by capture. Related to F08, F09, F15, F19, F20 and B19. |
| B22 | Bug: handle acknowledged-start status-read races and cancellation of already-terminal turns without duplicate launches or lost ownership. | [#55](https://github.com/etibger/agentic_tools/issues/55) | Live failure observed; exact interrupted turn and cleanup reconciled, source unchanged. Correction independently accepted locally: 249 native/retained tests, 23 additional independent boundary checks and 23 rejection cases passed. After independently accepted B17 recovery, the real replacement counting workflow passed with an automatic handoff, separate review, three fixed native tests and released resources. Update 26 October: the source is locally committed; no push, installation or release is claimed. Current integrated-candidate acceptance remains separate. Original failed attempt remains counted. Urgent and important; related to B04, B20, F10 and F15. |
| B23 | Investigation: explain and reduce AOS coordination overhead after AOS-001 completes. | [#56](https://github.com/etibger/agentic_tools/issues/56) | **Deferred until AOS-001 is explicitly complete**, then review for selection. Important, not urgent relative to finishing the pilot; B01 stays highest priority. Reconstruct effort, elapsed time, handoffs and forecast error without inventing missing attribution; distinguish necessary assurance from avoidable administration. Deliver evidence-backed causes and a small measured improvement proposal. Related to F04/#9, F10/#15, F19/#24, F20/#25 and B14/#47. |
| B24 | Enhancement: return to the decision list after saving an answer. | [#57](https://github.com/etibger/agentic_tools/issues/57) | Implemented, locally committed, independently code-accepted and adopted by the running HTTPS service at AOS 0.2.4. Same-tab GET navigation after approve/decline/cancel/replay is tested, including empty/nonempty lists, contrast and keyboard focus. Issue remains open for observation of the return control during the next real saved-answer interaction; no extra approval is manufactured for that check. Related to B17/#50, B10/#43 and B05/#38. No general installation or release claim. |

### Disposition of ideas raised during the first pilot

The supervised, human-readable HTTPS side tab is the qualified Q0 decision route; its use inside Codex avoids the earlier invisible pop-up problem (the user reported that Aerospace may move pop-ups to another workspace). The current route uses no CAPTCHA; exact-package approval still requires the user's explicit choice. Reproducible certificate trust and installation remain [AOS-INSTALL/#32](https://github.com/etibger/agentic_tools/issues/32) and [B03/#36](https://github.com/etibger/agentic_tools/issues/36). The requested Codex chatbot in that page is B07; possible AI-delegated decision authority is the separate, deferred B08. Emoji reactions in the Codex conversation are B09, an external app request. The readiness receipt and explicit handoff to the Herdr chat remain part of the approved pilot's Q3/P1 gates and [B05/#38](https://github.com/etibger/agentic_tools/issues/38); they are not admission by backlog status. These entries preserve the ideas without adding them to the current implementation scope.

A 25 October review of all four available chats in the Agentic Tools app project added B12–B15 and clarified F20/F22 below. Earlier installation, decision-page discussion, reactions, communication preferences, synchronization and scheduling ideas already had canonical items. Separate product features remain in their owning product's roadmap; private source conversations and data are not copied into this public register. This is a manual capture/readback snapshot, not an automated conversation scanner or a working backlog synchronizer. Existing approved obligations, including usage/cost reporting, retain their original acceptance status; capturing future ideas does not expand the current pilot.

A subsequent user-reported approval attempt exposed two distinct gaps: B16 tracks waking the coordinator after a successfully recorded decision; B17 tracks understandable expiry and capture recovery when no decision was saved. A successful save, successful notification and resumed execution must not be conflated. Adding these items does not implement a trigger or schedule a polling automation.

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

The 25 October dashboard discussion makes the intended discipline explicit: **planned integration, protected baselines and bounded regression verification** belong to the generic Agentic Tools workflow; AOS must enforce the adopted contract. F09/[#14](https://github.com/etibger/agentic_tools/issues/14) remains canonical, linked to F06/#11 (requirements), F15/#20 (finite budgets) and F20/#25 (version adoption). This clarification does not authorize a new runtime implementation or reopen settled approvals.

Acceptance for the shared workflow and its later AOS enforcement:

- Bring-up testing belongs inside each development task: build, intended behavior, relevant failures and immediate integration must be checked before a candidate is ready. Code written alone does not complete a task. Keep routine checks inside the task and meaningful milestone acceptance at a separate decision diamond; do not create an approval/agent handoff for every check.
- Before implementation, identify component/interface boundaries, the integration order, expected failure boundaries and the regression checks for each planned change. Include requirements-to-test coverage, a fast affected suite and the broader integration suite at the appropriate gate; do not improvise a full-project restart after every failure.
- Bind acceptance to an immutable source/configuration/tool/test baseline. Later work uses that accepted version or proposes an explicit new candidate; it must not mutate accepted files or overwrite historical evidence. Preserve contract tests and previously failing cases as regressions.
- For a change, record its affected requirements, interfaces, consumers and tests before requalification. Reuse unchanged evidence with a stated basis. Run the affected checks and required integration regressions before accepting the new candidate; the old candidate's result remains historical evidence for that old version.
- Separate a planned new version from a newly discovered defect in an unchanged version. For the latter, append a finding and explain which present acceptance claim or dependent work must be held. Preserve the original test result and approval; do not silently rewrite history or mark every upstream stage pending.
- A downstream failure first receives bounded diagnosis and a named responsible component. Reopen only demonstrably affected obligations. Record cause, owner, evidence, plan/effort impact and the criteria for closing that correction. Use an isolated candidate and retain rollback to the accepted baseline where applicable.
- Keep the execution dependency graph acyclic. Corrections are explicit versioned attempts with a finite repair/time/assignment allowance. At exhaustion, hold and present continue/change/defer/stop options with remaining acceptance and cost; no automatic repeated whole-plan loop and no resetting counters through a renamed task.
- In AOS, project candidate-scoped acceptance, affected holds and the remaining retry allowance consistently. Display simplification of dependency arrows never changes these rules. Preserve independent assessment and existing approval/authority limits.
- Qualification cases: downstream-only change preserves upstream acceptance; shared-interface change selects all affected consumers; a latent defect adds a scoped hold without erasing history; regression catches a previously fixed bug; duplicate events do not consume new attempts or relaunch; and exhausted repair allowance stops further work. Report planned versus actual recheck effort and repeated root causes.

Method references: NASA's [software regression-testing guidance](https://swehb.nasa.gov/spaces/SWEHBVD/pages/102695526/SWE-191%2B-%2BSoftware%2BRegression%2BTesting) connects planned regression selection with change-impact analysis and requirement coverage; its [requirements-change guidance](https://swehb.nasa.gov/spaces/7150/pages/16449679/SWE-053%2B-%2BManage%2BRequirements%2BChanges) describes controlled baselines and impact review. Apply these principles proportionately; this is not a claim of NASA process compliance. Current local contracts already cover some evidence/limit rules; complete reusable planning and AOS enforcement remain unqualified.

### F10: Use finite execution batches and idempotent handoffs

When already authorized, release a named build followed conditionally by a bounded test on the same identity. Stop on failure, drift or unexpected effects. Give each assignment an identity so duplicate completion events cannot trigger duplicate execution. Reconcile an interrupted assignment before retrying it.

Suggested owner: Coordinator and Verification. Trial: a build failure, duplicate completion and interrupted handoff. Success signal: appropriate stops, one native owner, preserved outputs and a recorded next action. Build on the existing audit's finite-release and queue proposals.

### F11: Keep the dashboard projection small and explain its freshness

Expose distinct times for a role observation, received report, state update, scheduler audit and browser fetch. Derive current stage, next action and pending decisions from canonical records. Keep large immutable history outside the live payload and provide a compact completion summary with links.

Suggested owner: Dashboard owner. Trial: fresh HTTP with stale role facts, a resolved approval and a completed assignment awaiting consumption. Success signal: each state is displayed accurately, including uncertainty, without a misleading active label.

B14/[#47](https://github.com/etibger/agentic_tools/issues/47) separately tracks the existing attributable usage/cost collection requirement. This item retains ownership of view consistency and freshness; a visible placeholder does not establish complete metering. Preserve the approved overall/detail dependency graph, owners, wait reasons and evidence links, with the selected task and zoom/pan stable across refreshes. Qualify those interactions against the same recorded revision rather than equating a fresh HTTP response with a current project view.

The current-phase dependency map is a standard shared-dashboard obligation, reinforced by the user's 25 October request to retain the interactive pattern used by the tiling-game dashboard. The local template now accepts a reusable `phase_graph`: branches/joins, per-step owner/status/wait/evidence, hover/focus preview, selected details, zoom/pan/Fit/Reset, keyboard close and an accessible dependency list. Its state is preserved across polling and, where session storage is available, tab reload. The [dashboard contract](docs/dashboard.md#inside-the-current-phase) and synthetic example define the maintained structure. Renderer checks and in-app served-page interaction checks pass, including reload persistence. Ready means work can start when assigned; produced results awaiting acceptance are Under review, and defects are Needs correction. Redundant shortcut arrows are omitted when the sequence already expresses the dependency; all acceptance prerequisites remain in the step details and accessible list. Genuine parallel branches stay visible. Milestone workflow logic adds action boxes, explicit acceptance/allowance diamonds and a finite correction return; recorded attempts remain separate and retain failures. Bring-up is part of each development task. A dated per-step forecast now retains its original target/range separately from actual active, approval-wait and technical-wait observations; revised estimates must remain comparable to the baseline. Broader viewport coverage, automatic collection and independent integrated acceptance remain outstanding. F11/#16 stays canonical and open; this is not a second dashboard product or a claim of automatic runtime collection.

### F12: Retain evidence according to the delivery claim

Define which source snapshots, logs, configuration and reports must survive task cleanup. Use manifests with content identity and retained artifact locations. Check that the people who will accept or maintain the feature can retrieve them. A transient CI URL needs an associated retained result when later reproduction matters.

Suggested owner: Verification and the receiving maintainer. Trial: reconstruct one acceptance decision after its temporary workspace is gone. Success signal: the claim, selected scope and result remain inspectable. Use existing storage before introducing a new evidence platform.

Baseline-recovery clarification from the 25 October pilot: a list of file hashes identifies a candidate but cannot recreate missing bytes. Retain the source/dependency/test snapshot, or a verified base plus scoped patch and all untracked files, together with the evidence needed to reproduce the acceptance claim. Demonstrate restoration into an isolated directory and rerun the selected check against the restored identity. A complete copy of the current local repair candidate has been retained and hash-verified; reconstruction of all earlier uncommitted candidates is not yet demonstrated. Keep that historical recovery coverage explicit and do not infer it from surviving logs. Related: F09/#14 (protected baselines and regression) and F20/#25 (version adoption). This remains an open qualification item, not a claim of release or installation.

### F13: Strengthen one representative oracle before expanding coverage

For a critical user journey, maintain a known-good case and a deliberately wrong result that the check must reject. For agent-generated outputs, independently review expected behavior and distinguish factual accuracy, citation quality and execution success. Expand mutation or fault-injection checks only where the risk warrants them.

Suggested owner: Verification. Trial: one important workflow boundary. Success signal: the test fails for the named plausible defect and passes for the accepted behavior, with the actual scope stated. Extend the existing oracle-calibration contract rather than creating another parallel suite.

### F14: Make challenge reviews answer a specific uncertainty

Select a challenging reviewer when a feature has a significant assumption, difficult interface, security boundary or ambiguous acceptance criterion. Give that role a concrete question and counterexample to investigate. Preserve independent findings and resolve disagreement with evidence.

Suggested owner: Coordinator. Trial: compare a targeted challenge with an unrestricted review on one suitable increment. Success signal: useful distinct findings and their assessment effort are recorded. Participation or a long debate is not itself a quality outcome.

### F15: Add explicit execution budgets and replan triggers

Permit a project to define limits for exploration, repeated repair attempts, compute use or elapsed dependency waiting. At a reached limit, summarize results, uncertainty and concrete options for reducing scope, changing approach or obtaining the missing resource. Use available usage data and preserve unknown categories.

Suggested owner: Coordinator and feature owner. Trial: a bounded investigation that encounters an unavailable dependency. Success signal: it ends in a usable decision packet without silently repeating work or changing the promised outcome.

B14/[#47](https://github.com/etibger/agentic_tools/issues/47) owns usage collection and aggregation. Budget policy must distinguish an observable time/turn limit from an estimated financial total or an actually enforceable spending cap.

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

Project-chat follow-up: keep one shared AOS implementation while pinning each pilot's workflow/runtime version, capabilities and approved contract. Product chats submit reusable changes to the shared backlog instead of creating their own AOS forks. A core update must not silently change an active pilot's scope, decisions or execution behavior. Demonstrate an update with one active and one held pilot, including compatibility rejection and explicit migration/rollback. B12 covers the tool-connection refresh mechanism; this item owns adoption and compatibility policy.

### F21: Keep publication adapters separate from project completion

Treat local files, a documentation site and approved collaboration systems as destinations for views of the project record. Configure the required audience and outputs per project. Track publication/readback as an obligation when selected, while keeping restricted evidence out of a public reusable kit.

The proposed AOS setup selects Confluence milestone snapshots alongside the live dashboard. Use the same source revision, preserve human page edits and show publication failures independently of execution. More interactive dashboard controls and bidirectional page editing remain future choices; they must use the existing decision path rather than create another approval record.

Suggested owner: Coordinator and documentation owner. Trial: a local project and a separately authorized collaboration-system output. Success signal: both use the same approved facts, with correct audience and source links and no duplicated authoritative state.

### F22: Limit concurrent projects by real bottlenecks

If several features run together, make shared writers, test environments, external dependencies and human decisions visible across projects. Limit started work when one of these resources is saturated. Allow useful independent preparation without creating more unfinished implementation.

Suggested owner: Coordinator. Trial: two features sharing one native verification environment. Success signal: clear ownership, no concurrent output corruption and observed queue times. Add a scheduler only after a simpler explicit queue proves insufficient.

The first [AOS concept](docs/aos-concept.md) deliberately supports one active project. Concurrent-project scheduling remains a later experiment, after the single-project handoff and recovery path is demonstrated.

Project-chat follow-up: allow separate bug, feature and new-product preparation to retain distinct records and owners without admitting all of them to autonomous execution. Preserve the currently approved one-active-AOS-project limit; independent manually operated projects are not silently migrated or stopped. Future multi-project qualification must prove that shared writers, native resources and approval records cannot cross-contaminate projects. F20 owns shared-core version adoption; B15 owns the deferred new-product path.

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

Dashboard clarification (F11 / [#16](https://github.com/etibger/agentic_tools/issues/16)): the coordinator-maintained display now separates a concise **Current work** view from **Attempt history**, names owners and treats review arrows as transitions rather than a queue. Added repair work and incomplete time accounting are visible alongside the unchanged forecast. This presentation improvement does not complete shared live collection or Q3 qualification.
