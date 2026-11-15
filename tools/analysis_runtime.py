"""C04 queue-bound analysis state, independent review and local publication.

Only this execution layer advances controlled current heads. C02 historical
imports remain imports, never implicit current/PASS records. Metadata gates
check traceability and reviewer separation, not semantic truth or human reading.
Files are immutable and installed before the single DB commit; interrupted files
are detectable orphans, and identical retries validate them before registration.
"""
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from . import execution_control as ctl
from .analysis_contract import revision_digest, validate_bundle, validate_transition
from .analysis_store import artifact, canonical, contract_package, digest, store_connection
from .audit_state import sha256

TEMPLATE='200d182ef85d4221116a25d7728d3baadfbeb0d9410a1f608222877f26756c5a'
COMPETENCIES={'성능 개선','근본 원인 분석','설계·트레이드오프','협업·소통','AI 활용·통제','안정성·장애 대응','오너십','학습 속도','사용자 관점'}
QUESTIONS={'가장 어려웠던 문제','갈등·의견 충돌','실패·실수','주도적 개선','빠른 학습'}
CHANGE_KINDS={'source_version','claim_revision','candidate_revision','user_correction','evidence_link','analysis_rule','spec','assessment_rule','verification_rule','template'}


def immutable_file(root,path,data):
    """No overwrite, fsync, atomic install; preserve originals/history on failure."""
    target=ctl.file_path(root,path,output=True)
    if target.exists():
        if target.read_bytes()!=data:raise ValueError('output path exists with different bytes')
        return sha256(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.staged-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        try:os.link(tmp,target)  # atomic no-replace; concurrent writers never clobber
        except FileExistsError:
            if target.read_bytes()!=data:raise ValueError('concurrent output conflict') from None
        directory=os.open(target.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()
    return sha256(target)


def current(db,ident):
    h=ctl.rowdict(db.execute('SELECT * FROM ctl_heads WHERE candidate_id=?',(ident,)))
    if h is None:raise ValueError('candidate has no controlled head')
    row=db.execute('SELECT bundle,grade FROM ana_runs WHERE id=?',(h['run_id'],)).fetchone()
    if not row or row[1]!='c01_valid':raise ValueError('current run is not C01 valid')
    b=json.loads(row[0]);c=next(x for x in b['candidates'] if x['id']==ident)
    if revision_digest(b,ident)!=h['revision']:raise ValueError('head/content mismatch')
    return h,b,c


def sources(db,root,b,c,contract,*,require_current=False):
    claims=[x for x in b['claims'] if x['id'] in c['claim_ids']]
    ids={e for x in claims for e in x['evidence_ids']}
    ledger={e['raw_path']:e['sha256'] for e in b['evidence'] if e['id'] in ids}
    if not ledger or not ledger.items()<=contract['inputs'].items():raise ValueError('all current evidence must be authorized hash-bound inputs')
    ctl.hashes(root,ledger)
    used_sources=[]
    for e in b['evidence']:
        if e['id'] not in ids:continue
        matches=db.execute('SELECT id FROM source_versions WHERE raw_path=? AND sha256=?',(e['raw_path'],e['sha256'])).fetchall()
        if len(matches)!=1:raise ValueError('source snapshot must have a unique registered version')
        if not any(v=={'id':matches[0][0],'path':e['raw_path'],'sha256':e['sha256']} for v in contract['source_versions']):
            raise ValueError('registered source version missing in contract')
        used_sources.append(matches[0][0])
    if require_current:
        for sid in used_sources:
            latest=db.execute('SELECT max(n.id) FROM source_versions old JOIN source_versions n ON n.origin=old.origin AND n.source_key=old.source_key WHERE old.id=?',(sid,)).fetchone()[0]
            if latest not in used_sources:raise ValueError('new source version requires evidence update/revalidation')
    return ledger


def review(db,b,c,record,contract,root):
    required={'id','verdict','reviewer','reviewed_at','revision_digest','input_hashes','claim_checks','original_refs',
              'review_scope','reason','counterevidence','issues','originals_reopened','attribution_checked','counterevidence_checked','followups_checked','resume_condition'}
    if set(record)!=required:raise ValueError('complete independent review record required')
    if record['verdict'] not in ('PASS','NEEDS_EVIDENCE','CONTRADICTED','PENDING','HELD'):raise ValueError('unknown review verdict')
    for k in ['id','reviewer','reviewed_at','review_scope','reason']:
        if not isinstance(record[k],str) or not record[k].strip():raise ValueError('review metadata required: '+k)
    if record['revision_digest']!=revision_digest(b,c['id']) or record['input_hashes']!=contract['inputs']:
        raise ValueError('stale review revision/inputs')
    authors={c['author']}|{x['author'] for table in ('claims','assessments') for x in b[table] if x['candidate_id']==c['id']}
    if record['reviewer'] in authors:raise ValueError('reviewer authored analysis')
    evid={e for x in b['claims'] if x['id'] in c['claim_ids'] for e in x['evidence_ids']}
    expected_refs=[{'evidence_id':e['id'],'location':e['location'],'raw_path':e['raw_path'],'sha256':e['sha256'],'version':e['version']} for e in b['evidence'] if e['id'] in evid]
    if sorted(record['original_refs'],key=canonical)!=sorted(expected_refs,key=canonical):raise ValueError('review original scope incomplete')
    if not isinstance(record['claim_checks'],list) or len(record['claim_checks'])!=len(c['claim_ids']) or {r.get('claim_id') for r in record['claim_checks']}!=set(c['claim_ids']):
        raise ValueError('all claims require individual review, without duplicates')
    for check in record['claim_checks']:
        if set(check)!={'claim_id','verdict','attribution','adoption_checked','alternatives_checked','reason','counterevidence'} or check['verdict'] not in ('SUPPORTED','NEEDS_EVIDENCE','CONTRADICTED') or check['attribution'] not in ('confirmed','unknown','incorrect') or not check['reason']:
            raise ValueError('claim finding/attribution/reason required')
        if not isinstance(check['counterevidence'],list):raise ValueError('counterevidence array required')
    if not isinstance(record['counterevidence'],list) or not isinstance(record['issues'],list):raise ValueError('review issues/counterevidence arrays required')
    for finding in record['counterevidence']:
        if not isinstance(finding,dict) or set(finding)!={'claim_id','evidence_id','reason'} or finding['claim_id'] not in c['claim_ids'] or finding['evidence_id'] not in evid or not finding['reason']:
            raise ValueError('counterevidence must reference reviewed claims and original evidence')
    for finding in record['claim_checks']:
        for counter in finding['counterevidence']:
            if not isinstance(counter,dict) or set(counter)!={'evidence_id','reason'} or counter['evidence_id'] not in evid or not counter['reason']:
                raise ValueError('claim counterevidence must reference reviewed original evidence')
    if record['verdict']=='PASS':
        if any(record[k] is not True for k in ('originals_reopened','attribution_checked','counterevidence_checked','followups_checked')):raise ValueError('review checks incomplete')
        if record['counterevidence']:raise ValueError('unresolved counterevidence blocks PASS')
        for x in record['claim_checks']:
            if x['verdict']!='SUPPORTED' or x['attribution']!='confirmed' or x['adoption_checked'] is not True or x['alternatives_checked'] is not True or x['counterevidence']:
                raise ValueError('unsupported attribution/adoption/alternatives/counterevidence blocks PASS')
        if any(i.get('blocking') and i.get('status')!='resolved' for i in record['issues']):raise ValueError('blocking issue')
    elif not record['resume_condition']:raise ValueError('non-PASS review requires resume condition')
    status={'PASS':'passed','NEEDS_EVIDENCE':'failed','CONTRADICTED':'failed','PENDING':'pending','HELD':'pending'}[record['verdict']]
    # New review supersedes previous verdicts only by explicit supersedes IDs.
    r={'id':record['id'],'candidate_id':c['id'],'reviewer':record['reviewer'],'reviewed_at':record['reviewed_at'],
       'revision_digest':record['revision_digest'],'status':status,'claim_ids':c['claim_ids'][:],
       'evidence_ids':sorted(evid),'issues':record['issues'],**{k:record[k] for k in ('originals_reopened','attribution_checked','counterevidence_checked','followups_checked')},
       'execution_review':copy.deepcopy(record)}
    if any(v['id']==r['id'] for v in b['verifications']):raise ValueError('review ID already used')
    b['verifications'].append(r);c['verification_ids'].append(r['id'])
    if record['verdict']!='PASS':
        c['disposition']='held';c['hold']={'missing_evidence':record['reason'],'question':record['review_scope'],'resume_condition':record['resume_condition']}
    return r


def pass_gate(db,b,c,contract,root):
    if c['disposition']!='active' or any(q.get('core') and not q.get('answer') for q in c.get('questions',[])):
        raise ValueError('held candidate/unanswered core question')
    ids=set(c['verification_ids']);records=[]
    for rid in sorted(ids):
        r=ctl.rowdict(db.execute('SELECT * FROM ctl_reviews WHERE id=?',(rid,)))
        if not r or r['candidate_id']!=c['id'] or r['stale'] or r['revision']!=revision_digest(b,c['id']):continue
        record=json.loads(r['record'])
        if any(i.get('blocking') and i.get('status')!='resolved' for i in record['issues']):raise ValueError('unresolved blocking issue')
        if record['verdict']!='PASS':raise ValueError('current failed/pending review requires explicit resolution')
        if record['input_hashes']!=contract['inputs']:raise ValueError('verification input set/hash changed')
        ctl.hashes(root,record['input_hashes']);records.append(r)
    if not records:raise ValueError('current complete independent execution PASS required')
    # C01 checks core facts, all-claim coverage, authors and assessment. It also
    # sees blocking issues in *every* attached review, not merely the PASS row.
    test=copy.deepcopy(b);candidate=next(x for x in test['candidates'] if x['id']==c['id'])
    if candidate['state'] not in ('VERIFIED','PUBLISHED'):
        advance(candidate,'VERIFIED',mode='skip_optional' if candidate['state']=='LINKED' else 'advance',reason='optional deep investigation skipped after independent review')
    if validate_bundle(test):raise ValueError('C01 verification gate rejected: '+canonical(validate_bundle(test)))
    return records[-1]['id']


def advance(c,state,*,mode='advance',reason=None):
    errors=validate_transition('candidate',c['state'],state,mode=mode,reason=reason)
    if errors:raise ValueError(canonical(errors))
    c['history'].append({'from':c['state'],'to':state,'mode':mode,**({'reason':reason} if reason else {})});c['state']=state


def invalidate(db,change,kind,reason,*,nodes=(),candidate_ids=()):
    """Traverse current C02 dependency graph backwards, preserving all revisions.

    Revalidation requests are queue intake in pending_approval, never permission
    to run. attach_revalidation binds them to an explicitly authorized queue unit.
    """
    ctl.atomic(db)
    if kind not in CHANGE_KINDS or not reason.strip():raise ValueError('typed change and reason required')
    record={'nodes':[list(n) for n in nodes],'candidate_ids':sorted(candidate_ids)}
    old=db.execute('SELECT kind,reason,record FROM ctl_changes WHERE id=?',(change,)).fetchone()
    if old and tuple(old)!=(kind,reason,canonical(record)):raise ValueError('change identity conflict')
    db.execute('INSERT INTO ctl_changes VALUES(?,?,?,?) ON CONFLICT DO NOTHING',(change,kind,reason,canonical(record)))
    affected=set(candidate_ids);front=set(map(tuple,nodes));seen=set(front)
    runs={r[0] for r in db.execute('SELECT run_id FROM ctl_heads')}
    edges=[tuple(r) for r in db.execute('SELECT run_id,from_kind,from_id,from_revision,to_kind,to_id,to_revision FROM ana_edges') if r[0] in runs]
    while front:
        next_nodes={tuple(e[1:4]) for e in edges if tuple(e[4:]) in front}-seen
        seen|=next_nodes;front=next_nodes
    affected|={ident for kind,ident,rev in seen if kind=='candidate'}
    for ident in sorted(affected):
        h=db.execute('SELECT revision FROM ctl_heads WHERE candidate_id=?',(ident,)).fetchone()
        if not h:raise ValueError('affected candidate not controlled')
        db.execute('UPDATE ctl_heads SET stale=1 WHERE candidate_id=?',(ident,))
        db.execute('UPDATE ctl_reviews SET stale=1 WHERE candidate_id=?',(ident,))
        db.execute('UPDATE ctl_publications SET valid=0 WHERE candidate_id=?',(ident,))
        db.execute("INSERT INTO ctl_revalidation VALUES(?,?,?,'pending_approval',NULL) ON CONFLICT DO NOTHING",(change,ident,h[0]))
    return sorted(affected)


def attach_revalidation(db,change,ident,unit,root):
    ctl.atomic(db);q,j,c=ctl.check(db,unit,root,dependencies=False)
    if c['target']!=ident or c['role']!='independent_verification':raise ValueError('authorized independent review job required')
    updated=db.execute("UPDATE ctl_revalidation SET state='queued',unit_id=? WHERE change_id=? AND candidate_id=? AND state='pending_approval'",(unit,change,ident))
    if updated.rowcount!=1:raise ValueError('revalidation intake missing/already queued')


def invalidate_source(db,source_id,change,reason):
    rows=db.execute('SELECT evidence_id,run_id FROM ana_locations WHERE source_version_id=?',(source_id,)).fetchall()
    nodes=[]
    for eid,run in rows:
        rev=db.execute("SELECT revision FROM ana_run_revisions WHERE run_id=? AND kind='evidence' AND object_id=?",(run,eid)).fetchone()
        if rev:nodes.append(('evidence',eid,rev[0]))
    return invalidate(db,change,'source_version',reason,nodes=nodes)


def render_experience(b,c,options,review_id,template_hash):
    required={'title','date','competencies','question_types','sections'}
    if set(options)!=required or not isinstance(options['title'],str) or not options['title'].strip() or '\n' in options['title']:
        raise ValueError('exact note fields/title required; arbitrary prose is not accepted')
    date.fromisoformat(options['date'])
    for field,vocab in [('competencies',COMPETENCIES),('question_types',QUESTIONS)]:
        values=options[field]
        if not isinstance(values,list) or not 1<=len(values)<=3 or len(set(values))!=len(values) or not set(values)<=vocab:raise ValueError('invalid frontmatter vocabulary')
    sections=options['sections'];required_sections={'summary','goal','constraints','role','judgment','result','speech'}
    if set(sections)!=required_sections:raise ValueError('all template sections require verified claim references')
    claims={x['id']:x for x in b['claims'] if x['id'] in c['claim_ids']}
    def content(name):
        ids=sections[name]
        if not isinstance(ids,list) or not ids or len(set(ids))!=len(ids):raise ValueError('section requires distinct claim IDs')
        texts=[]
        for ident in ids:
            if ident not in claims or claims[ident]['kind']=='ai_inference':raise ValueError('unverified hypotheses cannot enter experience notes')
            value=claims[ident]['text']
            if re.search(r'(?i)(^---|^#|score|잠정.{0,4}점수|가설|검증 대기)',value,re.M):raise ValueError('evaluation/hypothesis/markup outside note contract')
            texts.append(value)
        return ' '.join(texts)
    fm={'type':'case','project':c['project'],'created':options['date'],'updated':options['date'],
        'competencies':options['competencies'],'question_types':options['question_types'],'metric':'',
        'verified':True,'used_in':[],'status':'draft'}
    lines=['---']+[k+': '+canonical(v) for k,v in fm.items()]+['---','# '+options['title'],'',
       '## 한 줄 요약','> '+content('summary'),'','## 상황','- 목표: '+content('goal'),'- 제약: '+content('constraints'),'- 내 역할: '+content('role'),'',
       '## 판단',content('judgment'),'','## 결과','- 수치:','- '+content('result'),'','## 근거']
    evid={e for x in claims.values() if x['kind']!='ai_inference' for e in x['evidence_ids']}
    lines += ['- '+e['location']+' ('+e['id']+', '+e['sha256']+')' for e in b['evidence'] if e['id'] in evid]
    lines += ['', '## 30초 버전','> '+content('speech'),'','## 예상 꼬리 질문','- **Q.**','  - **A.**','']
    return '\n'.join(lines).encode(),fm


def complete(db,unit,token,request,handoff,root,*,fail_at=None):
    """Single caller transaction: queue, C02 relations, head, review, checkpoint.

    Actions: enroll, analyze, review, transition, change, publish. Replays of a
    completed unit use audit_completion; duplicate completion is rejected.
    """
    q,j,contract=ctl.active(db,unit,token,root)
    if contract['domain']!='analysis':raise ValueError('analysis domain required')
    action=request.get('action');ident=contract['target'];pending_review=None;publication=None;change=None
    role=contract['role'];phase=contract['phase']
    allowed={'enroll':({'pr_mining'},'enroll'),'analyze':(set(ctl.ROLES[:5]),'analyze'),
      'review':({'independent_verification'},'verify'),'transition':(set(ctl.ROLES[:6]),'analyze'),
      'change':(set(ctl.ROLES[:6]),'change'),'publish':({'note_writing'},'publish')}
    if action not in allowed or role not in allowed[action][0] or phase!=allowed[action][1]:raise ValueError('action outside authorized phase/role')
    if action=='enroll':
        if db.execute('SELECT 1 FROM ctl_heads WHERE candidate_id=?',(ident,)).fetchone():raise ValueError('candidate already enrolled')
        row=db.execute('SELECT bundle,grade FROM ana_runs WHERE id=?',(request['run_id'],)).fetchone()
        if not row or row[1]!='c01_valid':raise ValueError('legacy/incomplete run cannot be enrolled')
        b=json.loads(row[0]);c=next(x for x in b['candidates'] if x['id']==ident)
        if c['state'] in ('VERIFIED','PUBLISHED') or b['notes'] or any(v['status']=='passed' for v in b['verifications']):raise ValueError('enrollment cannot import PASS/publication')
    else:
        h,b,c=current(db,ident)
        if h['stale'] and action not in ('change','analyze','review'):raise ValueError('stale head requires revalidation')
    if len(b['candidates'])!=1 or c['project']!=q['project'] or b['analysis_version']!=contract['analysis_version']:
        raise ValueError('single scoped candidate and exact analysis version required')
    if revision_digest(b,ident)!=contract['target_revision']:raise ValueError('target revision mismatch')
    scope=json.loads(db.execute('SELECT scope FROM ctl_grants WHERE id=?',(j['grant_id'],)).fetchone()[0])
    if c['state'] not in scope['states'] or (action=='transition' and request['state'] not in scope['states']) or (action=='publish' and 'PUBLISHED' not in scope['states']):
        raise ValueError('candidate stage outside approved scope')
    sources(db,root,b,c,contract,require_current=action in ('review','publish') or action=='transition' and request['state']=='VERIFIED')
    before=copy.deepcopy(b)
    if action=='analyze':
        b=copy.deepcopy(request['bundle']);c=next(x for x in b['candidates'] if x['id']==ident)
        old=before['candidates'][0]
        if len(b['candidates'])!=1 or c['id']!=old['id'] or c['project']!=old['project'] or c['state']!=old['state'] or c['history']!=old['history'] or b['notes']:
            raise ValueError('analysis cannot change identity, history or publish')
        if any(v['status']=='passed' for v in b['verifications']):raise ValueError('analysis cannot create PASS')
        change=('analysis:'+str(unit),'candidate_revision','analysis content revision')
    elif action=='review':
        if h['stale']:
            for v in b['verifications']:v['status']='superseded'
            b['notes']=[];c['note_ids']=[]
            if c['state'] in ('VERIFIED','PUBLISHED'):
                advance(c,'INVESTIGATED',mode='revalidate',reason='current source/rule dependency invalidated; independent revalidation')
        resolutions=request.get('resolutions',{})
        if not isinstance(resolutions,dict):raise ValueError('explicit issue resolutions required')
        for v in b['verifications']:
            for issue in v['issues']:
                key=v['id']+':'+issue['id']
                if key in resolutions:
                    resolution=resolutions[key]
                    if set(resolution)!={'reason','evidence_ids'} or not resolution['reason'] or not resolution['evidence_ids'] or not set(resolution['evidence_ids'])<={e['id'] for e in b['evidence']}:
                        raise ValueError('resolution requires reason and current original evidence IDs')
                    issue.update(status='resolved',resolution=canonical(resolution))
        for rid in request.get('supersedes',[]):
            match=next((v for v in b['verifications'] if v['id']==rid),None)
            if match is None or match['status']=='superseded':raise ValueError('unknown/already superseded review')
            match['status']='superseded'
            db.execute('UPDATE ctl_reviews SET stale=1 WHERE id=? AND candidate_id=?',(rid,ident))
        if request.get('supersedes'):
            c['disposition']='active';c.pop('hold',None)
        pending_review=review(db,b,c,request['record'],contract,root)
    elif action=='transition':
        target=request['state']
        if target=='PUBLISHED':raise ValueError('PUBLISHED requires publication action')
        if target=='VERIFIED':pass_gate(db,b,c,contract,root)
        advance(c,target,mode=request.get('mode','advance'),reason=request.get('reason'))
    elif action=='change':
        if request['kind'] not in CHANGE_KINDS or not request['reason']:raise ValueError('typed change/reason required')
        change=(request['change_id'],request['kind'],request['reason'])
        for v in b['verifications']:v['status']='superseded'
        b['notes']=[];c['note_ids']=[]
        if c['state'] in ('VERIFIED','PUBLISHED'):advance(c,request.get('resume_state','INVESTIGATED'),mode='revalidate',reason=request['reason'])
        if 'bundle' in request:
            replacement=copy.deepcopy(request['bundle'])
            # Changed analysis content is handled by a separate authorized analyze
            # job, avoiding arbitrary mutation through a generic impact event.
            if replacement!=b:raise ValueError('change invalidates; content edits require analyze action')
    elif action=='publish':
        if c['state']!='VERIFIED':raise ValueError('only current VERIFIED candidate can publish')
        rid=pass_gate(db,b,c,contract,root)
        if sha256(ctl.file_path(root,'spec/NOTE_TEMPLATES.md'))!=TEMPLATE:raise ValueError('note template changed; revalidation required')
        if db.execute('SELECT 1 FROM ctl_publications WHERE candidate_id=? AND valid=1',(ident,)).fetchone():raise ValueError('duplicate current publication')
        path=request['path']
        if path not in contract['outputs'] or Path(path).parent!=Path('vault/experience')/c['project'] or not re.fullmatch(r'.+ \(.+\)\.md',Path(path).name):
            raise ValueError('new experience vault output path required')
        data,fm=render_experience(b,c,request['note'],rid,TEMPLATE)
        note_hash=hashlib.sha256(data).hexdigest()
        nid=request['note_id']
        if db.execute('SELECT 1 FROM ctl_publications WHERE id=? OR path=?',(nid,path)).fetchone():raise ValueError('publication ID/path already used; preserve previous file')
        n={'id':nid,'candidate_id':ident,'revision_digest':revision_digest(b,ident),'path':path,'template_sha256':TEMPLATE,
           'kind':'experience','publication_status':'published','sha256':note_hash,'verified':True,'status':'draft','used_in':[],
           'verification_id':rid,'claim_sections':request['note']['sections']}
        b['notes'].append(n);c['note_ids'].append(nid);advance(c,'PUBLISHED')
        publication=(n,data)
    if b['spec_version']!=contract['spec_version'] or b['analysis_version']!=contract['analysis_version'] or c['id']!=ident or c['project']!=q['project']:
        raise ValueError('analysis result scope/version mismatch')
    sources(db,root,b,c,contract,require_current=action in ('review','publish') or action=='transition' and request['state']=='VERIFIED')
    pkg=contract_package(b)
    for note in b['notes']:
        pkg['edges'].extend([['note',note['id'],'depends_on','verification',note['verification_id']],
                             ['note',note['id'],'depends_on','candidate',ident]])
    for verification in b['verifications']:
        reviewed=verification.get('execution_review',{})
        refutations={(f['claim_id'],f['evidence_id']) for f in reviewed.get('counterevidence',[])}
        refutations|={(finding['claim_id'],counter['evidence_id']) for finding in reviewed.get('claim_checks',[]) for counter in finding['counterevidence']}
        for claim_id,evidence_id in sorted(refutations):
            edge=['claim',claim_id,'refutes','evidence',evidence_id]
            if edge not in pkg['edges']:pkg['edges'].append(edge)
    outputs=contract['outputs'];json_paths=[p for p in outputs if p.startswith('results/') and p.endswith('.json')]
    if len(json_paths)!=1 or len(outputs)!=(2 if publication else 1):raise ValueError('one immutable C02 result package plus optional note required')
    result_path=json_paths[0];ledger={}
    ctl.handoff(contract,q['project'],handoff)
    # Recheck claim before installing anything; final recheck catches expiry during I/O.
    ctl.active(db,unit,token,root)
    if publication:ledger[publication[0]['path']]=immutable_file(root,publication[0]['path'],publication[1])
    ledger[result_path]=immutable_file(root,result_path,(canonical(pkg)+'\n').encode())
    if fail_at=='after_files':raise RuntimeError('synthetic interruption after file install')
    inputs=[artifact(root,p,contract['analysis_version']) for p in sorted(contract['inputs'])]
    predecessors={}
    if action!='enroll':
        for table,kind in [('candidates','candidate'),('claims','claim'),('evidence','evidence'),('assessments','assessment'),('verifications','verification'),('notes','note')]:
            old={x['id']:digest(x) for x in before[table]}
            predecessors.update({(kind,x['id']):old[x['id']] for x in b[table] if x['id'] in old and digest(x)!=old[x['id']]})
    run=store_connection(db,root,pkg,inputs,result_path,predecessors=predecessors)
    ctl.active(db,unit,token,root)
    if change:invalidate(db,*change,candidate_ids=[ident])
    db.execute('INSERT INTO ctl_heads(candidate_id,run_id,revision,stale) VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET run_id=excluded.run_id,revision=excluded.revision,stale=excluded.stale',
               (ident,run,revision_digest(b,ident),int(bool(change) or (action!='enroll' and h['stale']))))
    if pending_review:
        r=pending_review['execution_review']
        db.execute('INSERT INTO ctl_reviews VALUES(?,?,?,?,0,?,?)',(r['id'],ident,r['revision_digest'],r['verdict'],canonical(r),unit))
        if r['verdict']=='PASS':
            db.execute('UPDATE ctl_heads SET stale=0 WHERE candidate_id=?',(ident,))
            pending=list(map(tuple,db.execute("SELECT change_id,state,unit_id FROM ctl_revalidation WHERE candidate_id=? AND state<>'resolved'",(ident,))))
            db.execute("UPDATE ctl_revalidation SET state='resolved',unit_id=? WHERE candidate_id=? AND state<>'resolved'",(unit,ident))
            if pending:ctl.event(db,unit,'revalidation_resolved',{'previous_assignments':pending,'review_id':r['id']})
    if publication:
        n=publication[0]
        db.execute('INSERT INTO ctl_publications VALUES(?,?,?,?,?,?,1,?,?,?)',(n['id'],ident,n['revision_digest'],n['verification_id'],n['path'],n['sha256'],TEMPLATE,unit,canonical(n)))
    if fail_at=='before_checkpoint':raise RuntimeError('synthetic interruption before checkpoint')
    result={'execution_version':ctl.VERSION,'action':action,'candidate_id':ident,'revision':revision_digest(b,ident),'run_id':run}
    ctl.checkpoint(db,q,contract,result,ledger,handoff,root,run)
    return result


def audit_files(db,root,*,include_collection=False):
    """Read-only reconciliation. Never promote missing, corrupt or orphan files."""
    known={};findings=[]
    for artifacts, in db.execute('SELECT artifacts FROM ctl_checkpoints'):
        for path,h in json.loads(artifacts).items():
            if path in known and known[path]!=h:findings.append({'path':path,'status':'conflicting_registration'})
            known[path]=h
    bases=['results','vault']
    if include_collection:
        for scope, in db.execute('SELECT scope FROM ctl_grants'):
            for directory in json.loads(scope)['output_roots']:
                if Path(directory).is_relative_to('data/raw/github') or Path(directory).is_relative_to('data/raw/git'):
                    ctl.file_path(root,directory);bases.append(directory)
        for path,h in db.execute('SELECT raw_path,sha256 FROM source_versions'):
            if any(Path(path).is_relative_to(Path(base)) for base in bases):known[path]=h
    for p,h in known.items():
        try:
            if sha256(ctl.file_path(root,p))!=h:findings.append({'path':p,'status':'corrupt'})
        except OSError:findings.append({'path':p,'status':'missing'})
    for base in set(bases):
        directory=Path(root)/base
        if directory.exists():
            for p in directory.rglob('*'):
                if p.is_file() and str(p.relative_to(root)) not in known:
                    findings.append({'path':str(p.relative_to(root)),'status':'unregistered'})
    return findings


def reconcile(db,root):
    """Session-start/resume gate for filesystem/spec drift of controlled results.

    Hash only previously authorized ledgers. Commit this reconciliation before
    claiming work, so an ensuing rejected job cannot roll back invalidation.
    Missing/corrupt files produce revalidation intake, never automatic success.
    """
    ctl.atomic(db);changes=[]
    for ident,run in db.execute('SELECT candidate_id,run_id FROM ctl_heads').fetchall():
        row=db.execute('SELECT j.contract FROM ctl_checkpoints p JOIN ctl_jobs j ON j.unit_id=p.unit_id WHERE p.run_id=?',(run,)).fetchone()
        if not row:raise ValueError('head lacks committed execution checkpoint')
        contract=json.loads(row[0])
        for kind,ledger in [('spec',contract['spec_sha256']),('source_version',contract['inputs'])]:
            observed={}
            for path,expected in ledger.items():
                try:actual=sha256(ctl.file_path(root,path))
                except (OSError,ValueError):actual='missing-or-disallowed'
                if actual!=expected:observed[path]={'expected':expected,'actual':actual}
            if observed:
                event_kind='template' if set(observed)=={'spec/NOTE_TEMPLATES.md'} else kind
                change='drift:'+digest([ident,run,event_kind,observed])
                invalidate(db,change,event_kind,'authorized input/spec file drift: '+canonical(observed),candidate_ids=[ident])
                changes.append(change)
    return changes
