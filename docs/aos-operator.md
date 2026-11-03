# AOS coordinator commands

The maintained `aos` CLI and trusted coordinator MCP connection introduce work, save its exact concept and execution envelope, prepare the existing HTTPS decision, admit approved work, and pause, resume or cancel it. They call the same Store, Lifecycle and Worker used by the existing service. The separate legacy M0 MCP bridge does not expose workflow admission or lifecycle controls.

These commands establish an operator interface. Actual desktop integration, service lifetime, worker sandbox denial and Herdr readiness require their separately retained qualification evidence. A successful simulated workflow does not establish those results. The CLI has no command or flag for supplying a human approval answer.

## Small-feature execution

The **locally qualified 0.2.10** component adds the optional `isolated-checkout-v1` route using the existing ledger, human decision and worker. The [validation guide](aos-validation.md#small-feature-qualification) records its actual host and autonomous-rehearsal evidence and limits. This is local qualification, not installation or release. Existing exact-file recipes and historical failures retain their earlier behavior and evidence.

A maintained project JSON binds the isolated source/output roots, installed tools, native checks, private state/readiness directories and existing TLS certificate. A separate work request contains the feature, acceptance criteria, team and finite execution envelope. Prepare both files, then start the complete flow with one command:

```sh
uv run aos start --project /absolute/project.json --request /absolute/work.json
```

For an optional readiness-only check, use `uv run aos prepare-project --project /absolute/project.json --request /absolute/work.json`; it creates no product assignment or reservation. It is not a required extra step. `start` owns preparation, the decision listener and the exact-work worker in one foreground process, reusing valid retained readiness. It prints the decision URL only after the listener is ready. A verified saved approval advances work while chats are idle. The CLI never supplies a human answer. The host and owner must remain available; this command installs no background service and does not provide desktop chat wake-up.

Restart with the same files. A retained preparation binds their bytes, the runtime, recipe and original source baseline; authorized feature edits do not trigger a new baseline qualification. A duplicate owner is rejected. Changed runtime/input identity, uncertain ownership or exhausted limits produces a specific hold. Pause remains paused, and an existing approval is not recreated. The owner stops its work and listener on terminal delivery or an actionable hold; verify the returned cleanup outcome before considering the run finished.

The writer can edit the isolated checkout within declared change roots; the independent assessor reads the frozen candidate. Model permissions continue to protect repository controls, credentials and authoritative state. The explicitly approved **reviewed local tests** tier uses these fields in the maintained project configuration; retain its other required fields:

```json
{
  "recipe": {
    "execution_policy": "isolated-checkout-v1",
    "native_process_ownership": "host-owned-v1",
    "checkout_policy": {"native_validation": "reviewed-host-v1"}
  },
  "readiness": {"checks": []}
}
```

For this tier, preapproval readiness checks model roles, configuration and host metadata; it runs no project native checks. After the ordinary feature approval, a completed independent review must pass for the exact candidate before any host check starts. Missing, failed, interrupted or stale review evidence prevents host execution. The maintained commands use ordinary project tools and caches, the original deadlines and owned-process cleanup. They require no additional per-command approvals.

**Native authority:** tests, build scripts, dependencies and generated programs run with normal user permissions, including access to user files, credentials, AOS control files, local services and external networks. Treat that code as trusted. Model isolation remains enforced, but patch review cannot prove the later native code harmless. Cleanup covers observed owners and their process group; it cannot exclude unobserved detached processes. Before/after resource checks enforce observed growth/free-space limits, not transient peaks.

Plan for **2 + 3R + S assignments** in this tier: implementation and assessment, then one independent failure-triage job, repair and reassessment per permitted correction (`R`). A clean static review followed by a native failure can trigger that separately budgeted triage; it reads the exact retained failure evidence and launches no host checks. Only confirmed in-scope findings unlock repair, followed by fresh review and checks. Inconclusive findings or exhausted allowance return not-delivered evidence; no hidden final diagnostic job is added. A successful first pass still uses only implementation and assessment.

The existing constrained native route keeps **2 + 2R + S assignments**. The startup-retry allowance (`S`, default 0, maximum 2) remains separate in both modes. Each launched job retains its original deadline and full reservation. Only exact zero-model-work proof, known unchanged effects and positive owned cleanup can authorize a startup retry; failures are never refunded and unknown outcomes remain held.

`native_local_tests` is a separate optional constrained-network policy. It cannot be combined with `reviewed-host-v1`, is not the approved reviewed-host route, and has not qualified the Herdr full suite. A constrained-policy failure never selects host execution automatically. The reviewed-host route has passed local host qualification and the G2/G3 static-review correction rehearsal; live restart and native-failure triage are not claimed by that rehearsal. Keep automated checks separate from any required human acceptance. Commit, push, installation and publication require their actual authorization.

## Earlier qualified boundary

AOS component **0.2.4**, checked code `ce99e5d`, has independent acceptance for the supported tool handoff: 441 code checks, a real two-agent Rust trial with separate build and three passing tests, automatic handoff, cleanup, bounded replay, preserved original budgets/decisions and local dashboard/HTTPS adoption. The receiving Herdr chat verified the same version and approved/delivered work through CLI and HTTP. Earlier `d527ea5` Python workflow and awake idle-worker desktop-restart evidence retains its exact scope. Product commands, toolchain access and acceptance limits belong to the owning project. Herdr’s earlier v0.3 requirements approval is retained; its historical failed execution was retired through supported local recovery with the charged reservation preserved. New product execution requires its exact qualified recipe, resource permissions, native UI/sensor/performance checks and separately approved allowance. No app reload is needed for fresh CLI calls.

Use the explicit CLI state directory and worker roots for the intended shared service. A connector reporting version 0.2.3 can still point at earlier prototype state: if that connector cannot find work which the authoritative ledger contains, investigate the connection without recreating the approval. The original prototype connector is not the coordinator connection described below. Verify the receiving chat against the same known work, source identity and dashboard before handoff; a check in another chat does not satisfy that requirement.

## Built-in coordinator connection

AOS **0.2.5** adds the optional `serve-coordinator` MCP transport over the existing coordinator contracts. The earlier 0.2.4 acceptance above retains its exact scope. This connection does not create another ledger, answer approvals or change worker execution logic.

Configure `aos serve-coordinator` with the same `--state-dir` and complete `--worker-writable-root` list as the shared service, plus its existing `--decision-origin https://127.0.0.1:8443`. Use a trusted project’s local `.codex/config.toml`; do not publish machine paths or enable the coordinator in assigned workers. The old `serve` command remains the isolated M0 compatibility/probe surface and must not be confused with this route.

The connection exposes seven tools:

- `interface_capabilities`: exact component/source identity, connected ledger and declared roots.
- `read_work`: current record and operator capabilities, including separate approval and execution status.
- `read_service_status` and `read_dashboard`: the existing ledger projections.
- `operation_schema`: the strict input schema for an operation.
- `coordinator_operation`: introduce work, present a complete concept, register its contract, prepare a decision, reconcile existing prerequisites, admit, pause, resume or cancel.
- `wait_for_decision` (0.2.6): observe the exact prepared HTTPS decision during one bounded pending tool call.

Read the operation schema, supply its exact request, and read work back after a mutation. Concept requests include the complete execution envelope; omitted fields are not silently added. Operation IDs, stale revision/digest rejection and approval/resource/budget gates are identical to the CLI. `prepare-decision` returns the existing HTTPS link and accepts no approval answer. A successful tool response can still contain a recorded hold; inspect the execution state and blockers.

Admission makes approved work eligible. It does not launch a worker process. The existing bounded `aos worker` command still owns execution and cancellation cleanup; a coordinator must explicitly operate that process with the approved recipe. There is no new background launcher, process-stop tool or automatic chat wake-up. The optional worker audience is read-only, and actual assigned-agent filesystem/network/integration denial remains mandatory.

After changing a project connection, refresh that MCP connection and verify tool discovery in the receiving chat. Confirm `mcp_coordinator_version: 2`, the expected state directory and current work before using mutations. A configured file or a test client alone does not establish that an already-open chat has refreshed its tools. Historical Store fields such as `dispatch_available` describe the old direct interface; use the `operator` receipt for transport availability and the lifecycle state for execution eligibility. A running dashboard can retain its accepted process identity while a newer coordinator reads the same ledger; compare ledger revision/digest separately from process code identity.

### Wait for a saved decision

AOS **0.2.6** adds a read-only wait to the trusted coordinator connection. Present the exact decision link, then call `wait_for_decision` with the `work_id`, `challenge_id` and `digest` returned by preparation. `timeout_seconds` is an integer from 0 to 30, default 30. Zero reads immediately. The existing HTTPS service remains the only answer route.

The wait checks the existing ledger inside a single pending MCP request, with short asynchronous sleeps and no database transaction held between reads. It creates no notification ledger, background process or model-polling loop. Separate HTTPS and MCP processes can observe the same committed decision. Database lock waits are limited to 50 milliseconds per read; a missing/unreadable database or malformed saved response returns a reconciliation hold. Invalid request identities and unsafe state configuration remain rejected tool calls. A verified answer can return to the waiting chat without a manual “done” message while that request is open.

| Result | Coordinator action |
| --- | --- |
| `decision_recorded` | Inspect the outcome. Read current work and use the existing guarded admission only if appropriate. Approval can still have prerequisite, pause, budget or runtime holds. |
| `timeout` | End this wait and retain the same pending request. Do not cancel work, invent an answer or request another approval. A later explicit wait uses the same identities. Do not keep retrying indefinitely. |
| `superseded` | The proposal or unanswered request became stale. Read current work; the old answer cannot authorize a newer proposal. |
| `held` | Preserve the work control, terminal state or recovery-copy hold. |
| `reconciliation_required` | A saved answer lacks verified readback, its receipt changed, or the ledger is unavailable. Reconcile the original decision through the existing recovery route; do not ask for approval again. |

Disconnecting or cancelling the tool ends observation only. Reconnect and retry with the same identities to read the same decision. Two waiters may observe the same receipt; this is not exactly-once execution. Stable mutation operation IDs and transactional admission remain responsible for avoiding duplicate work. Worker audiences cannot call the wait tool.

This feature returns to an **already pending tool call**. It does not wake an idle desktop chat, launch workers, or extend an approved deadline. Source/protocol tests do not qualify the desktop client's timeout or automatic model continuation. In the next trial chat, first verify the selected component candidate, coordinator interface 2, tool discovery and the intended ledger; then observe a real human decision while the wait is pending. An answer arriving after the wait ends remains durable but does not wake the chat. A desktop restart or shared-service replacement is not required by this change.

## Environment and authority

Run from the source checkout using its locked environment:

```sh
uv sync --locked --dev
uv run aos version
uv run aos --help
```

`version` returns the component and distribution versions, command schema version, and a SHA-256 digest covering the AOS, reusable check-adapter and maintained project-adapter Python package files. It also reports every allowlisted adapter's ID, integer version, evidence kind and adapter-package digest. These identities exclude state, configuration, dependencies and documentation; retain the tested Git commit separately. `status` includes the same receipt under `operator`. Neither command invokes a model or reconciles work.

Manual state commands require `--state-dir` and one or more `--worker-writable-root` options. Use the same authoritative directory and complete root list across the coordinator, decision server and worker. State must be private, user-owned, mode 0700, outside every worker-writable root, and inaccessible to assigned agents. The runtime sandbox must enforce that denial, including decision credentials and the loopback decision endpoint. A root declaration or an `--audience` label cannot replace OS enforcement. Assigned agents must not receive the coordinator connection or decision capabilities.

Run the coordinator from an already authorized chat or administrator context. The CLI performs no role authentication of another process running as the same OS user. It inherits the existing filesystem and runtime boundary; do not expose arbitrary coordinator command execution to assigned agents.

## Command contract

| Command | Input and effect |
| --- | --- |
| `prepare-project --project FILE --request FILE` | Validate maintained inputs, readiness and finish capacity without product dispatch. |
| `start --project FILE --request FILE` | Own readiness, the human decision page and exact-work execution through delivery or a specific hold. |
| `introduce-work --request FILE` | Save title, structure and provisional team from a strict JSON request. |
| `present-concept --request FILE` | Save the versioned concept and complete execution envelope; return its canonical digest. |
| `register-contract` | Copy the exact saved concept's execution envelope into the existing lifecycle contract and bind its concept revision/digest. No independent scope copy is needed. |
| `prepare-decision` | Prepare the existing HTTPS request; an identical retry reads the same current challenge or verified saved decision. It never supplies an answer. |
| `admit` | Re-read and verify the saved approval and contract, check prerequisites/ownership, then enqueue eligible work or record a hold. It does not start a model itself. |
| `reconcile-prerequisites --request FILE` | Record evidence for existing named technical prerequisites before first admission, retaining the exact approved package. |
| `status WORK_ID` | Read the durable work record. `--surface service` and `--surface dashboard` project the same authoritative records. |
| `pause`, `resume`, `cancel` | Delegate to existing lifecycle control and owned-effect reconciliation. |
| `worker` | Run the existing worker with an explicit cycle cap and either `--fake-plan` or the separately approved `--runtime-recipes`. |
| `dashboard-serve` | Explicitly run the packaged read-only loopback dashboard for an allowlist of work aliases and a chosen port. |
| `dashboard-inspect`, `dashboard-stop` | Read the private dashboard process receipt; inspect its exact instance or request its authenticated graceful stop. |
| `backup-ledger`, `restore-ledger` | Create an exclusive private ledger backup or restore its exact identities into a fresh, dispatch-held recovery directory. |
| `recover-legacy-cancellation --request FILE [--apply]` | Preview a narrowly qualified legacy retirement; apply only the exact independently reviewed current-state evidence. |
| `recover-startup-failure --request FILE [--apply]` | Preview a qualified pre-thread failure recovery; retain the failed attempt, charges and resource breach while releasing verified stopped ownership. |
| `continue-recovered-startup --request FILE [--apply]` | Preview a separately approved continuation after recovery; preserve historical failure and cumulative accounting, then require explicit admission. |
| `archive-preview`, `archive-prepare-local`, `archive-publish-local`, `archive-read-local` | Preview, retain and verify milestones against the disposable local destination; external publication remains unavailable. |

`register-contract`, `prepare-decision`, `admit`, `pause`, `resume` and `cancel` require `--operation-id`, `--work-id`, `--expected-work-revision` and `--digest`. The digest identifies the current concept; the work revision identifies the current mutable record. Read status after intervening operations or worker progress. Mutation checks are transactional. A stale revision/digest is rejected rather than silently refreshed.

Keep an operation ID and its complete arguments stable across a retry. The saved result may describe the original operation rather than the current state; read status for current facts. Reusing an ID with different arguments is an error. A newly considered attempt after a hold needs a new ID and freshly read revision. Do not retry a held admission with the old ID expecting it to become eligible.

Commands emit JSON results. Exit 0 means the command completed, including replay of an existing result; inspect the result. Exit 2 means rejection or unavailable input/state, with an error object on stderr (argument parser errors use normal argparse text). Exit 3 means an explicit operational hold: admission/resume has recorded blockers, or a dashboard listener is unavailable. Details appear on stdout. A held admission is not dispatch permission. The foreground worker prints no per-cycle result; use status. Preserve error evidence before correcting the named cause.

Pause fences new dispatch. An already running assignment may complete; its result and pause survive worker restart. Resume rechecks existing authority and prerequisites. Cancel persists `cancel_requested`; the worker must subsequently reconcile its exact owned effects. Resources remain owned while effects are unknown. Stopping a foreground worker process does not cancel work or prove child cleanup. Terminal delivered/cancelled/retired work cannot be resumed.

Keep one foreground worker alive for the approved assignments. `--max-cycles` bounds iterations, not elapsed time; each tick can include bounded runtime work, and the original assignment deadlines still apply. Short repeated invocations can close a healthy session when their cycle caps expire. The worker does not exit automatically when delivery completes. Once authoritative terminal state, released resources and positive runtime cleanup receipts establish that no owned work remains, Ctrl+C or SIGINT to that exact worker runs its normal shutdown; verify process exit and stopped worker state afterward. SIGTERM and SIGKILL do not provide that cleanup path.

After an approve, decline or cancel answer is saved, **Back to decisions** performs a same-tab GET of the current decision list/status. It does not resubmit the form. The outcome and exact saved identity stay visible on the result page; replaying the same answer reads that original decision.

## Runtime readiness and operator helpers

### Startup failure and instruction readiness

Component **0.2.7** reconciles a matched `thread/start` rejection without inventing a model run. It preserves the original reservation and deadline, records the exact rejection, stops only the owned server, and verifies source/output inventories and child cleanup. Only this positive evidence yields `startup_failed`; EOF, contradictory replies, unknown owners, lingering effects and incomplete observations remain held. Restart, poll and cancel reuse the recorded attempt rather than launching a replacement. Historical uncertain attempts are not retrospectively cleared by this repair.

Before preparing a model recipe, inspect the applicable instruction chain:

```sh
uv run aos instruction-manifest --source /absolute/project --support-file /absolute/required-policy
```

Bind the resulting object as `instruction_manifest` in the exact recipe. Discovery follows global instructions and the configured repository-root-to-working-directory chain, including override/fallback names and explicit supporting files. It pins bytes, aliases and discovery settings. Dispatch rejects missing instructions, drift, truncation and protected paths; configuration and credential directories remain denied. This is a manifest of required files, not permission to widen the approved recipe.

If the complete applicable chain exceeds the configured document limit, pass `--project-doc-max-bytes 131072` (or another sufficient explicit value) to `instruction-manifest`. The resulting `runtime_overrides` is part of the exact manifest and recipe. It may increase the configured limit up to 1 MiB; it cannot truncate guidance or lower the configured value. Startup applies that process-only setting and verifies its effective value before and after session creation. Global configuration and instruction files remain byte-pinned and unmodified. Include every required `--support-file` when producing the final manifest.

The runtime performs actual startup readiness before `turn/start`. Its internal `start(assignment, preflight_only=True)` qualification path opens and cleans up one thread without a model turn; it still consumes the original reserved attempt and cannot be promoted or retried as product work. It is not a general worker mode or proof that a model followed the instructions.

Component **0.2.8** additionally records a session acknowledged before a failed preflight as an `acknowledged_no_turn_failure`. Exact thread/attempt identity, zero turn intent, positive owned cleanup and unchanged source/output are required. A failed preflight is never successful qualification. Unknown or malformed acknowledgments, legacy records without this evidence, and changed effects remain held. No proof is carried across a subsequent model-turn intent.

When a new project's trust entry is expected during session creation, `instruction-manifest --allow-project-registration` can explicitly declare that one transition in the recipe. This requires an existing global configuration and no entry for the exact canonical project root. Before dispatch the manifest remains byte-exact. After acknowledgment only that absent entry becoming exactly `trust_level = "trusted"` is permitted; all other parsed configuration, config paths/aliases, instructions, support files and discovery settings must match. The runtime retains before/after hashes without attributing the writer, rereads effective configuration and repeats the actual permission probe. It never writes a trust setting itself. Legacy manifests have no exception. A fresh recipe manifest is required for later assignments after a configuration change; startup qualification cannot be promoted into product work.

**Host layout:** the earlier global instruction symlink was denied by the pinned runtime even though its target was readable. The owner replaced it with a byte-identical regular file; actual instruction access and session creation then passed. The retained first regular-file qualification failed on a new project-trust entry and incomplete post-acknowledgment finalization. Two subsequent actual 0.2.8 checks independently qualified clean rejection and declared startup with writer/assessor permissions and no model turns. Each future recipe still needs its own exact binding. The old symlink failure is not waived, and a policy reinstall can restore that layout. No directory permission expansion, home substitution, installed-policy edit or runtime upgrade is included. Historical legacy attempts without contemporaneous ownership/outcome evidence remain held; this repair cannot manufacture their cleanup or release their resources.

### Retirement of legacy ownership

`recover-legacy-cancellation` is a host-only operation for a single legacy assignment held at `thread_request`, with zero model-turn intent and no contemporaneous startup or stop receipt. It requires a verified local reboot after that attempt, a stopped worker, current process/open-file quiescence and independently reviewed adoption of the exact preserved source contents. Ordinary cancellation still requires its original positive cleanup evidence.

The request binds `operation_id`, `work_id`, `expected_work_revision`, the concept `digest`, `attempt_id`, the original `runtime_record_sha256`, and a protected evidence file's `evidence_path` and `evidence_sha256`. Inspect current facts with `legacy_recovery.inspect_legacy`; retain the authority, local execution provenance, complete delivery inventory and an independent affirmative report bound to that exact host and snapshot digest. The bundle and its references must be private user-owned regular files outside every worker-writable root. An operator-authored authority record describes existing human instructions; it is not a formal decision-page answer.

Run the command without `--apply` to preview the exact packet. Apply rereads the facts while holding the worker lock and a ledger transaction. Drift, active access, a different boot, conflicting stop claims, mismatched assignments/resources, or changed budget bindings reject retirement. A successful result sets execution to `retired` and releases the recorded resources. It preserves the original uncertain runtime record, charged reservations, approvals and content; it does not accept a delivery, manufacture a historical cleanup receipt, refund an allowance or admit other work. Keep the same operation ID and request for a retry. A later feature still needs its own valid execution binding.

`recover-startup-failure` separately reconciles the supported pre-thread failure whose concurrent cancellation lost its original no-turn proof. It requires the pinned producer, independent main-path failure evidence, an immutable copy of the original runtime row, exact current records, a stopped worker and fresh process/filesystem observations. Its preview changes no execution records; apply commits the separate terminal receipt, assignment/resource disposition and stopped resource guard together. The original failed runtime row, reservations and resource breach remain. The work stays held: this recovery neither acknowledges the observation gap nor authorizes another attempt. Unsupported producers, drift, contradictory effects and unresolved owners reject recovery.

After that recovery, `continue-recovered-startup` supports one narrowly scoped continuation of the same work. It requires a new verified human decision bound to the exact remaining execution contract, the original resource guard, recovery evidence and explicit uncertainty about the unobserved interval. The operation archives the original guard and contract, preserves all failed assignments and charged reservations, and installs the approved contract with a fresh resource-failure latch. It retains the original baseline, byte caps and observed high-water floors; later failures still stop execution. A pending or failed continuation remains held and cannot discard an observation through retry. Successful continuation stays held until ordinary explicit admission creates its one audited successor. This is not an approval answer, a refunded attempt or a general reset.

### Session recovery and decision briefs

An expired browser session, missing cookie or invalid session token now explains how to recover and offers **Reopen the proposal and check its status**. The link performs a read-only GET. It preserves saved answers and never repeats the submitted choice; a pending proposal requires a fresh explicit answer. Host, origin, fetch-metadata and CSRF checks remain enforced. Actual rendered-browser recovery remains a qualification requirement (#50).

Use the [decision brief](templates.md) when preparing a choice for the user. A new concept may include `decision_context` with six nonempty plain-text fields: `problem`, `example`, `effort`, `dependencies`, `evidence` and `stopping_point`, each at most 4,096 characters. Supply `recommendation`, `alternative` and `consequence` together. The full brief is bound into the package digest and rendered above the expandable technical record. Omitted context leaves historical packages unchanged. The fields do not enforce factual accuracy or readable language; representative decision-owner review remains necessary (#43).

### Selected backlog reconciliation

The bounded #34 helper reconciles one existing operational-table ID and its already mapped issue/project item. GitHub owns tracker status, urgency and importance; Markdown owns the maintained outcome and disposition. Neither side supplies AOS approval or delivery acceptance.

```sh
uv run aos backlog-preview --markdown futures.md --item B01 --direction github-to-markdown --project-owner OWNER --project-number 1 --intent /absolute/private/inbound.json
uv run aos backlog-reconcile --intent /absolute/private/inbound.json --apply-local
uv run aos backlog-preview --markdown futures.md --item B01 --direction markdown-to-github --project-owner OWNER --project-number 1 --intent /absolute/private/outbound.json
```

Inspect the retained preview before applying it. Local apply requires fresh matching issue/project and source observations; previews older than 15 minutes require a new preview. It changes only the selected row's managed tracker span and verifies the result. Repeated reconciliation of the same completed intent is read-only. Malformed markers, conflicting edits, duplicate identity and missing project coverage are held with failure history. The returned status names observation and last successful reconciliation times.

Outbound reconciliation prepares a managed issue-body block without writing to GitHub. Review the entire public payload, then use the existing explicitly authorized issue-maintenance route and run `backlog-reconcile` without `--apply-local` to verify fresh remote readback. Pattern checks do not replace disclosure review. Unmanaged issue text remains intact. This helper does not create items, continuously synchronize, modify project fields or provide a cross-system transaction; the broader #34 remains open.

## Check adapters and allowances

AOS dispatches exact argument lists, owns processes and validates normalized evidence. The separate `agentic_tools.check_adapters` package supplies a static allowlist: `pytest-v1`, `native-build-v1`, `cargo-test-v1` and `native-tests-v1`. There is no runtime discovery or worker-supplied adapter code. A recipe check selects `result_kind` as `pytest`, `build`, `cargo-test` or `native-tests`; an optional `adapter_id` must agree. Historical untyped recipes retain their original pytest/JUnit interpretation. Typed recipes must supply an explicit `probe_interpreter`; the permission probe never infers Python from a native build command.

Only the pytest adapter appends its historical JUnit/cache-provider arguments. Native build and Cargo commands retain their exact approved arguments. A literal `{output}` inside an argument is replaced with that attempt's isolated output directory without shell evaluation. The project must approve the full toolchain command and readable roots. A denied loader dependency is a technical blocker; adapter selection cannot expand permissions or alter the approved recipe. Any required loader environment belongs in the project's exact argument list, not in AOS product logic.

Each result binds its candidate, adapter identity, exact executed arguments, exit code, normalized counts, raw stdout/stderr hashes, adapter input artifacts such as JUnit, and combined-output hash. A build reports kind `build` and zero selected tests; it cannot satisfy a test obligation. Tests require recognized results, positive selection and every selected case passing. Failed, skipped-only, zero-test, malformed, missing-output and unrecognized results block acceptance. Cached native evidence is checked for raw-output drift before replay. The approved `runtime_recipe.checks` defines each named build/test obligation; a generic `check_kinds` map can also declare obligations for other evidence routes.

The native test adapter additionally requires an exact `suite_counts` map, for example `{"unittest": 1, "bun": 1}` for a command that runs both suites. Supported families are `nextest`, `unittest` and `bun`; every declared invocation must produce a complete report with positive selection. Missing, additional or inconsistent reports reject the result. Nextest's selection-filtered cases are retained separately as `unselected`, while selected skips cannot pass. The immutable command and its zero exit status remain necessary; a passing sibling suite cannot hide an absent or failed suite. This parser does not qualify filesystem permissions or native child cleanup.

An exact recipe may declare `native_write_roots`, each containing an absolute `path` and a `kind` of `generated`, `cache` or `cache-file`. Generated directories must be strict source children disjoint from every mutable or immutable source file; only those declared directories are excluded from the frozen source fingerprint. Cache directories stay outside source and evidence. A cache-file grant names one existing or absent regular file under a real parent, without granting adjacent writes. The writer and source assessor do not receive these native write permissions. Separate snapshots retain generated/cache effects and root identities; they do not attribute unrelated global-cache activity to the check.

Native declarations reject protected paths and physical aliases, including hardlinks outside the declared writable namespaces and macOS volume aliases. Trusted interpreters, their libraries, instructions, adapter code and receipts remain protected. Overlapping native jobs are rejected before probes or output creation, and cached check evidence must match the same permissions and check obligations. Retaining a generated directory does not authorize deleting it or unrelated caches.

Native hardlinks are permitted between declared generated or cache directory roots, with read-only and protected descendants excluded. Optional `temporary_hardlink_paths` entries reference exact `parent` and `name` pairs already declared as temporary trees; only those trees gain the capability, and preexisting matching paths remain denied. Both endpoints require permission on the qualified macOS kernel. Output directories, other temporary paths and individual cache files do not acquire hardlink permission merely because they allow writes; retained alias observations still have to reconcile.

A failed native permission probe can record `preparation_failed` only when the same trusted call positively observed its fixed isolated probe exit and no product supervisor was created. The result retains the exact probe, profile and source/output effects and replays as a failure. A saved intent alone, an interrupted preparation, an ambiguous launch or contradictory owner/effect evidence still holds ownership; none is converted into successful cleanup or a passing check.

Replay of completed preparation cleanup verifies its recorded temporary before/after observations and their proof bindings. Later operating-system changes to those temporary files do not reopen completed ownership. `native_preparation.restore_completed_failure` can preview and restore a previously downgraded completed receipt using its exact check ID, raw-record hash, original receipt digest and stable operation ID. It restores the original cleanup evidence and phase only, with a separate atomic audit record. Live preparation still checks current temporary state; restoration supplies no new cleanup observation or resource allowance.

Routine shutdown selects unresolved native checks and preserves completed history. If an earlier housekeeping failure removed completed cleanup from an ordinary check with a saved kernel-census receipt, `native_cleanup_recovery.restore_completed_cleanup` supports a read-only preview and atomic restoration of up to 64 exact records. The request binds an original journal snapshot, each current and prior raw-record hash, the full saved receipt digest and a stable operation ID. The retained cleanup must reproduce the original record exactly; current process, profile, output and resource-identity checks must also pass. Restoration records a separate audit and changes only the original phase and cleanup fields. It neither recalculates historical effects nor changes test outcomes, accounting or execution authority.

The macOS native route can select `native_process_ownership: "sandbox-census-v1"` with `native_sandbox.version: "macos-seatbelt-v1"`. A trusted observer identifies processes through the kernel's same-sandbox signal checks, including detached descendants, and sends cleanup evidence over a private control channel. Workload output cannot supply that evidence. A missing observer, incomplete census or changed process identity keeps ownership held. The ordinary model process route receives none of these native capabilities.

The declarative native profile can bind exact `unix_socket_roots`, `allow_pty`, and `temporary_paths` entries with `parent`, restricted `name` template and `kind`. Existing matching temporary paths are observed and denied; a name match alone never proves ownership or authorizes cleanup. Normal project temporary locations remain in use. Separate `directory_create_paths` permit only creation of exact directories and normal already-exists results; they grant no child access, socket, chmod or deletion capability. Optional `iokit_user_clients: ["AppleSMCClient"]` grants access to that user-client class; it is not a kernel filter limiting which SMC commands a program can issue.

`owned_temporary_roots` references exact literal temporary-tree entries that the trusted controller must create exclusively. The controller retains the original temporary snapshot and journals each new directory's identity before execution. After verified process shutdown, it can remove only that same empty directory. Preexisting paths, replacement, unexpected contents or incomplete creation evidence remain held; there is no adoption by name or recursive cleanup. This supports repeated checks that normally leave an empty application temporary directory behind.

`directory_read_paths` permits reading an exact existing directory, including its entry names, without granting access to child contents or writes. This supports native tool selectors that inspect their installed toolchain's root while retaining separate explicit grants for the executable and SDK subtrees. It supplies no model permission or cleanup ownership.

`file_read_paths` binds exact canonical regular files with their SHA-256 identities for native checks. These declarations preserve explicit denies and protected credentials, and supply no directory, sibling, write or model permission. Writer and assessor profiles configure exact denials, including equal physical aliases, after applying their normal and instruction-file grants. Startup probes must demonstrate those denials on the actual kernel; unsupported combinations with built-in platform access reject before a model turn. Qualify the exact file and its native consumer before including the declaration in an execution recipe.

Model profiles consolidate immutable branches after applying alias-aware denials, while preserving explicit permission boundaries. Writers retain writable ancestors of every exact mutable destination, including absent files, for temporary siblings and atomic saves. Entirely immutable branches become read-only, including future unlisted files within them. Assessors remove only redundant read entries. Uncertain path identity retains the original entries; native check profiles are unchanged. This reduces permission-resolution work without increasing access or execution limits. Exact effective-configuration and operating-system checks remain required for each product recipe.

A recipe can opt into `model_write_scope: "existing-mutable-files"` when every mutable destination already exists as a canonical, regular, single-link file. This mode keeps the source read-only and grants the writer access only to those exact files, preserving other permission boundaries. Direct edits are supported; atomic saves can use a temporary file in the attempt output directory on the same filesystem, followed by replacement of an approved file. Unlisted source files and temporary siblings are not writable. Invalid or missing destinations reject this mode rather than widening access. The reviewer remains read-only. Before a model turn, the permission probe checks every mutable file with a nonmutating write-open and verifies its identity and contents; native check permissions and probes are unchanged. Omitting the option retains the preceding behavior.

`directory_write_access_paths` supports native tools that first test whether a declared temporary parent is writable. It applies only to exact existing parents already named in `temporary_paths`; child creation and access still require the separate templates. The literal directory capability grants no directory listing, metadata changes, removal or subtree writes. Qualify both the tool's normal parent selection and denial of neighboring and preexisting files.

Optional `ephemeral_tcp` binds the host's qualified port range. The observer records existing TCP listeners and excludes them, together with `denied_tcp_ports`, from the generated profile. The retained profile uses individual positive outbound port rules and a TCP-only inbound rule; broad localhost grants are unsupported. This is host-local TCP access, not a private network namespace. Protected decision endpoints, UDP and undeclared Unix sockets remain denied. An exact command requiring `pgrep -af` can use the protected wrapper generated by `native_pgrep.wrapper(probe_interpreter)` at the front of its explicit PATH. Bind the wrapper and helper source hashes; do not grant the system process-information broker merely to make the system `pgrep` work.

Optional `resource_limits` binds `output_roots`, `source_evidence_roots`, `max_output_growth_bytes`, `max_source_evidence_growth_bytes`, `min_free_bytes` and a `sample_interval_seconds` from 1 to 10. The source and attempt evidence must be covered, with declared generated output counted separately. A durable work baseline survives later assignments and restarts. Observed breaches, failed observations and active observation gaps prevent acceptance even if cleanup later frees space. This is bounded sampling and stop-on-observed-breach behavior, not a filesystem quota; the exact recipe and its qualification must state that limit. Native execution also retains the original assignment and check deadlines rather than restarting their clocks after reconnect.

Resource sampling follows that declared interval; process polling does not repeatedly rewrite the same baseline. Normal observers scan and serialize outside the SQLite writer transaction, then compare the exact saved record before committing. Concurrent observations retain observed failures and the largest recorded growth. A committed observation from the same owner can refresh the freshness anchor during a retry; the original operation and execution deadlines remain fixed. Serialized resource records have a 128 MiB bound; oversized state is rejected. Contention retries remain bounded by the original observation deadline, and a failed durable write leaves protected failure evidence that prevents acceptance after cleanup or restart. These protections preserve the journal's existing transaction mode and do not enlarge any allowance.

Native admission reads a verified SQLite covering index of ownership identities before loading unresolved records. SQLite maintains the index with the authoritative rows; missing or changed schema and malformed identities hold admission. Installation on legacy history requires a passive journal with completed native records.

New technical phases use compact control records for admission, observer identity, resource ownership and the original clocks. Large historical records are written before activation or after verified shutdown. A passive phase cannot launch native preparation; its exact observer must be started and alive, and its resource guard active. Reconciliation closes admission before publishing stopped execution and keeps incomplete cleanup on the compact path. Legacy active phases without these controls are available only for cleanup. Accounting, recovery and export also validate compact authority and retained stopped phases. Archive revision checks reject intervening writes, including older writers that would otherwise hide active execution or completed charges.

Native permission profiles are rendered by one bounded trusted Python child so expensive path comparisons do not starve the coordinator's resource observer. The child receives pinned source bytes and exact inputs, disables site imports and bytecode, and forbids forks, network use and filesystem mutations. Its output is bounded and accepted only after successful exit, source verification and exact profile hashing. Rendering consumes the original check and phase deadlines. Failed or interrupted preparation requires positive child reaping; ambiguous ownership remains held. Resource and execution authority are checked again before creating an owned temporary root.

Unix socket rules combine forbidden prefixes that share a parent into bounded groups. Canonical negative prefixes use literal `subpath` predicates; uncertain spellings retain the regular-expression fallback. Every forbidden prefix is retained, and a group is omitted from a positive rule only when the two cannot overlap. Separate denial rules cover the complete set. This limits repeated policy construction without broadening socket access.

`TechnicalQualification` provides host-only baseline testing through the same native boundary in a private journal. Its receipts always carry `accepted: false`; it creates no concept, decision, model assignment or product reservation. Each phase has one original deadline and retains exact source and product-budget byte checks. A corrected phase requires positively stopped prior execution and retains the original resource roots and ceilings. Observed growth is conservatively debited across phases. Export returns lower remaining limits and an immutable `resource_limits_carry_forward` capsule whose saved baseline also counts growth during the later approval wait. Export seals the technical journal against further spending. The final human-bound recipe must preserve the capsule and resource scope; baseline tests do not accept a future feature or authorize its execution.

For a reviewed observation gap that occurred entirely after owned workload shutdown and before any next workload launch, `acknowledge_quiescent_gap` can append a host-only disposition to that private journal. It validates the exact retained native evidence and fresh source, product-budget and resource observations. The original breached guard, blocked phase, elapsed time and deadline remain unchanged; a passive continuation preserves its original baseline and every high-water charge. The disposition grants no execution or time. A separately authorized corrected phase still needs its own bound, and export still requires a complete successful final recipe. A workload gap, ambiguous process evidence, changed protected state or a new resource failure remains blocked.

`acknowledge_prelaunch_observation_failure` supports the narrower case where a retained `Rejected` observation failure occurred in a phase whose complete native record inventory proves that neither a permission probe nor a workload supervisor started. It binds the exact original guard, baseline, prior dispositions and cumulative charges, verifies preparation cleanup and protected state, and records a fresh passive resource observation. The old failure and blocked receipt remain unchanged. A started probe, admitted workload, incomplete cleanup or changed evidence rejects this disposition; it supplies no execution authority or additional allowance.

`acknowledge_rejected_admission_observation` handles a separately reviewed refusal before a native record was admitted. It requires the exact supported refusal path, no phase-native rows or launch artifacts, joined observer threads with the original owner process proven absent, resolved historical ownership, unchanged protected state, and exact bindings to both the original guard failure and its separate observation-failure file. A new passive continuation carries their observed growth and preserves earlier dispositions. The blocked phase, both failures, original clocks and charges remain recorded; this operation grants no work or additional allowance.

`acknowledge_intercheck_observation_failure` covers a retained bounded-scan failure after completed checks and before exactly the next preparation-only check. It binds the complete ordered records to a supported, previously qualified producer, requires a healthy final resource sample taken after the last workload's census, and proves that the next supervisor, permission probe and renderer never started. The final status must explicitly record no resource failure. Protected source, product budget, prior dispositions and failure files must still match. This establishes the completed-workload boundary; it does not establish when the failed scan began or exclude unobserved external changes to shared caches. Original failures, check outcomes, deadlines and maximum recorded charges remain intact.

This recovery reserves one exact operation before making a fresh passive observation outside the writer transaction. Pending or failed claims block admission, export and other recovery operations. Completed retries return their saved outcome without another scan; an interrupted scan remains held. Finalization rechecks the original authority and exact claim ownership. A stale or failed finalization retains observed failures and growth instead of allowing another operation to bypass them. A successful disposition grants no work, time or feature approval; a new complete qualification is still required.

If a baseline build reveals another normal generated directory, `extend_generated_root` can add one existing empty directory strictly inside the same source. It requires stopped phases, unchanged source and product-budget evidence, a stable operation ID and a retained reason. The directory cannot overlap source files or protected paths; all original roots, ceilings and debits remain. Its exact identity and empty state must survive through the next captured baseline. Unexpected early contents or replacement remain a recorded failure instead of becoming an uncharged baseline. This host-only correction supplies no product approval.

Typed checks bind the selected adapter receipts from `aos version` through `execution_envelope.check_adapters`. Alternatively, when the approved package explicitly delegates named adapter selection to an existing technical prerequisite through `adapter_architecture.minimum_adapters`, the non-author assessor may produce this receipt after accepting the exact source candidate:

```json
{
  "format": "aos-check-adapters-v1", "accepted": true, "independent": true,
  "work_id": "EXAMPLE", "concept_digest": "sha256:EXACT_APPROVED_PACKAGE",
  "runtime_recipe_digest": "sha256:EXACT_APPROVED_RECIPE",
  "source_digest": "sha256:ACCEPTED_AOS_AND_ADAPTER_SOURCE",
  "check_adapters": {},
  "source_author": "approved source author", "assessor": "approved independent assessor"
}
```

Replace the empty map with the exact selected adapter IDs and complete receipts from `aos version`; include only adapters used by that recipe. The author and assessor must be distinct approved team members, and the assessor cannot be the fixture writer. Store the receipt outside all worker-writable roots, then use the existing `reconcile-prerequisites` route to satisfy the already approved prerequisite. Its reference must be `aos-check-adapters-v1:RECEIPT_FILE_SHA256_HEX:/absolute/receipt.json`. Missing, altered, stale, symlinked or worker-writable receipts hold execution. This is trusted coordinator/independent-assessment evidence, not cryptographic human authentication or a new Concept Approval. It cannot introduce an adapter ID or scope absent from the approved package.

Exactly one allowance is required in the approved envelope. `shared_qualification_budget` retains the original shared identity and ledger; its supported ceiling is ten jobs and 3,000 reserved seconds, with at most 300 seconds per job. A lower approved package remains limited to its own lower ceiling. An increased supported ceiling alone grants no jobs. Product execution instead requires all these explicit fields:

```json
{
  "product_budget": {
    "budget_id": "example-runtime-v1", "product_id": "example-product",
    "ledger_directory": "/absolute/original-state",
    "max_assignments": 2, "max_job_seconds": 300, "max_seconds": 600
  }
}
```

There is no default product allowance. Both allowance types use the original `codex-runtime.sqlite3` ledger. Historical attempts without budget metadata remain charged to the original qualification allowance, using the original full 300-second reservation when duration metadata is absent. Every failed or ambiguous launched intent keeps its slot and full reservation. Product ID and canonical source are durably bound to one allowance; renaming work, a budget, a source or a revision, restarting, or retrying cannot reset it. Different approved products retain separate totals. Product limits cannot be enlarged through this interface after first reservation; a future extension protocol requires separately defined authority. Restore copies remain dispatch-held and never reset runtime sidecars.

## Maintained dashboard process

The dashboard service reads the same ledger through the existing snapshot collector and projection. It does not reconcile workflow state, launch workers, record decisions or publish history. Explicitly start it in a foreground terminal after the selected work exists:

```sh
uv run aos dashboard-serve --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --work demo=CLI-DEMO --port 18480
```

Use your existing configured paths in place of the disposable example variables. Repeat `--work alias=WORK_ID` for each permitted view (1–32). Aliases must be unique kebab-case names; all work IDs are checked before listening. The port is mandatory; `--port 0` selects an unused disposable port. The service binds only `127.0.0.1`. It prints a JSON process receipt with its actual origin, instance, source version and renderer digest, then remains in the foreground. No persistent startup service is installed.

Open the returned origin, or for this example [the demo dashboard](http://127.0.0.1:18480/work/demo/dashboard.html). The complete read allowlist is:

| Resource | Content |
| --- | --- |
| `/` | Links to configured dashboards |
| `/health.json` | Public process/version receipt; no stop credential or state-directory path |
| `/work/ALIAS/dashboard.html` | Packaged renderer |
| `/work/ALIAS/workflow-state.json` | Current dashboard projection of that work |
| `/work/ALIAS/snapshot.json` | Exact service snapshot of that work, including its saved package/decision and factual execution records |
| `/work/ALIAS/history.json` | Read-only local milestone summaries, available only with an explicit `--archive-journal` |

Selected-work records may contain private project information. Keep this local service within its authorized audience. It serves no arbitrary files, raw evidence paths, TLS keys, decision credentials, private process receipt or unconfigured work IDs. Queries, traversal aliases, foreign Host/Origin headers and cross-site fetches are rejected; no CORS access is granted. The existing HTTPS human-decision service remains separate.

From another terminal, inspect and stop this state directory's exact dashboard instance:

```sh
uv run aos dashboard-inspect --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture"
uv run aos dashboard-stop --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture"
```

Inspection returns `absent`, `listening`, `stopped` or `unavailable`. Exit 3 means its listener could not be verified; it is not proof that an owned process stopped. The private mode-0600 `dashboard-service.json` receipt and lock live beside the ledger. One dashboard process owns each state directory. A duplicate start returns `dashboard_owned`; an occupied port returns `dashboard_port_in_use`. Both leave the existing owner/listener alone.

Stop validates the saved loopback instance before sending its private, instance-bound credential. Its control endpoint rejects browser-origin requests and unauthenticated shutdown. Successful stop waits for the reconciled `stopped` receipt and removes that credential; repeated stop reads the same result. No process is killed. Ctrl+C or SIGTERM to this exact foreground process also closes its listener and records stopped state. These operations do not stop the separate worker or alter workflow controls.

After an abrupt process failure, inspection retains the old instance as unavailable. An explicit new `dashboard-serve` may start only after acquiring the existing exclusive lock; it writes a fresh instance and credential. There is no force-kill or automatic takeover of a live owner. A stale/mismatched endpoint never authorizes stopping another process. Investigate a rejected or unconfirmed stop before reusing ownership.

The renderer lives in `src/agentic_tools/aos/assets/dashboard.html` and is included in built wheels. `templates/dashboard.html` is a relative symlink to that same file, preserving the existing download and renderer-check paths. Runtime resource loading uses the package, with no lookup in private experiment directories or the source checkout. Maintainers can run `uv build --wheel` to produce a local package artifact; installation and live adoption remain separately authorized. Fresh-export/wheel tests serve the extracted package from a directory outside the checkout without installing it.

## Technical prerequisites and recovery copies

When an approved package names a prerequisite whose technical evidence becomes satisfied later, use `reconcile-prerequisites` before its first admission. Read the current work revision and concept digest, then save a strict request such as:

```json
{
  "operation_id": "qualified-route-observation-1", "work_id": "CLI-DEMO",
  "expected_work_revision": 4, "digest": "sha256:REPLACE_WITH_CURRENT_CONCEPT_DIGEST",
  "evidence": {
    "simulated_fixture": {
      "satisfied": true, "reference": "REPLACE_WITH_DURABLE_EVIDENCE_REFERENCE",
      "observed_at": "2026-10-26T04:00:00+00:00"
    }
  }
}
```

```sh
uv run aos reconcile-prerequisites --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --request "$aos_demo/prerequisites.json"
uv run aos status --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO
```

The command requires the existing verified approval, fixed prerequisite names, exact current revision/digest, timezone-qualified observation times and nonempty evidence references. It records those observations durably and changes only the corresponding boolean technical facts. The original concept, approval, scope and limits remain intact. It refuses previously admitted work, any assignment/resource ownership, unrelated names, stale requests and conflicting replays. It does not clear independent blockers or launch work. After successful reconciliation, use a fresh admission operation ID and the new revision. There is no general active-work prerequisite editor; investigate such a hold within its existing ownership boundary.

For a disposable recovery rehearsal, create a new backup path and retain its returned `sha256` and table inventory:

```sh
uv run aos backup-ledger --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --backup "$aos_demo/backups/ledger.sqlite3"
uv run aos restore-ledger --state-dir "$aos_demo/recovery-copy" --worker-writable-root "$aos_demo/fixture" --backup "$aos_demo/backups/ledger.sqlite3" --sha256 REPLACE_WITH_RETURNED_SHA256
uv run aos status --state-dir "$aos_demo/recovery-copy" --worker-writable-root "$aos_demo/fixture" CLI-DEMO
```

Backup uses SQLite's consistent backup API and never overwrites an existing file. Restore requires exact backup bytes, integrity checks, a fresh private destination and quiescent ledger ownership. Admitted work, nonterminal assignments, resource ownership, unresolved owned stops or a running worker reject restore. Successful readback verifies identical logical table contents and saved decision identities; the original ledger and backup remain available. Failed or interrupted destinations are retained and cannot be reused.

Every restored ledger is a **recovery copy held from dispatch**. A private `restore-hold.json` marker is flushed and synced before copying the database. Status reports the hold; admission/resume remain held and the worker refuses to start. No command clears this fence or adopts the copy. Runtime, archive and shared-budget sidecars are not restored or reset. This ledger-only rehearsal establishes neither a second admitted AOS nor live migration; separately authorized adoption must retain the original shared authority and budget boundaries.

## Connected status and local history

CLI service/dashboard status and the maintained HTTP dashboard use the same work revision, decisions, assignments, blockers and usage records. Local collectors do not reconcile state or record an approval. An approved concept can therefore remain visibly held without creating a new pending decision. Completed assignment results appear as historical facts; a completed result becomes accepted history only when it matches the accepted delivery candidate.

Collector health and observation times are separate from execution state. A long-running dashboard retains its last good snapshot during a later collection failure and marks it failed/stale; a new CLI process with no last good observation reports unavailable data. Neither case invents current ownership. Usage totals retain the existing duplicate/reset counter handling; partial or excluded observations, unsupported prices and unavailable provider billing remain explicit. Token-derived estimates do not establish billed spend.

Local archive commands preserve one exact source revision and digest for each work/milestone. To exercise the existing disposable destination:

```sh
uv run aos archive-preview --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO --milestone demo-check
uv run aos archive-prepare-local --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO --milestone demo-check --journal "$aos_demo/history/intents.sqlite3" --intent-id demo-check-1 --fake-destination "$aos_demo/history/pages.sqlite3"
uv run aos archive-publish-local --journal "$aos_demo/history/intents.sqlite3" --intent-id demo-check-1 --fake-destination "$aos_demo/history/pages.sqlite3"
uv run aos archive-read-local --journal "$aos_demo/history/intents.sqlite3" --intent-id demo-check-1
```

Keep the intent ID, source and destination stable across a retry. Failed attempts remain recorded; an interrupted attempt stays `started` until a later attempt independently reads back the destination. An identical retry preserves one destination version, and a conflicting edit is rejected without overwrite. A prepared intent or a failed/started attempt is not verified publication. `archive-read-local` reads without migrating or writing the journal. These commands provide local qualification only; they grant no Confluence or other external publication authority.

Connect that journal explicitly on both status surfaces:

```sh
uv run aos status --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO --surface dashboard --archive-journal "$aos_demo/history/intents.sqlite3"
uv run aos dashboard-serve --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --work demo=CLI-DEMO --port 18480 --archive-journal "$aos_demo/history/intents.sqlite3"
```

The journal must remain outside worker-writable roots. HTTP history includes only configured-work milestone and attempt summaries, source identities, observation health and a records digest. It serves neither the journal file nor destination paths, raw archived packages or raw exception details. If history collection fails, the long-running service retains the last good records and timestamp with an explicit failure blocker; after restart with no good observation, history is unavailable. The displayed Findings/Lessons distinguish this local history from current pending work and always retain the external-publication boundary.

## Disposable simulated workflow

The example below uses only simulated output. It creates no product patch and invokes no model. Choose a fresh directory and work ID for each independent run. Do not point it at live work or use its simulated route labels as real qualification evidence.

```sh
aos_demo=private/tmp/to_persist/aos-coordinator-demo
mkdir -p "$aos_demo/fixture"
```

Create `introduction.json` in that directory:

```json
{
  "operation_id": "demo-introduction", "work_id": "CLI-DEMO",
  "title": "Disposable simulated coordinator workflow", "structure": ["fixture/"],
  "team": [
    {"name": "writer", "role": "Implementer", "grade": "AOS-G4", "model": "gpt-6.1-sol", "effort": "high", "scope": "Simulated fixture output only"},
    {"name": "assessor", "role": "Independent assessor", "grade": "AOS-G4", "model": "gpt-6.1-sol", "effort": "high", "scope": "Simulated fixture assessment only"}
  ]
}
```

Create `concept.json` alongside it:

```json
{
  "operation_id": "demo-concept", "work_id": "CLI-DEMO", "expected_work_revision": 1,
  "package_id": "CLI-DEMO-DI", "revision": 1,
  "body": "Run a disposable simulated implementation and independent assessment; no product changes or model calls.",
  "acceptance": ["Simulated fixture check passes and ownership is released"],
  "team_envelope": [
    {"name": "writer", "role": "Implementer", "grade": "AOS-G4", "model": "gpt-6.1-sol", "effort": "high", "scope": "Simulated fixture output only"},
    {"name": "assessor", "role": "Independent assessor", "grade": "AOS-G4", "model": "gpt-6.1-sol", "effort": "high", "scope": "Simulated fixture assessment only"}
  ],
  "authority": "Disposable simulation only; no model calls, product writes, commit, installation or publication",
  "execution_envelope": {
    "revision": 1, "workflow": "bug-fix", "baseline": "synthetic-v1", "behavior_delta": "Simulated fixture result",
    "required_checks": ["fixture"], "compatibility_checks": [],
    "writer": "writer", "assessor": "assessor", "receiving_owner": "demo-owner",
    "authority": "Disposable simulation only; no model calls, product writes, commit, installation or publication",
    "resources": ["CLI-DEMO:fixture"], "prerequisites": {"simulated_fixture": true},
    "qualified_route": "SIMULATED:CLI-demo", "max_assignments": 2, "max_repairs": 0,
    "max_seconds": 60, "model": "gpt-6.1-sol", "effort": "high"
  }
}
```

The request schemas reject extra top-level fields, duplicate JSON keys, invalid types and embedded approval answers. Adapter-specific fields inside `execution_envelope` are retained, including recipe digest and shared budget; the existing adapter still checks them before a real launch. Feature-change envelopes also require compatibility checks. Registration rejects incomplete envelopes and existing contracts; it does not implement replanning an existing contract.

```sh
uv run aos introduce-work --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --request "$aos_demo/introduction.json"
uv run aos present-concept --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --request "$aos_demo/concept.json"
```

Copy the returned digest into `aos_digest` below. After these two commands the work revision is 2. Register **before** preparing the decision, because registration advances that revision.

```sh
aos_digest='sha256:REPLACE_WITH_RETURNED_DIGEST'
uv run aos register-contract --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --operation-id demo-contract --work-id CLI-DEMO --expected-work-revision 2 --digest "$aos_digest"
uv run aos prepare-decision --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --operation-id demo-decision --work-id CLI-DEMO --expected-work-revision 3 --digest "$aos_digest"
```

Use the existing supervised HTTPS decision service for this exact state. If starting a disposable service, supply the operator's verified certificate, private key, unused port and qualification evidence:

```sh
uv run aos decide-serve --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --certificate /absolute/path/certificate.pem --key /absolute/path/private-key.pem --port 18443 --qualification-reference 'EXACT_QUALIFIED_ROUTE_REFERENCE'
```

The foreground service prints its loopback HTTPS URL. Open it in the authorized decision tab and review the exact proposal before choosing. Its qualification label alone proves neither a visible human answer nor worker isolation. Closing the page or stopping/restarting its server supplies no answer. Do not restart or replace a live decision server to run this example. The automated test uses a disposable certificate and synthetic HTTPS client; that client is not a human qualification.

Read back the decision and current revision. Approval in this uninterrupted example advances the work revision to 4. A saved approval remains recorded while technical prerequisites are held; do not ask the user to approve the same package again to clear a technical blocker.

```sh
uv run aos status --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO
uv run aos admit --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --operation-id demo-admit --work-id CLI-DEMO --expected-work-revision 4 --digest "$aos_digest"
```

Only `execution: eligible` admits work. Preapproval, a declined/stale approval, a changed execution envelope, unverified readback, missing prerequisites, exhausted limits or conflicting resource ownership cannot authorize a launch. Real `--runtime-recipes` also require their exact digest and the existing qualification/budget protections; do not substitute real recipes into this simulated example.

Create `fake-plan.json` for the existing deterministic runtime:

```json
[
  {"kind": "implementation", "result": {
    "status": "completed", "executor": "writer",
    "candidate": {"identity": "sha256:synthetic-candidate", "source_identity": "sha256:synthetic-source", "inventory": ["fixture.py"]},
    "output": "SIMULATED:author", "owned_processes": [], "effects_reconciled": true, "partial_mutations": "none; simulation only"
  }},
  {"kind": "assessment", "result": {
    "status": "completed", "executor": "assessor",
    "candidate": {"identity": "sha256:synthetic-candidate", "source_identity": "sha256:synthetic-source", "inventory": ["fixture.py"]},
    "observed_source_identity": "sha256:synthetic-source",
    "owned_processes": [], "effects_reconciled": true, "partial_mutations": "none; simulation only",
    "initial_reports": {"execution": "SIMULATED:execution", "review": "SIMULATED:review", "independent": true},
    "checks": [{"name": "fixture", "command": "SIMULATED fixture check", "environment": "disposable simulation", "executor": "assessor", "selected": 1, "result": "pass", "output": "SIMULATED:fixture", "scope": "synthetic fixture", "limits": "simulation only", "candidate": {"identity": "sha256:synthetic-candidate", "source_identity": "sha256:synthetic-source", "inventory": ["fixture.py"]}}]
  }}
]
```

Pause and resume before the first dispatch to exercise the controls. Admission advanced revision 4 to 5; pause advances it to 6. If anything else changed, substitute the latest revision from status.

```sh
uv run aos pause --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --operation-id demo-pause --work-id CLI-DEMO --expected-work-revision 5 --digest "$aos_digest"
uv run aos worker --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --fake-plan "$aos_demo/fake-plan.json" --max-cycles 1 --interval 0.1
uv run aos resume --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --operation-id demo-resume --work-id CLI-DEMO --expected-work-revision 6 --digest "$aos_digest"
uv run aos worker --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" --fake-plan "$aos_demo/fake-plan.json" --max-cycles 3 --interval 0.1
uv run aos status --state-dir "$aos_demo/state" --worker-writable-root "$aos_demo/fixture" CLI-DEMO
```

Expected final facts are `execution: delivered`, two assignments, an accepted **simulated** assessment, released resources and a stopped foreground worker. The worker consumes completion and schedules assessment automatically. Starting it again reconciles saved attempt IDs rather than duplicating work. No chat wake-up is implemented.

To exercise cancellation instead, issue `cancel` with a fresh operation ID and the latest revision **before delivery**, then run the same bounded worker to reconcile owned effects. Inspect status until cancellation is reconciled or an explicit unknown-effects blocker retains ownership. Do not treat the command's `cancel_requested` response as proof of stopped processes.

## Maintainer checks

The [reproduction and qualification guide](aos-validation.md) lists the complete fresh-export commands, retained regression inventory and actual-host obligations for independent assessment.

```sh
uv run pytest tests/test_aos_coordinator_cli.py
uv run pytest tests/test_aos_recovery_controls.py tests/test_aos_archive.py tests/test_aos_projection.py tests/test_aos_dashboard_service.py
uv run pytest
uv run mkdocs build --strict
```

The command tests cover disposable HTTPS preparation/answer readback, both workflow types, idempotent retries, automatic assessment handoff, pause/resume across worker processes, owned cancellation, stale/wrong identity and malformed requests. Recovery checks include preexisting projection compatibility, guarded prerequisite evidence, exact backup/restore identities, dispatch fences and interrupted restore persistence. Local history checks retain failed writes, interruptions, retries and conflicts while comparing CLI/HTTP source identities. Their synthetic HTTPS answers are isolated test evidence. Real receiving-chat connection, desktop closure/reopen, adoption and independent acceptance remain separate delivery gates.
