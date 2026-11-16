"""Disposable state and synthetic metadata receipts only; no real readiness/model."""
from di_plan_fixture import synthetic_plan
import copy
import fcntl
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import subprocess
import time

import pytest

from agentic_tools.aos import instructions
from agentic_tools.aos import predispatch_feature_amendment as rebind
from agentic_tools.aos.lifecycle import Lifecycle
from agentic_tools.aos.store import Store, Rejected, canonical, digest
from test_aos_lifecycle import contract
from test_aos_store import TEAM as BASE_TEAM
TEAM = [{**BASE_TEAM[0], 'name': name, 'role': role} for name, role in (('writer', 'Implementer'), ('assessor', 'Independent assessor'))]
from test_aos_legacy_recovery import legacy


def write(path, value):
    path.write_text(canonical(value) + '\n')
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def approve(store, work, concept_digest, operation):
    # Explicit fixture seam: this is no claim of an actual user interaction.
    cap = store.host_capability('fixture-qualified-host', 'SIMULATED:unit-test-only')
    challenge = store.begin_approval(operation, work, store.read(work)['revision'], concept_digest, capability=cap)
    return store.finish_approval(challenge['challenge_id'], 'accept', True, capability=cap,
        exact_answer='Approve fixture', source_channel='fixture-host', interaction_reference=operation)['receipt']


class Fixture:
    EXTRA_SOURCE = {}
    APPROVED_READINESS_COPY = False
    READINESS_ATTEMPTS = 1
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path.resolve()
        self.work = 'RF-BACKEND-001'
        self.source, self.outputs, self.private = [self.root / p for p in ('source', 'outputs', 'private')]
        for p in (self.source, self.outputs, self.private): p.mkdir(mode=0o700)
        self.home = self.root / 'home'; self.home.mkdir(mode=0o700)
        (self.home / '.codex').mkdir(mode=0o700)
        self.config = self.home / '.codex/config.toml'; self.config.write_text('model = "old-model"\n')
        monkeypatch.setenv('HOME', str(self.home))
        (self.source / 'fixture.py').write_text('VALUE = 1\n')
        for name, content in self.EXTRA_SOURCE.items():
            path = self.source / name; path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        self.store = Store(self.root / 'state', (self.source, self.outputs))
        self.lifecycle = Lifecycle(self.store)
        self.store.introduce('intro', self.work, 'Fixture', ['source'], TEAM)
        self.recipe = {'execution_policy': 'isolated-checkout-v1', 'source': str(self.source), 'outputs': str(self.outputs),
            'read_roots': [], 'native_write_roots': [], 'native_protected_paths': [], 'max_seconds': 14400,
            'probe_interpreter': str(Path(sys.executable).resolve()), 'requires_detached_processes': False,
            'native_process_ownership': 'host-owned-v1', 'implementation_prompt': 'Fixture writer', 'assessment_prompt': 'Fixture reviewer',
            'checks': [{'name': 'fixture', 'argv': ['python', '-m', 'pytest'], 'scope': 'Fixture', 'timeout_seconds': 600}],
            'checkout_policy': {'change_roots': ['fixture.py', *self.EXTRA_SOURCE], 'protected_roots': [], 'native_validation': 'reviewed-host-v1',
                'endpoint_limits': {'minimum_free_bytes': 1, 'max_owned_growth_bytes': 10000, 'max_output_bytes': 10000}},
            'instruction_manifest': instructions.discover(self.source)}
        self.readiness = self.private / 'readiness'; self.readiness.mkdir(mode=0o700)
        (self.readiness / 'control').mkdir(mode=0o700)
        self.journal = self.readiness / 'control/readiness.json'
        entries = []
        for index in range(1, self.READINESS_ATTEMPTS + 1):
            rid = 'readiness-' + str(index)
            initial = self.result(self.recipe, rid)
            initial_ref = write(self.readiness / 'control' / (rid + '.json'), initial)
            entries.append(self.entry(rid, initial_ref))
        write(self.journal, {'format': 'aos-readiness-v1', 'state_dir': str(self.store.directory), 'source': str(self.source),
            'max_seconds': 600, 'max_attempts': 3, 'runs': entries})
        if self.APPROVED_READINESS_COPY:
            copied = self.private / 'approved-readiness-copy.json'
            copied.write_text(json.dumps(self.approved_readiness_value(copy.deepcopy(initial)),
                                         ensure_ascii=False, indent=2) + '\n')
            initial_ref = rebind._file(copied)[1]
        inputs, hashes = [], {}
        for key in rebind.CODE_FIELDS:
            p = self.private / (key + '.py'); p.write_text('# original ' + key + '\n')
            ref = rebind._file(p)[1]; inputs.append(ref); hashes[key] = ref['sha256']
        inputs.append(initial_ref)
        self.control = self.private / 'control'; self.control.mkdir(mode=0o700)
        (self.control / 'clock.lock').touch(mode=0o600)
        self.repository = self.root / 'repository'; self.repository.mkdir(mode=0o700)
        self.acks = self.private / 'acks'; self.acks.mkdir(mode=0o700)
        self.git('init', '-b', 'codex/rf-backend-001')
        self.git('config', 'user.email', 'fixture@example.invalid'); self.git('config', 'user.name', 'Synthetic test')
        (self.repository / 'fixture.py').write_text('VALUE = 0\n')
        self.git('add', 'fixture.py'); self.git('commit', '-m', 'synthetic kickoff')
        kickoff = self.git('rev-parse', 'HEAD')
        (self.repository / 'fixture.py').write_text((self.source / 'fixture.py').read_text())
        self.git('add', 'fixture.py'); self.git('commit', '-m', 'synthetic baseline')
        for name, content in self.EXTRA_SOURCE.items():
            path=self.repository/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        if self.EXTRA_SOURCE:
            kickoff=self.baseline_commit if hasattr(self,'baseline_commit') else self.git('rev-parse','HEAD')
            self.git('add', *self.EXTRA_SOURCE);self.git('commit','-m','synthetic extra baseline')
        self.baseline_commit = self.git('rev-parse', 'HEAD')
        self.old_controller = {'format': 'rnf-controller-contract-v1', 'work_id': self.work,
            'state_dir': str(self.store.directory), 'worker_roots': list(map(str, self.store.worker_writable_roots)),
            'control_root': str(self.control), 'repository': str(self.repository), 'ack_root': str(self.acks),
            'kickoff_sha': kickoff, 'stage_order': ['stage1-storage','stage1-runtime','stage2-import'], 'wall_seconds': 172800, 'active_seconds': 21600,
            'coordinator_operations': ['baseline-commit'], 'input_files': inputs, **hashes,
            'baseline_inventory': {str(p.relative_to(self.source)):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                'size':p.stat().st_size,'mode':'100644'} for p in sorted(self.source.rglob('*')) if p.is_file()}}
        self.old_path = self.private / 'old-controller.json'; write(self.old_path, self.old_controller)
        self.new_controller = copy.deepcopy(self.old_controller)
        for key in rebind.CODE_FIELDS:
            p = self.private / ('new-' + key + '.py'); p.write_text('# new ' + key + '\n')
            ref = rebind._file(p)[1]; self.new_controller['input_files'].append(ref); self.new_controller[key] = ref['sha256']
        gate_root = self.private / 'assessment'; gate_root.mkdir(mode=0o700)
        native_root = self.private / 'checkpoint-acks/native'; native_root.mkdir(mode=0o700, parents=True)
        di = self.private / 'di.html'; di.write_text('Revised synthetic exact feature DI')
        task = self.private / 'task-plan.json'; task.write_text('{}')
        helper = self.private / 'rnf_native_handoff.py'; helper.write_text('# synthetic helper fixture only\n')
        schema = self.private / 'native-schema.json'; schema.write_text('{}')
        catalog = self.private / 'metadata/native-input-catalog-v2/catalog.json'
        catalog.parent.parent.mkdir(mode=0o700)
        catalog.parent.mkdir(mode=0o700); catalog.write_text('{}')
        refs = [rebind._file(p)[1] for p in (di, task, helper, schema)]
        self.new_controller['input_files'].extend(refs)
        catalog_ref = rebind._file(catalog)[1]; self.new_controller['input_files'].append(catalog_ref)
        self.new_controller.update(active_seconds=72000, coordinator_reservation_plan=rebind.PLAN,
            native_catalog=str(catalog), native_catalog_sha256=catalog_ref['sha256'],
            coordinator_operations=list(rebind.SLOTS),
            checkpoint_wait_seconds={'stage1-storage':900,'stage1-runtime':4500,'stage2-import':900,'repair':900},
            approved_di=refs[0], approved_task_plan=refs[1], native_handoff_sha256=refs[2]['sha256'],
            native_handoff={'format':'rnf-native-handoff-controller-v1','module_path':str(helper),'schema_path':str(schema),
                'task_root':str(self.root),'prepared_root':str(self.root / 'private/metadata/native-prepared')},
            assessment_evidence_gate={'format':'aos-assessment-evidence-gate-v1','receipt_root':str(gate_root),
                'native_handoff_root':str(native_root),'categories':['exact-source-safety-review','imports-replay','service-restart','backup-restore','final-verification'],
                'feature_deadline':0})
        self.new_recipe = copy.deepcopy(self.recipe)
        self.new_recipe.update(max_seconds=28800,implementation_prompt='Read '+str(di)+' and '+str(task),
            assessment_prompt='Read '+str(di)+' and '+str(task))
        self.recipe_path = self.private / 'new-recipe.json'; write(self.recipe_path,self.new_recipe)
        self.update_path = self.private / 'concept-update.json'
        write(self.update_path,{'body':'Exact bounded revised fixture feature',
            'decision_context':{k:'Synthetic exact amendment; unchanged start/charges, zero execution until new saved approval.' for k in
                ('problem','example','effort','dependencies','evidence','stopping_point')},
            'recommendation':'Review bounded amendment','alternative':'Keep held','consequence':'No execution without exact approval'})
        self.new_path = self.private / 'new-controller.json'; write(self.new_path, self.new_controller)
        bounds = contract({'digest': 'placeholder'}, 'feature-change', execution_policy='isolated-checkout-v1',
            max_assignments=10, max_seconds=14400, max_startup_retries=2,
            resources=['source', 'rnf-controller:' + digest(self.old_controller)], runtime_recipe=self.recipe,
            runtime_recipe_digest=digest(self.recipe), product_budget={'budget_id': 'fixture-budget', 'product_id': 'fixture',
                'ledger_directory': str(self.store.directory), 'max_assignments': 10, 'max_job_seconds': 14400, 'max_seconds': 144000})
        bounds.pop('concept_revision'); bounds.pop('concept_digest')
        old = self.store.present('present', self.work, 1, 'FIXTURE-DI', 1, 'Unchanged feature', ['Check fixture'], TEAM,
                                 bounds['authority'], execution_envelope=bounds, di_plan=synthetic_plan(bounds))
        self.digest = old['digest']
        self.lifecycle.register('register', self.work, {**bounds, 'concept_revision': 1, 'concept_digest': self.digest})
        self.decision = approve(self.store, self.work, self.digest, 'original-decision')
        approved = datetime.fromisoformat(self.decision['created_at']).timestamp()
        start = {'identity': self.digest, 'operation': 'baseline-commit', 'seconds': 3300, 'started_at': approved}
        write(self.control / 'operation-baseline-commit.json', start)
        write(self.control / 'completed-baseline-commit.json', {'start_digest': digest(start), 'completed_at': approved})
        write(self.control / 'clock.json', {'identity': self.digest, 'approved_at': approved, 'deadline': approved + 172800,
            'active_limit': 21600, 'spent': 3300, 'last': approved, 'open': None, 'held': False})
        self.new_controller['assessment_evidence_gate']['feature_deadline'] = approved + 172800
        write(self.new_path,self.new_controller)
        for p in self.control.glob('*-*.json'): p.chmod(0o400)
        with sqlite3.connect(self.store.directory / 'codex-runtime.sqlite3') as db:
            db.execute('CREATE TABLE attempt (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE native_check (id TEXT PRIMARY KEY,attempt_id TEXT,record TEXT NOT NULL)')
            db.execute('CREATE TABLE runtime_budget (id TEXT PRIMARY KEY,record TEXT NOT NULL)')
            db.execute('CREATE TABLE product_budget_scope (scope TEXT PRIMARY KEY,budget_id TEXT NOT NULL)')
        (self.store.directory / 'codex-runtime.sqlite3').chmod(0o600)
        self.destination = self.private / 'refresh'; self.destination.mkdir(mode=0o700)
        self.request = {'operation_id': 'refresh-settings', 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'], 'digest': self.digest,
            'decision_id': self.decision['decision_id'], 'original_controller_contract_path': str(self.old_path),
            'controller_contract_path': str(self.new_path), 'original_readiness_receipt_path': initial_ref['path'],
            'readiness_directory': str(self.readiness), 'destination_directory': str(self.destination),
            'recipe_path':str(self.recipe_path),'concept_update_path':str(self.update_path)}
        self.config.write_text('model = "new-model"\n')
        self.calls = []
        monkeypatch.setattr(rebind.readiness, 'run_readiness', self.metadata)

    def git(self, *args):
        return subprocess.run(['git','-c','core.fsmonitor=false','-C',str(self.repository),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.decode().strip()

    def approved_readiness_value(self, value):
        return value

    def result(self, recipe, rid):
        return {'id': rid, 'ready': True, 'status': 'passed', 'simulated': False, 'model_turns': 0, 'product_attempts': 0,
            'limits': {'max_attempts': 3, 'max_seconds': 600},
            'product_reservations': 0, 'cleanup_verified': True, 'readiness_scope': 'model-metadata-only',
            'requested_native_checks': [], 'native': {'checks': [], 'execution': 'not_requested'},
            'roles': [{'role': role, 'passed': True, 'configuration_verified': True, 'model_turns': 0,
                'thread_id': 'fixture-only', 'cleanup': {'verified': True, 'no_launch': True}} for role in ['implementation', 'assessment']],
            'prepared_recipe': copy.deepcopy(recipe), 'runtime_identity': rebind.readiness._runtime_identity(),
            'resource_output': str(self.readiness / 'outputs' / rid)}

    def entry(self, rid, ref):
        return {'id': rid, 'input_key': 'fixture', 'receipt': ref['path'], 'sha256': ref['sha256'], 'state': 'passed'}

    def metadata(self, recipe, **kwargs):
        self.calls.append(kwargs)
        journal = json.loads(self.journal.read_text()); rid = 'readiness-' + str(len(journal['runs']) + 1)
        result = self.result(recipe, rid); ref = write(self.readiness / 'control' / (rid + '.json'), result)
        journal['runs'].append(self.entry(rid, ref)); write(self.journal, journal)
        return result

    def prepare(self):
        return rebind.prepare(self.lifecycle, self.request)

    def publish(self, prepared):
        presented = self.store.present(**prepared['packet']['concept'])
        saved = approve(self.store, self.work, presented['digest'], 'technical-decision')
        return {'operation_id': self.request['operation_id'], 'work_id': self.work,
            'expected_work_revision': self.store.read(self.work)['revision'], 'digest': presented['digest'],
            'decision_id': saved['decision_id'], 'packet_path': prepared['packet_path'], 'packet_sha256': prepared['packet_sha256']}



@pytest.fixture
def f(tmp_path,monkeypatch):
    return Fixture(tmp_path,monkeypatch)


def applied(f):
    prepared=f.prepare(); request=f.publish(prepared); receipt=rebind.apply(f.lifecycle,request)
    return receipt,{'operation_id':request['operation_id'],'work_id':f.work,'receipt_digest':digest(receipt)},prepared


def test_amendment_preserves_original_physical_clock_and_unmaterialized_product(f):
    before=(f.control/'clock.json').read_bytes()
    receipt,ref,prepared=applied(f)
    assert (f.control/'clock.json').read_bytes()==before
    assert receipt['coordinator_allowance_amendment']['effective_limit_seconds']==72000
    assert receipt['registered_contract']['max_assignments']==5
    assert receipt['registered_contract']['max_seconds']==28800
    assert receipt['registered_contract']['max_repairs']==1
    assert receipt['registered_contract']['max_startup_retries']==0
    assert rebind.verify(f.store,ref)==receipt
    assert rebind.apply(f.lifecycle,receipt['apply_request'])==receipt
    with sqlite3.connect(f.store.directory/'codex-runtime.sqlite3') as db:
        assert db.execute('select count(*) from runtime_budget').fetchone()[0]==0


def charge(f,receipt,slot,*,opened=False,seconds=None):
    clock=json.loads((f.control/'clock.json').read_text()); at=time.time()
    seconds=rebind.SLOTS[slot] if seconds is None else seconds
    start={'identity':f.digest,'operation':slot,'seconds':seconds,'started_at':at,
        'slot':slot,'context':{'work_id':f.work,'concept_digest':receipt['to_concept_digest'],
            'recipe_digest':receipt['to_recipe_digest'],'slot':slot,'kind':'coordinator'},
        'authority_receipt_digest':digest(receipt)}
    path=f.control/('operation-'+slot+'.json');write(path,start);path.chmod(0o400)
    clock.update(spent=clock['spent']+seconds,last=at,open={'id':slot,'reserved':seconds,'started_at':at} if opened else None)
    if not opened:
        path=f.control/('completed-'+slot+'.json');write(path,{'start_digest':digest(start),'completed_at':at});path.chmod(0o400)
    write(f.control/'clock.json',clock)


def test_verify_after_21600_with_full_receipts_and_physical_cap_unchanged(f):
    receipt,ref,_=applied(f)
    for slot in ('initial-imports-replay','initial-backup-restore','initial-final-verification'):
        charge(f,receipt,slot)
    assert json.loads((f.control/'clock.json').read_text())['spent']==24900
    assert rebind.verify(f.store,ref)==receipt
    assert json.loads((f.control/'clock.json').read_text())['active_limit']==21600


def test_shared_verify_allows_one_owned_open_but_release_requires_settled(f):
    receipt,ref,_=applied(f);charge(f,receipt,'colima-transition',opened=True)
    assert rebind.verify(f.store,ref)==receipt
    with pytest.raises(Rejected,match='settled'):
        rebind.current_accounting(f.store,receipt,settled=True)


@pytest.mark.parametrize('field,value',[('max_repairs',2),('max_seconds',28801),('max_assignments',6),('authority','anything'),('resources',[])])
def test_packet_cannot_widen_selected_scope_even_after_fixture_approval(f,field,value):
    prepared=f.prepare(); packet=copy.deepcopy(prepared['packet']);packet['concept']['execution_envelope'][field]=value
    path=f.destination/'tampered.json';ref=rebind._retain(path,packet)
    with pytest.raises(Rejected):
        rebind.apply(f.lifecycle,f.publish({'packet':packet,'packet_path':str(path),'packet_sha256':ref['sha256']}))


def test_budget_scope_or_zero_work_attempt_blocks_prepare(f):
    with sqlite3.connect(f.store.directory/'codex-runtime.sqlite3') as db:
        db.execute('insert into runtime_budget values(?,?)',('fixture-budget','{}'))
    with pytest.raises(Rejected,match='materialized'): f.prepare()
    assert not f.calls


@pytest.mark.parametrize('mutation',['model-readable-path','unbound-hash'])
def test_native_catalog_requires_exact_private_saved_pin(f,mutation):
    prepared=f.prepare();controls=copy.deepcopy(prepared['packet']['metadata']['controller_contract'])
    if mutation=='model-readable-path':
        path=str(f.private/'approved-inputs/catalog.json')
        controls['native_catalog']=path
        controls['input_files'].append({'path':path,'sha256':controls['native_catalog_sha256']})
    else:controls['native_catalog_sha256']='f'*64
    with pytest.raises(Rejected,match='private structural catalog'):
        rebind._controller_delta(f.old_controller,controls,controls['accounting_origin'],prepared['packet']['metadata']['original_clock'])


def test_arbitrary_unreceipted_charge_is_rejected(f):
    receipt,ref,_=applied(f);clock=json.loads((f.control/'clock.json').read_text());clock['spent']+=1;write(f.control/'clock.json',clock)
    with pytest.raises(Rejected,match='Charges'): rebind.verify(f.store,ref)


class CopiedReadinessFixture(Fixture):
    APPROVED_READINESS_COPY = True


@pytest.fixture
def copied(tmp_path, monkeypatch):
    return CopiedReadinessFixture(tmp_path, monkeypatch)


def original_readiness_refs(f):
    approved = next(r for r in f.old_controller['input_files']
                    if r['path'] == f.request['original_readiness_receipt_path'])
    original_id = json.loads(Path(approved['path']).read_text())['id']
    entry = next(e for e in json.loads(f.journal.read_text())['runs'] if e['id'] == original_id)
    return approved, {'path': entry['receipt'], 'sha256': entry['sha256']}


def test_pretty_copy_and_compact_journal_preserve_both_raw_identities(copied):
    approved, actual = original_readiness_refs(copied)
    assert approved['path'] != actual['path'] and approved['sha256'] != actual['sha256']
    before = {r['path']: Path(r['path']).read_bytes() for r in (approved, actual)}
    journal_before = json.loads(copied.journal.read_text())
    clock_before = (copied.control / 'clock.json').read_bytes()
    receipt, ref, prepared = applied(copied)
    metadata = prepared['packet']['metadata']
    assert approved in metadata['original_controller']['input_files']
    assert metadata['readiness_before'] == journal_before
    assert metadata['readiness_before']['runs'][-1]['sha256'] == actual['sha256']
    assert json.loads(copied.journal.read_text())['runs'][:-1] == journal_before['runs']
    assert rebind.verify(copied.store, ref) == receipt
    assert rebind.apply(copied.lifecycle, receipt['apply_request']) == receipt
    assert len(copied.calls) == 1
    assert all(Path(path).read_bytes() == raw for path, raw in before.items())
    assert (copied.control / 'clock.json').read_bytes() == clock_before


COPY_DRIFT = [
    (('status',), 'failed'), (('cleanup_verified',), False),
    (('limits', 'max_seconds'), 599), (('limits', 'max_attempts'), 2),
    (('prepared_recipe', 'max_seconds'), 14401), (('id',), 'readiness-9'),
    (('resource_output',), '/unrelated/outputs/readiness-1'),
    (('runtime_identity', 'binary_sha256'), 'f' * 64),
    (('roles', 0, 'thread_id'), 'different-owner'),
    (('roles', 0, 'cleanup', 'verified'), False),
    (('limits', 'max_seconds'), 600.0), (('limits', 'max_attempts'), 3.0),
    (('model_turns',), False), (('cleanup_verified',), 1),
    (('roles', 0, 'model_turns'), 0.0), (('signature',), 'added-field'),
]


@pytest.mark.parametrize('path,value', COPY_DRIFT)
def test_separately_pinned_semantic_copy_drift_rejects_before_readiness(tmp_path, monkeypatch, path, value):
    class Drift(CopiedReadinessFixture):
        def approved_readiness_value(self, result):
            target = result
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return result
    f = Drift(tmp_path, monkeypatch)
    before = f.journal.read_bytes()
    with pytest.raises(Rejected):
        f.prepare()
    assert not f.calls and f.journal.read_bytes() == before


def test_copy_array_order_is_significant(tmp_path, monkeypatch):
    class Reordered(CopiedReadinessFixture):
        def approved_readiness_value(self, result):
            result['roles'].reverse()
            return result
    f = Reordered(tmp_path, monkeypatch)
    with pytest.raises(Rejected, match='exact journal receipt'):
        f.prepare()
    assert not f.calls


@pytest.mark.parametrize('path,value', [
    (('prepared_recipe', 'max_seconds'), 14400.0),
    (('prepared_recipe', 'requires_detached_processes'), 0),
    (('prepared_recipe', 'checks', 0, 'timeout_seconds'), 600.0),
    (('limits', 'max_seconds'), 600.0), (('limits', 'max_attempts'), 3.0),
])
def test_both_pinned_receipts_must_match_typed_recipe_and_limits_before_metadata(tmp_path, monkeypatch, path, value):
    class NumericHistory(CopiedReadinessFixture):
        def result(self, recipe, rid):
            result = super().result(recipe, rid)
            if rid == 'readiness-1':
                target = result
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return result
    f = NumericHistory(tmp_path, monkeypatch)
    before = f.journal.read_bytes()
    with pytest.raises(Rejected):
        f.prepare()
    assert not f.calls and f.journal.read_bytes() == before


def test_copy_object_order_is_only_representation(tmp_path, monkeypatch):
    class Reordered(CopiedReadinessFixture):
        def approved_readiness_value(self, result):
            return dict(reversed(list(result.items())))
    f = Reordered(tmp_path, monkeypatch)
    receipt, ref, _ = applied(f)
    assert rebind.verify(f.store, ref) == receipt


@pytest.mark.parametrize('which', ['approved', 'journal'])
@pytest.mark.parametrize('phase', ['prepare', 'apply', 'verify'])
def test_historical_raw_reformatting_tamper_holds_every_phase(copied, which, phase):
    approved, actual = original_readiness_refs(copied)
    if phase == 'apply':
        prepared = copied.prepare()
        request = copied.publish(prepared)
    elif phase == 'verify':
        _, ref, _ = applied(copied)
    path = Path((approved if which == 'approved' else actual)['path'])
    # Semantically identical replacement still violates the separately pinned
    # raw identity, even though representation differences are supported.
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(Rejected):
        if phase == 'prepare':
            copied.prepare()
        elif phase == 'apply':
            rebind.apply(copied.lifecycle, request)
        else:
            rebind.verify(copied.store, ref)
    assert len(copied.calls) == (0 if phase == 'prepare' else 1)


@pytest.mark.parametrize('which', ['approved', 'journal'])
def test_readiness_pair_rereads_raw_refs_after_initial_read(copied, monkeypatch, which):
    approved, actual = original_readiness_refs(copied)
    original = rebind._journal
    def raced(directory, reference):
        path = Path((approved if which == 'approved' else actual)['path'])
        path.write_bytes(path.read_bytes() + b' ')
        return original(directory, reference)
    monkeypatch.setattr(rebind, '_journal', raced)
    with pytest.raises(Rejected):
        copied.prepare()
    assert not copied.calls


@pytest.mark.parametrize('which', ['approved', 'journal'])
def test_history_change_during_metadata_keeps_charged_attempt_but_no_packet(copied, monkeypatch, which):
    approved, actual = original_readiness_refs(copied)
    before = json.loads(copied.journal.read_text())
    def raced(recipe, **kwargs):
        result = copied.metadata(recipe, **kwargs)
        path = Path((approved if which == 'approved' else actual)['path'])
        path.write_bytes(path.read_bytes() + b' ')
        return result
    monkeypatch.setattr(rebind.readiness, 'run_readiness', raced)
    with pytest.raises(Rejected):
        copied.prepare()
    after = json.loads(copied.journal.read_text())
    assert len(copied.calls) == 1 and after['runs'][:-1] == before['runs']
    assert not (copied.destination / 'packet.json').exists()


@pytest.mark.parametrize('which', ['approved', 'journal'])
def test_packet_cannot_retarget_historical_receipt(copied, which):
    prepared = copied.prepare()
    packet = copy.deepcopy(prepared['packet'])
    approved, actual = original_readiness_refs(copied)
    source = Path((approved if which == 'approved' else actual)['path'])
    clone = copied.private / 'retargeted-receipt.json'
    clone.write_bytes(source.read_bytes())
    if which == 'approved':
        packet['request']['original_readiness_receipt_path'] = str(clone)
    else:
        packet['metadata']['readiness_before']['runs'][0]['receipt'] = str(clone)
    with pytest.raises(Rejected):
        rebind._validate_packet(copied.store, packet)


class TwoPriorReadinessFixture(CopiedReadinessFixture):
    READINESS_ATTEMPTS = 2


def test_genuine_two_prior_attempts_append_only_the_last_allowed_attempt(tmp_path, monkeypatch):
    f = TwoPriorReadinessFixture(tmp_path, monkeypatch)
    before = json.loads(f.journal.read_text())
    raw = {e['receipt']: Path(e['receipt']).read_bytes() for e in before['runs']}
    receipt, ref, prepared = applied(f)
    assert rebind.verify(f.store, ref) == receipt
    after = json.loads(f.journal.read_text())
    assert after['runs'][:-1] == before['runs'] and len(after['runs']) == after['max_attempts'] == 3
    assert prepared['packet']['metadata']['readiness_before'] == before
    assert len(f.calls) == 1 and all(Path(p).read_bytes() == value for p, value in raw.items())


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('mutation', ['id', 'path', 'alias', 'state', 'output', 'limits'])
def test_every_retained_journal_entry_requires_exact_producer_identity(tmp_path, monkeypatch, index, mutation):
    f = TwoPriorReadinessFixture(tmp_path, monkeypatch)
    journal = json.loads(f.journal.read_text())
    entry = journal['runs'][index]
    path = Path(entry['receipt'])
    if mutation in ('id', 'output', 'limits'):
        value = json.loads(path.read_text())
        if mutation == 'id':
            value['id'] = 'readiness-99'
        elif mutation == 'output':
            value['resource_output'] = str(f.readiness / 'outputs/foreign')
        else:
            value['limits']['max_seconds'] = 600.0
        entry['sha256'] = write(path, value)['sha256']
    elif mutation == 'path':
        replacement = path.with_name('renamed.json')
        replacement.write_bytes(path.read_bytes())
        entry['receipt'] = str(replacement)
    elif mutation == 'alias':
        entry['receipt'] = str(path.parent) + '/./' + path.name
    else:
        entry['state'] = 'failed'
    write(f.journal, journal)
    before = f.journal.read_bytes()
    with pytest.raises(Rejected):
        f.prepare()
    assert not f.calls and f.journal.read_bytes() == before


@pytest.mark.parametrize('mutation', ['duplicate', 'reorder', 'truncate', 'empty'])
def test_original_journal_cannot_reuse_reorder_or_drop_attempts(tmp_path, monkeypatch, mutation):
    f = TwoPriorReadinessFixture(tmp_path, monkeypatch)
    journal = json.loads(f.journal.read_text())
    if mutation == 'duplicate':
        journal['runs'][0] = {**journal['runs'][1], 'id': 'readiness-1'}
    elif mutation == 'reorder':
        journal['runs'].reverse()
        for index, entry in enumerate(journal['runs'], 1):
            entry['id'] = 'readiness-' + str(index)
    elif mutation == 'truncate':
        journal['runs'] = [{**journal['runs'][-1], 'id': 'readiness-1'}]
    else:
        journal['runs'] = []
    write(f.journal, journal)
    before = f.journal.read_bytes()
    with pytest.raises(Rejected):
        f.prepare()
    assert not f.calls and f.journal.read_bytes() == before
