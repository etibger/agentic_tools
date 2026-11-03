"""Read-only proof for one stopped intercheck observation failure.

The reviewed packet attributes the historical producer. Its healthy final
sample and strict next-check preparation establish no owned workload after
that anchor, not when the failed scan began or continuous cache stability.
No validation result grants work, time, cleanup or approval authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path

from agentic_tools.check_adapters import identities
from . import checks, native_admission_recovery, native_journal, native_paths
from . import native_preparation, native_processes, native_renderer, resource_limits, technical_control
from .codex_runtime import CodexRuntime
from .store import Rejected, digest

FORMAT = 'aos-intercheck-observation-forensics-v1'
SEMANTICS = 'post-census-intercheck-v1'
MAX_EVIDENCE = 8 * 1024 * 1024
MAX_RECORD = 64 * 1024 * 1024
MAX_PROOF = 512 * 1024
SUPPORTED_SOURCES = {'aos/__init__.py': '720340ec2f1530443fbff3f78fd4a06af08880d478752309c6c70881a6cb1f5e',
 'aos/approval_web.py': '8b0f79e04aa3bb7c2052c2997b220ae84fe9806fe884aefa1080730247266078',
 'aos/archive.py': '9665a8370a5bb6757180987bbe831b6c0a315a0c16eb1cf02e83114c3a8079d7',
 'aos/backlog.py': 'b25a1f9adb251e146d435f03b5881eb69b41504131872fb77b432a7a76220306',
 'aos/budgets.py': '48c9fdab46e733418487fc9cb1d8908de377972159588f5f68b736bf0978877f',
 'aos/checks.py': '27e0b2a134696177b7ebf98515dca89b4bc3667e924a90e4b760f8cedd1c3a26',
 'aos/cli.py': '7d4a8203b0fe4b6e83a37e61e9f42a03dbb3e24bdd2464133d9f639d025326e9',
 'aos/codex_runtime.py': 'ad14cb54b916fcef7c9046c21410c430fe7670c826d90bb5066e387c4125afef',
 'aos/coordinator.py': '5e36bebbf2c1c4eab763cf9302dd9a6df4d7f41f88c675f34b6a84c04e64f42c',
 'aos/coordinator_mcp.py': '0b6c48e7476a98ddf6ad0e38082b458e9c5ef65a2faedcfdcd5d71fdbce07ad4',
 'aos/dashboard_service.py': 'c870c427c0c1096b312621073009948c19d989ce9d696f11a714bfdd25402cf0',
 'aos/decision_wait.py': '7d2a610339559c1445406f836b865cd3ed8534167474676da7087a6643d031ef',
 'aos/instructions.py': '19188c84dc7ad75d820a94c07bbdb183b623404c58ea28a3b89a9ae9fa577448',
 'aos/interface.py': 'd2e7db11d05925b69bd8c3f1a5add9f5d6d404a2c61e3392993da21ba7ddce0d',
 'aos/legacy_recovery.py': '1712f283ee1bc7d8b4aa3b965e4125636538f7b65ae8cdc56380968e2d7a678e',
 'aos/lifecycle.py': '4b72f84ca237e230ac0ee0f9edd90332d9b2e03fcdb3d3cde9af9519cc489b28',
 'aos/native_admission_recovery.py': '91f71eb6edebecfaa8a60829b7672d6013462f450dcc1fdedc563bcbcbba1d0f',
 'aos/native_cleanup_recovery.py': '7efccdd0763b4814026c1ca96f8d03db588f382c750c8947eb8182080d5093ab',
 'aos/native_journal.py': 'dad78dc2390b84fb44923e5addb9ad953eda94cc0d49b2f9e38e24201df2c1fa',
 'aos/native_network.py': 'ef7abb267e9c2a1b494af1758d422b3aeb002225b60814ae9930492e7bc6681c',
 'aos/native_paths.py': '44fb8f5d6d96d48ecebb6af5caa6d3013469262b2462bf203f9ae2ae5a060715',
 'aos/native_pgrep.py': 'eb579d09bcd2406ea8245617613dba5a1c050a5b027d982110c68ec177962e61',
 'aos/native_preparation.py': '13266e8b4334d261f9aee9b1bc843f2af1a806da144b203af8c7a4a4d1b0ba08',
 'aos/native_processes.py': '4143d982e49a17f2743f026c02a3788b41b88a5f4161433f1e23f99c56c3293d',
 'aos/native_qualification.py': 'c3713440b2333c21afe1c700154ebd9ba1a5b826831237277111c1e2a13fa196',
 'aos/native_quiescence.py': 'ce26c398d6e4f1cbb79efddd3fdfad9530f313a8c8218a51295e18cf4fc774ba',
 'aos/native_renderer.py': '2e4620cbfd1cef5ed5f9b499967d3860814d6c0f814bb2c6c4f626f73f86800d',
 'aos/native_sandbox.py': 'ba9a0146d9d2c50db184c59308dd7f0d0f1cfc5ab4ca26e84a7d070d099fc1b8',
 'aos/native_temporary.py': '7cda8c2ae97c5b5ce738bd303ae0cda0589cadd608b3bd783d5a7fac29806177',
 'aos/projection.py': 'dd96d3748a2bfb7011eefe1e9efd1a57117d7d951affbd369aba92aa3c08a6d5',
 'aos/resource_limits.py': '7348091ca65cdadbc0271801c79c5e725aab92fbe5d875be9812dfb4a18d1695',
 'aos/runtime.py': '6897d3f024bea9141043b341b48594bc5b19b8094c14fa5354da0f3d3d4b5eef',
 'aos/store.py': '9822de9ec75f38b3c48ea9f31c528ca5cc10c924e17d715f6e8fa11be3181a8d',
 'aos/technical_control.py': '7b94defed7c0acac7227b20c57acbc48e22b87fcb1cbf71495ab9cda93498d71',
 'aos/worker.py': '09fc147a2ed4d6b5cea03a14131867b0f141b26c0f7f3ad7cfcc7b47fd03e4c4',
 'check_adapters/__init__.py': '0c36599816ef9ec0b16ac549f456d06d1ffbec0aeb8811fcb42d3781c61ef8a8',
 'check_adapters/cargo_test.py': 'f1c833069af28429450ca7b0d5d33dae166455bffe772e90fd989d1b63157ba7',
 'check_adapters/native_build.py': '06e203a5d1f42a219944d17c8a4f160471c511bc77289dce50470f3918ce9784',
 'check_adapters/native_tests.py': '01742927e701bbf5d7525d9e7cc6cf9f977988410d7028cb46149a932ca9b47b',
 'check_adapters/pytest.py': 'd9ecf5f572e290eddc4818be6486f69ed5eaaf53ec3dc66b689da6c41a3c3401'}
CAPS = {'output_roots': 'max_output_growth_bytes', 'source_evidence_roots': 'max_source_evidence_growth_bytes'}
FAILURE_FIELDS = {'reason': 'observation_failed', 'error': 'Rejected', 'error_code': 'resource_observation',
                  'error_message': 'Resource observation exceeded its bounded scan'}


def _reject(message):
    raise Rejected('technical_intercheck', message)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _reject('Duplicate evidence keys cannot establish an exact intercheck boundary')
        value[key] = item
    return value


def _parse(raw, limit=MAX_RECORD):
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        _reject('Retained evidence exceeds its bounded JSON size')
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            _reject('Nonfinite JSON evidence is unsupported')
        return number
    return json.loads(raw, object_pairs_hook=_unique, parse_float=finite,
                      parse_constant=lambda _: _reject('Nonfinite JSON evidence is unsupported'))


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or path.resolve() != path
            or any(part.is_symlink() for part in (path, *path.parents))):
        _reject('Evidence paths must be canonical without aliases')
    return path


def _identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
            'mode': stat.S_IMODE(info.st_mode), 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def _file(value, *, limit=MAX_EVIDENCE, private=False):
    path = _path(value)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_size > limit or before.st_mode & 0o022
                or private and stat.S_IMODE(before.st_mode) != 0o600):
            _reject('Evidence must be an owned bounded non-aliased regular file')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > limit or _identity(before) != _identity(after)
            or _identity(after) != _identity(path.stat(follow_symlinks=False))):
        _reject('An evidence file changed during its read')
    return raw, _identity(after)


def _json_artifact(pin):
    if not isinstance(pin, dict) or set(pin) != {'path', 'sha256'}:
        _reject('Exact artifact path and hash are required')
    raw, _ = _file(pin['path'])
    if hashlib.sha256(raw).hexdigest() != pin['sha256']:
        _reject('A reviewed artifact hash changed')
    return _parse(raw.decode(), MAX_EVIDENCE)


def _pinned_file(path, sha256, *, limit=MAX_RECORD):
    raw, identity = _file(path, limit=limit)
    if hashlib.sha256(raw).hexdigest() != sha256:
        _reject('A retained artifact hash changed')
    return raw, {'sha256': sha256, 'identity': identity}


def _absent(path):
    path = _path(str(path))
    try:
        path.lstat()
    except FileNotFoundError:
        return str(path)
    _reject('A preparation-only check acquired a launch artifact')


def evidence(path, sha256):
    try:
        packet = _json_artifact({'path': str(path), 'sha256': sha256})
        if not isinstance(packet, dict) or packet.get('format') != FORMAT or packet.get('read_only') is not True:
            _reject('The exact sealed intercheck forensic packet is required')
        return packet
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise Rejected('technical_intercheck', 'The intercheck evidence packet is unavailable or malformed') from error


def sidecars(database, resource_ids):
    """Capture absence or exact current identities without changing any latch."""
    result = {}
    if len(set(resource_ids)) != len(resource_ids) or len(resource_ids) > 96:
        _reject('Resource latch inventory is duplicate or unbounded')
    for key in sorted(resource_ids):
        path = resource_limits._failure_path(database, key)
        _path(str(path))
        try:
            raw, identity = _file(str(path), limit=4096, private=True)
        except FileNotFoundError:
            if path.is_symlink():
                _reject('An absent resource latch became an alias')
            result[key] = {'path': str(path), 'exists': False}
        else:
            result[key] = {'path': str(path), 'exists': True, 'identity': identity,
                           'sha256': hashlib.sha256(raw).hexdigest(), 'content': _parse(raw.decode(), 4096)}
    return result


def _source(packet):
    saved = packet['execution_source']
    manifest, verified = (_json_artifact(saved[key]) for key in ('manifest', 'verification'))
    files = {name.removeprefix('src/agentic_tools/'): value for name, value in manifest['files'].items()
             if name.startswith(('src/agentic_tools/aos/', 'src/agentic_tools/check_adapters/'))}
    if (saved.get('semantics') != SEMANTICS or files != SUPPORTED_SOURCES
            or saved.get('source_digest') != digest(files)
            or manifest.get('version', {}).get('source_digest', digest(files)) != digest(files)
            or verified.get('source_digest') != digest(files) or verified.get('source_unchanged') is not True
            or type(verified.get('pytest', {}).get('exit_code')) is not int or verified['pytest']['exit_code'] != 0):
        _reject('The exact supported producer and matching source-stable verification are required')
    bindings = {}
    for key in ('manifest', 'verification'):
        pin = saved[key]
        _, bindings[pin['path']] = _pinned_file(pin['path'], pin['sha256'], limit=MAX_EVIDENCE)
    return saved['source_digest'], bindings


def _capture(q, record, resource_ids):
    with q.runtime._db() as db:
        if not db.in_transaction:
            db.execute('BEGIN')
        native_journal.verify(db)
        size = db.execute('SELECT length(CAST(record AS BLOB)) FROM technical_qualification WHERE id=?',
                          (q.qualification_id,)).fetchone()
        if not size or size[0] > 256 * 1024 * 1024:
            _reject('The retained archive is missing or exceeds its read bound')
        saved = db.execute('SELECT record FROM technical_qualification WHERE id=?', (q.qualification_id,)).fetchone()
        if not saved or _parse(saved[0], 256 * 1024 * 1024) != record:
            _reject('The stopped archive differs from the supplied snapshot')
        snapshot = {'archive_raw': saved[0],
                    'archive_revision': technical_control.revision(db, q.qualification_id),
                    'native_revision': native_journal.revision(db), 'native_rows': {}, 'resource_guards': {}}
        count, maximum, total = db.execute('SELECT count(*),max(length(CAST(record AS BLOB))),'
                                          'sum(length(CAST(record AS BLOB))) FROM native_check').fetchone()
        if count > 1024 or (maximum or 0) > MAX_RECORD or (total or 0) > 1024 * 1024 * 1024:
            _reject('Native inventory exceeds its read bound')
        for key, raw in db.execute('SELECT id,record FROM native_check ORDER BY id'):
            snapshot['native_rows'][key] = raw
        guard_bytes = 0
        for key in resource_ids:
            size = db.execute('SELECT length(CAST(record AS BLOB)) FROM resource_limit WHERE work_id=?', (key,)).fetchone()
            if not size or size[0] > MAX_RECORD:
                _reject('A required resource guard is missing or exceeds its read bound')
            guard_bytes += size[0]
            if guard_bytes > 1024 * 1024 * 1024:
                _reject('The guard inventory exceeds its aggregate read bound')
            found = db.execute('SELECT record FROM resource_limit WHERE work_id=?', (key,)).fetchone()
            if not found:
                _reject('A required historical resource guard is missing')
            snapshot['resource_guards'][key] = found[0]
    return snapshot


def _raw_map(raws, maximum):
    if not isinstance(raws, dict) or not 1 <= len(raws) <= maximum:
        _reject('An exact bounded record inventory is required')
    result, hashes, total = {}, {}, 0
    for key, raw in raws.items():
        if not isinstance(key, str):
            _reject('A record identity is malformed')
        value = _parse(raw)
        total += len(raw.encode())
        if total > 1024 * 1024 * 1024:
            _reject('The record inventory exceeds its aggregate read bound')
        result[key], hashes[key] = value, hashlib.sha256(raw.encode()).hexdigest()
    return result, hashes


def _safe_growth(guard):
    if (guard['active_attempt'] is not None or guard['scope_digest'] != digest(guard['scope'])
            or set(guard['high_water_growth_bytes']) != set(CAPS)):
        _reject('An inactive exact resource scope and high-water record are required')
    for group, cap in CAPS.items():
        value, limit = guard['high_water_growth_bytes'][group], guard['scope'][cap]
        if type(value) is not int or value < 0 or type(limit) is not int or limit < 0 or value > limit:
            _reject('A recorded byte violation cannot be acknowledged as an intercheck failure')
    free, minimum = guard['last_observation']['available_bytes'], guard['scope']['min_free_bytes']
    if (type(minimum) is not int or minimum < 0 or not isinstance(free, dict) or not free
            or any(type(value) is not int or value < minimum for value in free.values())):
        _reject('A recorded free-space violation remains unresolved')


def _scope_matches(recipe, bound):
    """Bind saved contract data without walking any current resource root."""
    current = dict(recipe['resource_limits'])
    for key in CAPS:
        current[key] = sorted(current[key])
    expected = {key: value for key, value in bound.items()
                if key not in ('source', 'generated_roots', 'temporary_paths', 'scan_seconds',
                               'max_observation_gap_seconds')}
    generated = sorted(entry['path'] for entry in recipe.get('native_write_roots', [])
                       if entry['kind'] == 'generated')
    if (current != expected or bound['source'] != recipe['source'] or bound['generated_roots'] != generated
            or bound['temporary_paths'] != recipe.get('native_sandbox', {}).get('temporary_paths', [])
            or type(bound['sample_interval_seconds']) is not int or not 1 <= bound['sample_interval_seconds'] <= 10
            or bound['scan_seconds'] != 10 or bound['max_observation_gap_seconds'] != bound['sample_interval_seconds'] + 12):
        _reject('The retained guard no longer matches its exact original recipe scope')


def _ack_sidecar(state, resource_id, guard, dispositions):
    historical = []
    for item in dispositions:
        acknowledgement = item['acknowledgment']
        saved = acknowledgement.get('failure_sidecar', acknowledgement.get('proof', {}).get('failure_sidecar'))
        failures = item.get('historical_breaches', [item.get('historical_breach')])
        if saved and saved.get('content', {}).get('work_id') == resource_id:
            historical.append((item, saved, failures))
    if state['exists'] is False:
        if historical:
            _reject('An acknowledged resource failure latch disappeared')
        return dict(guard['high_water_growth_bytes'])
    content = state['content']
    if (set(content) != {'work_id', 'scope_digest', 'breach', 'high_water_growth_bytes'}
            or content['work_id'] != resource_id or content['scope_digest'] != guard['scope_digest']
            or set(content['high_water_growth_bytes']) != set(CAPS)):
        _reject('A resource failure latch names a different scope')
    matching = [item for item, saved, failures in historical
                if all(saved.get(key) == state[key] for key in ('path', 'sha256', 'content'))
                and content['breach'] in failures]
    if len(matching) != 1 or len(historical) != 1:
        _reject('A new or unacknowledged resource failure latch cannot be ignored')
    floors = {}
    for group, cap in CAPS.items():
        value = content['high_water_growth_bytes'][group]
        if (type(value) is not int or value < 0 or value > guard['scope'][cap]
                or matching[0]['resource_receipt']['high_water_growth_bytes'][group] < value):
            _reject('A retained resource latch floor was not carried by its acknowledgment')
        floors[group] = max(value, guard['high_water_growth_bytes'][group])
    return floors


def _planned(phase):
    recipe, planned, bindings = phase['recipe'], {}, identities()
    if (not isinstance(recipe['checks'], list) or not 2 <= len(recipe['checks']) <= 64
            or phase['output'] != str(Path(recipe['outputs']) / ('technical-' + phase['id']))):
        _reject('A bounded ordered recipe with an exact phase output is required')
    for index, check in enumerate(recipe['checks']):
        name = check['name']
        if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name:
            _reject('A planned check name is malformed')
        output = _path(str(Path(phase['output']) / name))
        adapter = checks.adapter_for(check)
        if recipe.get('_adapter_bindings', {}).get(adapter.ADAPTER_ID) != bindings[adapter.ADAPTER_ID]:
            _reject('Historical command expansion differs from its bound adapter')
        command = adapter.command([part.replace('{output}', recipe['outputs']) for part in check['argv']], output)
        key = 'nc-' + digest([str(output), command, phase['recipe_digest']])[7:39]
        if key in planned or any(value['name'] == name for value in planned.values()):
            _reject('The planned checks have duplicate identities')
        planned[key] = {'index': index, 'name': name, 'output': str(output), 'command': command}
    return planned


def _artifact_hashes(row, packet_artifacts):
    receipt, boundary = row['receipt'], row['native_sandbox_boundary']
    expected = {**receipt['raw_outputs'], receipt['output']: receipt['output_sha256'], boundary['path']: boundary['sha256']}
    status_raw, status_identity = _file(row['status'], limit=MAX_RECORD)
    expected[row['status']] = hashlib.sha256(status_raw).hexdigest()
    if any(packet_artifacts.get(path) != value for path, value in expected.items()):
        _reject('A completed check artifact differs from the sealed packet')
    bindings = {row['status']: {'sha256': expected[row['status']], 'identity': status_identity}}
    for path, sha256 in expected.items():
        if path != row['status']:
            _, bindings[path] = _pinned_file(path, sha256)
    CodexRuntime._verify_check_receipt(receipt)
    if CodexRuntime.verify_completed_native(row) != row:
        _reject('Completed native verification changed the retained receipt')
    final_raw, final_identity = _file(row['status'], limit=MAX_RECORD)
    if final_raw != status_raw or final_identity != status_identity:
        _reject('A native status changed during verification')
    return _parse(status_raw.decode()), expected, bindings


def _recheck_sidecars(proof):
    states = proof['sidecars']
    if not isinstance(states, dict) or not 1 <= len(states) <= 96:
        _reject('The retained sidecar fence is malformed')
    for state in states.values():
        if state['exists'] is False:
            _absent(state['path'])
        elif state['exists'] is True:
            raw, identity = _file(state['path'], limit=4096, private=True)
            if (identity != state['identity'] or hashlib.sha256(raw).hexdigest() != state['sha256']
                    or _parse(raw.decode(), 4096) != state['content']):
                _reject('A resource latch changed before the recovery commit')
        else:
            _reject('A retained sidecar existence fence is malformed')


def recheck_sidecars(proof):
    """Small final latch fence; bounded to 96 private 4 KiB files."""
    try:
        _recheck_sidecars(proof)
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise Rejected('technical_intercheck', 'A resource latch fence is unavailable') from error


def recheck(proof):
    """Rehash retained artifacts outside the writer; never scan inventories."""
    try:
        if len(json.dumps(proof, ensure_ascii=False).encode()) > MAX_PROOF:
            _reject('The compact recovery proof exceeds its bound')
        artifacts = proof['artifacts']
        if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 512:
            _reject('The exact artifact fence is malformed')
        for path, binding in artifacts.items():
            _, current = _pinned_file(path, binding['sha256'])
            if current != binding:
                _reject('A retained artifact identity changed before recovery')
        absent = proof['absent_artifacts']
        if not isinstance(absent, list) or len(absent) > 512 or len(absent) != len(set(absent)):
            _reject('The exact absent artifact fence is malformed')
        for path in absent:
            _absent(path)
        _recheck_sidecars(proof)
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise Rejected('technical_intercheck', 'A retained recovery artifact fence is unavailable') from error


def validate(q, record, phase, guard, packet, *, control, snapshot=None):
    """Validate a caller-captured stopped snapshot; mutation must fence it again."""
    try:
        return _validate(q, record, phase, guard, packet, control=control, snapshot=snapshot)
    except Rejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        raise Rejected('technical_intercheck', 'Incomplete or changed intercheck evidence') from error


def _validate(q, record, phase, guard, packet, *, control, snapshot):
    from .native_qualification import _acknowledged, _dispositions
    dispositions = _dispositions(record)
    phases = record['phases']
    if (not isinstance(phases, list) or not 1 <= len(phases) <= 32
            or len({p['id'] for p in phases}) != len(phases)
            or len({p['resource_id'] for p in phases}) != len(phases)
            or len(dispositions) > 32
            or any(len({item[key] for item in dispositions}) != len(dispositions)
                   for key in ('phase_id', 'operation_id', 'resource_id'))
            or {item['resource_id'] for item in dispositions} & {p['resource_id'] for p in phases}):
        _reject('The bounded phase and prior disposition identities must be unique')
    if (packet.get('format') != FORMAT or packet.get('read_only') is not True
            or packet.get('qualification_id') != q.qualification_id or packet.get('phase_id') != phase['id']
            or packet.get('qualification_digest') != digest(record) or packet.get('phase_digest') != digest(phase)
            or packet.get('control_digest') != digest(control)
            or packet.get('source_baseline_digest') != digest(record['source_baseline'])
            or packet.get('budget_before') != record['budget_before']
            or phase['recipe_digest'] != digest(phase['recipe'])
            or record['budget_paths'] != list(q.budget_paths)):
        _reject('The exact qualification, phase, recipe or protected baselines changed')
    if (record.get('export') or record['active_phase'] is not None or record['phases'][-1] != phase
            or phase['receipt'].get('status') != 'blocked' or type(phase['receipt'].get('model_calls')) is not int
            or phase['receipt']['model_calls'] != 0 or any(item['phase_id'] == phase['id'] for item in dispositions)
            or any(p['state'] != 'stopped' or p.get('receipt', {}).get('stopped') is not True
                   or p['receipt'].get('source_unchanged') is not True
                   or p['receipt'].get('product_budget_unchanged') is not True for p in record['phases'])):
        _reject('Only a latest stopped blocked technical phase without another disposition is eligible')
    if (not isinstance(control, dict) or control.get('format') != technical_control.FORMAT
            or control.get('qualification_id') != q.qualification_id or control.get('active') is not False
            or control.get('archive_binding') != technical_control.binding(phase)
            or any(control['phase'].get(key) != phase.get(key)
                   for key in (*technical_control.IMMUTABLE, *technical_control.MUTABLE))):
        _reject('Exact inactive control and archival completion must agree')
    native_admission_recovery._owner(phase)
    producer, producer_artifacts = _source(packet)
    failure = guard['breach']
    if (not isinstance(failure, dict) or set(failure) != {*FAILURE_FIELDS, 'observed_at'}
            or any(failure.get(key) != value for key, value in FAILURE_FIELDS.items())
            or type(failure['observed_at']) not in (int, float) or not math.isfinite(failure['observed_at'])
            or packet.get('resource_guard_digest') != digest(guard)
            or packet.get('guard_baseline_digest') != digest(guard['baseline'])
            or packet.get('guard_scope_digest') != guard['scope_digest']):
        _reject('Only the exact retained bounded-scan observation failure is eligible')
    failures = [record['breach'], *record.get('unresolved_failures', [])]
    if failure not in failures or any(value and value != failure and not _acknowledged(record, value) for value in failures):
        _reject('Another unresolved observation failure remains')
    prior = [{'operation_id': item['operation_id'], 'phase_id': item['phase_id'],
              'acknowledgment_digest': digest(item['acknowledgment']), 'resource_id': item['resource_id'],
              'resource_receipt': item['resource_receipt']} for item in dispositions]
    if (packet.get('prior_dispositions') != prior or packet.get('prior_charged_growth_bytes') != record['charged_growth_bytes']
            or set(record['charged_growth_bytes']) != set(CAPS)
            or any(type(value) is not int or value < 0 for value in record['charged_growth_bytes'].values())):
        _reject('Earlier dispositions or charged resource floors changed')
    resource_ids = {p['resource_id'] for p in record['phases']} | {item['resource_id'] for item in dispositions}
    snapshot = _capture(q, record, sorted(resource_ids)) if snapshot is None else snapshot
    if (set(snapshot) != {'archive_raw', 'archive_revision', 'native_revision', 'native_rows', 'resource_guards'}
            or _parse(snapshot['archive_raw'], 256 * 1024 * 1024) != record
            or any(type(snapshot[key]) is not int or snapshot[key] < 0 or packet.get(key) != snapshot[key]
                   for key in ('archive_revision', 'native_revision'))):
        _reject('The stopped archive/native revision fence changed')
    rows, row_hashes = _raw_map(snapshot['native_rows'], 1024)
    guards, guard_hashes = _raw_map(snapshot['resource_guards'], 96)
    if (set(guards) != resource_ids or packet.get('native_rows') != row_hashes
            or packet.get('resource_guards') != guard_hashes or guards[phase['resource_id']] != guard):
        _reject('The exact complete native and resource inventories changed')
    for key, row in rows.items():
        _path(row['output'])
        if (row.get('check_id') != key or row.get('phase') != 'clean'
                or row.get('cleanup', {}).get('stopped') is not True or row['cleanup'].get('effects_reconciled') is not True):
            _reject('Unresolved historical native ownership remains')
    latch_states = sidecars(q.runtime.path, sorted(resource_ids))
    if packet.get('sidecars') != latch_states:
        _reject('A resource failure latch appeared or changed after review')
    resource_floors = {}
    for key, saved in guards.items():
        _safe_growth(saved)
        resource_floors[key] = _ack_sidecar(latch_states[key], key, saved, dispositions)
    for previous in record['phases']:
        saved = guards[previous['resource_id']]
        _scope_matches(previous['recipe'], saved['scope'])
        if previous['id'] != phase['id'] and saved['breach'] and not _acknowledged(record, saved['breach'], previous['id']):
            _reject('An earlier phase has an unacknowledged observation failure')
    for item in dispositions:
        prior = next((value for value in record['phases'] if value['id'] == item['phase_id']), None)
        saved = guards[item['resource_id']]
        original = guards[prior['resource_id']] if prior else None
        if (not original or saved['scope'] != original['scope'] or saved['baseline'] != original['baseline']
                or saved['breach'] or item['resource_receipt']['breach']
                or any(saved['high_water_growth_bytes'][key] < item['resource_receipt']['high_water_growth_bytes'][key]
                       for key in CAPS)):
            _reject('A prior passive continuation is no longer healthy')
    planned = _planned(phase)
    phase_rows = {key: row for key, row in rows.items() if Path(row['output']).is_relative_to(Path(phase['output']))}
    ordered = list(planned)
    count = len(phase_rows)
    if not 2 <= count <= len(ordered) or set(phase_rows) != set(ordered[:count]):
        _reject('The phase must be exactly a completed prefix and its immediate next preparation')
    if any(key in rows and key not in phase_rows for key in ordered):
        _reject('A planned check identity is present outside its exact phase output')
    for key in ordered[:count]:
        row, expected = phase_rows[key], planned[key]
        if (row['output'] != expected['output'] or row.get('candidate') != phase['recipe_digest']
                or row.get('command') != expected['command'] or row.get('attempt_id') is not None
                or row.get('native_path_contract') != native_paths.contract(phase['recipe'])):
            _reject('Native command, recipe or path ownership differs from the ordered plan')
    artifacts, bindings, prefix, samples = {}, dict(producer_artifacts), [], []
    for key in ordered[:count - 1]:
        row, expected = phase_rows[key], planned[key]
        receipt = row['receipt']
        if (row.get('preparation_failure') or row.get('ownership_backend') != native_processes.BACKEND
                or not row.get('native_sandbox_boundary') or receipt.get('native_check_id') != key
                or row['native_sandbox_boundary'].get('renderer_receipt', {}).get('component_sources') != SUPPORTED_SOURCES
                or receipt.get('candidate') != phase['recipe_digest'] or receipt.get('name') != expected['name']
                or receipt.get('command') != expected['command'] or receipt.get('cleanup') != row['cleanup']
                or receipt.get('executor') != 'technical-qualification-only'
                or receipt.get('result') not in ('pass', 'fail', 'blocked', 'cancelled')):
            _reject('Every prefix check requires its unchanged complete native receipt')
        status, observed_artifacts, observed_bindings = _artifact_hashes(row, packet['artifacts'])
        artifacts.update(observed_artifacts)
        for path, binding in observed_bindings.items():
            if path in bindings and bindings[path] != binding:
                _reject('Two artifact roles disagree about the same retained file')
            bindings[path] = binding
        census, sample = status.get('census', {}), status.get('resource_guard', {})
        if (census.get('reconciled') is not True or census.get('boundary_verified') is not True
                or census.get('remaining') != [] or census.get('errors') != []
                or 'resource_failure' not in status or status['resource_failure'] is not None
                or not sample or sample.get('breach') is not None or sample.get('scope_digest') != guard['scope_digest']
                or sample.get('active_attempt') != phase['id']
                or type(sample.get('observed_at')) not in (int, float) or not math.isfinite(sample['observed_at'])
                or sample['observed_at'] >= failure['observed_at'] or receipt.get('resource_guard') != sample):
            _reject('The completed prefix lacks a healthy same-scope post-census final observation')
        for group, cap in CAPS.items():
            value = sample['high_water_growth_bytes'][group]
            if type(value) is not int or value < 0 or value > guard['scope'][cap]:
                _reject('A prefix resource observation exceeded its bound')
        free = sample['available_bytes']
        if (not isinstance(free, dict) or not free
                or any(type(value) is not int or value < guard['scope']['min_free_bytes'] for value in free.values())):
            _reject('A prefix final sample retained a free-space violation')
        prefix.append({'check_id': key, 'name': expected['name'], 'receipt_digest': digest(receipt),
                       'outcome': receipt['result'], 'status_sha256': observed_artifacts[row['status']]})
        samples.append(sample)
    # The pinned producer executes in recipe order, then reconciliation stores
    # complete receipts in the native journal's check-ID order.
    if packet.get('artifacts') != artifacts or phase.get('checks') != [phase_rows[k]['receipt'] for k in sorted(ordered[:count - 1])]:
        _reject('The sealed artifact inventory or retained completed outcomes changed')
    if phase['receipt'].get('checks') != phase['checks']:
        _reject('The blocked phase receipt changed its completed check outcomes')
    tail = phase_rows[ordered[count - 1]]
    native_preparation.verify(tail)
    preparation = tail['preparation_failure']
    proof, cleanup = preparation['proof'], tail['cleanup']
    if (proof.get('stage') != 'profile' or proof.get('supervisor_popen') != 'not_called'
            or proof.get('probe') != {'state': 'not_started'}
            or proof.get('renderer') != {'format': native_renderer.FORMAT, 'state': 'not_started'}
            or proof.get('profile') is not None or proof.get('owned_temporary') is not None
            or proof.get('native_observer_timeouts') != [] or tail.get('owned_temporary') is not None
            or any(key in tail for key in ('native_sandbox_boundary', 'owner', 'receipt', 'native_process_receipt',
                                          'native_launch_disposition', 'native_path_probe'))
            or tail.get('members') != {}
            or preparation.get('error') != 'Rejected: resource_limit_exceeded: Retained resource guard violation: observation_failed'):
        _reject('The immediate next check must have no renderer, probe, helper or native launch')
    for key, suffix in (('status', '.json'), ('release', '.release'), ('cancel_path', '.cancel')):
        if tail[key] != str(Path(q.directory) / 'native-status' / (tail['check_id'] + suffix)):
            _reject('The preparation-only launch artifact path differs from its exact identity')
    absent_artifacts = [_absent(tail[name]) for name in ('status', 'release', 'cancel_path')]
    absent_artifacts += [_absent(Path(q.directory) / 'native-status' / (tail['check_id'] + '.sb'))]
    absent_artifacts += [_absent(Path(tail['output']) / name) for name in ('stdout.txt', 'stderr.txt', 'output.txt')]
    anchor = phase_rows[ordered[count - 2]]
    endpoints = {'source': anchor['cleanup']['source_after'],
                 'native': anchor['cleanup']['native_path_effects']['after']}
    if (endpoints['source'] != tail['before_source'] or endpoints['source'] != cleanup['source_after']
            or endpoints['native'] != tail['before_native_paths']
            or endpoints['native'] != cleanup['native_path_effects']['after']
            or cleanup['native_temporary_effects']['before'] != cleanup['native_temporary_effects']['after']
            or cleanup['native_temporary_effects'].get('preexisting_preserved') is not True
            or tail['before_outputs'] != cleanup['outputs_after']
            or cleanup.get('output_mutations') != {'status': 'none', 'changes': []}):
        _reject('The exact intercheck source/native/temporary endpoints do not agree')
    floors = dict(resource_floors[phase['resource_id']])
    for sample in samples:
        for group in CAPS:
            floors[group] = max(floors[group], sample['high_water_growth_bytes'][group])
    result = {'kind': 'intercheck-observation-failure', 'native_rows': {key: row_hashes[key] for key in phase_rows},
            'historical_native_rows': row_hashes, 'resource_guards': guard_hashes,
            'archive_revision': snapshot['archive_revision'], 'native_revision': snapshot['native_revision'],
            'qualification_digest': digest(record), 'historical_phase_digest': digest(phase), 'control_digest': digest(control),
            'original_guard_digest': digest(guard), 'scope_digest': guard['scope_digest'],
            'guard_baseline_digest': digest(guard['baseline']), 'historical_breach': failure,
            'sidecars': latch_states, 'resource_high_water_floors': resource_floors,
            'minimum_high_water_growth_bytes': floors, 'prior_charged_growth_bytes': record['charged_growth_bytes'],
            'completed_prefix': prefix, 'next_preparation': {'check_id': tail['check_id'], 'receipt_digest': preparation['receipt_digest']},
            'healthy_anchor': {'check_id': anchor['check_id'], 'resource_sample': samples[-1],
                               'baseline_binding': 'Exact phase resource authority and pinned producer chain; status carries scope only'},
            'endpoint_digests': {key: digest(value) for key, value in endpoints.items()},
            'artifacts': bindings, 'absent_artifacts': absent_artifacts,
            'executed_source_digest': producer,
            'limitations': 'The reviewed packet attributes the producer. No owned workload follows the healthy post-census anchor; '
                           'the failed scan start and cause are unknown. Endpoints do not establish continuous shared-cache stability. '
                           'Historical failures and check outcomes remain unchanged; this grants no work, time or approval.'}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_PROOF:
        _reject('The compact recovery proof exceeds its bound')
    return result
