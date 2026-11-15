import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]/'tools'
sys.path.insert(0,str(TOOLS))
import finalize_chatgpt_body_review as review
from chatgpt_body_review import sha


class BodyReviewTest(unittest.TestCase):
    def test_coverage_requires_no_gap_and_complete_end(self):
        self.assertTrue(review.full([[0,5],[3,10]],10))
        self.assertFalse(review.full([[0,5],[6,10]],10))
        self.assertFalse(review.full([[0,9]],10))

    def test_corrections_preserve_last_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'reviews.jsonl';p.write_text('\n'.join(json.dumps({'id':'x','decision':v}) for v in ['DEV','NON_DEV','HOLD']))
            self.assertEqual(review.latest(p)['x']['decision'],'HOLD')

    def fixture(self,out):
        (out/'reviews').mkdir();(out/'reads').mkdir()
        for k in 'ABC':
            (out/f'shard_{k}.json').write_text(json.dumps([{'id':k,'review_chars':10}]));
            (out/'reviews'/f'{k}.jsonl').write_text(json.dumps({'id':k,'decision':'NON_DEV','full_text_read':True,'reason':'synthetic','reviewer':k})+'\n')
            (out/'reads'/f'{k}.jsonl').write_text(json.dumps({'ranges':[{'id':k,'range':[0,10]}]})+'\n')

    def test_nondev_requires_different_complete_reviewer_and_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out)
            checks=[{'id':i,'decision':d,'reviewer':r,'full_text_read':True,'independent_recheck':True,'read_ledger':'checker'} for i,d,r in [('A','NON_DEV','other'),('B','DEV','other'),('C','NON_DEV','C')]]
            (out/'reviews/root_checks.jsonl').write_text('\n'.join(json.dumps(x) for x in checks))
            (out/'reads/checker.jsonl').write_text(json.dumps({'ranges':[{'id':i,'range':[0,10]} for i in 'ABC']}))
            with patch.object(review,'OUT',out):
                result,final=review.merge(require_complete=False)
                self.assertEqual([x['body_decision'] for x in final],['NON_DEV','HOLD','HOLD'])
                self.assertEqual(result['pending_nondev_recheck_ids'],['C'])
                with self.assertRaises(ValueError):review.merge()

    def test_missing_primary_never_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out);(out/'reads/A.jsonl').write_text('')
            with patch.object(review,'OUT',out):
                result,rows=review.merge(require_complete=False)
                self.assertEqual(result['primary_complete'],2);self.assertEqual(rows[0]['body_decision'],'HOLD')

    def test_user_resolves_hold_without_faking_full_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'user_decisions.jsonl'
            original={'id':'hold','body_decision':'HOLD','reason':'incomplete reading','primary':None,'independent_checks':[]}
            record={'id':'hold','decision':'NON_DEV','reviewer':'user','basis':'user_confirmation','source':'synthetic explicit user instruction','base_review_sha256':review.decision_digest(original)}
            path.write_text(json.dumps(record)+'\n')
            rows=[dict(original)];self.assertEqual(review.user_resolutions(rows,path),1)
            self.assertEqual(rows[0]['body_decision'],'NON_DEV');self.assertIsNone(rows[0]['primary'])
            self.assertNotIn('full_text_read',rows[0]);self.assertEqual(rows[0]['agent_body_decision'],'HOLD')
            changed=dict(original,reason='different underlying review')
            with self.assertRaises(ValueError):review.user_resolutions([changed],path)
            with self.assertRaises(ValueError):review.user_resolutions([dict(original,body_decision='DEV')],path)

    def test_user_resolution_keeps_missing_primary_and_finalization_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out);(out/'reads/A.jsonl').write_text('')
            with patch.object(review,'OUT',out),patch.object(review,'dump',lambda *a,**kw:None):
                _,rows=review.merge(require_complete=False)
                confirmations=[{'id':r['id'],'decision':'DEV' if r['id']=='A' else 'NON_DEV','reviewer':'user','basis':'user_confirmation','source':'synthetic approval','base_review_sha256':review.decision_digest(r)} for r in rows]
                (out/'reviews/user_decisions.jsonl').write_text('\n'.join(json.dumps(r) for r in confirmations))
                result,resolved=review.merge(require_complete=False)
                self.assertEqual(result['counts'],{'DEV':1,'NON_DEV':2})
                self.assertEqual(result['missing_primary_ids'],['A']);self.assertEqual(result['unresolved_classification_ids'],[])
                self.assertIsNone(resolved[0]['primary'])
                with self.assertRaises(ValueError):review.merge(require_complete=True)

    def test_filtered_index_rebuilds_fts_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prior=root/'prior';out=root/'out';prior.mkdir();out.mkdir();p=prior/'search.sqlite3'
            db=sqlite3.connect(p);db.executescript((TOOLS/'chatgpt_pipeline/search_schema.sql').read_text())
            for i in ['dev','nondev']:
                db.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)',(i,'chatgpt','account',i,'rev','AMBIGUOUS','time','time','node','processed'))
                db.execute('INSERT INTO conversation_locations VALUES(?,?,?,?,?,?)',(i,i,'batch','source','hash','/0'))
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,i,'node','chatgpt','account',i,i,'rev','provider','user','time','hash','/mapping/node/message','user_statement_unverified','indexed'))
                db.execute('INSERT INTO segments(id,message,part_pointer,start,end,text,text_sha256,rule,reason,redacted,semantic_kind,analysis_unit,project_hints) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,i,'/content/parts/0',0,10,i+' searchable','hash','rule','synthetic',0,'user_statement_unverified',i,'[]'))
            db.commit();db.close();before=sha(p)
            ds=[{'id':i,'body_decision':'NON_DEV' if i=='nondev' else 'DEV','body_rule':'test'} for i in ['dev','nondev']]
            with patch.object(review,'OUT',out),patch.object(review,'PRIOR',prior):r=review.build_index(ds)
            self.assertEqual(r['after']['conversations'],1);self.assertEqual(r['nondev_conversations_in_new_search'],0);self.assertEqual(sha(p),before)
            c=sqlite3.connect(out/'search.sqlite3');self.assertEqual(c.execute("select count(*) from message_fts where message_fts match 'nondev'").fetchone()[0],0);self.assertEqual(c.execute("select count(*) from message_fts where message_fts match 'dev'").fetchone()[0],1);c.close()

def synthetic_prior(prior):
    p=prior/'search.sqlite3';db=sqlite3.connect(p);db.executescript((TOOLS/'chatgpt_pipeline/search_schema.sql').read_text())
    db.execute('INSERT INTO github_docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('gh',1,'repo','proj',1,'pr','k','path','hash','/0','obj','author','time','gh text'))
    for i in ['dev','nondev']:
        db.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?)',(i,'chatgpt','account',i,'rev','AMBIGUOUS','time','time','node','processed'))
        db.execute('INSERT INTO conversation_locations VALUES(?,?,?,?,?,?)',(i,i,'batch','source','hash','/0'))
        db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?)',(i,'node',None,'[]','[]',i))
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,i,'node','chatgpt','account',i,i,'rev','provider','user','time','hash','/mapping/node/message','user_statement_unverified','indexed'))
        db.execute('INSERT INTO segments(id,message,part_pointer,start,end,text,text_sha256,rule,reason,redacted,semantic_kind,analysis_unit,project_hints) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,i,'/content/parts/0',0,10,i+' searchable','hash','rule','synthetic',0,'user_statement_unverified',i,'[]'))
        db.execute('INSERT INTO search_runs VALUES(?,?,?,?,?,?,?,?,?)',('run-'+i,'chatgpt_to_github',i,'[]','{}','candidate','synthetic','rule',1))
        db.execute('INSERT INTO links VALUES(?,?,?,?,?,?,?,?,?)',('link-'+i,'run-'+i,i,'gh','chatgpt_to_github','[]','candidate','synthetic','pending'))
    rowid=db.execute("select rowid from segments where id='dev'").fetchone()[0]
    db.execute("INSERT INTO message_fts(rowid,text,identifiers) VALUES(?,?,?)",(rowid,'dev searchable','x'))
    db.commit();db.close();return p

class IndexSyncTest(unittest.TestCase):
    def fixture(self,out,decisions=('NON_DEV','DEV','DEV')):
        (out/'reviews').mkdir();(out/'reads').mkdir()
        for k,d in zip('ABC',decisions):
            (out/f'shard_{k}.json').write_text(json.dumps([{'id':k,'review_chars':10,'path':'raw/'+k,'pointer':'/0','sha256':'h'+k}]))
            (out/'reviews'/f'{k}.jsonl').write_text(json.dumps({'id':k,'decision':d,'full_text_read':True,'reason':'synthetic','reviewer':k})+'\n')
            (out/'reads'/f'{k}.jsonl').write_text(json.dumps({'ranges':[{'id':k,'range':[0,10]}]})+'\n')

    def resolve(self,out,ids,decision='NON_DEV'):
        with patch.object(review,'OUT',out),patch.object(review,'dump',lambda *a,**kw:None):
            _,rows=review.merge(require_complete=False)
        recs=[{'id':r['id'],'decision':decision,'reviewer':'user','basis':'user_confirmation','source':'synthetic approval','base_review_sha256':review.decision_digest(r)} for r in rows if r['id'] in ids]
        (out/'reviews/user_decisions.jsonl').write_text('\n'.join(json.dumps(r) for r in recs))

    def test_user_resolved_unread_allowed_only_with_opt_in_and_kept_in_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';out.mkdir();progress=Path(tmp)/'progress';progress.mkdir()
            self.fixture(out);(out/'reads/A.jsonl').write_text('');self.resolve(out,{'A'})
            with patch.object(review,'OUT',out):
                with self.assertRaises(ValueError):review.merge(require_complete=True,progress_dir=progress)
                result,rows=review.merge(require_complete=True,allow_user_resolved_unread=True,progress_dir=progress)
            self.assertEqual(result['missing_primary_ids'],['A']);self.assertEqual(result['user_resolved_unread_ids'],['A'])
            self.assertEqual(rows[0]['body_decision'],'NON_DEV');self.assertIsNone(rows[0]['primary'])
            self.assertTrue((progress/'BODY_DECISIONS.json').exists());self.assertFalse((out/'PROGRESS.json').exists())

    def test_opt_in_still_blocks_unresolved_unread_and_pending_independent_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';out.mkdir();progress=Path(tmp)/'p';progress.mkdir()
            self.fixture(out);(out/'reads/A.jsonl').write_text('')
            with patch.object(review,'OUT',out):
                with self.assertRaises(ValueError):review.merge(require_complete=True,allow_user_resolved_unread=True,progress_dir=progress)
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';out.mkdir();progress=Path(tmp)/'p';progress.mkdir()
            self.fixture(out,('NON_DEV','NON_DEV','DEV'));(out/'reads/A.jsonl').write_text('');self.resolve(out,{'A'})
            with patch.object(review,'OUT',out):
                with self.assertRaises(ValueError):review.merge(require_complete=True,allow_user_resolved_unread=True,progress_dir=progress)
                result,_=review.merge(require_complete=False,allow_user_resolved_unread=True,progress_dir=progress)
            self.assertEqual(result['pending_nondev_recheck_ids'],['B'])

    def recheck_rows(self):
        return [{'id':i,'body_decision':d,'reason':'agent','path':'raw/'+i,'pointer':'/0','sha256':'h'+i,'primary':{'decision':d}} for i,d in [('x','NON_DEV'),('y','NON_DEV'),('z','DEV')]]

    def recheck_record(self,i,decision,**kw):
        r={'id':i,'decision':decision,'previous_final_decision':'NON_DEV','changed':decision!='NON_DEV','basis':'user_confirmation','reviewer':'user','source':'synthetic chat','raw_location':{'path':'raw/'+i,'pointer':'/0','sha256':'h'+i}}
        r.update(kw);return r

    def test_apply_recheck_changes_only_flagged_rows_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'recheck.jsonl';p.write_text('\n'.join(json.dumps(r) for r in [self.recheck_record('x','DEV'),self.recheck_record('y','NON_DEV')]))
            rows=self.recheck_rows();summary=review.apply_recheck(rows,p)
            self.assertEqual(summary,{'changed_ids':['x'],'confirmed_unchanged_ids':['y']})
            x,y,z=rows
            self.assertEqual((x['body_decision'],x['pre_recheck_body_decision'],x['pre_recheck_reason'],x['primary']['decision']),('DEV','NON_DEV','agent','NON_DEV'))
            self.assertEqual(x['final_basis'],'user_recheck_confirmation');self.assertEqual(y['body_decision'],'NON_DEV');self.assertIn('recheck_confirmation',y)
            self.assertEqual(z['body_decision'],'DEV');self.assertNotIn('recheck_confirmation',z)

    def test_apply_recheck_rejects_mismatch(self):
        bad=[[self.recheck_record('x','DEV',raw_location={'path':'raw/x','pointer':'/1','sha256':'hx'})],
             [self.recheck_record('z','DEV')],
             [self.recheck_record('x','DEV',changed=False)],
             [self.recheck_record('x','DEV'),self.recheck_record('x','DEV')],
             [self.recheck_record('missing','DEV')],
             [self.recheck_record('x','DEV',reviewer='agent')],
             [self.recheck_record('x','DEV',previous_final_decision='HOLD')]]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'recheck.jsonl'
            for records in bad:
                p.write_text('\n'.join(json.dumps(r) for r in records))
                with self.assertRaises(ValueError):review.apply_recheck(self.recheck_rows(),p)

    def test_build_index_to_new_dir_filters_links_vacuums_and_verifies(self):
        import sync_chatgpt_index as sync
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prior=root/'prior';old_out=root/'old';new=root/'new'
            for d in (prior,old_out,new):d.mkdir()
            p=synthetic_prior(prior);before=sha(p);old_stats=sync.per_classification_stats(p)
            ds=[{'id':'dev','body_decision':'DEV','body_rule':'t'},{'id':'nondev','body_decision':'NON_DEV','body_rule':'t'}]
            with patch.object(review,'OUT',old_out),patch.object(review,'PRIOR',prior):
                r=review.build_index(ds,out_dir=new,extra_meta={'link_status':'historical; re-verification required'})
            self.assertEqual(list(old_out.iterdir()),[]);self.assertEqual(sha(p),before)
            self.assertEqual(r['after'],{'conversations':1,'messages':1,'segments':1,'links':1});self.assertEqual(r['freelist_pages_after_vacuum'],0)
            v=sync.verify_new(new/'search.sqlite3',p,['dev'],['nondev'],old_stats)
            self.assertTrue(all(n==0 for n in v['nondev_rows_remaining'].values()))
            self.assertEqual(v['old_rows_for_excluded_nondev'],{'conversations':1,'messages':1,'segments':1,'links':1})
            self.assertTrue(v['new_classification_ids_equal_dev_set']);self.assertEqual(v['dev_conversations_with_count_mismatch'],[])
            self.assertEqual(v['checks']['integrity_check'],'ok');self.assertEqual(v['checks']['foreign_key_errors'],0)
            self.assertTrue(all(v['checks']['fts_rowids_equal_segments'].values()))
            self.assertEqual(v['checks']['meta']['link_status'],'historical; re-verification required')
            with self.assertRaises(ValueError),patch.object(review,'PRIOR',prior):review.build_index(ds,out_dir=new)

    def test_final_basis_labels(self):
        import sync_chatgpt_index as sync
        self.assertEqual(sync.final_basis({'final_basis':'user_recheck_confirmation','resolution_basis':'user_confirmation'}),'user_recheck_confirmation')
        self.assertEqual(sync.final_basis({'resolution_basis':'user_confirmation'}),'user_confirmation_hold_resolution')
        self.assertEqual(sync.final_basis({'independent_checks':[{}]}),'agent_body_review+independent_recheck')

if __name__=='__main__':unittest.main()
