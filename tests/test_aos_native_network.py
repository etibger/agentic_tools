import copy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from agentic_tools.aos import native_network as network, native_sandbox as sandbox
from agentic_tools.aos.store import Rejected, digest


def recipe(first=49152, last=65535, denied=()):
    return {'native_sandbox': {'version': sandbox.VERSION,
                              'ephemeral_tcp': {'first': first, 'last': last},
                              'denied_tcp_ports': list(denied)}}


def observation(candidate, listening=()):
    bounds = candidate['native_sandbox']['ephemeral_tcp']
    denied = sorted(set(listening) | set(candidate['native_sandbox'].get('denied_tcp_ports', [])))
    allowed = [p for p in range(bounds['first'], bounds['last'] + 1) if p not in denied]
    return {'format': network.VERSION, **bounds, 'listening_ports': sorted(set(listening)),
            'denied_ports': denied, 'allowed_port_count': len(allowed),
            'allowed_ports_digest': digest(allowed), 'listen_scope': network.LISTEN_SCOPE}


@pytest.mark.parametrize('bounds', [True, {}, {'first': True, 'last': 65535},
                                  {'first': 1, 'last': 2}, {'first': 10000, 'last': 65535},
                                  {'first': 60000, 'last': 50000}, {'first': 60000, 'last': 65536},
                                  {'first': 49152, 'last': 65535, 'extra': True}])
def test_exact_bounded_nonprivileged_range(bounds):
    candidate = recipe()
    candidate['native_sandbox']['ephemeral_tcp'] = bounds
    with pytest.raises(Rejected):
        sandbox.config(candidate)


def test_all_ipv4_ipv6_listeners_are_retained_without_connection_details():
    data = ('Active Internet connections (including servers)\n'
            'Proto Recv-Q Send-Q Local Address Foreign Address (state)\n'
            'tcp4 0 0 127.0.0.1.8443 *.* LISTEN\n'
            'tcp6 0 0 ::1.60000 *.* LISTEN\n'
            'tcp4 0 0 *.60000 *.* LISTEN\n'
            'tcp4 0 0 127.0.0.1.50001 192.0.2.1.443 ESTABLISHED\n')
    assert network.listening_ports(data) == [8443, 60000]
    with pytest.raises(Rejected):
        network.listening_ports(data.replace('*.60000', '*.unknown'))
    with pytest.raises(Rejected):
        network.listening_ports('')


def test_positive_destinations_exclude_all_existing_and_explicit_protected_ports():
    candidate = recipe(50000, 50003, [8443, 50001])
    saved = observation(candidate, [50002])
    rules = network.rules(candidate, saved)
    outbound = [line for line in rules if 'network-outbound' in line]
    assert outbound == ['(allow network-outbound (remote tcp "localhost:50000"))',
                        '(allow network-outbound (remote tcp "localhost:50003"))']
    assert 'localhost:*' not in ''.join(outbound)
    inbound = next(line for line in rules if 'network-inbound' in line)
    assert '(require-all (local tcp)' in inbound
    assert 'localhost:*' not in inbound
    for port in (8443, 50001, 50002):
        assert '(require-not (local tcp "*:' + str(port) + '"))' in inbound


@pytest.mark.parametrize('key,value', [('denied_ports', []), ('first', 49153),
                                      ('allowed_ports_digest', 'forged'), ('allowed_port_count', True),
                                      ('listen_scope', 'private network namespace')])
def test_changed_or_misleading_snapshot_rejected(key, value):
    candidate = recipe(denied=[8443])
    saved = observation(candidate)
    saved[key] = value
    with pytest.raises(Rejected):
        network.allowed_ports(candidate, saved)


def test_observation_range_drift_and_incomplete_native_inspection_reject(monkeypatch):
    monkeypatch.setattr(network, '_run', lambda argv: '50000\n65535\n')
    with pytest.raises(Rejected, match='differs'):
        network.observe(recipe())
    monkeypatch.setattr(network, '_run', lambda argv: '49152\n65535\n' if argv[0].endswith('sysctl') else '')
    with pytest.raises(Rejected, match='incomplete'):
        network.observe(recipe())


def test_new_listener_in_previously_allowed_range_blocks_launch(monkeypatch):
    candidate = recipe(denied=[8443])
    saved = observation(candidate, [50000])
    monkeypatch.setattr(network, 'observe', lambda _: observation(candidate, [50000, 50001]))
    with pytest.raises(Rejected, match='new listener'):
        network.verify_observation(candidate, saved)
    monkeypatch.setattr(network, 'observe', lambda _: observation(candidate, [8443]))
    network.verify_observation(candidate, saved)  # Already denied endpoints cannot widen the profile.


def test_disabled_network_never_accepts_snapshot_or_implicit_tcp():
    assert network.observe({}) is None
    assert network.rules({}, None) == []
    with pytest.raises(Rejected):
        network.rules({}, observation(recipe()))
    with pytest.raises(Rejected):
        sandbox.render(recipe(), {':root': 'deny', ':minimal': 'read'})


@pytest.mark.skipif(sys.platform != 'darwin', reason='Qualified native macOS boundary')
def test_real_full_ephemeral_policy_permits_owned_tcp_and_blocks_protected_and_external(tmp_path):
    owned = []
    udp_peers = {}
    try:
        for family, host in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
            listener = socket.socket(family)
            listener.bind((host, 0))
            listener.listen()
            owned.append(listener)
            peer = socket.socket(family, socket.SOCK_DGRAM)
            peer.bind((host, 0))
            udp_peers[host] = peer
        for port in range(31101, 31180):
            listener = socket.socket()
            try:
                listener.bind(('127.0.0.1', port))
                listener.listen()
                owned.append(listener)
                break
            except OSError:
                listener.close()
        assert len(owned) == 3
        unused = socket.socket()
        unused.bind(('127.0.0.1', 0))
        claim = unused.getsockname()[1]
        unused.close()
        candidate = recipe(denied=[claim, owned[2].getsockname()[1]])
        observed = network.observe(candidate)
        assert set(s.getsockname()[1] for s in owned).issubset(observed['listening_ports'])
        python = Path(sys.executable).resolve()
        writable = tmp_path / 'native-output'
        writable.mkdir()
        fs = {':root': 'deny', ':minimal': 'read', str(Path(sys.base_prefix).resolve()): 'read',
              str(python.parent.parent): 'read', str(writable): 'write'}
        profile = sandbox.render(candidate, fs, preexisting={}, network_observation=observed)
        path = tmp_path / 'native.sb'
        path.write_text(profile)
        protected = [(s.getsockname()[0], s.getsockname()[1]) for s in owned]
        code = '''import socket,json
from pathlib import Path
r={}
Path('write-proof').write_text('filesystem write is allowed independently of socket policy')
for family,host in [(socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')]:
 s=socket.socket(family);s.bind((host,0));s.listen()
 c=socket.socket(family);c.settimeout(.5);c.connect(s.getsockname());c.sendall(b'owned')
 a,_=s.accept();r[host]=a.recv(20).decode();a.close();c.close();s.close()
for host,port in PROTECTED:
 try:
  c=socket.create_connection((host,port),.3);c.close();r[str(port)]='unexpected connected'
 except OSError as e:r[str(port)]=e.errno
for host in ('127.0.0.1','0.0.0.0'):
 s=socket.socket()
 try:s.bind((host,CLAIM));s.listen();r['claim:'+host]='unexpected listening'
 except OSError as e:r['claim:'+host]=e.errno
 finally:s.close()
try:
 c=socket.create_connection(('192.0.2.1',EXTERNAL_PORT),.3);c.close();r['external']='unexpected connected'
except OSError as e:r['external']=e.errno
print(json.dumps(r))
'''.replace('PROTECTED', repr(protected)).replace('CLAIM', str(claim)).replace(
            'EXTERNAL_PORT', str(network.allowed_ports(candidate, observed)[0]))
        code = code.replace('print(json.dumps(r))', '''
for family,host in [(socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')]:
 s=socket.socket(family,socket.SOCK_DGRAM)
 try:s.bind((host,0));r['udp-bind:'+host]='unexpected bind'
 except OSError as e:r['udp-bind:'+host]=e.errno
 finally:s.close()
 s=socket.socket(family,socket.SOCK_DGRAM)
 try:s.sendto(b'owned fixture',(host,UDP_PORTS[host]));r['udp-send:'+host]='unexpected send'
 except OSError as e:r['udp-send:'+host]=e.errno
 finally:s.close()
s=socket.socket(socket.AF_UNIX)
try:s.bind('undeclared.sock');r['uds-bind']='unexpected bind'
except OSError as e:r['uds-bind']=e.errno
finally:s.close()
print(json.dumps(r))'''.replace('UDP_PORTS', repr({host: peer.getsockname()[1] for host, peer in udp_peers.items()})))
        result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(path), str(python), '-I', '-S', '-c', code],
                                capture_output=True, text=True, timeout=30, cwd=writable)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data['127.0.0.1'] == data['::1'] == 'owned'
        assert all(data[str(port)] == 1 for _, port in protected)
        assert data['claim:127.0.0.1'] == data['claim:0.0.0.0'] == data['external'] == 1
        assert (writable / 'write-proof').read_text().startswith('filesystem write')
        assert all(data[key] == 1 for key in ('udp-bind:127.0.0.1', 'udp-bind:::1',
                                              'udp-send:127.0.0.1', 'udp-send:::1', 'uds-bind'))
    finally:
        for listener in owned:
            listener.close()
        for peer in udp_peers.values():
            peer.close()
