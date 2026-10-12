# Implementation, specification and intermediate commits

The Implementer delivers code, tests and a human-readable specification for each approved stage. Documentation is a required implementation output, and Verification uses it alongside the approved DI to assess behavior.

## Generate readable product documentation

During DI, name the versioned specification sources, renderer, exact build/check commands, allowed paths and preview or built-artifact destination. Reuse the repository's documentation tools. If none exist, choose a suitable renderer such as MkDocs within the authorized tool/dependency scope. Record unavailable prerequisites explicitly rather than silently dropping documentation.

Keep relevant sources aligned with each stage:

| Area | Required content when affected |
| --- | --- |
| Behavior and contracts | Requirements, data flow, API/interface schemas, requests/responses and compatibility |
| Edge cases | Defaults, validation, errors, limits, lifecycle and failure behavior |
| Deployment and operation | Installation, configuration/authentication, startup, health, shutdown and recovery |
| Verification | Acceptance examples, native commands, offline fixtures where required, coverage and platform limits |

Generate a navigable, human-readable site or equivalent rendered artifact from those sources. Give the reviewer a usable preview or artifact link. Keep generated output out of Git unless explicitly requested or required by the repository.

Automatic rendering does not replace authored expectations. API schemas and executable examples can supplement prose, but generating expected behavior solely from implementation constants can hide the same defect from both implementation and verifier. A README is a useful entry point; it does not replace specification pages for substantive contracts and operations.

## Use documentation during independent assessment

The Implementer runs the native documentation build and relevant checks before freezing a candidate. Verification independently checks:

- the specification against approved requirements and actual product behavior;
- representative examples and error/default behavior, using an independent oracle;
- the exact source identity, generation command, output and drift/links checks available in the repo;
- readability, navigation and rendered pages when UI tools work, or the precise visual-check blocker.

Keep semantics separate from successful rendering. Documentation-only corrections need proportional checks; rerun product tests only when changed behavior, examples or unresolved evidence justify it. Verification reports findings; the Implementer remains the product/documentation source writer.

## Create intermediate checkpoints

When commits are authorized, preserve coherent implementation stages as commits before starting dependent work. Do not leave all completed stages staged until final delivery.

| Responsibility | Required action |
| --- | --- |
| Implementer | Default checkpoint owner: review and stage the scoped implementation, create its commit and report exact identity |
| Verification | Confirm the reachable commit/tree against the frozen inventory and associate executed results; may create a checkpoint through an explicit serialized commit handoff |
| Coordinator | Assign one commit owner, consume both receipts and resolve a missing checkpoint before the next dependent implementation assignment |

A delegated Verification commit uses the agreed frozen files/index; it does not authorize product edits, unrelated staging or a second simultaneous Git writer. Retain verification helpers/logs outside tracking unless their inclusion is explicitly authorized. Follow repo policy on separate documentation commits; one stage can have an implementation commit and a separate specification commit.

Record the stage, SHA, parent, paths, owner, actual timestamp and check status. A successful commit preserves a rollback point; it does not establish gate acceptance. Preserve failed reports and label unaccepted candidates honestly.

If commits are not authorized or execution is blocked, record the pending checkpoint and precise dependency. Use the base, scoped patch, new-file inventory and hashes to identify an assessment candidate, but do not claim that snapshot is a requested commit. Request only missing authority; do not ask again when it already exists.

Create new commits for corrections. Do not amend, squash, replace, backdate or push intermediate commits without the corresponding explicit authority. A final push remains a separate authorized delivery action.
