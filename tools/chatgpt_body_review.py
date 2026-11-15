"""Read-only review pages for approved title-AMBIGUOUS conversations.

No classifier: an agent must read every page and supply the semantic decision.
Only the new review directory receives ledgers; originals and prior DBs stay RO.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/chatgpt-body-review-2026-11-15'
PRIOR=ROOT/'results/chatgpt-c-2026-11-15'

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()

def pointer_get(v,p):
    for k in p.split('/')[1:] if p else []:
        k=k.replace('~1','/').replace('~0','~');v=v[int(k)] if isinstance(v,list) else v[k]
    return v

def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def render(row,cache):
    p=ROOT/row['path']
    if row['path'] not in cache:
        if sha(p)!=row['sha256']:raise ValueError('raw hash changed')
        cache[row['path']]=json.loads(p.read_text())
    c=pointer_get(cache[row['path']],row['pointer'])
    lines=[f"CONVERSATION {row['id']} | account={row['account']} | provider_id={row['conversation_id']} | TITLE={row['title']}"]
    count=0
    for nid,node in c.get('mapping',{}).items():
        m=node.get('message')
        if not isinstance(m,dict):continue
        count+=1;role=(m.get('author') or {}).get('role','unknown')
        lines.append(f"\nMESSAGE node={nid} id={m.get('id')} parent={node.get('parent')} role={role}")
        content=m.get('content') or {};parts=content.get('parts',[])
        if isinstance(parts,list):
            for i,x in enumerate(parts):
                if isinstance(x,str):lines.append(f'PART {i}\n'+x)
                else:lines.append(f'PART {i} [non-text asset not opened]')
        if isinstance(content.get('text'),str):lines.append('TEXT\n'+content['text'])
    lines.append(f'\nEND CONVERSATION {row["id"]} ({count} messages; all mapping branches)')
    return '\n'.join(lines)

def prepare():
    OUT.mkdir(exist_ok=False);(OUT/'reviews').mkdir();(OUT/'reads').mkdir()
    db=sqlite3.connect('file:'+str(PRIOR/'catalog.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    rows=[dict(r) for r in db.execute("SELECT c.id,c.account,c.conversation_id,c.title,c.pointer,f.path,f.sha256 FROM classifications c JOIN files f ON f.id=c.file_id WHERE c.category='AMBIGUOUS' ORDER BY c.id")];db.close()
    assert len(rows)==1097
    cache={}
    for r in rows:
        text=render(r,cache);r['review_chars']=len(text);r['review_sha256']=hashlib.sha256(text.encode()).hexdigest()
    bins={k:[] for k in 'ABC'};sizes={k:0 for k in bins}
    for r in sorted(rows,key=lambda x:-x['review_chars']):
        k=min(sizes,key=sizes.get);bins[k].append(r);sizes[k]+=r['review_chars']
    for k in bins:
        bins[k].sort(key=lambda x:(x['review_chars'],x['id']))
        dump(OUT/f'shard_{k}.json',bins[k])
    dump(OUT/'INPUT_MANIFEST.json',{'rows':rows,'source_hashes':{r['path']:r['sha256'] for r in rows},'shard_chars':sizes,'title_nondev_accessed':False})
    print(json.dumps({'count':len(rows),'shard_chars':sizes,'shard_counts':{k:len(v) for k,v in bins.items()}}))

def read(shard,start,offset,budget,*,review_list=None):
    rows=json.loads((OUT/review_list if review_list else OUT/f'shard_{shard}.json').read_text());cache={};remaining=budget;items=[];i=start;off=offset
    while i<len(rows) and remaining>0:
        r=rows[i];text=render(r,cache)
        if hashlib.sha256(text.encode()).hexdigest()!=r['review_sha256']:raise ValueError('review representation drift')
        n=min(remaining,len(text)-off);end=off+n
        items.append({'id':r['id'],'index':i,'chars':len(text),'range':[off,end],'conversation_complete':end==len(text),'text':text[off:end]})
        remaining-=n
        if end==len(text):i+=1;off=0
        else:off=end;break
    page={'shard':shard,'start':[start,offset],'next':[i,off],'shard_complete':i==len(rows),'items':items}
    # Coverage means delivered to the agent; semantic completion is separately attested.
    ledger={'start':[start,offset],'next':[i,off],'ranges':[{'id':x['id'],'range':x['range']} for x in items]}
    (OUT/'reads'/f'{shard}.jsonl').open('a').write(json.dumps(ledger)+'\n')
    print(json.dumps(page,ensure_ascii=False))

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='op',required=True);sub.add_parser('prepare')
    q=sub.add_parser('read');q.add_argument('shard');q.add_argument('start',type=int);q.add_argument('--offset',type=int,default=0);q.add_argument('--chars',type=int,default=45000);q.add_argument('--review-list')
    a=p.parse_args()
    if a.op=='prepare':prepare()
    else:read(a.shard,a.start,a.offset,a.chars,review_list=a.review_list)

if __name__=='__main__':main()
