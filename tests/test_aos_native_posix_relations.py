"""Differential path relations without policy, ownership or timing changes."""
from contextlib import contextmanager
import json
import os
from pathlib import Path, PosixPath, PurePosixPath, PureWindowsPath
import sys
from types import SimpleNamespace

import pytest

from agentic_tools.aos import native_paths, native_sandbox as sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Rejected
from test_aos_native_relation_memo import profile_fixture


def old_lexical(path, parent):
    if path.is_relative_to(parent):
        return path.relative_to(parent).parts
    return None


def old_ancestors(path, cache):
    """Frozen R23 ancestry, including its cache and exception behavior."""
    key = ('ancestors', path)
    if key not in cache:
        values = []
        for ancestor in (path, *path.parents):
            identity_key = ('identity', ancestor)
            if identity_key not in cache:
                try:
                    info = ancestor.stat()
                    cache[identity_key] = (info.st_dev, info.st_ino)
                except (FileNotFoundError, NotADirectoryError):
                    cache[identity_key] = None
                except OSError as error:
                    raise Rejected('native_scope', 'Cannot verify physical native path ancestry') from error
            if cache[identity_key] is not None:
                values.append((cache[identity_key], path.relative_to(ancestor).parts))
        cache[key] = tuple(values)
    return cache[key]


def old_overlaps(left, right, *, _cache=None):
    if left == right or left.is_relative_to(right) or right.is_relative_to(left):
        return True
    cache = {} if _cache is None else _cache
    ancestors = {}
    for identity, suffix in old_ancestors(right, cache):
        ancestors.setdefault(identity, []).append(suffix)
    for identity, suffix in old_ancestors(left, cache):
        for other in ancestors.get(identity, ()):
            if suffix[:len(other)] == other or other[:len(suffix)] == suffix:
                return True
    return False


def old_uncached(path, parent, cache):
    if path.is_relative_to(parent):
        return path.relative_to(parent).parts
    key = ('relative_parent_ancestors', parent)
    if key not in cache:
        ancestors = {}
        for identity, suffix in old_ancestors(parent, cache):
            ancestors.setdefault(identity, []).append(suffix)
        cache[key] = {identity: tuple(suffixes) for identity, suffixes in ancestors.items()}
    for identity, suffix in old_ancestors(path, cache):
        for parent_suffix in cache[key].get(identity, ()):
            if suffix[:len(parent_suffix)] == parent_suffix:
                return suffix[len(parent_suffix):]
    return None


@contextmanager
def old_relations(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(native_paths, '_ancestors', old_ancestors)
        patch.setattr(native_paths, 'overlaps', old_overlaps)
        patch.setattr(sandbox, '_ancestors', old_ancestors)
        patch.setattr(sandbox, 'overlaps', old_overlaps)
        patch.setattr(sandbox, '_relative_parts_uncached', old_uncached)
        yield


@pytest.mark.parametrize('path,parent,expected', [
    ('/a', '/a', ()), ('/a/b', '/a', ('b',)), ('/ab/b', '/a', None),
    ('/a', '/', ('a',)), ('/', '/', ()), ('//a/b', '//a', ('b',)),
    ('//a', '/', None), ('/a', '//', None), ('//', '/', None),
    ('/a', '.', None), ('.', '/', None), ('a', '.', ('a',)), ('.', '.', ()),
    ('a/b', 'a', ('b',)), ('a', '/a', None), ('/a', 'a', None),
    ('a/../b', 'a', ('..', 'b')), ('a/../b', 'a/..', ('b',)),
    ('../a', '..', ('a',)), ('a/./b', 'a', ('b',)),
    ('/Case/Leaf', '/case', None), ('/café/雪', '/café', ('雪',)),
])
def test_exact_posix_lexical_relation_matches_r23(path, parent, expected):
    path, parent = PosixPath(path), PosixPath(parent)
    assert old_lexical(path, parent) == expected
    assert native_paths._lexical_relative_parts(path, parent) == expected


@pytest.mark.parametrize('path,parent', [
    (PurePosixPath('/a/b'), PurePosixPath('/a')),
    (PurePosixPath('/a'), PurePosixPath('.')),
    (PosixPath('/a/b'), PurePosixPath('/a')),
    (PureWindowsPath('C:/ROOT/Child'), PureWindowsPath('c:/root')),
    (PureWindowsPath('C:Child'), PureWindowsPath('c:')),
    (PureWindowsPath('C:/Child'), PureWindowsPath('D:/')),
    (PureWindowsPath('//HOST/share/Dir/Leaf'), PureWindowsPath('//host/SHARE/dir')),
])
def test_other_path_types_retain_the_original_lexical_behavior(path, parent):
    assert native_paths._lexical_relative_parts(path, parent) == old_lexical(path, parent)


def test_subclass_method_overrides_are_not_bypassed():
    calls = []

    class CustomPath(PosixPath):
        def is_relative_to(self, other):
            calls.append(('is_relative_to', str(self), str(other)))
            return False

        def relative_to(self, other):
            raise AssertionError('False lexical relation must not request a suffix')

    path, parent = CustomPath('/a/b'), CustomPath('/a')
    assert native_paths._lexical_relative_parts(path, parent) is None
    assert calls == [('is_relative_to', '/a/b', '/a')]


def test_subclass_overlap_does_not_add_a_relative_to_call():
    calls = []

    class CustomPath(PosixPath):
        def is_relative_to(self, other):
            calls.append(('is_relative_to', str(self), str(other)))
            return True

        def relative_to(self, other):
            raise AssertionError('R23 overlap only asks the boolean relation')

    assert native_paths.overlaps(CustomPath('/left'), CustomPath('/right')) is True
    assert calls == [('is_relative_to', '/left', '/right')]


@pytest.mark.parametrize('fault', ['none', 'missing', 'not-directory', 'permission'])
@pytest.mark.parametrize('subclass', [False, True], ids=['exact-posix', 'subclass-fallback'])
def test_ancestor_suffix_stat_order_and_partial_cache_are_identical(monkeypatch, fault, subclass):
    class CustomPath(PosixPath):
        pass

    cls = CustomPath if subclass else PosixPath
    path = cls('/a/b/c')
    sequence = (path, *path.parents)

    def run(function):
        calls, cache = [], {('identity', sequence[2]): (1, 99)}

        def observed(ancestor, *args, **kwargs):
            calls.append(ancestor)
            if ancestor == path and fault == 'missing':
                raise FileNotFoundError('fixture leaf')
            if ancestor == sequence[1] and fault == 'not-directory':
                raise NotADirectoryError('fixture parent')
            if ancestor == sequence[1] and fault == 'permission':
                raise PermissionError('fixture parent')
            return SimpleNamespace(st_dev=1, st_ino=sequence.index(ancestor) + 1)

        with monkeypatch.context() as patch:
            patch.setattr(PosixPath, 'stat', observed)
            outcomes = []
            for _ in range(2):
                try:
                    outcomes.append(function(path, cache))
                except Rejected as error:
                    outcomes.append((error.code, str(error), type(error.__cause__)))
        return outcomes, calls, cache

    assert run(native_paths._ancestors) == run(old_ancestors)


def test_exact_posix_lexical_fast_path_does_not_materialize_ancestors(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Lexical tuple comparison must not call the allocating path methods')

    monkeypatch.setattr(PosixPath, 'is_relative_to', forbidden)
    monkeypatch.setattr(PosixPath, 'relative_to', forbidden)
    assert native_paths._lexical_relative_parts(PosixPath('/a/b'), PosixPath('/a')) == ('b',)
    assert native_paths._lexical_relative_parts(PosixPath('/a'), PosixPath('.')) is None
    assert native_paths.overlaps(PosixPath('/a/b'), PosixPath('/a'))


def test_physical_aliases_and_absent_suffixes_match_r23(tmp_path):
    root = tmp_path.resolve()
    parent, alias = root / 'parent', root / 'alias'
    parent.mkdir()
    alias.symlink_to(parent, target_is_directory=True)
    original, hardlink = parent / 'original', root / 'hardlink'
    original.write_text('fixture\n')
    os.link(original, hardlink)
    pairs = [(alias, parent), (parent, alias), (alias / 'missing', parent),
             (parent / 'missing', alias), (original, hardlink), (hardlink, original),
             (original / 'missing', hardlink), (root / 'near-parent', parent)]
    for path, ancestor in pairs:
        old_cache, new_cache = {}, {}
        assert sandbox._relative_parts_uncached(path, ancestor, new_cache) == old_uncached(path, ancestor, old_cache)
        assert new_cache == old_cache
        assert native_paths.overlaps(path, ancestor) == old_overlaps(path, ancestor)


def test_same_cache_identity_drift_and_new_cache_match_r23(tmp_path):
    root = tmp_path.resolve()
    first, second, alias = (root / name for name in ('first', 'second', 'alias'))
    first.mkdir()
    second.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    old_cache, new_cache = {}, {}
    assert sandbox._relative_parts_uncached(first / 'before', alias, new_cache) == old_uncached(first / 'before', alias, old_cache)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    for path in (first / 'after', second / 'after'):
        assert sandbox._relative_parts_uncached(path, alias, new_cache) == old_uncached(path, alias, old_cache)
        assert sandbox._relative_parts_uncached(path, alias, {}) == old_uncached(path, alias, {})
    assert new_cache == old_cache


@pytest.mark.parametrize('kind', ['case', 'firmlink', 'double-root'])
def test_lexical_misses_still_use_physical_aliases(tmp_path, kind):
    parent = tmp_path.resolve() / 'MixedCase'
    parent.mkdir()
    if kind == 'case':
        alias = parent.with_name('mixedcase')
    elif kind == 'firmlink':
        if sys.platform != 'darwin':
            pytest.skip('macOS Data-volume alias')
        alias = Path('/System/Volumes/Data') / parent.relative_to('/')
    else:
        alias = Path('//' + str(parent).lstrip('/'))
    if not alias.is_dir() or not os.path.samefile(alias, parent):
        pytest.skip('Fixture filesystem does not provide this alias')
    assert native_paths._lexical_relative_parts(alias, parent) is None
    assert sandbox._relative_parts_uncached(alias / 'missing', parent, {}) == old_uncached(alias / 'missing', parent, {}) == ('missing',)
    assert native_paths.overlaps(alias, parent) == old_overlaps(alias, parent) is True


@pytest.mark.parametrize('access', ['deny', 'read'])
def test_physical_explicit_access_remains_restrictive(tmp_path, monkeypatch, access):
    source = tmp_path.resolve() / 'source'
    source.mkdir()
    selected, alias = source / 'selected', tmp_path.resolve() / 'alias'
    selected.write_text('fixture\n')
    os.link(selected, alias)
    permissions = {':root': 'deny', str(source): 'write', str(alias): access}
    actual = CodexRuntime._model_path_access(permissions, selected)
    with old_relations(monkeypatch):
        expected = CodexRuntime._model_path_access(permissions, selected)
    assert actual == expected == access


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
@pytest.mark.parametrize('native', [False, True], ids=['model', 'native'])
@pytest.mark.parametrize('exact', [False, True], ids=['legacy', 'exact-files'])
def test_ordered_profile_bytes_match_r23(tmp_path, monkeypatch, assessment, native, exact):
    fixture = profile_fixture(tmp_path, monkeypatch)
    if exact:
        fixture.recipe['model_write_scope'] = 'existing-mutable-files'
    actual = fixture.runtime._profile(fixture.recipe, assessment=assessment, native=native)
    with old_relations(monkeypatch):
        expected = fixture.runtime._profile(fixture.recipe, assessment=assessment, native=native)
    assert json.dumps(actual, ensure_ascii=False, separators=(',', ':')) == json.dumps(expected, ensure_ascii=False, separators=(',', ':'))


def test_native_policy_bytes_and_preexisting_boundaries_match_r23(tmp_path, monkeypatch):
    source, outputs, temporary, state = [tmp_path.resolve() / name for name in ('source', 'outputs', 'temporary', 'state')]
    for path in (source, outputs, temporary, state):
        path.mkdir()
    existing = temporary / 'posix-job-100'
    existing.mkdir()
    (existing / 'control').write_text('preexisting fixture\n')
    denied, readonly = temporary / 'posix-job-101/private', temporary / 'posix-job-101/readonly'
    recipe = {'native_sandbox': {'version': sandbox.VERSION, 'temporary_paths': [
        {'parent': str(temporary), 'name': 'posix-job-{pid}', 'kind': 'tree'},
        {'parent': str(temporary), 'name': 'posix-file-{counter}', 'kind': 'file'}]}}
    filesystem = {':root': 'deny', ':minimal': 'read', str(source): 'read', str(outputs): 'write',
                  str(state): 'deny', str(temporary): 'read', str(denied): 'deny', str(readonly): 'read'}
    before = sandbox.temporary_snapshot(recipe)
    actual = sandbox.render(recipe, filesystem, preexisting=before, protected=(source, state)).encode()
    with old_relations(monkeypatch):
        expected = sandbox.render(recipe, filesystem, preexisting=before, protected=(source, state)).encode()
    assert actual == expected
    for path in (existing, denied, readonly):
        assert ('(require-not (subpath ' + json.dumps(str(path)) + '))').encode() in actual
    assert sandbox.temporary_snapshot(recipe) == before
