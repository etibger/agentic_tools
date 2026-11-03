"""An explicit runtime byte limit preserves every instruction and config pin."""
import json
from pathlib import Path

import pytest

from agentic_tools.aos.cli import main
from agentic_tools.aos.instructions import (MAX_RUNTIME_DOC_BYTES, discover, readback,
                                           runtime_overrides, validate, verify_settings)
from agentic_tools.aos.store import Rejected
from test_aos_instructions import tree, register_project


def test_explicit_limit_retains_complete_chain_and_unchanged_config(tree, tmp_path):
    home, repo, source = tree
    (repo / 'AGENTS.md').write_text('a' * 32768)
    (source / 'AGENTS.md').write_text('b' * 32768)
    before = (home / '.codex/config.toml').read_bytes()
    with pytest.raises(Rejected, match='instruction_truncation'):
        discover(source)
    with pytest.raises(Rejected, match='instruction_truncation'):
        discover(source, project_doc_max_bytes=65536)
    manifest = discover(source, project_doc_max_bytes=131072)
    assert [r['path'] for r in manifest['chain']] == list(map(str, [
        home / '.codex/AGENTS.md', repo / 'AGENTS.md', source.parent / 'AGENTS.override.md', source / 'AGENTS.md']))
    assert sum(r['bytes'] for r in manifest['chain']) > 65536
    assert manifest['runtime_overrides'] == {'project_doc_max_bytes': 131072}
    assert runtime_overrides(manifest) == ['project_doc_max_bytes=131072']
    assert readback(manifest, source)['manifest'] == manifest
    assert len(validate(manifest, source, tmp_path / 'state')) == 5  # Includes original global alias target.
    assert (home / '.codex/config.toml').read_bytes() == before
    verify_settings(manifest, manifest['settings'])
    for config in ({}, {**manifest['settings'], 'project_doc_max_bytes': 32768},
                   {**manifest['settings'], 'project_doc_max_bytes': '131072'}):
        with pytest.raises(Rejected, match='instruction_config'):
            verify_settings(manifest, config)


def test_default_manifest_remains_byte_for_byte_compatible(tree):
    manifest = discover(tree[2])
    assert 'runtime_overrides' not in manifest
    assert runtime_overrides(manifest) == runtime_overrides(None) == []
    assert readback(manifest, tree[2])['manifest'] == manifest


@pytest.mark.parametrize('limit', [True, False, 0, -1, 32767, 131072.0, '131072', MAX_RUNTIME_DOC_BYTES + 1])
def test_limit_override_is_bounded_and_cannot_lower_configured_limit(tree, limit):
    with pytest.raises(Rejected, match='instruction_limit'):
        discover(tree[2], project_doc_max_bytes=limit)


def test_global_and_nested_configuration_remain_pinned_under_override(tree, tmp_path):
    home, repo, source = tree
    manifest = discover(source, project_doc_max_bytes=131072)
    config = home / '.codex/config.toml'
    original = config.read_text()
    config.write_text('project_doc_max_bytes=65536\n' + original)
    with pytest.raises(Rejected, match='instruction_drift'):
        validate(manifest, source, tmp_path / 'state')
    config.write_text(original)
    (source / '.codex').mkdir()
    (source / '.codex/config.toml').write_text('project_doc_max_bytes=65536\n')
    with pytest.raises(Rejected, match='instruction_drift'):
        validate(manifest, source, tmp_path / 'state')


@pytest.mark.parametrize('overrides', [None, {}, {'project_root_markers': ['.git']},
    {'project_doc_max_bytes': 131072, 'approval_policy': 'never'}, {'project_doc_max_bytes': True},
    {'project_doc_max_bytes': 0}, {'project_doc_max_bytes': MAX_RUNTIME_DOC_BYTES + 1},
    {'project_doc_max_bytes': '131072'}, {'project_doc_max_bytes': 65536}])
def test_manifest_override_cannot_smuggle_settings_or_disagree_with_pins(tree, overrides):
    manifest = discover(tree[2], project_doc_max_bytes=131072)
    manifest['runtime_overrides'] = overrides
    for action in (lambda: runtime_overrides(manifest), lambda: readback(manifest, tree[2]),
                   lambda: verify_settings(manifest, manifest['settings'])):
        with pytest.raises(Rejected, match='instruction_limit'):
            action()


def test_declared_registration_and_limit_compose_without_allowing_other_drift(tree, tmp_path):
    home, repo, source = tree
    manifest = discover(source, allow_project_registration=True, project_doc_max_bytes=131072)
    validate(manifest, source, tmp_path / 'state')
    register_project(home, repo)
    validate(manifest, source, tmp_path / 'state', after_startup=True)
    with pytest.raises(Rejected, match='instruction_drift'):
        validate(manifest, source, tmp_path / 'state')
    (repo / 'AGENTS.md').write_text('changed guidance')
    with pytest.raises(Rejected, match='instruction_drift'):
        validate(manifest, source, tmp_path / 'state', after_startup=True)


def test_cli_limit_is_inspection_only_and_preserves_truncation_failure(tree, capsys):
    home, repo, source = tree
    (repo / 'AGENTS.md').write_text('a' * 40000)
    before = (home / '.codex/config.toml').read_bytes()
    assert main(['instruction-manifest', '--source', str(source)]) == 2
    assert 'instruction_truncation' in capsys.readouterr().err
    assert main(['instruction-manifest', '--source', str(source), '--project-doc-max-bytes', '131072']) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest == discover(source, project_doc_max_bytes=131072)
    assert (home / '.codex/config.toml').read_bytes() == before


@pytest.mark.parametrize('effective_limit', [131072, 32768])
def test_runtime_applies_exact_limit_before_pre_turn_verification(tmp_path, monkeypatch, effective_limit):
    import test_aos_codex_runtime as fixtures
    from test_aos_codex_runtime import claim
    monkeypatch.setattr(fixtures, 'discover', lambda source: discover(source, project_doc_max_bytes=131072))
    fixture = fixtures.prepared.__wrapped__(tmp_path, monkeypatch)
    p = next(fixture)
    try:
        def groups(**kwargs):
            return {c.proc.pid: {'parent': 1, 'group': c.proc.pid, 'started': 'saved-birth'}
                    for c in p.backend.clients if not c.proc.exited}
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.process_groups', groups)
        monkeypatch.setattr('agentic_tools.aos.codex_runtime.processes',
                            lambda **kwargs: {pid: (row['parent'], row['started']) for pid, row in groups().items()})
        for assessor in (False, True):
            overrides, profile = p.runtime._profile(p.recipe, assessor)
            assert overrides.count('project_doc_max_bytes=131072') == 1
            assert profile['filesystem'][str(Path.home() / '.codex')] == 'deny'
        original_factory = p.runtime.factory
        def factory(*args):
            client = original_factory(*args)
            client.config['project_doc_max_bytes'] = effective_limit
            return client
        p.runtime.factory = factory
        assignment = claim(p)
        result = p.runtime.start(assignment)
        if effective_limit == 131072:
            assert result['state'] == 'active'
            assert p.backend.clients[-1].config['project_doc_max_bytes'] == 131072
            assert p.backend.count('thread/start') == p.backend.count('turn/start') == 1
            p.runtime.cancel(assignment['attempt_id'])
        else:
            assert result['state'] == 'startup_failed'
            assert result['result']['startup_outcome']['kind'] == 'before_thread_request'
            assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
    finally:
        fixture.close()
