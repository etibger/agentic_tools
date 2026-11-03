"""Bounded UDS denial groups preserve the declared path languages.

These tests interpret only the emitted Boolean path predicates. They do not
claim that a Python predicate evaluation qualifies macOS kernel behavior.
"""
import copy
import json
from pathlib import Path
import re

import pytest

from agentic_tools.aos import native_sandbox as sandbox
from agentic_tools.aos.store import Rejected


def expression(line):
    tokens = iter(re.findall(r'\(|\)|#?"(?:\\.|[^"\\])*"|[^\s()]+', line))

    def consume(token):
        if token == '(':
            parts = []
            for token in tokens:
                if token == ')':
                    return parts
                parts.append(consume(token))
            raise AssertionError('Unterminated emitted expression')
        if token.startswith('#"'):
            return json.loads(token[1:])
        if token.startswith('"'):
            return json.loads(token)
        assert token != ')'
        return token

    parsed = consume(next(tokens))
    assert list(tokens) == []
    return parsed


def matches(predicate, path):
    operation, *operands = predicate
    if operation in ('path-regex', 'regex'):
        assert len(operands) == 1
        return re.search(operands[0], str(path)) is not None
    if operation == 'require-any':
        assert operands
        return any(matches(value, path) for value in operands)
    if operation == 'require-all':
        assert operands
        return all(matches(value, path) for value in operands)
    if operation == 'require-not':
        assert len(operands) == 1
        return not matches(operands[0], path)
    if operation == 'literal':
        return str(path) == operands[0]
    if operation == 'subpath':
        return Path(path).is_relative_to(operands[0])
    raise AssertionError('Unexpected path predicate: ' + operation)


def subpath_members(leaf):
    predicate = expression(leaf)
    assert predicate[0] == 'require-any' and len(predicate) > 1
    assert all(term[0] == 'subpath' and len(term) == 2 for term in predicate[1:])
    return [term[1] for term in predicate[1:]]


def socket_rules(profile, effect):
    result = []
    for line in profile.splitlines():
        if line.startswith('(' + effect + ' ') and ' unix-socket ' in line:
            parsed = expression(line)
            endpoint = parsed[-1]
            assert endpoint[:2] in (['local', 'unix-socket'], ['remote', 'unix-socket'])
            assert parsed[1:-1] == (['network-bind', 'network-inbound'] if endpoint[0] == 'local'
                                     else ['network-outbound'])
            result.append((endpoint[0], endpoint[2], line))
    return result


def socket_allowed(profile, direction, path):
    # Check the positive allow predicates themselves: explicit deny rules must
    # not conceal a missing require-not on a broad positive UDS predicate.
    return any(matches(predicate, path) for where, predicate, _ in socket_rules(profile, 'allow')
               if where == direction)


def allow_parts(predicate):
    """Read the Boolean graph, without depending on regex construction text."""
    if predicate[0] != 'require-all':
        positive, negative = predicate, []
    else:
        positive = predicate[1]
        assert all(term[0] == 'require-not' and len(term) == 2 for term in predicate[2:])
        negative = [term[1] for term in predicate[2:]]
    assert positive[0] == 'path-regex', 'Each allow must have exactly one positive socket scope'
    return positive, negative


def assert_global_denial_coverage(profile, denied):
    """Every forbidden prefix and descendant remains explicitly denied."""
    for direction in ('local', 'remote'):
        predicates = [term for where, term, _ in socket_rules(profile, 'deny') if where == direction]
        for path in denied:
            for descendant in (path, Path(path) / 'child', Path(path) / 'nested' / 'child'):
                assert any(matches(term, descendant) for term in predicates), (direction, descendant)
    local = [term for where, term, _ in socket_rules(profile, 'deny') if where == 'local']
    remote = [term for where, term, _ in socket_rules(profile, 'deny') if where == 'remote']
    assert local == remote


def file_allowed(profile, operation, path):
    for line in profile.splitlines():
        if not line.startswith('(allow file-'):
            continue
        rule = expression(line)
        predicates = [item for item in rule[1:] if isinstance(item, list)]
        operations = [item for item in rule[1:] if isinstance(item, str)]
        if (operation in operations or operation.split('-', 2)[:2] == ['file', 'read'] and 'file-read*' in operations
                or operation.split('-', 2)[:2] == ['file', 'write'] and 'file-write*' in operations):
            if any(matches(predicate, path) for predicate in predicates):
                return True
    return False


@pytest.fixture
def scope(tmp_path):
    result = {name: tmp_path / name for name in ('source', 'output', 'temporary', 'state')}
    for path in result.values():
        path.mkdir()
    result['mapping'] = {':root': 'deny', ':minimal': 'read', str(result['source']): 'read',
                         str(result['output']): 'write', str(result['state']): 'deny'}
    return result


def candidate(entries=(), roots=(), denials=()):
    return {'native_sandbox': {'version': sandbox.VERSION, 'temporary_paths': list(entries),
            'unix_socket_roots': list(map(str, roots)), 'denied_unix_sockets': list(map(str, denials))}}


def render(scope, recipe, *, before=None, protected=()):
    return sandbox.render(recipe, scope['mapping'], preexisting={} if before is None else before,
                          protected=protected)


@pytest.mark.parametrize('direction', ['local', 'remote'])
def test_empty_socket_scope_has_no_positive_rule_even_with_exclusions(scope, direction):
    protected = scope['state'] / 'protected.sock'
    explicit = scope['output'] / 'reserved.sock'
    recipe = candidate([{'parent': str(scope['temporary']), 'name': 'native-{pid}.tmp', 'kind': 'file'}],
                       denials=[explicit])
    text = render(scope, recipe, protected=[protected])
    assert socket_rules(text, 'allow') == []
    assert len(socket_rules(text, 'deny')) == 4
    for path in (scope['output'] / 'worker.sock', scope['temporary'] / 'native-45.tmp', protected, explicit):
        assert not socket_allowed(text, direction, path)
    assert file_allowed(text, 'file-write-data', scope['temporary'] / 'native-45.tmp')


@pytest.mark.parametrize('kind', ['root', 'socket', 'tree'])
def test_single_socket_scope_retains_its_exact_boundary(scope, kind):
    entries, roots = [], []
    if kind == 'root':
        roots = [scope['output']]
        allowed = [scope['output'] / 'worker.sock', scope['output'] / 'nested' / 'worker.sock']
        excluded = [scope['output'], scope['output'].with_name('output-sibling') / 'worker.sock']
    else:
        entries = [{'parent': str(scope['temporary']), 'name': 'worker-{pid}.sock', 'kind': kind}]
        base = scope['temporary'] / 'worker-45.sock'
        if kind == 'socket':
            allowed, excluded = [base], [base / 'child', Path(str(base) + '-suffix')]
        else:
            allowed, excluded = [base / 'child', base / 'nested' / 'worker.sock'], [base, Path(str(base) + '-suffix') / 'child']
        excluded += [scope['temporary'] / 'worker-0.sock', scope['temporary'] / 'worker-bad.sock']
    text = render(scope, candidate(entries, roots))
    assert len(socket_rules(text, 'allow')) == 2
    for direction in ('local', 'remote'):
        for path in allowed:
            assert socket_allowed(text, direction, path), (direction, path)
        for path in excluded:
            assert not socket_allowed(text, direction, path), (direction, path)


def test_multiple_scopes_keep_individual_positives_and_all_denial_sources(scope):
    temp, output = scope['temporary'], scope['output']
    entries = [{'parent': str(temp), 'name': 'session-{pid}', 'kind': 'tree'},
               {'parent': str(temp), 'name': 'rpc-{pid}.sock', 'kind': 'socket'},
               {'parent': str(temp), 'name': 'data-{pid}', 'kind': 'file'}]
    old = temp / 'session-12'
    old.mkdir()
    (old / 'keep').write_text('preexisting resource')
    explicit = output / 'reserved.sock'
    explicit_tree_socket = temp / 'session-34' / 'reserved.sock'
    protected = scope['state'] / 'protected.sock'
    recipe = candidate(entries, [output], [explicit, explicit_tree_socket, explicit])
    before = sandbox.temporary_snapshot(recipe)
    text = render(scope, recipe, before=before, protected=[protected, protected])
    rules = socket_rules(text, 'allow')
    assert len(rules) == 6
    allowed = [output / 'worker.sock', output / 'nested' / 'worker.sock',
               temp / 'session-34' / 'worker.sock', temp / 'session-34' / 'nested' / 'worker.sock',
               temp / 'rpc-55.sock', output / 'reserved.sock-sibling']
    denied = [old, old / 'worker.sock', old / 'nested' / 'worker.sock', explicit, explicit / 'child',
              explicit_tree_socket, explicit_tree_socket / 'child', protected, protected / 'child',
              temp / 'rpc-55.sock-suffix', temp / 'rpc-55.sock' / 'child', temp / 'session-34',
              temp / 'data-55', temp / 'unknown.sock', output.with_name('output-sibling') / 'worker.sock']
    for direction in ('local', 'remote'):
        for path in allowed:
            assert socket_allowed(text, direction, path), (direction, path)
        for path in denied:
            assert not socket_allowed(text, direction, path), (direction, path)
    assert_global_denial_coverage(text, [old, explicit, explicit_tree_socket, protected])
    for direction in ('local', 'remote'):
        scoped = [allow_parts(term) for where, term, _ in rules if where == direction]
        assert len(scoped) == 3
        # A completely unrelated protected path remains a global denial, but
        # it need not enlarge any of these disjoint positive predicates.
        assert all(not any(matches(term, protected) for term in negative) for _, negative in scoped)
        for positive, negative in scoped:
            if matches(positive, output / 'worker.sock'):
                assert any(matches(term, explicit) for term in negative)
            elif matches(positive, temp / 'session-34' / 'worker.sock'):
                assert all(any(matches(term, path) for term in negative) for path in (old, explicit_tree_socket))
            else:
                assert matches(positive, temp / 'rpc-55.sock')
                assert negative == []
    assert (old / 'keep').read_text() == 'preexisting resource'


def test_socket_scope_factoring_does_not_change_filesystem_permissions(scope):
    temp, output = scope['temporary'], scope['output']
    entries = [{'parent': str(temp), 'name': 'session-{pid}', 'kind': 'tree'},
               {'parent': str(temp), 'name': 'rpc-{pid}.sock', 'kind': 'socket'},
               {'parent': str(temp), 'name': 'file-{pid}', 'kind': 'file'}]
    old = temp / 'session-11'
    old.mkdir()
    recipe = candidate(entries)
    before = sandbox.temporary_snapshot(recipe)
    base = render(scope, recipe, before=before)
    expanded = copy.deepcopy(recipe)
    expanded['native_sandbox'].update(unix_socket_roots=[str(output)], denied_unix_sockets=[str(output / 'reserved.sock')])
    text = render(scope, expanded, before=before)
    non_socket = lambda value: [line for line in value.splitlines() if ' unix-socket ' not in line]
    assert non_socket(text) == non_socket(base)
    assert len(socket_rules(base, 'allow')) == 4
    assert len(socket_rules(text, 'allow')) == 6
    for path in (output / 'worker.sock', temp / 'session-22' / 'child', temp / 'rpc-22.sock', temp / 'file-22'):
        assert file_allowed(text, 'file-read-data', path)
        assert file_allowed(text, 'file-write-data', path)
    assert file_allowed(text, 'file-read-data', scope['source'] / 'main.py')
    for path in (scope['source'] / 'main.py', scope['state'] / 'private', old / 'child', temp / 'unknown'):
        assert not file_allowed(text, 'file-write-data', path)
    for path in (scope['state'] / 'private', old / 'child', temp / 'unknown'):
        assert not file_allowed(text, 'file-read-data', path)


def test_many_socket_patterns_reuse_whole_groups_only_where_needed(scope):
    entries = [{'parent': str(scope['temporary']), 'name': f'rpc-{number}-{{pid}}.sock', 'kind': 'socket'}
               for number in range(96)]
    denied = [scope['temporary'] / f'rpc-{number}-45.sock' for number in (0, 31, 63, 95)]
    text = render(scope, candidate(entries, denials=denied))
    rules = socket_rules(text, 'allow')
    assert len(rules) == 192
    assert len(socket_rules(text, 'deny')) == 2
    assert_global_denial_coverage(text, denied)
    for direction in ('local', 'remote'):
        for number in range(96):
            path = scope['temporary'] / f'rpc-{number}-45.sock'
            assert socket_allowed(text, direction, path) is (path not in denied)
        assert not socket_allowed(text, direction, scope['temporary'] / 'rpc-96-45.sock')
        assert not socket_allowed(text, direction, scope['temporary'] / 'rpc-95-45.sock-suffix')
        for where, predicate, _ in rules:
            if where != direction:
                continue
            positive, negative = allow_parts(predicate)
            relevant = [path for path in denied if matches(positive, path)]
            if relevant:
                # One relevant member retains the entire shared group. Other
                # members are harmless because they cannot match this positive.
                assert len(negative) == 1
                assert all(matches(negative[0], path) for path in denied)
            else:
                assert negative == []


@pytest.mark.parametrize('count', [16, 17])
def test_sibling_denial_groups_observe_bound_without_losing_prefixes(scope, count):
    output = scope['output']
    denied = [output / f'reserved-{index:02}.sock' for index in range(count)]
    text = render(scope, candidate(roots=[output], denials=denied))
    assert_global_denial_coverage(text, denied)
    for direction in ('local', 'remote'):
        groups = [term for where, term, _ in socket_rules(text, 'deny') if where == direction]
        assert len(groups) == (1 if count == 16 else 2)
        # Discover each group's members by evaluating the emitted predicate,
        # rather than reproducing its string construction or slicing algorithm.
        memberships = [[path for path in denied if matches(term, path)] for term in groups]
        assert sorted(map(len, memberships)) == ([16] if count == 16 else [1, 16])
        assert sorted(path for members in memberships for path in members) == sorted(denied)
        _, negative = next(allow_parts(term) for where, term, _ in socket_rules(text, 'allow') if where == direction)
        assert negative == groups
        for path in denied:
            assert not socket_allowed(text, direction, path)
            assert not socket_allowed(text, direction, path / 'child')
            assert socket_allowed(text, direction, Path(str(path) + '-sibling'))
        assert socket_allowed(text, direction, output / 'unrelated.sock')


@pytest.mark.parametrize('count', [16, 17])
def test_only_last_member_relevance_retains_the_required_whole_group(scope, count):
    temp = scope['temporary']
    denied = [temp / f'aaa-{index:02}.sock' for index in range(count - 1)] + [temp / 'zzz-45.sock']
    recipe = candidate([{'parent': str(temp), 'name': 'zzz-{pid}.sock', 'kind': 'socket'}], denials=denied)
    text = render(scope, recipe)
    assert_global_denial_coverage(text, denied)
    for direction in ('local', 'remote'):
        positive, negative = next(allow_parts(term) for where, term, _ in socket_rules(text, 'allow') if where == direction)
        assert [path for path in denied if matches(positive, path)] == [denied[-1]]
        assert len(negative) == 1
        retained = [path for path in denied if matches(negative[0], path)]
        assert retained == (denied if count == 16 else [denied[-1]])
        assert not socket_allowed(text, direction, denied[-1])
        assert socket_allowed(text, direction, temp / 'zzz-46.sock')


@pytest.mark.parametrize('kind', ['socket', 'tree'])
def test_relevance_distinguishes_exact_roots_descendants_and_siblings(scope, kind):
    temp = scope['temporary']
    entry = {'parent': str(temp), 'name': 'worker-{pid}.sock', 'kind': kind}
    assert sandbox._socket_denial_relevant(entry, str(temp), {})
    assert sandbox._socket_denial_relevant(entry, str(temp.parent), {})
    assert sandbox._socket_denial_relevant(entry, str(temp / 'worker-45.sock'), {})
    assert sandbox._socket_denial_relevant(entry, str(temp / 'worker-45.sock' / 'child'), {}) is (kind == 'tree')
    for denial in (temp / 'worker-45.sock-sibling', temp / 'worker-bad.sock',
                   temp.with_name('temporary-sibling') / 'worker-45.sock'):
        assert sandbox._socket_denial_relevant(entry, str(denial), {}) is False


def test_case_alias_overlap_is_retained_before_physical_checks(monkeypatch):
    # Isolate the casefold language branch from filesystem spelling validation.
    # This is a conservative-selection test, not macOS alias-access proof.
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    calls = []
    monkeypatch.setattr(sandbox, '_relative_parts', lambda *args: calls.append(args) or None)
    entry = {'kind': 'tree', 'parent': '/fixture/native', 'name': 'worker-{pid}.sock'}
    assert sandbox._socket_denial_relevant(entry, '/FIXTURE/NATIVE/WORKER-45.SOCK/child', {})
    assert calls == []
    assert sandbox._socket_denial_relevant(entry, '/FIXTURE/NATIVE/WORKER-45.SOCK-sibling', {}) is False
    assert len(calls) == 2


@pytest.mark.parametrize('relation', ['descendant', 'ancestor', 'case-descendant', 'disjoint-suffix'])
def test_data_volume_alias_relations_are_used_conservatively(monkeypatch, relation):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    parent = Path('/private/fixture/native')
    denial = Path('/System/Volumes/Data/private/fixture/alias.sock')
    suffix = {'descendant': ('worker-45.sock',), 'case-descendant': ('WORKER-45.SOCK',),
              'disjoint-suffix': ('worker-45.sock-sibling',)}.get(relation)
    calls = []

    def physical(path, base, cache):
        calls.append((path, base))
        if (path, base) == (denial, parent):
            return suffix
        assert (path, base) == (parent, denial)
        return ('native',) if relation == 'ancestor' else None

    monkeypatch.setattr(sandbox, '_relative_parts', physical)
    entry = {'kind': 'socket', 'parent': str(parent), 'name': 'worker-{pid}.sock'}
    assert sandbox._socket_denial_relevant(entry, str(denial), {}) is (relation != 'disjoint-suffix')
    assert calls == [(denial, parent), (parent, denial)]


@pytest.mark.parametrize('failure', [PermissionError, OSError, RuntimeError])
def test_uncertain_physical_relation_keeps_the_denial(monkeypatch, failure):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))

    def unavailable(*args):
        raise failure('Synthetic unavailable ancestor identity')

    monkeypatch.setattr(sandbox, '_relative_parts', unavailable)
    entry = {'kind': 'socket', 'parent': '/fixture/native', 'name': 'worker-{pid}.sock'}
    assert sandbox._socket_denial_relevant(entry, '/another/namespace/denial.sock', {}) is True


def test_unknown_spelling_remains_singleton_and_relevant(monkeypatch):
    uncertain = '/fixture/CASE-alias.sock'

    def canonical(path, **kwargs):
        if path == uncertain:
            raise ValueError('Synthetic noncanonical spelling')
        return Path(path)

    monkeypatch.setattr(sandbox, '_canonical', canonical)
    paths = ['/fixture/a.sock', uncertain, '/fixture/b.sock']
    groups = sandbox._socket_denial_groups(paths, {})
    assert sorted(path for members, _ in groups for path in members) == sorted(paths)
    assert next(members for members, _ in groups if uncertain in members) == [uncertain]
    assert next(leaf for members, leaf in groups if uncertain in members) == (
        '(path-regex #' + json.dumps('^' + uncertain.replace('.', '[.]') + '(/.*)?$') + ')')
    entry = {'kind': 'socket', 'parent': '/unrelated', 'name': 'worker-{pid}.sock'}
    assert sandbox._socket_denial_relevant(entry, uncertain, {}) is True
    for members, leaf in groups:
        predicate = expression(leaf)
        for path in members:
            assert matches(predicate, path)
            assert matches(predicate, path + '/child')
            assert not matches(predicate, path + '-sibling')


@pytest.mark.parametrize('parent', ['/', '/fixture.v1'])
def test_grouped_literals_keep_root_separator_dots_and_spaces_exact(monkeypatch, parent):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    paths = [str(Path(parent) / name) for name in ('a name.sock', 'a-name.sock')]
    groups = sandbox._socket_denial_groups(paths, {})
    assert len(groups) == 1
    predicate = expression(groups[0][1])
    for path in paths:
        assert matches(predicate, path)
        assert matches(predicate, path + '/child')
        for other in (path + '-sibling', path.replace('.sock', 'Xsock'), '/' + path):
            assert not matches(predicate, other)
    if parent != '/':
        assert not matches(predicate, paths[0].replace('fixture.v1', 'fixtureXv1'))


def test_membership_mach_lookup_remains_an_exact_native_service(scope):
    text = render(scope, candidate())
    rules = [expression(line) for line in text.splitlines() if line.startswith('(allow mach-lookup ')]
    names = []
    for rule in rules:
        assert rule[:2] == ['allow', 'mach-lookup']
        for predicate in rule[2:]:
            assert predicate[0] == 'global-name' and len(predicate) == 2
            names.append(predicate[1])
    assert set(names) == {'com.apple.system.opendirectoryd.libinfo', 'com.apple.system.opendirectoryd.membership',
                          'com.apple.cfprefsd.daemon', 'com.apple.cfprefsd.agent', 'com.apple.system.logger'}


def legacy_denial_groups(paths, cache):
    """Frozen pre-budget grouping, for byte/permission compatibility checks."""
    parents, indices = {}, []
    for index, path in enumerate(paths):
        if sandbox._socket_canonical(path, cache):
            parents.setdefault(str(Path(path).parent), []).append(index)
        else:
            indices.append([index])
    for siblings in parents.values():
        indices.extend(siblings[start:start + 16] for start in range(0, len(siblings), 16))
    result = []
    for group in sorted(indices, key=lambda items: items[0]):
        members = [paths[index] for index in group]
        if len(members) == 1:
            pattern = '^' + members[0].replace('.', '[.]') + '(/.*)?$'
        else:
            parent = str(Path(members[0]).parent)
            prefix = parent.replace('.', '[.]') + ('' if parent == '/' else '/')
            pattern = '^' + prefix + '(' + '|'.join(Path(p).name.replace('.', '[.]') for p in members) + ')(/.*)?$'
        result.append((members, '(path-regex #' + json.dumps(pattern, ensure_ascii=True) + ')'))
    return result


def encoded_literal_bytes(leaf):
    assert leaf.startswith('(path-regex #') and leaf.endswith(')')
    return len(leaf[len('(path-regex '):-1].encode())


@pytest.mark.parametrize('extra', [0, 1])
def test_grouping_budget_counts_exact_encoded_literal_boundary(monkeypatch, extra):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    parent = '/' + '/'.join(('p' * 200, 'q' * 200, 'r' * 150))
    first, second = 'a' * 80, 'b' * 80
    initial = '#'+json.dumps('^'+parent+'/('+first+'|'+second+')(/.*)?$', ensure_ascii=True)
    second += 'b' * (768 - len(initial.encode()) + extra)
    paths = [parent+'/'+first, parent+'/'+second]
    old = legacy_denial_groups(paths, {})
    assert encoded_literal_bytes(old[0][1]) == 768 + extra
    groups = sandbox._socket_denial_groups(paths, {})
    assert len(groups) == (1 if extra == 0 else 2)
    if extra == 0:
        assert [members for members, _ in groups] == [members for members, _ in old]
    assert [member for members, _ in groups for member in members] == paths
    assert all(subpath_members(leaf) == members for members, leaf in groups)


@pytest.mark.parametrize('parent', ['/fixture', '/fixture@4.v1/' + 'parent' * 20,
                                    '/fixture/' + 'v.' * 100])
@pytest.mark.parametrize('name_size', [40, 120])
def test_long_groups_keep_complete_exact_prefix_union(monkeypatch, parent, name_size):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    paths = [parent + '/' + f'entry-{index:02}.@' + 'x' * name_size for index in range(33)]
    groups = sandbox._socket_denial_groups(paths, {})
    assert [member for members, _ in groups for member in members] == paths
    predicates = [expression(leaf) for _, leaf in groups]
    assert all(len(members) <= 16 and (len(members) == 1 or
               encoded_literal_bytes(legacy_denial_groups(members, {})[0][1]) <= 768)
               for members, _ in groups)
    assert all(subpath_members(leaf) == members for members, leaf in groups)
    candidates = [parent, parent+'/unrelated']
    for path in paths:
        candidates += [path, path+'/child', path+'/nested/child', path+'-sibling',
                       path.replace('@', 'X'), path.replace('.', 'X'), path[:-1]]
    for candidate_path in candidates:
        expected = any(candidate_path == path or candidate_path.startswith(path+'/') for path in paths)
        assert any(matches(predicate, candidate_path) for predicate in predicates) == expected


@pytest.mark.parametrize('canonical', [True, False])
def test_oversized_singletons_preserve_raw_budget_and_uncertain_fallback(monkeypatch, canonical):
    def spelling(path, **kwargs):
        if not canonical:
            raise ValueError('Uncertain spelling')
        return Path(path)
    monkeypatch.setattr(sandbox, '_canonical', spelling)
    parent = '/' + '/'.join(['very.long@parent' * 10] * 6)
    paths = [parent+'/first.sock', parent+'/second.sock']
    groups = sandbox._socket_denial_groups(paths, {})
    assert [members for members, _ in groups] == [[path] for path in paths]
    for members, leaf in groups:
        previous = legacy_denial_groups(members, {})[0][1]
        assert encoded_literal_bytes(previous) > 768
        if canonical:
            assert subpath_members(leaf) == members
        else:
            assert leaf == previous
        assert matches(expression(leaf), members[0])
        assert matches(expression(leaf), members[0] + '/child')
        assert not matches(expression(leaf), members[0] + '-sibling')


def test_split_groups_preserve_positive_and_negative_permissions(scope, monkeypatch):
    output, temporary = scope['output'], scope['temporary']
    denied = [output / (f'reserved-{index:02}.@' + 'long-name' * 10) for index in range(33)]
    denied += [temporary / ('unrelated-' + str(index) + 'x' * 120) for index in range(16)]
    denied.append(temporary/'worker-45')
    recipe = candidate([{'parent': str(temporary), 'name': 'worker-{pid}', 'kind': 'tree'}],
                       roots=[output], denials=denied)
    current = render(scope, recipe)
    with monkeypatch.context() as old:
        old.setattr(sandbox, '_socket_denial_groups', legacy_denial_groups)
        previous = render(scope, recipe)
    assert_global_denial_coverage(current, denied)
    candidates = [output/'allowed.sock', temporary/'worker-46'/'child.sock', temporary/'other']
    for path in denied:
        candidates += [path, path/'child.sock', Path(str(path)+'-sibling'),
                       Path(str(path).replace('@', 'X')), Path(str(path).replace('.', 'X'))]
    for direction in ('local', 'remote'):
        for path in candidates:
            assert socket_allowed(current, direction, path) == socket_allowed(previous, direction, path)
    assert current != previous  # The long groups really split in this fixture.


def test_small_profiles_keep_mixed_parent_group_order_and_exact_subpaths(scope, monkeypatch):
    output, temporary = scope['output'], scope['temporary']
    denied = [output/'a@4.sock', temporary/'middle.sock', output/'b.v1.sock']
    groups = sandbox._socket_denial_groups(list(map(str, denied)), {})
    assert [members for members, _ in groups] == [[str(denied[0]), str(denied[2])], [str(denied[1])]]
    assert subpath_members(groups[0][1]) == [str(denied[0]), str(denied[2])]
    assert subpath_members(groups[1][1]) == [str(denied[1])]
    recipe = candidate(roots=[output], denials=denied)
    current = render(scope, recipe)
    with monkeypatch.context() as old:
        old.setattr(sandbox, '_socket_denial_groups', legacy_denial_groups)
        previous = render(scope, recipe)
    assert previous != current
    non_socket = lambda text: [line for line in text.splitlines() if ' unix-socket ' not in line]
    assert non_socket(previous) == non_socket(current)
    for direction in ('local', 'remote'):
        for path in [output/'allowed', *denied, *(Path(str(p)+'-sibling') for p in denied)]:
            assert socket_allowed(current, direction, path) == socket_allowed(previous, direction, path)


@pytest.mark.parametrize('names,expected', [
    (['pre-one', 'pre-two'], 'pre-(one|two)'),
    (['red.end', 'blue.end'], '(red|blue)[.]end'),
    (['pre-one.end', 'pre-two.end'], 'pre-(one|two)[.]end'),
    (['alpha', 'bravo'], '(alpha|bravo)'),
    (['lib.one@4.sock', 'lib.two@4.sock'], 'lib[.](one|two)@4[.]sock'),
    (['abc', 'abcdef'], 'ab(c|cdef)'),
    (['abc', 'defabc'], '(a|defa)bc'),
    (['aaaa', 'aaaaa'], 'aaa(a|aa)'),
])
def test_literal_affixes_have_hand_checked_nonempty_exact_patterns(names, expected):
    paths = ['/fixture@4.v1/'+name for name in names]
    pattern = sandbox._socket_factored_pattern(paths)
    assert pattern == '^/fixture@4[.]v1/' + expected + '(/.*)?$'
    assert '(|' not in pattern and '|)' not in pattern and '()' not in pattern
    for path in paths:
        assert re.fullmatch(pattern, path)
        assert re.fullmatch(pattern, path+'/child')
        assert not re.fullmatch(pattern, path+'-sibling')
        assert not re.fullmatch(pattern, path.replace('/fixture@4.v1/', '/fixtureX4.v1/'))
        assert not re.fullmatch(pattern, path.replace('/fixture@4.v1/', '/fixture@4Xv1/'))


@pytest.mark.parametrize('parent,expected_parent', [('/', '/'), ('/fixture.v1', '/fixture[.]v1'),
                                                    ('/fixture@4/a-b', '/fixture@4/a-b')])
def test_factored_prefix_union_matches_exhaustive_finite_names(parent, expected_parent):
    from itertools import product
    middles = [''.join(chars) for length in range(1, 4) for chars in product('ab', repeat=length)]
    names = ['pre.'+middle+'@end' for middle in middles]
    root = parent.rstrip('/')+'/'
    pattern = sandbox._socket_factored_pattern([root+name for name in names])
    expected = '^'+expected_parent+('' if parent == '/' else '/')+'pre[.]('+'|'.join(middles)+')@end(/.*)?$'
    assert pattern == expected
    # Enumerate a larger alphabet and length bound than the finite member set.
    for length in range(5):
        for chars in product('ab.@-', repeat=length):
            middle = ''.join(chars)
            path = root+'pre.'+middle+'@end'
            for suffix in ('', '/child', '/nested/child'):
                assert bool(re.fullmatch(pattern, path+suffix)) is (middle in middles)
            for near_miss in (path+'-sibling', path.replace('@end', 'Xend'),
                              path.replace('pre.', 'preX'), root+'pre.'+middle+'@en'):
                assert not re.fullmatch(pattern, near_miss)


def test_subpath_emission_does_not_repack_raw_budget_groups(monkeypatch):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    paths = ['/fixture/deny.%02d@'%index+'x'*42 for index in range(33)]
    groups = sandbox._socket_denial_groups(paths, {})
    # Raw encoded size is 21+53*n: 14 members use 763 bytes, 15 use 816.
    # A different emitted representation must not change the already
    # qualified raw-budget partition or relevance choices.
    assert [members for members, _ in groups] == [paths[:14], paths[14:28], paths[28:]]
    assert all(subpath_members(leaf) == members for members, leaf in groups)
    assert all(encoded_literal_bytes(legacy_denial_groups(members, {})[0][1]) <= 768
               for members, _ in groups)
    assert all(len(members) <= 16 for members, _ in groups)


def test_validation_patterns_keep_no_affix_and_singleton_bytes(monkeypatch):
    monkeypatch.setattr(sandbox, '_canonical', lambda path, **kwargs: Path(path))
    paths = ['/fixture/alpha', '/fixture/bravo']
    assert sandbox._socket_factored_pattern(paths) == '^/fixture/(alpha|bravo)(/.*)?$'
    groups = sandbox._socket_denial_groups(paths, {})
    assert len(groups) == 1 and subpath_members(groups[0][1]) == paths
    assert sandbox._socket_factored_pattern(['/fixture/single.sock']) == '^/fixture/single[.]sock(/.*)?$'
    oversized = '/'+('/long@parent.'*80).strip('/')+'/single.sock'
    assert sandbox._socket_factored_pattern([oversized]) == '^'+oversized.replace('.', '[.]')+'(/.*)?$'


def test_subpath_emission_preserves_all_prior_group_guards_and_non_socket_bytes(scope, monkeypatch):
    output = scope['output']
    paths = [output/('deny.%02d@'%index+'x'*42) for index in range(33)]
    recipe = candidate(roots=[output], denials=paths)
    current = render(scope, recipe)
    current_groups = sandbox._socket_denial_groups

    def previous_groups(paths, cache):
        return [(members, '(path-regex #' + json.dumps(sandbox._socket_factored_pattern(members)) + ')')
                for members, _ in current_groups(paths, cache)]

    with monkeypatch.context() as prior_emission:
        prior_emission.setattr(sandbox, '_socket_denial_groups', previous_groups)
        prior = render(scope, recipe)
    assert current != prior
    assert [line for line in current.splitlines() if ' unix-socket ' not in line] == [
        line for line in prior.splitlines() if ' unix-socket ' not in line]
    for direction in ('local', 'remote'):
        old_rules = [(where, term) for where, term, _ in socket_rules(prior, 'deny') if where == direction]
        new_rules = [(where, term) for where, term, _ in socket_rules(current, 'deny') if where == direction]
        assert [[str(path) for path in paths if matches(term, path)] for _, term in old_rules] == [
            [str(path) for path in paths if matches(term, path)] for _, term in new_rules]
        probes = [output/'allowed']
        for path in paths:
            probes += [path, path/'child', Path(str(path)+'-sibling'),
                       Path(str(path).replace('@', 'X')), Path(str(path).replace('.', 'X'))]
        for path in probes:
            assert socket_allowed(current, direction, path) == socket_allowed(prior, direction, path)


@pytest.mark.parametrize('path', ['/', '/fixture/../reserved.sock'])
def test_noncanonical_and_root_prefixes_keep_exact_regex_fallback(monkeypatch, path):
    monkeypatch.setattr(sandbox, '_canonical', lambda value, **kwargs: Path(value))
    groups = sandbox._socket_denial_groups([path], {})
    assert groups == [([path], '(path-regex #' + json.dumps(
        '^' + path.replace('.', '[.]') + '(/.*)?$') + ')')]
    entry = {'kind': 'root', 'parent': '/unrelated'}
    assert sandbox._socket_denial_relevant(entry, path, {}) is True
    if path == '/':
        # Preserve the historical root expression instead of changing it
        # into a subpath rule that would cover every absolute descendant.
        assert matches(expression(groups[0][1]), '/')
        assert not matches(expression(groups[0][1]), '/child')


@pytest.mark.parametrize('canonical', [True, False])
@pytest.mark.parametrize('name', ['unsafe|name', 'unsafe"name', 'unsafe(name', 'unsafe\\name'])
def test_subpath_representation_cannot_bypass_existing_literal_validation(monkeypatch, canonical, name):
    def spelling(path, **kwargs):
        if not canonical:
            raise ValueError('Uncertain spelling')
        return Path(path)

    monkeypatch.setattr(sandbox, '_canonical', spelling)
    with pytest.raises(Rejected, match='unsupported regex-literal characters'):
        sandbox._socket_denial_groups(['/fixture/' + name], {})


def test_canonical_denial_prefix_is_independent_of_current_file_type(scope):
    output = scope['output']
    reserved = output / 'reserved@4.sock'
    recipe = candidate(roots=[output], denials=[reserved])
    absent = render(scope, recipe)
    reserved.write_text('owned fixture')
    regular = render(scope, recipe)
    reserved.unlink()
    reserved.mkdir()
    directory = render(scope, recipe)
    for effect in ('allow', 'deny'):
        assert socket_rules(absent, effect) == socket_rules(regular, effect) == socket_rules(directory, effect)
    for text in (absent, regular, directory):
        for direction in ('local', 'remote'):
            denied = [term for where, term, _ in socket_rules(text, 'deny') if where == direction]
            assert denied == [['require-any', ['subpath', str(reserved)]]]
            for path in (reserved, reserved / 'child.sock', reserved / 'nested' / 'child.sock'):
                assert matches(denied[0], path)
                assert not socket_allowed(text, direction, path)
            assert socket_allowed(text, direction, Path(str(reserved) + '-sibling'))
            assert socket_allowed(text, direction, output / 'unrelated.sock')
