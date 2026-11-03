"""Evidence for one stopped technical phase rejected before native admission.

This read-only validator grants neither execution nor cleanup authority. The
caller retains the original failures and accounting before a passive sample.
Unknown historical producers cannot establish no-launch from missing rows.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path

from . import checks, native_processes, resource_limits, technical_control
from .store import Rejected, digest

FORMAT = 'aos-rejected-admission-observation-forensics-v1'
SEMANTICS = 'native-ownership-before-intent-v1'
# Versioned implementation evidence, independent of a product or qualification.
# Include the imported code closure: an unchanged admission method alone cannot
# prove that its preparation constructor or resource gate launched nothing.
LEGACY_PRE_INTENT_SOURCES = {
    'aos/__init__.py': '720340ec2f1530443fbff3f78fd4a06af08880d478752309c6c70881a6cb1f5e',
    'aos/approval_web.py': '8b0f79e04aa3bb7c2052c2997b220ae84fe9806fe884aefa1080730247266078',
    'aos/archive.py': '9665a8370a5bb6757180987bbe831b6c0a315a0c16eb1cf02e83114c3a8079d7',
    'aos/backlog.py': 'b25a1f9adb251e146d435f03b5881eb69b41504131872fb77b432a7a76220306',
    'aos/budgets.py': '48c9fdab46e733418487fc9cb1d8908de377972159588f5f68b736bf0978877f',
    'aos/checks.py': '27e0b2a134696177b7ebf98515dca89b4bc3667e924a90e4b760f8cedd1c3a26',
    'aos/cli.py': '7d4a8203b0fe4b6e83a37e61e9f42a03dbb3e24bdd2464133d9f639d025326e9',
    'aos/codex_runtime.py': 'e6bd9fc92e22251b8944b5df56020447c275b37d54c0b9d3540d8d31a95d96ec',
    'aos/coordinator.py': '5e36bebbf2c1c4eab763cf9302dd9a6df4d7f41f88c675f34b6a84c04e64f42c',
    'aos/coordinator_mcp.py': '0b6c48e7476a98ddf6ad0e38082b458e9c5ef65a2faedcfdcd5d71fdbce07ad4',
    'aos/dashboard_service.py': 'c870c427c0c1096b312621073009948c19d989ce9d696f11a714bfdd25402cf0',
    'aos/decision_wait.py': '7d2a610339559c1445406f836b865cd3ed8534167474676da7087a6643d031ef',
    'aos/instructions.py': '19188c84dc7ad75d820a94c07bbdb183b623404c58ea28a3b89a9ae9fa577448',
    'aos/interface.py': 'd2e7db11d05925b69bd8c3f1a5add9f5d6d404a2c61e3392993da21ba7ddce0d',
    'aos/legacy_recovery.py': '1712f283ee1bc7d8b4aa3b965e4125636538f7b65ae8cdc56380968e2d7a678e',
    'aos/lifecycle.py': '4b72f84ca237e230ac0ee0f9edd90332d9b2e03fcdb3d3cde9af9519cc489b28',
    'aos/native_network.py': 'ef7abb267e9c2a1b494af1758d422b3aeb002225b60814ae9930492e7bc6681c',
    'aos/native_paths.py': '44fb8f5d6d96d48ecebb6af5caa6d3013469262b2462bf203f9ae2ae5a060715',
    'aos/native_pgrep.py': 'eb579d09bcd2406ea8245617613dba5a1c050a5b027d982110c68ec177962e61',
    'aos/native_preparation.py': 'e70f02787b3fae9bd1a0af31ad6a82ba1db68f3216b7de4e24bca0b6bcc7e33e',
    'aos/native_processes.py': '4143d982e49a17f2743f026c02a3788b41b88a5f4161433f1e23f99c56c3293d',
    'aos/native_qualification.py': '7d204c073492f7c056f2ea0fbb9cec0b90e9f77e0a35aa703bfa9c01562cee0e',
    'aos/native_quiescence.py': 'ce26c398d6e4f1cbb79efddd3fdfad9530f313a8c8218a51295e18cf4fc774ba',
    'aos/native_renderer.py': '2e4620cbfd1cef5ed5f9b499967d3860814d6c0f814bb2c6c4f626f73f86800d',
    'aos/native_sandbox.py': 'ba9a0146d9d2c50db184c59308dd7f0d0f1cfc5ab4ca26e84a7d070d099fc1b8',
    'aos/native_temporary.py': '7cda8c2ae97c5b5ce738bd303ae0cda0589cadd608b3bd783d5a7fac29806177',
    'aos/projection.py': 'dd96d3748a2bfb7011eefe1e9efd1a57117d7d951affbd369aba92aa3c08a6d5',
    'aos/resource_limits.py': '519dbe84b0cffba9ac9f2fc4e6a7698c0dfb06b5b684612e3b449b2113f8d68b',
    'aos/runtime.py': '6897d3f024bea9141043b341b48594bc5b19b8094c14fa5354da0f3d3d4b5eef',
    'aos/store.py': '9822de9ec75f38b3c48ea9f31c528ca5cc10c924e17d715f6e8fa11be3181a8d',
    'aos/worker.py': '09fc147a2ed4d6b5cea03a14131867b0f141b26c0f7f3ad7cfcc7b47fd03e4c4',
    'check_adapters/__init__.py': '0c36599816ef9ec0b16ac549f456d06d1ffbec0aeb8811fcb42d3781c61ef8a8',
    'check_adapters/cargo_test.py': 'f1c833069af28429450ca7b0d5d33dae166455bffe772e90fd989d1b63157ba7',
    'check_adapters/native_build.py': '06e203a5d1f42a219944d17c8a4f160471c511bc77289dce50470f3918ce9784',
    'check_adapters/native_tests.py': '01742927e701bbf5d7525d9e7cc6cf9f977988410d7028cb46149a932ca9b47b',
    'check_adapters/pytest.py': 'd9ecf5f572e290eddc4818be6486f69ed5eaaf53ec3dc66b689da6c41a3c3401',
}
_REFUSALS = {
    'native_ownership: Native ownership changed during baseline capture',
    'native_ownership: A native row has an unresolved phase identity',
    'native_ownership: An unresolved native check lacks complete path ownership',
    'native_ownership: A live or unresolved native check owns an overlapping path',
    'native_ownership: An unresolved native check owns the shared temporary namespace',
}
_GROUPS = {'output_roots': 'max_output_growth_bytes',
           'source_evidence_roots': 'max_source_evidence_growth_bytes'}


def _reject(message):
    raise Rejected('technical_rejected_admission', message)


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or path.resolve() != path
            or any(p.is_symlink() for p in (path, *path.parents))):
        _reject('Evidence paths must be exact canonical paths without aliases')
    return path


def _json_file(path, sha256, *, limit=8 * 1024 * 1024, private=False):
    path = _path(str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_size > limit or private and stat.S_IMODE(before.st_mode) != 0o600):
            _reject('Reviewed evidence must be a bounded owned regular file')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    current = path.stat(follow_symlinks=False)
    if (len(raw) > limit or hashlib.sha256(raw).hexdigest() != sha256
            or any(getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(current, key)
                   for key in fields)):
        _reject('Reviewed evidence changed after its hash was sealed')
    return json.loads(raw)


def evidence(path, sha256):
    try:
        packet = _json_file(path, sha256)
        if not isinstance(packet, dict) or packet.get('format') != FORMAT or packet.get('read_only') is not True:
            _reject('The exact read-only rejected-admission forensic packet is required')
        return packet
    except Rejected:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise Rejected('technical_rejected_admission', 'The sealed forensic packet is unavailable or malformed') from error


def _source(packet):
    source = packet['execution_source']
    if source.get('semantics') != SEMANTICS:
        _reject('The executed producer has no supported pre-admission semantics')
    manifest = _json_file(**source['manifest'])
    verification = _json_file(**source['verification'])
    files = {name.removeprefix('src/agentic_tools/'): value for name, value in manifest['files'].items()
             if name.startswith(('src/agentic_tools/aos/', 'src/agentic_tools/check_adapters/'))}
    if (files != LEGACY_PRE_INTENT_SOURCES or source.get('source_digest') != digest(files)
            or manifest.get('version', {}).get('source_digest', digest(files)) != digest(files)
            or verification.get('source_digest') != digest(files) or verification.get('source_unchanged') is not True
            or type(verification.get('pytest', {}).get('exit_code')) is not int
            or verification['pytest']['exit_code'] != 0):
        _reject('Exact supported historical code and matching passing verification are required')
    return source['source_digest']


def _owner(phase):
    owner, joined = phase.get('observer_owner'), phase.get('observer_joined', {})
    if (phase.get('execution_started') is not True or not isinstance(owner, dict)
            or type(owner.get('pid')) is not int or owner['pid'] <= 0
            or type(owner.get('uid')) is not int or owner['uid'] != os.getuid()
            or not isinstance(owner.get('birth'), list) or len(owner['birth']) != 2
            or any(type(v) is not int or v < 0 for v in owner['birth'])
            or owner['birth'][0] == 0 or owner['birth'][1] >= 1_000_000
            or joined.get('owner') != owner or joined.get('watcher_joined') is not True
            or joined.get('cancellers_joined') is not True
            or type(joined.get('observed_at')) not in (int, float) or not math.isfinite(joined['observed_at'])):
        _reject('The original observer needs an exact identity and positive join receipt')
    try:
        current = native_processes.identity(owner['pid'])
        if current is not None and (not isinstance(current, dict) or current.get('pid') != owner['pid']
                                    or type(current.get('uid')) is not int
                                    or not isinstance(current.get('birth'), list) or len(current['birth']) != 2
                                    or any(type(v) is not int or v < 0 for v in current['birth'])
                                    or current['birth'][0] == 0 or current['birth'][1] >= 1_000_000):
            _reject('Observed process identity is incomplete')
        if native_processes.same(owner, current):
            _reject('The original observer owner is still alive')
    except (OSError, RuntimeError, KeyError, TypeError) as error:
        raise Rejected('technical_rejected_admission', 'Original observer absence is not proven') from error


def _absent(path):
    path = _path(str(path))
    try:
        path.lstat()
    except FileNotFoundError:
        return str(path)
    _reject('A rejected-admission output or launch artifact exists')


def _artifacts(q, phase):
    recipe = phase['recipe']
    planned = recipe['checks']
    if phase['output'] != str(Path(recipe['outputs']) / ('technical-' + phase['id'])):
        _reject('The phase output differs from its historical admission recipe')
    if not isinstance(planned, list) or not planned or len({c['name'] for c in planned}) != len(planned):
        _reject('The exact nonempty planned native check list is required')
    absent = [_absent(Path(phase['output']))]
    expected = {}
    status = _path(str(q.directory / 'native-status'))
    for check in planned:
        name = check['name']
        if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name:
            _reject('A planned check has an invalid output identity')
        output = Path(phase['output']) / name
        adapter = checks.adapter_for(check)
        from agentic_tools.check_adapters import identities
        if recipe.get('_adapter_bindings', {}).get(adapter.ADAPTER_ID) != identities()[adapter.ADAPTER_ID]:
            _reject('Historical command expansion differs from its bound adapter')
        command = adapter.command([part.replace('{output}', recipe['outputs']) for part in check['argv']], output)
        check_id = 'nc-' + digest([str(output.resolve()), command, phase['recipe_digest']])[7:39]
        expected[check_id] = {'name': name, 'command_digest': digest(command), 'output': str(output)}
        absent.extend(_absent(status / (check_id + suffix)) for suffix in ('.sb', '.json', '.release', '.cancel'))
    return expected, absent


def validate(q, record, phase, guard, packet, *, control=None):
    """Validate cold evidence; caller must recheck these bindings at mutation."""
    try:
        return _validate(q, record, phase, guard, packet, control=control)
    except Rejected:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError, AttributeError) as error:
        raise Rejected('technical_rejected_admission', 'Incomplete or changed rejected-admission evidence') from error


def _validate(q, record, phase, guard, packet, *, control):
    from .native_qualification import _acknowledged, _dispositions
    if (packet.get('format') != FORMAT or packet.get('read_only') is not True
            or packet.get('qualification_id') != q.qualification_id or packet.get('phase_id') != phase['id']
            or packet.get('qualification_digest') != digest(record) or packet.get('phase_digest') != digest(phase)
            or packet.get('control_digest') != (digest(control) if control is not None else None)
            or packet.get('source_baseline_digest') != digest(record['source_baseline'])
            or packet.get('budget_before') != record['budget_before']
            or phase['recipe_digest'] != digest(phase['recipe'])):
        _reject('The reviewed exact phase, source, budget or journal changed')
    if (record.get('export') or record['active_phase'] is not None or record['phases'][-1] != phase
            or guard['active_attempt'] is not None or phase['state'] != 'stopped'
            or phase.get('checks') != [] or phase['receipt'].get('checks') != []
            or phase['receipt'].get('status') != 'blocked' or type(phase['receipt'].get('model_calls')) is not int
            or phase['receipt']['model_calls'] != 0
            or any(p['state'] != 'stopped' or p.get('receipt', {}).get('stopped') is not True
                   or p['receipt'].get('source_unchanged') is not True
                   or p['receipt'].get('product_budget_unchanged') is not True for p in record['phases'])):
        _reject('Only the exact latest stopped zero-work technical phase can be reviewed')
    if control is not None:
        if (control.get('format') != technical_control.FORMAT or control.get('qualification_id') != q.qualification_id
                or control.get('active') is not False or control.get('archive_binding') != technical_control.binding(phase)
                or any(control['phase'].get(key) != phase.get(key)
                       for key in (*technical_control.IMMUTABLE, *technical_control.MUTABLE))):
            _reject('The compact control differs from the exact stopped archival phase')
    _owner(phase)
    source_digest = _source(packet)
    failure = packet['admission_failure']
    refusals = [error for error in phase['execution_errors'] if error in _REFUSALS]
    if (failure != {'check': phase['recipe']['checks'][0]['name'], 'error': 'native_ownership'}
            or len(refusals) != 1):
        _reject('The exact first-check pre-intent ownership refusal is required')
    prior = [{'operation_id': item['operation_id'], 'phase_id': item['phase_id'],
              'acknowledgment_digest': digest(item['acknowledgment']), 'resource_id': item['resource_id'],
              'resource_receipt': item['resource_receipt']} for item in _dispositions(record)]
    if (packet.get('prior_dispositions') != prior
            or packet.get('prior_charged_growth_bytes') != record['charged_growth_bytes']
            or any(item['phase_id'] == phase['id'] for item in _dispositions(record))):
        _reject('The prior dispositions or original charged growth changed')
    primary = guard['breach']
    if (not primary or primary.get('reason') != 'active_observation_gap'
            or type(primary.get('observed_at')) not in (int, float) or not math.isfinite(primary['observed_at'])
            or packet.get('resource_guard_digest') != digest(guard)
            or packet.get('guard_baseline_digest') != digest(guard['baseline'])
            or packet.get('guard_scope_digest') != guard['scope_digest'] or guard['scope_digest'] != digest(guard['scope'])):
        _reject('The exact original observation-gap guard is required')
    saved_sidecar = packet['failure_sidecar']
    sidecar_path = resource_limits._failure_path(q.runtime.path, phase['resource_id'])
    if saved_sidecar['path'] != str(sidecar_path):
        _reject('The reviewed secondary failure belongs to another resource guard')
    sidecar = _json_file(sidecar_path, saved_sidecar['sha256'], limit=4096, private=True)
    if (sidecar != saved_sidecar['content'] or sidecar.get('work_id') != phase['resource_id']
            or sidecar.get('scope_digest') != guard['scope_digest']
            or sidecar.get('breach', {}).get('reason') != 'resource_observation_unavailable'
            or sidecar['breach'] == primary):
        _reject('Both distinct original observation failures must remain hash-bound')
    failures = [record['breach'], *record.get('unresolved_failures', [])]
    if primary not in failures or any(f and f not in (primary, sidecar['breach']) and not _acknowledged(record, f)
                                      for f in failures):
        _reject('Another unresolved qualification failure remains')
    floors = {}
    for group, cap in _GROUPS.items():
        values = [guard['high_water_growth_bytes'][group], sidecar['high_water_growth_bytes'][group]]
        if (any(type(value) is not int or value < 0 for value in values)
                or type(guard['scope'][cap]) is not int or guard['scope'][cap] < 0
                or max(values) > guard['scope'][cap]
                or type(record['charged_growth_bytes'][group]) is not int or record['charged_growth_bytes'][group] < 0):
            _reject('An observed resource overrun cannot be reclassified as rejected admission')
        floors[group] = max(values)
    available = guard['last_observation']['available_bytes']
    if (not available or any(type(value) is not int or value < 0 for value in available.values())
            or type(guard['scope']['min_free_bytes']) is not int or guard['scope']['min_free_bytes'] < 0
            or min(available.values()) < guard['scope']['min_free_bytes']):
        _reject('A retained free-space violation cannot be acknowledged')
    expected, absent = _artifacts(q, phase)
    reviewed = {item['id']: item['raw_record_sha256'] for item in packet['native_rows']}
    if len(reviewed) != len(packet['native_rows']):
        _reject('Duplicate native evidence identities are not an exact inventory')
    actual = {}
    with q.runtime._db() as db:
        raw_guard = db.execute('SELECT record FROM resource_limit WHERE work_id=?', (phase['resource_id'],)).fetchone()
        if (not raw_guard or json.loads(raw_guard[0]) != guard
                or hashlib.sha256(raw_guard[0].encode()).hexdigest() != packet.get('resource_guard_raw_sha256')
                or resource_limits._read_failure(db, phase['resource_id'], guard['scope']) != sidecar):
            _reject('The original guard or retained secondary failure changed')
        for key, raw in db.execute('SELECT id,record FROM native_check ORDER BY id'):
            row = json.loads(raw)
            output = _path(row['output'])
            if (row.get('check_id') != key or key in expected or output.is_relative_to(Path(phase['output']))
                    or row.get('phase') != 'clean' or row.get('cleanup', {}).get('stopped') is not True
                    or row['cleanup'].get('effects_reconciled') is not True):
                _reject('A phase row or unresolved historical native owner remains')
            actual[key] = hashlib.sha256(raw.encode()).hexdigest()
        if actual != reviewed:
            _reject('The full historical native inventory changed after review')
        for previous_phase in record['phases']:
            previous = resource_limits._read(db, previous_phase['resource_id'])
            if (not previous or previous['active_attempt'] is not None
                    or previous['breach'] and previous_phase['id'] != phase['id']
                    and not _acknowledged(record, previous['breach'], previous_phase['id'])):
                _reject('Another phase resource observation remains unresolved')
        for item in _dispositions(record):
            previous = resource_limits._read(db, item['resource_id'])
            if not previous or previous['active_attempt'] is not None or previous['breach'] or item['resource_receipt']['breach']:
                _reject('A prior accounting continuation is not healthy and inactive')
    return {'kind': 'rejected-admission-observation', 'native_rows': {}, 'historical_native_rows': actual,
            'expected_unadmitted_checks': expected, 'absent_artifacts': absent,
            'historical_breaches': [primary, sidecar['breach']], 'failure_sidecar': saved_sidecar,
            'original_guard_digest': digest(guard), 'historical_phase_digest': digest(phase),
            'qualification_digest': digest(record), 'control_digest': packet.get('control_digest'),
            'scope_digest': guard['scope_digest'], 'guard_baseline_digest': digest(guard['baseline']),
            'minimum_high_water_growth_bytes': floors, 'prior_charged_growth_bytes': record['charged_growth_bytes'],
            'observer_owner': phase['observer_owner'], 'observer_joined': phase['observer_joined'],
            'executed_source_digest': source_digest, 'admission_refusal': refusals[0],
            'limitations': 'The reviewed packet attributes execution to the hash-bound supported producer; this validator '
                           'does not independently attest historical execution. That producer admitted no native intent, '
                           'permission probe or workload. Both observation failures remain historical; '
                           'no continuous observation or work/cleanup authority is asserted'}
