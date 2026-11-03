"""Pure contract tests; all host observations are supplied by fixtures."""
import copy
import subprocess

import pytest

from agentic_tools.aos import native_network as network
from agentic_tools.aos.store import Rejected


def recipe(first=50000, last=50003, denied=(8443, 50001)):
    return {'execution_policy': 'isolated-checkout-v1', 'checkout_policy': {
        'native_local_tests': {'ephemeral_tcp': {'first': first, 'last': last},
                               'denied_tcp_ports': list(denied)}}}


def host(monkeypatch, *, first=50000, last=50003, listening=(50002,)):
    state = {'listening': tuple(listening), 'calls': []}

    def output(argv):
        state['calls'].append(argv)
        if argv == ['/usr/sbin/sysctl', '-n', 'net.inet.ip.portrange.first',
                    'net.inet.ip.portrange.last']:
            return f'{first}\n{last}\n'
        assert argv == ['/usr/sbin/netstat', '-an', '-p', 'tcp']
        return ('Active Internet connections (including servers)\n'
                'Proto Recv-Q Send-Q Local Address Foreign Address (state)\n'
                + ''.join(f'tcp4 0 0 127.0.0.1.{port} *.* LISTEN\n'
                          for port in state['listening']))

    monkeypatch.setattr(network, '_run', output)
    return state


def test_checkout_mapping_preserves_legacy_observation_and_rules_without_mutation(monkeypatch):
    candidate = recipe()
    before = copy.deepcopy(candidate)
    legacy = {'native_sandbox': copy.deepcopy(candidate['checkout_policy']['native_local_tests'])}
    host(monkeypatch)
    observed = network.observe(candidate)
    assert network.contract(candidate) == network.contract(legacy) == {'first': 50000, 'last': 50003}
    assert observed == network.observe(legacy)
    assert network.allowed_ports(candidate, observed) == [50000, 50003]
    assert network.rules(candidate, observed) == network.rules(legacy, observed)
    assert [line for line in network.rules(candidate, observed) if 'network-outbound' in line] == [
        '(allow network-outbound (remote tcp "localhost:50000"))',
        '(allow network-outbound (remote tcp "localhost:50003"))',
    ]
    assert candidate == before and 'native_sandbox' not in candidate


@pytest.mark.parametrize('candidate', [
    {'execution_policy': 'isolated-checkout-v1'},
    {'execution_policy': 'isolated-checkout-v1', 'checkout_policy': {}},
])
def test_absent_local_networking_is_disabled_without_host_observation(monkeypatch, candidate):
    def unexpected(_):
        raise AssertionError('Network-disabled recipes must not inspect the host')

    monkeypatch.setattr(network, '_run', unexpected)
    assert network.contract(candidate) is None
    assert network.observe(candidate) is None
    assert network.rules(candidate, None) == []
    network.verify_observation(candidate, None)
    with pytest.raises(Rejected):
        network.verify_observation(candidate, {})
    with pytest.raises(Rejected):
        network.rules(candidate, {})


@pytest.mark.parametrize('value', [None, False, True, [], {},
    {'ephemeral_tcp': {'first': 50000, 'last': 50001}},
    {'denied_tcp_ports': []},
    {'ephemeral_tcp': None, 'denied_tcp_ports': []},
    {'ephemeral_tcp': {'first': 50000, 'last': 50001}, 'denied_tcp_ports': [], 'extra': True},
])
def test_local_settings_require_exact_object_and_explicit_fields(value):
    candidate = recipe()
    candidate['checkout_policy']['native_local_tests'] = value
    with pytest.raises(Rejected):
        network.contract(candidate)


@pytest.mark.parametrize('bounds', [False, True, {}, [],
    {'first': True, 'last': 50003}, {'first': 50000, 'last': True},
    {'first': 1023, 'last': 1024}, {'first': 50001, 'last': 50000},
    {'first': 50000, 'last': 65536}, {'first': 1024, 'last': 17408},
    {'first': 50000, 'last': 50003, 'extra': 1},
])
def test_local_range_has_same_strict_nonprivileged_bounds(bounds):
    candidate = recipe()
    candidate['checkout_policy']['native_local_tests']['ephemeral_tcp'] = bounds
    with pytest.raises(Rejected):
        network.contract(candidate)


@pytest.mark.parametrize('first,last', [(1024, 1024), (49152, 65535)])
def test_local_range_accepts_single_port_and_exact_maximum(first, last):
    assert network.contract(recipe(first, last, ())) == {'first': first, 'last': last}


@pytest.mark.parametrize('ports', [None, False, True, (8443,), '8443', [True], [0],
                                   [65536], [8443, 8443], list(range(1, 130))])
def test_protected_local_ports_are_bounded_unique_integers(ports):
    candidate = recipe()
    candidate['checkout_policy']['native_local_tests']['denied_tcp_ports'] = ports
    with pytest.raises(Rejected):
        network.contract(candidate)


def test_exact_port_limit_and_outside_range_protection_are_retained(monkeypatch):
    candidate = recipe(denied=range(1, 129))
    host(monkeypatch, listening=())
    observed = network.observe(candidate)
    assert observed['denied_ports'] == list(range(1, 129))
    assert network.allowed_ports(candidate, observed) == [50000, 50001, 50002, 50003]


@pytest.mark.parametrize('policy', [None, False, 'unknown-policy'])
def test_local_settings_cannot_implicitly_select_an_execution_policy(policy):
    candidate = recipe()
    candidate['execution_policy'] = policy
    with pytest.raises(Rejected, match='execution policy'):
        network.contract(candidate)
    del candidate['execution_policy']
    with pytest.raises(Rejected, match='execution policy'):
        network.contract(candidate)


@pytest.mark.parametrize('legacy', [None, {}, {'ephemeral_tcp': {'first': 50000, 'last': 50003}}])
def test_new_policy_cannot_mix_legacy_native_sandbox_even_when_tcp_absent(legacy):
    for candidate in (recipe(), {'execution_policy': 'isolated-checkout-v1', 'checkout_policy': {}}):
        candidate['native_sandbox'] = legacy
        with pytest.raises(Rejected, match='cannot mix'):
            network.contract(candidate)


@pytest.mark.parametrize('checkout', [None, False, True, []])
def test_checkout_settings_container_cannot_be_non_object(checkout):
    with pytest.raises(Rejected, match='must be an object'):
        network.contract({'execution_policy': 'isolated-checkout-v1', 'checkout_policy': checkout})


def test_legacy_explicit_none_ephemeral_range_remains_disabled():
    assert network.contract({'native_sandbox': {'ephemeral_tcp': None}}) is None


def test_prelaunch_new_listener_in_retained_allowed_set_blocks(monkeypatch):
    candidate = recipe()
    state = host(monkeypatch)
    observed = network.observe(candidate)
    state['listening'] = (50002, 50003)
    with pytest.raises(Rejected, match='new listener'):
        network.verify_observation(candidate, observed)


def test_prelaunch_listener_removal_or_new_already_protected_port_is_safe(monkeypatch):
    candidate = recipe()
    state = host(monkeypatch)
    observed = network.observe(candidate)
    state['listening'] = (8443, 50001, 60000)
    network.verify_observation(candidate, observed)
    # Rechecking never expands the previously rendered profile after a listener exits.
    assert network.allowed_ports(candidate, observed) == [50000, 50003]


def test_changed_protected_ports_or_host_range_cannot_reuse_observation(monkeypatch):
    candidate = recipe()
    host(monkeypatch)
    observed = network.observe(candidate)
    changed = copy.deepcopy(candidate)
    changed['checkout_policy']['native_local_tests']['denied_tcp_ports'].append(50003)
    with pytest.raises(Rejected, match='Protected destinations differ'):
        network.allowed_ports(changed, observed)
    host(monkeypatch, first=50001)
    with pytest.raises(Rejected, match='differs from the approved recipe'):
        network.verify_observation(candidate, observed)


def test_exhausted_destinations_cannot_produce_observation(monkeypatch):
    candidate = recipe(50000, 50000, ())
    host(monkeypatch, first=50000, last=50000, listening=(50000,))
    with pytest.raises(Rejected, match='No native test destination'):
        network.observe(candidate)


def deadline_host(monkeypatch, durations):
    clock = {'now': 100.0, 'calls': []}
    monkeypatch.setattr(network.time, 'monotonic', lambda: clock['now'])

    def run(argv, **kwargs):
        index = len(clock['calls'])
        assert index < len(durations), 'No observer may launch after the shared deadline'
        clock['calls'].append((list(argv), kwargs['timeout']))
        clock['now'] += durations[index]
        output = ('50000\n50003\n' if argv[0].endswith('sysctl') else
                  'Active Internet connections (including servers)\n'
                  'Proto Recv-Q Send-Q Local Address Foreign Address (state)\n')
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')

    monkeypatch.setattr(network.subprocess, 'run', run)
    return clock


def test_one_second_deadline_is_shared_by_both_observers(monkeypatch):
    clock = deadline_host(monkeypatch, [0.4, 0.4])
    observed = network.observe(recipe(), deadline=101.0)
    assert observed['allowed_port_count'] == 3
    assert [timeout for _, timeout in clock['calls']] == pytest.approx([1.0, 0.6])
    assert clock['now'] == pytest.approx(100.8)


def test_long_deadline_keeps_ten_second_per_observer_ceiling(monkeypatch):
    clock = deadline_host(monkeypatch, [0.4, 0.4])
    network.observe(recipe(), deadline=200.0)
    assert [timeout for _, timeout in clock['calls']] == [10, 10]


@pytest.mark.parametrize('durations,expected_calls', [([1.0], 1), ([0.4, 0.7], 2)])
def test_late_reaped_result_is_rejected_without_starting_an_extra_observer(monkeypatch, durations, expected_calls):
    clock = deadline_host(monkeypatch, durations)
    with pytest.raises(Rejected, match='deadline expired'):
        network.observe(recipe(), deadline=101.0)
    assert len(clock['calls']) == expected_calls


def test_budget_expiring_between_observers_prevents_second_launch(monkeypatch):
    moments = iter([100.0, 100.0, 100.4, 101.0])
    monkeypatch.setattr(network.time, 'monotonic', lambda: next(moments))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs['timeout']))
        assert argv[0] == '/usr/sbin/sysctl'
        return subprocess.CompletedProcess(argv, 0, stdout='50000\n50003\n', stderr='')

    monkeypatch.setattr(network.subprocess, 'run', run)
    with pytest.raises(Rejected, match='deadline expired'):
        network.observe(recipe(), deadline=101.0)
    assert len(calls) == 1


@pytest.mark.parametrize('entry', ['run', 'observe', 'verify'])
@pytest.mark.parametrize('deadline', [True, False, float('inf'), float('-inf'), float('nan'),
    '101', object(), pytest.param(10**400, id='overflowing-integer'), 99.0, 100.0])
def test_invalid_or_expired_deadline_rejects_before_any_subprocess(monkeypatch, entry, deadline):
    monkeypatch.setattr(network.time, 'monotonic', lambda: 100.0)

    def unexpected(*args, **kwargs):
        raise AssertionError('An invalid or expired deadline cannot launch a process')

    monkeypatch.setattr(network.subprocess, 'run', unexpected)
    with pytest.raises(Rejected, match='deadline'):
        if entry == 'run':
            network._run(['/usr/sbin/sysctl'], deadline=deadline)
        elif entry == 'observe':
            network.observe(recipe(), deadline=deadline)
        else:
            network.verify_observation(recipe(), None, deadline=deadline)


def test_no_deadline_preserves_ten_second_run_without_reading_clock(monkeypatch):
    def unexpected_clock():
        raise AssertionError('Legacy calls do not need an absolute deadline')

    monkeypatch.setattr(network.time, 'monotonic', unexpected_clock)
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout='ready', stderr='')

    monkeypatch.setattr(network.subprocess, 'run', run)
    assert network._run(['/usr/sbin/sysctl']) == 'ready'
    assert calls == [{'capture_output': True, 'text': True, 'timeout': 10}]


def test_verify_passes_same_deadline_and_rejects_a_late_return(monkeypatch):
    candidate = recipe()
    host(monkeypatch)
    observed = network.observe(candidate)
    clock = {'now': 100.0}
    monkeypatch.setattr(network.time, 'monotonic', lambda: clock['now'])
    calls = []

    def observe(actual, *, deadline):
        assert actual is candidate
        calls.append(deadline)
        return observed

    monkeypatch.setattr(network, 'observe', observe)
    network.verify_observation(candidate, observed, deadline=101.0)
    assert calls == [101.0]

    def late(actual, *, deadline):
        assert actual is candidate and deadline == 101.0
        clock['now'] = deadline
        return observed

    monkeypatch.setattr(network, 'observe', late)
    with pytest.raises(Rejected, match='deadline expired'):
        network.verify_observation(candidate, observed, deadline=101.0)


@pytest.mark.parametrize('error,code', [
    (subprocess.TimeoutExpired(['/usr/sbin/sysctl'], 1), 'native_network'),
    (OSError('launch uncertain'), 'native_network_launch_unknown'),
    (subprocess.SubprocessError('reaping uncertain'), 'native_network_launch_unknown'),
])
def test_deadline_does_not_reclassify_timeout_or_unknown_launch(monkeypatch, error, code):
    monkeypatch.setattr(network.time, 'monotonic', lambda: 100.0)

    def failed(*args, **kwargs):
        assert kwargs['timeout'] == 1.0
        raise error

    monkeypatch.setattr(network.subprocess, 'run', failed)
    with pytest.raises(Rejected) as caught:
        network._run(['/usr/sbin/sysctl'], deadline=101.0)
    assert caught.value.code == code
