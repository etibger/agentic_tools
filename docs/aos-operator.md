# AOS coordinator commands

The maintained `aos` CLI is the coordinator route for introducing work, saving its exact concept and execution envelope, preparing the existing HTTPS decision, admitting approved work, and pausing, resuming or cancelling it. It calls the same Store, Lifecycle and Worker used by the existing service. The MCP bridge does not expose workflow admission or lifecycle controls.

These commands establish an operator interface. Actual desktop integration, service lifetime, worker sandbox denial and Herdr readiness require their separately retained qualification evidence. A successful simulated workflow does not establish those results. The CLI has no command or flag for supplying a human approval answer.

## Environment and authority

Run from the source checkout using its locked environment:

```sh
uv sync --locked --dev
uv run aos version
uv run aos --help
```

`version` returns the component and distribution versions, command schema version, and a SHA-256 digest of the actual AOS Python package files. This identity excludes state, configuration, dependencies and documentation; retain the tested Git commit separately. `status` includes the same receipt under `operator`. Neither command invokes a model or reconciles work.

Every state command requires `--state-dir` and one or more `--worker-writable-root` options. Use the same authoritative directory and complete root list across the coordinator, decision server and worker. State must be private, user-owned, mode 0700, outside every worker-writable root, and inaccessible to assigned agents. The runtime sandbox must enforce that denial, including decision credentials and the loopback decision endpoint. A root declaration or an `--audience` label cannot replace OS enforcement. Assigned agents must not receive the coordinator connection or decision capabilities.

Run the coordinator from an already authorized chat or administrator context. The CLI performs no role authentication of another process running as the same OS user. It inherits the existing filesystem and runtime boundary; do not expose arbitrary coordinator command execution to assigned agents.

## Command contract

| Command | Input and effect |
| --- | --- |
| `introduce-work --request FILE` | Save title, structure and provisional team from a strict JSON request. |
| `present-concept --request FILE` | Save the versioned concept and complete execution envelope; return its canonical digest. |
| `register-contract` | Copy the exact saved concept's execution envelope into the existing lifecycle contract and bind its concept revision/digest. No independent scope copy is needed. |
| `prepare-decision` | Prepare the existing HTTPS request; an identical retry reads the same current challenge or verified saved decision. It never supplies an answer. |
| `admit` | Re-read and verify the saved approval and contract, check prerequisites/ownership, then enqueue eligible work or record a hold. It does not start a model itself. |
| `status WORK_ID` | Read the durable work record. `--surface service` and `--surface dashboard` project the same authoritative records. |
| `pause`, `resume`, `cancel` | Delegate to existing lifecycle control and owned-effect reconciliation. |
| `worker` | Run the existing worker with an explicit cycle cap and either `--fake-plan` or the separately approved `--runtime-recipes`. |
| `dashboard-serve` | Explicitly run the packaged read-only loopback dashboard for an allowlist of work aliases and a chosen port. |
| `dashboard-inspect`, `dashboard-stop` | Read the private dashboard process receipt; inspect its exact instance or request its authenticated graceful stop. |

`register-contract`, `prepare-decision`, `admit`, `pause`, `resume` and `cancel` require `--operation-id`, `--work-id`, `--expected-work-revision` and `--digest`. The digest identifies the current concept; the work revision identifies the current mutable record. Read status after intervening operations or worker progress. Mutation checks are transactional. A stale revision/digest is rejected rather than silently refreshed.

Keep an operation ID and its complete arguments stable across a retry. The saved result may describe the original operation rather than the current state; read status for current facts. Reusing an ID with different arguments is an error. A newly considered attempt after a hold needs a new ID and freshly read revision. Do not retry a held admission with the old ID expecting it to become eligible.

Commands emit JSON results. Exit 0 means the command completed, including replay of an existing result; inspect the result. Exit 2 means rejection or unavailable input/state, with an error object on stderr (argument parser errors use normal argparse text). Exit 3 means an explicit operational hold: admission/resume has recorded blockers, or a dashboard listener is unavailable. Details appear on stdout. A held admission is not dispatch permission. The foreground worker prints no per-cycle result; use status. Preserve error evidence before correcting the named cause.

Pause fences new dispatch. An already running assignment may complete; its result and pause survive worker restart. Resume rechecks existing authority and prerequisites. Cancel persists `cancel_requested`; the worker must subsequently reconcile its exact owned effects. Resources remain owned while effects are unknown. Stopping a foreground worker process does not cancel work or prove child cleanup. Terminal delivered/cancelled work cannot be resumed.

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

```sh
uv run pytest tests/test_aos_coordinator_cli.py
uv run pytest
uv run mkdocs build --strict
```

The command tests cover disposable HTTPS preparation/answer readback, both workflow types, idempotent retries, automatic assessment handoff, pause/resume across worker processes, owned cancellation, stale/wrong identity and malformed requests. Their synthetic HTTPS answers are isolated test evidence. Real receiving-chat connection, desktop closure/reopen, adoption and independent acceptance remain separate delivery gates.
