# Design investigation and approval

A DI is a software design and verification package. It should be concrete enough for a busy feature owner to approve without reading every source file.

## Required content

| Part | Evidence or decision |
| --- | --- |
| Problem and scope | Before/after example, requirements, exclusions and intended delivery |
| Current behavior | Pinned source identity; real symbols and execution paths |
| Ownership | Shared runtime facts versus client presentation; reuse and compatibility boundaries |
| Proposed design | Data flow, interfaces, schema/examples, defaults, errors, reload and alternatives |
| Failure boundaries | Missing/stale/invalid data, state transitions, input interpretation and resource contention |
| Environment | Native recipes, host, prerequisites, baseline, required checks and unavailable platforms |
| Bring-up order | Task ID, dependency, owner, candidate boundary, checks and exit gate |
| Traceability | Requirement → source/consumer boundary → task → observable check → coverage limit |
| Decisions | Recommendation, alternatives, consequence and concise approval request |

Use the [DI template](templates.md). A diagram is helpful, but label observed architecture separately from a proposed change.

## Publish before implementation

The submitted kickoff names a Confluence parent and audience. Publish the full DI as its child, check for duplicates, and read back the actual title, parent, complete content, tables and links. Send the verified URL before dependent implementation or feature testing. Missing access or a destination is a visible blocker with a recommended resolution; do not substitute an unpublished local draft silently. Keep a local copy and track revisions.

## Approval record

Record who approved which design version, when and with which decisions or exceptions. Register pending questions in the dashboard with recommendation, consequence, requester, affected gate and timestamp. On reply, record the answer and clear the request promptly. DI approval is distinct from environment bring-up; name the remaining prerequisite rather than leaving approval pending. Keep material later revisions explicit.

A task name alone is not an acceptance contract. A stage such as “configuration lifecycle” needs approved default, validation, error-context and reload behavior, plus the checks that distinguish them.

## Bounded acceptance contract

For each affected clause:

1. Identify its direct consumer and input/error boundary.
2. Name the smallest observation that distinguishes correct from incorrect behavior.
3. Name a plausible false positive the check must reject.
4. State whether evidence comes from source review, native execution, live runtime or a limitation.
5. Record exact candidate and result.

This is a compact acceptance packet, not a demand for another exhaustive suite.

## Generalized example

The configuration stage initially had green tests but a missing diagnostic context. The gate stayed held; a narrow source fix produced a new candidate and independent CLI fixtures checked it.

The lesson is to include the actual user-facing error boundary in the requirement contract, not to create an unlimited list of generic adversarial tests.
