"""Literal @ paths retain exact native regex boundaries."""
import json
import re
from pathlib import Path

import pytest

from agentic_tools.aos import native_sandbox as sandbox
from agentic_tools.aos.store import Rejected


@pytest.mark.parametrize('path', [
    '/', '/fixture/old-name_1', '/fixture/a name.v1',
    '/opt/homebrew/etc/openssl@4/openssl.cnf', '/fixture/native@4.v1/file',
])
def test_regex_path_preserves_safe_literals(path):
    encoded = sandbox._regex_path(path)
    assert encoded == path.replace('.', '[.]')
    assert re.fullmatch(encoded, path)
    assert not re.fullmatch(encoded, path + '-sibling')
    if '@' in path:
        assert not re.fullmatch(encoded, path.replace('@', ''))
        assert not re.fullmatch(encoded, path.replace('@', 'X'))
    if '.' in path:
        assert not re.fullmatch(encoded, path.replace('.', 'X'))


@pytest.mark.parametrize('character', list('\\"\'\n\r\t\x00[](){}*+?|^$:;!%&=<>,'))
def test_regex_path_still_rejects_nonliteral_syntax(character):
    with pytest.raises(Rejected, match='unsupported regex-literal characters'):
        sandbox._regex_path('/fixture@4/native' + character + 'path')


def test_literal_at_does_not_expand_temporary_basename_grammar():
    with pytest.raises(Rejected, match='Temporary'):
        sandbox._name_regex('native@4-{pid}')


def test_at_parent_and_protected_prefix_render_with_exact_groups(tmp_path):
    root = tmp_path / 'native@4.v1'
    root.mkdir()
    temporary = tmp_path / 'temp@4'
    temporary.mkdir()
    protected = tmp_path / 'openssl@4'
    protected.mkdir()
    config = protected / 'openssl.cnf'
    config.write_text('public fixture')
    denied = [root / name for name in ('openssl@4', 'reserve@4')]
    recipe = {'native_sandbox': {'version': sandbox.VERSION,
              'unix_socket_roots': [str(root)],
              'denied_unix_sockets': list(map(str, denied)),
              'temporary_paths': [{'parent': str(temporary), 'name': 'native-{pid}', 'kind': 'tree'}]}}
    filesystem = {':root': 'deny', ':minimal': 'read', str(root): 'write', str(config): 'read'}
    profile = sandbox.render(recipe, filesystem, protected=[config], preexisting={})
    assert '^' + sandbox._regex_path(str(root)) + '/.*$' in profile
    assert '^' + sandbox._regex_path(str(temporary)) + '/native-' in profile
    assert '(subpath ' + json.dumps(str(config)) + ')' in profile
    groups = sandbox._socket_denial_groups(list(map(str, denied)), {})
    assert len(groups) == 1
    assert groups[0][0] == list(map(str, denied))
    expected = '(require-any ' + ' '.join('(subpath ' + json.dumps(str(path)) + ')' for path in denied) + ')'
    assert groups[0][1] == expected
    pattern = sandbox._socket_factored_pattern(list(map(str, denied)))
    assert pattern == '^' + str(root).replace('.', '[.]') + '/(openssl|reserve)@4(/.*)?$'
    for path in denied:
        assert re.fullmatch(pattern, str(path))
        assert re.fullmatch(pattern, str(path / 'child.sock'))
        assert not re.fullmatch(pattern, str(path) + '-sibling/child.sock')
        assert not re.fullmatch(pattern, str(path).replace('@', 'X') + '/child.sock')


def test_previously_supported_profile_bytes_are_unchanged(tmp_path, monkeypatch):
    root = tmp_path / 'native.v1'
    root.mkdir()
    recipe = {'native_sandbox': {'version': sandbox.VERSION,
              'unix_socket_roots': [str(root)],
              'denied_unix_sockets': [str(root / name) for name in ('reserved.sock', 'other.sock')]}}
    filesystem = {':root': 'deny', ':minimal': 'read', str(root): 'write'}
    current = sandbox.render(recipe, filesystem, preexisting={})

    def prior_regex(value):
        if not re.fullmatch(r'[A-Za-z0-9_ ./-]+', value):
            raise Rejected('native_sandbox', 'Unsupported old path')
        return value.replace('.', '[.]')

    monkeypatch.setattr(sandbox, '_regex_path', prior_regex)
    assert sandbox.render(recipe, filesystem, preexisting={}) == current
