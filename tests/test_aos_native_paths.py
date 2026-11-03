"""Declared native paths do not become source exclusions or model privileges."""
import copy
import os
import socket
from pathlib import Path
import sys

import pytest

from agentic_tools.aos import native_paths
from agentic_tools.aos.codex_runtime import candidate, inventory
from agentic_tools.aos.checks import bindings
from agentic_tools.aos.store import Rejected, canonical
from test_aos_codex_runtime import prepared, native_fixture


def paths(source, outputs, cache):
    target = source / 'target'
    target.mkdir()
    cache.mkdir()
    return {'source': str(source), 'outputs': str(outputs), 'mutable': ['main.rs'],
            'immutable': {'tests.rs': 'bound-hash'}, 'native_write_roots': [
                {'path': str(target), 'kind': 'generated'}, {'path': str(cache), 'kind': 'cache'}]}


@pytest.fixture
def recipe(tmp_path):
    source, outputs = tmp_path / 'source', tmp_path / 'outputs'
    source.mkdir()
    outputs.mkdir()
    (source / 'main.rs').write_text('product')
    (source / 'tests.rs').write_text('assert product')
    return paths(source, outputs, tmp_path / 'cache')


def test_only_exact_generated_subtree_leaves_source_identity(recipe):
    source = Path(recipe['source'])
    before = candidate(source, recipe=recipe)
    (source / 'target' / 'native.bin').write_bytes(b'build')
    assert candidate(source, recipe=recipe) == before
    assert candidate(source) != before  # Old recipes retain their complete inventory.
    (source / 'new-product.rs').write_text('new product')
    assert candidate(source, recipe=recipe) != before
    with pytest.raises(Rejected, match='exact source'):
        inventory(Path(recipe['outputs']), recipe=recipe)


@pytest.mark.parametrize('bad', ['source', 'mutable', 'immutable', 'outputs', 'ancestor', 'control', 'duplicate', 'nested', 'alias'])
def test_native_roots_reject_broad_protected_and_aliased_scopes(recipe, bad, tmp_path):
    source = Path(recipe['source'])
    roots = recipe['native_write_roots']
    if bad == 'source':
        roots[0]['path'] = str(source)
    elif bad in ('mutable', 'immutable'):
        name = 'generated-input'
        (source / name).mkdir()
        recipe['mutable' if bad == 'mutable' else 'immutable'] = [name + '/file'] if bad == 'mutable' else {name + '/file': 'hash'}
        roots[0]['path'] = str(source / name)
    elif bad == 'outputs':
        roots[1]['path'] = recipe['outputs']
    elif bad == 'ancestor':
        roots[1]['path'] = str(source.parent)
    elif bad == 'control':
        (source / '.git').mkdir()
        roots[0]['path'] = str(source / '.git')
    elif bad == 'duplicate':
        roots.append(dict(roots[0]))
    elif bad == 'nested':
        nested = source / 'target' / 'nested'
        nested.mkdir()
        roots.append({'path': str(nested), 'kind': 'generated'})
    else:
        alias = tmp_path / 'alias'
        alias.symlink_to(Path(roots[1]['path']), target_is_directory=True)
        roots[1]['path'] = str(alias)
    with pytest.raises(Rejected):
        native_paths.validate(recipe)


def test_native_roots_protect_instruction_state_and_declared_tool_paths(recipe, tmp_path):
    cache = Path(recipe['native_write_roots'][1]['path'])
    for mode in ('state', 'tool', 'instruction'):
        current = copy.deepcopy(recipe)
        if mode == 'tool':
            current['native_protected_paths'] = [str(cache / 'compiler')]
        elif mode == 'instruction':
            current['instruction_manifest'] = {'support': [{'path': str(cache / 'policy')}]}
        with pytest.raises(Rejected, match='protected'):
            native_paths.validate(current, protected=(cache / 'state',) if mode == 'state' else ())


def test_generated_effects_record_links_without_reading_targets(recipe, tmp_path):
    target = Path(recipe['native_write_roots'][0]['path'])
    before = native_paths.snapshot(recipe)
    outside = tmp_path / 'unrelated'
    outside.write_text('private input')
    (target / 'link').symlink_to(outside)
    effects = native_paths.reconcile(recipe, before)
    assert effects['changes'][str(target)] == ['link']
    entry = effects['after'][str(target)]['entries']['link']
    assert entry['kind'] == 'symlink' and entry['target'] == str(outside)
    assert 'private input' not in str(effects)


def test_exact_absent_cache_file_creation_and_removal_are_observed(recipe, tmp_path):
    parent = tmp_path / 'cargo-home'
    parent.mkdir()
    cache = parent / '.package-cache'
    recipe['native_write_roots'].append({'path': str(cache), 'kind': 'cache-file'})
    before = native_paths.snapshot(recipe)
    assert before[str(cache)]['entries'][cache.name] == {'kind': 'absent'}
    cache.write_text('native cache')
    after = native_paths.reconcile(recipe, before)
    assert after['changes'][str(cache)] == [cache.name]
    cache.unlink()
    assert native_paths.reconcile(recipe, after['after'])['changes'][str(cache)] == [cache.name]
    cache.mkdir()
    with pytest.raises(Rejected, match='regular files'):
        native_paths.validate(recipe)


def test_native_hardlinks_require_every_inode_alias_inside_writable_scopes(recipe, tmp_path):
    target = Path(recipe['native_write_roots'][0]['path'])
    artifact = target / 'artifact'
    artifact.write_text('native build output')
    os.link(artifact, target / 'artifact-alias')
    observed = native_paths.snapshot(recipe)
    assert observed[str(target)]['entries']['artifact']['nlink'] == 2
    os.link(artifact, tmp_path / 'outside-alias')
    with pytest.raises(Rejected, match='alias outside'):
        native_paths.snapshot(recipe)


def test_physical_alias_overlap_preserves_absent_path_boundaries(tmp_path):
    directory = tmp_path / 'native-scopes'
    directory.mkdir()
    alias = Path('/System/Volumes/Data') / str(directory).lstrip('/')
    if not alias.exists() or not alias.samefile(directory):
        pytest.skip('Requires macOS data-volume firmlink')
    assert native_paths.overlaps(directory / 'absent', alias / 'absent' / 'child')
    assert not native_paths.overlaps(directory / 'left', alias / 'right')


def test_replaced_generated_root_and_live_socket_hold_reconciliation(recipe, tmp_path, monkeypatch):
    target = Path(recipe['native_write_roots'][0]['path'])
    before = native_paths.snapshot(recipe)
    target.rename(tmp_path / 'original-target')
    target.mkdir()
    with pytest.raises(Rejected, match='identity changed'):
        native_paths.reconcile(recipe, before)
    monkeypatch.chdir(target)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind('live.sock')
        with pytest.raises(Rejected, match='socket'):
            native_paths.snapshot(recipe)


def test_only_native_profile_grants_build_cache_writes(prepared, tmp_path):
    p = prepared
    declarations = paths(p.source, p.outputs, tmp_path / 'cache')
    p.recipe['native_write_roots'] = declarations['native_write_roots']
    target, cache = [entry['path'] for entry in p.recipe['native_write_roots']]
    for assessment in (False, True):
        _, model = p.runtime._profile(p.recipe, assessment=assessment)
        assert model['filesystem'][target] == 'read'
        assert model['filesystem'].get(cache) != 'write'
        assert model['network'] == {'enabled': False}
    _, native = p.runtime._profile(p.recipe, assessment=True, native=True)
    assert native['filesystem'][str(p.source)] == 'read'
    assert native['filesystem'][target] == native['filesystem'][cache] == 'write'
    assert native['filesystem'][str(p.source / 'test_example.py')] == 'read'


def test_native_root_cannot_make_trusted_adapter_writable(prepared):
    from agentic_tools import check_adapters
    p = prepared
    adapter_root = check_adapters.code_paths()[0].parent
    p.recipe['native_write_roots'] = [{'path': str(adapter_root), 'kind': 'cache'}]
    with pytest.raises(Rejected, match='adapter code'):
        bindings(p.store, 'Q2', {}, p.recipe)


def test_native_root_cannot_make_coordinator_code_writable(prepared):
    from agentic_tools.aos import checks
    p = prepared
    p.recipe['native_write_roots'] = [{'path': str(Path(checks.__file__).parent), 'kind': 'cache'}]
    with pytest.raises(Rejected, match='coordinator code'):
        bindings(p.store, 'Q2', {}, p.recipe)


def test_native_cache_cannot_contain_qualification_receipt(tmp_path, monkeypatch):
    from test_aos_check_adapters import prepared_checks, qualification
    cache = tmp_path / 'native-cache'
    cache.mkdir()
    p = prepared_checks(tmp_path, monkeypatch, delegated=True, recipe_update=lambda recipe:
                        recipe.update(native_write_roots=[{'path': str(cache), 'kind': 'cache'}]))
    try:
        qualification(p, path=cache / 'qualification.json')
        with pytest.raises(Rejected, match='qualification receipt'):
            bindings(p.store, 'ADAPTERS', p.package, p.recipe)
    finally:
        p.runtime.close()


def test_unresolved_native_writer_conflicts_are_symmetric(recipe, tmp_path):
    source, outputs = tmp_path / 'other-source', tmp_path / 'other-outputs'
    source.mkdir()
    outputs.mkdir()
    other = {**recipe, 'source': str(source), 'outputs': str(outputs),
             'native_write_roots': [recipe['native_write_roots'][1]]}
    row = {'source': other['source'], 'outputs_root': other['outputs'], 'native_path_recipe': other}
    with pytest.raises(Rejected, match='overlapping'):
        native_paths.reject_conflicts(recipe, [row])
    # A new source may not be placed inside another job's writable cache.
    nested = {**recipe, 'source': recipe['native_write_roots'][1]['path'], 'native_write_roots': []}
    with pytest.raises(Rejected, match='overlapping'):
        native_paths.reject_conflicts(nested, [row])
    independent = {**other, 'native_write_roots': []}
    native_paths.reject_conflicts(independent, [row])


@pytest.mark.parametrize('kind', ['implementation', 'assessment'])
def test_actual_model_boundary_denies_native_write_roots(tmp_path, kind):
    p = native_fixture(tmp_path, 'no-child')
    declarations = paths(p.source, p.outputs, tmp_path / 'cache')
    p.recipe.update(native_write_roots=declarations['native_write_roots'], probe_interpreter=sys.executable)
    try:
        observed = p.runtime._probe_sandbox({'assignment': {'kind': kind}, 'attempt_id': 'path-probe'}, p.recipe)
        assert observed['checks']['native_path_access_0']
        assert observed['checks']['native_path_access_1']
        assert all(observed['checks'].values())
        assert all(not list(Path(root['path']).iterdir()) for root in p.recipe['native_write_roots'])
    finally:
        p.runtime.close()


def test_conflicting_admission_performs_no_native_probe_or_output_creation(tmp_path, monkeypatch):
    p = native_fixture(tmp_path, 'no-child')
    declarations = paths(p.source, p.outputs, tmp_path / 'cache')
    p.recipe.update(native_write_roots=declarations['native_write_roots'], probe_interpreter=sys.executable)
    row = {'check_id': 'nc-unresolved', 'attempt_id': None, 'phase': 'cleanup_unknown',
           'source': str(p.source), 'outputs_root': str(p.outputs),
           'output': str(p.outputs / 'prior-check'), 'native_path_recipe': p.recipe}
    with p.runtime._db() as db:
        db.execute('INSERT INTO native_check VALUES(?,?,?)', (row['check_id'], None, canonical(row)))
    monkeypatch.setattr(p.runtime, '_probe_sandbox', lambda *_a, **_k: pytest.fail('probe before ownership'))
    try:
        with pytest.raises(Rejected, match='overlapping'):
            p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', candidate(p.source, recipe=p.recipe))
        assert not (p.outputs / 'check').exists()
    finally:
        p.runtime.close()


def test_real_native_check_writes_declared_build_roots_and_preserves_source(tmp_path):
    p = native_fixture(tmp_path, 'no-child')
    target, cache = p.source / 'target', tmp_path / 'native-cache'
    target.mkdir()
    cache.mkdir()
    cargo_home = tmp_path / 'cargo-home'
    cargo_home.mkdir()
    cache_file = cargo_home / '.package-cache'
    body = ('from pathlib import Path\nimport pytest\n'
            'def test_native():\n'
            f' Path({str(target / "artifact")!r}).write_text("built")\n'
            f' Path({str(cache / "cache-entry")!r}).write_text("cached")\n'
            f' Path({str(cache_file)!r}).write_text("native lock")\n'
            ' with pytest.raises(PermissionError):\n'
            f'  Path({str(cargo_home / "unapproved")!r}).write_text("forbidden")\n'
            f' link=Path({str(target / "source-link")!r})\n'
            f' link.symlink_to({str(p.source / "test_native.py")!r})\n'
            ' with pytest.raises(PermissionError):\n'
            '  link.write_text("forbidden source mutation")\n')
    (p.source / 'test_native.py').write_text(body)
    p.recipe.update(native_write_roots=[{'path': str(target), 'kind': 'generated'},
                                       {'path': str(cache), 'kind': 'cache'},
                                       {'path': str(cache_file), 'kind': 'cache-file'}],
                    probe_interpreter=sys.executable)
    p.recipe['immutable'] = inventory(p.source, recipe=p.recipe)
    frozen = candidate(p.source, recipe=p.recipe)
    try:
        result = p.runtime._check(p.recipe, p.check, p.outputs / 'check', 'native-test', frozen)
        assert result['result'] == 'pass'
        assert candidate(p.source, recipe=p.recipe) == frozen
        assert result['cleanup']['partial_mutations']['changes'] == []
        effects = result['cleanup']['native_path_effects']
        assert effects['changes'][str(target)] == ['artifact', 'source-link']
        assert effects['changes'][str(cache)] == ['cache-entry']
        assert effects['changes'][str(cache_file)] == ['.package-cache']
        row = p.runtime._native_get(result['native_check_id'])
        assert all(row['native_path_probe']['checks'].values())
        revised = copy.deepcopy(p.recipe)
        revised['checks'][0]['scope'] = 'different evidence obligation'
        with pytest.raises(Rejected, match='saved check contract'):
            p.runtime._check(revised, revised['checks'][0], p.outputs / 'check', 'native-test', frozen)
        target.rename(tmp_path / 'retired-target')
        target.mkdir()
        with pytest.raises(Rejected, match='identity changed'):
            p.runtime._native_stop(row['check_id'])
    finally:
        p.runtime.close()
