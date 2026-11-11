"""Preparation stays read-only; start delegates all ownership to its supervisor."""
import json

import pytest

from agentic_tools.aos import cli, project
from agentic_tools.aos.codex_runtime import CodexRuntime
from agentic_tools.aos.store import Store
from test_aos_project import setup as project_setup


@pytest.fixture
def command(project_setup, monkeypatch):
    p = project_setup
    original = project.prepare
    monkeypatch.setattr(project, 'prepare', lambda project_path, request_path:
                        original(project_path, request_path, readiness_runner=p.runner))
    monkeypatch.setattr(Store, '__init__', lambda *args, **kwargs: pytest.fail('No Store during CLI preparation'))
    monkeypatch.setattr(CodexRuntime, '__init__', lambda *args, **kwargs: pytest.fail('No product runtime during CLI preparation'))
    p.command = lambda: cli.main(['prepare-project', '--project', str(p.project_file), '--request', str(p.request_file)])
    return p


def test_command_returns_complete_ready_packet_without_product_side_effects(command, capsys):
    p = command
    assert p.command() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == '' and result['ready']
    assert result['input_bindings']['project']['path'] == str(p.project_file)
    assert result['concept']['execution_envelope']['baseline'].startswith('sha256:')
    assert result['approval_recorded'] is result['reservation_performed'] is False
    assert list(p.state.iterdir()) == [] and len(p.calls) == 1


def test_command_creates_exact_directory_plan_then_checks_readiness_once(command, capsys):
    p = command
    p.outputs.rmdir()
    assert p.command() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ready'] and result['directory_preparation']['prepared']
    assert result['directory_preparation']['created_directories'] == [str(p.outputs)]
    assert p.outputs.stat().st_mode & 0o777 == 0o700
    assert len(p.calls) == 1 and list(p.state.iterdir()) == []


def test_command_reports_one_blocker_and_no_directory_side_effect_for_bad_request(command, capsys):
    p = command
    p.outputs.rmdir()
    p.request['concept']['authority'] = 'Changed'
    p.save()
    assert p.command() == 3
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == '' and result['blocker']['code'] == 'project_request'
    assert not p.outputs.exists() and p.calls == []


def test_later_readiness_failure_retains_created_directories_and_never_retries(command, capsys):
    p = command
    p.outputs.rmdir()
    p.result_change = lambda r: r.update(ready=False, blockers=[{'detail': 'Owned check failed; inspect retained output'}])
    assert p.command() == 3
    result = json.loads(capsys.readouterr().out)
    assert not result['ready'] and result['directory_preparation']['created_directories'] == [str(p.outputs)]
    assert p.outputs.is_dir() and len(p.calls) == 1
    assert 'Owned check failed' in result['blocker']['detail']
    assert list(p.state.iterdir()) == []


def test_directory_creation_failure_is_reported_without_readiness_or_second_try(command, capsys, monkeypatch):
    p = command
    p.outputs.rmdir()
    def denied(*args, **kwargs):
        return {'prepared': False, 'blocker': {'code': 'project_directories', 'detail': 'Directory ownership changed'},
                'created_directories': []}
    monkeypatch.setattr(project, 'prepare_directories', denied)
    assert p.command() == 3
    result = json.loads(capsys.readouterr().out)
    assert result['blocker']['detail'] == 'Directory ownership changed'
    assert not p.outputs.exists() and p.calls == []


def test_existing_version_command_still_needs_no_state(command, capsys):
    assert cli.main(['version']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['aos_component_version']
    assert list(command.state.iterdir()) == [] and command.calls == []


def test_relative_project_and_request_arguments_preserve_canonical_bindings(command, capsys, monkeypatch):
    p = command
    monkeypatch.chdir(p.control)
    assert cli.main(['prepare-project', '--project', p.project_file.name, '--request', p.request_file.name]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ready']
    assert result['input_bindings']['project']['path'] == str(p.project_file)
    assert result['input_bindings']['request']['path'] == str(p.request_file)
    assert list(p.state.iterdir()) == [] and len(p.calls) == 1


def test_relative_input_symlink_is_still_rejected(command, capsys, monkeypatch):
    p = command
    alias = p.control / 'project-alias.json'
    alias.symlink_to(p.project_file)
    monkeypatch.chdir(p.control)
    assert cli.main(['prepare-project', '--project', alias.name, '--request', p.request_file.name]) == 3
    result = json.loads(capsys.readouterr().out)
    assert not result['ready'] and result['blocker']['code'] == 'native_scope'
    assert p.calls == [] and list(p.state.iterdir()) == []


@pytest.fixture
def start_command(command, monkeypatch):
    from agentic_tools.aos import supervisor
    p = command
    p.start_calls = []
    p.start_result = {'format': 'aos-supervisor-v1', 'work_id': 'FIXTURE', 'state': 'delivered',
                      'cleanup': {'verified': True}}
    p.start_error = None
    def run(project_path, request_path):
        p.start_calls.append((project_path, request_path))
        if p.start_error is not None:
            raise p.start_error
        return p.start_result
    monkeypatch.setattr(supervisor, 'start', run)
    monkeypatch.setattr(project, 'prepare', lambda *args, **kwargs:
                        pytest.fail('The start command must not prepare independently of its supervisor'))
    monkeypatch.setattr(project, 'prepare_directories', lambda *args, **kwargs:
                        pytest.fail('Only the supervisor may initialize start directories'))
    p.start_command = lambda: cli.main(['start', '--project', str(p.project_file), '--request', str(p.request_file)])
    return p


@pytest.mark.parametrize('state,expected', [('delivered', 0), ('declined', 0), ('cancelled', 0),
                                            ('held', 3), ('paused', 3), ('superseded', 3), ('interrupted', 3)])
def test_start_delegates_once_and_preserves_supervisor_terminal_result(start_command, capsys, state, expected):
    p = start_command
    p.start_result['state'] = state
    assert p.start_command() == expected
    output = capsys.readouterr()
    assert output.err == '' and json.loads(output.out) == p.start_result
    assert p.start_calls == [(p.project_file, p.request_file)]
    assert p.calls == [] and list(p.state.iterdir()) == []


def test_start_passes_relative_inputs_to_the_existing_project_reader(start_command, capsys, monkeypatch):
    from pathlib import Path
    p = start_command
    monkeypatch.chdir(p.control)
    assert cli.main(['start', '--project', p.project_file.name, '--request', p.request_file.name]) == 0
    assert p.start_calls == [(Path(p.project_file.name), Path(p.request_file.name))]
    assert json.loads(capsys.readouterr().out) == p.start_result
    assert p.calls == [] and list(p.state.iterdir()) == []


def test_start_preserves_one_supervisor_blocker_without_retry(start_command, capsys):
    p = start_command
    p.start_result.update(state='held', detail='Exact source changed after approval',
                          next_action='Review the changed source before another start')
    assert p.start_command() == 3
    result = json.loads(capsys.readouterr().out)
    assert result == p.start_result and len(p.start_calls) == 1
    assert p.calls == []


def test_start_uses_existing_cli_error_handling_without_setup(start_command, capsys):
    from agentic_tools.aos.store import Rejected
    p = start_command
    p.start_error = Rejected('supervisor_owned', 'The existing supervisor still owns this work')
    assert p.start_command() == 2
    output = capsys.readouterr()
    assert output.out == '' and json.loads(output.err)['error'] == 'supervisor_owned'
    assert len(p.start_calls) == 1 and p.calls == [] and list(p.state.iterdir()) == []


def test_assessment_continuation_cli_preserves_exact_support_file_paths(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from agentic_tools.aos import assessment_continuation, lifecycle, supervisor
    state = tmp_path / 'state'
    support = tmp_path / 'policy.json'
    selected = SimpleNamespace(directory=state)
    monkeypatch.setattr(cli, 'Store', lambda *args, **kwargs: selected)
    monkeypatch.setattr(lifecycle, 'Lifecycle', lambda store: store)
    prepared = {'introduction': {'work_id': 'FIXTURE'}}
    calls = []
    def prepare(context, **options):
        assert context is selected
        assert options == {'operation_id': 'continue-fixture', 'recovery_operation_id': 'recover-fixture',
                           'support_files': [str(support)]}
        calls.append(options)
        return prepared
    monkeypatch.setattr(assessment_continuation, 'prepare', prepare)
    retained = []
    monkeypatch.setattr(supervisor, '_retain', lambda path, packet: retained.append((path, packet)))
    assert cli.main(['prepare-assessment-continuation', '--state-dir', str(state),
                     '--worker-writable-root', str(tmp_path / 'source'),
                     '--operation-id', 'continue-fixture', '--recovery-operation-id', 'recover-fixture',
                     '--support-file', str(support)]) == 0
    result = json.loads(capsys.readouterr().out)
    expected = state / 'prepared-assessment-FIXTURE-continue-fixture.json'
    assert result == {'ready': True, 'continuation': str(expected), 'work_id': 'FIXTURE',
                      'approval_recorded': False}
    assert len(calls) == 1 and retained == [(expected, prepared)]


def test_source_review_evidence_cli_forwards_exact_read_only_inputs(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from agentic_tools.aos import source_review_evidence, lifecycle
    selected = SimpleNamespace(directory=tmp_path / 'state')
    stores, calls = [], []
    def store(*args, **kwargs):
        stores.append((args, kwargs))
        return selected
    monkeypatch.setattr(cli, 'Store', store)
    monkeypatch.setattr(lifecycle.Lifecycle, 'for_reading', lambda context: context)
    descriptor = {'format': 'fixture-descriptor'}
    def prepare(context, **options):
        assert context is selected
        calls.append(options)
        return descriptor
    monkeypatch.setattr(source_review_evidence, 'prepare', prepare)
    destination = tmp_path / 'retained-review.json'
    assert cli.main(['prepare-source-review-evidence', '--state-dir', str(selected.directory),
                     '--worker-writable-root', str(tmp_path / 'source'), '--work-id', 'FIXTURE',
                     '--git-base', 'a' * 40, '--destination', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out) == descriptor
    assert stores[0][1] == {'read_only': True}
    assert calls == [{'work_id': 'FIXTURE', 'destination': destination, 'git_base': 'a' * 40}]


def test_assessment_review_continuation_cli_retains_bound_descriptor(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from agentic_tools.aos import assessment_review_continuation, lifecycle, supervisor
    state = tmp_path / 'state'
    descriptor = {'format': 'fixture-descriptor', 'candidate_identity': 'frozen-candidate'}
    evidence = tmp_path / 'descriptor.json'
    evidence.write_text(json.dumps(descriptor))
    evidence.chmod(0o400)
    selected = SimpleNamespace(directory=state)
    monkeypatch.setattr(cli, 'Store', lambda *args, **kwargs: selected)
    monkeypatch.setattr(lifecycle, 'Lifecycle', lambda context: context)
    prepared = {'introduction': {'work_id': 'FIXTURE'}}
    calls, retained = [], []
    def prepare(context, **options):
        assert context is selected
        calls.append(options)
        return prepared
    monkeypatch.setattr(assessment_review_continuation, 'prepare', prepare)
    monkeypatch.setattr(supervisor, '_retain', lambda path, packet: retained.append((path, packet)))
    assert cli.main(['prepare-assessment-review-continuation', '--state-dir', str(state),
                     '--worker-writable-root', str(tmp_path / 'source'), '--operation-id', 'review-fixture',
                     '--work-id', 'FIXTURE', '--failed-assignment-id', 'failed-fixture',
                     '--source-review-evidence', str(evidence), '--readiness-extra-attempt']) == 0
    expected = state / 'prepared-assessment-review-FIXTURE-review-fixture.json'
    assert calls == [{'operation_id': 'review-fixture', 'work_id': 'FIXTURE',
                      'failed_assignment_id': 'failed-fixture', 'source_review_evidence': descriptor,
                      'readiness_extra_attempt': True}]
    assert retained == [(expected, prepared)]
    assert json.loads(capsys.readouterr().out) == {'ready': True, 'continuation': str(expected),
                                                'work_id': 'FIXTURE', 'approval_recorded': False}
