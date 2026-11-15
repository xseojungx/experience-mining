"""Merge explicit agent judgments and build a new filtered search snapshot.

No semantic classifier lives here. Missing reading/second review cannot exclude
a conversation. Existing title history, originals and prior index are read-only.
"""
import collections
import hashlib
import json
import sqlite3

from chatgpt_body_review import OUT, PRIOR, dump


def latest(path):
    result={}
    if path.exists():
        for line in path.read_text().splitlines():
            r=json.loads(line)
            if r['decision'] not in ('DEV','NON_DEV','HOLD'):raise ValueError('invalid verdict')
            result[r['id']]=r
    return result

def coverage(ledger):
    ranges=collections.defaultdict(list)
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            for r in json.loads(line)['ranges']:ranges[r['id']].append(r['range'])
    return ranges

def full(ranges,n):
    end=0
    for a,b in sorted(ranges):
        if a>end:return False
        end=max(end,b)
    return end==n

def decision_digest(row):
    return hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def user_resolutions(rows,path):
    """Explicit human classifications override HOLD; do not invent read coverage.

    Original agent verdicts and missing-primary facts remain intact. A changed
    underlying review must be reconciled instead of reusing an old confirmation.
    """
    confirmed=latest(path);by_id={r['id']:r for r in rows}
    for ident,record in confirmed.items():
        row=by_id.get(ident)
        if (row is None or row['body_decision']!='HOLD'
                or record.get('basis')!='user_confirmation' or record.get('reviewer')!='user'
                or record['decision'] not in ('DEV','NON_DEV') or not record.get('source')
                or record.get('base_review_sha256')!=decision_digest(row)):
            raise ValueError('invalid or stale user classification: '+ident)
        row['agent_body_decision']=row['body_decision']
        row['agent_reason']=row['reason']
        row['body_decision']=record['decision']
        row['reason']='explicit user classification; original reading/agent history preserved'
        row['resolution_basis']='user_confirmation'
        row['user_resolution']=record
    return len(confirmed)

def apply_recheck(rows,path):
    """Apply a separate user recheck ledger over final NON_DEV rows.

    Each row must name an existing conversation by id and identical raw location
    (path/pointer/sha256), and its target must currently be NON_DEV. Earlier
    agent/user history stays on the row; the recheck record is added beside it.
    """
    by_id={r['id']:r for r in rows};seen=set();changed=[];confirmed=[]
    for line in path.read_text().splitlines():
        record=json.loads(line);ident=record.get('id');row=by_id.get(ident);loc=record.get('raw_location') or {}
        if (row is None or ident in seen or record.get('basis')!='user_confirmation' or record.get('reviewer')!='user'
                or record.get('decision') not in ('DEV','NON_DEV') or not record.get('source')
                or record.get('previous_final_decision')!='NON_DEV' or row['body_decision']!='NON_DEV'
                or record.get('changed')!=(record['decision']!='NON_DEV')
                or (loc.get('path'),loc.get('pointer'),loc.get('sha256'))!=(row.get('path'),row.get('pointer'),row.get('sha256'))):
            raise ValueError('invalid or stale recheck classification: '+str(ident))
        seen.add(ident)
        if record['changed']:
            row['pre_recheck_body_decision']=row['body_decision'];row['pre_recheck_reason']=row['reason']
            row['body_decision']=record['decision']
            row['reason']='explicit user recheck classification; earlier agent/user history preserved'
            row['final_basis']='user_recheck_confirmation';row['recheck_resolution']=record;changed.append(ident)
        else:
            row['recheck_confirmation']=record;confirmed.append(ident)
    return {'changed_ids':changed,'confirmed_unchanged_ids':confirmed}

def merge(require_complete=True,*,allow_user_resolved_unread=False,progress_dir=None):
    """allow_user_resolved_unread (opt-in): unread primary items count as
    classification-complete only when an explicit user classification resolved
    them; missing_primary_ids stays in the result. progress_dir redirects the
    PROGRESS/BODY_DECISIONS writes away from the original review directory."""
    progress_dir=progress_dir or OUT
    original={};first={};missing=[]
    for shard in 'ABC':
        rows=json.loads((OUT/f'shard_{shard}.json').read_text());reviews=latest(OUT/'reviews'/f'{shard}.jsonl');ranges=coverage(OUT/'reads'/f'{shard}.jsonl')
        for r in rows:
            original[r['id']]=r
            verdict=reviews.get(r['id'])
            if not verdict or not verdict.get('full_text_read') or not full(ranges[r['id']],r['review_chars']):missing.append(r['id'])
            else:first[r['id']]=verdict
    checks={}
    for path in sorted((OUT/'reviews').glob('*checks*.jsonl')):
        for ident,review in latest(path).items():
            if not review.get('independent_recheck'):continue
            ledger=OUT/'reads'/(review['read_ledger']+'.jsonl')
            if not review.get('full_text_read') or ident not in original or not full(coverage(ledger)[ident],original[ident]['review_chars']):continue
            checks.setdefault(ident,[]).append(review)
    final=[];needs=[]
    for ident,r in original.items():
        one=first.get(ident);status='HOLD';reason='primary full reading incomplete';independent=[]
        if one:
            status=one['decision'];reason=one['reason']
            independent=[v for v in checks.get(ident,[]) if v['reviewer']!=one['reviewer']]
            if status=='NON_DEV':
                if not independent:status='HOLD';reason='independent NON_DEV full reread pending';needs.append(ident)
                elif any(x['decision']=='DEV' for x in independent):status='HOLD';reason='independent review identified development context; disagreement retained for review'
                elif any(x['decision']=='HOLD' for x in independent):status='HOLD';reason='independent review left uncertain development relevance'
        final.append({**r,'title_category':'AMBIGUOUS','body_decision':status,'body_rule':'body-semantic-v1','reason':reason,'primary':one,'independent_checks':independent})
    user_count=user_resolutions(final,OUT/'reviews'/'user_decisions.jsonl')
    result={'target':len(original),'primary_complete':len(first),'missing_primary_ids':missing,'pending_nondev_recheck_ids':needs,
            'counts':dict(collections.Counter(r['body_decision'] for r in final)),
            'primary_counts':dict(collections.Counter(r['decision'] for r in first.values())),
            'user_resolution_count':user_count,
            'unresolved_classification_ids':[r['id'] for r in final if r['body_decision']=='HOLD']}
    blocking=missing
    if allow_user_resolved_unread:
        resolved={r['id'] for r in final if r.get('resolution_basis')=='user_confirmation' and r['body_decision'] in ('DEV','NON_DEV')}
        blocking=[i for i in missing if i not in resolved]
        result['user_resolved_unread_ids']=[i for i in missing if i in resolved]
    dump(progress_dir/'PROGRESS.json',result)
    if require_complete and (blocking or needs):raise ValueError('incomplete primary or independent reviews')
    if require_complete:dump(progress_dir/'BODY_DECISIONS.json',final)
    return result,final

def build_index(decisions,out_dir=None,extra_meta=None):
    out_dir=out_dir or OUT
    path=out_dir/'search.sqlite3'
    if path.exists():raise ValueError('filtered index already exists; do not overwrite')
    old=sqlite3.connect('file:'+str(PRIOR/'search.sqlite3')+'?mode=ro',uri=True);old.execute('PRAGMA query_only=ON')
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;old.backup(db);old.close()
    excluded=[r['id'] for r in decisions if r['body_decision']=='NON_DEV']
    before={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['conversations','messages','segments','links']}
    try:
        db.execute('PRAGMA foreign_keys=ON')
        with db:
            db.execute('CREATE TEMP TABLE excluded_classifications(id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO excluded_classifications VALUES(?)',[(x,) for x in excluded])
            db.execute('CREATE TEMP TABLE excluded_conversations AS SELECT DISTINCT conversation AS id FROM conversation_locations WHERE classification_id IN (SELECT id FROM excluded_classifications)')
            db.execute('CREATE TEMP TABLE excluded_messages AS SELECT id FROM messages WHERE conversation IN (SELECT id FROM excluded_conversations)')
            db.execute('CREATE TEMP TABLE excluded_segments AS SELECT id FROM segments WHERE message IN (SELECT id FROM excluded_messages)')
            db.execute('DELETE FROM links WHERE segment IN (SELECT id FROM excluded_segments)')
            db.execute("DELETE FROM search_runs WHERE direction='chatgpt_to_github' AND source_id IN (SELECT id FROM excluded_segments)")
            for side in ('left_id','right_id'):
                db.execute('DELETE FROM relations WHERE '+side+' IN (SELECT id FROM excluded_messages UNION SELECT id FROM excluded_conversations UNION SELECT id FROM excluded_segments)')
            db.execute('DELETE FROM assets WHERE message IN (SELECT id FROM excluded_messages)')
            db.execute('DELETE FROM segments WHERE id IN (SELECT id FROM excluded_segments)')
            db.execute('DELETE FROM nodes WHERE conversation IN (SELECT id FROM excluded_conversations)')
            db.execute('DELETE FROM messages WHERE id IN (SELECT id FROM excluded_messages)')
            db.execute('DELETE FROM conversation_locations WHERE conversation IN (SELECT id FROM excluded_conversations)')
            db.execute('DELETE FROM conversations WHERE id IN (SELECT id FROM excluded_conversations)')
            for table in ['message_fts','substring_fts']:db.execute('INSERT INTO '+table+'('+table+") VALUES('delete-all')")
            for r in db.execute('SELECT * FROM searchable_segments').fetchall():
                ids=' '.join(['chatgpt',r['account'],r['conversation_id'],r['message_id']]+json.loads(r['project_hints']))
                db.execute('INSERT INTO message_fts(rowid,text,identifiers) VALUES(?,?,?)',(r['rowid'],r['text'],ids))
                db.execute('INSERT INTO substring_fts(rowid,text) VALUES(?,?)',(r['rowid'],r['text']))
            db.execute('CREATE TABLE body_review_decisions(classification_id TEXT PRIMARY KEY,decision TEXT NOT NULL,rule TEXT NOT NULL,record TEXT NOT NULL)')
            db.executemany('INSERT INTO body_review_decisions VALUES(?,?,?,?)',[(r['id'],r['body_decision'],r['body_rule'],json.dumps(r,ensure_ascii=False)) for r in decisions])
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('body_review','CG-BODY-20261115:body-semantic-v1'))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('link_history_caveat','Retained prior candidate matches filtered by body decisions; no fresh bidirectional search or attribution verification. Historical hit_count/scope describe original search corpus.'))
            for key,value in (extra_meta or {}).items():db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,value))
            if db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('FK failure')
        # Deleted rows can survive in free pages of the backup copy; rewrite the file.
        db.execute('VACUUM')
        after={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in before}
        freelist=db.execute('PRAGMA freelist_count').fetchone()[0]
        nondev=db.execute("SELECT count(*) FROM conversation_locations l JOIN body_review_decisions d ON d.classification_id=l.classification_id WHERE d.decision='NON_DEV'").fetchone()[0]
        fts_counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['message_fts','substring_fts']}
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        if nondev or freelist or integrity!='ok' or any(n!=after['segments'] for n in fts_counts.values()):raise ValueError('filtered index invariant failed')
        result={'before':before,'after':after,'confirmed_nondev':len(excluded),'nondev_conversations_in_new_search':nondev,'fts_counts':fts_counts,'integrity':integrity,'foreign_key_errors':0,'freelist_pages_after_vacuum':freelist,'source_index_modified':False,'links':'filtered historical candidates; not re-searched'}
        dump(out_dir/'INDEX_RESULT.json',result);return result
    finally:db.close()

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--progress',action='store_true');p.add_argument('--build-index',action='store_true');a=p.parse_args()
    result,decisions=merge(require_complete=not a.progress);print(json.dumps(result,ensure_ascii=False))
    if a.build_index:print(json.dumps(build_index(decisions),ensure_ascii=False))
