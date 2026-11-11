"""Actual disposable lifecycle/Git/custody fixtures; no real product execution."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import time
import pytest
from agentic_tools.aos import assessment_evidence as gate
from agentic_tools.aos import predispatch_feature_amendment as amendment
from agentic_tools.aos.codex_runtime import candidate
from agentic_tools.aos.store import Rejected, canonical, digest, now
from agentic_tools.aos.worker import Worker, FakeRuntime
from test_aos_predispatch_feature_amendment import Fixture, applied, write, charge
from test_aos_lifecycle import implementation, assessment

CLEAN = {'stopped': True, 'owned_processes': [], 'effects_reconciled': True, 'partial_mutations': 'synthetic observed effects reconciled'}

def immutable(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    ref=write(path,value);path.chmod(0o400);return ref

class GateFixture(Fixture):
    def __init__(self,tmp_path,monkeypatch):
        super().__init__(tmp_path,monkeypatch)
        self.receipt,self.receipt_ref,self.prepared=applied(self)
        self.controls=self.prepared['packet']['metadata']['controller_contract']
        self.contract=self.receipt['registered_contract']
        facts={key:{'satisfied':True,'reference':'SIMULATED:disposable qualification','observed_at':now()} for key in self.contract['prerequisites']}
        with self.store._connection() as db:
            assert not (errors := self.lifecycle._approved_envelope_errors(db,self.work,self.contract)), errors
        self.lifecycle.reconcile_prerequisites('fixture-prerequisites',self.work,self.store.read(self.work)['revision'],self.contract['concept_digest'],facts)
        result=self.lifecycle.admit('fixture-admit',self.work)
        assert result['execution']=='eligible',result
        with Worker(self.lifecycle,None,reconcile_on_enter=False) as worker:
            self.writer=self.lifecycle.claim(self.lifecycle.assignments()[0]['assignment_id'],worker.epoch)
            assert self.writer
            chain=self.control/'chain';chain.mkdir(mode=0o700)
            previous=self.baseline_commit
            immutable(chain/'0000.json',{'ordinal':0,'work_id':self.work,'concept_digest':self.contract['concept_digest'],
                'recipe_digest':self.contract['runtime_recipe_digest'],'previous_sha':self.controls['kickoff_sha'],
                'actual_sha':previous,'inventory':self.controls['baseline_inventory']})
            stages=[]
            for ordinal,increment in enumerate(self.controls['stage_order'],1):
                text='VALUE = '+str(ordinal+1)+'\n'
                (self.repository/'fixture.py').write_text(text);(self.source/'fixture.py').write_text(text)
                self.git('add','fixture.py');self.git('commit','-m','synthetic '+increment)
                commit=self.git('rev-parse','HEAD');tree=self.git('rev-parse','HEAD^{tree}')
                full=gate._full_inventory(self.contract['runtime_recipe'])[1]
                at=time.time();seconds=self.controls['checkpoint_wait_seconds'][increment]
                request={'work_id':self.work,'concept_digest':self.contract['concept_digest'],'recipe_digest':self.contract['runtime_recipe_digest'],
                    'assignment_id':self.writer['assignment_id'],'attempt_id':self.writer['attempt_id'],'ordinal':ordinal,
                    'previous_sha':previous,'increment':increment,'inventory_digest':digest(full),'final':ordinal==3,'requested_at':at}
                ack={**request,'request_digest':digest(request),'actual_sha':commit,'tree':tree,'recorded_at':at}
                context={'work_id':self.work,'concept_digest':self.contract['concept_digest'],'recipe_digest':self.contract['runtime_recipe_digest'],
                    'slot':increment,'kind':'checkpoint','assignment_id':self.writer['assignment_id'],'attempt_id':self.writer['attempt_id'],
                    'request_digest':digest(request),'ordinal':ordinal,'increment':increment,'requested_at':at,
                    'assignment_deadline':datetime.fromisoformat(self.writer['sent_at']).timestamp()+28800,
                    'feature_deadline':self.controls['assessment_evidence_gate']['feature_deadline']}
                start={'identity':self.digest,'operation':f'checkpoint-{ordinal:04d}','seconds':seconds,'started_at':at,
                    'slot':increment,'context':context,'authority_receipt_digest':digest(self.receipt)}
                immutable(self.control/('operation-'+start['operation']+'.json'),start)
                immutable(self.control/('completed-'+start['operation']+'.json'),{'start_digest':digest(start),'completed_at':at})
                clock=json.loads((self.control/'clock.json').read_text());clock.update(last=at,spent=clock['spent']+seconds);write(self.control/'clock.json',clock)
                ack_name=str(ordinal)+'-'+self.writer['attempt_id']+'.json';immutable(self.acks/ack_name,ack)
                stages.append(increment)
                immutable(chain/f'{ordinal:04d}.json',{**{k:ack[k] for k in ('work_id','concept_digest','recipe_digest','assignment_id','attempt_id')},
                    'ordinal':ordinal,'previous_sha':previous,'actual_sha':commit,'inventory':full,'ack':ack,'ack_file':ack_name,
                    'completed_stages':list(stages),'final':ordinal==3})
                previous=commit
            self.commit=previous
            result=self.lifecycle.complete('fixture-implementation',self.writer['assignment_id'],implementation(candidate(self.source,recipe=self.contract['runtime_recipe'])))
            assert result['disposition']=='assessment_required',result
        self.assessor=[a for a in self.lifecycle.assignments() if a['kind']=='assessment'][0]
        self.rootproof=Path(self.controls['assessment_evidence_gate']['receipt_root'])

    def context(self):
        return gate.release_context(self.lifecycle,self.assessor,commit=self.commit,controller_digest=digest(self.controls))

    def source_failure(self):
        context=self.context()
        report=gate.prepare_report(self.lifecycle,self.assessor,category=gate.CATEGORIES[0],commit=self.commit,
            controller_digest=digest(self.controls),completed_at=now(),cleanup=CLEAN,
            review={'reviewed_files':sorted(context['source_inventory']),
                    'invocation_profiles':[], 'findings':[{'path':'fixture.py','line':1,'severity':'blocking','detail':'Synthetic concrete unsafe implementation'}]})
        ref=immutable(self.rootproof/'source-review.json',report)
        reports=[{'category':gate.CATEGORIES[0],'outcome':'failed','reference':ref,'blocked_by':[]}]+[
            {'category':c,'outcome':'not_run','reference':None,'blocked_by':[gate.CATEGORIES[0]]} for c in gate.CATEGORIES[1:]]
        release=gate.prepare_release(self.lifecycle,self.assessor,reports=reports,commit=self.commit,controller_digest=digest(self.controls),
            expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        return immutable(self.rootproof/('assessment-'+self.assessor['assignment_id']+'.json'),release)

@pytest.fixture
def g(tmp_path,monkeypatch):
    return GateFixture(tmp_path,monkeypatch)

def test_missing_proof_does_not_claim_or_charge(g):
    before=g.store.read(g.work)
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        assert g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch) is None
    row=g.lifecycle.assignments()[-1]
    assert row['state']=='proposed' and row['sent_at'] is None
    with g.store._connection() as db:
        assert db.execute('select count(*) from lifecycle_event where kind=? and detail like ?',('launch_intent','%'+g.assessor['assignment_id']+'%')).fetchone()[0]==0


def test_real_source_failure_can_claim_readonly_but_never_native_or_deliver(g):
    g.source_failure()
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        claimed=g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch)
        assert claimed
        assert gate.verify(g.lifecycle,claimed)['mode']=='source_failure_review'
        with pytest.raises(Rejected,match='read-only'): gate.verify(g.lifecycle,claimed,native=True)
        result=g.lifecycle.complete('forged-pass',claimed['assignment_id'],assessment('feature-change',json.loads(claimed['candidate'])))
        assert result['disposition']!='delivered'


def test_mode_only_source_change_blocks_actual_claim(g):
    g.source_failure();(g.source/'fixture.py').chmod(0o755)
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        assert g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch) is None
    assert g.lifecycle.assignments()[-1]['sent_at'] is None


def test_saved_launch_rechecks_immutable_release(g):
    ref=g.source_failure()
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        claimed=g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch);assert claimed
        assert g.lifecycle.can_launch_saved(claimed)
        Path(ref['path']).chmod(0o600)
        assert not g.lifecycle.can_launch_saved(claimed)


def test_fake_git_commit_cannot_prepare_release(g):
    with pytest.raises(Rejected):
        gate.release_context(g.lifecycle,g.assessor,commit='f'*40,controller_digest=digest(g.controls))


def test_later_unreceipted_accounting_change_invalidates_claim(g):
    g.source_failure();clock=json.loads((g.control/'clock.json').read_text());clock['spent']+=1;write(g.control/'clock.json',clock)
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        assert g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch) is None

class ProfileFixture(GateFixture):
    EXTRA_SOURCE = {'rhythm_form/__init__.py':'', 'rhythm_form/acceptance.py':'# synthetic reviewed acceptance entrypoint\n'}

    def profile(self, category='final-verification', phase='final-verification'):
        import sys
        context=self.context();root=self.rootproof/self.assessor['assignment_id']/self.assessor['attempt_id']
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        output=root/('phase-'+phase)
        target={'profile':'rnf-backend','vm_id':'fixture-vm','container_id':'fixture-container','image_digest':'sha256:'+'1'*64,
                'schema_digest':'sha256:'+'2'*64,'database':'fixture-db','import_generation':'fixture-generation'}
        input_ref=immutable(self.private/(phase+'-input.json'),{'fixture':'synthetic actual input'})
        manifest={'format':'aos-coordinator-phase-input-v1','work_id':self.work,'source_digest':context['source_digest'],
            'commit':self.commit,'tree':context['tree'],'task_plan_digest':context['task_plan']['sha256'],'phase':phase,
            'runtime_target':target,'inputs':[input_ref],'output_root':str(output)}
        manifest_ref=immutable(self.private/(phase+'-manifest.json'),manifest)
        tool={'path':sys.executable,'resolved':str(Path(sys.executable).resolve()),
              'sha256':hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}
        profile=gate.prepare_invocation_profile(self.lifecycle,self.assessor,category=category,phase=phase,commit=self.commit,
            controller_digest=digest(self.controls),tools=[tool],manifest=manifest_ref,inputs=[input_ref],output_root=str(output),
            runtime_target=target,environment={'PATH':'/usr/bin:/bin','LANG':'C','PYTHONDONTWRITEBYTECODE':'1'})
        return profile

@pytest.fixture
def p(tmp_path,monkeypatch):
    return ProfileFixture(tmp_path,monkeypatch)

def test_profile_is_exact_committed_module_invocation(p):
    profile=p.profile()
    assert profile['entrypoint']['inventory']==p.context()['source_inventory']['rhythm_form/acceptance.py']
    assert profile['argv'][1:3]==['-m','rhythm_form.acceptance']

@pytest.mark.parametrize('mutation',['echo','pythonpath','virtualenv','entrypoint','tool','operation','cwd','foreign-output'])
def test_profile_rejects_execution_or_source_substitution(p,mutation):
    profile=p.profile();context=p.context()
    if mutation=='echo':profile['argv']=['/bin/echo','passed']
    elif mutation=='pythonpath':profile['environment']['PYTHONPATH']='/unrelated'
    elif mutation=='virtualenv':profile['environment']['VIRTUAL_ENV']='/unrelated'
    elif mutation=='entrypoint':profile['entrypoint']['inventory']['sha256']='0'*64
    elif mutation=='tool':profile['tools'][0]['sha256']='0'*64
    elif mutation=='operation':profile['operation_slot']='initial-imports-replay'
    elif mutation=='foreign-output':
        profile['output_root']=str(p.private/'foreign-private-output')
        manifest=gate._read(p.store,profile['manifest']);manifest['output_root']=profile['output_root']
        profile['manifest']=immutable(p.private/'foreign-manifest.json',manifest)
    else:profile['cwd']='/private/tmp'
    with pytest.raises(Rejected):gate._validate_profile(p.store,profile,context)


def test_successful_source_review_requires_every_fixed_profile(p):
    context=p.context()
    with pytest.raises(Rejected,match='phase invocation'):
        gate.prepare_report(p.lifecycle,p.assessor,category=gate.CATEGORIES[0],commit=p.commit,controller_digest=digest(p.controls),
            completed_at=now(),cleanup=CLEAN,review={'reviewed_files':sorted(context['source_inventory']),'findings':[],
                'invocation_profiles':[immutable(p.private/'one-profile.json',p.profile())]})


def test_typed_wrapper_cannot_replace_actual_owned_execution(p):
    profile=p.profile();context=p.context()
    runtime={**profile['runtime_target'],**{k:'sha256:'+'3'*64 for k in
        ('source_fingerprint','canonical_fingerprint','revision_fingerprint','provenance_fingerprint')}}
    runtime['identity_digest']=digest(runtime)
    command={'profile':immutable(p.private/'profile.json',profile),'category':profile['category'],'argv':profile['argv'],
        'cwd':profile['cwd'],'owned_execution':immutable(p.private/'not-an-execution.json',{'passed':True})}
    with pytest.raises(Rejected,match='maintained owned command'):
        gate._validate_owned_command(p.store,command,context,runtime)

@pytest.mark.parametrize('method',['start','lookup','poll'])
def test_cached_runtime_rechecks_release_before_any_observation(g,monkeypatch,method):
    from agentic_tools.aos.codex_runtime import CodexRuntime
    g.source_failure()
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        assignment=g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch)
    runtime=CodexRuntime(g.lifecycle,{g.work:g.contract['runtime_recipe']},work_id=g.work)
    cached={'assignment':assignment,'attempt_id':assignment['attempt_id'],'terminal_observation':{'state':'completed'}}
    monkeypatch.setattr(runtime,'_get',lambda aid:cached)
    monkeypatch.setattr(runtime,'_cached_terminal',lambda row:pytest.fail('Stale cached observation escaped release gate'))
    (g.source/'fixture.py').chmod(0o755)
    with pytest.raises(Rejected):
        getattr(runtime,method)(assignment if method=='start' else assignment['attempt_id'])


def test_native_deadline_keeps_actual_job_and_clamps_feature_cleanup(g,monkeypatch):
    from agentic_tools.aos.codex_runtime import CodexRuntime
    runtime=CodexRuntime(g.lifecycle,{g.work:g.contract['runtime_recipe']},work_id=g.work)
    stamp=datetime.now(timezone.utc); feature=stamp.timestamp()+120
    record={'assignment':{'sent_at':stamp.isoformat()},'created_at':stamp.isoformat(),'startup_cleanup_reserve':0}
    monkeypatch.setattr(runtime,'_get',lambda aid:record)
    monkeypatch.setattr(runtime,'_coordinator_evidence',lambda a:{'gate':{'feature_deadline':feature}})
    recipe={**g.contract['runtime_recipe'],'_attempt_id':'synthetic'}
    actual=runtime._native_deadline(recipe,{'timeout_seconds':900})
    assert actual.timestamp()<=feature-30
    assert record['assignment']['sent_at']==stamp.isoformat() and recipe['max_seconds']==28800


@pytest.mark.parametrize('boundary',['start','native'])
@pytest.mark.parametrize('fault',['missing-store','malformed-gate','null-gate','missing-required-gate'])
def test_declared_gate_cannot_skip_missing_authority(g,monkeypatch,boundary,fault):
    from types import SimpleNamespace
    from agentic_tools.aos.codex_runtime import CodexRuntime
    runtime=CodexRuntime(g.lifecycle,{g.work:g.contract['runtime_recipe']},work_id=g.work)
    monkeypatch.setattr(runtime,'_get',lambda *_:pytest.fail('Reached runtime record before authority gate'))
    monkeypatch.setattr(runtime,'_client',lambda *_:pytest.fail('Reached model before authority gate'))
    if fault=='missing-store':
        monkeypatch.setattr(g.lifecycle,'store',SimpleNamespace(directory=g.store.directory))
        expected=AttributeError
    else:
        contract={**g.contract,'assessment_evidence_gate':{'format':'malformed'}}
        if fault=='null-gate':contract['assessment_evidence_gate']=None
        if fault=='missing-required-gate':contract.pop('assessment_evidence_gate')
        monkeypatch.setattr(g.lifecycle,'_run',lambda db,work:({},contract))
        expected=Rejected
    with pytest.raises(expected):
        if boundary=='start':runtime.start(g.assessor)
        else:runtime._coordinator_evidence(g.assessor,native=True)


def test_nonassessment_needs_no_assessment_store():
    from types import SimpleNamespace
    lifecycle=SimpleNamespace()
    assert gate.verify(lifecycle,{'kind':'implementation'}) is None
    assert gate.verify_launch_intent(lifecycle,{'kind':'implementation'}) is None


@pytest.mark.parametrize('contract',[{'assessment_evidence_gate':None},{'predispatch_feature_amendment':{}},
                                   {'assessment_evidence_gate':{},'predispatch_feature_amendment':{}}])
def test_declared_or_required_gate_cannot_be_disabled(contract):
    with pytest.raises(Rejected):gate.configuration(contract,None)
    assert gate.configuration({},None) is None


def test_claim_rechecks_live_expiry_after_proof_reads(g,monkeypatch):
    from types import SimpleNamespace
    reference=g.source_failure();release=json.loads(Path(reference['path']).read_text())
    clock={'now':time.time()};real=gate._validate_value
    monkeypatch.setattr(gate,'time',SimpleNamespace(time=lambda:clock['now'],monotonic=time.monotonic))
    def delayed(*args,**kwargs):
        proof=real(*args,**kwargs);clock['now']=datetime.fromisoformat(release['expires_at']).timestamp();return proof
    monkeypatch.setattr(gate,'_validate_value',delayed)
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        assert g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch) is None
    actual=next(a for a in g.lifecycle.assignments() if a['assignment_id']==g.assessor['assignment_id'])
    assert actual['state']=='proposed' and actual['sent_at'] is None


def test_native_boundary_rechecks_expiry_after_lease_read(g,monkeypatch):
    from types import SimpleNamespace
    g.source_failure()
    with Worker(g.lifecycle,None,reconcile_on_enter=False) as worker:
        claimed=g.lifecycle.claim(g.assessor['assignment_id'],worker.epoch)
    proof=gate.verify(g.lifecycle,claimed);clock={'now':time.time()}
    proof['mode']='normal_assessment'
    monkeypatch.setattr(gate,'time',SimpleNamespace(time=lambda:clock['now'],monotonic=time.monotonic))
    monkeypatch.setattr(gate,'verify_in',lambda *a,**k:proof)
    def delayed(*args,**kwargs):clock['now']=datetime.fromisoformat(proof['release']['expires_at']).timestamp()
    monkeypatch.setattr(gate,'verify_native_handoff',delayed)
    with pytest.raises(Rejected,match='expired'):gate.verify(g.lifecycle,claimed,native=True)


@pytest.fixture
def native_failure_files(tmp_path):
    root=tmp_path.resolve();output=root/'a-native';output.mkdir();checkdir=output/'native';checkdir.mkdir()
    frozen={'identity':'sha256:'+'a'*64}
    review=output/'review-a-native.txt';review.write_text(json.dumps({'candidate_identity':frozen['identity'],
        'verdict':'pass','findings':[],'limits':'Synthetic retained diagnostic fixture only'}))
    ref={'path':str(review),'sha256':hashlib.sha256(review.read_bytes()).hexdigest()}
    raw=checkdir/'stdout.txt';raw.write_text('SIMULATED native assertion failed\n')
    transcript=checkdir/'output.txt';transcript.write_bytes(raw.read_bytes())
    check={'name':'native','candidate':frozen,'result':'fail','cleanup':CLEAN,
        'output':str(transcript),'output_sha256':hashlib.sha256(transcript.read_bytes()).hexdigest(),
        'raw_outputs':{str(raw):hashlib.sha256(raw.read_bytes()).hexdigest()}}
    failed={'candidate':frozen,'initial_reports':{'review':ref},'checks':[check],
        'reviewed_host_validation':{'phase':'native_failed','review':ref}}
    context={'candidate':frozen,'assessment_context':{'purpose':'native_failure_triage',
        'origin':{'attempt_id':'a-native','result_sha256':digest(failed)},'failure_result':failed}}
    return {'recipe':{'outputs':str(root),'checks':[{'name':'native'}]},'context':context,'raw':raw,'review':review,'root':root}


def test_native_failure_diagnostics_reuse_maintained_readers(native_failure_files):
    p=native_failure_files
    gate._native_failure_evidence(p['recipe'],p['context'],time.time()+60)


@pytest.mark.parametrize('mutation',['raw-changed','raw-missing','review-changed','foreign-raw','uncertain-cleanup','expired'])
def test_native_failure_diagnostics_cannot_be_substituted(native_failure_files,mutation):
    p=native_failure_files;context=p['context'];failed=context['assessment_context']['failure_result'];deadline=time.time()+60
    if mutation=='raw-changed':p['raw'].write_text('changed')
    elif mutation=='raw-missing':p['raw'].unlink()
    elif mutation=='review-changed':p['review'].write_text('{}')
    elif mutation=='foreign-raw':
        foreign=p['root']/'foreign.txt';foreign.write_bytes(p['raw'].read_bytes())
        failed['checks'][0]['raw_outputs']={str(foreign):hashlib.sha256(foreign.read_bytes()).hexdigest()}
    elif mutation=='uncertain-cleanup':failed['checks'][0]['cleanup']={**CLEAN,'effects_reconciled':False}
    else:deadline=time.time()-1
    context['assessment_context']['origin']['result_sha256']=digest(failed)
    with pytest.raises(Rejected):gate._native_failure_evidence(p['recipe'],context,deadline)
