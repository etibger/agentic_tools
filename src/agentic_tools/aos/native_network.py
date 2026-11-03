"""Observed host TCP boundary for native checks; no private namespace is claimed.

macOS wildcard TCP grants cannot safely subtract individual destination ports.
Use finite positive outbound grants and a protected-listener snapshot instead.
"""
from __future__ import annotations

import math
import subprocess
import time

from .store import Rejected, digest

VERSION = 'macos-native-ephemeral-tcp-v1'
LISTEN_SCOPE = 'host-local binding; protected listeners excluded; not a private network namespace'


def _configuration(recipe: dict) -> dict:
    """Read one explicit policy's TCP settings without translating contracts."""
    checkout = recipe.get('checkout_policy', {})
    local_present = isinstance(checkout, dict) and 'native_local_tests' in checkout
    if recipe.get('execution_policy') != 'isolated-checkout-v1':
        if local_present:
            raise Rejected('native_network', 'Local test networking requires the isolated-checkout execution policy')
        return recipe.get('native_sandbox', {})
    if 'native_sandbox' in recipe:
        raise Rejected('native_network', 'Isolated checkout cannot mix native sandbox networking')
    if not isinstance(checkout, dict):
        raise Rejected('native_network', 'Isolated checkout settings must be an object')
    if not local_present:
        return {}
    value = checkout['native_local_tests']
    if (not isinstance(value, dict) or set(value) != {'ephemeral_tcp', 'denied_tcp_ports'}
            or value['ephemeral_tcp'] is None):
        raise Rejected('native_network', 'Local tests require an exact TCP range and protected-port list')
    ports = value['denied_tcp_ports']
    if (not isinstance(ports, list) or len(ports) > 128
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports)):
        raise Rejected('native_network', 'Protected local test ports require at most 128 unique valid integers')
    return value


def contract(recipe: dict) -> dict | None:
    value = _configuration(recipe).get('ephemeral_tcp')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'first', 'last'}
            or any(type(value[k]) is not int for k in value)
            or not 1024 <= value['first'] <= value['last'] <= 65535
            or value['last'] - value['first'] + 1 > 16384):
        raise Rejected('native_network', 'Declare an exact nonprivileged ephemeral TCP range of at most 16384 ports')
    return value


def listening_ports(output: str) -> list[int]:
    """Parse all IPv4/IPv6 LISTEN rows; retain no other connection information."""
    if not output.startswith('Active Internet connections') or 'Local Address' not in output:
        raise Rejected('native_network', 'Native listener inspection was incomplete')
    result = set()
    for line in output.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith('tcp'):
            continue
        if len(fields) < 6 or fields[0] not in ('tcp4', 'tcp6'):
            raise Rejected('native_network', 'Unrecognized native TCP observation')
        if fields[-1] != 'LISTEN':
            continue
        try:
            port = int(fields[3].rsplit('.', 1)[1])
            if not 1 <= port <= 65535:
                raise ValueError('port')
        except (IndexError, ValueError) as error:
            raise Rejected('native_network', 'Unrecognized listening endpoint') from error
        result.add(port)
    return sorted(result)


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return 10
    try:
        valid = type(deadline) in (int, float) and math.isfinite(deadline)
    except OverflowError:
        valid = False
    if not valid:
        raise Rejected('native_network', 'Native TCP observation deadline must be a finite monotonic number')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Rejected('native_network', 'Native TCP observation deadline expired')
    return min(10, remaining)


def _run(argv: list[str], *, deadline: float | None = None) -> str:
    timeout = _remaining(deadline)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        _remaining(deadline)  # A reaped process returning late cannot establish readiness.
        if result.returncode or result.stderr or len(result.stdout) > 8 * 1024 * 1024:
            raise ValueError('Native TCP observation failed')
        return result.stdout
    except Rejected:
        raise
    except subprocess.TimeoutExpired as error:
        # subprocess.run kills and waits for these exact no-fork host observers.
        raise Rejected('native_network', 'Native TCP observation exceeded its bounded wait') from error
    except (OSError, subprocess.SubprocessError) as error:
        raise Rejected('native_network_launch_unknown', 'Native observer launch/reaping is unproved') from error
    except ValueError as error:
        raise Rejected('native_network', 'Complete native TCP observation is required') from error


def observe(recipe: dict, *, deadline: float | None = None) -> dict | None:
    _remaining(deadline)
    options = {} if deadline is None else {'deadline': deadline}
    bounds = contract(recipe)
    if bounds is None:
        return None
    try:
        first, last = map(int, _run(['/usr/sbin/sysctl', '-n', 'net.inet.ip.portrange.first',
                                    'net.inet.ip.portrange.last'], **options).split())
    except Rejected:
        raise
    except ValueError as error:
        raise Rejected('native_network', 'Native ephemeral range is unavailable') from error
    if bounds != {'first': first, 'last': last}:
        raise Rejected('native_network', 'The host ephemeral range differs from the approved recipe')
    existing = listening_ports(_run(['/usr/sbin/netstat', '-an', '-p', 'tcp'], **options))
    denied = sorted(set(existing) | set(_configuration(recipe).get('denied_tcp_ports', [])))
    allowed = [port for port in range(first, last + 1) if port not in denied]
    if not allowed:
        raise Rejected('native_network', 'No native test destination remains in the approved range')
    _remaining(deadline)
    return {'format': VERSION, **bounds, 'listening_ports': existing, 'denied_ports': denied,
            'allowed_port_count': len(allowed), 'allowed_ports_digest': digest(allowed),
            'listen_scope': LISTEN_SCOPE}


def allowed_ports(recipe: dict, observation: dict) -> list[int]:
    bounds = contract(recipe)
    keys = {'format', 'first', 'last', 'listening_ports', 'denied_ports', 'allowed_port_count',
            'allowed_ports_digest', 'listen_scope'}
    if (bounds is None or not isinstance(observation, dict) or set(observation) != keys
            or observation['format'] != VERSION
            or observation['listen_scope'] != LISTEN_SCOPE
            or any(observation[k] != v for k, v in bounds.items())):
        raise Rejected('native_network', 'An exact retained native network observation is required')
    for key in ('listening_ports', 'denied_ports'):
        values = observation[key]
        if (not isinstance(values, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in values)
                or values != sorted(set(values))):
            raise Rejected('native_network', 'Invalid native listener observation')
    if set(observation['denied_ports']) != (set(observation['listening_ports'])
                                          | set(_configuration(recipe).get('denied_tcp_ports', []))):
        raise Rejected('native_network', 'Protected destinations differ from the retained observation')
    allowed = [port for port in range(bounds['first'], bounds['last'] + 1)
               if port not in observation['denied_ports']]
    if (not allowed or type(observation['allowed_port_count']) is not int
            or observation['allowed_port_count'] != len(allowed)
            or observation['allowed_ports_digest'] != digest(allowed)):
        raise Rejected('native_network', 'Retained permitted destinations differ')
    return allowed


def verify_observation(recipe: dict, previous: dict | None, *, deadline: float | None = None) -> None:
    _remaining(deadline)
    options = {} if deadline is None else {'deadline': deadline}
    if contract(recipe) is None:
        if previous is not None:
            raise Rejected('native_network', 'Unexpected TCP authority in a network-disabled recipe')
        return
    permitted = set(allowed_ports(recipe, previous))
    current = observe(recipe, **options)
    _remaining(deadline)
    if permitted.intersection(set(current['listening_ports']) - set(previous['listening_ports'])):
        raise Rejected('native_network', 'A new listener now occupies a permitted test destination; prepare a fresh boundary')


def rules(recipe: dict, observation: dict | None) -> list[str]:
    """Positive outbound rules must remain separate to avoid compiler limits."""
    if contract(recipe) is None:
        if observation is not None:
            raise Rejected('native_network', 'Unexpected network observation')
        return []
    allowed = allowed_ports(recipe, observation)
    # Port zero cannot be named in SBPL. Native bind(0) requires this local
    # binding grant. Required tests explicitly bind loopback. Inbound policy
    # excludes protected ports without a wildcard-positive term: combining
    # such a term with exclusions defeats the exclusions on this host.
    result = ['(allow network-bind (local tcp "localhost:*"))']
    excluded = ['(require-not (local tcp "*:' + str(port) + '"))'
                for port in observation['denied_ports']]
    if excluded:
        # Protocol-only filtering preserves exclusions. A host/port wildcard
        # instead defeats them; no protocol filter would also grant UDP/UDS.
        result.append('(allow network-inbound (require-all (local tcp) ' + ' '.join(excluded) + '))')
    else:
        result.append('(allow network-inbound (local tcp "localhost:*"))')
    result.extend('(allow network-outbound (remote tcp "localhost:' + str(port) + '"))' for port in allowed)
    return result
