"""Two pass title gate and conservative local segment extraction.

JSON decoding is structural parsing, not semantic review. In the first pass only
id/title fields are accessed. No summaries, settings or attachments are parsed.
The second pass never accesses mapping/content for a NON_DEV conversation.
"""
import contextlib
import fcntl
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..analysis_store import canonical, digest

TITLE_RULE = 'title-v1'
BODY_RULE = 'segment-v1'
ROOT = Path(__file__).resolve().parents[2]
SPEC = {'AGENTS.md':'4189149bc52a0fb856705aff02637175071eb9355ff6df0653802499e32e7bd2',
        'spec/MASTER_PLAN.md':'9adc24e851a4f59509554fef488fa2094c36d3b1795aefdd9b076e30c35e8d3a',
        'spec/NOTE_TEMPLATES.md':'200d182ef85d4221116a25d7728d3baadfbeb0d9410a1f608222877f26756c5a'}
DEV = re.compile(r'(?i)\b(?:react|typescript|javascript|python|java|node(?:\.?js)?|next\.?js|nestjs|express|spring|sql|mysql|sqlite|postgres|redis|mongodb|api|rest|graphql|http|https|cors|oauth|jwt|github|git|docker|aws|ec2|s3|vercel|netlify|vite|webpack|eslint|css|html|tailwind|prisma|sequelize|typeorm|npm|pnpm|yarn|jest|vitest|usequery|useeffect|usestate|tanstack|zustand|recoil|redux|figma|linux|bash|zsh|powershell|websocket|socket\.io|supabase|firebase|flutter|swift|kotlin|c\+\+|c#|dnd|ci/cd|influy|spitching)\b|프론트엔드|백엔드|코딩|프로그래밍|자바스크립트|타입스크립트|리액트|파이썬|깃허브|깃 |깃랩|컴포넌트|데이터베이스|알고리즘|자료구조|배포|디버깅|코드(?:\s|$|에서|를|가|의|에|로|수정|리뷰|작성|설명|분석)|소스 ?코드|렌더링|리렌더|라우팅|라우터|브랜치|커밋|풀 ?리퀘|풀리퀘|의존성|패키지|환경변수|빌드|쿼리|스키마|미들웨어|엔드포인트|타입 ?오류|컴파일|비동기|콜백|훅\b|바텀시트|만수무강')
# Only unmistakable everyday subjects; conflict with any development cue => AMBIGUOUS.
NON_DEV = re.compile(r'(?i)여행\s?(?:일정|코스|계획|준비|추천)|(?:오늘|내일|저녁|점심|아침)\s?(?:메뉴|뭐\s?먹)|(?:맛집|식당|카페)\s?(?:추천|예약)|(?:김치|된장찌개|김치찌개|파스타|계란|라면|볶음밥)\s?(?:레시피|요리법|만들기)|(?:사주|운세|타로|꿈해몽|꿈\s?해몽|별자리)|(?:감기|두통|복통|생리통|치통|피부염|알레르기)\s?(?:증상|원인|약|치료)|(?:항공권|호텔)\s?(?:예약|추천|취소)|(?:강아지|고양이)\s?(?:사료|간식|건강)|(?:옷|신발)\s?(?:코디|사이즈\s?추천)')
CODE = re.compile(r'(?m)(?:^|\s)(?:const|let|var|function|import|export|return|async|await|class|def|SELECT|CREATE TABLE)\b|=>|</?[A-Za-z][^>]*>|\b[A-Za-z_$][\w$]*\([^\n]{0,160}\)\s*[;{]|\b(?:TypeError|ReferenceError|SyntaxError|Error:)\b|(?:src|app|components|pages|hooks|services)/[\w./\[\]-]+|\b[\w.-]+\.(?:tsx?|jsx?|py|sql|css|html|yml|yaml|json)\b')
SECRET = re.compile(r'(?i)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|(?:bearer\s+)[\w.+/-]{12,}|(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*[\x22\x27]?[^\s\x22\x27,;]{4,})')
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)')
HOME = re.compile(r'/(?:Users|home)/[^/\s]+')
PROJECTS = {'influy':re.compile('influy',re.I),'spitching':re.compile('spitching',re.I),
            '만수무강':re.compile(r'만수무강|UMC-MSMG',re.I),
            'ON':re.compile(r'UMC-ON|ON-FRONTEND|ON-web',re.I)}


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def esc(value):return str(value).replace('~','~0').replace('/','~1')


def safe(root,relative):
    root=Path(root).resolve();p=Path(relative);q=root/p
    if p.is_absolute() or '..' in p.parts or not q.resolve().is_relative_to(root) or any(x.is_symlink() for x in (q,*q.parents)):
        raise ValueError('unsafe path')
    return q


def utc(value):
    if isinstance(value,(float,int)):
        try:return datetime.fromtimestamp(value,timezone.utc).isoformat()
        except (OverflowError,ValueError,OSError):return None
    return value if isinstance(value,str) and value else None


def connect(path,*,readonly=False):
    path=Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('symlink database path')
    path=path.resolve()
    if not readonly:
        parent=path.parent
        allowed=(parent.parent==ROOT/'results' and parent.name.startswith('chatgpt-c-')) or (
            not parent.is_relative_to(ROOT) and parent.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        if not allowed or path.name not in ('catalog.sqlite3','search.sqlite3'):
            raise ValueError('writes require isolated chatgpt-c results or system-temp database')
    db=sqlite3.connect(path.as_uri()+'?mode=ro' if readonly else path,uri=readonly)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
    if readonly:db.execute('PRAGMA query_only=ON')
    else:db.execute('PRAGMA journal_mode=DELETE')
    return db


@contextlib.contextmanager
def run_lock(output):
    output=Path(output)
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):raise ValueError('symlink output')
    output.mkdir(parents=True,exist_ok=True)
    with (output/'pipeline.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)


def initialize(output):
    output=Path(output)
    for name,sql in [('catalog.sqlite3','schema.sql'),('search.sqlite3','search_schema.sql')]:
        db=connect(output/name)
        expected=file_hash(Path(__file__).with_name(sql))
        try:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            if exists:
                v=db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if not v or v[0]!=expected:raise ValueError('schema version drift; use a fresh output directory')
            db.executescript(Path(__file__).with_name(sql).read_text())
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('schema',expected))
            rules=digest([TITLE_RULE,DEV.pattern,NON_DEV.pattern,BODY_RULE,CODE.pattern,SECRET.pattern,EMAIL.pattern,PHONE.pattern,HOME.pattern])
            prior=db.execute("SELECT value FROM meta WHERE key='classification_rules'").fetchone()
            if prior and prior[0]!=rules:raise ValueError('classification rule drift; new version/output required')
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('classification_rules',rules));db.commit()
        finally:db.close()


def classify_title(title):
    """Pure function with exactly one title string input. No conversation access."""
    d=DEV.search(title);n=NON_DEV.search(title)
    if d and n:return 'AMBIGUOUS','conflicting title cues',1
    if d:return 'DEV','explicit development title cue: '+d.group(0),1
    if n:return 'NON_DEV','unambiguous everyday title cue: '+n.group(0),0
    return 'AMBIGUOUS','title alone insufficient; no automatic exclusion',1


def conversations(value):
    if isinstance(value,list):return [(f'/{i}',x) for i,x in enumerate(value)]
    if isinstance(value,dict) and isinstance(value.get('conversations'),list):return [(f'/conversations/{i}',x) for i,x in enumerate(value['conversations'])]
    if isinstance(value,dict) and ('conversation_id' in value or 'mapping' in value):return [('',value)]
    raise ValueError('unsupported conversation container')


def inventory(root,output,*,allowed_accounts=None):
    root=Path(root).resolve();db=connect(Path(output)/'catalog.sqlite3');rows=[]
    try:
        account_dirs=[a for a in sorted(safe(root,'data/raw/chatgpt').iterdir()) if a.is_dir()]
        if allowed_accounts is None and root==ROOT:
            authorization=json.loads((Path(output)/'AUTHORIZATION.json').read_text())
            allowed_accounts=[p for p in authorization['input_roots'] if p.startswith('data/raw/chatgpt/')]
        if allowed_accounts is not None and {str(a.relative_to(root)) for a in account_dirs}!=set(allowed_accounts):
            raise ValueError('actual source folders differ from exact authorized account roots')
        for a in account_dirs:
            if not a.is_dir():continue
            safe(root,a.relative_to(root));account='source-'+digest(['chatgpt',str(a.relative_to(root))])[:24]
            fs=[]
            for p in sorted(a.rglob('*')):
                if p.is_symlink():raise ValueError('symlink in raw export')
                if p.is_file():fs.append((p,p.stat().st_size,file_hash(p)))
            batch='batch-'+digest([(str(p.relative_to(a)),n,h) for p,n,h in fs])
            with db:
                db.execute('INSERT OR IGNORE INTO accounts VALUES(?,?,?,NULL)',(account,'chatgpt',str(a.relative_to(root))))
                db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?)',(batch,account,canonical([(str(p.relative_to(a)),h) for p,n,h in fs])))
                for p,n,h in fs:
                    rel=str(p.relative_to(a));kind='conversations' if re.fullmatch(r'conversations(?:-\d+)?\.json',rel) else 'unopened_metadata_or_asset'
                    ident=digest([batch,rel,h]);db.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?,?,?)',(ident,account,batch,str(p.relative_to(root)),rel,n,h,kind))
                    rows.append({'id':ident,'account':account,'batch':batch,'path':str(p.relative_to(root)),'relative_path':rel,'size':n,'sha256':h,'kind':kind})
        return rows
    finally:db.close()


def titles(root,output,files):
    db=connect(Path(output)/'catalog.sqlite3');failures=[]
    try:
        for f in files:
            if f['kind']!='conversations':continue
            if db.execute("SELECT 1 FROM stages WHERE file_id=? AND stage='title' AND version=? AND status='complete'",(f['id'],TITLE_RULE)).fetchone():continue
            try:
                path=safe(root,f['path'])
                if file_hash(path)!=f['sha256']:raise ValueError('input_changed')
                arr=conversations(json.loads(path.read_text()))
                with db:
                    for pointer,c in arr:
                        if not isinstance(c,dict):raise ValueError('invalid_conversation')
                        # Deliberately no mapping/content/create_time access here.
                        cid=c.get('conversation_id') or c.get('id');kind='provider' if cid else 'source_pointer_surrogate'
                        cid=str(cid or ('missing-'+digest([f['sha256'],pointer])))
                        title=c.get('title') if isinstance(c.get('title'),str) else ''
                        category,reason,review=classify_title(title);ident=digest([f['id'],pointer,TITLE_RULE])
                        db.execute('INSERT OR IGNORE INTO classifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (ident,f['account'],f['id'],cid,kind,pointer,title,category,reason,TITLE_RULE,review,'excluded' if category=='NON_DEV' else 'pending_body'))
                    db.execute('INSERT OR REPLACE INTO stages VALUES(?,?,?,?,?,NULL)',(f['id'],'title',TITLE_RULE,'complete',len(arr)))
            except (ValueError,OSError,TypeError) as e:
                code=type(e).__name__ # Never log exception text containing private data.
                with db:db.execute('INSERT OR REPLACE INTO stages VALUES(?,?,?,?,?,?)',(f['id'],'title',TITLE_RULE,'failed',0,code))
                failures.append({'file_id':f['id'],'stage':'title','error_code':code})
        with db:db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('title_pass',digest([(f['id'],TITLE_RULE) for f in files if f['kind']=='conversations'])))
        return failures
    finally:db.close()


def redact(text):
    for pattern in (SECRET,EMAIL,PHONE,HOME):text=pattern.sub('[REDACTED]',text)
    return text


def text_parts(content,prefix='/content'):
    if not isinstance(content,dict):return []
    parts=content.get('parts')
    if isinstance(parts,list):return [(prefix+'/parts/'+str(i),x) for i,x in enumerate(parts) if isinstance(x,str)]
    if isinstance(content.get('text'),str):return [(prefix+'/text',content['text'])]
    return []


def select_segments(text):
    """Paragraph/fenced-code units. Uncertain/ordinary paragraphs stay unindexed."""
    pattern=r'```[\s\S]*?(?:```|\Z)|[^\n]+(?:\n(?!\s*\n|```)[^\n]+)*'
    result=[]
    for match in re.finditer(pattern,text):
        raw=match.group(0)
        cue=DEV.search(raw) or CODE.search(raw)
        if not cue:continue
        # Mixed everyday paragraphs need explicit code; hold instead of broad inclusion.
        if NON_DEV.search(raw) and not CODE.search(raw):continue
        cleaned=redact(raw)
        if cleaned.strip():result.append((match.start(),match.end(),cleaned,'technical paragraph/code cue',int(cleaned!=raw)))
    return result


def semantic(role,text):
    if role=='assistant':return 'ai_proposal_unverified'
    if role=='user':
        if re.search(r'적용했|채택했|구현했|수정했|해결했|선택했',text):return 'user_reported_action_unverified'
        return 'user_statement_unverified'
    return 'tool_output_unverified'


def asset_refs(value,pointer=''):
    """Identifiers only; never dereference a file or copy attachment metadata text."""
    found=[]
    if isinstance(value,dict):
        for k,v in value.items():
            p=pointer+'/'+esc(k)
            if k in ('asset_pointer','file_id') and isinstance(v,str):found.append((p,v,k))
            elif k=='attachments' and isinstance(v,list):
                for i,a in enumerate(v):
                    if isinstance(a,dict):
                        for key in ('id','file_id'):
                            if isinstance(a.get(key),str):found.append((p+'/'+str(i)+'/'+key,a[key],'attachment'))
            else:found.extend(asset_refs(v,p))
    elif isinstance(value,list):
        for i,x in enumerate(value):
            if isinstance(x,(dict,list)):found.extend(asset_refs(x,pointer+'/'+str(i)))
    return found


def ingest_conversation(db,c,row,f,*,fail_after=None):
    if row['category'] not in ('DEV','AMBIGUOUS'):raise ValueError('body access denied by title classification')
    mapping=c.get('mapping')
    if not isinstance(mapping,dict):raise ValueError('unsupported_mapping')
    rev=digest(c);cid=row['conversation_id'];account=row['account']
    key=digest(['chatgpt',account,cid,rev])
    exists=db.execute('SELECT 1 FROM conversations WHERE id=?',(key,)).fetchone()
    db.execute('INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)',
       (key,'chatgpt',account,cid,rev,row['category'],utc(c.get('create_time')),utc(c.get('update_time')),c.get('current_node'),'processed'))
    db.execute('INSERT OR IGNORE INTO conversation_locations VALUES(?,?,?,?,?,?)',(row['id'],key,f['batch'],f['path'],f['sha256'],row['pointer']))
    if exists:return
    children={str(n):[] for n in mapping}
    for n,node in mapping.items():
        if not isinstance(node,dict):raise ValueError('invalid_node')
        p=node.get('parent')
        if p is not None:children.setdefault(str(p),[]).append(str(n))
    for index,(nid,node) in enumerate(mapping.items()):
        msg=node.get('message');mkey=None
        if isinstance(msg,dict):
            mid=str(msg.get('id') or 'node:'+str(nid));mrev=digest(msg)
            mkey=digest(['chatgpt',account,cid,mid,mrev,str(nid),rev])
            role=(msg.get('author') or {}).get('role','unknown');parts=text_parts(msg.get('content'))
            suf='/mapping/'+esc(nid)+'/message';count=0
            db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (mkey,key,str(nid),'chatgpt',account,cid,mid,mrev,'provider' if msg.get('id') else 'node_surrogate',role,utc(msg.get('create_time')),digest(msg.get('content')),suf,semantic(role,''),'empty'))
            for part_pointer,text in parts:
                if role not in ('user','assistant','tool'):continue
                for start,end,cleaned,reason,redacted in select_segments(text):
                    ident=digest([mkey,part_pointer,start,end,BODY_RULE]);skind=semantic(role,text[start:end])
                    hints=[p for p,r in PROJECTS.items() if r.search(cleaned)]
                    # An analysis unit is a source span, not an asserted unified experience.
                    unit='span-unit:'+ident
                    cur=db.execute('INSERT OR IGNORE INTO segments(id,message,part_pointer,start,end,text,text_sha256,rule,reason,redacted,semantic_kind,analysis_unit,project_hints) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (ident,mkey,part_pointer,start,end,cleaned,hashlib.sha256(cleaned.encode()).hexdigest(),BODY_RULE,reason,redacted,skind,unit,canonical(hints)))
                    if cur.rowcount:
                        rid=cur.lastrowid;identifiers=' '.join(['chatgpt',account,cid,mid]+hints)
                        db.execute('INSERT INTO message_fts(rowid,text,identifiers) VALUES(?,?,?)',(rid,cleaned,identifiers))
                        db.execute('INSERT INTO substring_fts(rowid,text) VALUES(?,?)',(rid,cleaned))
                    count+=1
            status='indexed' if count else ('excluded_role' if role not in ('user','assistant','tool') else ('held' if parts else 'empty'))
            db.execute('UPDATE messages SET status=? WHERE id=?',(status,mkey))
            for p,aid,kind in asset_refs(msg):
                db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,0)',(digest([mkey,p,aid]),mkey,aid,suf+p,kind))
        db.execute('INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?)',(key,str(nid),node.get('parent'),canonical(node.get('children',[])),canonical(children.get(str(nid),[])),mkey))
        if fail_after is not None and index>=fail_after:raise RuntimeError('synthetic interruption')


def extract(root,output,files,*,fail_after=None):
    db=connect(Path(output)/'search.sqlite3');db.execute('ATTACH DATABASE ? AS cat',(str(Path(output)/'catalog.sqlite3'),));failures=[]
    try:
        if not db.execute("SELECT 1 FROM cat.meta WHERE key='title_pass'").fetchone():raise ValueError('title pass must precede body processing')
        for f in files:
            if f['kind']!='conversations':continue
            if not db.execute("SELECT 1 FROM cat.stages WHERE file_id=? AND stage='title' AND version=? AND status='complete'",(f['id'],TITLE_RULE)).fetchone():continue
            if db.execute("SELECT 1 FROM cat.stages WHERE file_id=? AND stage='body' AND version=? AND status='complete'",(f['id'],BODY_RULE)).fetchone():continue
            try:
                p=safe(root,f['path'])
                if file_hash(p)!=f['sha256']:raise ValueError('input_changed')
                arr=conversations(json.loads(p.read_text()));count=0
                with db:
                    for pointer,c in arr:
                        row=db.execute('SELECT * FROM cat.classifications WHERE file_id=? AND pointer=? AND rule=?',(f['id'],pointer,TITLE_RULE)).fetchone()
                        if row is None:raise ValueError('title classification missing')
                        if row['category']=='NON_DEV':continue # no mapping/content/hash of object access
                        ingest_conversation(db,c,row,f,fail_after=fail_after);count+=1
                        db.execute("UPDATE cat.classifications SET status='processed' WHERE id=?",(row['id'],))
                    db.execute('INSERT OR REPLACE INTO cat.stages VALUES(?,?,?,?,?,NULL)',(f['id'],'body',BODY_RULE,'complete',count))
            except (ValueError,OSError,TypeError,RuntimeError) as e:
                code=type(e).__name__
                with db:db.execute('INSERT OR REPLACE INTO cat.stages VALUES(?,?,?,?,?,?)',(f['id'],'body',BODY_RULE,'failed',0,code))
                failures.append({'file_id':f['id'],'stage':'body','error_code':code})
        return failures
    finally:db.close()


def literal_query(value):return '"'+value.replace('"','""')+'"'


def search(db,query,*,substring=False,account=None,conversation_id=None,message_id=None,since=None,until=None,project=None,pr_number=None,repository=None,limit=20):
    if (not query.strip() and pr_number is None and repository is None) or not 1<=limit<=1000:raise ValueError('query or GitHub scope and bounded limit required')
    if substring and len(query)<3:raise ValueError('substring query requires >=3 characters')
    table='substring_fts' if substring else 'message_fts';params=[literal_query(query)] if query.strip() else [];where=[]
    for k,v in [('account',account),('conversation_id',conversation_id),('message_id',message_id)]:
        if v is not None:where.append('v.'+k+'=?');params.append(v)
    for op,v in [('>=',since),('<=',until)]:
        if v is not None:where.append('v.created_at'+op+'?');params.append(v)
    if project:
        if project not in PROJECTS:raise ValueError('unknown project hint')
        where.append("EXISTS(SELECT 1 FROM json_each(v.project_hints) WHERE value=?)");params.append(project)
    if pr_number is not None or repository is not None:
        ghwhere=[]
        if pr_number is not None:
            if type(pr_number) is not int or pr_number<1:raise ValueError('positive PR number required')
            ghwhere.append('g.pr_number=?');params.append(pr_number)
        if repository is not None:ghwhere.append('g.origin=?');params.append(repository)
        where.append("v.id IN(SELECT l.segment FROM links l JOIN github_docs g ON g.id=l.github_doc JOIN search_runs r ON r.id=l.search_run WHERE json_extract(r.scope,'$.corpus')=(SELECT value FROM meta WHERE key='current_link_corpus') AND "+' AND '.join(ghwhere)+')')
    if query.strip():sql=f'SELECT v.*,bm25({table}) AS relevance FROM {table} JOIN searchable_segments v ON v.rowid={table}.rowid WHERE {table} MATCH ?'
    else:sql='SELECT v.*,0 AS relevance FROM searchable_segments v WHERE 1=1'
    if where:sql+=' AND '+' AND '.join(where)
    sql+=' ORDER BY relevance,v.id LIMIT ?';params.append(limit)
    rows=[dict(x) for x in db.execute(sql,params)]
    for r in rows:
        r['locations']=[dict(x) for x in db.execute('SELECT * FROM conversation_locations WHERE conversation=? ORDER BY classification_id',(r['conversation'],))]
    return rows


def pointer_get(value,pointer):
    for part in pointer.split('/')[1:] if pointer else []:
        part=part.replace('~1','/').replace('~0','~')
        value=value[int(part)] if isinstance(value,list) else value[part]
    return value


def trace(root,db,segment_id):
    row=db.execute('SELECT * FROM searchable_segments WHERE id=?',(segment_id,)).fetchone()
    if row is None:raise ValueError('unknown indexed segment')
    loc=db.execute('SELECT * FROM conversation_locations WHERE conversation=? ORDER BY classification_id LIMIT 1',(row['conversation'],)).fetchone()
    p=safe(root,loc['path'])
    if file_hash(p)!=loc['sha256']:raise ValueError('source hash changed')
    c=pointer_get(json.loads(p.read_text()),loc['pointer'])
    # The caller has already proved this is a permitted indexed conversation.
    msg=pointer_get(c,row['pointer_suffix'])
    if digest(msg)!=row['revision'] or digest(msg.get('content'))!=row['content_sha256']:raise ValueError('message revision mismatch')
    text=pointer_get(msg,row['part_pointer']);span=text[row['start']:row['end']]
    if redact(span)!=row['text']:raise ValueError('source span mismatch')
    return {'id':row['id'],'path':loc['path'],'sha256':loc['sha256'],
            'pointer':loc['pointer']+row['pointer_suffix']+row['part_pointer'],
            'start':row['start'],'end':row['end'],'matches':True}
