"""Independent boundary regressions for native writable path declarations."""
import copy
import os
import sys

import pytest

from agentic_tools.aos import native_paths
from agentic_tools.aos import native_processes
from agentic_tools.aos.codex_runtime import candidate, inventory
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import native_fixture


def test_native_cache_file_cannot_modify_immutable_source_through_hardlink(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    cache = tmp_path / 'cache-file'
    source_file = p.source / 'test_native.py'
    body = ('from pathlib import Path\n'
            'def test_native():\n'
            ' try:\n'
            f'  Path({str(cache)!r}).write_text("changed through hardlink\\n")\n'
            ' except PermissionError:\n'
            '  pass\n')
    source_file.write_text(body)
    os.link(source_file, cache)
    p.recipe.update(native_write_roots=[{'path': str(cache), 'kind': 'cache-file'}],
                    probe_interpreter=sys.executable)
    try:
        try:
            p.recipe['immutable'] = inventory(p.source, recipe=p.recipe)
            frozen = candidate(p.source, recipe=p.recipe)
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'independent-path-review', frozen)
        except Rejected:
            pass
        assert source_file.read_text() == body, 'Writable cache alias changed immutable source'
    finally:
        p.runtime.close()


def test_cached_native_result_requires_identical_native_path_permissions(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    cache1, cache2 = tmp_path / 'cache-one', tmp_path / 'cache-two'
    cache1.mkdir()
    cache2.mkdir()
    p.recipe.update(native_write_roots=[{'path': str(cache1), 'kind': 'cache'}],
                    probe_interpreter=sys.executable)
    frozen = candidate(p.source, recipe=p.recipe)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'independent-path-review', frozen)
        assert result['result'] == 'pass'
        changed_recipe = copy.deepcopy(p.recipe)
        changed_recipe['native_write_roots'] = [{'path': str(cache2), 'kind': 'cache'}]
        with pytest.raises(Rejected):
            p.runtime._check(changed_recipe, p.check, p.outputs / 'check', 'independent-path-review', frozen)
    finally:
        p.runtime.close()


def test_native_generated_root_cannot_create_writable_hardlink_to_source(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    target = p.source / 'target'
    target.mkdir()
    cache = target / 'source-alias'
    source_file = p.source / 'test_native.py'
    body = ('from pathlib import Path\nimport os\n'
            'def test_native():\n'
            ' try:\n'
            f'  os.link({str(source_file)!r}, {str(cache)!r})\n'
            f'  Path({str(cache)!r}).write_text("changed through new hardlink\\n")\n'
            ' except PermissionError:\n'
            '  pass\n')
    source_file.write_text(body)
    p.recipe.update(native_write_roots=[{'path': str(target), 'kind': 'generated'}],
                    probe_interpreter=sys.executable)
    p.recipe['immutable'] = inventory(p.source, recipe=p.recipe)
    frozen = candidate(p.source, recipe=p.recipe)
    try:
        try:
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'independent-path-review', frozen)
        except Rejected:
            pass
        assert source_file.read_text() == body, 'A generated hardlink changed immutable source'
    finally:
        p.runtime.close()


def test_native_state_case_alias_cannot_write_protected_state(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    alias = p.state.with_name(p.state.name.upper())
    if not alias.exists() or not alias.samefile(p.state):
        pytest.skip('Case alias fixture requires a case-insensitive filesystem')
    canary = alias / 'native-forbidden-canary'
    source_file = p.source / 'test_native.py'
    body = ('from pathlib import Path\n'
            'def test_native():\n'
            ' try:\n'
            f'  Path({str(canary)!r}).write_text("forbidden state write\\n")\n'
            ' except PermissionError:\n'
            '  pass\n')
    source_file.write_text(body)
    p.recipe.update(native_write_roots=[{'path': str(alias), 'kind': 'cache'}],
                    probe_interpreter=sys.executable)
    p.recipe['immutable'] = inventory(p.source)
    frozen = candidate(p.source)
    try:
        try:
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'independent-path-review', frozen)
        except Rejected:
            pass
        assert not canary.exists(), 'Case alias made protected state writable'
    finally:
        p.runtime.close()


def test_host_python_dependency_cannot_be_declared_writable(tmp_path):
    import json
    from pathlib import Path
    p = native_fixture(tmp_path, 'no-child')
    dependency = Path(json.__file__).resolve()
    p.recipe.update(native_write_roots=[{'path': str(dependency), 'kind': 'cache-file'}],
                    probe_interpreter=sys.executable)
    try:
        with pytest.raises(Rejected):
            native_paths.validate(p.recipe)
    finally:
        p.runtime.close()


@pytest.mark.parametrize('root_name', ['source', 'outputs'])
def test_census_interpreter_cannot_be_model_writable(tmp_path, root_name):
    p = native_fixture(tmp_path, 'no-child')
    root = p.source if root_name == 'source' else p.outputs
    interpreter = root / 'python3'
    interpreter.write_text('synthetic trusted-interpreter placeholder; never executed\n')
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    probe_interpreter=str(interpreter))
    try:
        with pytest.raises(ValueError, match='model-writable'):
            native_processes.validate(p.recipe)
    finally:
        p.runtime.close()


def test_selected_venv_dependencies_cannot_be_model_or_native_writable(tmp_path):
    from pathlib import Path
    p = native_fixture(tmp_path, 'no-child')
    venv = tmp_path / 'selected-python-env'
    (venv / 'bin').mkdir(parents=True)
    dependency = venv / 'lib' / 'site-packages' / 'project'
    dependency.mkdir(parents=True)
    interpreter = venv / 'bin' / 'python3'
    interpreter.symlink_to(Path(sys.executable).resolve())
    p.recipe.update(native_process_ownership=native_processes.BACKEND,
                    probe_interpreter=str(interpreter))
    try:
        source_recipe = {**p.recipe, 'source': str(dependency)}
        with pytest.raises(ValueError, match='model-writable'):
            native_processes.validate(source_recipe)
        writable_recipe = {**p.recipe, 'native_write_roots': [{'path': str(dependency), 'kind': 'cache'}]}
        with pytest.raises(Rejected):
            native_paths.validate(writable_recipe)
    finally:
        p.runtime.close()


def test_macos_firmlink_alias_cannot_evade_protected_roots(tmp_path):
    from pathlib import Path
    p = native_fixture(tmp_path, 'no-child')
    alias = Path('/System/Volumes/Data') / str(p.state).lstrip('/')
    try:
        if not alias.exists() or not alias.samefile(p.state):
            pytest.skip('Firmlink alias fixture requires the macOS data-volume alias')
        p.recipe['native_write_roots'] = [{'path': str(alias), 'kind': 'cache'}]
        with pytest.raises(Rejected):
            native_paths.validate(p.recipe, protected=(p.state,))
    finally:
        p.runtime.close()


def test_macos_firmlink_alias_cannot_make_trusted_artifact_writable(tmp_path):
    from pathlib import Path
    from agentic_tools.aos.checks import _protected
    directory = tmp_path / 'trusted'
    directory.mkdir()
    artifact = directory / 'control.json'
    artifact.write_text('{}')
    alias = Path('/System/Volumes/Data') / str(directory).lstrip('/')
    if not alias.exists() or not alias.samefile(directory):
        pytest.skip('Firmlink alias fixture requires the macOS data-volume alias')
    assert not _protected(artifact, (alias,)), 'An aliased writable root covers the trusted artifact'
