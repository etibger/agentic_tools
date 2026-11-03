"""Small static, versioned allowlist of reusable evidence interpreters.

No discovery, imports from recipes, approval decisions or product rules live here.
"""
import hashlib
import json
from pathlib import Path

from . import cargo_test, native_build, native_tests, pytest

_ADAPTERS = {adapter.ADAPTER_ID: adapter for adapter in (pytest, native_build, cargo_test, native_tests)}
_KINDS = {'pytest': 'pytest-v1', 'build': 'native-build-v1', 'cargo-test': 'cargo-test-v1', 'native-tests': 'native-tests-v1'}


def select(check: dict):
    adapter_id = check.get('adapter_id')
    expected = _KINDS.get(check.get('result_kind', 'pytest'))
    if expected is None or adapter_id is not None and adapter_id != expected:
        raise ValueError('Unknown or inconsistent trusted check adapter')
    if 'bun_commands' in check and expected != native_tests.ADAPTER_ID:
        raise ValueError('bun_commands is supported only by native-tests-v1')
    return native_tests.bind(check) if expected == native_tests.ADAPTER_ID else _ADAPTERS[expected]


def identities() -> dict[str, dict]:
    root = Path(__file__).parent
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))}
    code_digest = 'sha256:' + hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {name: {'adapter_id': name, 'version': adapter.VERSION, 'source_digest': code_digest,
                   'evidence_kind': adapter.KIND} for name, adapter in _ADAPTERS.items()}


def code_paths() -> list[Path]:
    return sorted(Path(__file__).parent.glob('*.py'))
