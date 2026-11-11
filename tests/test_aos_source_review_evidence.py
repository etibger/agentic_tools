"""Real disposable Git/worktree fixtures prove exact baseline review transport."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from agentic_tools.aos import source_review_evidence as evidence
from agentic_tools.aos.codex_runtime import candidate, inventory
from agentic_tools.aos.store import Rejected, canonical, digest


def git(root, *arguments):
    # All writes and commits below belong solely to pytest's disposable fixture.
    return subprocess.run([
        'git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
        '-c', 'user.name=AOS Fixture', '-c', 'user.email=aos-fixture@example.invalid',
        '-c', 'commit.gpgsign=false', *arguments,
    ], cwd=root, check=True, capture_output=True).stdout


def decode(content):
    assert content['encoding'] == 'base64'
    value = base64.b64decode(content['data'], validate=True)
    assert hashlib.sha256(value).hexdigest() == content['sha256']
    return value


@pytest.fixture
def frozen_patch(tmp_path):
    root = tmp_path.resolve()
    repository = root / 'repo'
    repository.mkdir()
    git(repository, 'init', '-q')
    original = {
        'modified.txt': b'original first line\noriginal second line\n',
        'unchanged.txt': b'unchanged\n',
        'removed.txt': b'removed content\n',
        'binary.bin': b'\x00\xffBASE\x01\n',
        'script.sh': b'#!/bin/sh\nexit 0\n',
    }
    for name, content in original.items():
        (repository / name).write_bytes(content)
    (repository / 'script.sh').chmod(0o755)
    (repository / 'alias').symlink_to('unchanged.txt')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'synthetic baseline')
    revision = git(repository, 'rev-parse', 'HEAD').decode().strip()
    source = root / 'source'
    git(repository, 'worktree', 'add', '--detach', str(source), revision)
    outputs = root / 'outputs'
    outputs.mkdir()
    recipe = {'source': str(source), 'outputs': str(outputs), 'execution_policy': 'isolated-checkout-v1'}
    baseline = inventory(source, recipe=recipe)
    (source / 'modified.txt').write_bytes(b'candidate first line\noriginal second line\n')
    (source / 'binary.bin').write_bytes(b'\x00\xfeCANDIDATE\x02\n')
    (source / 'added.txt').write_bytes(b'new untracked candidate file\n')
    (source / 'removed.txt').unlink()
    (source / 'alias').unlink()
    (source / 'alias').symlink_to('modified.txt')
    (source / 'script.sh').chmod(0o644)
    frozen = candidate(source, recipe=recipe)
    protected = root / 'evidence'
    protected.mkdir(mode=0o700)
    c = SimpleNamespace(root=root, repository=repository, source=source, outputs=outputs,
                        original=original, baseline=baseline, frozen=frozen, recipe=recipe,
                        revision=revision, destination=protected / 'source-review.json')
    c.build_kwargs = {
        'work_id': 'fixture-work', 'baseline_assignment_id': 'fixture-implementation',
        'git_base': revision, 'destination': c.destination, 'recipe': recipe,
        'writable_roots': (source, outputs),
    }
    c.validate_kwargs = {
        'source': source, 'baseline_identity': digest(baseline), 'candidate': frozen,
        'work_id': 'fixture-work', 'recipe': recipe, 'writable_roots': (source, outputs),
    }
    return c


def build(c):
    return evidence.build(c.source, c.baseline, c.frozen, **c.build_kwargs)


def rewrite(c, descriptor, mutate, *, repin=False):
    path = Path(descriptor['path'])
    value = json.loads(path.read_text())
    mutate(value)
    path.chmod(0o600)
    raw = (canonical(value) + '\n').encode()
    path.write_bytes(raw)
    path.chmod(0o400)
    if repin:
        descriptor = {**descriptor, 'sha256': hashlib.sha256(raw).hexdigest()}
    return descriptor


def test_exact_baseline_text_binary_new_deleted_symlink_and_mode_evidence(frozen_patch):
    c = frozen_patch
    before = git(c.source, 'status', '--porcelain=v1', '-z')
    descriptor = build(c)
    value = evidence.validate(descriptor, **c.validate_kwargs)
    assert descriptor['format'] == 'aos-source-review-descriptor-v1'
    assert descriptor['baseline_identity'] == digest(c.baseline)
    assert descriptor['candidate_identity'] == c.frozen['identity']
    assert descriptor['git_base'] == c.revision
    assert value['format'] == 'aos-source-review-evidence-v1'
    assert value['baseline_inventory'] == c.baseline
    assert value['candidate_inventory'] == c.frozen['inventory']
    changes = {entry['path']: entry for entry in value['changes']}
    assert set(changes) == {'modified.txt', 'binary.bin', 'added.txt', 'removed.txt', 'alias', 'script.sh'}
    assert changes['added.txt']['kind'] == 'added' and changes['added.txt']['old'] is None
    assert decode(changes['added.txt']['new']) == (c.source / 'added.txt').read_bytes()
    assert changes['removed.txt']['kind'] == 'deleted' and changes['removed.txt']['new'] is None
    assert decode(changes['removed.txt']['old']) == c.original['removed.txt']
    assert decode(changes['modified.txt']['old']) == c.original['modified.txt']
    assert decode(changes['modified.txt']['new']) == (c.source / 'modified.txt').read_bytes()
    assert decode(changes['binary.bin']['old']) == c.original['binary.bin']
    assert decode(changes['binary.bin']['new']) == (c.source / 'binary.bin').read_bytes()
    assert changes['alias']['old']['kind'] == changes['alias']['new']['kind'] == 'symlink'
    assert decode(changes['alias']['old']) == b'unchanged.txt'
    assert decode(changes['alias']['new']) == b'modified.txt'
    assert changes['script.sh']['old']['mode'] == '100755'
    assert changes['script.sh']['new']['mode'] == '100644'
    assert value['candidate_modes']['script.sh'] == '100644'
    assert 'GIT binary patch' in value['git_patch']
    assert 'original first line' in value['git_patch'] and 'candidate first line' in value['git_patch']
    assert git(c.source, 'status', '--porcelain=v1', '-z') == before
    assert inventory(c.source, recipe=c.recipe) == c.frozen['inventory']


def test_non_utf8_text_patch_round_trips_and_rejects_tampering(frozen_patch):
    c = frozen_patch
    content = b'caf\xe9\n'
    (c.source / 'added.txt').write_bytes(content)
    c.frozen = candidate(c.source, recipe=c.recipe)
    c.validate_kwargs['candidate'] = c.frozen
    descriptor = build(c)
    value = evidence.validate(descriptor, **c.validate_kwargs)
    change = next(entry for entry in value['changes'] if entry['path'] == 'added.txt')
    assert decode(change['new']) == content
    assert 'text' not in change['new']
    patch = value['git_patch']
    assert set(patch) == {'encoding', 'data', 'sha256'}
    assert patch['encoding'] == 'base64'
    raw_patch = base64.b64decode(patch['data'], validate=True)
    assert hashlib.sha256(raw_patch).hexdigest() == patch['sha256']
    assert b'+caf\xe9\n' in raw_patch
    assert b'GIT binary patch' in raw_patch
    bound = evidence.seed(descriptor, c.outputs, **c.validate_kwargs)
    assert evidence.verify_copy(bound, c.outputs, **c.validate_kwargs) == value
    descriptor = rewrite(c, descriptor,
        lambda record: record['git_patch'].update(data=base64.b64encode(b'altered patch').decode('ascii')),
        repin=True)
    with pytest.raises(Rejected, match='Patch differs'):
        evidence.validate(descriptor, **c.validate_kwargs)


def test_original_commit_not_index_is_the_baseline(frozen_patch):
    c = frozen_patch
    (c.source / 'modified.txt').write_bytes(b'staged intermediate version\n')
    git(c.source, 'add', 'modified.txt')
    (c.source / 'modified.txt').write_bytes(b'final unstaged candidate version\n')
    c.frozen = candidate(c.source, recipe=c.recipe)
    c.validate_kwargs['candidate'] = c.frozen
    before = git(c.source, 'diff', '--cached', '--binary', '--no-ext-diff', c.revision)
    descriptor = build(c)
    value = evidence.validate(descriptor, **c.validate_kwargs)
    changed = next(x for x in value['changes'] if x['path'] == 'modified.txt')
    assert decode(changed['old']) == c.original['modified.txt']
    assert decode(changed['new']) == b'final unstaged candidate version\n'
    assert 'staged intermediate version' not in value['git_patch']
    assert git(c.source, 'diff', '--cached', '--binary', '--no-ext-diff', c.revision) == before


def test_wrong_git_base_cannot_replace_approved_baseline(frozen_patch):
    c = frozen_patch
    (c.repository / 'modified.txt').write_bytes(b'different committed baseline\n')
    git(c.repository, 'add', 'modified.txt')
    git(c.repository, 'commit', '-qm', 'another synthetic baseline')
    other = git(c.repository, 'rev-parse', 'HEAD').decode().strip()
    with pytest.raises(Rejected):
        evidence.build(c.source, c.baseline, c.frozen, **{**c.build_kwargs, 'git_base': other})
    assert not c.destination.exists()


@pytest.mark.parametrize('field,value', [
    ('sha256', '0' * 64), ('work_id', 'other-work'),
    ('baseline_identity', 'sha256:' + '1' * 64), ('candidate_identity', 'sha256:' + '2' * 64),
    ('git_base', '0' * 40),
])
def test_descriptor_identity_changes_rejected(frozen_patch, field, value):
    c = frozen_patch
    descriptor = {**build(c), field: value}
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


@pytest.mark.parametrize('field', ['baseline_inventory', 'candidate_inventory'])
def test_rehashed_inventories_cannot_replace_approved_identities(frozen_patch, field):
    c = frozen_patch
    descriptor = build(c)
    descriptor = rewrite(c, descriptor, lambda value: value[field].update({'modified.txt': '0' * 64}), repin=True)
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


def test_modified_old_bytes_rejected_even_with_rehashed_artifact(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    def damage(value):
        changed = next(x for x in value['changes'] if x['path'] == 'modified.txt')
        fake = b'fabricated old content\n'
        changed['old'].update(data=base64.b64encode(fake).decode(), sha256=hashlib.sha256(fake).hexdigest(), text=fake.decode())
    descriptor = rewrite(c, descriptor, damage, repin=True)
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


def test_raw_artifact_mutation_rejected(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    rewrite(c, descriptor, lambda value: value.update(git_patch='untrusted replacement'))
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


@pytest.mark.parametrize('change', ['contents', 'mode'])
def test_candidate_drift_after_build_rejected(frozen_patch, change):
    c = frozen_patch
    descriptor = build(c)
    if change == 'contents':
        (c.source / 'modified.txt').write_bytes(b'new unapproved contents\n')
    else:
        (c.source / 'script.sh').chmod(0o755)
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


def test_evidence_cannot_live_in_worker_writable_root(frozen_patch):
    c = frozen_patch
    destination = c.outputs / 'worker-writable.json'
    with pytest.raises(Rejected):
        evidence.build(c.source, c.baseline, c.frozen, **{**c.build_kwargs, 'destination': destination})
    assert not destination.exists()


def test_symlink_descriptor_path_rejected(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    alias = c.destination.parent / 'alias.json'
    alias.symlink_to(c.destination.name)
    with pytest.raises(Rejected):
        evidence.validate({**descriptor, 'path': str(alias)}, **c.validate_kwargs)


def test_seed_is_exact_attempt_copy_and_tampering_rejected(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    run_outputs = c.outputs / 'assessment-fixture'
    run_outputs.mkdir()
    bound = evidence.seed(descriptor, run_outputs, **c.validate_kwargs)
    assert Path(bound['path']).parent == run_outputs
    assert bound['descriptor'] == descriptor
    assert hashlib.sha256(Path(bound['path']).read_bytes()).hexdigest() == bound['sha256']
    verified = evidence.verify_copy(bound, run_outputs, **c.validate_kwargs)
    assert verified['candidate_inventory'] == c.frozen['inventory']
    path = Path(bound['path'])
    path.chmod(0o600)
    path.write_text('{}\n')
    with pytest.raises(Rejected):
        evidence.verify_copy(bound, run_outputs, **c.validate_kwargs)


def test_copy_cannot_be_redirected_to_another_attempt(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    run_outputs = c.outputs / 'assessment-fixture'
    other_outputs = c.outputs / 'other-assessment'
    run_outputs.mkdir(); other_outputs.mkdir()
    bound = evidence.seed(descriptor, run_outputs, **c.validate_kwargs)
    with pytest.raises(Rejected):
        evidence.verify_copy(bound, other_outputs, **c.validate_kwargs)


def test_rehashed_patch_must_describe_verified_exact_bytes(frozen_patch):
    c = frozen_patch
    descriptor = build(c)
    descriptor = rewrite(c, descriptor, lambda value: value.update(
        git_patch='diff --git a/modified.txt b/modified.txt\nNo actual source changes\n'), repin=True)
    with pytest.raises(Rejected):
        evidence.validate(descriptor, **c.validate_kwargs)


def test_seed_rechecks_second_read_before_creating_attempt_copy(frozen_patch, monkeypatch):
    c = frozen_patch
    descriptor = build(c)
    run_outputs = c.outputs / 'assessment-fixture'
    run_outputs.mkdir()
    original = evidence._artifact_bytes
    calls = []

    def changed_second_read(path, roots=()):
        raw = original(path, roots)
        calls.append(str(path))
        return raw + b'\n' if len(calls) == 2 else raw

    monkeypatch.setattr(evidence, '_artifact_bytes', changed_second_read)
    with pytest.raises(Rejected):
        evidence.seed(descriptor, run_outputs, **c.validate_kwargs)
    assert len(calls) == 2
    assert not (run_outputs / evidence.FILENAME).exists()


@pytest.mark.parametrize('stage', ['build', 'validate'])
def test_aggregate_baseline_limit_rejects_individually_small_blobs(frozen_patch, monkeypatch, stage):
    c = frozen_patch
    descriptor = build(c) if stage == 'validate' else None
    largest = max(len(raw) for raw in c.original.values())
    aggregate = sum(len(raw) for raw in c.original.values()) + len(b'unchanged.txt')
    assert largest < aggregate
    monkeypatch.setattr(evidence, 'MAX_TREE_BYTES', largest)
    with pytest.raises(Rejected):
        if stage == 'validate':
            evidence.validate(descriptor, **c.validate_kwargs)
        else:
            build(c)
    if stage == 'build':
        assert not c.destination.exists()


@pytest.mark.parametrize('stage', ['build', 'validate'])
def test_aggregate_candidate_limit_counts_new_untracked_content(frozen_patch, monkeypatch, stage):
    c = frozen_patch
    (c.source / 'added.txt').write_bytes(b'x' * 1024)
    (c.source / 'other-added.txt').write_bytes(b'y' * 1024)
    c.frozen = candidate(c.source, recipe=c.recipe)
    c.validate_kwargs['candidate'] = c.frozen
    limit = 1500
    assert sum(len(raw) for raw in c.original.values()) + len(b'unchanged.txt') < limit
    sizes = [(c.source / name).lstat().st_size for name in c.frozen['inventory']]
    assert max(sizes) < limit < sum(sizes)
    descriptor = build(c) if stage == 'validate' else None
    monkeypatch.setattr(evidence, 'MAX_TREE_BYTES', limit)
    with pytest.raises(Rejected):
        if stage == 'validate':
            evidence.validate(descriptor, **c.validate_kwargs)
        else:
            build(c)
    if stage == 'build':
        assert not c.destination.exists()
