"""Inspection-only SDK capability receipt. This does not run an agent turn."""

from __future__ import annotations

import inspect
import subprocess
from importlib.metadata import version

from codex_cli_bin import bundled_codex_path
from openai_codex import AsyncCodex, AsyncThread, AsyncTurnHandle, CodexConfig, Sandbox, TurnResult
from openai_codex.generated.v2_all import ReadOnlySandboxPolicy, Thread, ThreadTokenUsage


def capabilities() -> dict:
    """Report the installed/pinned API without authentication or model invocation."""
    binary = bundled_codex_path()
    runtime = subprocess.run([str(binary), "--version"], text=True, capture_output=True, timeout=10, check=True)
    return {
        "inspection_only": True, "model_turns_started": 0,
        "sdk_version": version("openai-codex"), "runtime_package_version": version("openai-codex-cli-bin"),
        "bundled_runtime_version": runtime.stdout.strip(), "runtime_binary": str(binary),
        "api": {
            "thread_start": str(inspect.signature(AsyncCodex.thread_start)),
            "turn_start": str(inspect.signature(AsyncThread.turn)),
            "thread_read": str(inspect.signature(AsyncThread.read)),
            "interrupt": str(inspect.signature(AsyncTurnHandle.interrupt)),
            "event_stream": str(inspect.signature(AsyncTurnHandle.stream)),
            "terminal_event": "turn/completed",
            "config_fields": list(CodexConfig.__dataclass_fields__),
            "result_fields": list(TurnResult.__dataclass_fields__),
            "usage_fields": list(ThreadTokenUsage.model_fields),
            "thread_model_fields": [name for name in Thread.model_fields if "model" in name or "effort" in name],
            "read_only_policy_fields": list(ReadOnlySandboxPolicy.model_fields),
            "sandbox_presets": [mode.value for mode in Sandbox],
        },
        "qualification": {
            "completion": "unrun", "status": "unrun", "interrupt": "unrun",
            "effective_permissions": "unrun", "model_and_effort_receipts": "unrun",
            "usage_observation": "unrun", "host_human_decision": "unqualified",
            "inherited_mcp_plugins": "must be disabled and verified before a turn",
            "environment": "CodexConfig.env overlays inherited process environment; it is not a replacement",
            "read_only_limit": "Preset blocks writes but does not restrict reads to the project",
            "decision_authority": "No persisted worker decision credential exists; bridge capability is process-local",
        },
    }
