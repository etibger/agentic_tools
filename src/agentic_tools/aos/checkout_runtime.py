"""Small checkout probes and endpoint observations; no workflow or budget ledger."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time

from . import checkout_boundary, native_paths, native_processes, native_network
from .store import Rejected, canonical, digest, now


def observe_resources(recipe: dict, *, deadline: float | None = None) -> dict:
    """Observe owned source/generated/output bytes, not shared caches or peaks."""
    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise Rejected('checkout_observation', 'Endpoint observation exceeded the original deadline')
    check_deadline()
    generated = native_paths.generated_roots(recipe) if checkout_boundary.enabled(recipe) else ()
    totals = []
    for root in (Path(recipe['source']), Path(recipe['outputs'])):
        total, pending, seen = 0, [root], set()
        while pending:
            check_deadline()
            directory = pending.pop()
            for path in directory.iterdir():
                if deadline is not None and time.monotonic() >= deadline:
                    raise Rejected('checkout_observation', 'Endpoint observation exceeded the original deadline')
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    identity = info.st_dev, info.st_ino
                    if identity not in seen:
                        total += info.st_size
                        seen.add(identity)
                elif stat.S_ISLNK(info.st_mode) and root == Path(recipe['source']) and checkout_boundary.enabled(recipe):
                    # Native artifact snapshots already retain build aliases
                    # without following them. Count the same objects here.
                    if not any(path.is_relative_to(base) for base in generated):
                        checkout_boundary.validate_source_link(recipe, path)
                    total += info.st_size
                else:
                    raise Rejected('checkout_observation', 'Owned endpoint contains a link or special file')
        totals.append(total)
    check_deadline()
    return {'observed_at': now(), 'owned_bytes': sum(totals), 'output_bytes': totals[1],
            'free_bytes': min(shutil.disk_usage(recipe[key]).free for key in ('source', 'outputs')),
            'scope': 'Owned source, generated files and outputs at this endpoint only; shared caches and transient peaks are not measured.'}


def enforce_endpoints(recipe: dict, before: dict, after: dict) -> dict:
    limits = recipe['checkout_policy']['endpoint_limits']
    values = [before.get('owned_bytes'), after.get('owned_bytes'), after.get('output_bytes'), after.get('free_bytes')]
    if any(type(value) is not int or value < 0 for value in values):
        raise Rejected('checkout_observation', 'Endpoint measurements are incomplete')
    growth = max(0, after['owned_bytes'] - before['owned_bytes'])
    failures = []
    if after['free_bytes'] < limits['minimum_free_bytes']:
        failures.append('available disk space is below the agreed minimum')
    if growth > limits['max_owned_growth_bytes']:
        failures.append('observed owned growth exceeds its allowance')
    if after['output_bytes'] > limits['max_output_bytes']:
        failures.append('observed output bytes exceed their allowance')
    receipt = {'before': before, 'after': after, 'observed_growth_bytes': growth,
               'limits': limits, 'passed': not failures, 'blockers': failures,
               'limitation': 'Endpoint checks do not establish or enforce transient peaks.'}
    if failures:
        error = Rejected('checkout_resource_limit', '; '.join(failures))
        error.receipt = receipt
        raise error
    return receipt


def merge_resources(previous: dict | None, current: dict) -> dict:
    """Endpoint facts are monotonic even when cancellation saves an older view."""
    if previous is None:
        return current
    if (not isinstance(current, dict) or previous.get('mode') != current.get('mode')
            or previous.get('baseline') != current.get('baseline')):
        raise Rejected('checkout_observation', 'The original endpoint scope/baseline cannot change')
    result = {**current, 'breach': previous.get('breach') or current.get('breach')}
    for key in ('observed_growth_high_water', 'output_high_water'):
        values = [previous.get(key), current.get(key)]
        if any(type(value) is not int or value < 0 for value in values):
            raise Rejected('checkout_observation', 'Invalid retained endpoint accounting')
        result[key] = max(values)
    return result


def _write(path: Path, value: dict) -> None:
    # Only the protected host-owned control directory may contain this journal.
    temporary = path.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _rows(*, deadline: float | None = None) -> dict:
    remaining = 2 if deadline is None else min(2, deadline-time.monotonic())
    if remaining <= 0:
        raise Rejected('checkout_deadline', 'The process observation deadline expired')
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid='],
                            capture_output=True, text=True, check=True, timeout=remaining)
    return {int(p): {'parent': int(parent), 'group': int(group), 'uid': int(uid)}
            for line in result.stdout.splitlines() if len(parts := line.split()) == 4
            for p, parent, group, uid in [parts]}


def _owned(owner: dict, retained: dict, *, allow_lost: bool = False, deadline: float | None = None) -> dict:
    current = native_processes.identity(owner['pid'])
    if not native_processes.same(owner, current):
        if allow_lost:
            return {pid: actual for pid, saved in retained.items()
                    if native_processes.same(saved, actual := native_processes.identity(pid))}
        raise Rejected('checkout_cleanup_unknown', 'Probe supervisor birth identity is no longer live')
    confirmed = {owner['pid']: current}
    changed = set()
    for pid, saved in retained.items():
        actual = native_processes.identity(pid)
        if native_processes.same(saved, actual):
            confirmed[pid] = actual
        elif actual is not None:
            changed.add(pid)
    rows = _rows(deadline=deadline)
    while True:
        added = False
        for pid, row in rows.items():
            if pid in confirmed or pid in changed:
                continue
            if row['parent'] not in confirmed and row['group'] != owner['pid']:
                continue
            actual = native_processes.identity(pid)
            if actual is None:
                continue
            parent = confirmed.get(actual['parent'])
            parent_live = parent is not None and native_processes.same(parent, native_processes.identity(parent['pid']))
            group_live = actual['group'] == owner['pid'] and native_processes.same(owner, native_processes.identity(owner['pid']))
            if parent_live or group_live:
                if actual['uid'] != owner['uid']:
                    raise Rejected('checkout_cleanup_unknown', 'Unexpected cross-user probe descendant')
                confirmed[pid] = actual
                added = True
        if not added:
            break
    return {pid: value for pid, value in confirmed.items() if pid != owner['pid']}


def _run_probe(argv, *, cwd, environment, control, deadline) -> dict:
    """Fixed nofork payload behind a retained supervisor and launch barrier."""
    from .codex_runtime import NATIVE_SUPERVISOR
    receipt_path = control / 'process.json'
    status, release = control / 'status.json', control / 'release'
    record = {'phase': 'not_started', 'argv_digest': digest(argv), 'started_at': now(),
              'deadline_monotonic': deadline, 'cleanup_verified': False,
              'limitation': 'Observed owners only; no exclusion of unobserved detached processes.'}
    _write(receipt_path, record)
    child = None
    owner = None
    retained = {}
    failure = None
    with (control / 'stdout.txt').open('x') as out, (control / 'stderr.txt').open('x') as err:
        try:
            if time.monotonic() >= deadline - 5:
                raise Rejected('checkout_deadline', 'No remaining probe execution and cleanup allowance')
            payload = {'argv': argv, 'cwd': str(cwd), 'status': str(status), 'release': str(release)}
            record['phase'] = 'launch_intent'
            _write(receipt_path, record)
            child = subprocess.Popen([sys.executable, '-I', '-c', NATIVE_SUPERVISOR, canonical(payload)],
                                     cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                     stdout=out, stderr=err, start_new_session=True)
            owner = native_processes.identity(child.pid)
            if not owner or owner['group'] != child.pid or owner['uid'] != os.getuid():
                raise Rejected('checkout_cleanup_unknown', 'No exact probe supervisor identity')
            record.update(phase='running', owner=owner)
            _write(receipt_path, record)
            child.stdin.write(b'G')
            child.stdin.close()
            while not status.is_file():
                retained.update(_owned(owner, retained, deadline=deadline))
                record['observed'] = list(retained.values())
                _write(receipt_path, record)
                if child.poll() is not None:
                    raise Rejected('checkout_cleanup_unknown', 'Probe supervisor exited before its child receipt')
                if time.monotonic() >= deadline - 5:
                    raise Rejected('checkout_deadline', 'Probe reached its original execution deadline')
                time.sleep(.05)
        except BaseException as error:
            failure = str(error)
        finally:
            try:
                record['observed'] = list(retained.values())
                if child is None and record['phase'] == 'not_started':
                    record['cleanup_verified'] = True
                    record['no_launch'] = True
                if child is not None and child.stdin and not child.stdin.closed:
                    child.stdin.close()  # No launch byte was sent on an identity failure.
                if child is not None and owner is None:
                    child.wait(timeout=max(.001, min(2, deadline - time.monotonic())))
                    record.update(owner_exit=child.returncode, cleanup_verified=child.returncode == 0)
                elif child is not None:
                    owner_live = native_processes.same(owner, native_processes.identity(owner['pid']))
                    members = _owned(owner, retained, allow_lost=True, deadline=deadline)
                    retained.update(members)
                    for pid, saved in members.items():
                        if native_processes.same(saved, native_processes.identity(pid)):
                            try:
                                os.kill(pid, signal.SIGINT)
                            except ProcessLookupError:
                                pass
                    while members and time.monotonic() < deadline - 2:
                        time.sleep(.05)
                        members = _owned(owner, retained, allow_lost=True, deadline=deadline)
                        retained.update(members)
                    record['observed'] = list(retained.values())
                    if not owner_live:
                        raise Rejected('checkout_cleanup_unknown', 'Lost supervisor; retained children reconciled but unobserved effects remain unknown')
                    if members or not status.is_file():
                        raise Rejected('checkout_cleanup_unknown', 'Probe children or exit receipt remain unresolved')
                    release.write_text('owned children observed stopped\n')
                    child.wait(timeout=max(.001, min(2, deadline - time.monotonic())))
                    group_empty = not any(row['group'] == owner['pid'] for row in _rows(deadline=deadline).values())
                    absent = not native_processes.same(owner, native_processes.identity(owner['pid']))
                    known_absent = all(not native_processes.same(saved, native_processes.identity(pid)) for pid, saved in retained.items())
                    record.update(owner_exit=child.returncode, owner_absent=absent, group_empty=group_empty,
                                  cleanup_verified=child.returncode == 0 and absent and known_absent and group_empty,
                                  child=json.loads(status.read_text()), observed=list(retained.values()))
            except BaseException as error:
                failure = failure or str(error)
                record['cleanup_error'] = str(error)
    record['observed'] = list(retained.values())
    record.update(phase='stopped' if record['cleanup_verified'] else 'cleanup_unknown', error=failure, finished_at=now())
    _write(receipt_path, record)
    if failure or not record['cleanup_verified'] or record.get('child', {}).get('exit_code') != 0:
        error = Rejected('checkout_probe', 'Permission probe failed; retained evidence: ' + str(receipt_path))
        error.cleanup_verified = record['cleanup_verified']
        error.receipt_path = str(receipt_path)
        raise error
    return record


_PROBE = r'''import json,os,socket,sys
from pathlib import Path
p=json.loads(sys.argv[1]); checks={}
def denied(fn):
 try: fn()
 except PermissionError:return True
 return False
def opened(path,flags):
 fd=os.open(path,flags);os.close(fd)
def canary(path):
 with Path(path).open('x') as stream:stream.write('bounded probe')
 Path(path).unlink();return True
checks['state_read_denied']=denied(lambda:list(Path(p['state']).iterdir()))
checks['state_write_denied']=denied(lambda:opened(p['state']+'/forbidden-probe',os.O_WRONLY|os.O_CREAT))
checks['control_read_denied']=denied(lambda:opened(p['control'],os.O_RDONLY))
checks['credentials_denied']=denied(lambda:list(Path(p['credentials']).iterdir()))
checks['credential_config_denied']=denied(lambda:opened(p['credentials']+'/config.toml',os.O_RDONLY))
checks['output_write']=canary(p['output_canary'])
if p['role']=='writer':checks['source_access']=canary(p['source_canary'])
else:checks['source_access']=denied(lambda:canary(p['source_canary']))
for index,path in enumerate(p['instructions']):
 checks['instruction_read_'+str(index)]=Path(path).read_bytes() is not None
 checks['instruction_write_denied_'+str(index)]=denied(lambda path=path:opened(path,os.O_WRONLY))
with socket.socket() as connection:
 connection.settimeout(.2)
 checks['network_denied']=denied(lambda:connection.connect(('127.0.0.1',8443)))
print(json.dumps(checks,sort_keys=True),flush=True)
raise SystemExit(0 if all(value is True for value in checks.values()) else 1)
'''


def _probe(recipe: dict, *, state_dir: Path, output: Path, control: Path, role: str,
          after_startup: bool = False, deadline: float, native_profile: str | None = None,
          preparation=None) -> dict:
    from .codex_runtime import CodexRuntime, PROFILE, bundled_codex_path
    from . import instructions
    if role not in ('writer', 'assessor', 'native'):
        raise Rejected('checkout_probe', 'Unknown permission-probe role')
    state_dir, output, control = Path(state_dir).resolve(), Path(output).resolve(), Path(control).resolve()
    runtime = object.__new__(CodexRuntime)
    runtime.directory = state_dir
    scoped = copy.deepcopy(recipe)
    guards = scoped['checkout_policy'].get('protected_roots', [])
    scoped['checkout_policy']['protected_roots'] = sorted(set(guards) | {str(state_dir), str(control)})
    scoped = checkout_boundary.scoped_outputs(scoped, output, prepare=True)
    overrides, profile = runtime._profile(scoped, assessment=role != 'writer', after_startup=after_startup, native=role == 'native')
    name = 'probe-' + role + ('-after' if after_startup else '-before')
    directory = control / name
    if directory.exists():
        raise Rejected('checkout_probe', 'This permission probe already has retained evidence; no implicit rerun')
    directory.mkdir(parents=True, mode=0o700)
    protected = directory / 'canary'
    protected.write_text('private control probe\n')
    canary = name + '-' + digest(str(directory))[7:19]
    paths = instructions.validate(recipe['instruction_manifest'], Path(recipe['source']), state_dir, after_startup=after_startup)
    # Native checks deliberately have no access to account-level instruction files.
    if role == 'native':
        paths = [path for path in paths if not Path(path).is_relative_to(Path.home() / '.codex')]
    payload = {'state': str(state_dir), 'control': str(protected), 'credentials': str(Path.home() / '.codex'),
               'output_canary': str(output / canary), 'source_canary': str(Path(recipe['source']) / canary),
               'role': role, 'instructions': paths}
    command = [str(Path(recipe['probe_interpreter']).resolve()), '-I', '-S', '-c', _PROBE, canonical(payload)]
    local_observations = None
    if role == 'native':
        if native_profile is None:
            policy = directory / 'native.sb'
            if scoped['checkout_policy'].get('native_local_tests'):
                temporary = checkout_boundary.observe_temporary(scoped, deadline=deadline)
                network = native_network.observe(scoped, deadline=deadline)
                policy.write_text(checkout_boundary.render_native(scoped, state_dir,
                    temporary_observation=temporary, network_observation=network))
                local_observations = {'temporary': temporary, 'network': network}
                _write(directory / 'local-test-boundary.json', local_observations)
                native_network.verify_observation(scoped, network, deadline=deadline)
                checkout_boundary.verify_temporary_observation(scoped, temporary, deadline=deadline)
            else:
                policy.write_text(checkout_boundary.render_native(scoped, state_dir))
            native_profile = str(policy)
        argv = ['/usr/bin/sandbox-exec', '-f', native_profile, *command]
    else:
        argv = [str(bundled_codex_path())]
        for override in overrides:
            argv.extend(['-c', override])
        argv.extend(['sandbox', '-P', PROFILE, '--include-managed-config', '-C', recipe['source'], *command])
    if preparation is not None:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise Rejected('checkout_deadline', 'The original permission-probe deadline expired before launch')
        run = preparation.run_probe(argv, code=_PROBE, cwd=recipe['source'], capture_output=True,
                                    text=True, env=checkout_boundary.environment(scoped, native=True),
                                    timeout=min(15, remaining))
        result = {'exit_code': run.returncode, 'checks': json.loads(run.stdout), 'cleanup': dict(preparation.probe)}
    else:
        ownership = _run_probe(argv, cwd=Path(recipe['source']), environment=checkout_boundary.environment(scoped, native=role=='native'),
                               control=directory, deadline=min(deadline, time.monotonic()+20))
        raw = (directory / 'stdout.txt').read_bytes()
        if len(raw) > 65536:
            raise Rejected('checkout_probe', 'Probe output exceeds its bounded result shape')
        result = {'exit_code': ownership['child']['exit_code'], 'checks': json.loads(raw), 'cleanup': ownership}
    expected = {'state_read_denied','state_write_denied','control_read_denied','credentials_denied',
                'credential_config_denied','output_write','source_access','network_denied'}
    expected.update('instruction_'+kind+'_'+str(index) for index in range(len(paths)) for kind in ('read','write_denied'))
    if (result['exit_code'] != 0 or set(result['checks']) != expected
            or any(value is not True for value in result['checks'].values())
            or protected.read_text() != 'private control probe\n'
            or Path(payload['source_canary']).exists() or Path(payload['output_canary']).exists()):
        raise Rejected('checkout_probe', 'Effective permission checks failed or left changed canaries')
    result.update(observed_at=now(), role=role, model_calls=0, path=str(directory))
    if local_observations is not None:
        result['local_test_boundary'] = local_observations
    _write(directory / 'result.json', result)
    return result


def probe(recipe: dict, *, state_dir: Path, output: Path, control: Path, role: str,
          after_startup: bool = False, deadline: float, native_profile: str | None = None,
          preparation=None) -> dict:
    """Expose cleanup truth on every failure; transport closure is not cleanup."""
    try:
        return _probe(recipe, state_dir=state_dir, output=output, control=control, role=role,
                      after_startup=after_startup, deadline=deadline, native_profile=native_profile,
                      preparation=preparation)
    except BaseException as error:
        if not hasattr(error, 'cleanup_verified'):
            path = Path(control).resolve() / ('probe-' + role + ('-after' if after_startup else '-before')) / 'process.json'
            error.receipt_path = str(path)
            if preparation is not None:
                error.cleanup_verified = preparation.probe.get('state') in ('not_started', 'unqualified', 'reaped')
            elif path.is_file():
                try:
                    error.cleanup_verified = json.loads(path.read_text()).get('cleanup_verified') is True
                except (OSError, ValueError):
                    error.cleanup_verified = False
            else:
                # The fixed runner writes its intent before Popen. Absence is
                # known no-launch only within this live invocation.
                error.cleanup_verified = True
        raise
