"""Evidence compatibility projections and real-data invariant checks."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..analysis_contract import CONTRACT_VERSION, SPEC_VERSION, validate_bundle
from ..analysis_store import canonical, contract_package, digest
from . import VERSION
from .core import BODY_RULE, TITLE_RULE, classify_title, connect, file_hash, pointer_get, redact, safe


def write_json(path,value):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def summary(output):
    c=connect(Path(output)/'catalog.sqlite3',readonly=True);s=connect(Path(output)/'search.sqlite3',readonly=True)
    try:
        active=s.execute("SELECT value FROM meta WHERE key='current_link_corpus'").fetchone()
        active=active[0] if active else ''
        return {'version':VERSION,'title_rule':TITLE_RULE,'body_rule':BODY_RULE,
          'accounts':[dict(a)|{'classifications':[dict(x) for x in c.execute('SELECT category,status,count(*) AS count FROM classifications WHERE account=? GROUP BY category,status',(a['id'],))],
                              'files':c.execute('SELECT count(*) FROM files WHERE account=?',(a['id'],)).fetchone()[0]} for a in c.execute('SELECT * FROM accounts ORDER BY physical_path')],
          'titles':[dict(x) for x in c.execute('SELECT category,status,count(*) AS count FROM classifications GROUP BY category,status')],
          'stages':[dict(x) for x in c.execute('SELECT stage,status,count(*) AS files,sum(count) AS items FROM stages GROUP BY stage,status')],
          'counts':{t:s.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['conversations','conversation_locations','nodes','messages','segments','assets','github_docs','search_runs','links','relations']},
          'current_corpus':active,
          'current_search_runs':s.execute("SELECT count(*) FROM search_runs WHERE json_extract(scope,'$.corpus')=?",(active,)).fetchone()[0],
          'current_links':s.execute("SELECT count(*) FROM links l JOIN search_runs r ON r.id=l.search_run WHERE json_extract(r.scope,'$.corpus')=?",(active,)).fetchone()[0],
          'message_status':[dict(x) for x in s.execute('SELECT role,status,count(*) AS count FROM messages GROUP BY role,status')],
          'link_status':[dict(x) for x in s.execute("SELECT l.direction,l.status,count(*) AS count FROM links l JOIN search_runs r ON r.id=l.search_run WHERE json_extract(r.scope,'$.corpus')=? GROUP BY l.direction,l.status",(active,))],
          'search_status':[dict(x) for x in s.execute("SELECT direction,status,count(*) AS count FROM search_runs WHERE json_extract(scope,'$.corpus')=? GROUP BY direction,status",(active,))],
          'project_hints':[dict(x) for x in s.execute('SELECT j.value AS hint,count(*) AS segments FROM segments,json_each(project_hints) j GROUP BY j.value')],
          'unindexed_conversations':s.execute("SELECT count(*) FROM conversations c WHERE NOT EXISTS(SELECT 1 FROM messages m WHERE m.conversation=c.id AND m.status='indexed')").fetchone()[0],
          'redacted_segments':s.execute('SELECT count(*) FROM segments WHERE redacted=1').fetchone()[0],
          'caveat':'Deterministic heuristics, not semantic/attribution verification. Uncertain text held; bounded exact-clue links pending independent review.'}
    finally:c.close();s.close()


def export_compatibility(output):
    output=Path(output)
    writer=connect(output/'search.sqlite3')
    try:
        with writer:writer.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('evidence_registered_at',datetime.now(timezone.utc).isoformat()))
        now=writer.execute("SELECT value FROM meta WHERE key='evidence_registered_at'").fetchone()[0]
    finally:writer.close()
    db=connect(output/'search.sqlite3',readonly=True)
    evidence=[];locations=[];pending=[]
    try:
        locmap={};all_locs={}
        for loc in db.execute('SELECT * FROM conversation_locations ORDER BY classification_id'):
            locmap.setdefault(loc['conversation'],loc);all_locs.setdefault(loc['conversation'],[]).append(dict(loc))
        for row in db.execute('SELECT * FROM searchable_segments ORDER BY id'):
            loc=locmap[row['conversation']]
            source_id=canonical(['chatgpt',row['account'],row['conversation_id'],row['message_id'],row['revision']])
            pointer=loc['pointer']+row['pointer_suffix']+row['part_pointer']
            e={'id':'chatgpt-evidence:'+row['id'],'source_id':source_id,'origin':'chatgpt',
               'raw_path':loc['path'],'sha256':loc['sha256'],'version':row['revision'],
               'location':pointer+':chars='+str(row['start'])+'-'+str(row['end']),
               'author':row['role']+'@'+row['account'],'created_at':row['created_at'],'collected_at':now,
               'provider':'chatgpt','source_account_id':row['account'],'conversation_id':row['conversation_id'],
               'message_id':row['message_id'],'revision':row['revision'],'export_batch_id':loc['batch'],
               'content_sha256':row['content_sha256'],'span_sha256':row['text_sha256'],
               'semantic_kind':row['semantic_kind'],'implementation_confirmed':False,'verification_status':'pending'}
            if not row['created_at']:
                pending.append({'evidence':e,'reason':'missing source timestamp; C01 required field unresolved'})
                continue
            evidence.append(e)
            locations.append({'evidence_id':e['id'],'raw_path':loc['path'],'snapshot_sha256':loc['sha256'],
                'source_version_id':None,'selector':{'location':e['location'],'version':e['version'],'source_id':source_id}})
        bundle={'contract_version':CONTRACT_VERSION,'spec_version':SPEC_VERSION,'analysis_version':VERSION,
                'candidates':[],'claims':[],'evidence':evidence,'assessments':[],'verifications':[],'notes':[]}
        diagnostics=validate_bundle(bundle)
        if diagnostics:raise ValueError('evidence projection failed common C01 validator')
        package=contract_package(bundle)
        # Use the existing C02 builder rather than a parallel relation model.
        write_json(output/'C01_EVIDENCE_BUNDLE.json',bundle)
        write_json(output/'C02_COMPATIBILITY_PACKAGE.json',package)
        write_json(output/'PENDING_EVIDENCE.json',pending)
        with (output/'LINK_CANDIDATES.jsonl').open('w') as out, (output/'FOR_CODEX_B.jsonl').open('w') as b:
            segment_map={x['id']:dict(x) for x in db.execute('SELECT * FROM searchable_segments')}
            github_map={x['id']:dict(x) for x in db.execute('SELECT id,source_version_id,origin,project,pr_number,kind,source_key,path,sha256,pointer,object_id,author,created_at FROM github_docs')}
            active=db.execute("SELECT value FROM meta WHERE key='current_link_corpus'").fetchone()
            active=active[0] if active else ''
            for l in db.execute("SELECT l.* FROM links l JOIN search_runs r ON r.id=l.search_run WHERE json_extract(r.scope,'$.corpus')=? ORDER BY l.id",(active,)):
                segment=segment_map[l['segment']]
                locs=all_locs[segment['conversation']]
                gh=github_map[l['github_doc']]
                r=dict(l);r['clues']=json.loads(r['clues']);r['chatgpt']={k:segment[k] for k in ['account','conversation_id','message_id','revision','role','created_at','part_pointer','start','end','pointer_suffix','content_sha256','text_sha256','semantic_kind']};r['chatgpt']['locations']=locs;r['github']=gh
                out.write(canonical(r)+'\n')
                if gh['project']=='influy' and gh['pr_number'] in (37,70,45):
                    r['candidate_mapping']='PR scope only; relevance to individual pilot claims unverified'
                    b.write(canonical(r)+'\n')
        changes={'kind':'evidence_link','state':'pending_approval','apply_to_operational_db':False,
                 'reason':'new ChatGPT exact-clue search candidates; independent original/attribution/adoption review required',
                 'candidate_ids_proposed':['influy-37-dnd-gesture','influy-37-tab-routing','influy-70-disabled-state','influy-70-upload-timing','influy-70-home-query','influy-45-deploy-workflow'],
                 'qualification':'Scope proposal only; does not assert all candidates are affected. Filter FOR_CODEX_B by PR and inspect originals before any invalidation.',
                 'current_verifications_reused':False,'statuses_changed':False,'next_operation':'review proposed evidence relevance; record explicit C04 invalidate only under future authorized integration'}
        write_json(output/'CHANGE_IMPACT_PROPOSAL.json',changes)
        result={'c01_valid_evidence':len(evidence),'c01_diagnostics':diagnostics,'pending_missing_metadata':len(pending),
                'c02_builder_reused':True,'operational_import_performed':False,'candidate_state_updates':0}
        write_json(output/'COMPATIBILITY_RESULT.json',result);return result
    finally:db.close()


def validate(root,output,files,*,github_sources=()):
    output=Path(output);c=connect(output/'catalog.sqlite3',readonly=True);db=connect(output/'search.sqlite3',readonly=True)
    errors=[];checked=Counter();cache={}
    try:
        for conn,label in [(c,'catalog'),(db,'search')]:
            if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':errors.append(label+':integrity')
            if conn.execute('PRAGMA foreign_key_check').fetchall():errors.append(label+':foreign_keys')
        for f in files:
            if file_hash(safe(root,f['path']))!=f['sha256']:errors.append('raw_changed:'+f['id'])
            checked['raw_files']+=1
        for f in github_sources:
            if file_hash(safe(root,f['path']))!=f['sha256']:errors.append('github_changed:'+str(f['source_version_id']))
            checked['github_source_files']+=1
        excluded=set()
        for row in c.execute('SELECT * FROM classifications'):
            result=classify_title(row['title'])
            if tuple(result)!=(row['category'],row['reason'],row['needs_review']):errors.append('classification_drift:'+row['id'])
            if row['category']=='NON_DEV':
                excluded.add(row['id'])
                if row['status']!='excluded':errors.append('nondev_status:'+row['id'])
            checked['title_only_decisions']+=1
        for loc in db.execute('SELECT * FROM conversation_locations'):
            if loc['classification_id'] in excluded:errors.append('NON_DEV_in_search:'+loc['classification_id'])
            cl=c.execute('SELECT * FROM classifications WHERE id=?',(loc['classification_id'],)).fetchone()
            conv=db.execute('SELECT * FROM conversations WHERE id=?',(loc['conversation'],)).fetchone()
            if not cl or cl['account']!=conv['account'] or cl['conversation_id']!=conv['conversation_id']:errors.append('account_or_identity_mismatch')
            checked['permitted_conversations']+=1
        # Order by source file to decode each split once, without opening NON_DEV fields.
        locmap={}
        for loc in db.execute('SELECT * FROM conversation_locations ORDER BY classification_id'):
            locmap.setdefault(loc['conversation'],loc)
        for row in db.execute('SELECT v.* FROM searchable_segments v JOIN (SELECT conversation,min(path) AS path FROM conversation_locations GROUP BY conversation) l ON l.conversation=v.conversation ORDER BY l.path,v.conversation,v.id'):
            loc=locmap[row['conversation']]
            if loc['path'] not in cache:
                cache.clear();cache[loc['path']]=json.loads(safe(root,loc['path']).read_text())
            conv=pointer_get(cache[loc['path']],loc['pointer']);msg=pointer_get(conv,row['pointer_suffix'])
            span=pointer_get(msg,row['part_pointer'])[row['start']:row['end']]
            if digest(msg)!=row['revision'] or digest(msg.get('content'))!=row['content_sha256'] or redact(span)!=row['text']:errors.append('trace_mismatch:'+row['id'])
            checked['segments_traced']+=1
        n=db.execute('SELECT count(*) FROM segments').fetchone()[0]
        for table in ['message_fts','substring_fts']:
            if db.execute('SELECT count(*) FROM '+table).fetchone()[0]!=n:errors.append(table+':count')
            if db.execute('SELECT count(*) FROM segments s LEFT JOIN '+table+' f ON f.rowid=s.rowid WHERE f.rowid IS NULL').fetchone()[0]:errors.append(table+':missing_rows')
        orphan_parents=db.execute('SELECT count(*) FROM nodes n WHERE parent IS NOT NULL AND NOT EXISTS(SELECT 1 FROM nodes p WHERE p.conversation=n.conversation AND p.node_id=n.parent)').fetchone()[0]
        # Node relationships are preserved even when the export itself is partial.
        result={'clean':not errors,'errors':errors,'checked':dict(checked),'nondev_in_search':sum(1 for l in db.execute('SELECT classification_id FROM conversation_locations') if l[0] in excluded),
                'orphan_parent_refs_preserved':orphan_parents,'assets_opened':0,'semantic_verification':'pending',
                'source_accounts':c.execute('SELECT count(*) FROM accounts').fetchone()[0]}
        write_json(output/'VALIDATION_RESULT.json',result);return result
    finally:c.close();db.close()
