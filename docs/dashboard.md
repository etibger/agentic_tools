# Dashboard and usage

The initial workflow used a custom HTML/CSS/JavaScript dashboard served by a task-local Python process. It was not a third-party project management application. Its display consumed recorded evidence; the HTML did not independently decide gates.

The proposed [AOS user surfaces](aos-concept.md#three-connected-user-surfaces) retain this dashboard approach as the main live overview, alongside chat decisions and Confluence history. The downloadable renderer puts user requests first, separates technical blockers, displays recorded usage/cost with coverage, and supports an interactive map inside the current phase. It reads coordinator-supplied state; automatic runtime collection and independently qualified integration remain incomplete. The existing extended workflow and its watchdog instructions below continue to describe the manual implementation.

The example below is an interactive, static demonstration; it is separate from the downloadable live renderer described under Start the live dashboard. Its controls change synthetic state only.

<iframe src="../assets/dashboard-demo.html" title="Synthetic multi-agent dashboard example" loading="lazy"></iframe>

[Open the dashboard example at full width](assets/dashboard-demo.html).

## Start the live dashboard

Download [dashboard.html](downloads/dashboard.html) and [workflow-state.example.json](downloads/workflow-state.example.json) into the task artifact folder; rename the JSON to `workflow-state.json`. Replace synthetic entries with actual records, set `synthetic` to false and keep the state out of the public template repo. Run the host's supported Python command from that folder:

~~~sh
python3 -m http.server 8079 --bind 127.0.0.1
~~~

Share `http://127.0.0.1:8079/dashboard.html` (choose an available port). This is a real polling renderer, with no dependency installation. The coordinator writes JSON atomically after every meaningful event; `updated_at` is the source update time, not each browser refresh. The display fetches every two seconds with no-store caching. Keep `build_id` current after renderer changes. Failure retains the last good state and shows an error; source data older than five minutes is visibly stale. A working fetch is not evidence of fresh agent status.

The sample is explicitly synthetic. Reset its dates/tasks/decisions/history before a live run. `baseline_at` is the approved plan time; accepted tasks need actual `accepted_at`. History records actual remaining approved gates after acceptance, reopening or scope changes. Set `completed_at` only when all feature gates are accepted, at the final acceptance time. Workflow closure can occur later after the retrospective/publication.

Keep actual automation ID/status, next scheduler run and last audit in `watchdog`; use [the watchdog prompt](downloads/watchdog.txt) with a runtime scheduling tool for a five-minute heartbeat. Serving HTML does not create that timer. Reuse an existing matching automation. Audit every actual agent for completion, activity, idle/blocked or attention-needed states; do not depend on volunteered messages. Distinguish scheduled runs from manual audits. Quiet unchanged runs; consume reports and dispatch ready authorized handoffs. Pause at full delivery including lessons learned and the post-retrospective actions milestone, or explicit user pause.

Include a separate **Post-retrospective actions** task after the retrospective in `tasks`, with its actual acceptance timestamp. Link a bounded action register from the dashboard evidence and lessons output; record owners, statuses, triggers, success criteria and result/commit evidence. Immediate authorized fixes must execute and be verified. Deferred experiments stay Proposed/Deferred; tracking them does not claim execution. If a later user request adds a task, annotate the scope change in history rather than silently changing the original baseline.

## Approvals needed from you

The **Needs your attention** section appears first. All necessary requests live in `decisions`, including setup, DI choices and bounded validation exceptions. Each stable ID includes question, recommendation, alternatives, consequence, affected gate, requester, requested time and request/evidence URL. Record `approved`, `declined` or `withdrawn`, answer and answered time when actually received. Resolved entries leave the pending list but stay in answered history. When none remain, display **No approvals pending**. Do not repeat approved requests or invent new approval requirements.

The dashboard is read-only: each pending card shows an `answer_destination` such as the coordinator chat, with that chat as the fallback when the field is absent. The coordinator consumes the answer and updates state and the next handoff. A pending request is not inferred from an idle agent. Show the exact remaining bring-up prerequisite after DI approval; never label the approval itself pending after the user has granted it.

Optional `attention_note` explains what the user should do when there is no pending decision. Keep technical problems in `blockers`, with `title`, `detail`, `owner` and `next_action`. A blocker does not generate an approval request. Optional task `depends_on` lists predecessor IDs. These fields remain compatible with the version 1 sample state.

The always-visible usage panel accepts optional `usage` fields: `total_tokens`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `coverage`, `source` and `observed_at`. Optional `api_equivalent` and `actual_billed` objects contain `amount` and `currency`; the estimate also records `assumptions`. Missing prices display **Unpriced** or **Unavailable**, while a measured zero remains zero. State what is excluded from partial totals. The renderer does not collect usage or fetch prices itself.

## A useful glance

Show the current stage prominently, followed by approved task cards, actual agent assignments, the next handoff, required user decision, findings and evidence. Let the layout expand with screen width. Use legible axes and completion labels on a bounded responsive burndown. The supplied SVG has a viewBox and explicit aspect ratio so it cannot expand beyond its card. Check narrow and wide screens, flat initial progress, partial completion and final completion with a future clock. Display actual current stage and next owner/action/dependency at the top; avoid vague “see actual stage” captions.

Agent rows should show role, actual state, assignment, next dependency and last meaningful update. “Done” means assignment done. It does not mean the feature has been delivered.

The **Terms and abbreviations** section at the bottom explains common workflow, burndown and usage terms. Add project-specific entries through the optional `glossary` array, with `term` and `meaning` strings. Explain the codes actually displayed, such as local milestone and qualification labels. Use ordinary language in the main view; the glossary is supporting help, not a reason to leave status or decisions cryptic.

## Inside the current phase

The reusable template includes a **Current phase · detailed dependencies** section between the milestone overview and technical blockers. This is a standard dashboard requirement, tracked by F11/[#16](https://github.com/etibger/agentic_tools/issues/16). Populate it from the same recorded project state as the overview. A high-level milestone card does not replace the steps inside that milestone.

Use the optional `phase_graph` object in `workflow-state.json`. Existing version 1 producers remain compatible; without the object, the page explicitly says that no detailed dependency record was supplied. The downloadable example includes a synthetic branch and join.

| Field | Meaning |
| --- | --- |
| `id`, `title` | Stable phase identity and readable heading. Changing the identity starts a separate inspection view. |
| `summary`, `next_action` | Brief current facts and the next responsible action. Explain the practical meaning; keep technical identifiers in step details. |
| `source_revision`, `observed_at`, `provenance` | Exact source revision, fact-observation time and how the view was produced. A new browser fetch is not a new observation. The phase shows its own stale warning after five minutes. |
| `lanes` | Optional `{row, title}` labels for independent workstreams. |
| `nodes` | Required step records described below. Arrows are derived only from their `depends_on` references. |

Each node has a stable lowercase kebab-case `id`, readable `title`, `status`, `owner`, factual `summary`, `wait_reason` or next action, `observed_at`, and `depends_on` IDs. Integer `column` and `row` place it on a small grid; prerequisites must be in earlier columns. Optional `evidence` entries have a `label` and HTTP(S) `url`. Serve only explicitly allowed evidence through the project's existing private boundary; never expose an entire working directory, decision credentials or private conversation history. Invalid IDs, unknown references, duplicate positions or dependency cycles reject the new snapshot and retain the last good view with a visible error.

Use `accepted`, `active`, `ready`, `review`, `changes_requested`, `waiting`, `held`, `failed` or `pending` explicitly. Accepted applies to the named step's evidence, not the entire phase. Active requires an actual observation. Ready means a task can start when assigned; it does not grant execution authority. A produced result awaiting independent acceptance is `review` (Under review), not Ready. Use `changes_requested` (Needs correction) when assessment finds a defect. Show the reviewer's actual activity separately from the status of the result they are checking. A missing user decision and a technical blocker must have distinct explanations and owners; the graph never creates an approval request from a waiting node.

Use a single acceptance diamond at a meaningful milestone exit. Development completion includes the block's appropriate build, intended-behavior, failure-case and immediate-integration checks. Code written with required bring-up unrun or failing stays active/blocked; only a tested candidate is ready for review. Group routine implementation and checks inside action boxes; keep their detail inspectable without turning every check into an agent handoff or approval. An optional `phase_flow` uses the same node fields plus `kind: action | decision` and bounded `repair_loops` (`from`, `to`, `label`, positive integer `limit`, and `used`). It enables **Current work** (the default) and **Attempt history** views, each retaining its inspection position. The current view shows the accepted entry gate and remaining tasks, named owners and actual blockers. Keep superseded failures and unused review paths in history, with their replacement outcome; they are not a current review queue. Edge labels such as “On completion: independent review” describe a transition, not present activity. The independent assessor owns technical review; the coordinator reconciles its evidence, while human decisions remain in the attention section. The current view can show a conditional return arrow; actual recorded dependencies remain acyclic and each failed/repaired attempt retains its own identity. A diagram does not enforce convergence, authorize another attempt or reset a budget. Show an explicit stop/replan branch when a further correction is unjustified or outside the allowance.

Dependencies name their transition condition as well as ordering. Optional `dependency_conditions` maps prerequisite IDs to `accepted` (default, solid arrow), `completed` (a result is ready for independent review, blue dotted arrow), or `failed` (a failed review triggers an already-authorized correction, amber dashed arrow). Put the failure/review condition above the destination and in its details and accessible list. A failed gate may start a bounded correction; it may not release the normal success path. Conditions describe recorded workflow intent and grant neither authority nor additional attempts. Reject unknown conditions or keys that do not name a prerequisite.

Optional `dependency_labels` supplies short plain-language branch labels (for example, “Yes: continue” or “No: stop and present options”) keyed by an existing prerequisite. Labels explain the condition without changing it. Decision branches may run downward; normal dependency cycles remain invalid.

Draw the minimum set of arrows that preserves the dependency paths (transitive reduction). For example, if A → B → C already appears, omit a separate A → C shortcut. Keep genuine independent branches and joins visible. This is a display simplification only: retain the complete `depends_on` list and show it as **All required checks** in step details and the accessible list. An earlier failed, reopened or invalidated requirement still blocks acceptance; hiding its shortcut arrow never changes execution eligibility. The page states how many shortcut arrows it omitted.

Hover or keyboard focus previews a step; click or Enter keeps its details open beside the graph. Details show its owner, status, prerequisites, observation time, wait reason and evidence. Escape or the close control dismisses the panel. The accessible dependency list provides the same relationships without requiring visual navigation. Zoom in/out, Fit, Reset, scrolling and background dragging support large graphs. Polling preserves zoom, pan, selection and open list state while refreshing the selected step's facts. Where browser session storage is available, zoom, pan and selection also survive a page reload within that tab; otherwise polling retains them in memory. Changing phase does not carry a selected step into another phase.

Verify a branch/join, blocked and failed steps, safe text/links, changed source facts, an invalid or failed fetch, keyboard selection/close, narrow and wide layouts, and zoom/pan/selection across polling and reload. Renderer checks are necessary; they do not qualify an automatic collector, dispatch, real workflow completion or the full AOS desktop journey. Keep the linked backlog item open until its agreed acceptance boundary has evidence.

## Forecast and actual time

Keep a dated original estimate for each internal step, a target and range, assumptions, dependencies and the milestone it forecasts. Use active elapsed minutes, not aggregate agent minutes or monetary cost. Record approval waiting and technical waiting separately. A later forecast is a new revision with reasons; never overwrite the baseline to hide error. Keep unknown actual durations unknown, and do not invent estimates for work already finished. The optional `forecast` view compares retained estimates with explicit coordinator observations. It does not collect timings automatically or authorize extra attempts. One correction allowance and its schedule effect must be explicit; a limit or mandatory-evidence failure causes a hold, not an automatic extension.

## Burndown contract

- Y axis: **approved tasks remaining**, with the baseline named.
- X axis: **actual elapsed wall-clock minutes/hours** from the approved baseline.
- Steps: accepted gate completion, reopening or explicit scope change.
- Annotations: which task completed, at what time, and what remains.
- Provisional history: separate series or clear boundary; never reinterpret earlier counts as approved tasks.
- Completed task: freeze the range and elapsed duration at the final accepted task timestamp.
- Freshness: display sample age separately; a growing “last updated” age does not extend task duration.

Five-minute sampling is a checkpoint cadence, not a project day. Counts do not measure effort, cost, agent quality or percentage of code finished.

After a structural edit, confirm served build identity, reload and inspect the visible stage/labels when UI tools work. Otherwise record the specific visual-check blocker. HTTP bytes alone do not prove the user has loaded them.

## Usage column glossary

| Column | Plain-language meaning |
| --- | --- |
| API-equivalent USD Standard / Fast | Hypothetical API cost using stated model/rate assumptions for standard or faster service. It is not the user's subscription bill; request pricing classification may be unknown. |
| Total tokens | Provider-reported total under its documented convention. Reasoning output and cached input can be subsets, so do not add every column together. |
| Input / cached input | Input is what the model received: prompts, context and tool results. Cached input is the reported portion reused from a provider cache; it may receive a different hypothetical rate. |
| Cache-write input | Input reported as creating a cache entry, if the provider exposes a separate counter/category. Missing data means unavailable, not zero. Do not charge it separately without provider-specific rules. |
| Output / reasoning output | Output is tokens produced by the model. Reasoning output is a reported subset/category for internal reasoning where supplied; it is not the private reasoning text. Avoid double counting. |
| Counter time | Timestamp of the usage counter's latest observation; not cumulative CPU time or elapsed development time. |

Token meanings and prices are provider/model specific. Record source, effective date, currency, input/output/cache rates, availability and assumptions. With compatible counters, an estimate often uses uncached input × input rate + cached input × cache-read rate + output × output rate. Add separately billed cache writes only when the provider's contract actually calls for them.

## CPU, memory and measurement health

Scope measurements to owned local processes. Show CPU units/convention, sampled memory (for example RSS), sample interval, process enrollment, collector status and gaps. Cloud model compute is not local CPU. Short-lived or detached processes may be missed.

Unknown counters display unavailable/unpriced. Measured zero remains distinct. Optional telemetry must not block feature correctness gates or grow into an unrequested monitoring platform.

The output section links the verified Confluence DI and its lessons-learned child; show unpublished/blocked status honestly. At full delivery, stop collectors and pause watchdogs within their authorized scope and retain a final snapshot. Task completion, broader workflow closure and collector stop are separate timestamps.

When a forecast exists, keep its original estimates and finish target immutable. Show additional repair/integration work in the visible `forecast.additional_work` list with `title`, `measured` (including the measurement basis) and `outcome`. Use `forecast.accounting_note` to state elapsed time and gaps in attribution. An unknown duration does not mean no work occurred; do not turn elapsed absence into human waiting or treat a replacement attempt as the total cost of the task.
