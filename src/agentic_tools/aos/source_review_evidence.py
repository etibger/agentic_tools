"""Explicitly bound source-review data; never a model Git permission grant."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import tempfile
from . import native_paths
from .store import Rejected, canonical, digest

FORMAT = 'aos-source-review-evidence-v1'
DESCRIPTOR_FORMAT = 'aos-source-review-descriptor-v1'
FILENAME = 'source-review-evidence.json'
MAX_BYTES = 32 * 1024 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 128 * 1024 * 1024
MAX_PATHS = 10000
DESCRIPTOR_KEYS = {'format', 'path', 'sha256', 'work_id', 'baseline_identity', 'candidate_identity', 'git_base'}


def _reject(message):
    raise Rejected('source_review_evidence', message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: _reject('Duplicate evidence keys')
        result[key] = value
    return result


def _path(name):
    if not isinstance(name, str) or not name or '\x00' in name: _reject('Exact relative source path required')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name: _reject('Canonical relative source path required')
    return path


def _roots(source, recipe, writable_roots):
    roots = {Path(source), *(Path(p) for p in writable_roots)}
    if recipe is not None:
        roots.add(Path(recipe['outputs']))
        roots.update(Path(r['path']) for r in recipe.get('native_write_roots', []))
    return roots


def _artifact_bytes(path, roots=()):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        _reject('Evidence must be a canonical protected file')
    if any(native_paths.overlaps(path, root) for root in roots): _reject('Evidence overlaps worker-writable scope')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400 or before.st_size > MAX_BYTES):
            _reject('Evidence must remain owned, immutable, single-link and bounded')
        raw = stream.read(MAX_BYTES + 1); after = os.fstat(stream.fileno())
    final = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')
    if len(raw) > MAX_BYTES or any(getattr(before,k) != getattr(after,k) or getattr(before,k) != getattr(final,k) for k in fields):
        _reject('Evidence changed while reading')
    return raw


def _git(source, args, *, data=None, limit=MAX_TREE_BYTES, allowed=(0,)):
    env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_LAZY_FETCH': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    run = subprocess.run(['/usr/bin/git', '-c', 'core.fsmonitor=false', '--no-pager', *args], cwd=source,
                         input=data, capture_output=True, timeout=60, env=env)
    if run.returncode not in allowed or len(run.stdout) > limit or len(run.stderr) > 65536:
        _reject('Bounded read-only Git extraction failed')
    return run.stdout


def _tree(source, git_base, baseline):
    if not re.fullmatch('[a-f0-9]{40}', git_base): _reject('Exact committed Git base required')
    if _git(source, ['rev-parse','--verify','HEAD']).decode().strip() != git_base:
        _reject('Checkout HEAD differs from review base')
    manifest, order, total = {}, [], 0
    for row in _git(source,['ls-tree','-rlz','--full-tree',git_base],limit=4*1024*1024).split(b'\0'):
        if not row: continue
        header, name = row.split(b'\t',1); mode, kind, oid, size = header.decode('ascii').split()
        path=name.decode('utf-8'); _path(path); length=int(size); total+=length
        if mode not in ('100644','100755','120000') or kind!='blob' or not re.fullmatch('[a-f0-9]{40}',oid):
            _reject('Only committed files and source links are supported')
        if path in manifest or len(manifest)>=MAX_PATHS or not 0<=length<=MAX_BLOB_BYTES or total>MAX_TREE_BYTES:
            _reject('Committed baseline exceeds evidence limits')
        manifest[path]={'mode':mode,'oid':oid,'size':length};order.append(path)
    if set(baseline)!=set(manifest)|{'.git'}: _reject('Baseline must equal committed tree plus worktree pointer')
    raw=_git(source,['cat-file','--batch'],data=''.join(manifest[p]['oid']+'\n' for p in order).encode(),limit=total+len(order)*128)
    offset,blobs=0,{}
    for path in order:
        end=raw.index(b'\n',offset);fields=raw[offset:end].decode('ascii').split();item=manifest[path]
        if fields!=[item['oid'],'blob',str(item['size'])]: _reject('Git returned a different baseline blob')
        offset=end+1;blob=raw[offset:offset+item['size']];offset+=item['size']
        if raw[offset:offset+1]!=b'\n': _reject('Truncated baseline blob stream')
        offset+=1; hashed=('symlink:' if item['mode']=='120000' else '')+_sha(blob)
        if baseline.get(path)!=hashed: _reject('Git tree differs from approved original baseline')
        item['sha256']=_sha(blob);blobs[path]=blob
    if offset!=len(raw): _reject('Unexpected baseline blob data')
    pointer=source/'.git'
    if not pointer.is_file() or pointer.is_symlink() or _sha(pointer.read_bytes())!=baseline['.git']:
        _reject('Original worktree pointer changed')
    return manifest,blobs


def _current(source,names):
    if len(names) > MAX_PATHS: _reject('Candidate path count exceeds evidence limit')
    values,modes,total={},{},0
    for name in names:
        _path(name);path=source/name;info=path.lstat()
        total += info.st_size
        if info.st_size > MAX_BLOB_BYTES or total > MAX_TREE_BYTES:
            _reject('Candidate content exceeds evidence limit')
        if stat.S_ISLNK(info.st_mode): raw,mode=os.fsencode(os.readlink(path)),'120000'
        elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
            if info.st_size>MAX_BLOB_BYTES: _reject('Source file exceeds evidence limit')
            raw,mode=path.read_bytes(),'100755' if info.st_mode&stat.S_IXUSR else '100644'
        else: _reject('Source evidence requires regular files or source links')
        if len(raw) != info.st_size: _reject('Candidate changed while reading source content')
        values[name],modes[name]=raw,mode
    return values,modes


def _content(raw,mode):
    result={'encoding':'base64','data':base64.b64encode(raw).decode('ascii'),'sha256':_sha(raw),
            'kind':'symlink' if mode=='120000' else 'file','mode':mode}
    try: result['text']=raw.decode('utf-8')
    except UnicodeDecodeError: pass
    return result


def _decode(value):
    if (not isinstance(value,dict) or set(value) not in ({'encoding','data','sha256','kind','mode'},
        {'encoding','data','sha256','kind','mode','text'}) or value['encoding']!='base64'
        or value['mode'] not in ('100644','100755','120000') or value['kind']!=('symlink' if value['mode']=='120000' else 'file')):
        _reject('Malformed source content record')
    raw=base64.b64decode(value['data'],validate=True)
    if len(raw)>MAX_BLOB_BYTES or value['sha256']!=_sha(raw): _reject('Source content hash differs')
    if 'text' in value and value['text'].encode('utf-8')!=raw: _reject('Text differs from exact source bytes')
    return raw


def _patch(changes,directory):
    """Use raw snapshots, avoiding index state and repository filters."""
    with tempfile.TemporaryDirectory(prefix='source-review-',dir=directory) as scratch:
        root=Path(scratch)
        for side in ('old','new'):
            (root/side).mkdir();names=[c['path'] for c in changes if c[side] is not None]
            if any(any(str(p) in names for p in _path(n).parents if str(p)!='.') for n in names):
                _reject('Source evidence paths overlap a file or link')
            for change in changes:
                content=change[side]
                if content is None: continue
                target=root/side/change['path'];target.parent.mkdir(parents=True,exist_ok=True);raw=_decode(content)
                if content['kind']=='symlink': os.symlink(os.fsdecode(raw),target)
                else: target.write_bytes(raw);target.chmod(0o755 if content['mode']=='100755' else 0o644)
        raw = _git(root,['-c','core.attributesfile=/dev/null','diff','--no-index','--binary','--full-index',
            '--no-ext-diff','--no-textconv','--','old','new'],allowed=(0,1),limit=MAX_BYTES)
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            # Preserve non-UTF-8 text bytes; Git's --binary does not encode them.
            return {'encoding': 'base64', 'data': base64.b64encode(raw).decode('ascii'), 'sha256': _sha(raw)}


def build(source,baseline,frozen,*,work_id,baseline_assignment_id,git_base,destination,recipe=None,writable_roots=()):
    from .codex_runtime import candidate
    source,destination=Path(source),Path(destination);roots=_roots(source,recipe,writable_roots)
    if (not destination.is_absolute() or destination.parent.resolve()!=destination.parent or not destination.parent.is_dir()
            or any(native_paths.overlaps(destination,r) for r in roots)):
        _reject('Use an existing protected evidence directory outside worker writes')
    if candidate(source,recipe=recipe)!=frozen: _reject('Current source differs from frozen candidate')
    tree,old=_tree(source,git_base,baseline);new,modes=_current(source,frozen['inventory'])
    changed=sorted(n for n in baseline.keys()|frozen['inventory'].keys() if baseline.get(n)!=frozen['inventory'].get(n)
                   or n in tree and n in modes and tree[n]['mode']!=modes[n])
    if '.git' in changed: _reject('Worktree control identity is outside source changes')
    changes=[{'path':n,'kind':'added' if n not in baseline else 'deleted' if n not in new else 'modified',
              'old':_content(old[n],tree[n]['mode']) if n in old else None,
              'new':_content(new[n],modes[n]) if n in new else None} for n in changed]
    evidence={'format':FORMAT,'work_id':work_id,'source':str(source),'git_base':git_base,'baseline_assignment_id':baseline_assignment_id,
              'baseline_identity':digest(baseline),'baseline_inventory':baseline,'candidate_identity':frozen['identity'],
              'candidate_inventory':frozen['inventory'],'candidate_modes':modes,'git_tree':tree,'changes':changes,
              'git_patch':_patch(changes,destination.parent),
              'limits':'Exact original-baseline-to-candidate bytes, independent of index/staging; no review verdict or test result.'}
    if candidate(source,recipe=recipe)!=frozen or _current(source,frozen['inventory'])[1]!=modes:
        _reject('Candidate changed during evidence extraction')
    raw=(canonical(evidence)+'\n').encode()
    if len(raw)>MAX_BYTES: _reject('Evidence exceeds its bounded size')
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
    return {'format':DESCRIPTOR_FORMAT,'path':str(destination),'sha256':_sha(raw),
            **{k:evidence[k] for k in ('work_id','baseline_identity','candidate_identity','git_base')}}


def validate(descriptor,*,source,baseline_identity,candidate,work_id,recipe=None,writable_roots=()):
    from .codex_runtime import inventory
    if (not isinstance(descriptor,dict) or set(descriptor)!=DESCRIPTOR_KEYS or descriptor['format']!=DESCRIPTOR_FORMAT
        or descriptor['work_id']!=work_id or descriptor['baseline_identity']!=baseline_identity
        or descriptor['candidate_identity']!=candidate['identity'] or not re.fullmatch('[a-f0-9]{64}',descriptor['sha256'])
        or not re.fullmatch('[a-f0-9]{40}',descriptor['git_base'])): _reject('Descriptor differs from approved baseline and candidate')
    source=Path(source);raw=_artifact_bytes(descriptor['path'],_roots(source,recipe,writable_roots))
    if _sha(raw)!=descriptor['sha256']: _reject('Original source review evidence changed')
    evidence=json.loads(raw,object_pairs_hook=_unique);baseline,current=evidence['baseline_inventory'],evidence['candidate_inventory']
    if (evidence.get('format')!=FORMAT or evidence.get('source')!=str(source)
        or any(evidence.get(k)!=descriptor[k] for k in ('work_id','baseline_identity','candidate_identity','git_base'))
        or digest(baseline)!=baseline_identity or digest(current)!=candidate['identity'] or current!=candidate['inventory']
        or candidate.get('source_identity')!=candidate['identity'] or len(baseline)>MAX_PATHS or len(current)>MAX_PATHS
        or not evidence.get('baseline_assignment_id') or inventory(source,recipe=recipe)!=current):
        _reject('Evidence does not bind original baseline and fresh frozen candidate')
    tree=evidence['git_tree']
    actual_tree, _ = _tree(source, descriptor['git_base'], baseline)
    if tree != actual_tree: _reject('Evidence tree differs from the exact committed baseline')
    if set(tree)|{'.git'}!=set(baseline) or set(evidence['candidate_modes'])!=set(current): _reject('Evidence omits source paths')
    for name,item in tree.items():
        _path(name)
        if (set(item)!={'mode','oid','size','sha256'} or item['mode'] not in ('100644','100755','120000')
            or not re.fullmatch('[a-f0-9]{40}',item['oid']) or not 0<=item['size']<=MAX_BLOB_BYTES
            or baseline[name]!=('symlink:' if item['mode']=='120000' else '')+item['sha256']):
            _reject('Committed tree manifest differs from original baseline')
    new,modes=_current(source,current)
    if modes!=evidence['candidate_modes']: _reject('Candidate file modes changed')
    expected=sorted(n for n in baseline.keys()|current.keys() if baseline.get(n)!=current.get(n)
                    or n in tree and n in modes and tree[n]['mode']!=modes[n]);changes=evidence['changes']
    if [c.get('path') for c in changes]!=expected or '.git' in expected: _reject('Evidence omits, duplicates or adds changes')
    for change in changes:
        name=change['path'];_path(name);kind='added' if name not in baseline else 'deleted' if name not in current else 'modified'
        if set(change)!={'path','kind','old','new'} or change['kind']!=kind: _reject('Changed-path classification differs')
        for side,hashes in (('old',baseline),('new',current)):
            content=change[side]
            if name not in hashes:
                if content is not None: _reject('Absent side has unexpected content')
                continue
            blob=_decode(content);expected_hash=('symlink:' if content['kind']=='symlink' else '')+_sha(blob)
            if expected_hash!=hashes[name] or side=='new' and blob!=new[name]: _reject('Changed content differs from exact inventory')
            if content['mode']!=(tree[name]['mode'] if side=='old' else modes[name]): _reject('Changed content mode differs')
    if not evidence.get('limits'): _reject('Patch data and limitations required')
    if evidence['git_patch'] != _patch(changes, Path(descriptor['path']).parent):
        _reject('Patch differs from the exact changed source bytes')
    return evidence


def prepare(lifecycle,work_id,destination,git_base):
    """Read retained authority; write only the requested protected artifact."""
    store=lifecycle.store
    with store._connection() as db:
        run=db.execute('SELECT * FROM lifecycle WHERE work_id=?',(work_id,)).fetchone()
        writers=list(db.execute("SELECT * FROM assignment WHERE work_id=? AND kind='implementation' AND state='completed'",(work_id,)))
        if not run or not run['candidate'] or len(writers)!=1: _reject('One completed original writer and frozen candidate required')
        contract=json.loads(run['contract']);frozen=json.loads(run['candidate']);writer=dict(writers[0])
    with sqlite3.connect((store.directory/'codex-runtime.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT record FROM attempt WHERE id=?',(writer['attempt_id'],)).fetchone()
    if not row: _reject('Original writer baseline missing')
    original=json.loads(row[0]);baseline=original.get('baseline')
    if not isinstance(baseline,dict) or digest(baseline)!=contract['baseline'] or original.get('phase')!='terminal':
        _reject('Original baseline differs from retained approval')
    recipe=contract['runtime_recipe']
    return build(Path(recipe['source']),baseline,frozen,work_id=work_id,baseline_assignment_id=writer['assignment_id'],
                 git_base=git_base,destination=destination,recipe=recipe,writable_roots=store.worker_writable_roots)


def seed(descriptor,run_outputs,**kwargs):
    validate(descriptor,**kwargs)
    raw=_artifact_bytes(descriptor['path'],_roots(kwargs['source'],kwargs.get('recipe'),kwargs.get('writable_roots',())))
    if _sha(raw) != descriptor['sha256']: _reject('Original evidence changed before copying')
    destination=Path(run_outputs)/FILENAME
    if destination.parent.resolve()!=destination.parent or not destination.parent.is_dir(): _reject('Canonical existing attempt output required')
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as stream: stream.write(raw)
    return {'path':str(destination),'sha256':descriptor['sha256'],'descriptor':descriptor}


def verify_copy(bound,run_outputs,**kwargs):
    if not isinstance(bound,dict) or set(bound)!={'path','sha256','descriptor'}: _reject('Retained source review binding missing')
    descriptor=bound['descriptor'];evidence=validate(descriptor,**kwargs);expected=Path(run_outputs)/FILENAME
    if bound['path']!=str(expected) or bound['sha256']!=descriptor['sha256'] or _sha(_artifact_bytes(expected))!=bound['sha256']:
        _reject('Scoped source review evidence changed')
    return evidence
