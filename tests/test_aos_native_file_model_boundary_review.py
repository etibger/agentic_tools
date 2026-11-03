"""Native file permissions cannot leak through model read roots or path aliases."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from codex_cli_bin import bundled_codex_path

from agentic_tools.aos import instructions, native_sandbox
from agentic_tools.aos.codex_runtime import CodexRuntime, PROFILE, clean_environment
from agentic_tools.aos.store import Rejected
from test_aos_native_owned_root_model_review import effective_config


GRANTS = ['parent', 'alias-parent', 'exact-file', 'alias-exact-file', 'instruction']
pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native/model physical file boundary')


def physical_alias(path):
    alias = Path('/System/Volumes/Data') / path.relative_to('/')
    if not alias.exists() or not os.path.samefile(path, alias):
        pytest.skip('Fixture has no physical Data-volume alias')
    return alias


def file_fixture(tmp_path, grant, monkeypatch, *, native_file=None):
    source, outputs, state, parent = [tmp_path / name for name in ('source', 'outputs', 'state', 'native-data')]
    for path in (source, outputs, state, parent):
        path.mkdir()
    selected, sibling = native_file or parent / 'selected-native-data', parent / 'readable-neighbor'
    if native_file is None:
        selected.write_text('native-only fixture data\n')
    selected_before = selected.read_bytes()
    sibling.write_text('permitted sibling\n')
    (source / 'immutable.py').write_text('immutable source\n')
    (source / 'mutable.py').write_text('mutable source\n')
    (outputs / 'control').write_text('permitted output\n')
    alias = physical_alias(selected)
    direct_link, alias_link = source / 'native-file-link', source / 'native-alias-link'
    direct_link.symlink_to(selected)
    alias_link.symlink_to(alias)
    selected_grant = None if grant in ('instruction', 'minimal') else (
        alias if grant == 'alias-exact-file' else selected if grant == 'exact-file' else (
            physical_alias(parent) if grant == 'alias-parent' else parent))
    read_roots = {str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()), str(sibling)}
    if selected_grant is not None:
        read_roots.add(str(selected_grant))
    recipe = {
        'source': str(source), 'outputs': str(outputs),
        'read_roots': sorted(read_roots),
        'mutable': ['mutable.py'],
        'immutable': {'immutable.py': hashlib.sha256((source / 'immutable.py').read_bytes()).hexdigest()},
        'probe_interpreter': str(Path(sys._base_executable).resolve()),
        'native_sandbox': {'version': native_sandbox.VERSION, 'file_read_paths': [
            {'path': str(selected), 'sha256': hashlib.sha256(selected_before).hexdigest()}]},
    }
    if grant == 'instruction':
        # Exercise the final profile ordering only. This does not claim that
        # a contradictory instruction/native-file manifest passes admission.
        manifest = {'chain': [], 'support': [dict(recipe['native_sandbox']['file_read_paths'][0])],
                    'configs': []}
        recipe['instruction_manifest'] = manifest
        def inherited_read(value, source_root, state_root, *, after_startup=False):
            assert value == manifest and source_root == source and state_root == state
            return [str(selected)]
        monkeypatch.setattr(instructions, 'validate', inherited_read)
        monkeypatch.setattr(instructions, 'runtime_overrides', lambda value: [])
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime.directory = state
    return SimpleNamespace(source=source, outputs=outputs, state=state, parent=parent,
                           selected=selected, sibling=sibling, alias=alias, direct_link=direct_link,
                           alias_link=alias_link, grant=grant, selected_grant=selected_grant,
                           selected_before=selected_before, system_native_file=native_file is not None,
                           recipe=recipe, runtime=runtime)


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
@pytest.mark.parametrize('grant', GRANTS)
def test_model_native_file_exact_denies_survive_read_roots_and_config_readback(tmp_path, monkeypatch, assessment, grant):
    p = file_fixture(tmp_path, grant, monkeypatch)
    _, model = p.runtime._profile(p.recipe, assessment=assessment)
    _, native = p.runtime._profile(p.recipe, assessment=True, native=True)
    denied = {str(p.selected)}
    if grant in ('exact-file', 'alias-exact-file'):
        denied.add(str(p.selected_grant))
    elif grant in ('parent', 'alias-parent'):
        assert model['filesystem'][str(p.selected_grant)] == 'read'
    for path in denied:
        assert model['filesystem'][path] == 'deny'
    assert model['filesystem'].get(str(p.parent)) != 'deny'
    assert model['filesystem'][str(p.sibling)] == 'read'
    assert model['filesystem'][str(p.source)] == ('read' if assessment else 'write')
    assert model['filesystem'][str(p.outputs)] == 'write'
    # The native route still receives the same original read declarations.
    for path in p.recipe['read_roots']:
        assert native['filesystem'][path] == 'read'
    assert native['filesystem'].get(str(p.selected)) != 'deny'
    if grant == 'instruction':
        assert str(p.selected) not in p.recipe['read_roots']
        assert str(p.parent) not in p.recipe['read_roots']
        assert native['filesystem'][str(p.selected)] == 'read'
    config = effective_config(model)
    p.runtime._verify_config(config, model)
    for path in denied:
        for replacement in (None, 'read', 'write'):
            tampered = copy.deepcopy(config)
            filesystem = tampered['permissions'][PROFILE]['filesystem']
            if replacement is None:
                del filesystem[path]
            else:
                filesystem[path] = replacement
            with pytest.raises(Rejected, match='Effective filesystem'):
                p.runtime._verify_config(tampered, model)


PROGRAM = '''import json,os,sys
from pathlib import Path
p=json.loads(sys.argv[1]);observed={}
def opened(path,flags):
 fd=os.open(path,flags)
 try:return os.read(fd,256).decode() if flags==os.O_RDONLY else None
 finally:os.close(fd)
def attempt(name,action):
 try:observed[name]={'allowed':True,'value':action()}
 except OSError as error:
  observed[name]={'allowed':False,'error_type':type(error).__name__,'errno':error.errno}
# Deliberately follow links. An ELOOP/O_NOFOLLOW result is not access denial.
for label,path in p['selected_paths'].items():
 attempt(label+'_read',lambda path=path:opened(path,os.O_RDONLY))
 if p['selected_writes']:attempt(label+'_write',lambda path=path:opened(path,os.O_WRONLY))
attempt('sibling_read',lambda:opened(p['sibling'],os.O_RDONLY))
attempt('sibling_write',lambda:opened(p['sibling'],os.O_WRONLY))
attempt('source_read',lambda:opened(p['immutable'],os.O_RDONLY))
attempt('immutable_write',lambda:opened(p['immutable'],os.O_WRONLY))
attempt('mutable_write',lambda:opened(p['mutable'],os.O_WRONLY))
attempt('output_read',lambda:opened(p['output_control'],os.O_RDONLY))
def output_write():
 path=Path(p['output_created'])
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:os.write(fd,b'bounded output control')
 finally:os.close(fd)
 try:return path.read_text()
 finally:path.unlink()
attempt('output_write',output_write)
print(json.dumps(observed,sort_keys=True))
'''


def permission_run(p, tmp_path, *, native, assessment):
    overrides, profile = p.runtime._profile(p.recipe, assessment=assessment, native=native)
    payload = {'selected_paths': {'canonical': str(p.selected), 'physical_alias': str(p.alias),
                                 'symlink': str(p.direct_link), 'alias_symlink': str(p.alias_link)},
               'selected_writes': not p.system_native_file,
               'sibling': str(p.sibling), 'immutable': str(p.source / 'immutable.py'),
               'mutable': str(p.source / 'mutable.py'), 'output_control': str(p.outputs / 'control'),
               'output_created': str(p.outputs / 'created-control')}
    if p.system_native_file:
        payload['selected_paths'].update(lexical=str(p.lexical), lexical_symlink=str(p.lexical_link))
    if native:
        raw = native_sandbox.render(p.recipe, profile['filesystem'], preexisting={},
                                    protected=(p.state, Path.home() / '.codex', p.source))
        saved = tmp_path / 'native-file-profile.sb'
        saved.write_text(raw)
        argv = ['/usr/bin/sandbox-exec', '-f', str(saved)]
    else:
        argv = [str(bundled_codex_path())]
        for override in overrides:
            argv.extend(['-c', override])
        argv.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', str(p.source)])
    argv.extend([p.recipe['probe_interpreter'], '-I', '-S', '-B', '-c', PROGRAM, json.dumps(payload)])
    run = subprocess.run(argv, cwd=p.source, capture_output=True, text=True,
                         env=clean_environment(), timeout=20)
    observed = json.loads(run.stdout) if run.returncode == 0 else None
    evidence = {'lane': 'native' if native else 'model', 'assessment': assessment, 'grant': p.grant,
                'profile': profile, 'exit_code': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
                'observed': observed, 'model_calls': 0, 'shared_journals_used': False,
                'runtime_source_sha256': hashlib.sha256(Path(sys.modules[CodexRuntime.__module__].__file__).read_bytes()).hexdigest(),
                'test_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (tmp_path / 'native-file-model-boundary-proof.json').write_text(json.dumps(evidence, indent=2) + '\n')
    assert run.returncode == 0, run.stderr
    assert observed
    return observed


def assert_denied(observed, names):
    for name in names:
        assert observed[name]['allowed'] is False, (name, observed[name])
        assert observed[name]['error_type'] == 'PermissionError', (name, observed[name])
        assert observed[name]['errno'] in (1, 13), (name, observed[name])


def assert_controls(p, observed, *, writer):
    for name, value in (('sibling_read', 'permitted sibling\n'), ('source_read', 'immutable source\n'),
                        ('output_read', 'permitted output\n'), ('output_write', 'bounded output control')):
        assert observed[name] == {'allowed': True, 'value': value}
    assert_denied(observed, ['sibling_write', 'immutable_write'])
    if writer:
        assert observed['mutable_write'] == {'allowed': True, 'value': None}
    else:
        assert_denied(observed, ['mutable_write'])
    assert p.selected.read_bytes() == p.selected_before
    assert p.sibling.read_text() == 'permitted sibling\n'
    assert (p.source / 'immutable.py').read_text() == 'immutable source\n'
    assert (p.source / 'mutable.py').read_text() == 'mutable source\n'
    assert not (p.outputs / 'created-control').exists()


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
@pytest.mark.parametrize('grant', GRANTS)
def test_actual_model_file_denial_follows_symlinks_and_physical_aliases(tmp_path, monkeypatch, assessment, grant):
    p = file_fixture(tmp_path, grant, monkeypatch)
    observed = permission_run(p, tmp_path, native=False, assessment=assessment)
    assert_denied(observed, [label + '_' + operation for label in
                            ('canonical', 'physical_alias', 'symlink', 'alias_symlink')
                            for operation in ('read', 'write')])
    assert_controls(p, observed, writer=not assessment)


@pytest.mark.parametrize('grant', GRANTS)
def test_actual_native_pinned_file_reads_stay_available(tmp_path, monkeypatch, grant):
    p = file_fixture(tmp_path, grant, monkeypatch)
    observed = permission_run(p, tmp_path, native=True, assessment=True)
    for label in ('canonical', 'physical_alias', 'symlink', 'alias_symlink'):
        assert observed[label + '_read'] == {'allowed': True, 'value': 'native-only fixture data\n'}
        assert_denied(observed, [label + '_write'])
    assert_controls(p, observed, writer=False)


@pytest.mark.parametrize('assessment', [False, True], ids=['writer', 'assessor'])
def test_actual_startup_rejects_unsupported_native_only_mime_restriction(tmp_path, monkeypatch, assessment):
    from agentic_tools.aos import native_processes
    from agentic_tools.aos.codex_runtime import candidate
    from agentic_tools.aos.worker import Worker
    from test_aos_check_adapters import prepared_checks
    from test_aos_lifecycle import implementation

    selected, lexical = Path('/private/etc/apache2/mime.types'), Path('/etc/apache2/mime.types')
    if not selected.is_file() or not lexical.is_file():
        pytest.skip('Installed public Apache MIME data is unavailable')
    assert os.path.samefile(selected, lexical)
    selected_before = selected.read_bytes()
    pinned_file = {'path': str(selected), 'sha256': hashlib.sha256(selected_before).hexdigest()}

    def native_only_recipe(recipe):
        recipe.update(native_process_ownership=native_processes.BACKEND,
                      native_sandbox={'version': native_sandbox.VERSION, 'file_read_paths': [pinned_file]})

    # Approval, lifecycle, and metadata transport are explicitly synthetic and
    # confined to this fixture. Only the pinned CLI permission probe is real.
    p = prepared_checks(tmp_path, monkeypatch, recipe_update=native_only_recipe)
    monkeypatch.setattr(p.runtime, '_probe_sandbox', CodexRuntime._probe_sandbox.__get__(p.runtime))
    actual_run, binary, probes = subprocess.run, str(bundled_codex_path()), []

    def capture_probe(argv, *args, **kwargs):
        proof = None
        if argv[0] == binary and 'sandbox' in argv:
            proof = {'argv': list(argv), 'timeout_seconds': kwargs.get('timeout')}
            probes.append(proof)
        try:
            run = actual_run(argv, *args, **kwargs)
        except subprocess.SubprocessError as error:
            if proof is not None:
                proof['failure'] = type(error).__name__ + ': ' + str(error)
            raise
        if proof is not None:
            proof.update(exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr)
            try:
                proof['checks'] = json.loads(run.stdout)
            except ValueError:
                proof['checks'] = None
        return run

    monkeypatch.setattr(subprocess, 'run', capture_probe)
    try:
        with Worker(p.lifecycle, p.runtime) as worker:
            assignment = p.lifecycle.claim(p.lifecycle.assignments()[0]['assignment_id'], worker.epoch)
            if assessment:
                # Create the frozen assessment assignment from a labeled
                # simulated author result; no writer runtime or turn runs.
                completed = p.lifecycle.complete('synthetic-author-result', assignment['assignment_id'],
                                                 implementation(candidate(p.source)))
                assert completed['disposition'] == 'assessment_required'
                assignment = p.lifecycle.claim(p.lifecycle.assignments()[-1]['assignment_id'], worker.epoch)
        assert assignment['kind'] == ('assessment' if assessment else 'implementation')
        # The unsupported file read originates only from inherited :minimal.
        assert all(not selected.is_relative_to(Path(root)) for root in p.recipe['read_roots'])
        _, model = p.runtime._profile(p.recipe, assessment=assessment)
        assert model['filesystem'][':minimal'] == 'read'
        assert model['filesystem'][str(selected)] == 'deny'

        observation = p.runtime.start(assignment)
        saved = p.runtime._get(assignment['attempt_id'])
        evidence = {'assessment': assessment, 'profile': model, 'probes': probes,
                    'observation': observation, 'startup_outcome': saved.get('startup_outcome'),
                    'model_calls_attempted': saved['model_calls_attempted'], 'model_calls': p.runtime.model_calls,
                    'metadata_transport': 'SIMULATED scripted RPC; no real app-server or model call',
                    'metadata_methods': [method for method, _ in p.backend.calls],
                    'shared_journals_used': False, 'native_file': pinned_file,
                    'runtime_source_sha256': hashlib.sha256(Path(sys.modules[CodexRuntime.__module__].__file__).read_bytes()).hexdigest(),
                    'test_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (tmp_path / 'native-mime-startup-rejection-proof.json').write_text(json.dumps(evidence, indent=2) + '\n')
        assert len(probes) == 1
        probe_argv = probes[0]['argv']
        assert probe_argv[-12:-8] == [str(p.source), saved['run_outputs'], str(p.runtime.directory.resolve()),
                                     str(Path.home() / '.codex')]
        assert probe_argv[-8:-6] == [assignment['kind'], assignment['attempt_id']]
        assert probe_argv[-3] == 'model' and json.loads(probe_argv[-1]) == [pinned_file]
        assert probes[0]['exit_code'] == 1
        checks = probes[0]['checks']
        assert checks and checks['native_file_read_denied_0'] is False
        assert {name for name, value in checks.items() if value is not True} == {'native_file_read_denied_0'}
        assert 'sandbox_probe_failure' not in saved  # Recognized evidence, not a startup/timeout failure.
        assert saved['startup_outcome']['kind'] == 'before_thread_request'
        assert saved['error'] == ('Rejected: runtime_sandbox: '
                                  'OS permission probe did not demonstrate required denials/access')
        # Scripted metadata has no real process identity, so cleanup may remain
        # unknown. The permission gate and absence of any model turn are exact.
        assert observation['state'] in ('unknown', 'startup_failed')
        assert observation['model_calls_attempted'] == saved['model_calls_attempted'] == p.runtime.model_calls == 0
        assert p.backend.count('config/read') == 1
        assert p.backend.count('thread/start') == p.backend.count('turn/start') == 0
        assert not saved.get('thread_id') and not saved.get('turn_id')
        assert selected.read_bytes() == selected_before
    finally:
        p.runtime.close()
