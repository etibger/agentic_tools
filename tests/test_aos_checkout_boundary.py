"""The explicit checkout mode preserves authority while avoiding file catalogues."""
import copy
import json
import os
import re
from pathlib import Path

import pytest

from agentic_tools.aos import checkout_boundary as boundary, native_network, native_paths
from agentic_tools.aos.store import Rejected, digest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    temporary = tmp_path / 'native-tmp'
    temporary.mkdir()
    monkeypatch.setenv('TMPDIR', str(temporary) + '/')
    for name in ('source', 'outputs', 'state', 'tools', 'live-session', 'cache'):
        (tmp_path / name).mkdir()
    source = tmp_path / 'source'
    (source / 'src').mkdir()
    (source / 'src' / 'main.py').write_text('print("product")\n')
    (source / 'AGENTS.md').write_text('Retained instructions\n')
    (source / '.git').mkdir()
    (source / '.git' / 'config').write_text('readonly control\n')
    (source / 'target').mkdir()
    tools = tmp_path / 'tools'
    (tools / 'bin').mkdir()
    compiler = tools / 'bin' / 'compiler'
    compiler.write_text('not executed by these tests\n')
    compiler.chmod(0o700)
    recipe = {'execution_policy': boundary.POLICY, 'source': str(source),
              'outputs': str(tmp_path / 'outputs'), 'read_roots': [str(tools)],
              'native_write_roots': [{'path': str(source / 'target'), 'kind': 'generated'},
                                     {'path': str(tmp_path / 'cache'), 'kind': 'cache'}],
              'checkout_policy': {'change_roots': ['src', 'tests', 'Cargo.toml'],
                                  'protected_roots': [str(tmp_path / 'live-session'), str(home / '.config/project-runtime')],
                                  'native_environment': {'PATH': str(tools / 'bin') + ':/usr/bin:/bin',
                                                         'ZIG': str(compiler), 'CARGO_BUILD_JOBS': '4'},
                                  'endpoint_limits': {'minimum_free_bytes': 1000,
                                                      'max_owned_growth_bytes': 2000,
                                                      'max_output_bytes': 3000}}}
    return recipe, tmp_path / 'state', home


def test_legacy_recipe_is_not_reinterpreted():
    legacy = {'source': 'not validated here', 'resource_limits': {'old': 'contract'}}
    assert not boundary.enabled(legacy)
    assert boundary.validate(legacy, Path('/not-opened')) is None
    with pytest.raises(Rejected, match='Unsupported explicit'):
        boundary.enabled({'execution_policy': 'future-or-misspelled'})


def test_writer_assessor_and_native_have_distinct_boundaries(setup):
    recipe, state, home = setup
    writer = boundary.profile(recipe, state)
    assessor = boundary.profile(recipe, state, assessment=True)
    native = boundary.profile(recipe, state, native=True)
    source, output = recipe['source'], recipe['outputs']
    assert writer[source] == 'write'
    assert assessor[source] == native[source] == 'read'
    assert all(role[output] == 'write' for role in (writer, assessor, native))
    for role in (writer, assessor, native):
        assert role[str(state)] == role[str(home / '.codex')] == 'deny'
        assert role[str(home / '.config/project-runtime')] == 'deny'
        assert role[str(Path(source) / '.git')] == role[str(Path(source) / 'AGENTS.md')] == 'read'
        assert role[recipe['checkout_policy']['protected_roots'][0]] == 'deny'
    target, cache = [r['path'] for r in recipe['native_write_roots']]
    assert writer[target] == assessor[target] == 'read'
    assert cache not in writer and cache not in assessor
    assert native[target] == native[cache] == 'write'
    assert native['/private/tmp'] == 'write'
    assert '/private/tmp' not in writer
    assert native['/private/tmp/codex-app-tools'] == 'deny'


@pytest.mark.parametrize('key,value', [('resource_limits', None), ('native_sandbox', {}),
    ('resource_observation_continuation', {}), ('resource_limits_carry_forward', {}), ('model_write_scope', 'existing-mutable-files'),
    ('mutable', ['src/main.py']), ('immutable', {'src/main.py': 'hash'})])
def test_new_policy_cannot_silently_mix_legacy_contracts(setup, key, value):
    recipe, state, _ = setup
    recipe[key] = value
    with pytest.raises(Rejected, match='mix exact-file'):
        boundary.validate(recipe, state)


@pytest.mark.parametrize('bad', [None, {}, {'extra': 1}, {'minimum_free_bytes': True,
    'max_owned_growth_bytes': 1, 'max_output_bytes': 1}, {'minimum_free_bytes': 0,
    'max_owned_growth_bytes': 1, 'max_output_bytes': 1}, {'minimum_free_bytes': 1,
    'max_owned_growth_bytes': 2**63, 'max_output_bytes': 1}])
def test_endpoint_limits_are_explicit_positive_counts(setup, bad):
    recipe, state, _ = setup
    recipe['checkout_policy']['endpoint_limits'] = bad
    with pytest.raises(Rejected, match='Endpoint limits'):
        boundary.validate(recipe, state)


@pytest.mark.parametrize('key', ['source', 'outputs', 'read_roots'])
def test_declared_roots_cannot_overlap_shared_state(setup, key):
    recipe, state, _ = setup
    recipe[key] = [str(state)] if key == 'read_roots' else str(state)
    with pytest.raises(Rejected, match='authoritative state'):
        boundary.validate(recipe, state)


def test_symlink_and_whole_home_roots_are_rejected(setup, tmp_path):
    recipe, state, home = setup
    alias = tmp_path / 'alias'
    alias.symlink_to(recipe['source'], target_is_directory=True)
    recipe['source'] = str(alias)
    with pytest.raises(Rejected, match='aliases'):
        boundary.validate(recipe, state)
    recipe['source'] = str(home)
    with pytest.raises(Rejected, match='whole-home'):
        boundary.validate(recipe, state)


def test_physical_state_alias_is_rejected_on_macos(setup):
    recipe, state, _ = setup
    alias = Path('/System/Volumes/Data') / str(state).lstrip('/')
    if not alias.exists() or not alias.samefile(state):
        pytest.skip('Requires the macOS data-volume firmlink')
    recipe['read_roots'].append(str(alias))
    with pytest.raises(Rejected):
        boundary.validate(recipe, state)


def test_source_and_output_cross_links_are_rejected_without_mutation(setup):
    recipe, _, _ = setup
    source = Path(recipe['source']) / 'src/main.py'
    output = Path(recipe['outputs']) / 'source-alias'
    os.link(source, output)
    before = source.read_bytes()
    with pytest.raises(Rejected, match='hardlinks'):
        boundary.validate_tree(recipe)
    assert output.exists() and source.read_bytes() == before


def test_output_link_to_external_file_is_rejected(setup, tmp_path):
    recipe, _, _ = setup
    outside = tmp_path / 'private-outside'
    outside.write_text('do not modify')
    os.link(outside, Path(recipe['outputs']) / 'external-alias')
    with pytest.raises(Rejected, match='hardlinks'):
        boundary.validate_tree(recipe)
    assert outside.read_text() == 'do not modify'


def test_generated_internal_links_keep_existing_native_closure_check(setup, tmp_path):
    recipe, _, _ = setup
    target = Path(recipe['source']) / 'target'
    artifact = target / 'artifact'
    artifact.write_text('build')
    os.link(artifact, target / 'artifact-alias')
    assert boundary.validate_tree(recipe)['source'] == 3
    native_paths.snapshot(recipe)
    os.link(artifact, tmp_path / 'external-artifact-alias')
    with pytest.raises(Rejected, match='alias outside'):
        native_paths.snapshot(recipe)


def test_source_symlinks_are_not_followed(setup, tmp_path):
    recipe, _, _ = setup
    target = tmp_path / 'private'
    target.mkdir()
    (Path(recipe['source']) / 'src' / 'alias').symlink_to(target, target_is_directory=True)
    with pytest.raises(Rejected):
        boundary.validate_tree(recipe)


@pytest.mark.parametrize('path', ['src/../outside.py', '/absolute', 'src//main.py', 'src\\main.py',
    '.git/config', 'src/.codex/config.toml', 'src/AGENTS.md', 'AGENTS.override.md', 'AGENTS.extra'])
def test_change_scope_rejects_aliases_and_controls_even_under_whole_checkout(setup, path):
    recipe, _, _ = setup
    recipe['checkout_policy']['change_roots'] = ['.']
    assert not boundary.changes_allowed(recipe, [path])


def test_changes_are_scoped_and_manifest_support_is_immutable(setup):
    recipe, _, _ = setup
    assert boundary.changes_allowed(recipe, ['src/main.py', 'tests/new.py', 'Cargo.toml'])
    assert not boundary.changes_allowed(recipe, ['README.md'])
    assert not boundary.changes_allowed(recipe, ['src-sibling/main.py'])
    source = Path(recipe['source'])
    recipe['instruction_manifest'] = {'support': [{'path': str(source / 'src/policy.txt'),
                                                  'resolved': str(source / 'src/policy.txt')}]}
    assert not boundary.changes_allowed(recipe, ['src/policy.txt'])
    assert boundary.changes_allowed(recipe, ['src/main.py'])


def test_native_profile_keeps_global_instruction_credentials_closed(setup):
    recipe, state, home = setup
    global_instruction = home / '.codex/AGENTS.md'
    recipe['instruction_manifest'] = {'chain': [{'path': str(global_instruction),
                                                'resolved': str(global_instruction)}]}
    fs = boundary.profile(recipe, state, native=True)
    assert fs[str(home / '.codex')] == 'deny'
    assert str(global_instruction) not in fs
    assert 'network-outbound' in boundary.render_native(recipe, state)


def test_cargo_cache_files_do_not_grant_control_or_future_subtree_writes(setup):
    recipe, state, home = setup
    cargo = home / '.cargo'
    cargo.mkdir()
    for name in ('config.toml', 'env', 'credentials.toml'):
        (cargo / name).write_text('private representative fixture')
    leaf = cargo / '.global-cache'
    recipe['read_roots'].append(str(cargo))
    recipe['native_write_roots'].append({'path': str(leaf), 'kind': 'cache-file'})
    fs = boundary.profile(recipe, state, native=True)
    assert fs[str(cargo)] == 'read' and fs[str(leaf)] == 'write'
    assert fs[str(cargo / 'credentials.toml')] == 'deny'
    policy = boundary.render_native(recipe, state)
    assert '(literal ' + json.dumps(str(leaf)) + ')' in policy
    assert '(subpath ' + json.dumps(str(leaf)) + ')' not in policy
    leaf.mkdir()
    with pytest.raises(Rejected, match='regular files'):
        boundary.validate(recipe, state)


def test_cargo_directory_write_and_control_file_write_are_rejected(setup):
    recipe, state, home = setup
    cargo = home / '.cargo'
    cargo.mkdir()
    for declaration in ({'path': str(cargo), 'kind': 'cache'},
                        {'path': str(cargo / 'config.toml'), 'kind': 'cache-file'}):
        current = copy.deepcopy(recipe)
        current['native_write_roots'].append(declaration)
        with pytest.raises(Rejected):
            boundary.validate(current, state)


def test_native_policy_protects_prefixes_after_file_replacement(setup, tmp_path):
    recipe, state, _ = setup
    control = tmp_path / 'native-tmp' / 'control'
    control.write_text('private representative fixture')
    recipe['checkout_policy']['protected_roots'].append(str(control))
    policy = boundary.render_native(recipe, state)
    # The compiled negative is a prefix even while the control is a file.
    assert '(require-not (subpath ' + json.dumps(str(control)) + '))' in policy
    assert '(require-not (literal ' + json.dumps(str(control)) + '))' not in policy
    # No writable ancestor covers source in this fixture: the whole checkout
    # is read-only, so individual instruction exclusions would be redundant.
    assert boundary.profile(recipe, state, native=True)[recipe['source']] == 'read'


def test_native_ipc_is_owned_output_only_and_never_tcp(setup):
    recipe, state, _ = setup
    policy = boundary.render_native(recipe, state)
    network = [line for line in policy.splitlines() if '(allow network-' in line]
    assert len(network) == 2
    assert all('unix-socket' in line and recipe['outputs'] in line for line in network)
    assert not any('tcp' in line for line in network)
    assert 'local unix-socket (subpath "/private/tmp")' not in policy
    assert '(allow default)' not in policy


def test_native_environment_does_not_inherit_tokens_or_redirect_caches(setup, monkeypatch):
    recipe, state, _ = setup
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-copy')
    monkeypatch.setenv('CARGO_HOME', '/untrusted-cache')
    boundary.validate(recipe, state)
    model = boundary.environment(recipe)
    native = boundary.environment(recipe, native=True)
    assert 'OPENAI_API_KEY' not in native and 'CARGO_HOME' not in native
    assert native['TMPDIR'] == os.environ['TMPDIR']
    assert native['RUSTUP_AUTO_INSTALL'] == '0' and native['CARGO_NET_OFFLINE'] == 'true'
    assert model['PATH'] == '/usr/bin:/bin:/usr/sbin:/sbin'
    assert 'TMPDIR' not in model and 'ZIG' not in model


@pytest.mark.parametrize('key,value', [('RUSTUP_AUTO_INSTALL', '1'), ('CARGO_NET_OFFLINE', 'false'),
    ('CARGO_BUILD_JOBS', '1000'), ('PATH', '.:/usr/bin'), ('DYLD_INSERT_LIBRARIES', '/injected'),
    ('CARGO_HOME', '/redirected'), ('RUSTUP_TOOLCHAIN', '../untrusted')])
def test_native_environment_rejects_expanded_or_unavailable_authority(setup, key, value):
    recipe, state, _ = setup
    recipe['checkout_policy']['native_environment'][key] = value
    with pytest.raises(Rejected):
        boundary.validate(recipe, state)


def test_native_tool_cannot_come_from_mutable_checkout(setup):
    recipe, state, _ = setup
    compiler = Path(recipe['source']) / 'src/compiler'
    compiler.write_text('mutable compiler')
    compiler.chmod(0o700)
    recipe['checkout_policy']['native_environment']['ZIG'] = str(compiler)
    recipe['read_roots'].append(recipe['source'])
    with pytest.raises(Rejected, match='read-only trusted'):
        boundary.validate(recipe, state)


@pytest.mark.parametrize('where', ['source', 'child', 'tools', 'state'])
def test_native_temp_cannot_open_source_or_trusted_roots(setup, monkeypatch, where):
    recipe, state, _ = setup
    if where == 'source':
        temporary = Path(recipe['source'])
    elif where == 'child':
        temporary = Path(recipe['source']) / 'src'
    elif where == 'state':
        temporary = state
    else:
        temporary = Path(recipe['read_roots'][0]) / 'bin'
    monkeypatch.setenv('TMPDIR', str(temporary))
    with pytest.raises(Rejected, match='Native temporary'):
        boundary.validate(recipe, state)


def test_temp_ancestor_keeps_source_readonly_and_generated_temp_is_explicit(setup, monkeypatch):
    recipe, state, _ = setup
    source = Path(recipe['source'])
    monkeypatch.setenv('TMPDIR', str(source.parent))
    fs = boundary.profile(recipe, state, native=True)
    assert fs[str(source)] == 'read'
    assert fs[str(state)] == 'deny'
    monkeypatch.setenv('TMPDIR', str(source / 'target'))
    fs = boundary.profile(recipe, state, native=True)
    assert fs[str(source)] == 'read' and fs[str(source / 'target')] == 'write'


def test_tool_roots_cannot_be_relabelled_writable_cache(setup):
    recipe, state, _ = setup
    recipe['native_write_roots'].append({'path': recipe['read_roots'][0], 'kind': 'cache'})
    with pytest.raises(Rejected, match='trusted read/tool'):
        boundary.validate(recipe, state)


def test_project_test_environment_uses_readonly_inputs_and_owned_xdg(setup):
    recipe, state, _ = setup
    tools = Path(recipe['read_roots'][0])
    windows = tools / 'libc.txt'
    windows.write_text('existing Windows SDK description')
    output = Path(recipe['outputs'])
    env = recipe['checkout_policy']['native_environment']
    env.update(RUSTUP_HOME=str(tools), NEXTEST_TEST_THREADS='1', NEXTEST_USER_CONFIG_FILE='none',
               LIBGHOSTTY_VT_WINDOWS_LIBC=str(windows))
    for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
        path = output / key.lower()
        path.mkdir()
        env[key] = str(path)
    boundary.validate(recipe, state)
    actual = boundary.environment(recipe, native=True)
    assert actual['NEXTEST_USER_CONFIG_FILE'] == 'none'
    assert actual['GIT_CONFIG_COUNT'] == '1'
    assert actual['GIT_CONFIG_KEY_0'] == 'core.fsmonitor'
    assert actual['GIT_CONFIG_VALUE_0'] == 'false'
    env['XDG_RUNTIME_DIR'] = str(state)
    with pytest.raises(Rejected, match='strict output children'):
        boundary.validate(recipe, state)


def test_explicit_nextest_configuration_stays_readonly(setup):
    recipe, state, _ = setup
    config = Path(recipe['read_roots'][0]) / 'nextest.toml'
    config.write_text('[profile.default]')
    recipe['checkout_policy']['native_environment']['NEXTEST_USER_CONFIG_FILE'] = str(config)
    boundary.validate(recipe, state)
    recipe['checkout_policy']['native_environment']['NEXTEST_USER_CONFIG_FILE'] = str(Path(recipe['outputs']) / 'config')
    with pytest.raises(Rejected):
        boundary.validate(recipe, state)


@pytest.mark.parametrize('deadline', [0.0, 10.0])
def test_tree_deadline_rejects_before_any_directory_observation(setup, monkeypatch, deadline):
    recipe, _, _ = setup
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: 10.0)
    def unexpected(_path):
        raise AssertionError('Expired scan must not enumerate a directory')
    monkeypatch.setattr(Path, 'iterdir', unexpected)
    with pytest.raises(Rejected) as error:
        boundary.validate_tree(recipe, deadline=deadline)
    assert error.value.code == 'checkout_deadline'


def test_tree_deadline_stops_before_metadata_for_a_newly_listed_entry(setup, monkeypatch):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    entry = source / 'late-entry'
    entry.write_text('must not be observed after deadline')
    clock = [0.0]
    original_iterdir, original_lstat = Path.iterdir, Path.lstat
    def listing(path):
        if path == source:
            clock[0] = 10.0
            yield entry
        else:
            yield from original_iterdir(path)
    def metadata(path, *args, **kwargs):
        if path == entry and clock[0] >= 10.0:
            raise AssertionError('Expired entry was inspected')
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(Path, 'iterdir', listing)
    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(Rejected) as error:
        boundary.validate_tree(recipe, deadline=10.0)
    assert error.value.code == 'checkout_deadline'


def test_tree_deadline_stops_before_descending_into_a_queued_directory(setup, monkeypatch):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    child = source / 'src'
    clock = [0.0]
    original_iterdir = Path.iterdir
    def listing(path):
        if path == source:
            yield child
            clock[0] = 10.0
        elif path == child:
            raise AssertionError('Expired scan descended into a queued directory')
        else:
            yield from original_iterdir(path)
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(Path, 'iterdir', listing)
    with pytest.raises(Rejected) as error:
        boundary.validate_tree(recipe, deadline=10.0)
    assert error.value.code == 'checkout_deadline'


def test_tree_deadline_checks_empty_roots_too(setup, tmp_path, monkeypatch):
    recipe, _, _ = setup
    empty = tmp_path / 'empty-source'
    empty.mkdir()
    recipe['source'] = str(empty)
    recipe['native_write_roots'] = []
    clock = [0.0]
    original_iterdir = Path.iterdir
    def listing(path):
        if path == empty:
            clock[0] = 10.0
            return iter(())
        return original_iterdir(path)
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(Path, 'iterdir', listing)
    with pytest.raises(Rejected) as error:
        boundary.validate_tree(recipe, deadline=10.0)
    assert error.value.code == 'checkout_deadline'


def test_tree_scan_finishes_within_shared_deadline(setup, monkeypatch):
    recipe, _, _ = setup
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: 9.0)
    assert boundary.validate_tree(recipe, deadline=10.0) == {'source': 3, 'outputs': 0}



def _xdg_recipe(recipe):
    original = Path(recipe['outputs'])
    env = recipe['checkout_policy']['native_environment']
    for key, suffix in (('XDG_CONFIG_HOME', 'config/nested'), ('XDG_STATE_HOME', 'state'),
                        ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'run')):
        path = original / suffix
        path.mkdir(parents=True)
        env[key] = str(path)
    return recipe


def test_scoped_outputs_remaps_owned_xdg_without_mutating_recipe_or_files(setup, tmp_path):
    recipe, _, _ = setup
    _xdg_recipe(recipe)
    before = copy.deepcopy(recipe)
    output = tmp_path / 'role-output'
    output.mkdir()
    scoped = boundary.scoped_outputs(recipe, output)
    assert recipe == before
    assert list(output.iterdir()) == []
    assert scoped['outputs'] == str(output)
    assert scoped['checkout_policy']['native_environment']['XDG_CONFIG_HOME'] == str(output / 'config/nested')
    for key, value in before['checkout_policy']['native_environment'].items():
        if not key.startswith('XDG_'):
            assert scoped['checkout_policy']['native_environment'][key] == value
    scoped['checkout_policy']['change_roots'].append('new-scope')
    assert recipe == before


def test_scoped_output_preparation_creates_only_private_owned_directories(setup, tmp_path):
    recipe, state, _ = setup
    _xdg_recipe(recipe)
    output = tmp_path / 'role-output'
    output.mkdir()
    (output / 'state').mkdir(mode=0o755)
    scoped = boundary.scoped_outputs(recipe, output, prepare=True)
    assert {p.name for p in output.iterdir()} == {'config', 'state', 'data', 'run'}
    for suffix in ('config', 'config/nested', 'data', 'run'):
        assert (output / suffix).stat().st_mode & 0o777 == 0o700
    assert (output / 'state').stat().st_mode & 0o777 == 0o755
    boundary.validate(scoped, state)
    assert boundary.profile(scoped, state, native=True)[str(output)] == 'write'
    assert recipe['outputs'] != scoped['outputs']


def test_scoping_keeps_legacy_behavior_and_never_prepares_legacy_directories(tmp_path):
    original = {'outputs': '/old', 'mutable': ['legacy'], 'resource_limits': {'legacy': True}}
    output = tmp_path / 'not-created'
    assert boundary.scoped_outputs(original, output, prepare=True) == {**original, 'outputs': str(output)}
    assert not output.exists()


def test_scoped_outputs_require_an_existing_real_output(setup, tmp_path):
    recipe, _, _ = setup
    _xdg_recipe(recipe)
    missing = tmp_path / 'missing'
    with pytest.raises(Rejected):
        boundary.scoped_outputs(recipe, missing, prepare=True)
    assert not missing.exists()
    alias = tmp_path / 'alias'
    alias.symlink_to(recipe['outputs'], target_is_directory=True)
    with pytest.raises(Rejected):
        boundary.scoped_outputs(recipe, alias, prepare=True)


@pytest.mark.parametrize('target', ['source', 'read', 'state', 'live'])
def test_scoped_outputs_reject_protected_or_read_namespaces(setup, target):
    recipe, state, _ = setup
    _xdg_recipe(recipe)
    recipe['checkout_policy']['protected_roots'].append(str(state))
    output = {'source': Path(recipe['source']), 'read': Path(recipe['read_roots'][0]),
              'state': state, 'live': Path(recipe['checkout_policy']['protected_roots'][0])}[target]
    before = sorted(p.name for p in output.iterdir())
    with pytest.raises(Rejected, match='Scoped output overlaps'):
        boundary.scoped_outputs(recipe, output, prepare=True)
    assert sorted(p.name for p in output.iterdir()) == before


def test_scoped_outputs_reject_an_unowned_original_xdg_path_before_creating_anything(setup, tmp_path):
    recipe, _, _ = setup
    _xdg_recipe(recipe)
    output = tmp_path / 'new-output'
    output.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    recipe['checkout_policy']['native_environment']['XDG_RUNTIME_DIR'] = str(outside)
    with pytest.raises(Rejected, match='strict owned output children'):
        boundary.scoped_outputs(recipe, output, prepare=True)
    assert list(output.iterdir()) == []


def test_scoped_outputs_reject_existing_aliases_before_creating_anything(setup, tmp_path):
    recipe, _, _ = setup
    _xdg_recipe(recipe)
    output = tmp_path / 'new-output'
    output.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (output / 'state').symlink_to(outside, target_is_directory=True)
    with pytest.raises(Rejected):
        boundary.scoped_outputs(recipe, output, prepare=True)
    assert sorted(p.name for p in output.iterdir()) == ['state']
    assert list(outside.iterdir()) == []


def test_scoped_outputs_validate_all_directory_types_before_preparing(setup, tmp_path):
    recipe, _, _ = setup
    _xdg_recipe(recipe)
    output = tmp_path / 'new-output'
    output.mkdir()
    (output / 'state').write_text('do not replace')
    with pytest.raises(Rejected, match='non-directory'):
        boundary.scoped_outputs(recipe, output, prepare=True)
    assert sorted(p.name for p in output.iterdir()) == ['state']
    assert (output / 'state').read_text() == 'do not replace'



def test_direct_internal_instruction_link_is_valid_without_reading_target(setup, monkeypatch):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    link = source / 'CLAUDE.md'
    link.symlink_to('AGENTS.md')
    def unexpected(*_args, **_kwargs):
        raise AssertionError('Source-link validation must not read file contents')
    monkeypatch.setattr(Path, 'read_bytes', unexpected)
    monkeypatch.setattr(Path, 'read_text', unexpected)
    assert boundary.validate_source_link(recipe, link) == 'AGENTS.md'
    assert boundary.validate_tree(recipe)['source'] == 4
    assert link.is_symlink()


def test_relative_parent_link_can_stay_inside_source(setup):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    link = source / 'src/guidance'
    link.symlink_to('../AGENTS.md')
    assert boundary.validate_source_link(recipe, link) == '../AGENTS.md'


@pytest.mark.parametrize('kind', ['absolute', 'external', 'missing', 'directory', 'chain', 'cycle', 'generated'])
def test_source_link_rejects_nonordinary_or_unowned_targets(setup, tmp_path, kind):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    link = source / 'alias'
    if kind == 'absolute':
        target = str(source / 'AGENTS.md')
    elif kind == 'external':
        outside = tmp_path / 'outside.txt'
        outside.write_text('private')
        target = '../outside.txt'
    elif kind == 'missing':
        target = 'missing.txt'
    elif kind == 'directory':
        target = 'src'
    elif kind == 'chain':
        (source / 'intermediate').symlink_to('AGENTS.md')
        target = 'intermediate'
    elif kind == 'cycle':
        (source / 'intermediate').symlink_to('alias')
        target = 'intermediate'
    else:
        (source / 'target/generated.txt').write_text('artifact')
        target = 'target/generated.txt'
    link.symlink_to(target)
    with pytest.raises(Rejected):
        boundary.validate_source_link(recipe, link)
    assert link.is_symlink()


def test_source_link_rejects_directory_alias_even_before_parent_cancellation(setup, tmp_path):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    outside = tmp_path / 'outside'
    outside.mkdir()
    (source / 'directory-alias').symlink_to(outside, target_is_directory=True)
    link = source / 'alias'
    link.symlink_to('directory-alias/../AGENTS.md')
    with pytest.raises(Rejected):
        boundary.validate_source_link(recipe, link)


def test_source_link_cannot_expose_a_target_with_an_external_hardlink(setup, tmp_path):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    os.link(source / 'AGENTS.md', tmp_path / 'external-instruction-alias')
    link = source / 'alias'
    link.symlink_to('AGENTS.md')
    with pytest.raises(Rejected, match='one regular file'):
        boundary.validate_source_link(recipe, link)


def test_output_symlinks_remain_forbidden_even_when_target_is_internal(setup):
    recipe, _, _ = setup
    output = Path(recipe['outputs'])
    (output / 'ordinary.txt').write_text('output')
    (output / 'alias').symlink_to('ordinary.txt')
    with pytest.raises(Rejected, match='unsupported symlink'):
        boundary.validate_tree(recipe)


def test_source_link_helper_does_not_expand_legacy_policy(setup):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    link = source / 'alias'
    link.symlink_to('AGENTS.md')
    del recipe['execution_policy']
    with pytest.raises(Rejected, match='explicit isolated-checkout'):
        boundary.validate_source_link(recipe, link)


def test_source_link_target_text_change_during_observation_is_rejected(setup, monkeypatch):
    recipe, _, _ = setup
    source = Path(recipe['source'])
    link = source / 'alias'
    link.symlink_to('AGENTS.md')
    original = os.readlink
    seen = []
    def changed(path, *args, **kwargs):
        if Path(path) == link:
            seen.append(True)
            if len(seen) > 1:
                return 'src/main.py'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'readlink', changed)
    with pytest.raises(Rejected, match='changed during observation'):
        boundary.validate_source_link(recipe, link)



@pytest.mark.parametrize('target', ['AGENTS.md/', 'AGENTS.md/.'])
def test_source_link_rejects_file_target_with_directory_suffix(setup, target):
    recipe, _, _ = setup
    link = Path(recipe['source']) / 'alias'
    link.symlink_to(target)
    assert link.is_symlink() and not link.exists()
    with pytest.raises(Rejected, match='existing ordinary file'):
        boundary.validate_source_link(recipe, link)



def test_standard_openssl_bootstrap_file_is_an_exact_native_only_read(setup):
    recipe, state, _ = setup
    path = '/private/etc/ssl/openssl.cnf'
    for assessment in (False, True):
        assert path not in boundary.profile(recipe, state, assessment=assessment)
    assert boundary.profile(recipe, state, native=True)[path] == 'read'
    policy = boundary.render_native(recipe, state)
    assert '(literal "' + path + '")' in policy
    assert '(subpath "/private/etc/ssl")' not in policy
    recipe['native_write_roots'].append({'path': path, 'kind': 'cache-file'})
    with pytest.raises(Rejected):
        boundary.validate(recipe, state)



@pytest.mark.parametrize('assessment,native', [(False, False), (True, False), (False, True)])
def test_manifest_configs_stay_unreadable_while_instructions_remain_readable(setup, tmp_path, assessment, native):
    recipe, state, _ = setup
    source = Path(recipe['source'])
    source_config = source / '.codex/config.toml'
    source_config.parent.mkdir()
    source_config.write_text('private project configuration')
    ancestor_config = tmp_path / '.codex/config.toml'
    ancestor_config.parent.mkdir()
    ancestor_config.write_text('private ancestor configuration')
    guidance = tmp_path / 'support.txt'
    guidance.write_text('readable policy guidance')
    agents = source / 'AGENTS.md'
    recipe['instruction_manifest'] = {
        'chain': [{'path': str(agents), 'resolved': str(agents)}],
        'support': [{'path': str(guidance), 'resolved': str(guidance)}],
        'configs': [{'path': str(path), 'resolved': str(path)} for path in (source_config, ancestor_config)],
    }
    boundary.validate(recipe, state)
    fs = boundary.profile(recipe, state, assessment=assessment, native=native)
    assert fs[str(source_config)] == fs[str(ancestor_config)] == 'deny'
    assert fs[str(agents)] == fs[str(guidance)] == 'read'
    if native:
        policy = boundary.render_native(recipe, state)
        for path in (source_config, ancestor_config):
            assert '(require-not (subpath ' + json.dumps(str(path)) + '))' in policy


def test_manifest_config_alias_and_retained_target_are_both_denied(setup, tmp_path):
    recipe, state, _ = setup
    source = Path(recipe['source'])
    config = source / '.codex/config.toml'
    config.parent.mkdir()
    actual = tmp_path / 'retained-private-config'
    actual.write_text('private configuration')
    config.symlink_to(actual)
    recipe['instruction_manifest'] = {'configs': [{'path': str(config), 'resolved': str(actual)}]}
    for native in (False, True):
        fs = boundary.profile(recipe, state, native=native)
        assert fs[str(config)] == fs[str(actual)] == 'deny'


@pytest.mark.parametrize('kind', ['chain', 'support', 'configs'])
def test_every_manifest_path_is_protected_from_native_cache_writes(setup, kind):
    recipe, state, _ = setup
    cache = Path(recipe['native_write_roots'][1]['path'])
    protected = cache / 'trusted-input'
    protected.write_text('do not modify')
    recipe['instruction_manifest'] = {kind: [{'path': str(protected), 'resolved': str(protected)}]}
    with pytest.raises(Rejected, match='protected'):
        boundary.validate(recipe, state)


def _policy_forms(text):
    """Read only the generated S-expression grammar for independent assertions."""
    forms, stack = [], []
    for token in re.findall(r'#?"(?:\\.|[^"\\])*"|[()]|[^()\s]+', text):
        if token == '(':
            stack.append([])
        elif token == ')':
            node = stack.pop()
            (stack[-1] if stack else forms).append(node)
        else:
            assert stack
            stack[-1].append(token)
    assert not stack
    return forms


def _expanded_policy(text):
    forms = _policy_forms(text)
    declarations = [form for form in forms if form[0] == 'define']
    definitions = {form[1]: form[2] for form in declarations}
    assert len(definitions) == len(declarations), 'Guard definitions must never collide'
    def expand(node, active=()):
        if isinstance(node, str):
            if node in definitions:
                assert node not in active
                return expand(definitions[node], (*active, node))
            return node
        items = [expand(item, active) for item in node]
        if items and items[0] == 'require-all':
            flattened = []
            for item in items[1:]:
                flattened.extend(item[1:] if isinstance(item, list) and item[0] == 'require-all' else [item])
            # Predicate conjunction is associative and commutative. Operation
            # order and all non-conjunction expression structure stay intact.
            return ['require-all', *sorted(flattened, key=repr)]
        return items
    def emit(node):
        return node if isinstance(node, str) else '(' + ' '.join(map(emit, node)) + ')'
    return '\n'.join(emit(expand(form)) for form in forms if form[0] != 'define')


@pytest.fixture
def local_tests(setup, tmp_path, monkeypatch):
    recipe, state, home = setup
    first = tmp_path / 'native-tmp'
    second = tmp_path / 'shared-tmp'
    second.mkdir()
    # Source/output/tool roots deliberately remain outside both observed roots.
    monkeypatch.setattr(boundary, '_temporary_roots', lambda: (first, second))
    recipe['checkout_policy']['native_local_tests'] = {
        'ephemeral_tcp': {'first': 50000, 'last': 50003}, 'denied_tcp_ports': [8443]}
    allowed = [50000, 50002, 50003]
    network = {'format': native_network.VERSION, 'first': 50000, 'last': 50003,
               'listening_ports': [50001], 'denied_ports': [8443, 50001],
               'allowed_port_count': len(allowed), 'allowed_ports_digest': digest(allowed),
               'listen_scope': native_network.LISTEN_SCOPE}
    return recipe, state, (first, second), network


def test_native_local_mode_does_not_change_writer_or_assessor_permissions(local_tests):
    recipe, state, _, _ = local_tests
    default = copy.deepcopy(recipe)
    del default['checkout_policy']['native_local_tests']
    for assessment in (False, True):
        assert boundary.profile(recipe, state, assessment=assessment) == boundary.profile(
            default, state, assessment=assessment)
        assert boundary.environment(recipe) == boundary.environment(default)


def test_temporary_census_retains_names_of_all_kinds_without_following_links(local_tests):
    recipe, _, (root, other), _ = local_tests
    (root / 'file').write_text('existing private data')
    (root / 'directory').mkdir()
    (root / 'directory/descendant').write_text('not traversed')
    (root / 'root-link').symlink_to('/')
    os.mkfifo(other / 'pipe')
    saved = boundary.observe_temporary(recipe)
    assert saved['scope'] == boundary._TEMPORARY_SCOPE
    assert {row['path']: [entry['name'] for entry in row['entries']] for row in saved['roots']} == {
        str(root): ['directory', 'file', 'root-link'], str(other): ['pipe']}
    assert len(saved['roots'][0]['entries']) == 3
    link = next(entry for entry in saved['roots'][0]['entries'] if entry['name'] == 'root-link')
    assert link['inode'] == (root / 'root-link').lstat().st_ino


def test_old_names_remain_excluded_after_removal_replacement_and_descendant_changes(local_tests):
    recipe, state, (root, _), network = local_tests
    old = root / 'existing'
    old.write_text('retained name')
    gone = root / 'removed'
    gone.mkdir()
    saved = boundary.observe_temporary(recipe)
    old.unlink()
    old.mkdir()
    (old / 'new-child').write_text('still inside the excluded subtree')
    gone.rmdir()
    boundary.verify_temporary_observation(recipe, saved)
    policy = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    for path in (old, gone):
        exclusion = '(require-not (subpath ' + json.dumps(str(path)) + '))'
        relevant = [line for line in _expanded_policy(policy).splitlines()
                    if line.startswith('(allow file-read-metadata ')
                    or ((line.startswith('(allow file-write* ') or line.startswith('(allow file-link '))
                        and '(subpath ' + json.dumps(str(root)) + ')' in line)]
        assert len(relevant) >= 3
        assert all(exclusion in line for line in relevant)


def test_temporary_names_created_after_preparation_block_go(local_tests):
    recipe, _, (root, _), _ = local_tests
    saved = boundary.observe_temporary(recipe)
    (root / 'new-service-namespace').mkdir()
    with pytest.raises(Rejected, match='new temporary child'):
        boundary.verify_temporary_observation(recipe, saved)


def test_replaced_temporary_root_blocks_go_even_without_new_child_names(local_tests):
    recipe, _, (root, _), _ = local_tests
    saved = boundary.observe_temporary(recipe)
    root.rename(root.with_name('retired-tmp'))
    root.mkdir()
    with pytest.raises(Rejected, match='root identity'):
        boundary.verify_temporary_observation(recipe, saved)


def test_lexical_tmpdir_alias_retains_each_old_name_and_protected_prefix(local_tests, tmp_path, monkeypatch):
    recipe, state, (root, _), network = local_tests
    alias = tmp_path / 'tmp-alias'
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv('TMPDIR', str(alias) + '/')
    (root / 'old-session').mkdir()
    guard = root / 'future-control'
    recipe['checkout_policy']['protected_roots'].append(str(guard))
    saved = boundary.observe_temporary(recipe)
    assert str(alias) in saved['roots'][0]['aliases']
    policy = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    network_lines = [line for line in _expanded_policy(policy).splitlines() if 'unix-socket' in line]
    root_patterns = ['(path-regex #' + json.dumps('^' + str(parent).replace('.', '[.]') + '/.*$') + ')'
                     for parent in (root, alias)]
    related = [line for line in network_lines if any(pattern in line for pattern in root_patterns)]
    assert len(related) >= 4
    for parent in (root, alias):
        old = '(require-not (subpath ' + json.dumps(str(parent / 'old-session')) + '))'
        guard = '(require-not (subpath ' + json.dumps(str(parent / 'future-control')) + '))'
        assert all(old in line for line in related)
        assert all(guard in line for line in network_lines)  # Protected guards are never pruned.
    alias.unlink()
    alias.mkdir()
    with pytest.raises(Rejected, match='alias'):
        boundary.verify_temporary_observation(recipe, saved)


def test_old_symlink_name_does_not_claim_ownership_of_global_target(local_tests):
    recipe, state, (root, _), network = local_tests
    (root / 'root-link').symlink_to('/')
    saved = boundary.observe_temporary(recipe)
    policy = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    assert '(require-not (subpath ' + json.dumps(str(root / 'root-link')) + '))' in policy
    assert '(require-not (subpath "/"))' not in policy


def test_optional_native_network_keeps_finite_positive_destinations(local_tests):
    recipe, state, (root, other), network = local_tests
    policy = boundary.render_native(recipe, state, temporary_observation=boundary.observe_temporary(recipe),
                                    network_observation=network)
    assert '(remote tcp "localhost:50000")' in policy
    assert '(remote tcp "localhost:50001")' not in policy
    assert '(remote tcp "localhost:*")' not in policy
    for directory in (root, other):
        assert any(str(directory) in line and 'unix-socket' in line for line in policy.splitlines())


def test_enabled_local_mode_requires_both_retained_observations(local_tests):
    recipe, state, _, network = local_tests
    with pytest.raises(Rejected, match='retained temporary'):
        boundary.render_native(recipe, state, network_observation=network)
    with pytest.raises(Rejected, match='network observation'):
        boundary.render_native(recipe, state, temporary_observation=boundary.observe_temporary(recipe))


def test_default_native_rejects_injected_temporary_observation(local_tests, monkeypatch):
    recipe, state, _, _ = local_tests
    saved = boundary.observe_temporary(recipe)
    del recipe['checkout_policy']['native_local_tests']
    monkeypatch.setattr(boundary.os, 'scandir', lambda _: pytest.fail('Disabled mode must not inspect temporary roots'))
    assert boundary.observe_temporary(recipe) is None
    assert boundary.verify_temporary_observation(recipe, None) is None
    with pytest.raises(Rejected, match='Unexpected temporary'):
        boundary.render_native(recipe, state, temporary_observation=saved)


def test_existing_temporary_namespace_cannot_be_reopened_as_owned_output(local_tests):
    recipe, state, (root, _), network = local_tests
    output = root / 'prior-output'
    output.mkdir()
    recipe['outputs'] = str(output)
    saved = boundary.observe_temporary(recipe)
    with pytest.raises(Rejected, match='pre-existing temporary namespace'):
        boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)


@pytest.mark.parametrize('deadline', [True, float('inf'), float('nan')])
def test_temporary_observation_rejects_nonfinite_or_boolean_deadline(local_tests, deadline):
    recipe, _, _, _ = local_tests
    with pytest.raises(Rejected, match='finite monotonic'):
        boundary.observe_temporary(recipe, deadline=deadline)


def test_temporary_observation_checks_deadline_before_empty_roots(local_tests, monkeypatch):
    recipe, _, _, _ = local_tests
    monkeypatch.setattr(boundary.time, 'monotonic', lambda: 5)
    with pytest.raises(Rejected, match='deadline'):
        boundary.observe_temporary(recipe, deadline=5)


def test_temporary_observation_is_bounded_by_entry_count(local_tests, monkeypatch):
    recipe, _, (root, _), _ = local_tests
    for name in ('first', 'second', 'third'):
        (root / name).touch()
    monkeypatch.setattr(boundary, '_MAX_TEMPORARY_CHILDREN', 2)
    with pytest.raises(Rejected, match='entry limit'):
        boundary.observe_temporary(recipe)


def test_incomplete_temporary_census_is_a_preparation_failure(local_tests, monkeypatch):
    recipe, _, _, _ = local_tests
    def denied(_):
        raise PermissionError('representative denied root')
    monkeypatch.setattr(boundary.os, 'scandir', denied)
    with pytest.raises(Rejected, match='complete temporary'):
        boundary.observe_temporary(recipe)


@pytest.mark.parametrize('change', ['extra-root', 'alias', 'traversal', 'identity', 'duplicate'])
def test_retained_temporary_observation_rejects_changed_authority(local_tests, change):
    recipe, state, (root, _), network = local_tests
    (root / 'existing').touch()
    saved = boundary.observe_temporary(recipe)
    row = saved['roots'][0]
    if change == 'extra-root':
        saved['roots'].append(copy.deepcopy(row))
    elif change == 'alias':
        row['aliases'].append('/unapproved')
    elif change == 'traversal':
        row['entries'][0]['name'] = '../escape'
    elif change == 'identity':
        row['identity']['inode'] = True
    else:
        row['entries'].append(copy.deepcopy(row['entries'][0]))
    with pytest.raises(Rejected):
        boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)



def test_factored_native_policy_expands_to_exact_unfactored_predicates(local_tests, monkeypatch):
    recipe, state, (root, other), network = local_tests
    for parent, name in ((root, 'first'), (root, 'second'), (other, 'third')):
        (parent / name).touch()
    saved = boundary.observe_temporary(recipe)
    factored = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    def inline(paths, **_):
        predicates = ['(require-not (subpath ' + json.dumps(path) + '))' for path in paths]
        return [], '(require-all ' + ' '.join(predicates) + ')' if predicates else None
    monkeypatch.setattr(boundary, '_shared_exclusions', inline)
    unfactored = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    assert _expanded_policy(factored) == _expanded_policy(unfactored)
    assert [line for line in factored.splitlines() if ' tcp' in line] == [
        line for line in unfactored.splitlines() if ' tcp' in line]
    paths = [str(Path(alias) / entry['name']) for row in saved['roots']
             for alias in row['aliases'] for entry in row['entries']]
    metadata = next(line for line in _expanded_policy(factored).splitlines()
                    if line.startswith('(allow file-read-metadata '))
    for path in paths:
        assert '(require-not (subpath ' + json.dumps(path) + '))' in metadata


@pytest.mark.parametrize('counts', [(4096, 0), (2048, 2048), (4067, 29)])
def test_large_census_has_bounded_guards_and_correct_root_selection(local_tests, counts):
    recipe, state, _, network = local_tests
    saved = boundary.observe_temporary(recipe)
    # Synthetic retained metadata exercises the maximum observed-name shape
    # without creating thousands of unrelated temporary filesystem objects.
    for row, count in zip(saved['roots'], counts):
        row['entries'] = [{'name': f'existing-{index:04d}', 'device': 1, 'inode': index + 1,
                           'uid': 501, 'mode': 0o100600} for index in range(count)]
    policy = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    forms = _policy_forms(policy)
    definitions = [form for form in forms if form[0] == 'define']
    names, literals, expanded = set(), [], {}
    def inspect(node):
        if isinstance(node, str):
            assert node in names  # Definition references are strictly prior.
            return expanded[node]
        if node[0] == 'require-all':
            assert 2 <= len(node) - 1 <= 16
        elif node[0] == 'require-not':
            assert len(node) == 2 and node[1][0] == 'subpath' and len(node[1]) == 2
            path = json.loads(node[1][1])
            literals.append(path)
            return {path}
        else:
            pytest.fail('A shared exclusion may only factor the existing conjunction')
        return set().union(*(inspect(child) for child in node[1:]))
    for definition in definitions:
        assert len(definition) == 3 and definition[1] not in names
        expanded[definition[1]] = inspect(definition[2])
        names.add(definition[1])
    expected = list(_observed_paths(saved))
    from collections import Counter
    repeats = 1 if 0 in counts else 2  # Full metadata plus each separate root sequence.
    assert Counter(literals) == Counter({path: repeats for path in expected})
    assert len(definitions) < len(expected) // 4
    def guards(node):
        if isinstance(node, str):
            return expanded.get(node, set())
        return set().union(*(guards(child) for child in node))
    metadata = next(form for form in forms if form[:2] == ['allow', 'file-read-metadata'])
    assert guards(metadata) == set(expected)
    for form in forms:
        if form[0] != 'allow' or not isinstance(form[-1], list) or form[-1][1:2] != ['unix-socket']:
            continue
        pattern = json.loads(form[-1][2][1][1][1:])
        expected_here = set()
        for row in saved['roots']:
            if pattern in {'^' + alias.replace('.', '[.]') + '/.*$' for alias in row['aliases']}:
                expected_here = {str(Path(alias) / entry['name']) for alias in row['aliases'] for entry in row['entries']}
        assert guards(form) == expected_here
    literal_bytes = sum(len(('(require-not (subpath ' + json.dumps(path) + '))').encode()) for path in expected)
    # The complete text contains bounded per-root inventories plus shared
    # definition/reference overhead; it does not multiply them by every rule.
    assert len(policy.encode()) < 2 * literal_bytes + 512 * len(forms)
    assert max(len(line.encode()) for line in policy.splitlines()) < 20000


def test_empty_occupied_set_emits_no_new_definitions(local_tests):
    recipe, state, _, network = local_tests
    policy = boundary.render_native(recipe, state, temporary_observation=boundary.observe_temporary(recipe),
                                    network_observation=network)
    assert '(define aos-temporary-guard-' not in policy
    assert 'aos-temporary-guard-' not in policy



def _observed_paths(saved):
    return tuple(sorted(str(Path(alias) / entry['name']) for row in saved['roots']
                        for alias in row['aliases'] for entry in row['entries']))


def test_occupied_pruning_retains_overlap_and_proves_disjoint_prefix_behavior(local_tests):
    recipe, _, (root, other), _ = local_tests
    (root / 'occupied').mkdir()
    (root / 'occupied/child').touch()
    (other / 'separate').touch()
    saved = boundary.observe_temporary(recipe)
    occupied = _observed_paths(saved)
    aliases = [(Path(alias), Path(row['path'])) for row in saved['roots'] for alias in row['aliases']]
    def logical(path):
        path = Path(path)
        for alias, canonical in sorted(aliases, key=lambda pair: len(pair[0].parts), reverse=True):
            if path.is_relative_to(alias):
                path = canonical / path.relative_to(alias)
                break
        return tuple(part.casefold() for part in path.parts)
    def below(path, parent):
        path, parent = logical(path), logical(parent)
        return path[:len(parent)] == parent
    positives = [str(root), str(other), str(root / 'occupied'), str(root / 'occupied/child'),
                 recipe['source'], *(str(alias) for alias, _ in aliases)]
    witnesses = {recipe['source'], str(root), str(other), str(root / 'fresh'), str(root / 'occupied-other')}
    witnesses.update(occupied)
    witnesses.update(str(Path(path) / 'child') for path in occupied)
    witnesses.update(str(Path(path).with_name(Path(path).name + '-adjacent')) for path in occupied)
    for positive in positives:
        retained = boundary._occupied_for_positive(positive, occupied, saved['roots'], {})
        for removed in set(occupied) - set(retained):
            assert not below(positive, removed) and not below(removed, positive)
        for point in witnesses:
            for literal in (False, True):
                matches = logical(point) == logical(positive) if literal else below(point, positive)
                old = matches and not any(below(point, denial) for denial in occupied)
                new = matches and not any(below(point, denial) for denial in retained)
                assert old == new, (positive, point, literal)
    assert boundary._occupied_for_positive(recipe['source'], occupied, saved['roots'], {}) == ()
    assert boundary._occupied_for_positive('/', occupied, saved['roots'], {}) == occupied
    assert boundary._occupied_for_positive(None, occupied, saved['roots'], {}) == occupied
    assert boundary._occupied_for_positive('unknown-regex.*', occupied, saved['roots'], {}) == occupied


def test_occupied_pruning_retains_case_aliases_symlinks_and_physical_aliases(local_tests):
    recipe, _, (root, _), _ = local_tests
    item = root / 'mixed-Case'
    item.write_text('existing file')
    outside = Path(recipe['source']) / 'src/target'
    outside.write_text('separate object')
    link = root / 'old-link'
    link.symlink_to(outside)
    hardlink = Path(recipe['source']) / 'src/hardlink'
    os.link(item, hardlink)
    saved = boundary.observe_temporary(recipe)
    occupied = _observed_paths(saved)
    for positive in (str(item.with_name('MIXED-case')), str(hardlink)):
        kept = boundary._occupied_for_positive(positive, occupied, saved['roots'], {})
        assert str(item) in kept
    # An observed symlink is uncertain; never resolve it into new authority.
    kept = boundary._occupied_for_positive(recipe['source'], occupied, saved['roots'], {})
    assert str(link) in kept
    item.unlink()
    item.mkdir()
    assert str(item) in boundary._occupied_for_positive(str(item / 'child'), occupied, saved['roots'], {})


def test_occupied_relation_errors_retain_all_restrictions(local_tests, monkeypatch):
    recipe, _, (root, _), _ = local_tests
    (root / 'old').touch()
    saved = boundary.observe_temporary(recipe)
    occupied = _observed_paths(saved)
    def unknown(*_):
        raise OSError('physical ancestry is unavailable')
    monkeypatch.setattr(boundary.native_sandbox, '_relative_parts', unknown)
    assert boundary._occupied_for_positive(recipe['source'], occupied, saved['roots'], {}) == occupied


def test_distinct_selected_guards_have_unique_prior_definitions_and_unchanged_tcp(local_tests):
    recipe, state, (root, other), network = local_tests
    (root / 'first').touch()
    (other / 'second').touch()
    saved = boundary.observe_temporary(recipe)
    policy = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    definitions = [form for form in _policy_forms(policy) if form[0] == 'define']
    names = [form[1] for form in definitions]
    assert len(names) == len(set(names)) and len(names) >= 3
    _expanded_policy(policy)  # Resolves every reference, rejecting cycles/collisions.
    actual_tcp = [line for line in policy.splitlines() if ' tcp' in line]
    assert actual_tcp == native_network.rules(recipe, network)



def _uncached_occupied_selection(root, occupied, temporary, cache):
    """Reference selection from the retained pre-memoization implementation."""
    if root is None or root == '/':
        return tuple(occupied)
    aliases = sorted(((Path(alias), Path(row['path'])) for row in temporary for alias in row['aliases']),
                     key=lambda pair: len(pair[0].parts), reverse=True)
    def ordinary(path):
        parsed = Path(path)
        for alias, canonical in aliases:
            if parsed.is_relative_to(alias):
                return str(canonical / parsed.relative_to(alias))
        return path
    parent = ordinary(root)
    return tuple(path for path in occupied if boundary.native_sandbox._socket_denial_relevant(
        {'parent': parent, 'kind': 'root'}, ordinary(path), cache))


@pytest.mark.parametrize('shape', ['ordinary', 'symlink', 'replacement'])
def test_memoization_preserves_exact_rendered_policy_bytes(local_tests, monkeypatch, shape):
    recipe, state, (root, other), network = local_tests
    first = root / 'first'
    first.write_text('retained name')
    (other / 'second').mkdir()
    if shape == 'symlink':
        (other / 'old-alias').symlink_to(Path(recipe['source']) / 'src/main.py')
    saved = boundary.observe_temporary(recipe)
    if shape == 'replacement':
        first.unlink()
        first.mkdir()
    actual = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    monkeypatch.setattr(boundary, '_occupied_for_positive', _uncached_occupied_selection)
    expected = boundary.render_native(recipe, state, temporary_observation=saved, network_observation=network)
    assert actual.encode() == expected.encode()


def test_normalized_alias_pairs_and_positive_selections_are_evaluated_once(local_tests, monkeypatch):
    recipe, _, (root, other), _ = local_tests
    (root / 'first').touch()
    (other / 'second').touch()
    saved = boundary.observe_temporary(recipe)
    occupied, cache = _observed_paths(saved), {}
    original, calls = boundary.native_sandbox._socket_denial_relevant, []
    def counted(entry, denial, proof):
        calls.append((entry['parent'], denial))
        return original(entry, denial, proof)
    monkeypatch.setattr(boundary.native_sandbox, '_socket_denial_relevant', counted)
    expected = boundary._occupied_for_positive(str(root), occupied, saved['roots'], cache)
    before = list(calls)
    assert len(before) == 2 and len(set(before)) == 2
    for alias in saved['roots'][0]['aliases']:
        assert boundary._occupied_for_positive(alias, occupied, saved['roots'], cache) == expected
    assert calls == before
    assert all(path in occupied for path in expected)  # Emit original alias spellings.


def test_occupied_plan_cache_key_includes_names_and_alias_mapping(local_tests):
    recipe, _, (root, _), _ = local_tests
    (root / 'first').touch()
    saved = boundary.observe_temporary(recipe)
    occupied, cache = list(_observed_paths(saved)), {}
    original = boundary._occupied_for_positive(str(root), occupied, saved['roots'], cache)
    (root / 'second').touch()
    occupied.append(str(root / 'second'))
    updated = boundary._occupied_for_positive(str(root), occupied, saved['roots'], cache)
    assert updated == (*original, str(root / 'second'))
    # A different retained alias mapping cannot reuse the previous plan.
    different = copy.deepcopy(saved['roots'])
    different[0]['aliases'] = [str(root)]
    assert boundary._occupied_for_positive(str(root), occupied, different, cache) == _uncached_occupied_selection(
        str(root), occupied, different, {})


def test_uncertain_positive_shortcut_keeps_all_and_cache_is_not_global(local_tests, tmp_path, monkeypatch):
    recipe, _, (root, _), _ = local_tests
    (root / 'first').touch()
    saved = boundary.observe_temporary(recipe)
    occupied = _observed_paths(saved)
    positive = tmp_path / 'positive-alias'
    positive.symlink_to(Path(recipe['source']), target_is_directory=True)
    original = boundary.native_sandbox._socket_denial_relevant
    def unexpected(*_):
        pytest.fail('An uncertain positive already requires retaining every exclusion')
    monkeypatch.setattr(boundary.native_sandbox, '_socket_denial_relevant', unexpected)
    cache = {}
    assert boundary._occupied_for_positive(str(positive), occupied, saved['roots'], cache) == occupied
    positive.unlink()
    positive.mkdir()
    assert boundary._occupied_for_positive(str(positive), occupied, saved['roots'], cache) == occupied
    monkeypatch.setattr(boundary.native_sandbox, '_socket_denial_relevant', original)
    assert boundary._occupied_for_positive(str(positive), occupied, saved['roots'], {}) == ()



def test_reviewed_host_mode_keeps_model_permissions_exact(setup):
    recipe, state, home = setup
    original = copy.deepcopy(recipe)
    recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    assert boundary.reviewed_host(recipe) is True
    assert boundary.reviewed_host(original) is False
    for assessment in (False, True):
        actual = boundary.profile(recipe, state, assessment=assessment)
        assert actual == boundary.profile(original, state, assessment=assessment)
        assert actual[str(state)] == actual[str(home / '.codex')] == 'deny'
    assert boundary.environment(recipe) == boundary.environment(original)
    assert boundary.environment(recipe, native=True) == boundary.environment(original, native=True)


@pytest.mark.parametrize('value', [None, False, True, {}, [], '', 'reviewed-host-v2', 'isolated'])
def test_reviewed_host_mode_is_an_exact_opt_in(setup, value):
    recipe, state, _ = setup
    recipe['checkout_policy']['native_validation'] = value
    with pytest.raises(Rejected, match='Unsupported native validation tier'):
        boundary.reviewed_host(recipe)
    with pytest.raises(Rejected, match='Unsupported native validation tier'):
        boundary.validate(recipe, state)


def test_reviewed_host_selector_is_pure_and_rejects_policy_mixing():
    assert boundary.reviewed_host({}) is False
    assert boundary.reviewed_host({'execution_policy': boundary.POLICY}) is False
    recipe = {'execution_policy': boundary.POLICY,
              'checkout_policy': {'native_validation': 'reviewed-host-v1'}}
    assert boundary.reviewed_host(recipe) is True
    recipe['checkout_policy']['native_local_tests'] = None
    with pytest.raises(Rejected, match='cannot mix native_local_tests'):
        boundary.reviewed_host(recipe)
    del recipe['checkout_policy']['native_local_tests']
    del recipe['execution_policy']
    with pytest.raises(Rejected, match='requires isolated-checkout-v1'):
        boundary.reviewed_host(recipe)


@pytest.mark.parametrize('mode,backend,valid', [
    (None, 'sandbox-census-v1', True),
    (None, 'host-owned-v1', False),
    ('reviewed-host-v1', 'host-owned-v1', True),
    ('reviewed-host-v1', 'sandbox-census-v1', False),
    ('reviewed-host-v1', None, False),
])
def test_reviewed_host_ownership_token_requires_exact_tier_pair(setup, monkeypatch, mode, backend, valid):
    import sys
    from agentic_tools.aos import native_processes
    recipe, _, _ = setup
    recipe['probe_interpreter'] = sys.executable
    recipe['native_process_ownership'] = backend
    if mode:
        recipe['checkout_policy']['native_validation'] = mode
    monkeypatch.setattr(native_processes.sys, 'platform', 'darwin')
    monkeypatch.setattr(native_processes, 'census', lambda: pytest.fail('Configuration must not run census'))
    if valid:
        native_processes.validate(recipe)
    else:
        with pytest.raises(ValueError, match='exact reviewed-host-v1'):
            native_processes.validate(recipe)


def test_reviewed_host_ownership_retains_the_interpreter_boundary(setup, monkeypatch):
    from agentic_tools.aos import native_processes
    recipe, _, _ = setup
    interpreter = Path(recipe['source']) / 'owned-python'
    interpreter.write_text('must never execute')
    recipe.update(probe_interpreter=str(interpreter), native_process_ownership='host-owned-v1')
    recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    monkeypatch.setattr(native_processes.sys, 'platform', 'darwin')
    with pytest.raises(ValueError, match='outside model-writable roots'):
        native_processes.validate(recipe)



def test_reviewed_host_ownership_rejects_unqualified_platform(setup, monkeypatch):
    from agentic_tools.aos import native_processes
    recipe, _, _ = setup
    recipe['checkout_policy']['native_validation'] = 'reviewed-host-v1'
    recipe['native_process_ownership'] = 'host-owned-v1'
    monkeypatch.setattr(native_processes.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='Reviewed host ownership is qualified only on macOS'):
        native_processes.validate(recipe)


@pytest.mark.parametrize('count', [16, 18, 32])
@pytest.mark.parametrize('assessment,native', [(False, False), (True, False), (False, True)])
def test_bounded_extra_protections_only_add_denies(setup, count, assessment, native):
    recipe, state, home = setup
    before = boundary.profile(recipe, state, assessment=assessment, native=native)
    original = list(recipe['checkout_policy']['protected_roots'])
    added = [str(state.parent / ('retained-control-' + str(i))) for i in range(count - len(original))]
    recipe['checkout_policy']['protected_roots'] += added
    actual = boundary.profile(recipe, state, assessment=assessment, native=native)
    assert actual == {**before, **{path: 'deny' for path in added}}
    assert actual[str(home / '.codex')] == 'deny'
    assert recipe['checkout_policy']['protected_roots'] == original + added


def test_protected_roots_over_finite_bound_still_rejected(setup):
    recipe, state, _ = setup
    recipe['checkout_policy']['protected_roots'] = [str(state.parent / ('guard-' + str(i))) for i in range(33)]
    with pytest.raises(Rejected, match='at most 32'):
        boundary.profile(recipe, state)


@pytest.mark.parametrize('target', ['source', 'outputs'])
def test_extra_protections_never_reopen_conflicting_grants(setup, target):
    recipe, state, _ = setup
    recipe['checkout_policy']['protected_roots'] += [str(state.parent / ('guard-' + str(i))) for i in range(15)]
    recipe['checkout_policy']['protected_roots'].append(recipe[target])
    with pytest.raises(Rejected, match='overlap authoritative'):
        boundary.profile(recipe, state)


def test_relocated_readiness_retains_all_original_and_derived_guards(setup):
    from agentic_tools.aos import readiness
    recipe, state, _ = setup
    old = state.parent / 'original-readiness'
    protected = [str(state), str(old / 'control'), str(old / 'runtime')]
    protected += [str(state.parent / ('retained-' + str(i))) for i in range(12)]
    recipe['checkout_policy']['protected_roots'] = protected
    current = state.parent / 'supplemental-readiness'
    output = current / 'outputs' / 'readiness-1' / 'implementation'
    output.mkdir(parents=True)
    scoped = readiness._scoped(recipe, output, (state, current / 'control', current / 'runtime'))
    assert len(scoped['checkout_policy']['protected_roots']) == 17
    expected = set(protected) | {str(current / 'control'), str(current / 'runtime')}
    assert set(scoped['checkout_policy']['protected_roots']) == expected
    actual = boundary.profile(scoped, state)
    assert all(actual[path] == 'deny' for path in expected)
    assert actual[str(output)] == 'write'
    assert recipe['checkout_policy']['protected_roots'] == protected
