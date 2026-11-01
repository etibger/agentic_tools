from pathlib import Path

import pytest

from agentic_tools.aos.instructions import discover, validate, verify_settings
from agentic_tools.aos.store import Rejected
from test_aos_codex_runtime import prepared, claim
from test_aos_startup_recovery import rejected


@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: home)
    repo = tmp_path / 'repo'
    source = repo / 'export/source'
    source.mkdir(parents=True)
    (repo / '.git').mkdir()
    target = home / 'policy.md'
    target.write_text('global guidance')
    (home / '.codex/AGENTS.md').symlink_to(target)
    (repo / 'AGENTS.md').write_text('enclosing repository')
    (source.parent / 'AGENTS.override.md').write_text('export override')
    (source.parent / 'AGENTS.md').write_text('superseded')
    (source / 'GUIDE.md').write_text('local fallback')
    (home / '.codex/config.toml').write_text('project_doc_fallback_filenames=["GUIDE.md"]\n')
    return home, repo, source


def test_nested_export_order_alias_and_exact_read_boundary(tree, tmp_path):
    home, repo, source = tree
    manifest = discover(source)
    assert [p['path'] for p in manifest['chain']] == list(map(str, [
        home / '.codex/AGENTS.md', repo / 'AGENTS.md', source.parent / 'AGENTS.override.md', source / 'GUIDE.md']))
    assert manifest['chain'][0]['resolved'] == str(home / 'policy.md')
    assert manifest['chain'][0]['aliases']
    reads = validate(manifest, source, tmp_path / 'state')
    assert str(home / 'policy.md') in reads
    assert str(home / '.codex') not in reads


@pytest.mark.parametrize('mutation', ['content', 'missing', 'new_override', 'alias', 'root', 'config', 'truncation'])
def test_changed_instruction_route_is_never_silently_omitted(tree, tmp_path, mutation):
    home, repo, source = tree
    manifest = discover(source)
    if mutation == 'content':
        (home / 'policy.md').write_text('new guidance')
    elif mutation == 'missing':
        (repo / 'AGENTS.md').unlink()
    elif mutation == 'new_override':
        (source / 'AGENTS.override.md').write_text('new precedence')
    elif mutation == 'alias':
        (home / 'other.md').write_text('global guidance')
        (home / '.codex/AGENTS.md').unlink()
        (home / '.codex/AGENTS.md').symlink_to(home / 'other.md')
    elif mutation == 'root':
        (source / '.git').mkdir()
    else:
        (home / '.codex/config.toml').write_text('project_doc_max_bytes=' + ('1' if mutation == 'truncation' else '65536'))
    with pytest.raises(Rejected):
        validate(manifest, source, tmp_path / 'state')


def test_broken_or_denied_instruction_is_not_missing(tree, monkeypatch):
    home, repo, source = tree
    (home / 'policy.md').unlink()
    with pytest.raises(Rejected, match='cannot be read'):
        discover(source)
    (home / 'policy.md').write_text('global guidance')
    read = Path.read_bytes
    def denied(path):
        if path == home / 'policy.md':
            raise PermissionError('denied')
        return read(path)
    monkeypatch.setattr(Path, 'read_bytes', denied)
    with pytest.raises(Rejected, match='cannot be read'):
        discover(source)


def test_instruction_alias_cannot_open_credentials_or_state(tree, tmp_path):
    home, repo, source = tree
    (home / '.codex/auth.json').write_text('fixture only')
    (home / '.codex/AGENTS.md').unlink()
    (home / '.codex/AGENTS.md').symlink_to(home / '.codex/auth.json')
    with pytest.raises(Rejected, match='protected'):
        validate(discover(source), source, tmp_path / 'state')


def test_intermediate_protected_alias_is_rejected(tree, tmp_path):
    home, repo, source = tree
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'alias').symlink_to(home / 'policy.md')
    (home / '.codex/AGENTS.md').unlink()
    (home / '.codex/AGENTS.md').symlink_to(state / 'alias')
    with pytest.raises(Rejected, match='protected'):
        validate(discover(source), source, state)


def test_effective_discovery_settings_must_match(tree):
    manifest = discover(tree[2])
    with pytest.raises(Rejected, match='discovery'):
        verify_settings(manifest, {})
    verify_settings(manifest, manifest['settings'])


def test_missing_manifest_cannot_reuse_approved_recipe(prepared):
    p = prepared
    p.recipe.pop('instruction_manifest')
    with pytest.raises(Rejected, match='exact approved'):
        p.runtime.start(claim(p))
    assert p.backend.count('thread/start') == 0


def test_writer_and_assessor_instruction_files_are_read_only(prepared):
    p = prepared
    for assessment in (False, True):
        _, profile = p.runtime._profile(p.recipe, assessment)
        assert profile['filesystem'][str(p.source)] == ('read' if assessment else 'write')
        for instruction in p.recipe['instruction_manifest']['chain']:
            assert profile['filesystem'][instruction['path']] == 'read'
            assert profile['filesystem'][instruction['resolved']] == 'read'
        assert profile['filesystem'][str(Path.home() / '.codex')] == 'deny'
        assert profile['filesystem'][':root'] == 'deny'


def test_startup_only_qualification_never_starts_or_restarts_model(rejected):
    p = rejected
    p.runtime.factory = p.backend.factory  # Actual success, scripted transport.
    assignment = claim(p)
    first = p.runtime.start(assignment, preflight_only=True)
    assert first['state'] == 'startup_qualified'
    assert first['result']['effects_reconciled']
    assert first['result']['status'] == 'qualified'
    assert p.runtime.start(assignment) == first
    assert p.runtime.cancel(assignment['attempt_id'])['effects_reconciled']
    assert p.backend.count('thread/start') == 1 and p.backend.count('turn/start') == 0
    assert p.runtime.budget_receipt()['attempts_reserved'] == 1
