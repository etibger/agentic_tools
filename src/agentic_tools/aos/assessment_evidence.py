"""Protected exact-candidate coordinator proof gates assessment, never verdicts.

The verifier performs no scheduler, clock, Git or runtime writes. It is shared by
preclaim, saved-launch, cached-attempt, each native GO and final completion gates.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from .store import Rejected, digest
from . import predispatch_settings_rebind as retained

FORMAT = 'aos-assessment-evidence-gate-v1'
RELEASE = 'aos-coordinator-assessment-release-v1'
REPORT = 'aos-coordinator-evidence-report-v1'
COMMAND = 'aos-coordinator-command-result-v1'
PROFILE = 'aos-final-coordinator-invocation-v1'
PHASES = {'imports-replay': ('migration-reconciliation', 'imports-replay'), 'service-restart': ('restart',),
          'backup-restore': ('backup-restore',), 'final-verification': ('final-verification',)}
TARGET_FIELDS = {'profile', 'vm_id', 'container_id', 'image_digest', 'schema_digest', 'database', 'import_generation'}
CATEGORIES = ('exact-source-safety-review', 'imports-replay', 'service-restart', 'backup-restore', 'final-verification')
MODES = {'normal_assessment', 'source_failure_review', 'data_failure_assessment', 'native_failure_triage'}
RUNTIME_FIELDS = {'profile', 'vm_id', 'container_id', 'image_digest', 'schema_digest', 'database', 'import_generation',
                  'source_fingerprint', 'canonical_fingerprint', 'revision_fingerprint', 'provenance_fingerprint', 'identity_digest'}
REPORT_FIELDS = {'format', 'work_id', 'assignment_id', 'attempt_id', 'decision_id', 'concept_digest', 'recipe_digest',
                 'task_plan_digest', 'candidate_digest', 'source_digest', 'commit', 'tree', 'category', 'outcome',
                 'completed_at', 'operation', 'runtime', 'commands', 'review', 'cleanup'}
COMMAND_FIELDS = {'format', 'work_id', 'source_digest', 'commit', 'tree', 'category', 'runtime_digest', 'argv', 'cwd',
                  'started_at', 'completed_at', 'exit_code', 'timed_out', 'stdout', 'stderr', 'assertions', 'cleanup',
                  'profile', 'owned_execution', 'phase_result'}
ASSERTIONS = {'imports-replay': {'all_exports_processed', 'duplicate_replay_stable', 'all_essential_metrics_queryable'},
              'service-restart': {'same_database_retained', 'service_healthy'},
              'backup-restore': {'backup_created', 'isolated_restore_matches'},
              'final-verification': {'api_query_contract', 'source_runtime_correspondence', 'all_metric_families_queryable'}}


def require(value, message):
    if not value:
        raise Rejected('assessment_evidence', message)


def configuration(contract, store):
    if 'assessment_evidence_gate' not in contract:
        require('predispatch_feature_amendment' not in contract, 'Amended feature is missing its required assessment evidence gate')
        return None
    value = contract['assessment_evidence_gate']
    require(isinstance(value, dict) and set(value) == {'format', 'receipt_root', 'native_handoff_root', 'categories', 'feature_deadline'} and
            value['format'] == FORMAT and value['categories'] == list(CATEGORIES) and
            type(value['feature_deadline']) in (int, float) and math.isfinite(value['feature_deadline']), 'Invalid approved evidence gate')
    for key in ('receipt_root', 'native_handoff_root'):
        retained._protected(store, value[key], directory=True)
    roots = [Path(value[k]) for k in ('receipt_root', 'native_handoff_root')]
    require(not any(a.is_relative_to(b) for a, b in (roots, roots[::-1])), 'Evidence and handoff roots must be disjoint')
    return value


def _stamp(value):
    try:
        result = datetime.fromisoformat(value)
        require(result.tzinfo is not None, 'Timezone-qualified evidence timestamp required')
        return result.timestamp()
    except (TypeError, ValueError) as error:
        raise Rejected('assessment_evidence', 'Malformed evidence timestamp') from error


def _read(store, reference, *, raw=False):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}, 'Exact evidence reference required')
    retained._protected(store, reference['path'])
    value, actual = (retained._file if raw else retained._json)(reference['path'], immutable=True)
    require(actual == reference, 'Protected evidence changed')
    return value


def _file_ref(store, path):
    retained._protected(store, path)
    return retained._json(path, immutable=True)


def _authority(lifecycle, db, contract, work_id):
    from . import predispatch_feature_amendment as amendment
    corrective = contract.get('predispatch_corrective_rebind')
    supplemental = corrective or contract.get('predispatch_recovery')
    if supplemental is not None:
        if corrective is not None:
            from . import predispatch_corrective_rebind as recovery
        else:
            from . import predispatch_recovery as recovery
        require(isinstance(supplemental, dict) and set(supplemental) == {'operation_id', 'metadata_digest', 'allowances'} and recovery._schema(db),
                'Exact applied supplemental recovery authority is required')
        row = db.execute(f'SELECT record FROM {recovery.TABLE} WHERE operation_id=? AND work_id=?',
                         (supplemental['operation_id'], work_id)).fetchone()
        require(row is not None, 'Applied supplemental recovery receipt is missing')
        saved = json.loads(row[0])
        ref = {'operation_id': supplemental['operation_id'], 'work_id': work_id, 'receipt_digest': digest(saved)}
        receipt = recovery.verify(lifecycle.store, ref)
        packet = _read(lifecycle.store, receipt['packet'])
        controls = packet['metadata']['controller_contract']
        resource = packet['request']['controller_resource']
        prefix = resource[:-len(digest(packet['metadata']['original_controller']))]
        require(digest(packet['metadata']) == supplemental['metadata_digest'] and
                packet['request']['allowances'] == supplemental['allowances'] and
                receipt['to_concept_digest'] == contract['concept_digest'] and
                receipt['to_recipe_digest'] == contract['runtime_recipe_digest'] and
                prefix + digest(controls) in contract['resources'], 'Supplemental authority or controller differs')
        return receipt, controls, ref
    marker = contract.get('predispatch_feature_amendment')
    require(isinstance(marker, dict) and set(marker) == {'operation_id', 'metadata_digest'} and amendment._schema(db),
            'Exact applied feature amendment authority is required')
    row = db.execute(f'SELECT record FROM {amendment.TABLE} WHERE operation_id=? AND work_id=?',
                     (marker['operation_id'], work_id)).fetchone()
    require(row is not None, 'Applied amendment receipt is missing')
    saved = json.loads(row[0]); ref = {'operation_id': marker['operation_id'], 'work_id': work_id, 'receipt_digest': digest(saved)}
    receipt = amendment.verify(lifecycle.store, ref)
    packet = _read(lifecycle.store, receipt['packet'])
    controls = packet['metadata']['controller_contract']
    require(digest(packet['metadata']) == marker['metadata_digest'] and receipt['to_concept_digest'] == contract['concept_digest'] and
            receipt['to_recipe_digest'] == contract['runtime_recipe_digest'] and 'rnf-controller:' + digest(controls) in contract['resources'],
            'Amendment, controller or exact current approval differs')
    return receipt, controls, ref


def _full_inventory(recipe):
    from .codex_runtime import candidate
    value = candidate(Path(recipe['source']), recipe=recipe)
    root = Path(recipe['source']); rows = {}
    for name, expected in value['inventory'].items():
        path = root / name
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and path.resolve() == path,
                'Source contains aliases or nonordinary files')
        raw = path.read_bytes(); after = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(identity(before) == identity(after) and hashlib.sha256(raw).hexdigest() == expected, 'Source changed during mode inventory')
        rows[name] = {'sha256': expected, 'size': len(raw), 'mode': '100755' if before.st_mode & 0o111 else '100644'}
    return value, dict(sorted(rows.items()))


def _git_readback(repository, commit, *, require_head=True, deadline=None):
    root = Path(repository)
    require(root.is_absolute() and root.resolve() == root and root.is_dir() and re.fullmatch(r'[0-9a-f]{40}', commit), 'Exact Git repository/commit required')
    end = time.monotonic() + 30 if deadline is None else deadline
    def git(*args):
        require(time.monotonic() < end, 'Git readback deadline exceeded')
        try:
            result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(.001, end-time.monotonic())).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise Rejected('assessment_evidence', 'Actual Git readback failed') from error
        require(len(result) <= 134217728 and time.monotonic() < end, 'Git readback exceeded bounds')
        return result
    if require_head:
        require(git('rev-parse', 'HEAD').decode().strip() == commit and
                git('symbolic-ref', '--short', 'HEAD').decode().strip() == 'codex/rf-backend-001', 'Actual product HEAD/branch differs')
    parents = git('show', '-s', '--format=%P', commit).decode().strip().split()
    require(len(parents) == 1, 'Final product increment must have one actual parent')
    rows, total = {}, 0
    for entry in git('ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1); mode, kind, oid = meta.decode().split()
        require(kind == 'blob' and mode in ('100644', '100755') and len(rows) < 10000, 'Unexpected committed tree entry')
        size = int(git('cat-file', '-s', oid)); total += size
        require(total <= 134217728, 'Committed tree exceeds byte bound')
        raw = git('cat-file', 'blob', oid)
        require(size == len(raw), 'Committed object size differs')
        rows[name.decode()] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': size, 'mode': mode}
    result = {'commit': commit, 'tree': git('rev-parse', commit + '^{tree}').decode().strip(),
              'parent': parents[0], 'inventory': dict(sorted(rows.items()))}
    if require_head:
        require(not git('diff', '--cached', '--name-only') and not git('diff', '--name-only') and
                git('rev-parse', 'HEAD').decode().strip() == commit and
                git('symbolic-ref', '--short', 'HEAD').decode().strip() == 'codex/rf-backend-001', 'Committed source/index changed')
    return result


def _checkpoint(store, controls, contract, inventory, commit, work_id):
    root = Path(controls['control_root']) / 'chain'
    retained._protected(store, root, directory=True)
    files = sorted(root.glob('*.json'))
    require(2 <= len(files) <= 100, 'Final contemporaneous commit chain is missing')
    previous, stages, refs = None, [], []
    deadline = time.monotonic() + 30
    for ordinal, path in enumerate(files):
        require(path.name == f'{ordinal:04d}.json', 'Checkpoint chain is not contiguous')
        value, ref = _file_ref(store, path); refs.append(ref)
        require(value['ordinal'] == ordinal and value['work_id'] == work_id and
                value['concept_digest'] == contract['concept_digest'] and value['recipe_digest'] == contract['runtime_recipe_digest'] and
                value['previous_sha'] == (previous['actual_sha'] if previous else controls['kickoff_sha']), 'Checkpoint lineage differs')
        actual = _git_readback(controls['repository'], value['actual_sha'], require_head=ordinal==len(files)-1, deadline=deadline)
        require(actual['parent'] == value['previous_sha'] and actual['inventory'] == value['inventory'],
                'Historical checkpoint differs from its actual Git parent/tree/modes')
        if ordinal == 0:
            require(value['inventory'] == controls['baseline_inventory'], 'Checkpoint baseline differs')
        else:
            require(isinstance(value['ack_file'], str) and '/' not in value['ack_file'], 'Invalid checkpoint ACK path')
            ack, ack_ref = _file_ref(store, Path(controls['ack_root']) / value['ack_file']); refs.append(ack_ref)
            require(ack == value['ack'] and ack['tree'] == actual['tree'] and all(ack[k] == value[k] for k in
                    ('work_id', 'concept_digest', 'recipe_digest', 'ordinal', 'previous_sha', 'actual_sha', 'final')) and
                    ack['inventory_digest'] == digest(value['inventory']), 'Checkpoint ACK/source correspondence differs')
            if ack['increment'] != 'repair':
                require(len(stages) < 3 and ack['increment'] == controls['stage_order'][len(stages)], 'Checkpoint stage order differs')
                stages.append(ack['increment'])
            require(value['completed_stages'] == stages, 'Checkpoint completion history differs')
        previous = value
    require(previous['actual_sha'] == commit and previous['inventory'] == inventory and previous['final'] is True and
            stages == controls['stage_order'], 'Candidate has no exact final checkpoint covering ordered stages')
    require(actual['inventory'] == inventory and actual['parent'] == previous['previous_sha'] and
            actual['tree'] == previous['ack']['tree'], 'Actual Git commit/tree/modes differ from the final ACK')
    return {'references': refs, 'latest': previous, 'tree': actual['tree']}


def _native_failure_evidence(recipe, context, feature_deadline):
    """Recheck actual retained diagnostics before a fresh triage can be claimed."""
    from .codex_runtime import CodexRuntime
    evidence = context['assessment_context']
    origin, failed, frozen = evidence['origin'], evidence['failure_result'], context['candidate']
    require(failed.get('candidate') == frozen and digest(failed) == origin['result_sha256'] and
            failed.get('reviewed_host_validation', {}).get('phase') == 'native_failed',
            'Triage diagnostics do not bind the actual retained failed result')
    end = time.monotonic() + min(30, max(0, feature_deadline - time.time()))
    output = Path(recipe['outputs']) / origin['attempt_id']
    def ordinary(path):
        path = Path(path)
        require(time.monotonic() < end and path.is_absolute() and path.resolve() == path and
                path.is_relative_to(output) and not any(p.is_symlink() for p in (path, *path.parents)),
                'Native failure diagnostic is stale, aliased or outside its actual attempt')
        return path
    report = ordinary(output / ('review-' + origin['attempt_id'] + '.txt'))
    review, descriptor = CodexRuntime._read_source_review(report, frozen)
    require(review['verdict'] == 'pass' and not review['findings'] and
            descriptor == failed.get('initial_reports', {}).get('review') and
            descriptor == failed['reviewed_host_validation'].get('review'),
            'Original independent passing review changed before triage')
    checks = failed.get('checks')
    expected = {check['name'] for check in recipe['checks']}
    require(isinstance(checks, list) and all(isinstance(check, dict) for check in checks) and
            len(checks) == len(expected) and {check.get('name') for check in checks} == expected and
            any(check.get('result') == 'fail' for check in checks), 'Complete original native failure diagnostics are required')
    for check in checks:
        require(check.get('candidate') == frozen and check.get('result') in ('pass', 'fail') and
                check.get('cleanup', {}).get('stopped') is True and
                check['cleanup'].get('effects_reconciled') is True and check['cleanup'].get('owned_processes') == [] and
                isinstance(check.get('raw_outputs'), dict) and bool(check['raw_outputs']),
                'Native failure diagnostics lack exact candidate or positive cleanup')
        ordinary(check['output'])
        for path in check['raw_outputs']:
            ordinary(path)
        CodexRuntime._verify_check_receipt(check, deadline=end)
    require(time.monotonic() < end, 'Native failure diagnostic verification expired')


def _context_in(lifecycle, db, assignment, *, commit, controller_digest, at, settled=True):
    run, contract = lifecycle._run(db, assignment['work_id'])
    gate = configuration(contract, lifecycle.store)
    require(gate and assignment['kind'] == 'assessment', 'Assessment gate is not selected')
    require(type(at) in (int, float) and math.isfinite(at) and at < gate['feature_deadline'], 'Original feature window expired')
    receipt, controls, reference = _authority(lifecycle, db, contract, assignment['work_id'])
    require(controller_digest == digest(controls), 'Approved controller digest differs')
    require(_stamp(receipt['created_at']) <= at and _stamp(assignment['created_at']) <= at, 'Evidence predates current authority/assignment')
    actual, full = _full_inventory(contract['runtime_recipe'])
    require(actual == json.loads(assignment['candidate']) == json.loads(run['candidate']), 'Frozen exact AOS candidate differs')
    checkpoint = _checkpoint(lifecycle.store, controls, contract, full, commit, assignment['work_id'])
    from . import predispatch_feature_amendment as amendment
    if receipt.get('format') in ('aos-predispatch-recovery-v1', 'aos-predispatch-corrective-rebind-v1'):
        from . import predispatch_recovery as recovery
        accounting = recovery.current_accounting(lifecycle.store, receipt, settled=settled, at=at)
    else:
        accounting = amendment.current_accounting(lifecycle.store, receipt, settled=settled, at=at)
    for ref in checkpoint['references']:
        row = _read(lifecycle.store, ref)
        if 'ack' not in row:
            continue
        ack = row['ack']; op = accounting['operations'].get(f"checkpoint-{row['ordinal']:04d}")
        require(op and op['result'] and op['reservation'].get('context', {}).get('request_digest') == ack['request_digest'] and
                all(op['reservation']['context'][k] == ack[k] for k in ('assignment_id', 'attempt_id', 'increment', 'ordinal')) and
                op['reservation']['started_at'] <= ack['recorded_at'] <= op['result']['completed_at'],
                'Checkpoint ACK lacks its actual completed writer operation')
    context = {key: assignment[key] for key in ('work_id', 'assignment_id', 'attempt_id', 'concept_digest', 'contract_revision', 'predecessor')}
    context.update(source_root=contract['runtime_recipe']['source'], evidence_root=gate['receipt_root'],
        catalog_digest='sha256:' + controls['native_catalog_sha256'],
        assignment_created_at=assignment['created_at'], state_dir=str(lifecycle.store.directory), decision_id=receipt['to_decision_id'],
        recipe_digest=contract['runtime_recipe_digest'], cycle=run['cycle'], candidate=actual,
        source_inventory=full, source_digest=digest(full), commit=commit, tree=checkpoint['tree'],
        controller_digest=controller_digest, task_plan=controls['approved_task_plan'],
        amendment=reference, checkpoint=checkpoint, assessment_context=lifecycle._assessment_context_in(db, assignment),
        accounting={'identity': accounting['clock']['identity'], 'spent': accounting['clock']['spent'],
                    'receipt_digest': accounting['receipt_digest'], 'operations': accounting['operations']})
    context['retained_failure_release'] = None
    if context['assessment_context']['purpose'] == 'native_failure_triage':
        _native_failure_evidence(contract['runtime_recipe'], context, gate['feature_deadline'])
        origin = context['assessment_context']['origin']
        prior = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND attempt_id=?',
                           (origin['assignment_id'],origin['attempt_id'])).fetchone()
        require(prior and prior['state']=='failed','Actual retained failed assessor is missing')
        captured = _claimed_reference(db,prior)
        old = _read(lifecycle.store,captured['reference'])
        require(old.get('format')==RELEASE and old.get('mode') in ('normal_assessment','data_failure_assessment') and
                captured['mode']==old['mode'] and all(old['context'][k]==context[k] for k in
                ('state_dir','decision_id','concept_digest','recipe_digest','candidate','source_inventory','source_digest','commit','tree','controller_digest','task_plan')),
                'Triage predecessor was not the same released exact candidate/authority')
        # Preserve actual failed observation lineage. Do not replay commands or
        # demand that a now-closed historical native lease become active again.
        for item in old['reports']:
            if item['reference'] is None:
                continue
            report=_read(lifecycle.store,item['reference'])
            for command_ref in report['commands']:
                command=_read(lifecycle.store,command_ref)
                for key in ('stdout','stderr'):
                    _read(lifecycle.store,command[key],raw=True)
                _read(lifecycle.store,command['owned_execution'])
                if command['phase_result'] is not None:
                    _read(lifecycle.store,command['phase_result'])
                profile=_read(lifecycle.store,command['profile'])
                _read(lifecycle.store,profile['manifest'])
                for ref in profile['inputs']:
                    _read(lifecycle.store,ref,raw=True)
        context['retained_failure_release'] = captured
    return context, gate, controls


def release_context(lifecycle, assignment, *, commit, controller_digest, at=None):
    """Read-only facts for coordinator report construction; no publication."""
    with lifecycle.store._connection() as db:
        current = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment['assignment_id'],)).fetchone()
        require(current and all(current[k] == assignment[k] for k in ('work_id', 'attempt_id', 'kind', 'candidate')), 'Actual assignment differs')
        return _context_in(lifecycle, db, current, commit=commit, controller_digest=controller_digest,
                           at=time.time() if at is None else at)[0]



def native_preparation_context(lifecycle, assignment, *, commit, controller_digest, at=None):
    """Exact actual identities while the one native-preparation charge is open.

    This cannot produce a report/release or authorize a claim. Those entry points
    always independently rederive settled accounting.
    """
    with lifecycle.store._connection() as db:
        current = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment['assignment_id'],)).fetchone()
        require(current and current['state'] == 'proposed' and current['attempt_id'] == assignment['attempt_id'],
                'Native preparation requires the actual unclaimed assessor')
        return _context_in(lifecycle, db, current, commit=commit, controller_digest=controller_digest,
                           at=time.time() if at is None else at, settled=False)[0]


def prepare_command(lifecycle, assignment, *, category, commit, controller_digest, runtime, observation, at=None):
    """Bind actual coordinator-observed command data; report checks aggregation."""
    context = native_preparation_context(lifecycle, assignment, commit=commit, controller_digest=controller_digest, at=at)
    _runtime(runtime)
    identity = {k: context[k] for k in ('work_id', 'source_digest', 'commit', 'tree')}
    fields = COMMAND_FIELDS - set(identity) - {'format', 'category', 'runtime_digest'}
    require(isinstance(observation, dict) and set(observation) == fields and category in ASSERTIONS,
            'Exact observed command fields/category required')
    value = {'format': COMMAND, **identity, 'category': category, 'runtime_digest': runtime['identity_digest'], **observation}
    # This factory deliberately cannot turn a successful exit into category PASS.
    # The final report rereads all outputs and reconciles actual slot completion.
    require(type(value['exit_code']) is int and type(value['timed_out']) is bool and
            isinstance(value['assertions'], dict) and
            (value['assertions'] or value['exit_code'] != 0 or value['timed_out']) and set(value['assertions']) <= ASSERTIONS[category] and
            all(v in ('passed', 'failed') for v in value['assertions'].values()) and
            _stamp(value['started_at']) <= _stamp(value['completed_at']), 'Malformed observed command result')
    _validate_owned_command(lifecycle.store, value, context, runtime, settled=False)
    return value

def _report_identity(context):
    return {**{k: context[k] for k in ('work_id', 'assignment_id', 'attempt_id', 'decision_id', 'concept_digest', 'recipe_digest',
                                      'source_digest', 'commit', 'tree')},
            'candidate_digest': digest(context['candidate']), 'task_plan_digest': context['task_plan']['sha256']}


def _runtime(value):
    require(isinstance(value, dict) and set(value) == RUNTIME_FIELDS and value['profile'] == 'rnf-backend' and
            all(isinstance(v, str) and 0 < len(v) <= 256 for v in value.values()) and
            all(re.fullmatch(r'sha256:[0-9a-f]{64}', value[k]) for k in
                ('image_digest', 'schema_digest', 'source_fingerprint', 'canonical_fingerprint', 'revision_fingerprint', 'provenance_fingerprint')) and
            value['identity_digest'] == digest({k: v for k, v in value.items() if k != 'identity_digest'}),
            'Exact observed Colima/image/schema/database/import identity required')


def _validate_report(store, report, context, issued):
    from .lifecycle import stopped_with_effects
    require(isinstance(report, dict) and set(report) == REPORT_FIELDS and report['format'] == REPORT and
            all(report[k] == v for k, v in _report_identity(context).items()) and report['category'] in CATEGORIES and
            report['outcome'] in ('passed', 'failed') and
            context['checkpoint']['latest']['ack']['recorded_at'] <= _stamp(report['completed_at']) <= issued and
            isinstance(report['cleanup'],dict) and stopped_with_effects(report['cleanup']), 'Report identity, result, timestamp or cleanup differs')
    category = report['category']
    if category == CATEGORIES[0]:
        require(_stamp(context['assignment_created_at']) <= _stamp(report['completed_at']), 'Source safety review predates this actual assessment')
        review = report['review']
        require(report['operation'] is None and report['runtime'] is None and report['commands'] == [] and
                isinstance(review, dict) and set(review) == {'reviewed_files', 'findings', 'invocation_profiles'} and
                review['reviewed_files'] == sorted(context['source_inventory']) and isinstance(review['findings'], list),
                'Source safety review must cover every exact committed file')
        require(isinstance(review['invocation_profiles'], list), 'Explicit reviewed invocation profiles required')
        profiles = [_validate_profile(store, _read(store, ref), context) for ref in review['invocation_profiles']]
        require(not review['findings'] and sorted((p['category'],p['phase']) for p in profiles) == sorted((c,p) for c,ps in PHASES.items() for p in ps)
                or review['findings'] and profiles == [], 'Passing source review must authorize the exact phase invocation profiles')
        for finding in review['findings']:
            require(isinstance(finding, dict) and set(finding) == {'path', 'line', 'severity', 'detail'} and
                    finding['path'] in context['source_inventory'] and type(finding['line']) is int and finding['line'] > 0 and
                    finding['severity'] in ('blocking', 'major', 'minor') and isinstance(finding['detail'], str) and
                    1 <= len(finding['detail']) <= 16384, 'Invalid retained source safety finding')
        require(report['outcome'] == ('failed' if review['findings'] else 'passed'), 'Source outcome contradicts actual retained findings')
        return
    require(report['review'] is None and isinstance(report['operation'], dict) and
            set(report['operation']) == {'start', 'completion'}, 'Data report must bind its actual completed reservation')
    operations = context['accounting']['operations']
    allowed = ('imports-replay' if category == 'service-restart' else category)
    prefix = 'corrected' if context['cycle'] > 0 else 'initial'
    name = prefix + '-' + allowed
    op = operations.get(name)
    require(op and report['operation'] == {k: op[k] for k in ('start', 'completion')} and op['result'] is not None and
            op['reservation']['slot'] == name and op['reservation']['context']['kind'] == 'coordinator', 'Wrong actual data-operation lineage')
    _runtime(report['runtime'])
    require(isinstance(report['commands'], list) and 1 <= len(report['commands']) <= 100, 'Actual typed command results are required')
    observed, failed, last, phases = {}, False, 0, []
    for ref in report['commands']:
        command = _read(store, ref)
        require(set(command) == COMMAND_FIELDS and command['format'] == COMMAND and
                all(command[k] == context[k] for k in ('work_id', 'source_digest', 'commit', 'tree')) and
                command['category'] == category and command['runtime_digest'] == report['runtime']['identity_digest'] and
                isinstance(command['argv'], list) and 1 <= len(command['argv']) <= 256 and
                all(isinstance(x, str) and x and '\x00' not in x for x in command['argv']) and
                isinstance(command['cwd'], str) and Path(command['cwd']).is_absolute() and
                type(command['exit_code']) is int and -255 <= command['exit_code'] <= 255 and type(command['timed_out']) is bool and
                isinstance(command['cleanup'],dict) and stopped_with_effects(command['cleanup']), 'Typed command result or observed runtime is inconsistent')
        started, completed = _stamp(command['started_at']), _stamp(command['completed_at'])
        require(op['reservation']['started_at'] <= started <= completed <= op['result']['completed_at'] and started >= last,
                'Command falls outside its actual reservation or overlaps a prior command')
        last = completed
        profile = _validate_owned_command(store, command, context, report['runtime'])
        phases.append(profile['phase'])
        assertions = command['assertions']
        require(isinstance(assertions, dict) and set(assertions) <= ASSERTIONS[category] and
                all(v in ('passed', 'failed') for v in assertions.values()) and not set(assertions) & set(observed),
                'Command assertions are missing, unknown or duplicated')
        observed.update(assertions)
        failed = failed or command['exit_code'] != 0 or command['timed_out'] or 'failed' in assertions.values()
    require((phases == list(PHASES[category]) or failed and phases == list(PHASES[category])[:len(phases)]) and
            (failed or set(observed) == ASSERTIONS[category]) and last <= _stamp(report['completed_at']) <= datetime.fromtimestamp(op['result']['completed_at'],timezone.utc).timestamp() and
            report['outcome'] == ('failed' if failed else 'passed'), 'Data result contradicts actual command/assertion evidence')


def prepare_report(lifecycle, assignment, *, category, commit, controller_digest, completed_at,
                   operation=None, runtime=None, commands=(), review=None, cleanup, at=None):
    """Validate a coordinator-observed report before exclusive immutable publication."""
    at = time.time() if at is None else at
    context = release_context(lifecycle, assignment, commit=commit, controller_digest=controller_digest, at=at)
    if category == CATEGORIES[0]:
        outcome = 'failed' if isinstance(review, dict) and review.get('findings') else 'passed'
    else:
        observations = [_read(lifecycle.store, r) for r in commands]
        outcome = 'failed' if any(c.get('exit_code') != 0 or c.get('timed_out') or 'failed' in c.get('assertions', {}).values()
                                  for c in observations) else 'passed'
    value = {'format': REPORT, **_report_identity(context), 'category': category, 'outcome': outcome,
             'completed_at': completed_at, 'operation': operation, 'runtime': runtime,
             'commands': list(commands), 'review': review, 'cleanup': cleanup}
    _validate_report(lifecycle.store, value, context, at)
    return value


def _validate_value(lifecycle, db, assignment, value, *, at):
    fields = {'format', 'context', 'mode', 'issued_at', 'expires_at', 'reports', 'native_preparation'}
    require(isinstance(value, dict) and set(value) == fields and value['format'] == RELEASE, 'Unexpected release schema')
    context, gate, controls = _context_in(lifecycle, db, assignment, commit=value['context']['commit'],
        controller_digest=value['context']['controller_digest'], at=at)
    require(value['context'] == context, 'Release differs from current authority, source, checkpoint or settled operation history')
    issued, expiry = _stamp(value['issued_at']), _stamp(value['expires_at'])
    require(_stamp(assignment['created_at']) <= issued <= at < expiry <= gate['feature_deadline'], 'Release is stale or expired')
    require(isinstance(value['reports'], list) and all(isinstance(i,dict) for i in value['reports']) and [i.get('category') for i in value['reports']] == list(CATEGORIES), 'Every evidence category must be explicit')
    if context['assessment_context']['purpose']=='native_failure_triage':
        require(context['retained_failure_release'] is not None and value['mode']=='native_failure_triage' and
                value['native_preparation'] is None and all(item == {'category':category,'outcome':'not_run',
                    'reference':None,'blocked_by':['retained-native-failure']} for category,item in zip(CATEGORIES,value['reports'])),
                'Triage must retain its actual predecessor failure without rerunning coordinator/native work')
        return {'mode':'native_failure_triage','release':value,'gate':gate,'controls':controls,'runtime':None}
    failed, outcomes, runtime, source_review_at, source_published_at, profiles = set(), [], None, None, None, []
    for item in value['reports']:
        require(set(item) == {'category', 'outcome', 'reference', 'blocked_by'} and item['outcome'] in ('passed', 'failed', 'not_run'), 'Invalid category result')
        outcomes.append(item['outcome'])
        if item['outcome'] == 'not_run':
            require(item['reference'] is None and isinstance(item['blocked_by'], list) and item['blocked_by'] and
                    set(item['blocked_by']) <= failed, 'Missing evidence cannot be presented as an explained failure')
            continue
        require(item['blocked_by'] == [], 'Executed report cannot be marked blocked')
        report = _read(lifecycle.store, item['reference'])
        require(report['category'] == item['category'] and report['outcome'] == item['outcome'], 'Report/category result differs')
        _validate_report(lifecycle.store, report, context, issued)
        if item['category'] == CATEGORIES[0]:
            source_review_at = _stamp(report['completed_at'])
            st = Path(item['reference']['path']).stat()
            source_published_at = max(st.st_mtime,st.st_ctime)
            profiles = report['review']['invocation_profiles']
        else:
            require(outcomes[0] == 'passed' and all(max(source_review_at,source_published_at) <= _stamp(_read(lifecycle.store, r)['started_at']) for r in report['commands']),
                    'Source safety must pass before any downstream data command')
            require(all(_read(lifecycle.store, r)['profile'] in profiles for r in report['commands']),
                    'Command invocation was not authorized by the preceding exact source safety review')
        if report['runtime'] is not None:
            require(runtime is None or runtime == report['runtime'], 'Data evidence belongs to different observed runtime/generation')
            runtime = report['runtime']
        if item['outcome'] == 'failed':
            failed.add(item['category'])
    require(outcomes[0] != 'failed' or all(x == 'not_run' for x in outcomes[1:]), 'Source failure requires every downstream operation not run')
    purpose = context['assessment_context']['purpose']
    expected = ('native_failure_triage' if purpose == 'native_failure_triage' else 'source_failure_review' if outcomes[0] == 'failed'
                else 'data_failure_assessment' if failed else 'normal_assessment')
    require(outcomes[0] != 'not_run' and value['mode'] == expected and (failed or all(x == 'passed' for x in outcomes)), 'Mode misrepresents actual failed or missing evidence')
    proof = {'mode': expected, 'release': value, 'gate': gate, 'controls': controls, 'runtime': runtime}
    _verify_preparation(lifecycle.store, assignment, proof, at=at)
    return proof


def prepare_release(lifecycle, assignment, *, reports, commit, controller_digest, expires_at, native_preparation=None, at=None):
    at = math.floor((time.time() if at is None else at) * 1000000) / 1000000
    from datetime import timezone
    issued_at = datetime.fromtimestamp(at, timezone.utc).isoformat()
    at = _stamp(issued_at)
    context = release_context(lifecycle, assignment, commit=commit, controller_digest=controller_digest, at=at)
    failed = any(r['outcome'] == 'failed' for r in reports)
    mode = ('native_failure_triage' if context['assessment_context']['purpose'] == 'native_failure_triage' else
            'source_failure_review' if reports and reports[0]['outcome'] == 'failed' else
            'data_failure_assessment' if failed else 'normal_assessment')
    value = {'format': RELEASE, 'context': context, 'mode': mode,
             'issued_at': issued_at, 'expires_at': expires_at, 'reports': reports, 'native_preparation': native_preparation}
    with lifecycle.store._connection() as db:
        current = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment['assignment_id'],)).fetchone()
        _validate_value(lifecycle, db, current, value, at=at)
    return value


def _claimed_reference(db, assignment):
    rows = []
    for row in db.execute("SELECT detail FROM lifecycle_event WHERE work_id=? AND kind='launch_intent' ORDER BY event_id", (assignment['work_id'],)):
        value = json.loads(row[0])
        if value.get('assignment_id') == assignment['assignment_id']:
            rows.append(value.get('assessment_evidence'))
    require(len(rows) == 1 and isinstance(rows[0], dict), 'Exact retained launch release is missing')
    return rows[0]


def verify_in(lifecycle, db, assignment, *, at=None, check_source=True):
    _, contract = lifecycle._run(db, assignment['work_id'])
    gate = configuration(contract, lifecycle.store)
    if gate is None or assignment['kind'] != 'assessment':
        return None
    value, reference = _file_ref(lifecycle.store, Path(gate['receipt_root']) / ('assessment-' + assignment['assignment_id'] + '.json'))
    proof = _validate_value(lifecycle, db, assignment, value, at=time.time() if at is None else at)
    if assignment['state'] != 'proposed':
        require(_claimed_reference(db, assignment) == {'reference': reference, 'mode': value['mode']}, 'Claimed release was replaced or rebound')
    if at is None:
        _live_release(proof)
    return {**proof, 'reference': reference}


def _live_release(proof):
    at = time.time()
    require(math.isfinite(at) and _stamp(proof['release']['issued_at']) <= at <
            min(_stamp(proof['release']['expires_at']), proof['gate']['feature_deadline']),
            'Release expired or clock reversed during boundary verification')


def verify(lifecycle, assignment, *, native=False, check=None, output=None):
    if assignment.get('kind') != 'assessment':
        return None
    with lifecycle.store._connection() as db:
        _, contract = lifecycle._run(db, assignment['work_id'])
        if configuration(contract, lifecycle.store) is None:
            return None
        current = db.execute('SELECT * FROM assignment WHERE assignment_id=?', (assignment['assignment_id'],)).fetchone()
        require(current and all(current[k] == assignment[k] for k in ('work_id', 'attempt_id', 'kind', 'candidate', 'concept_digest', 'contract_revision', 'predecessor')), 'Actual assignment differs')
        result = verify_in(lifecycle, db, current)
        _, contract = lifecycle._run(db, current['work_id'])
    if result['mode'] in ('normal_assessment', 'data_failure_assessment') and current['state'] != 'proposed':
        verify_native_handoff(lifecycle.store, current, contract, result, check=None, output=None)
    if native:
        require(result['mode'] in ('normal_assessment', 'data_failure_assessment'), 'This release authorizes read-only independent review, not native execution')
        verify_native_handoff(lifecycle.store, current, contract, result, check=check, output=output)
    _live_release(result)
    return result




def verify_launch_intent(lifecycle, assignment):
    """Validate a saved intent before the scoped controller activates its lease."""
    if assignment.get('kind') != 'assessment':
        return None
    with lifecycle.store._connection() as db:
        _, contract = lifecycle._run(db, assignment['work_id'])
        if configuration(contract, lifecycle.store) is None:
            return None
    return activation_context(lifecycle,assignment)

def activation_context(lifecycle, assignment):
    """Postclaim readback for activating an already prepared native lease only.

    A new lease is possible only before the first runtime record exists. Existing
    runtime records require their already-active exact lease, so this API cannot
    recreate lost authority for a running or completed attempt. Ordinary runtime
    start/poll/native verification always requires the active lease independently.
    """
    from . import startup_failure_recovery as fencing
    with fencing._connection(lifecycle.store) as (db, _):
        current=db.execute('SELECT * FROM assignment WHERE assignment_id=?',(assignment['assignment_id'],)).fetchone()
        require(current and current['state']!='proposed' and current['sent_at'] and all(current[k]==assignment[k] for k in
                ('work_id','assignment_id','attempt_id','kind','candidate','concept_digest','contract_revision','predecessor','sent_at')),
                'Native activation requires the actual original claimed assignment')
        proof=verify_in(lifecycle,db,current)
        require(proof is not None,'Native activation has no retained coordinator release')
        if proof['mode'] in ('source_failure_review','native_failure_triage'):
            _live_release(proof)
            return proof
        _,contract=lifecycle._run(db,current['work_id'])
        runtime=db.execute('SELECT record FROM runtime.attempt WHERE id=?',(current['attempt_id'],)).fetchone()
        try:
            verify_native_handoff(lifecycle.store,current,contract,proof,check=None,output=None)
            _live_release(proof)
            return proof
        except (Rejected,OSError,ValueError,RuntimeError,KeyError,TypeError):
            require(runtime is None and current['state']=='sent','An existing attempt cannot recreate native lease authority')
        module=_native_module(lifecycle.store,proof['controls'])
        context=proof['release']['context']
        identity={k:context[k] for k in ('work_id','decision_id','concept_digest','recipe_digest','source_digest','catalog_digest','commit','assignment_id','attempt_id')}
        try:
            module.LeaseStore(proof['controls']['native_handoff']['task_root']).verify_prepared_template(
                identity,proof['release']['native_preparation'])
        except RuntimeError as error:
            raise Rejected('assessment_evidence','Fresh claimed native template is unavailable or conflicts with an existing lease') from error
        _live_release(proof)
        return proof

def completion_error(lifecycle, db, assignment, result):
    try:
        proof = verify_in(lifecycle, db, assignment)
        if proof is None:
            return None
        if proof['mode'] != 'normal_assessment' and result.get('status') == 'completed':
            return 'Negative coordinator evidence cannot deliver; retain the actual independent disposition'
        if proof['mode'] in ('source_failure_review', 'native_failure_triage') and (
                result.get('checks') or result.get('native_checks') or result.get('initial_reports', {}).get('execution')):
            return 'Read-only failure review cannot contain native execution'
    except (Rejected, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
        return 'Coordinator evidence is missing, changed or inconsistent: ' + str(error)
    return None


def _native_module(store, controls):
    pin = controls['native_handoff']; path = pin['module_path']
    reference = {'path': path, 'sha256': controls['native_handoff_sha256']}
    require(reference in controls['input_files'], 'Native helper bytes are not in the exact saved input authority')
    raw = _read(store, reference, raw=True)
    schema_ref = next((r for r in controls['input_files'] if r['path'] == pin['schema_path']), None)
    require(schema_ref is not None, 'Native protocol schema is not approved')
    schema = _read(store, schema_ref)
    companion_path = str(Path(path).with_name('coverage_contract.py'))
    companion_ref = next((r for r in controls['input_files'] if r['path'] == companion_path), None)
    require(companion_ref is not None, 'Native coverage companion has no exact saved sibling input pin')
    _read(store, companion_ref, raw=True)
    name = '_aos_approved_native_' + reference['sha256']
    module = sys.modules.get(name)
    if module is None:
        import types
        module = types.ModuleType(name); module.__file__ = path; sys.modules[name] = module
        try:
            exec(compile(raw, path, 'exec'), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None); raise
    require(digest(schema) == digest(module.PROTOCOL_SCHEMA), 'Native helper and approved protocol schema disagree')
    require(companion_ref['sha256'] == module.COVERAGE_CONTRACT_SHA256,
            'Native helper and separately approved coverage companion disagree')
    return module


def verify_native_handoff(store, assignment, contract, proof, *, check, output):
    controls = proof['controls']; context = proof['release']['context']
    module = _native_module(store, controls)
    require(assignment['sent_at'] and (check is None or check in contract['runtime_recipe']['checks']), 'Native attempt/check is not the actual approved assignment')
    expected = module.ExpectedBinding(work_id=assignment['work_id'], decision_id=context['decision_id'],
        concept_digest=assignment['concept_digest'], recipe_digest=contract['runtime_recipe_digest'],
        source_digest=context['source_digest'], catalog_digest=context['catalog_digest'], commit=context['commit'], assignment_id=assignment['assignment_id'],
        attempt_id=assignment['attempt_id'], run_outputs=str(Path(contract['runtime_recipe']['outputs']) / assignment['attempt_id']),
        feature_deadline=proof['gate']['feature_deadline'], job_deadline=_stamp(assignment['sent_at']) + contract['runtime_recipe']['max_seconds'])
    lease = module.LeaseStore(controls['native_handoff']['task_root'])
    observed = lease.verify_native(expected, check['name'], str(output)) if check is not None else None
    resolved = lease.resolve(expected)
    envelope = resolved['envelope'] if 'envelope' in resolved else resolved
    require(proof['runtime'] and envelope['runtime']['identity_digest'] == proof['runtime']['identity_digest'] and
            envelope['schema_digest'] == proof['runtime']['schema_digest'], 'Active native lease differs from released runtime/schema')
    _live_release(proof)
    return observed


def _verify_preparation(store, assignment, proof, *, at):
    ref = proof['release']['native_preparation']
    if proof['mode'] in ('source_failure_review', 'native_failure_triage'):
        require(ref is None, 'Read-only failure review must not claim native resources')
        return
    controls, context = proof['controls'], proof['release']['context']
    module = _native_module(store, controls)
    require(isinstance(ref, dict) and Path(ref.get('path', '')).is_relative_to(controls['native_handoff']['prepared_root']),
            'Native preparation is outside the exact protected prepared root')
    template = module.read_json(module.verify_ref(ref, root=Path(controls['native_handoff']['prepared_root'])))
    identity = {k: context[k] for k in ('work_id', 'decision_id', 'concept_digest', 'recipe_digest', 'source_digest', 'catalog_digest', 'commit', 'assignment_id', 'attempt_id')}
    require(template['binding_identity'] == identity, 'Prepared native template belongs to another actual assignment')
    census = module.read_json(module.verify_ref(template['resource_census']))
    operation = module.read_json(module.verify_ref(census['operation_evidence']))
    name = ('corrected' if context['cycle'] > 0 else 'initial') + '-final-verification'
    actual = context['accounting']['operations'].get(name)
    require(actual and actual['result'] and operation['reservation_id'] == name and
            operation['authority_digest'] == digest(actual['reservation']) and
            operation['started_at'] == actual['reservation']['started_at'] and
            operation['deadline'] == operation['started_at'] + actual['reservation']['seconds'] and
            operation['original_feature_deadline'] == proof['gate']['feature_deadline'] and
            actual['reservation']['started_at'] <= template['prepared_at'] <= actual['result']['completed_at'],
            'Prepared native resources lack their exact actual completed final-verification charge')
    require(proof['runtime'] and template['body']['runtime']['identity_digest'] == proof['runtime']['identity_digest'] and
            template['body']['schema_digest'] == proof['runtime']['schema_digest'], 'Prepared native runtime differs from completed coordinator evidence')
    if assignment['state'] == 'proposed':
        try:
            module.LeaseStore(controls['native_handoff']['task_root']).verify_prepared_template(identity, ref, now=at)
        except RuntimeError as error:
            raise Rejected('assessment_evidence', 'Prepared native handoff is unavailable or inconsistent') from error



def tool_identity(path):
    """Pin an ordinary executable separately from the smaller evidence-file cap."""
    requested=Path(path)
    require(requested.is_absolute(), 'Absolute invocation executable required')
    resolved=requested.resolve(strict=True);before=resolved.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid in (0,os.geteuid()) and
            before.st_mode & 0o111 and not before.st_mode & 0o022 and 0 < before.st_size <= 64*1024*1024,
            'Invocation executable exceeds the reviewed ordinary-file limit')
    raw=resolved.read_bytes();after=resolved.stat()
    signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mode,s.st_uid,s.st_gid,s.st_mtime_ns,s.st_ctime_ns)
    require(signature(before)==signature(after) and len(raw)==before.st_size and requested.resolve(strict=True)==resolved,
            'Invocation executable changed during readback')
    return {'path':str(requested),'resolved':str(resolved),'sha256':hashlib.sha256(raw).hexdigest()}

def _validate_profile(store, profile, context):
    fields = {'format', 'work_id', 'decision_id', 'concept_digest', 'recipe_digest', 'source_digest', 'commit', 'tree',
              'task_plan_digest', 'category', 'phase', 'operation_slot', 'entrypoint', 'argv', 'cwd', 'tools',
              'manifest', 'inputs', 'output_root', 'runtime_target', 'environment', 'environment_sha256'}
    require(isinstance(profile,dict) and set(profile)==fields and profile['format']==PROFILE and
            all(profile[k]==context[k] for k in ('work_id','decision_id','concept_digest','recipe_digest','source_digest','commit','tree')) and
            profile['task_plan_digest']==context['task_plan']['sha256'] and profile['category'] in PHASES and
            profile['phase'] in PHASES[profile['category']], 'Invocation profile is outside the exact approved final candidate/phase')
    category=profile['category']; slot=('imports-replay' if category=='service-restart' else category)
    require(profile['operation_slot']==('corrected' if context['cycle']>0 else 'initial')+'-'+slot,
            'Invocation profile names the wrong finite operation slot')
    require(profile['entrypoint']=={'module':'rhythm_form.acceptance','path':'rhythm_form/acceptance.py',
            'inventory':context['source_inventory'].get('rhythm_form/acceptance.py')} and
            profile['entrypoint']['inventory'] is not None,'Invocation must use the exact reviewed committed acceptance entrypoint')
    require(isinstance(profile['tools'],list) and 1<=len(profile['tools'])<=2,'Exact Python/launcher tool pins required')
    for tool in profile['tools']:
        require(set(tool)=={'path','resolved','sha256'} and Path(tool['path']).is_absolute() and
                tool_identity(tool['path'])==tool,'Actual invocation tool bytes/path changed')
    environment=profile['environment']
    require(isinstance(environment,dict) and {'PATH','LANG','PYTHONDONTWRITEBYTECODE'} <= set(environment) <=
            {'PATH','LANG','LC_ALL','LC_CTYPE','HOME','TMPDIR','UV_CACHE_DIR','UV_NO_CONFIG','PYTHONDONTWRITEBYTECODE'} and
            environment['PYTHONDONTWRITEBYTECODE']=='1' and all(isinstance(k,str) and isinstance(v,str) and '\x00' not in v for k,v in environment.items()) and
            profile['environment_sha256']==hashlib.sha256(json.dumps(environment,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'Invocation must pin a clean explicit environment without alternate Python/UV loading')
    argv=profile['argv']; tool=profile['tools'][0]
    prefix=[tool['path'],'-m','rhythm_form.acceptance']
    if Path(tool['path']).name=='uv':
        require(environment.get('UV_NO_CONFIG')=='1' and len(profile['tools'])==2 and Path(profile['tools'][1]['path']).name in ('python','python3'), 'Locked uv needs the exact project interpreter pin')
        prefix=[tool['path'],'run','--locked',profile['tools'][1]['path'],'-m','rhythm_form.acceptance']
    else:
        require(len(profile['tools'])==1 and re.fullmatch(r'python(?:3(?:\.[0-9]+)?)?',Path(tool['path']).name),'Only the reviewed Python entrypoint launcher is supported')
    require(argv==prefix+['--phase',profile['phase'],'--manifest',profile['manifest']['path']], 'Invocation argv differs from the fixed phase interface')
    manifest=_read(store,profile['manifest'])
    expected={'format':'aos-coordinator-phase-input-v1','work_id':context['work_id'],'source_digest':context['source_digest'],
              'commit':context['commit'],'tree':context['tree'],'task_plan_digest':context['task_plan']['sha256'],
              'phase':profile['phase'],'runtime_target':profile['runtime_target'],'inputs':profile['inputs'],'output_root':profile['output_root']}
    require(manifest==expected,'Actual phase input manifest differs from its reviewed profile')
    require(isinstance(profile['runtime_target'],dict) and set(profile['runtime_target'])==TARGET_FIELDS and
            profile['runtime_target']['profile']=='rnf-backend','Exact runtime target is required')
    require(isinstance(profile['inputs'],list) and 1<=len(profile['inputs'])<=32,'Exact protected runtime/config/input references required')
    for ref in profile['inputs']:
        _read(store,ref,raw=True)
    # This path can be absent before execution, but is always inside the protected
    # coordinator evidence root; it can never name source, VM state or originals.
    output=Path(profile['output_root'])
    require(output == Path(context['evidence_root']) / context['assignment_id'] / context['attempt_id'] / ('phase-' + profile['phase']),
            'Phase output must be the exact assignment/attempt/phase custody directory')
    require(output.is_absolute() and output.resolve()==output and not any(p.is_symlink() for p in (output,*output.parents)), 'Ordinary exact output path required')
    require(not any(output == root or output.is_relative_to(root) or root.is_relative_to(output) for root in store.worker_writable_roots),
            'Phase output overlaps a worker-writable root')
    retained._protected(store, output.parent, directory=True)
    require(profile['cwd']==context['source_root'],'Final acceptance command must run from the reviewed source snapshot')
    return profile


def prepare_invocation_profile(lifecycle, assignment, *, category, phase, commit, controller_digest,
                               tools, manifest, inputs, output_root, runtime_target, environment, at=None):
    context=release_context(lifecycle,assignment,commit=commit,controller_digest=controller_digest,at=at)
    tool=tools[0]['path']
    prefix=[tool,'run','--locked',tools[1]['path'],'-m','rhythm_form.acceptance'] if Path(tool).name=='uv' else [tool,'-m','rhythm_form.acceptance']
    slot='imports-replay' if category=='service-restart' else category
    value={'format':PROFILE,**{k:context[k] for k in ('work_id','decision_id','concept_digest','recipe_digest','source_digest','commit','tree')},
        'task_plan_digest':context['task_plan']['sha256'],'category':category,'phase':phase,
        'operation_slot':('corrected' if context['cycle']>0 else 'initial')+'-'+slot,
        'entrypoint':{'module':'rhythm_form.acceptance','path':'rhythm_form/acceptance.py','inventory':context['source_inventory'].get('rhythm_form/acceptance.py')},
        'argv':prefix+['--phase',phase,'--manifest',manifest['path']],'cwd':context['source_root'],'tools':tools,
        'manifest':manifest,'inputs':inputs,'output_root':output_root,'runtime_target':runtime_target,
        'environment':environment,'environment_sha256':hashlib.sha256(json.dumps(environment,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    return _validate_profile(lifecycle.store,value,context)


def _validate_owned_command(store, command, context, runtime, *, settled=True):
    from . import owned_host_command
    profile=_validate_profile(store,_read(store,command['profile']),context)
    require(command['category']==profile['category'] and command['argv']==profile['argv'] and command['cwd']==profile['cwd'] and
            all(runtime[k]==v for k,v in profile['runtime_target'].items()),'Actual command/runtime differs from the reviewed invocation')
    snapshot=_read(store,command['owned_execution'])
    try:
        actual=owned_host_command.verify_completed(snapshot)
    except (OSError,ValueError,RuntimeError) as error:
        raise Rejected('assessment_evidence','Actual maintained owned command is not positively cleaned') from error
    require(actual==snapshot and actual.get('format')=='owned-host-command-v1' and actual.get('controller_finished') is True and
            actual.get('launch_authorized') is True and actual.get('boundary_rechecked') is True and
            actual.get('no_launch') is not True and actual.get('environment_sha256')==profile['environment_sha256'] and actual.get('argv')==profile['argv'] and actual.get('cwd')==profile['cwd'] and
            actual.get('child',{}).get('exit_code')==command['exit_code'] and actual.get('timed_out')==command['timed_out'] and
            datetime.fromtimestamp(actual.get('started_at'),timezone.utc).timestamp()==_stamp(command['started_at']) and
            datetime.fromtimestamp(actual.get('checkpoint_at'),timezone.utc).timestamp()==_stamp(command['completed_at']),
            'Command result does not match its actual maintained executor receipt')
    operation=context['accounting']['operations'].get(profile['operation_slot'])
    require(operation and (operation['result'] is not None or not settled) and operation['reservation']['started_at'] <= actual['started_at'] <=
            actual['checkpoint_at'] <= (operation['result']['completed_at'] if operation['result'] else operation['reservation']['started_at'] + operation['reservation']['seconds']), 'Actual executor chronology exceeds its exact reservation')
    require(command['exit_code'] != 0 or command['timed_out'] or actual.get('passed') is True,
            'A zero exit cannot hide a failed maintained execution/ownership result')
    control=Path(actual['control'])
    require(control.is_relative_to(profile['output_root']),'Command owner control is outside its reviewed output root')
    for key in ('stdout','stderr'):
        require(command[key]['path']==str(control/(key+'.log')),'Command stream was substituted')
        _read(store,command[key],raw=True)
    failure=command['exit_code']!=0 or command['timed_out']
    if command['phase_result'] is None:
        require(failure and command['assertions']=={},'A successful command needs its actual structured phase result')
    else:
        result=_read(store,command['phase_result'])
        require(command['phase_result']['path']==str(Path(profile['output_root'])/'result.json') and
                set(result)=={'format','phase','source_digest','commit','tree','runtime_digest','assertions'} and
                result['format']=='aos-coordinator-phase-result-v1' and result['phase']==profile['phase'] and
                all(result[k]==command[k] for k in ('source_digest','commit','tree','runtime_digest','assertions')),
                'Command assertions are not its exact observed structured phase result')
    return profile


def terminal_cleanup(lifecycle, assignment):
    """Read actual maintained terminal/owned-stop evidence for lease closure.

    This never cancels, reconnects, resumes, signals, reconciles or changes rows.
    Disposable qualification processes use their separate owned-command proof.
    """
    from . import startup_failure_recovery as fencing, reviewed_host
    from .lifecycle import stopped_with_effects, stop_conflicts_in
    with fencing._connection(lifecycle.store) as (db, _):
        current = db.execute('SELECT * FROM assignment WHERE assignment_id=? AND attempt_id=? AND work_id=?',
            (assignment['assignment_id'], assignment['attempt_id'], assignment['work_id'])).fetchone()
        require(current and current['kind']=='assessment' and current['state'] in ('completed','failed','cancelled') and
                current['terminal_at'] and not stop_conflicts_in(db,current['work_id']), 'Exact assessment has no uncontradicted terminal disposition')
        raw = db.execute('SELECT record FROM runtime.attempt WHERE id=?',(current['attempt_id'],)).fetchone()
        require(raw is not None,'Exact retained runtime attempt is missing')
        record=json.loads(raw[0]); recorded=record.get('assignment',{})
        require(all(recorded.get(k)==current[k] for k in ('work_id','assignment_id','attempt_id','concept_digest','contract_revision','candidate','sent_at')) and
                record.get('attempt_id')==current['attempt_id'] and record.get('phase') in ('terminal','cancelled'),
                'Retained runtime/assignment identity or terminal state differs')
        stop=record.get('stop_receipt'); require(isinstance(stop,dict) and stopped_with_effects(stop) and
                stop.get('attempt_id')==current['attempt_id'], 'Positive actual model/native stop is missing')
        intents=[dict(r) for r in db.execute('SELECT * FROM owned_stop WHERE attempt_id=?',(current['attempt_id'],))]
        require(all(r['state']=='stopped' and json.loads(r['receipt'])==stop for r in intents),'Owned-stop history differs from actual retained cleanup')
        result=json.loads(current['result']) if current['result'] else None
        if current['state']!='cancelled':
            require(result and stopped_with_effects(result) and record.get('terminal_observation',{}).get('result')==result,
                    'Independent terminal result is not the actual retained runtime result')
        else:
            require(intents,'Cancellation requires positively reconciled original owned-stop authority')
        native=[]
        for row in db.execute('SELECT record FROM runtime.native_check WHERE attempt_id=?',(current['attempt_id'],)):
            value=json.loads(row[0]);require(value.get('attempt_id')==current['attempt_id'],'Foreign native row')
            reviewed_host.verify_completed(value)
            native.append({'check_id':value['check_id'],'record_digest':digest(value)})
        terminal=_stamp(current['terminal_at'])
        require(_stamp(current['sent_at'])<=terminal<=time.time(),'Invalid actual terminal chronology')
        return {'format':'aos-assessment-terminal-cleanup-v1','work_id':current['work_id'],
                'assignment_id':current['assignment_id'],'attempt_id':current['attempt_id'],'state':current['state'],
                'terminal_at':current['terminal_at'],'assignment_digest':digest(dict(current)),
                'runtime_digest':digest(record),'stop_digest':digest(stop),'native':native}


def invocation_guard(lifecycle, assignment, *, profile_reference, source_safety_reference, at=None):
    """Read-only before-GO callback for the existing maintained owned executor.

    Call this both before run_owned and from its before_go callback. It creates
    no process, output, reservation or scheduler state. The exact named operation
    must already be live, and the source safety report must already be published.
    """
    at=time.time() if at is None else at
    profile=_read(lifecycle.store,profile_reference)
    context=native_preparation_context(lifecycle,assignment,commit=profile['commit'],
                                      controller_digest=profile.get('controller_digest') or _controller_digest_for(lifecycle,assignment),at=at)
    _validate_profile(lifecycle.store,profile,context)
    report=_read(lifecycle.store,source_safety_reference)
    _validate_report(lifecycle.store,report,context,at)
    require(report['category']==CATEGORIES[0] and report['outcome']=='passed' and
            profile_reference in report['review']['invocation_profiles'], 'Invocation has no prior exact passing source safety authority')
    st=Path(source_safety_reference['path']).stat()
    require(max(st.st_mtime,st.st_ctime)<=at,'Source safety publication is in the future')
    operation=context['accounting']['operations'].get(profile['operation_slot'])
    require(operation and operation['result'] is None and operation['reservation']['started_at']<=at<
            operation['reservation']['started_at']+operation['reservation']['seconds'],'Invocation lacks its live original named reservation')
    return profile


def _controller_digest_for(lifecycle,assignment):
    with lifecycle.store._connection() as db:
        _,contract=lifecycle._run(db,assignment['work_id'])
        _,controls,_=_authority(lifecycle,db,contract,assignment['work_id'])
        return digest(controls)
