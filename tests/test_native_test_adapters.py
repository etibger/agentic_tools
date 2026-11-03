"""Native evidence must establish every explicitly declared test invocation."""
from pathlib import Path

import pytest

from agentic_tools import check_adapters
from agentic_tools.aos.checks import passed, validate_normalized


NEXT = ('    Starting 3 tests across 2 binaries (17 tests skipped)\n'
        '     Summary [   0.103s] 3 tests run: 3 passed, 17 skipped\n')
UNIT = '......\n----------------------------------------------------------------------\nRan 6 tests in 0.413s\n\nOK\n'
BUN = ('bun test v1.3.14 (0d9b296a)\n\nexample.test.ts:\n(pass) example\n'
       '\n 1 pass\n 0 fail\n 3 expect() calls\nRan 1 test across 1 file. [584.00ms]\n')


def evidence(tmp_path, counts, stdout='', stderr='', exit_code=0):
    adapter = check_adapters.select({'result_kind': 'native-tests', 'suite_counts': counts})
    result = adapter.interpret(tmp_path, stdout, stderr, exit_code)
    validate_normalized(result)
    return result


@pytest.mark.parametrize('family,report,count', [('nextest', NEXT, 3), ('unittest', UNIT, 6), ('bun', BUN, 1)])
@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_exact_native_suite_can_pass_in_either_stream(tmp_path, family, report, count, stream):
    result = evidence(tmp_path, {family: 1}, **{stream: report})
    assert passed(result, 0) and result['selected'] == result['passed'] == count
    assert result['suites'][0]['family'] == family
    assert result['suites'][0]['stream'] == stream


def test_nextest_filtering_is_reported_separately_from_selected_results(tmp_path):
    result = evidence(tmp_path, {'nextest': 1}, stderr=NEXT)
    assert result['skipped'] == 0
    assert result['suites'][0]['unselected'] == 17
    assert passed(result, 0)


def test_explicit_mixed_command_requires_every_expected_invocation(tmp_path):
    counts = {'nextest': 1, 'unittest': 2, 'bun': 5}
    result = evidence(tmp_path, counts, stderr=NEXT + UNIT + BUN + UNIT + BUN * 4)
    assert passed(result, 0) and result['selected'] == 20
    assert result['suite_counts'] == counts and len(result['suites']) == 8


@pytest.mark.parametrize('stdout,stderr', [('', NEXT + UNIT), (UNIT, BUN), (NEXT, BUN),
    ('', NEXT + UNIT + BUN * 2), (NEXT + UNIT + BUN, NEXT + UNIT + BUN)])
def test_missing_unlisted_or_duplicate_components_cannot_pass(tmp_path, stdout, stderr):
    result = evidence(tmp_path, {'nextest': 1, 'unittest': 1, 'bun': 1}, stdout, stderr)
    assert not result['recognized'] and not passed(result, 0)


@pytest.mark.parametrize('report', [NEXT, UNIT, BUN])
@pytest.mark.parametrize('exit_code', [1, -9, None, True])
def test_complete_positive_reports_never_override_unsuccessful_exit(tmp_path, report, exit_code):
    family = {NEXT: 'nextest', UNIT: 'unittest', BUN: 'bun'}[report]
    result = evidence(tmp_path, {family: 1}, stderr=report, exit_code=exit_code)
    assert not passed(result, exit_code)


@pytest.mark.parametrize('report', ['', 'Finished build successfully\n', '1 passed\n', 'OK\n'])
def test_missing_test_evidence_is_not_a_pass(tmp_path, report):
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=report), 0)


@pytest.mark.parametrize('counts', [None, {}, [], {'unknown': 1}, {'bun': True}, {'bun': 0}, {'bun': -1},
    {'bun': 1.0}, {'bun': '1'}, {'bun': 129}, {'bun': 2 ** 64}, {'bun': 128, 'nextest': 1}])
def test_native_expected_invocations_are_validated_before_execution(counts):
    with pytest.raises(ValueError, match='suite_counts'):
        check_adapters.select({'result_kind': 'native-tests', 'suite_counts': counts})


def test_missing_descriptor_and_mismatched_adapter_are_rejected():
    with pytest.raises(ValueError):
        check_adapters.select({'result_kind': 'native-tests'})
    with pytest.raises(ValueError):
        check_adapters.select({'result_kind': 'native-tests', 'adapter_id': 'native-build-v1', 'suite_counts': {'bun': 1}})


def test_bound_composition_cannot_be_mutated_through_recipe_object(tmp_path):
    check = {'result_kind': 'native-tests', 'suite_counts': {'bun': 1}}
    adapter = check_adapters.select(check)
    check['suite_counts']['bun'] = 2
    assert passed(adapter.interpret(tmp_path, '', BUN, 0), 0)
    argv = ['just', 'check']
    assert adapter.command(argv, tmp_path) == argv
    assert adapter.command(argv, tmp_path) is not argv


@pytest.mark.parametrize('report', [
    NEXT.replace('3 tests run', '2 tests run'),
    NEXT.replace('3 passed', '2 passed'),
    NEXT.replace('3 passed', '4 passed'),
    NEXT.replace('17 skipped', '18 skipped'),
    NEXT.replace('3 passed', '3 passed, 1 timed out'),
    NEXT.replace('3 passed', '3 passed, 0 failed, 0 failed'),
    NEXT.replace('3 tests across', '3 tests around'),
    NEXT.splitlines()[0] + '\n', NEXT.splitlines()[1] + '\n', NEXT + NEXT.splitlines()[1] + '\n',
    NEXT.splitlines()[0] + '\n' + NEXT, NEXT + '     Summary [garbled]\n',
    NEXT.replace('3 tests', '9999999999999999999999 tests'),
])
def test_nextest_incomplete_malformed_or_contradictory_reports_fail_closed(tmp_path, report):
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=report), 0)


def test_nextest_failure_is_not_overridden_by_outer_command_zero_exit(tmp_path):
    report = NEXT.replace('3 passed', '2 passed, 1 failed')
    result = evidence(tmp_path, {'nextest': 1}, stderr=report)
    assert result['recognized'] and result['failed'] == 1 and not passed(result, 0)


def test_nextest_slow_annotation_is_a_subset_of_passed_tests(tmp_path):
    assert passed(evidence(tmp_path, {'nextest': 1}, stderr=NEXT.replace('3 passed', '3 passed (2 slow)')), 0)
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=NEXT.replace('3 passed', '3 passed (4 slow)')), 0)


def test_nextest_header_cannot_hide_an_incomplete_extra_invocation(tmp_path):
    header = ' Nextest run ID 4117b80c-eb38-4ad2-bb2f-e025d74e59e9 with nextest profile: default\n'
    assert passed(evidence(tmp_path, {'nextest': 1}, stderr=header + NEXT), 0)
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=header + NEXT + header), 0)
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=header + header + NEXT), 0)
    assert not passed(evidence(tmp_path, {'nextest': 1}, stderr=header.replace('4117b80c', 'bad') + NEXT), 0)


@pytest.mark.parametrize('family,report', [
    ('nextest', NEXT.replace('3 tests', '0 tests').replace('3 passed', '0 passed')),
    ('unittest', UNIT.replace('6 tests', '0 tests')),
    ('bun', BUN.replace('1 pass', '0 pass').replace('1 test across', '0 tests across')),
])
def test_zero_selection_cannot_be_hidden_by_another_positive_suite(tmp_path, family, report):
    other, other_report = ('bun', BUN) if family != 'bun' else ('unittest', UNIT)
    result = evidence(tmp_path, {family: 1, other: 1}, stderr=report + other_report)
    assert result['selected'] > 0 and not passed(result, 0)


@pytest.mark.parametrize('status', ['OK (skipped=6)', 'OK (expected failures=1)',
    'FAILED (failures=1)', 'FAILED (errors=1)', 'FAILED (unexpected successes=1)',
    'FAILED (failures=1, errors=1, skipped=1, expected failures=1, unexpected successes=1)'])
def test_unittest_nonpass_categories_are_not_silently_counted_as_pass(tmp_path, status):
    result = evidence(tmp_path, {'unittest': 1}, stderr=UNIT.replace('OK', status))
    assert result['recognized'] and not passed(result, 0)


@pytest.mark.parametrize('report', [UNIT.replace('OK', 'OK (failures=1)'), UNIT.replace('OK', 'FAILED'),
    UNIT.replace('OK', 'FAILED (failures=9)'), UNIT.replace('OK', 'OK (skipped=1, skipped=1)'),
    UNIT.replace('OK', 'OK (novel=1)'), UNIT.replace('OK', 'OK (skipped=-1)'),
    UNIT.replace('OK', 'OK (skipped=99999999999)'), UNIT.replace('OK', 'OKAY'),
    UNIT.replace('OK', ''), UNIT + 'OK\n', UNIT.replace('\n\nOK', '\ncompiler output\nOK'),
    UNIT.replace('6 tests', '9999999999 tests')])
def test_unittest_malformed_and_inconsistent_results_remain_valid_failure_evidence(tmp_path, report):
    result = evidence(tmp_path, {'unittest': 1}, stderr=report)
    assert not result['recognized'] and not passed(result, 0)


@pytest.mark.parametrize('category', ['skip', 'todo', 'fail'])
def test_bun_nonpass_categories_block_success(tmp_path, category):
    report = BUN.replace(' 1 pass', ' 0 pass')
    if category == 'fail':
        report = report.replace(' 0 fail', ' 1 fail')
    else:
        report = report.replace(' 0 fail', ' 1 ' + category + '\n 0 fail')
    result = evidence(tmp_path, {'bun': 1}, stderr=report)
    assert result['recognized'] and not passed(result, 0)


@pytest.mark.parametrize('report', [BUN.replace('1 test across', '2 tests across'),
    BUN.replace(' 1 pass', ' 2 pass'), BUN.replace(' 0 fail\n', ''),
    BUN.replace(' 1 pass', ' 1 pass\n 1 pass'), BUN.replace(' 0 fail', ' 0 flaky\n 0 fail'),
    BUN.replace(' 1 pass', ' 1 error\n 1 pass'), BUN.replace(' 1 pass', ' 99999999999999999999 skip\n 1 pass'),
    BUN.replace(' 1 pass', ' -1 skip\n 1 pass'), BUN.replace(' 1 pass', ' 1.5 error\n 1 pass'),
    BUN + ' -1 skip\n',
    BUN.replace('v1.3.14', 'garbled'), BUN.replace(' 0 fail', ' 0 fail\nextra output'),
    BUN + 'Ran 1 test across 1 file. [0.1s]\n', BUN + ' 1 pass\n',
    BUN.replace('1 file.', '0 files.'), BUN.replace(' 1 pass', ' 9999999999 pass'),
    BUN.replace('Ran 1 test across 1 file. [584.00ms]\n', ''),
    BUN.replace('bun test v1.3.14 (0d9b296a)\n', ''), BUN + 'bun test v1.3.14 (0d9b296a)\n'])
def test_bun_requires_complete_consistent_terminal_report(tmp_path, report):
    assert not passed(evidence(tmp_path, {'bun': 1}, stderr=report), 0)


@pytest.mark.parametrize('family,report', [('nextest', NEXT), ('unittest', UNIT), ('bun', BUN)])
def test_color_and_crlf_are_supported_without_joining_fragments(tmp_path, family, report):
    colored = '\r\n'.join('\x1b[1;32m' + line + '\x1b[0m' for line in report.splitlines())
    assert passed(evidence(tmp_path, {family: 1}, stderr=colored), 0)
    head, tail = report.rsplit('\n', 2)[:2]
    assert not passed(evidence(tmp_path, {family: 1}, stdout=head + '\n', stderr=tail + '\n'), 0)


def test_registry_identity_preserves_versioned_static_allowlist():
    identity = check_adapters.identities()['native-tests-v1']
    assert identity['version'] == 1 and identity['evidence_kind'] == 'tests'
    assert identity['source_digest'].startswith('sha256:')
    assert Path(check_adapters.__file__).parent / 'native_tests.py' in check_adapters.code_paths()


@pytest.mark.parametrize('kind', ['pytest', 'build', 'cargo-test'])
def test_other_adapters_cannot_silently_ignore_bun_commands(kind):
    with pytest.raises(ValueError, match='bun_commands'):
        check_adapters.select({'result_kind': kind, 'bun_commands': ['bun test sample.test.ts']})



SPLIT_HEADER = 'bun test v1.3.14 (0d9b296a)\n'
SPLIT_COMMANDS = (
    'bun test scripts/release.test.ts',
    'bun test src/integration/agent-state.test.ts',
    'bun test src/integration/opencode.test.ts',
    'bun test src/integration/tui-session.test.ts',
    'bun test ./scripts/docs',
)
SPLIT_COUNTS = {'nextest': 1, 'unittest': 2, 'bun': 5}


def bun_frame(command, selected, *, files=1):
    test_word, file_word = ('test' if selected == 1 else 'tests'), ('file' if files == 1 else 'files')
    return (command + '\n\nexample.test.ts:\n(pass) representative case\n\n'
            + f' {selected} pass\n 0 fail\n {selected * 2} expect() calls\n'
            + f'Ran {selected} {test_word} across {files} {file_word}. [1.00ms]\n')


def split_streams():
    stdout = 'build preamble\n' + SPLIT_HEADER * 5 + 'documentation reminder\n'
    nextest = NEXT.replace('3 tests', '3792 tests').replace('3 passed', '3792 passed')
    frames = [bun_frame(command, count, files=3 if index == 4 else 1)
              for index, (command, count) in enumerate(zip(SPLIT_COMMANDS, (5, 18, 9, 28, 7)))]
    stderr = (nextest + UNIT.replace('6 tests', '149 tests') + frames[0]
              + UNIT.replace('6 tests', '7 tests') + ''.join(frames[1:]))
    return stdout, stderr


def split_evidence(tmp_path, stdout, stderr, *, exit_code=0, commands=SPLIT_COMMANDS,
                   counts=None):
    check = {'result_kind': 'native-tests', 'suite_counts': counts or SPLIT_COUNTS,
             'bun_commands': list(commands)}
    result = check_adapters.select(check).interpret(tmp_path, stdout, stderr, exit_code)
    validate_normalized(result)
    return result


def test_bound_split_bun_frames_preserve_each_invocation_and_stream_attribution(tmp_path):
    stdout, stderr = split_streams()
    result = split_evidence(tmp_path, stdout, stderr)
    assert passed(result, 0) and result['selected'] == result['passed'] == 4015
    assert result['suite_counts'] == SPLIT_COUNTS and len(result['suites']) == 8
    bun = [row for row in result['suites'] if row['family'] == 'bun']
    assert [row['selected'] for row in bun] == [5, 18, 9, 28, 7]
    assert [row['command'] for row in bun] == list(SPLIT_COMMANDS)
    assert [row['invocation'] for row in bun] == [1, 2, 3, 4, 5]
    assert all(row['stream'] == 'split' and row['header_stream'] == 'stdout'
               and row['summary_stream'] == 'stderr' for row in bun)
    assert all(row['stream'] == 'stderr' for row in result['suites'] if row['family'] != 'bun')


def test_split_bun_mode_requires_explicit_command_binding(tmp_path):
    stdout, stderr = split_streams()
    result = evidence(tmp_path, SPLIT_COUNTS, stdout, stderr)
    assert not result['recognized'] and not passed(result, 0)


def test_split_mode_preserves_color_and_crlf_support(tmp_path):
    stdout, stderr = split_streams()
    def colored(text):
        return '\r\n'.join('\x1b[1;32m' + line + '\x1b[0m' for line in text.splitlines())
    assert passed(split_evidence(tmp_path, colored(stdout), colored(stderr)), 0)


@pytest.mark.parametrize('category', ['fail', 'skip', 'todo'])
def test_complete_split_failure_is_recognized_but_never_passes(tmp_path, category):
    stdout, stderr = split_streams()
    stderr = stderr.replace(' 5 pass\n', ' 4 pass\n', 1)
    if category == 'fail':
        stderr = stderr.replace(' 0 fail\n', ' 1 fail\n', 1)
    else:
        stderr = stderr.replace(' 0 fail\n', f' 1 {category}\n 0 fail\n', 1)
    result = split_evidence(tmp_path, stdout, stderr)
    assert result['recognized'] and not passed(result, 0)
    assert result['selected'] == 4015 and result['passed'] == 4014


@pytest.mark.parametrize('exit_code', [1, -9, None, True])
def test_complete_split_report_cannot_override_unsuccessful_exit(tmp_path, exit_code):
    stdout, stderr = split_streams()
    assert not passed(split_evidence(tmp_path, stdout, stderr, exit_code=exit_code), exit_code)


@pytest.mark.parametrize('commands', [None, '', {}, (), [],
    list(SPLIT_COMMANDS[:-1]), [SPLIT_COMMANDS[0]] * 5,
    [*SPLIT_COMMANDS[:-1], 1], [*SPLIT_COMMANDS[:-1], None],
    [*SPLIT_COMMANDS[:-1], '']])
def test_split_command_list_is_strictly_bound_before_execution(commands):
    with pytest.raises(ValueError, match='bun_commands'):
        check_adapters.select({'result_kind': 'native-tests', 'suite_counts': SPLIT_COUNTS,
                               'bun_commands': commands})


@pytest.mark.parametrize('command', [
    'bun test', 'bun  test scripts/a.ts', ' bun test scripts/a.ts',
    'bun test scripts/a.ts ', 'bun test scripts/a.ts --watch',
    'bun test scripts/a.ts scripts/b.ts', 'bun test --help',
    "bun test 'scripts/a.ts'", 'bun test scripts/a.ts;echo',
    'bun test scripts/a.ts\n', 'bun test scripts/a.ts\t', 'bun test scripts/a.ts\x00',
    'bun test /absolute/a.ts', 'bun test ../outside.ts', 'bun test scripts/../outside.ts',
])
def test_split_commands_reject_unsupported_shell_or_target_syntax(command):
    with pytest.raises(ValueError, match='bun_commands'):
        check_adapters.select({'result_kind': 'native-tests', 'suite_counts': {'bun': 1},
                               'bun_commands': [command]})


def test_split_commands_require_a_declared_positive_bun_suite_count():
    with pytest.raises(ValueError, match='bun_commands'):
        check_adapters.select({'result_kind': 'native-tests', 'suite_counts': {'nextest': 1},
                               'bun_commands': [SPLIT_COMMANDS[0]]})


def test_bound_split_commands_do_not_follow_mutations_to_recipe_objects(tmp_path):
    original = 'bun test tests/one.test.ts'
    commands, counts = [original], {'bun': 1}
    adapter = check_adapters.select({'result_kind': 'native-tests', 'suite_counts': counts,
                                     'bun_commands': commands})
    commands[0] = 'bun test tests/substituted.test.ts'
    commands.append('bun test tests/extra.test.ts')
    counts['bun'] = 2
    result = adapter.interpret(tmp_path, SPLIT_HEADER, bun_frame(original, 1), 0)
    validate_normalized(result)
    assert passed(result, 0) and result['suite_counts'] == {'bun': 1}
    assert result['suites'][0]['command'] == original


@pytest.mark.parametrize('case', [
    'missing_header', 'extra_header', 'malformed_header', 'different_header',
    'stdout_count', 'stdout_unknown_count', 'stdout_summary', 'stdout_command',
    'stderr_version_header', 'missing_command', 'extra_command', 'substituted_target',
    'wrong_order', 'duplicate_target', 'unsupported_command', 'missing_summary',
    'orphan_count', 'orphan_summary', 'nested_frame', 'duplicate_count', 'unknown_count',
    'inconsistent_totals', 'noncontiguous_terminal', 'extra_complete_report',
    'trailing_incomplete_command', 'negative_trailing_count', 'summary_moved_to_stdout',
    'extra_malformed_command', 'extra_malformed_header', 'zero_selection', 'reversed_streams',
])
def test_split_bun_ambiguous_or_incomplete_frames_fail_closed(tmp_path, case):
    stdout, stderr = split_streams()
    first, second = SPLIT_COMMANDS[:2]
    first_summary = 'Ran 5 tests across 1 file. [1.00ms]\n'
    last_summary = 'Ran 7 tests across 3 files. [1.00ms]\n'
    variants = {
        'missing_header': (stdout.replace(SPLIT_HEADER, '', 1), stderr),
        'extra_header': (stdout + SPLIT_HEADER, stderr),
        'malformed_header': (stdout.replace('v1.3.14', 'garbled', 1), stderr),
        'different_header': (stdout.replace('v1.3.14', 'v1.3.15', 1), stderr),
        'stdout_count': (stdout + ' 1 pass\n', stderr),
        'stdout_unknown_count': (stdout + ' 0 flaky\n', stderr),
        'stdout_summary': (stdout + first_summary, stderr),
        'stdout_command': (stdout + first + '\n', stderr),
        'stderr_version_header': (stdout, stderr.replace(first + '\n', first + '\n' + SPLIT_HEADER, 1)),
        'missing_command': (stdout, stderr.replace(first + '\n', '', 1)),
        'extra_command': (stdout, stderr + bun_frame(first, 5)),
        'substituted_target': (stdout, stderr.replace(first, 'bun test tests/substituted.test.ts', 1)),
        'wrong_order': (stdout, stderr.replace(first, '__FIRST__', 1).replace(second, first, 1).replace('__FIRST__', second, 1)),
        'duplicate_target': (stdout, stderr.replace(second, first, 1)),
        'unsupported_command': (stdout, stderr.replace(first, first + ' --watch', 1)),
        'missing_summary': (stdout, stderr.replace(last_summary, '', 1)),
        'orphan_count': (stdout, ' 1 pass\n' + stderr),
        'orphan_summary': (stdout, first_summary + stderr),
        'nested_frame': (stdout, stderr.replace(' 5 pass\n', second + '\n 5 pass\n', 1)),
        'duplicate_count': (stdout, stderr.replace(' 5 pass\n', ' 5 pass\n 5 pass\n', 1)),
        'unknown_count': (stdout, stderr.replace(' 0 fail\n', ' 0 flaky\n 0 fail\n', 1)),
        'inconsistent_totals': (stdout, stderr.replace(first_summary, first_summary.replace('5 tests', '6 tests'), 1)),
        'noncontiguous_terminal': (stdout, stderr.replace(' 0 fail\n', ' 0 fail\ncompiler message\n', 1)),
        'extra_complete_report': (stdout, stderr + BUN),
        'trailing_incomplete_command': (stdout, stderr + first + '\n'),
        'negative_trailing_count': (stdout, stderr + ' -1 skip\n'),
        'summary_moved_to_stdout': (stdout + first_summary, stderr.replace(first_summary, '', 1)),
        'extra_malformed_command': (stdout, stderr + 'bun  test tests/extra.ts\n'),
        'extra_malformed_header': (stdout + 'bun\ttest v1.3.14 (0d9b296a)\n', stderr),
        'zero_selection': (stdout, stderr.replace(' 5 pass\n', ' 0 pass\n', 1).replace(first_summary, first_summary.replace('5 tests', '0 tests'), 1)),
        'reversed_streams': (stderr, stdout),
    }
    result = split_evidence(tmp_path, *variants[case])
    assert not result['recognized'] and not passed(result, 0)
