# Dashboard and usage

The initial workflow used a custom HTML/CSS/JavaScript dashboard served by a task-local Python process. It was not a third-party project management application. Its display consumed recorded evidence; the HTML did not independently decide gates.

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

Keep actual automation ID/status, next scheduler run and last audit in `watchdog`; use [the watchdog prompt](downloads/watchdog.txt) with a runtime scheduling tool for a five-minute heartbeat. Serving HTML does not create that timer. Reuse an existing matching automation. Quiet unchanged runs; consume reports and dispatch ready authorized handoffs. Pause at full delivery including lessons learned, or explicit user pause.

## Approvals needed from you

This section is always visible, above agent and chart details. All necessary requests live in `decisions`, including setup, DI choices and bounded validation exceptions. Each stable ID includes question, recommendation, alternatives, consequence, affected gate, requester, requested time and request/evidence URL. Record `approved`, `declined` or `withdrawn`, answer and answered time when actually received. Resolved entries leave the pending list but stay in answered history. When none remain, display **No approvals pending**. Do not repeat approved requests or invent new approval requirements.

The dashboard is read-only: answer in the linked request or coordinator chat. The coordinator consumes the answer and updates state and the next handoff. A pending request is not inferred from an idle agent. Show the exact remaining bring-up prerequisite after DI approval; never label the approval itself pending after the user has granted it.

## A useful glance

Show the current stage prominently, followed by approved task cards, actual agent assignments, the next handoff, required user decision, findings and evidence. Let the layout expand with screen width. Use legible axes and completion labels on a bounded responsive burndown. The supplied SVG has a viewBox and explicit aspect ratio so it cannot expand beyond its card. Check narrow and wide screens, flat initial progress, partial completion and final completion with a future clock. Display actual current stage and next owner/action/dependency at the top; avoid vague “see actual stage” captions.

Agent rows should show role, actual state, assignment, next dependency and last meaningful update. “Done” means assignment done. It does not mean the feature has been delivered.

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
