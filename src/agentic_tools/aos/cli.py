"""Explicitly started local prototype; no persistent startup installation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__, version_receipt
from .interface import HumanHostQualification, create_server
from .store import Rejected, Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aos", description="AOS supervised decisions and explicit bounded workers; real recipes require exact approval and qualification gates")
    parser.add_argument("--version", action="version", version=f"AOS component {__version__}; agentic-tools distribution {version_receipt()['distribution_version'] or 'unavailable'}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Read source identity and supported command capabilities; no state or model access")
    commands.add_parser("runtime-capabilities", help="Inspect pinned SDK/API and runtime version; no model invocation")
    instruction_command = commands.add_parser('instruction-manifest', help='Inspect exact ordered instruction files and aliases; no runtime, permission or approval change')
    instruction_command.add_argument('--source', type=Path, required=True)
    instruction_command.add_argument('--support-file', type=Path, action='append', default=[])
    instruction_command.add_argument('--project-doc-max-bytes', type=int,
                                     help='Bind a runtime-only instruction limit increase, at most 1 MiB; never edits configuration')
    instruction_command.add_argument('--allow-project-registration', action='store_true',
                                     help='Declare one absent-to-trusted entry for this exact project during startup; requires recipe approval')
    for name, help_text in (
            ('prepare-project', 'Prepare one bounded project request and host readiness; no approval or product work'),
            ('start', 'Own project preparation, its human decision page and bounded continuation in the foreground')):
        project_command = commands.add_parser(name, help=help_text)
        project_command.add_argument('--project', type=Path, required=True, help='Maintained strict project configuration')
        project_command.add_argument('--request', type=Path, required=True, help='Existing introduction and concept request')
        if name == 'start':
            project_command.add_argument('--continuation', type=Path, help='Exact immutable recovered-assessment preparation; requires its own saved decision')
    backlog_preview = commands.add_parser('backlog-preview', help='Preview one mapped backlog row against its existing GitHub issue/project; no target writes')
    backlog_preview.add_argument('--markdown', type=Path, required=True)
    backlog_preview.add_argument('--item', required=True)
    backlog_preview.add_argument('--direction', choices=('github-to-markdown', 'markdown-to-github'), required=True)
    backlog_preview.add_argument('--project-owner', required=True)
    backlog_preview.add_argument('--project-number', type=int, required=True)
    backlog_preview.add_argument('--intent', type=Path, required=True)
    backlog_reconcile = commands.add_parser('backlog-reconcile', help='Fresh readback of one retained preview; optionally apply its local Markdown update; never writes GitHub')
    backlog_reconcile.add_argument('--intent', type=Path, required=True)
    backlog_reconcile.add_argument('--apply-local', action='store_true')
    for name in ('archive-publish-local', 'archive-read-local'):
        command = commands.add_parser(name, help='Disposable local archive qualification only; no network publisher')
        command.add_argument('--journal', type=Path, required=True)
        command.add_argument('--intent-id', required=True)
        if name == 'archive-publish-local':
            command.add_argument('--fake-destination', type=Path, required=True)
    for name in ("serve", "serve-coordinator", "status", "archive-preview", "archive-prepare-local", "prepare-decision", "decide-serve", "worker", "backup-ledger", "recover-startup-approval",
                 "introduce-work", "present-concept", "register-contract", "admit", "pause", "resume", "cancel",
                 "dashboard-serve", "dashboard-inspect", "dashboard-stop", "reconcile-prerequisites", "restore-ledger", "recover-legacy-cancellation", "recover-startup-failure", "continue-recovered-startup", "recover-assessment-startup", "prepare-assessment-continuation",
                 "prepare-source-review-evidence", "prepare-assessment-review-continuation",
                 "prepare-predispatch-settings-rebind", "apply-predispatch-settings-rebind", "verify-predispatch-settings-rebind",
                 "prepare-predispatch-feature-amendment", "apply-predispatch-feature-amendment", "verify-predispatch-feature-amendment",
                 "prepare-predispatch-recovery", "qualify-predispatch-recovery", "apply-predispatch-recovery", "verify-predispatch-recovery"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True, help="Private authoritative directory outside all worker-writable roots")
        command.add_argument("--worker-writable-root", type=Path, action="append", required=True, help="Declare each worker-writable root; actual runtime denial still needs verification")
        if name.startswith("dashboard-"):
            if name == "dashboard-serve":
                command.add_argument("--work", action="append", required=True, help="Explicit allowed view: kebab-case-alias=WORK_ID; repeat for multiple works")
                command.add_argument("--port", type=int, required=True, help="Loopback HTTP port; 0 requests an unused disposable port")
                command.add_argument("--archive-journal", type=Path, help="Optional explicit local history journal; read-only and never served as a file")
        elif name in ('prepare-predispatch-settings-rebind', 'apply-predispatch-settings-rebind', 'verify-predispatch-settings-rebind',
                      'prepare-predispatch-feature-amendment', 'apply-predispatch-feature-amendment', 'verify-predispatch-feature-amendment',
                      'prepare-predispatch-recovery', 'qualify-predispatch-recovery', 'apply-predispatch-recovery', 'verify-predispatch-recovery'):
            command.add_argument('--request', type=Path, required=True, help='Exact protected settings-refresh request; never supplies approval or resets accounting')
        elif name == 'prepare-source-review-evidence':
            command.add_argument('--work-id', required=True)
            command.add_argument('--git-base', required=True)
            command.add_argument('--destination', type=Path, required=True)
        elif name == 'prepare-assessment-review-continuation':
            command.add_argument('--operation-id', required=True)
            command.add_argument('--work-id', required=True)
            command.add_argument('--failed-assignment-id', required=True)
            command.add_argument('--source-review-evidence', type=Path, required=True)
            command.add_argument('--readiness-extra-attempt', action='store_true', help='Retain prior technical checks and explicitly allow one additional metadata-only attempt')
        elif name == 'prepare-assessment-continuation':
            command.add_argument('--operation-id', required=True)
            command.add_argument('--recovery-operation-id', required=True)
            command.add_argument('--support-file', type=Path, action='append', default=[])
            command.add_argument('--readiness-extra-attempt', action='store_true', help='Retain prior technical checks and explicitly allow one additional metadata-only attempt')
        elif name in ("recover-legacy-cancellation", "recover-startup-failure", "continue-recovered-startup", "recover-assessment-startup"):
            command.add_argument("--request", type=Path, required=True, help="Exact protected current-state recovery packet; never an approval answer")
            command.add_argument("--apply", action="store_true", help="Apply the exact recovery; default only previews")
        elif name in ("introduce-work", "present-concept", "reconcile-prerequisites"):
            command.add_argument("--request", type=Path, required=True, help="Strict JSON request; no approval answer is accepted")
        elif name in ("register-contract", "admit", "pause", "resume", "cancel"):
            command.add_argument("--operation-id", required=True, help="Stable ID; retry with identical arguments")
            command.add_argument("--work-id", required=True)
            command.add_argument("--expected-work-revision", required=True, type=int)
            command.add_argument("--digest", required=True, help="Intended exact current concept digest")
        elif name == "serve-coordinator":
            command.add_argument("--audience", choices=("host", "worker"), default="host")
            command.add_argument("--decision-origin", required=True, help="Existing loopback HTTPS human-decision service origin")
        elif name == "serve":
            command.add_argument("--audience", choices=("host", "worker"), default="host")
            command.add_argument("--qualified-human-host-name", help="Operator configuration only; name is not proof of human identity")
            command.add_argument("--qualification-reference", help="Reference to independently observed explicit-human host evidence; never a model assertion")
        elif name == "status":
            command.add_argument("work_id")
            command.add_argument("--archive-journal", type=Path, help="Optional local history observation from the same configured journal as the dashboard")
            command.add_argument("--surface", choices=("record", "service", "dashboard"), default="record",
                                 help="Service and dashboard share one exact read-only source snapshot")
        elif name in ('archive-preview', 'archive-prepare-local'):
            command.add_argument('work_id')
            command.add_argument('--milestone', required=True)
            if name == 'archive-prepare-local':
                command.add_argument('--journal', type=Path, required=True)
                command.add_argument('--intent-id', required=True)
                command.add_argument('--fake-destination', type=Path, required=True)
        elif name == "prepare-decision":
            command.add_argument("--operation-id", required=True)
            command.add_argument("--work-id", required=True)
            command.add_argument("--expected-work-revision", required=True, type=int)
            command.add_argument("--digest", required=True)
        elif name == "backup-ledger":
            command.add_argument("--backup", type=Path, required=True, help="New private SQLite backup path; never overwritten")
        elif name == "restore-ledger":
            command.add_argument("--backup", type=Path, required=True, help="Retained quiescent backup; runtime/budget sidecars are not restored")
            command.add_argument("--sha256", required=True, help="Exact retained backup SHA-256; destination state directory must not exist")
        elif name == "recover-startup-approval":
            command.add_argument("--backup", type=Path, required=True, help="Exact consistent pre-recovery backup")
            command.add_argument("--evidence-bundle", type=Path, required=True,
                                 help="Trusted administrator only: pinned original startup chat, source readback, document, authorization and failed capture bytes")
        elif name == "worker":
            runtime = command.add_mutually_exclusive_group(required=True)
            runtime.add_argument("--fake-plan", type=Path, help="Q1 deterministic simulation plan; never invokes a real agent")
            runtime.add_argument("--runtime-recipes", type=Path, help="Q2 trusted recipes; exact digest must be in the approved execution envelope")
            command.add_argument("--max-cycles", type=int, required=True)
            command.add_argument("--interval", type=float, default=30)
        else:
            command.add_argument("--certificate", type=Path, required=True)
            command.add_argument("--key", type=Path, required=True)
            command.add_argument("--port", type=int, default=8443)
            command.add_argument("--qualification-reference", required=True,
                                 help="Operator evidence reference; this label alone is not proof of human action")
    args = parser.parse_args(argv)
    try:
        if args.command == 'start':
            from .supervisor import start
            options = {'continuation_path': args.continuation} if args.continuation is not None else {}
            result = start(args.project, args.request, **options)
            print(json.dumps(result, indent=2), flush=True)
            return 0 if result.get('state') in {'delivered', 'declined', 'cancelled'} else 3
        if args.command == 'prepare-project':
            from .project import prepare, prepare_directories
            result = prepare(args.project, args.request)
            if (not result['ready'] and result['blocker']['code'] == 'project_directories'
                    and result.get('required_directories')):
                directories = prepare_directories(args.project, args.request, plan=result)
                if directories['prepared']:
                    result = prepare(args.project, args.request)
                else:
                    result['blocker'] = directories['blocker']
                result['directory_preparation'] = directories
            print(json.dumps(result, indent=2))
            return 0 if result['ready'] else 3
        if args.command.startswith('backlog-'):
            from .backlog import GitHubReader, prepare, preview, reconcile
            if args.command == 'backlog-preview':
                result = prepare(args.intent, preview(args.markdown, args.item, args.direction,
                                 GitHubReader(args.project_owner, args.project_number)))
            else:
                retained = json.loads(args.intent.read_text())['plan']['snapshot']['project']
                result = reconcile(args.intent, GitHubReader(retained['owner'], retained['number']), apply_local=args.apply_local)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'instruction-manifest':
            from .instructions import discover
            print(json.dumps(discover(args.source, support_files=args.support_file,
                                      allow_project_registration=args.allow_project_registration,
                                      project_doc_max_bytes=args.project_doc_max_bytes), indent=2))
            return 0
        if args.command == "version":
            from .coordinator import capabilities
            print(json.dumps(capabilities(), indent=2))
            return 0
        if args.command.startswith("dashboard-"):
            from .dashboard_service import DashboardService, inspect_service, stop_service, work_views
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            if args.command == "dashboard-serve":
                with DashboardService(store, work_views(args.work), args.port, archive_journal=args.archive_journal) as service:
                    service.run()
                return 0
            result = inspect_service(store) if args.command == "dashboard-inspect" else stop_service(store)
            print(json.dumps(result, indent=2))
            return 3 if result.get("state") == "unavailable" else 0
        if args.command == 'prepare-source-review-evidence':
            from .source_review_evidence import prepare
            from .lifecycle import Lifecycle
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            result = prepare(Lifecycle.for_reading(store), work_id=args.work_id,
                             destination=args.destination, git_base=args.git_base)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'prepare-assessment-review-continuation':
            from .assessment_review_continuation import prepare
            from .assessment_continuation import _file
            from .lifecycle import Lifecycle
            from .project import _private, _unique, _nonfinite
            from .native_paths import _canonical
            from .supervisor import _retain
            evidence_path = _canonical(str(args.source_review_evidence.absolute()), regular_file=True)
            _private(evidence_path, file=True)
            if evidence_path.stat().st_size > 16384:
                raise Rejected('source_review_evidence', 'Evidence descriptor exceeds its size bound')
            evidence = json.loads(evidence_path.read_bytes(), object_pairs_hook=_unique, parse_constant=_nonfinite)
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            extra = {'readiness_extra_attempt': True} if args.readiness_extra_attempt else {}
            packet = prepare(Lifecycle(store), operation_id=args.operation_id, work_id=args.work_id,
                             failed_assignment_id=args.failed_assignment_id, source_review_evidence=evidence, **extra)
            path = store.directory / ('prepared-assessment-review-' + args.work_id + '-' + args.operation_id + '.json')
            if path.exists():
                if _file(path)[0] != packet:
                    raise Rejected('assessment_review_continuation', 'Existing continuation packet differs')
            else:
                _retain(path, packet)
            print(json.dumps({'ready': True, 'continuation': str(path),
                              'work_id': args.work_id, 'approval_recorded': False}, indent=2))
            return 0
        if args.command in ('prepare-predispatch-settings-rebind', 'apply-predispatch-settings-rebind', 'verify-predispatch-settings-rebind',
                      'prepare-predispatch-feature-amendment', 'apply-predispatch-feature-amendment', 'verify-predispatch-feature-amendment',
                      'prepare-predispatch-recovery', 'qualify-predispatch-recovery', 'apply-predispatch-recovery', 'verify-predispatch-recovery'):
            if args.command.endswith('predispatch-recovery'):
                from . import predispatch_recovery as rebind
            elif args.command.endswith('feature-amendment'):
                from . import predispatch_feature_amendment as rebind
            else:
                from . import predispatch_settings_rebind as rebind
            from .coordinator import request_file
            from .lifecycle import Lifecycle
            schemas = {prefix + '-predispatch-' + suffix: schema for suffix in ('settings-rebind', 'feature-amendment')
                       for prefix, schema in (('prepare', rebind.PrepareRequest), ('apply', rebind.ApplyRequest), ('verify', rebind.VerifyRequest))}
            if args.command.endswith('predispatch-recovery'):
                schemas.update({prefix + '-predispatch-recovery': schema for prefix, schema in
                    (('prepare', rebind.PrepareRequest), ('qualify', rebind.QualifyRequest), ('apply', rebind.ApplyRequest), ('verify', rebind.VerifyRequest))})
            request = request_file(args.request, schemas[args.command])
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            if args.command.startswith('verify-predispatch-'):
                result = rebind.verify(store, request)
            else:
                lifecycle = Lifecycle.for_reading(store)
                result = (rebind.prepare(lifecycle, request) if args.command.startswith('prepare-')
                          else rebind.qualify(lifecycle, request) if args.command.startswith('qualify-')
                          else rebind.apply(lifecycle, request))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'prepare-assessment-continuation':
            from .assessment_continuation import prepare, _file
            from .lifecycle import Lifecycle
            from .supervisor import _retain
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            extra = {'readiness_extra_attempt': True} if args.readiness_extra_attempt else {}
            packet = prepare(Lifecycle(store), operation_id=args.operation_id,
                             recovery_operation_id=args.recovery_operation_id,
                             support_files=[str(path) for path in args.support_file], **extra)
            work_id = packet['introduction']['work_id']
            path = store.directory / ('prepared-assessment-' + work_id + '-' + args.operation_id + '.json')
            if path.exists():
                if _file(path)[0] != packet:
                    raise Rejected('assessment_continuation', 'Existing continuation packet differs')
            else:
                _retain(path, packet)
            print(json.dumps({'ready': True, 'continuation': str(path),
                              'work_id': work_id, 'approval_recorded': False}, indent=2))
            return 0
        if args.command == 'recover-assessment-startup':
            from .coordinator import request_file
            from .assessment_recovery import AssessmentRecoveryRequest, recover
            from .lifecycle import Lifecycle
            request = request_file(args.request, AssessmentRecoveryRequest)
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=not args.apply)
            lifecycle = Lifecycle(store) if args.apply else Lifecycle.for_reading(store)
            print(json.dumps(recover(lifecycle, request, apply=args.apply), indent=2))
            return 0
        if args.command == "continue-recovered-startup":
            from .coordinator import request_file
            from .startup_resource_continuation import StartupResourceContinuationRequest, acknowledge
            from .lifecycle import Lifecycle
            request = request_file(args.request, StartupResourceContinuationRequest)
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=not args.apply)
            lifecycle = Lifecycle(store) if args.apply else Lifecycle.for_reading(store)
            print(json.dumps(acknowledge(lifecycle, request, apply=args.apply), indent=2))
            return 0
        if args.command == "recover-startup-failure":
            from .coordinator import request_file
            from .startup_failure_recovery import StartupFailureRecoveryRequest, recover
            from .lifecycle import Lifecycle
            request = request_file(args.request, StartupFailureRecoveryRequest)
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=not args.apply)
            lifecycle = Lifecycle(store) if args.apply else Lifecycle.for_reading(store)
            print(json.dumps(recover(lifecycle, request, apply=args.apply), indent=2))
            return 0
        if args.command == "recover-legacy-cancellation":
            from .coordinator import request_file
            from .legacy_recovery import LegacyRecoveryRequest, recover
            from .lifecycle import Lifecycle
            request = request_file(args.request, LegacyRecoveryRequest)
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            print(json.dumps(recover(Lifecycle(store), request, apply=args.apply), indent=2))
            return 0
        if args.command in ("introduce-work", "present-concept", "reconcile-prerequisites"):
            from .coordinator import ConceptRequest, IntroductionRequest, PrerequisiteRequest, request_file
            schema = {"introduce-work": IntroductionRequest, "present-concept": ConceptRequest,
                      "reconcile-prerequisites": PrerequisiteRequest}[args.command]
            request = request_file(args.request, schema)
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            if args.command == "reconcile-prerequisites":
                from .lifecycle import Lifecycle
                result = Lifecycle(store).reconcile_prerequisites(**request)
            else:
                result = store.introduce(**request) if args.command == "introduce-work" else store.present(**request)
            print(json.dumps(result, indent=2))
            return 0
        if args.command in ("register-contract", "admit", "pause", "resume", "cancel"):
            from .coordinator import register_contract
            from .lifecycle import Lifecycle
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            lifecycle = Lifecycle(store)
            if args.command == "register-contract":
                result = register_contract(lifecycle, args.operation_id, args.work_id, args.expected_work_revision, args.digest)
            elif args.command == "admit":
                result = lifecycle.admit(args.operation_id, args.work_id,
                                         expected_work_revision=args.expected_work_revision, expected_digest=args.digest)
            else:
                result = lifecycle.control(args.operation_id, args.work_id, args.command,
                                           expected_work_revision=args.expected_work_revision, expected_digest=args.digest)
            print(json.dumps(result, indent=2))
            # Held/blocked requests are recorded facts, not successful admission.
            return 3 if result.get("blockers") or (args.command == "admit" and result.get("execution") != "eligible") else 0
        if args.command == "runtime-capabilities":
            from .runtime import capabilities
            print(json.dumps(capabilities(), indent=2))
            return 0
        if args.command in ('archive-publish-local', 'archive-read-local'):
            from .archive import ArchiveJournal, LocalFakeDestination
            journal = ArchiveJournal(args.journal, read_only=args.command == 'archive-read-local')
            result = (journal.read(args.intent_id) if args.command == 'archive-read-local' else
                      journal.publish_local(args.intent_id, LocalFakeDestination(args.fake_destination)))
            print(json.dumps(result, indent=2))
            return 0
        if args.command in ('archive-preview', 'archive-prepare-local'):
            from .archive import ArchiveJournal, LocalFakeDestination, coordinator_journal, preview
            from .projection import collect
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            snapshot = collect(store, args.work_id)
            result = (preview(snapshot, args.milestone) if args.command == 'archive-preview' else
                      ArchiveJournal(coordinator_journal(args.journal, store.worker_writable_roots)).prepare(args.intent_id, snapshot, args.milestone,
                                                          LocalFakeDestination(args.fake_destination)))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "status":
            from .coordinator import capabilities
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            if args.surface == 'record':
                result = store.read(args.work_id)
            else:
                from .projection import collect, dashboard
                result = collect(store, args.work_id)
            if args.archive_journal:
                from .archive import LocalHistoryCollector, coordinator_journal
                journal = coordinator_journal(args.archive_journal, store.worker_writable_roots)
                result['local_history'] = LocalHistoryCollector(journal, args.work_id).collect()
            if args.surface == 'dashboard':
                result = dashboard(result)
            result["operator"] = capabilities()
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "backup-ledger":
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            print(json.dumps(store.consistent_backup(args.backup), indent=2))
            return 0
        if args.command == "restore-ledger":
            result = Store.restore_backup(args.backup, args.state_dir, tuple(args.worker_writable_root), args.sha256)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "recover-startup-approval":
            bundle = json.loads(args.evidence_bundle.read_text())
            # Validate before opening a writable store or considering a transaction.
            Store._startup_recovery_evidence(bundle)
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            result = store.recover_startup_approval(bundle, args.backup, capability=store.recovery_capability())
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "prepare-decision":
            from .coordinator import prepare_decision
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            result = prepare_decision(store, args.operation_id, args.work_id, args.expected_work_revision, args.digest)
            print(json.dumps({"completed": result["completed"], "challenge_id": result.get("challenge_id"),
                              "digest": result.get("digest"), "result": result.get("result")}, indent=2))
            return 0
        if args.command == "worker":
            from .lifecycle import Lifecycle
            from .worker import FakeRuntime, Worker
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            if store.recovery_hold():
                raise Rejected('recovery_copy_held', 'A restored recovery copy cannot create a runtime or reset runtime/shared-budget ownership')
            lifecycle = Lifecycle(store)
            if args.fake_plan:
                runtime = FakeRuntime(store.directory / "fake-runtime.sqlite3", json.loads(args.fake_plan.read_text()))
            else:
                from .codex_runtime import CodexRuntime
                runtime = CodexRuntime(lifecycle, json.loads(args.runtime_recipes.read_text()))
            try:
                with Worker(lifecycle, runtime) as worker:
                    worker.run(max_cycles=args.max_cycles, interval=args.interval)
            finally:
                if hasattr(runtime, 'close'):
                    runtime.close()
            return 0
        if args.command == "decide-serve":
            from .approval_web import serve_https
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            serve_https(store, certificate=str(args.certificate), key=str(args.key), port=args.port,
                        qualification_reference=args.qualification_reference)
            return 0
        if args.command == "serve-coordinator":
            from .coordinator_mcp import create_coordinator_server
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=args.audience == "worker")
            create_coordinator_server(store, decision_origin=args.decision_origin, audience=args.audience).run(transport="stdio")
            return 0
        if bool(args.qualified_human_host_name) != bool(args.qualification_reference):
            parser.error("qualified host name and qualification evidence reference must be configured together")
        if args.audience == "worker" and args.qualified_human_host_name:
            parser.error("worker connections cannot receive human decision capability")
        qualification = HumanHostQualification(args.qualified_human_host_name, args.qualification_reference) if args.qualified_human_host_name else None
        store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=args.audience == "worker")
        create_server(store, audience=args.audience, qualification=qualification).run(transport="stdio")
        return 0
    except (Rejected, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": getattr(error, "code", "state_unavailable"), "message": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
