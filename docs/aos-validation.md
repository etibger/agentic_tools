# AOS reproduction and qualification

The bounded Q2 bug/feature milestone is locally accepted. T1–T3 have local source and guide commits; the current readiness candidate is author-tested and awaits a non-author assessment. A local check, package build or configured CI job does not establish actual desktop qualification, adoption, installation or release. The [operator guide](aos-operator.md) defines supported commands; the [concept](aos-concept.md#acceptance-and-failure-tests) retains A1–A12 unchanged.

## Fresh source checks

Use a fresh checkout/export of the exact candidate on macOS with CPython **3.12.14**, uv, Node.js and OpenSSL available. `.python-version` and AOS CI select that exact interpreter. The AOS native-process tests exercise POSIX ownership and macOS process inspection. Hosted AOS CI selects `macos-latest`; the separate documentation workflow uses Ubuntu. Windows and other runtime hosts are not qualified by those jobs. CI pins uv 0.12.21 and Node 22; retain the actual local tool versions with a local result rather than claiming identical hosted execution.

`uv.lock` fixes runtime, documentation and development dependency versions; the build backend is separately pinned to hatchling 1.32.4 in `pyproject.toml`. The exact Python patch pin also avoids uv's generic-version alias directory: native sandbox profiles allow the resolved interpreter prefix, and an intermediate `cpython-3.12-…` alias outside it is correctly denied. Fresh-export bring-up reproduced that denial and verified the canonical interpreter under the same restrictions. No permission widening or test exemption is used. Sync into the export's own environment, without copying `.venv`, caches, private helpers or state:

```sh
uv sync --locked --dev --python 3.12.14
uv run aos version
uv run pytest --junitxml=/absolute/private/evidence/aos.xml
uv run python -m unittest discover -s scripts -p 'test_*.py'
uv run python scripts/check_gate.py templates/gate-state.example.json
uv run python scripts/check_publication.py
node scripts/check_dashboard.mjs
uv run mkdocs build --strict
```

The publication checker uses Git's file inventory. A clean exported source tree can have its own disposable Git index; never initialize or change the source checkout's index for this check. Include all intended public source, tests, guides and configuration, with symlinks preserved. Keep private attachments in their original location and run the checker on the clean public export. Its pass remains a bounded pattern check requiring semantic disclosure review.

The full pytest suite includes a fresh source/wheel bring-up: it preserves the template symlink, builds without private helpers, verifies packaged renderer bytes and serves the extracted wheel from outside the checkout. It uses disposable state and ports, no model invocation or installation. JUnit goes outside the exported public inputs; normal `.venv`, generated docs and cache locations remain unchanged. Run `uv build --wheel` separately when retaining a distribution artifact is part of the selected delivery.

The focused maintained regression command is:

```sh
uv run pytest tests/test_aos_authority_regressions.py tests/test_aos_owned_stop.py tests/test_aos_capture_controls.py tests/test_aos_recovery_controls.py
```

Hosted AOS and documentation workflows execute only after an authorized push or workflow dispatch. A locally passing equivalent command is not a hosted CI result. Native child-process fixtures are ordinary owned synthetic commands, not real model or desktop qualification.

## Retained private regression inventory

Earlier private assessments remain retained with their original results; they are not imported or redistributed. The following public-safe equivalents use repository helpers and disposable synthetic inputs. Counts and historical acceptance remain attached to the original candidate; the new suite protects the maintained behavior.

| Retained suite/probe | Public regression or separate obligation |
| --- | --- |
| `test_adversarial_q1` | `test_aos_q1_controls.py` covers deadline stop before polling, missing mutation evidence and a model/contract outside the approved package. |
| `test_reassessment_q1` | `test_aos_owned_stop.py` covers known-stop idempotence across restart, cancellation arriving during stop and terminal receipt replay. `test_aos_q1_controls.py` preserves legacy approval without an execution envelope while holding dispatch. |
| `test_final_q1` | `test_aos_q1_controls.py`, `test_aos_owned_stop.py` and lifecycle restart tests cover unknown/replied-lost stop reconciliation, paused ownership and conflicting stop receipts without release or relaunch. |
| `test_independent_r4` | Four maintained owned-stop regressions exercise an authoritative conflict journal with a missing derived blocker, interrupted resolution rollback, an old resolution replay after a new contradiction, and a new conflict during cancellation. |
| Q2 independent native child probes | `test_aos_codex_runtime.py` exercises ordinary child cleanup on pass/failure/timeout, active cancellation, process death and exact durable-owner recovery without reexecution. `test_aos_authority_regressions.py` adds the pre-execution barrier and missing-owner cases; changed-owner tests reject signaling an unrelated process. |
| Q2 pure adversarial matrix | `test_aos_authority_regressions.py` covers forged saved assignment fields, shared-budget identity/limit changes and widened effective tools/plugins/permissions/environment. Existing runtime tests cover recipe/baseline drift and exhausted durable reservations. These use no real SDK transport/model call. |
| Capture expiry/native rejection probes and `test_own_capture_adversarial` | `test_aos_capture_controls.py`, approval-web and decision-recovery tests cover delayed unchanged forms, stale/changed identities, recording/reply-loss retries, concurrent opposite answers, origin/fetch metadata and server-lifetime credentials. These simulated HTTP answers are not human qualification. |
| Pinned historical startup recovery success (`test_capture_recovery`) | Exact protected evidence bytes and original human provenance remain private. Generic backup/restore, decision readback and crash cases are public; the one pinned historical import's positive proof remains a separate protected-evidence assessment obligation. Do not replace its pins with synthetic approval or replay it into live state. |
| Native worker-denial/TLS/client probes | Effective-config unit tests cannot prove actual OS denial, certificate trust or a visible human action. These remain the host checks below. |

## Requirements and actual-host obligations

The neutral assessor independently checks the frozen source, executes affected checks and maps A1–A12 to evidence. It must retain historical identity/impact rationale when reusing Q0/Q1/Q2 evidence; an old pass does not accept this candidate. No code author accepts their own candidate.

| Requirement | Public evidence and remaining host boundary |
| --- | --- |
| A1–A3: introduction, approval, staffing | Store/MCP/CLI, Q1 authority and capture tests reject wrong authority/configuration. Actual chat structure/team disclosure and the exact visible HTTPS human choice need protected client evidence. |
| A4–A6: continuity, resources, restart/controls | Lifecycle, owned-stop, runtime and coordinator tests cover single handoffs, holds, control races and recovery. The approved awake-Mac close/reopen journey must observe the actual independent worker, original ledger, exact ownership and preserved decisions/budgets. Sleep/logout/termination are separately classified interruption cases. |
| A7: evidence and independence | Lifecycle tests reject stale/wrong candidates and missing/failed/unrun review evidence. The non-author still independently executes/reviews the frozen candidate and records its disposition. |
| A8: real user journey | Historical bounded Q2 real bug/feature runs retain their identities. The current supported chat/CLI/dashboard path and actual desktop journey remain a separate authorized integration gate; simulated tests cannot satisfy it. |
| A9–A10: scoped delivery and bounds | Tests preserve delivery flags, limits, ambiguous launches and unknown owned effects. Receiving-owner handoff, exact candidate/readback and any authorized cutover remain operational obligations; general installer/updater and automatic chat wake-up are deferred. |
| A11: coherent views and usage | Projection/dashboard/recovery tests compare source identities and retain collector failure/staleness, graph interaction, duplicate/reset counters and unavailable billing. The actual connected client version and same-work dashboard must be observed after reopen; missing chat/other-process usage remains excluded. |
| A12: archive boundary | Archive/recovery tests cover failed/interrupted writes, readback, retry and conflicts against a local fake destination. No external publication is implemented or qualified. Any later selected live publication requires its own destination/audience authority and saved-version readback. |

For actual-host work, verify the assigned worker cannot read/write the ledger, decision credentials or TLS key and cannot reach the decision endpoint; an operator root declaration is insufficient. Check trusted TLS and invalid-certificate failure on the selected route. Record actual source/capabilities: CLI coordinator controls are available, while MCP workflow controls remain read-only/unavailable.

Desktop adoption must use the original shared ledger and budget boundary. A restored ledger is an inspection/recovery copy with a persisted dispatch fence; it cannot become a second admitted service. Preserve protected sessions, named ports/owners and the old service until the separately approved safe-window/cutover procedure permits a change. T5 code acceptance alone grants neither T6 actions nor Herdr admission. The coordinator prepares those exact operational proposals separately; this guide does not authorize running them.
