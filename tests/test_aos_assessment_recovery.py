"""Synthetic journal fixtures exercise recovery, never a real approval or launch."""
import copy
import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing

import pytest

from agentic_tools.aos import assessment_recovery as recovery
from agentic_tools.aos.codex_runtime import candidate, directory_identity
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_lifecycle import setup


def retain(path, value):
    raw = value if isinstance(value, bytes) else canonical(value).encode()
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(0o400)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def rows(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {n: db.execute('SELECT * FROM "' + n + '" ORDER BY rowid').fetchall() for n in names}


def seal(c):
    c.request['expected_work_revision'] = c.store.read('Q1')['revision']
    c.evidence['snapshot'] = recovery.capture(c.lifecycle, c.request)
    core = {k: v for k, v in c.evidence.items() if k != 'review'}
    c.evidence['review'] = retain(c.root / 'review.json', {
        'format': recovery.FORMAT + '-review', 'work_id': 'Q1', 'attempt_id': c.aid,
        'evidence_digest': digest(core), 'verdict': 'pass', 'author': 'fixture-author',
        'reviewer': 'fixture-reviewer', 'reason': 'SIMULATED independent exact-evidence review'})
    ref = retain(c.root / 'evidence.json', c.evidence)
    c.request.update(evidence_path=ref['path'], evidence_sha256=ref['sha256'])


@pytest.fixture
def failed_assessment(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    store, lifecycle, current = setup(root)
    source, outputs = root / 'fixture', root / 'outputs'
    source.mkdir(); outputs.mkdir()
    (source / 'source.txt').write_text('candidate\n')
    frozen = candidate(source)
    aid = 'synthetic-failed-assessor'
    times = ['2026-01-01T00:00:0%d+00:00' % n for n in range(8)]
    recipe = {'source': str(source), 'outputs': str(outputs), 'max_seconds': 60}
    with store._transaction() as db:
        writer = dict(db.execute("SELECT * FROM assignment WHERE work_id='Q1'").fetchone())
        contract = json.loads(db.execute("SELECT contract FROM lifecycle WHERE work_id='Q1'").fetchone()[0])
        contract.update(execution_policy='isolated-checkout-v1', runtime_recipe=recipe, runtime_recipe_digest=digest(recipe))
        package = json.loads(db.execute("SELECT package FROM concept WHERE work_id='Q1'").fetchone()[0])
        bounds = {k: v for k, v in contract.items() if k not in ('concept_revision','concept_digest')}
        package['execution_envelope'] = bounds
        target = digest(package)
        contract['concept_digest'] = target
        db.execute("UPDATE concept SET package=?,digest=? WHERE work_id='Q1'", (canonical(package),target))
        db.execute("UPDATE decision SET digest=?,package_snapshot=? WHERE work_id='Q1'", (target,canonical(package)))
        db.execute("UPDATE challenge SET digest=? WHERE work_id='Q1'", (target,))
        db.execute("UPDATE assignment SET concept_digest=? WHERE work_id='Q1'", (target,))
        result = {'attempt_id':writer['attempt_id'],'executor':'writer','status':'completed','stopped':True,
                  'effects_reconciled':True,'owned_processes':[], 'partial_mutations':{'status':'none','changes':[]},'candidate':frozen}
        db.execute("UPDATE assignment SET state='completed',sent_at=?,terminal_at=?,result=? WHERE assignment_id=?",
                   (times[0],times[1],canonical(result),writer['assignment_id']))
        db.execute('INSERT INTO completion VALUES(?,?,?,?,?,?)',('writer-complete',writer['assignment_id'],'synthetic',canonical(result),'assessment_required',times[1]))
        assessor = dict(writer)
        assessor.update(assignment_id=aid,attempt_id=aid,kind='assessment',state='blocked_unknown_effects',executor='assessor',
                        predecessor='completion:writer-complete',candidate=canonical(frozen),concept_digest=target,
                        sent_at=times[2],eligible_at=times[2],created_at=times[2],terminal_at=None,result=None)
        db.execute('INSERT INTO assignment (' + ','.join(assessor) + ') VALUES (' + ','.join('?' for _ in assessor) + ')', tuple(assessor.values()))
        db.execute('INSERT INTO outbox VALUES(?,?,?,?)',(aid,'blocked',1,times[5]))
        db.execute("UPDATE lifecycle SET execution='held',control='owned_stop_requested',candidate=?,contract=? WHERE work_id='Q1'",(canonical(frozen),canonical(contract)))
        db.execute("UPDATE resource SET assignment_id=?,state='frozen' WHERE work_id='Q1'", (aid,))
        db.execute('INSERT INTO worker_status VALUES(1,1,?,?,?)',('synthetic-owner',times[7],'stopped'))
        unknown={'attempt_id':aid,'effects_reconciled':False,'owned_processes':None,'stopped':False,'partial_mutations':'unknown'}
        db.execute('INSERT INTO owned_stop VALUES(?,?,?,?,?,?,?)',(aid,'Q1','supervisor_stopped','blocked_unknown_effects',times[5],canonical(unknown),times[6]))
        for kind, detail, stamp in [
            ('launch_intent',{'assignment_id':aid,'epoch':1},times[2]),
            ('unknown_effects',recovery.FAILURE,times[3]),
            ('owned_stop_requested',{'assignment_id':aid,'reason':'supervisor_stopped','detail':'Foreground owner stopped; exact owned cleanup required'},times[5]),
            ('owned_stop_reconciled',unknown,times[6]),
        ]:
            db.execute('INSERT INTO lifecycle_event(work_id,kind,detail,created_at) VALUES(?,?,?,?)',('Q1',kind,canonical(detail),stamp))
        lifecycle._block(db,'Q1','supervisor_stopped','Original failed owner')
        lifecycle._block(db,'Q1','unknown_effects','Original unknown effect')
    runtime=store.directory/'codex-runtime.sqlite3'
    record={'attempt_id':writer['attempt_id'],'phase':'terminal','recipe_digest':digest(recipe),'budget_id':'fixed-budget','budget_seconds':60,
            'terminal_observation':{'result':result},'stop_receipt':{'stopped':True,'effects_reconciled':True,'owned_processes':[],'partial_mutations':{'status':'none','changes':[]}},
            'startup_paths':{str(source):directory_identity(source)},'observed_processes':{'123':'original birth   '},
            'server_history':[{'pid':123,'started':'original birth   ','group':123}]}
    with closing(sqlite3.connect(runtime)) as db, db:
        db.executescript('''CREATE TABLE attempt(id TEXT PRIMARY KEY,record TEXT NOT NULL);
        CREATE TABLE admission_denial(id TEXT PRIMARY KEY,record TEXT NOT NULL);
        CREATE TABLE runtime_budget(id TEXT PRIMARY KEY,record TEXT NOT NULL);
        CREATE TABLE product_budget_scope(scope TEXT PRIMARY KEY,budget_id TEXT NOT NULL);
        CREATE TABLE observed_process(attempt_id TEXT,pid TEXT,started TEXT,PRIMARY KEY(attempt_id,pid,started));
        CREATE TABLE native_check(id TEXT PRIMARY KEY,attempt_id TEXT,record TEXT NOT NULL);
        CREATE TABLE resource_limit(work_id TEXT PRIMARY KEY,record TEXT NOT NULL);''')
        db.execute('INSERT INTO attempt VALUES(?,?)',(writer['attempt_id'],canonical(record)))
        db.execute('INSERT INTO runtime_budget VALUES(?,?)',('fixed-budget',canonical({'max_assignments':6,'max_seconds':360,'held_charge':60})))
        db.execute('INSERT INTO product_budget_scope VALUES(?,?)',('product:Q1','fixed-budget'))
    runtime.chmod(0o600)
    manifest, producer = {}, {}
    fake_hashes={}
    for name in recovery.PRODUCER_FILES:
        ref=retain(root/name,('synthetic pinned producer '+name).encode());producer[name]=ref
        manifest['aos/'+name]=ref['sha256'];fake_hashes[name]=ref['sha256']
    monkeypatch.setattr(recovery,'PRODUCER_FILES',fake_hashes);monkeypatch.setattr(recovery,'PRODUCER',digest(manifest))
    producer['manifest']=retain(root/'manifest.json',manifest)
    inputs={name:retain(root/(name+'.json'),{'synthetic':name}) for name in ('project','request')}
    packet={'ready':True,'approval_recorded':False,'reservation_performed':False,'input_bindings':inputs,
            'project':{'state_dir':str(store.directory)},'concept':{'execution_envelope':bounds},
            'readiness':{'runtime_identity':{'runtime_source_digest':recovery.PRODUCER}}}
    packet_ref=retain(root/'prepared.json',{'format':'aos-supervisor-prepared-v1','packet_digest':digest(packet),'packet':packet})
    final={'format':'aos-supervisor-v1','work_id':'Q1','state':'held','blocker':'unknown_effects',
        'cleanup':{'runtime_closed':True,'decision_service':{'listener_stopped':True,'requests_stopped':True},
                   'owned_stop':{'known_stopped':False,'unresolved_attempts':[aid],'unlaunched_intents':[],'stop_conflicts':[]}}}
    owner={'source':'codex_app.read_thread','thread_id':'synthetic-thread','turn_id':'synthetic-turn','command_execution':{
        'type':'commandExecution','id':'synthetic-exit','command':'uv run aos start --project '+inputs['project']['path']+' --request '+inputs['request']['path'],
        'status':'failed','exitCode':3,'output':{'truncated':False,'text':canonical(final)}}}
    evidence={'format':recovery.FORMAT+'-evidence','work_id':'Q1','attempt_id':aid,'producer':producer,
              'prepared_packet':packet_ref,'owner_exit':retain(root/'owner.json',owner)}
    c=SimpleNamespace(root=root,store=store,lifecycle=lifecycle,request={'operation_id':'recover-synthetic','work_id':'Q1','attempt_id':aid,'digest':target},
                      evidence=evidence,aid=aid,writer=writer,source=source,outputs=outputs,runtime=runtime,record=record,owner=owner,packet=packet)
    monkeypatch.setattr(recovery,'process_groups',lambda **kwargs:{})
    seal(c)
    return c


def test_exact_preview_apply_and_idempotent_replay_preserve_history_and_charges(failed_assessment):
    c=failed_assessment
    old, runtime=rows(c.store.path),rows(c.runtime)
    preview=recovery.recover(c.lifecycle,c.request)
    assert preview['ready'] and not preview['applied']
    assert rows(c.store.path)==old and rows(c.runtime)==runtime
    applied=recovery.recover(c.lifecycle,c.request,apply=True)
    assert applied['applied'] and not applied['new_execution_authorized']
    assert recovery.recover(c.lifecycle,c.request,apply=True)==applied
    assert rows(c.runtime)==runtime
    with c.store._connection() as db:
        audit=recovery.read_audit(db,c.request['operation_id'],'Q1')
        assert audit['original_records']['stops'][0]['state']=='blocked_unknown_effects'
        assert audit['terminal_receipt']['provenance']=='separately-derived-after-failure'
        run=dict(db.execute("SELECT * FROM lifecycle WHERE work_id='Q1'").fetchone())
        assert (run['execution'],run['admitted'],run['control'])==('held',0,'none')
        assert json.loads(run['candidate'])==audit['candidate']
        assert db.execute("SELECT count(*) FROM assignment").fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM resource WHERE work_id='Q1'").fetchone()[0]==0
        assert db.execute('SELECT code FROM technical_blocker').fetchall()[0][0]=='assessment_recovered'
    new=rows(c.store.path)
    for name in ('concept','decision','challenge','decision_readback'):
        assert new[name]==old[name]
    assert new['lifecycle_event'][:len(old['lifecycle_event'])]==old['lifecycle_event']


@pytest.mark.parametrize('damage', ['launch','ack','event','stop','writer_cleanup','owner','review','candidate','output','trigger','packet','producer'])
def test_conflicting_provenance_is_rejected_without_effects(failed_assessment,damage):
    c=failed_assessment
    if damage=='launch':
        with closing(sqlite3.connect(c.runtime)) as db,db: db.execute('INSERT INTO attempt VALUES(?,?)',(c.aid,'{}'))
    elif damage=='writer_cleanup':
        c.record['stop_receipt']['stopped']=False
        with closing(sqlite3.connect(c.runtime)) as db,db: db.execute('UPDATE attempt SET record=?',(canonical(c.record),))
    elif damage in ('ack','event','stop','trigger'):
        with c.store._transaction() as db:
            if damage=='ack': db.execute('UPDATE assignment SET acknowledged_at=? WHERE assignment_id=?',('2026-01-01',c.aid))
            if damage=='event': db.execute("UPDATE lifecycle_event SET detail=? WHERE kind='unknown_effects'",(canonical('some other failure'),))
            if damage=='stop': db.execute("UPDATE owned_stop SET reason='cancel'")
            if damage=='trigger': db.execute('CREATE TRIGGER malicious AFTER UPDATE ON assignment BEGIN DELETE FROM decision; END')
    elif damage=='owner':
        c.owner['command_execution']['output']['truncated']=True;c.evidence['owner_exit']=retain(c.root/'owner.json',c.owner)
    elif damage=='review':
        value=json.loads((c.root/'review.json').read_text());value['reviewer']=value['author'];c.evidence['review']=retain(c.root/'review.json',value)
    elif damage=='candidate': (c.source/'source.txt').write_text('unexplained change')
    elif damage=='output': (c.outputs/c.aid).mkdir()
    elif damage=='packet': c.packet['ready']=False;c.evidence['prepared_packet']=retain(c.root/'prepared.json',{'format':'aos-supervisor-prepared-v1','packet_digest':digest(c.packet),'packet':c.packet})
    elif damage=='producer': c.evidence['producer']['worker.py']=retain(c.root/'worker.py',b'changed producer')
    if damage not in ('review','trigger'):
        seal(c)
    else:
        ref=retain(c.root/'evidence.json',c.evidence);c.request.update(evidence_path=ref['path'],evidence_sha256=ref['sha256'])
    before, runtime=rows(c.store.path),rows(c.runtime)
    with pytest.raises(Rejected): recovery.recover(c.lifecycle,c.request,apply=True)
    assert rows(c.store.path)==before and rows(c.runtime)==runtime


def test_changed_state_after_review_is_rejected(failed_assessment):
    c=failed_assessment
    with c.store._transaction() as db: db.execute("UPDATE worker_status SET heartbeat='2026-01-01T00:00:09+00:00'")
    with pytest.raises(Rejected,match='Reviewed state changed'): recovery.recover(c.lifecycle,c.request,apply=True)


def test_exclusive_owner_and_live_original_process_reject(failed_assessment,monkeypatch):
    c=failed_assessment
    fd=os.open(c.store.directory/'worker.lock',os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(Rejected,match='Stop the worker'): recovery.recover(c.lifecycle,c.request,apply=True)
    finally: os.close(fd)
    monkeypatch.setattr(recovery,'process_groups',lambda **kwargs:{123:{'started':'original birth','group':123}})
    with pytest.raises(Rejected,match='still present'): recovery.recover(c.lifecycle,c.request,apply=True)


def test_atomic_failure_rolls_back(failed_assessment):
    c=failed_assessment; before,runtime=rows(c.store.path),rows(c.runtime)
    def fail(stage):
        if stage=='assessment_recovery_after_updates': raise Rejected('injected','synthetic commit interruption')
    c.lifecycle._fault=fail
    with pytest.raises(Rejected,match='synthetic commit interruption'): recovery.recover(c.lifecycle,c.request,apply=True)
    assert rows(c.store.path)==before and rows(c.runtime)==runtime


def test_mutation_between_observation_and_commit_is_rejected(failed_assessment):
    c=failed_assessment; before=rows(c.store.path)
    def drift(stage):
        if stage=='assessment_recovery_before_commit': (c.source/'source.txt').write_text('concurrent edit')
    c.lifecycle._fault=drift
    with pytest.raises(Rejected,match='frozen candidate changed'): recovery.recover(c.lifecycle,c.request,apply=True)
    assert rows(c.store.path)==before
