"""Reuses hash-bound local GitHub snapshots; deterministic candidate search only."""
import json
import re
from pathlib import Path

from ..analysis_store import canonical, digest
from .core import PROJECTS, connect, file_hash, literal_query, redact, safe

LINK_RULE='exact-clue-v1'
REPOS={'INFLUY/Influy-FE':'influy','UMC-MSMG/Backend':'만수무강',
       'UMC-ON/ON-web':'ON','UMC-ON/ON-FRONTEND':'ON',
       '2024-capstone-project/SPitching-FE':'spitching'}
PERIODS={'influy':['2025-04','2025-09'],'spitching':['2024-09','2025-07'],
         '만수무강':['2024-11','2025-03'],'ON':['2024-07','2025-02']}


def objects(value,pointer):
    if isinstance(value,list):
        for i,x in enumerate(value):yield from objects(x,pointer+'/'+str(i))
    elif isinstance(value,dict):yield pointer,value


def github_index(root,output,snapshot):
    src=connect(snapshot,readonly=True);db=connect(Path(output)/'search.sqlite3');missing=[];sources=[]
    try:
        for r in src.execute('SELECT * FROM source_versions ORDER BY id'):
            match=re.fullmatch(r'pr:(\d+):(detail|reviews|comments|issue_comments|commits|files|compare-commits)(?::.*)?',r['source_key'])
            if not match or r['origin'] not in REPOS:continue
            p=safe(root,r['raw_path'])
            if not str(r['raw_path']).startswith('data/raw/github/'):continue
            if not p.is_file() or file_hash(p)!=r['sha256']:
                missing.append({'source_version_id':r['id'],'status':'missing_or_hash_mismatch'});continue
            sources.append({'source_version_id':r['id'],'path':r['raw_path'],'sha256':r['sha256']})
            if db.execute('SELECT 1 FROM github_docs WHERE source_version_id=? LIMIT 1',(r['id'],)).fetchone():continue
            raw=json.loads(p.read_text());payload=raw.get('payload');base_pointer='/payload'
            if match[2]=='compare-commits':
                payload=payload.get('commits',[]) if isinstance(payload,dict) else []
                base_pointer='/payload/commits'
            with db:
                for pointer,obj in objects(payload,base_pointer):
                    kind=match[2];text=[]
                    for field in ('title','body','path','filename','patch'):
                        if isinstance(obj.get(field),str):text.append(obj[field])
                    commit=obj.get('commit')
                    if isinstance(commit,dict) and isinstance(commit.get('message'),str):text.append(commit['message'])
                    if not text:continue
                    text=redact('\n'.join(text));ident=digest([r['id'],r['sha256'],pointer]);oid=obj.get('id') or obj.get('sha') or obj.get('filename')
                    user=obj.get('user') or obj.get('author') or {};author=user.get('login') if isinstance(user,dict) else None
                    created=obj.get('created_at') or obj.get('submitted_at') or ((commit or {}).get('author') or {}).get('date')
                    cur=db.execute('INSERT OR IGNORE INTO github_docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (ident,r['id'],r['origin'],REPOS[r['origin']],int(match[1]),kind,r['source_key'],r['raw_path'],r['sha256'],pointer,str(oid) if oid is not None else None,author,created,text))
                    if cur.rowcount:
                        rid=cur.lastrowid;ids=' '.join([r['origin'],REPOS[r['origin']],'PR'+match[1],str(oid or '')])
                        db.execute('INSERT INTO github_fts(rowid,text,identifiers) VALUES(?,?,?)',(rid,text,ids))
                        db.execute('INSERT INTO github_substrings(rowid,text) VALUES(?,?)',(rid,text))
        with db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('github_index',digest(sources)))
        return {'sources':sources,'failures':missing,'pr_inventory_count':src.execute('SELECT COUNT(*) FROM pr_inventory').fetchone()[0]}
    finally:src.close();db.close()


def clues(text):
    """Specific source strings before broad project/library names; bounded, logged."""
    candidates=[]
    patterns=[(r'(?:[\w@.-]+/)+[\w.\[\]-]+\.(?:tsx?|jsx?|css|py|sql|ya?ml|json)', 'path'),
              (r'\b[A-Za-z_$][\w$-]*\.(?:tsx?|jsx?|css|py|sql|ya?ml)\b','file'),
              (r'\b(?:use[A-Z]|[A-Z][a-z]+[A-Z]|[a-z]+[A-Z])[A-Za-z0-9_$]{3,}\b','identifier'),
              (r'\b(?:TypeError|ReferenceError|SyntaxError):[^\n]{8,100}','error'),
              (r'github\.com/[\w.-]+/[\w.-]+/(?:pull|commit)/[\w]+','github_url')]
    for pattern,kind in patterns:
        for m in re.finditer(pattern,text):
            token=m.group(0)
            generic={'useState','useEffect','useRef','useMemo','useCallback','useContext','useQuery','useMutation','className','onClick','onChange','addEventListener','removeEventListener','preventDefault','stopPropagation','querySelector','querySelectorAll','getElementById','JSON.stringify','setTimeout','setInterval','borderRadius','backgroundColor','fontSize','fontWeight','justifyContent','alignItems','queryKey','queryFn','staleTime','isLoading','isError','setState','handleClick','handleChange','consoleLog','node_modules','package.json'}
            if len(token)>=5 and token not in generic and '[REDACTED]' not in token and token not in {x['text'] for x in candidates}:
                candidates.append({'text':token,'kind':kind,'strength':'specific'})
    candidates=sorted(candidates,key=lambda x:(['path','file','error','github_url','identifier'].index(x['kind']),-len(x['text']),x['text']))[:6]
    if not candidates:
        for pattern in PROJECTS.values():
            m=pattern.search(text)
            if m:candidates.append({'text':m.group(0),'kind':'project_hint','strength':'weak'})
    return candidates


def discover_links(output,*,per_clue=8):
    db=connect(Path(output)/'search.sqlite3')
    try:
        corpus=digest([tuple(x) for x in db.execute('SELECT id,text_sha256 FROM segments ORDER BY id')]+[tuple(x) for x in db.execute('SELECT id,sha256 FROM github_docs ORDER BY id')])
        with db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('current_link_corpus',corpus))
        totals={};cache={}
        for direction in ('github_to_chatgpt','chatgpt_to_github'):
            source='github_docs' if direction=='github_to_chatgpt' else 'segments'
            target='segments' if source=='github_docs' else 'github_docs'
            fts='message_fts' if target=='segments' else 'github_fts'
            rows=db.execute('SELECT id,text FROM '+source+' ORDER BY id').fetchall();count=0
            # Chunk commits make interrupted discovery resumable. A single source
            # and all its links commit together; old corpus runs remain historical.
            for row in rows:
                query_clues=clues(row['text']);run=digest([corpus,direction,row['id'],LINK_RULE,per_clue])
                if db.execute('SELECT 1 FROM search_runs WHERE id=?',(run,)).fetchone():continue
                hits={}
                for clue in query_clues:
                    ck=(target,clue['text'])
                    if ck not in cache:
                        cache[ck]=[x[0] for x in db.execute(f'SELECT t.id,t.text FROM {fts} JOIN {target} t ON t.rowid={fts}.rowid WHERE {fts} MATCH ? ORDER BY bm25({fts}),t.id LIMIT ?',
                                                          (literal_query(clue['text']),per_clue)) if clue['text'].casefold() in x[1].casefold()]
                    for ident in cache[ck]:hits.setdefault(ident,[]).append(clue)
                hits=dict(sorted(hits.items(),key=lambda x:(-len(x[1]),x[0]))[:per_clue])
                status=('candidate' if any(x['strength']=='specific' for x in query_clues) else 'insufficient') if hits else ('not_found' if query_clues else 'not_searched')
                reason='exact textual clues only; project/adoption/implementation require independent review' if hits else ('no hit within bounded exact-clue search' if query_clues else 'no sufficiently specific automated clue; semantic/manual search pending')
                scope={'corpus':corpus,'target_table':target,'per_clue_limit':per_clue,'per_source_link_limit':per_clue,'clue_limit':6,'period_filter':None,'project_filter':None,'period_hints':PERIODS,'exhaustive':False}
                with db:
                    db.execute('INSERT INTO search_runs VALUES(?,?,?,?,?,?,?,?,?)',(run,direction,row['id'],canonical(query_clues),canonical(scope),status,reason,LINK_RULE,len(hits)))
                    for target_id,matched in hits.items():
                        seg,gh=(target_id,row['id']) if direction=='github_to_chatgpt' else (row['id'],target_id)
                        link_status='candidate' if any(x['strength']=='specific' for x in matched) else 'insufficient'
                        db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?,?,?,?,?,?,?)',(digest([run,seg,gh]),run,seg,gh,direction,canonical(matched),link_status,reason,'pending'))
                count+=1
            totals[direction]={'sources':len(rows),'new_search_runs':count}
        return totals
    finally:db.close()


def relations(output):
    db=connect(Path(output)/'search.sqlite3')
    try:
        with db:
            for group in db.execute('SELECT content_sha256 FROM messages WHERE status=\'indexed\' GROUP BY content_sha256 HAVING count(*)>1'):
                rows=db.execute('SELECT id,conversation,account FROM messages WHERE content_sha256=? ORDER BY id',(group[0],)).fetchall()
                for left,right in zip(rows,rows[1:]):
                    if left['conversation']==right['conversation']:continue
                    reason='exact content hash match; identity/context/adoption unverified; originals retained'
                    db.execute('INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?,?)',(digest(['same_content',left['id'],right['id']]),'same_content',left['id'],right['id'],'candidate',reason))
            for group in db.execute('SELECT account,conversation_id FROM conversations GROUP BY account,conversation_id HAVING count(*)>1'):
                rows=db.execute('SELECT id FROM conversations WHERE account=? AND conversation_id=? ORDER BY id',tuple(group)).fetchall()
                for left,right in zip(rows,rows[1:]):
                    db.execute('INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?,?)',(digest(['revision',left[0],right[0]]),'conversation_revision',left[0],right[0],'candidate','same provider identity; content-addressed snapshots, chronology unconfirmed'))
    finally:db.close()
