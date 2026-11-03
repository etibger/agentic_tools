"""Differential checks for invocation-local physical ancestor indexing.

These tests inspect pure permissions and renderer results. They launch no
processes and use only fixture-owned files for filesystem observations.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_sandbox as sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected


def old_uncached(path, parent, cache):
    """Exact algorithm before the parent-index memoization change."""
    if path.is_relative_to(parent):
        return path.relative_to(parent).parts
    parent_ancestors = {}
    for identity, suffix in sandbox._ancestors(parent, cache):
        parent_ancestors.setdefault(identity, []).append(suffix)
    for identity, suffix in sandbox._ancestors(path, cache):
        for parent_suffix in parent_ancestors.get(identity, ()):
            if suffix[:len(parent_suffix)] == parent_suffix:
                return suffix[len(parent_suffix):]
    return None


def old_relation(path, parent, cache):
    key = ('relative_parts', path, parent)
    if key not in cache:
        cache[key] = old_uncached(path, parent, cache)
    return cache[key]


def test_distinct_alias_children_build_the_parent_index_once(tmp_path, monkeypatch):
    parent = tmp_path / 'physical-parent'
    parent.mkdir()
    alias = tmp_path / 'parent-alias'
    alias.symlink_to(parent, target_is_directory=True)
    children = [alias / f'absent-child-{index}' for index in range(32)]
    original, iterations = sandbox._ancestors, []

    class CountedRows:
        def __init__(self, rows):
            self.rows = rows

        def __iter__(self):
            iterations.append(parent)
            return iter(self.rows)

    def observed(path, cache):
        rows = original(path, cache)
        return CountedRows(rows) if path == parent else rows

    monkeypatch.setattr(sandbox, '_ancestors', observed)
    reference_cache = {}
    expected = [old_relation(child, parent, reference_cache) for child in children]
    assert expected == [(child.name,) for child in children]
    assert len(iterations) == len(children)
    iterations.clear()
    cache = {}
    assert [sandbox._relative_parts(child, parent, cache) for child in children] == expected
    assert iterations == [parent]
    index = cache[('relative_parent_ancestors', parent)]
    assert all(isinstance(suffixes, tuple) for suffixes in index.values())
    assert sandbox._relative_parts(alias / 'one-more-child', parent, cache) == ('one-more-child',)
    assert cache[('relative_parent_ancestors', parent)] is index
    assert iterations == [parent]


@pytest.mark.parametrize('parent_rows,child_rows,expected', [
    ([((1, 2), ('branch', 'nested')), ((1, 2), ('branch',)),
      ((1, 2), ('branch', 'nested'))],
     [((1, 2), ('branch', 'nested', 'leaf'))], ('leaf',)),
    ([((1, 2), ('branch',)), ((1, 2), ('branch', 'nested'))],
     [((1, 2), ('branch', 'nested', 'leaf'))], ('nested', 'leaf')),
    ([((1, 2), ()), ((3, 4), ())],
     [((3, 4), ('first',)), ((1, 2), ('second',))], ('first',)),
], ids=['duplicate-suffixes', 'parent-order', 'child-order'])
def test_duplicate_identities_preserve_suffix_and_child_order(
        tmp_path, monkeypatch, parent_rows, child_rows, expected):
    parent, child = tmp_path / 'parent', tmp_path / 'child'
    rows = {parent: tuple(parent_rows), child: tuple(child_rows)}
    monkeypatch.setattr(sandbox, '_ancestors', lambda path, cache: rows[path])
    assert old_relation(child, parent, {}) == expected
    cache = {}
    assert sandbox._relative_parts(child, parent, cache) == expected
    ordered = {}
    for identity, suffix in parent_rows:
        ordered.setdefault(identity, []).append(suffix)
    assert cache[('relative_parent_ancestors', parent)] == {
        identity: tuple(suffixes) for identity, suffixes in ordered.items()}


@pytest.mark.parametrize('uncertain', ['parent', 'child'])
def test_uncertain_ancestry_propagates_without_caching_a_relation(tmp_path, monkeypatch, uncertain):
    parent, child = tmp_path / 'parent', tmp_path / 'child'
    failing, attempts = True, []

    def rows(path, cache):
        if failing and path == (parent if uncertain == 'parent' else child):
            attempts.append(path)
            # A partially consumed sequence must never become a parent index.
            yield (7, 8), ('unrelated',)
            raise Rejected('native_scope', 'Cannot verify physical native path ancestry')
        yield (1, 2), (() if path == parent else ('leaf',))

    monkeypatch.setattr(sandbox, '_ancestors', rows)
    with pytest.raises(Rejected, match='Cannot verify physical'):
        old_relation(child, parent, {})
    attempts.clear()
    cache = {}
    for _ in range(2):
        with pytest.raises(Rejected, match='Cannot verify physical'):
            sandbox._relative_parts(child, parent, cache)
        assert ('relative_parts', child, parent) not in cache
        assert (('relative_parent_ancestors', parent) in cache) == (uncertain == 'child')
    assert len(attempts) == 2
    failing = False
    assert sandbox._relative_parts(child, parent, cache) == old_relation(child, parent, {}) == ('leaf',)


def test_alias_replacement_keeps_old_invocation_semantics_and_refreshes_with_new_cache(tmp_path):
    first, second, alias = [tmp_path / name for name in ('first', 'second', 'parent-alias')]
    first.mkdir()
    second.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    old_cache, new_cache = {}, {}
    assert old_relation(first / 'before', alias, old_cache) == ('before',)
    assert sandbox._relative_parts(first / 'before', alias, new_cache) == ('before',)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    for path, expected in ((first / 'after', ('after',)), (second / 'after', None)):
        assert old_relation(path, alias, old_cache) == expected
        assert sandbox._relative_parts(path, alias, new_cache) == expected
    for path, expected in ((first / 'after', None), (second / 'after', ('after',))):
        assert old_relation(path, alias, {}) == expected
        assert sandbox._relative_parts(path, alias, {}) == expected


def test_case_alias_relationships_match_old_algorithm_when_supported(tmp_path):
    parent = tmp_path / 'MixedCaseParent'
    parent.mkdir()
    alias = tmp_path / 'mixedcaseparent'
    if not alias.exists() or not os.path.samefile(alias, parent):
        pytest.skip('Fixture volume does not provide case aliases')
    for path, root, expected in ((alias, parent, ()), (parent, alias, ()),
                                 (alias / 'MissingLeaf', parent, ('MissingLeaf',))):
        assert sandbox._relative_parts(path, root, {}) == old_relation(path, root, {}) == expected


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Data-volume firmlink alias')
def test_firmlink_relationships_match_old_algorithm_when_available(tmp_path):
    parent = tmp_path.resolve()
    alias = Path('/System/Volumes/Data') / parent.relative_to('/')
    if not alias.is_dir() or not os.path.samefile(alias, parent):
        pytest.skip('Fixture has no Data-volume firmlink alias')
    for path, root, expected in ((alias, parent, ()), (parent, alias, ()),
                                 (alias / 'absent-child', parent, ('absent-child',))):
        assert sandbox._relative_parts(path, root, {}) == old_relation(path, root, {}) == expected


@pytest.mark.parametrize('access', ['deny', 'read'])
def test_model_explicit_alias_access_stays_more_restrictive(tmp_path, monkeypatch, access):
    parent = tmp_path / 'physical-parent'
    parent.mkdir()
    selected = parent / 'selected'
    selected.write_text('fixture data\n')
    alias = tmp_path / 'parent-alias'
    alias.symlink_to(parent, target_is_directory=True)
    filesystem = {':root': 'deny', str(parent): 'write', str(alias / selected.name): access}
    paths = (selected, alias / selected.name)
    actual = [CodexRuntime._model_path_access(filesystem, path, {}) for path in paths]
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, '_relative_parts_uncached', old_uncached)
        expected = [CodexRuntime._model_path_access(filesystem, path, {}) for path in paths]
    assert actual == expected == [access, access]


def profile_fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source, outputs, state, data, temporary, home = [root / name for name in
        ('source', 'outputs', 'state', 'native-data', 'temporary', 'home')]
    for path in (source, outputs, state, data, temporary, home):
        path.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: False if path.name == 'config.toml'
                        and path.parent.name == '.codex' else exists(path))
    immutable, mutable = source / 'immutable.txt', source / 'mutable.txt'
    immutable.write_text('immutable fixture\n')
    mutable.write_text('mutable fixture\n')
    selected = data / 'selected'
    selected.write_text('ordinary native fixture\n')
    owned = temporary / 'memo-owned'
    descendant = owned / 'readable-child'
    descendant.mkdir(parents=True)
    recipe = {
        'source': str(source), 'outputs': str(outputs),
        'read_roots': [str(data), str(selected), str(owned), str(descendant)],
        'immutable': {immutable.name: hashlib.sha256(immutable.read_bytes()).hexdigest()},
        'mutable': [mutable.name],
        'native_sandbox': {
            'version': sandbox.VERSION,
            'file_read_paths': [{'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()}],
            'temporary_paths': [{'parent': str(temporary), 'name': owned.name, 'kind': 'tree'}],
            'owned_temporary_roots': [str(owned)],
        },
    }
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = state
    return SimpleNamespace(runtime=runtime, recipe=recipe, source=source, outputs=outputs,
                           selected=selected, owned=owned, descendant=descendant, mutable=mutable)


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
@pytest.mark.parametrize('native', [False, True], ids=['model', 'native'])
@pytest.mark.parametrize('exact', [False, True], ids=['legacy-scope', 'exact-files'])
def test_profiles_are_identical_for_both_roles_and_write_scopes(tmp_path, monkeypatch, assessment, native, exact):
    p = profile_fixture(tmp_path, monkeypatch)
    if exact:
        p.recipe['model_write_scope'] = 'existing-mutable-files'
    actual = p.runtime._profile(p.recipe, assessment=assessment, native=native)
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, '_relative_parts_uncached', old_uncached)
        expected = p.runtime._profile(p.recipe, assessment=assessment, native=native)
    assert actual == expected
    filesystem = actual[1]['filesystem']
    assert filesystem[str(p.outputs)] == 'write'
    if not native:
        assert all(filesystem[str(path)] == 'deny' for path in (p.selected, p.owned, p.descendant))
        if exact and not assessment:
            assert filesystem[str(p.source)] == 'read'
            assert filesystem[str(p.mutable)] == 'write'
    else:
        assert all(filesystem[str(path)] == 'read' for path in (p.selected, p.owned, p.descendant))


def test_native_render_bytes_preserve_temporary_preexisting_and_explicit_denials(tmp_path, monkeypatch):
    source, outputs, temporary, state = [tmp_path.resolve() / name for name in
        ('source', 'outputs', 'temporary', 'state')]
    for path in (source, outputs, temporary, state):
        path.mkdir()
    existing = temporary / 'memo-job-100'
    existing.mkdir()
    (existing / 'control').write_text('preexisting fixture\n')
    denied = temporary / 'memo-job-101' / 'private'
    readonly = temporary / 'memo-job-101' / 'readonly'
    recipe = {'native_sandbox': {
        'version': sandbox.VERSION,
        'temporary_paths': [{'parent': str(temporary), 'name': 'memo-job-{pid}', 'kind': 'tree'},
                            {'parent': str(temporary), 'name': 'memo-file-{counter}', 'kind': 'file'}],
    }}
    filesystem = {':root': 'deny', ':minimal': 'read', str(source): 'read', str(outputs): 'write',
                  str(state): 'deny', str(temporary): 'read', str(denied): 'deny', str(readonly): 'read'}
    before = sandbox.temporary_snapshot(recipe)
    assert set(before) == {str(existing)}
    actual = sandbox.render(recipe, filesystem, preexisting=before, protected=(source, state)).encode()
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, '_relative_parts_uncached', old_uncached)
        expected = sandbox.render(recipe, filesystem, preexisting=before, protected=(source, state)).encode()
    assert actual == expected
    for path in (existing, denied, readonly):
        assert ('(require-not (subpath ' + json.dumps(str(path)) + '))').encode() in actual
    assert sandbox.temporary_snapshot(recipe) == before
