"""C03 common queue contract. No implicit grants, network calls or source writes.

All mutations require the caller's IMMEDIATE/EXCLUSIVE transaction and FK ON.
A grant records an already obtained, exact authorization; this API cannot prove
that a recorded human approval is authentic. Legacy approval prose is never
parsed into new permissions. Completed legacy units remain historical.
"""
import json
import re
import sqlite3
import time
from pathlib import Path

from .analysis_contract import DECISIONS, SPEC_VERSION, STAGES
from .analysis_store import BASE, MIGRATION, ROOT, canonical, digest, statements
from .audit_state import sha256

VERSION = 'c03-c04-v1'
SQL = ROOT / 'state/migrations/003_control.sql'
ROLES = ('pr_mining','review_analysis','code_evolution','chatgpt_linking',
         'behavior_analysis','independent_verification','note_writing')


def atomic(db):
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('caller-owned transaction and foreign_keys=ON required')


def migrate(db):
    atomic(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone():
        if list(map(tuple,db.execute('SELECT version,sha256 FROM ctl_migrations'))) != [(1,sha256(SQL))]:
            raise ValueError('control schema version/checksum mismatch')
        probe=sqlite3.connect(':memory:')
        try:
            probe.executescript(BASE.read_text()+MIGRATION.read_text()+SQL.read_text())
            query="SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'ctl_%' ORDER BY name"
            if list(map(tuple,db.execute(query))) != list(probe.execute(query)):
                raise ValueError('control schema drift')
        finally:probe.close()
        return False
    for sql in statements(SQL.read_text()):db.execute(sql)
    db.execute('INSERT INTO ctl_migrations VALUES(1,?)',(sha256(SQL),))
    return True


def rowdict(cursor):
    row=cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description],row)) if row is not None else None


def event(db,unit,event_name,details):
    db.execute('INSERT INTO ctl_events(unit_id,event,details) VALUES(?,?,?)',(unit,event_name,canonical(details)))


def file_path(root,relative,*,output=False):
    p=Path(relative)
    root=Path(root).resolve(); target=root/p
    if p.is_absolute() or '..' in p.parts or any(x.is_symlink() for x in (target,*target.parents)) or not target.resolve().is_relative_to(root):
        raise ValueError('unsafe relative path')
    if output and (not p.parts or p.parts[0] not in ('results','vault')):
        raise ValueError('outputs must be under results/ or vault/; originals are read-only')
    if not output and str(p)!='AGENTS.md' and (not p.parts or p.parts[0] not in ('tests','spec','state','results','data','vault')
                       or any('chatgpt' in x.lower() for x in p.parts)):
        raise ValueError('input path outside approved common-runtime boundary')
    return target


def hashes(root,ledger):
    if not isinstance(ledger,dict) or not ledger:raise ValueError('nonempty input hash ledger required')
    for p,h in ledger.items():
        if sha256(file_path(root,p)) != h:raise ValueError('stale/missing input: '+p)


def grant(db,ident,approval_id,scope,source,*,valid_from,valid_until):
    """Explicit scope registration after checking the actual human instruction.

    No default projects, roles, targets, phases, source paths or output roots.
    Bind approval prose itself so edits/revocation fail every lifecycle check.
    """
    atomic(db)
    approval=db.execute('SELECT * FROM approvals WHERE id=?',(approval_id,)).fetchone()
    required={'projects','origins','operations','roles','phases','targets','input_paths','output_roots','decision_ids','spec_sha256','analysis_versions','states'}
    if not approval or not source.strip() or set(scope)!=required or valid_until<=valid_from:
        raise ValueError('explicit authorization source, bounds and exact scope required')
    for k in required-{'spec_sha256'}:
        if not isinstance(scope[k],list) or not scope[k] or len(set(scope[k]))!=len(scope[k]) or not all(isinstance(v,str) and v.strip() and v!='*' for v in scope[k]):
            raise ValueError('explicit non-wildcard scope list: '+k)
    if not set(scope['states'])<=set(STAGES)|{'collection'}:raise ValueError('unknown allowed stage')
    if not set(scope['decision_ids']) <= DECISIONS:raise ValueError('unknown decisions')
    if set(scope['spec_sha256'])!={'AGENTS.md','spec/MASTER_PLAN.md','spec/NOTE_TEMPLATES.md'}:raise ValueError('canonical hashes required')
    # Historical prose cannot be upgraded into a structured runtime permission.
    # A newly authorized execution record must carry the exact reviewed scope
    # and validity interval. Its source is the original human instruction locator.
    try:authorized=json.loads(approval[1])
    except (TypeError,ValueError):raise ValueError('legacy approval prose grants no structured execution permission') from None
    expected={'kind':'execution_authorization','scope':scope,'valid_from':valid_from,'valid_until':valid_until}
    if authorized!=expected or source!=approval[2] or type(valid_from) is not int or type(valid_until) is not int:
        raise ValueError('grant differs from recorded exact execution authorization')
    values=(ident,approval_id,digest(tuple(approval)),source,canonical(scope),valid_from,valid_until)
    existing=db.execute('SELECT id,approval_id,approval_digest,source,scope,valid_from,valid_until FROM ctl_grants WHERE id=?',(ident,)).fetchone()
    if existing is not None:
        if tuple(existing)!=values:raise ValueError('grant identity conflict')
        return
    db.execute('INSERT INTO ctl_grants(id,approval_id,approval_digest,source,scope,valid_from,valid_until) VALUES(?,?,?,?,?,?,?)',values)


def validate_contract(c):
    required={'domain','role','phase','target','target_revision','decision_ids','spec_version','spec_sha256','inputs','source_versions','analysis_version','outputs'}
    if not isinstance(c,dict) or set(c)!=required:raise ValueError('complete execution contract required')
    for k in ['domain','role','phase','target','target_revision','spec_version','analysis_version']:
        if not isinstance(c[k],str) or not c[k].strip():raise ValueError('nonempty contract field: '+k)
    for k in ['spec_sha256','inputs']:
        if not isinstance(c[k],dict) or not c[k] or any(not isinstance(p,str) or not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for p,h in c[k].items()):
            raise ValueError('explicit path/SHA256 ledger required: '+k)
    for k in ['decision_ids','outputs']:
        if not isinstance(c[k],list) or any(not isinstance(x,str) or not x.strip() for x in c[k]) or len(c[k])!=len(set(c[k])):raise ValueError('unique string array required: '+k)
    if c['domain']=='analysis' and not re.fullmatch('[0-9a-f]{64}',c['target_revision']):raise ValueError('analysis content revision digest required')
    if not isinstance(c['source_versions'],list):raise ValueError('source_versions array required')
    seen=set()
    for v in c['source_versions']:
        if not isinstance(v,dict) or set(v)!={'id','path','sha256'} or type(v['id']) is not int or v['id']<=0 or v['id'] in seen or c['inputs'].get(v['path'])!=v['sha256']:
            raise ValueError('unique registered source identity/hash required')
        seen.add(v['id'])


def check(db,unit,root,*,now=None,dependencies=True):
    now=int(time.time()) if now is None else now
    j=rowdict(db.execute('SELECT * FROM ctl_jobs WHERE unit_id=?',(unit,)))
    if j is None:raise ValueError('explicit execution contract missing; no legacy authorization inference')
    q=rowdict(db.execute('SELECT * FROM queue_units WHERE id=?',(unit,)))
    g=rowdict(db.execute('SELECT * FROM ctl_grants WHERE id=?',(j['grant_id'],)))
    c=json.loads(j['contract']);validate_contract(c);s=json.loads(g['scope'])
    approval=db.execute('SELECT * FROM approvals WHERE id=?',(g['approval_id'],)).fetchone()
    if (g['revoked'] or not g['valid_from']<=now<g['valid_until'] or not approval
            or g['approval_digest']!=digest(tuple(approval)) or q['approval_id']!=g['approval_id']):
        raise ValueError('expired/revoked/changed authorization')
    expected_authorization={'kind':'execution_authorization','scope':s,'valid_from':g['valid_from'],'valid_until':g['valid_until']}
    if json.loads(approval[1])!=expected_authorization or g['source']!=approval[2]:raise ValueError('runtime grant differs from human scope record')
    if digest(c)!=j['contract_digest']:raise ValueError('contract modified')
    for key,field in [('projects','project'),('origins','origin'),('operations','operation')]:
        if q[field] not in s[key]:raise ValueError('scope violation: '+field)
    for key,field in [('roles','role'),('phases','phase'),('targets','target'),('analysis_versions','analysis_version')]:
        if c[field] not in s[key]:raise ValueError('scope violation: '+field)
    if c['domain'] not in ('collection','analysis') or c['spec_version']!=SPEC_VERSION:
        raise ValueError('domain/spec version mismatch')
    if c['domain']=='collection' and (c['role']!='collection' or c['phase']!='collect' or 'collection' not in s['states']):raise ValueError('collection role/phase/stage required')
    if c['domain']=='analysis' and c['role'] not in ROLES:raise ValueError('unknown logical analysis role')
    if not c['decision_ids'] or not set(c['decision_ids'])<=set(s['decision_ids']):raise ValueError('decision scope mismatch')
    if c['spec_sha256']!=s['spec_sha256']:raise ValueError('spec authorization mismatch')
    hashes(root,c['spec_sha256']);hashes(root,c['inputs'])
    if not set(c['inputs'])<=set(s['input_paths']):raise ValueError('source scope violation')
    if c.get('source_versions'):
        for v in c['source_versions']:
            if db.execute('SELECT 1 FROM source_versions WHERE id=? AND raw_path=? AND sha256=?',(v['id'],v['path'],v['sha256'])).fetchone() is None or c['inputs'].get(v['path'])!=v['sha256']:
                raise ValueError('source version/hash mismatch')
    for p in c['outputs']:
        file_path(root,p,output=True)
        if not any(Path(p).is_relative_to(Path(x)) for x in s['output_roots']):raise ValueError('output scope violation')
    if c['domain']=='analysis':
        head=db.execute('SELECT revision,stale FROM ctl_heads WHERE candidate_id=?',(c['target'],)).fetchone()
        if head is None and c['phase']=='enroll':
            pass
        elif head is None or head[0]!=c['target_revision']:raise ValueError('outdated/missing target revision')
        if head:
            current_run=db.execute('SELECT r.bundle FROM ctl_heads h JOIN ana_runs r ON r.id=h.run_id WHERE h.candidate_id=?',(c['target'],)).fetchone()
            candidate=next(x for x in json.loads(current_run[0])['candidates'] if x['id']==c['target'])
            if candidate['state'] not in s['states']:raise ValueError('current candidate stage outside authorization')
        if head and head[1] and c['role'] not in ('independent_verification','pr_mining','review_analysis','code_evolution','chatgpt_linking','behavior_analysis'):
            raise ValueError('stale candidate blocks publication')
    if dependencies:
        for pred, in db.execute('SELECT predecessor FROM ctl_dependencies WHERE unit_id=?',(unit,)):
            audit_completion(db,pred,root)
    if c['domain']=='collection':
        collection_route(q,c,s,root)
    return q,j,c


def collection_route(q,c,s,root):
    operations={'detail','reviews','comments','issue_comments','commits','files'}
    op=q['operation'];key=q['unit_key']
    if op in operations:
        parts=key.split(':')
        if len(parts)!=2 or not parts[0].isdigit() or parts[1]!=op or c['target']!='pr:'+parts[0]:
            raise ValueError('PR route does not match approved operation/target')
    elif op=='commits-full':
        if not key.isdigit() or c['target']!='pr:'+key:raise ValueError('commit recovery target mismatch')
    elif op=='github-pr-list':
        if c['target']!='inventory:'+q['origin'] or key not in ('metadata-before','metadata-after') and not re.fullmatch(r'page:[1-9][0-9]*',key):
            raise ValueError('inventory route mismatch')
    # Dynamic content-addressed snapshots still need an explicitly approved root.
    roots=[Path('data/raw/github')/q['origin'],Path('data/raw/git')/q['origin']]
    for p in roots:file_path(root,str(p))
    if not any(any(p.is_relative_to(Path(a)) for a in s['output_roots']) for p in roots):
        raise ValueError('approved raw snapshot output root missing')


def register(db,scope,key,contract,grant_id,root,*,dependencies=(),max_attempts=4):
    atomic(db)
    validate_contract(contract)
    if type(max_attempts) is not int or max_attempts<1:
        raise ValueError('complete execution contract required')
    existing=db.execute('SELECT id FROM queue_units WHERE approval_id=? AND project=? AND origin=? AND operation=? AND unit_key=?',(*scope,key)).fetchone()
    if existing:
        ident=existing[0];j=rowdict(db.execute('SELECT * FROM ctl_jobs WHERE unit_id=?',(ident,)))
        if j is None or j['contract']!=canonical(contract) or j['grant_id']!=grant_id or j['max_attempts']!=max_attempts or set(dependencies)!={r[0] for r in db.execute('SELECT predecessor FROM ctl_dependencies WHERE unit_id=?',(ident,))}:
            raise ValueError('duplicate unit has conflicting contract')
        check(db,ident,root,dependencies=False);return ident
    if not isinstance(key,str) or not key.strip():raise ValueError('nonempty unit key required')
    db.execute('INSERT INTO queue_units(approval_id,project,origin,operation,unit_key) VALUES(?,?,?,?,?)',(*scope,key))
    ident=db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.execute('INSERT INTO ctl_jobs(unit_id,grant_id,contract,contract_digest,max_attempts) VALUES(?,?,?,?,?)',(ident,grant_id,canonical(contract),digest(contract),max_attempts))
    for pred in dependencies:db.execute('INSERT INTO ctl_dependencies VALUES(?,?)',(ident,pred))
    check(db,ident,root,dependencies=False)
    event(db,ident,'registered',{'dependencies':list(dependencies)})
    return ident


def active(db,ident,token,root,*,now=None):
    atomic(db)
    now=int(time.time()) if now is None else now
    q,j,c=check(db,ident,root,now=now)
    if j['disposition']!='active' or q['state']!='running' or not token or token!=q['token'] or q['lease_until']<=now:
        raise ValueError('no active unexpired claim for id/token')
    return q,j,c


def audit_completion(db,ident,root):
    q=rowdict(db.execute('SELECT * FROM queue_units WHERE id=?',(ident,)))
    p=rowdict(db.execute('SELECT * FROM ctl_checkpoints WHERE unit_id=?',(ident,)))
    if not q or q['state']!='complete' or not p or digest(json.loads(q['result']))!=p['result_digest']:
        raise ValueError('predecessor/checkpoint is incomplete or inconsistent')
    hashes(root,json.loads(p['artifacts']))
    if p['run_id']:
        run=db.execute('SELECT bundle,grade,result_artifact FROM ana_runs WHERE id=?',(p['run_id'],)).fetchone()
        if not run or json.loads(q['result']).get('run_id')!=p['run_id']:raise ValueError('checkpoint run missing/mismatched')
        a=db.execute('SELECT path,sha256 FROM ana_artifacts WHERE id=?',(run[2],)).fetchone()
        if not a or json.loads(p['artifacts']).get(a[0])!=a[1]:raise ValueError('checkpoint artifact/run relationship mismatch')
        package=json.loads(file_path(root,a[0]).read_text())
        if canonical(package['bundle'])!=run[0] or package['grade']!=run[1]:raise ValueError('registered run differs from file')
        from .analysis_store import KINDS, TABLES
        expected={(kind,obj['id'],digest(obj),canonical(obj)) for table,kind in zip(TABLES,KINDS) for obj in package['bundle'][table]}
        found={tuple(r) for r in db.execute('SELECT m.kind,m.object_id,m.revision,r.payload FROM ana_run_revisions m JOIN ana_revisions r ON r.kind=m.kind AND r.object_id=m.object_id AND r.revision=m.revision WHERE m.run_id=?',(p['run_id'],))}
        edges={tuple(r) for r in db.execute('SELECT from_kind,from_id,relation,to_kind,to_id FROM ana_edges WHERE run_id=?',(p['run_id'],))}
        if expected!=found or edges!=set(map(tuple,package['edges'])):raise ValueError('checkpoint analysis graph mismatch')
    return p


def handoff(c,project,payload):
    fields={'project','candidate_id','target_revision','observations','hypotheses','evidence_ids','unknowns','next_tasks'}
    if not isinstance(payload,dict) or set(payload)!=fields or (payload['project'],payload['candidate_id'],payload['target_revision'])!=(project,c['target'],c['target_revision']):
        raise ValueError('versioned role handoff required')
    if any(not isinstance(payload[k],list) for k in fields-{'project','candidate_id','target_revision'}):raise ValueError('handoff arrays required')


def checkpoint(db,q,c,result,artifacts,payload,root,run=None):
    handoff(c,q['project'],payload);hashes(root,artifacts)
    if set(artifacts)!=set(c['outputs']):raise ValueError('result outputs differ from contract')
    db.execute('INSERT INTO ctl_checkpoints(unit_id,result_digest,run_id,artifacts,handoff) VALUES(?,?,?,?,?)',(q['id'],digest(result),run,canonical(artifacts),canonical(payload)))
    db.execute("UPDATE queue_units SET state='complete',token=NULL,lease_until=NULL,result=?,last_error=NULL WHERE id=?",(canonical(result),q['id']))
    event(db,q['id'],'complete',{'run_id':run,'artifacts':artifacts})


def split(db,group,manifest):
    """Partition manifest explicitly covers the universe, including deliberate gaps."""
    atomic(db)
    if set(manifest)!={'project','pr','axis','universe','parts','unreviewed'} or manifest['axis'] not in ('feature','file','thread'):
        raise ValueError('explicit PR split manifest required')
    if len(manifest['universe'])!=len(set(manifest['universe'])) or len(manifest['unreviewed'])!=len(set(manifest['unreviewed'])):raise ValueError('duplicate split scope')
    all_units=[]
    for key,part in manifest['parts'].items():
        unit=db.execute('SELECT project FROM queue_units WHERE id=?',(key,)).fetchone()
        if not unit or unit[0]!=manifest['project']:raise ValueError('split references unknown/cross-project unit')
        if not part:raise ValueError('empty split')
        all_units.extend(part)
    if len(all_units)!=len(set(all_units)) or set(all_units)&set(manifest['unreviewed']) or set(all_units)|set(manifest['unreviewed'])!=set(manifest['universe']):
        raise ValueError('split overlap/coverage mismatch')
    db.execute('INSERT INTO ctl_splits(group_id,manifest) VALUES(?,?)',(group,canonical(manifest)))


def merge_split(db,group,unit):
    atomic(db)
    row=db.execute('SELECT manifest,merged_by FROM ctl_splits WHERE group_id=?',(group,)).fetchone()
    if not row or row[1] is not None:raise ValueError('split absent/already merged')
    m=json.loads(row[0])
    if m['unreviewed']:raise ValueError('unreviewed split scope')
    deps={str(x[0]) for x in db.execute('SELECT predecessor FROM ctl_dependencies WHERE unit_id=?',(unit,))}
    if deps!=set(m['parts']):raise ValueError('merge dependencies must cover exact split parts')
    db.execute('UPDATE ctl_splits SET merged_by=? WHERE group_id=?',(unit,group))
