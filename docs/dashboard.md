# Dashboard and usage

The initial workflow used a custom HTML/CSS/JavaScript dashboard served by a task-local Python process. It was not a third-party project management application. Its display consumed recorded evidence; the HTML did not independently decide gates.

The example below is an interactive, static demonstration. Its controls change synthetic state only.

<iframe src="../assets/dashboard-demo.html" title="Synthetic multi-agent dashboard example" loading="lazy"></iframe>

[Open the dashboard example at full width](assets/dashboard-demo.html).

## A useful glance

Show the current stage prominently, followed by approved task cards, actual agent assignments, the next handoff, required user decision, findings and evidence. Let the layout expand with screen width. Use legible axes and completion labels on a large burndown.

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

At delivery, stop collectors/watchdogs within their authorized scope and retain a final snapshot. Task completion, broader workflow closure and collector stop are separate timestamps.
