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
    commands.add_parser("runtime-capabilities", help="Inspect pinned SDK/API and runtime version; no model invocation")
    for name in ("serve", "status", "prepare-decision", "decide-serve", "worker", "backup-ledger", "recover-startup-approval"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True, help="Private authoritative directory outside all worker-writable roots")
        command.add_argument("--worker-writable-root", type=Path, action="append", required=True, help="Declare each worker-writable root; actual runtime denial still needs verification")
        if name == "serve":
            command.add_argument("--audience", choices=("host", "worker"), default="host")
            command.add_argument("--qualified-human-host-name", help="Operator configuration only; name is not proof of human identity")
            command.add_argument("--qualification-reference", help="Reference to independently observed explicit-human host evidence; never a model assertion")
        elif name == "status":
            command.add_argument("work_id")
        elif name == "prepare-decision":
            command.add_argument("--operation-id", required=True)
            command.add_argument("--work-id", required=True)
            command.add_argument("--expected-work-revision", required=True, type=int)
            command.add_argument("--digest", required=True)
        elif name == "backup-ledger":
            command.add_argument("--backup", type=Path, required=True, help="New private SQLite backup path; never overwritten")
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
        if args.command == "runtime-capabilities":
            from .runtime import capabilities
            print(json.dumps(capabilities(), indent=2))
            return 0
        if args.command == "status":
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            print(json.dumps(store.read(args.work_id), indent=2))
            return 0
        if args.command == "backup-ledger":
            store = Store(args.state_dir, tuple(args.worker_writable_root), read_only=True)
            print(json.dumps(store.consistent_backup(args.backup), indent=2))
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
            store = Store(args.state_dir, tuple(args.worker_writable_root))
            capability = store.host_capability("Codex local HTTPS decision tab", "operator-preparation-only")
            result = store.begin_approval(args.operation_id, args.work_id, args.expected_work_revision,
                                          args.digest, capability=capability)
            print(json.dumps({"completed": result["completed"], "challenge_id": result.get("challenge_id"),
                              "digest": result.get("digest"), "result": result.get("result")}, indent=2))
            return 0
        if args.command == "worker":
            from .lifecycle import Lifecycle
            from .worker import FakeRuntime, Worker
            store = Store(args.state_dir, tuple(args.worker_writable_root))
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
