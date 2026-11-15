"""Synthetic only: no personal exports, operational DB or network calls."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.chatgpt_pipeline import core, github, report


def message(mid,text,role='user',timestamp=1735689600):
    return {'id':mid,'author':{'role':role},'create_time':timestamp,
            'content':{'content_type':'text','parts':[text]}}


def conversation(cid,title,text):
    return {'id':cid,'conversation_id':cid,'title':title,'create_time':1735689600,
       'mapping':{'root':{'parent':None,'message':None},
                  'u':{'parent':'root','message':message('u1',text)},
                  'a':{'parent':'u','message':message('a1','React 코드: useState를 제안합니다.','assistant')},
                  'b':{'parent':'u','message':message('b1','별도 답변','assistant')}},'current_node':'a'}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.out=self.root/'derived';self.out.mkdir()
        self.raw=self.root/'data/raw/chatgpt/physical_one';self.raw.mkdir(parents=True)
        self.c1=conversation('same-id','React 상태 오류','React useState 코드 수정\n\n오늘 점심 메뉴는 김치찌개')
        self.n=conversation('private-id','제주 여행 일정','PRIVATE_NON_DEV_SENTINEL React secret')
        self.a=conversation('amb-id','문제 해결','TypeError: syntheticFailure\n\n일상적인 인사입니다.')
        self.write([self.c1,self.n,self.a]);core.initialize(self.out)

    def tearDown(self):self.tmp.cleanup()

    def write(self,values,name='conversations-000.json',folder=None):
        p=(folder or self.raw)/name;p.write_text(json.dumps(values,ensure_ascii=False));return p

    def passes(self):
        files=core.inventory(self.root,self.out)
        self.assertEqual(core.titles(self.root,self.out,files),[])
        self.assertEqual(core.extract(self.root,self.out,files),[])
        return files

    def db(self):return core.connect(self.out/'search.sqlite3')

    def test_conservative_title_rules(self):
        for title in ['프로젝트 계획','ON','새 대화','분석과 피드백','에러 해결','만드는 방법','일정 관리']:
            self.assertEqual(core.classify_title(title)[0],'AMBIGUOUS')
        self.assertEqual(core.classify_title('React 오류')[0],'DEV')
        self.assertEqual(core.classify_title('제주 여행 일정')[0],'NON_DEV')
        self.assertEqual(core.classify_title('여행 일정 API 구현')[0],'AMBIGUOUS')

    def test_title_gate_never_accesses_mapping(self):
        class Guard(dict):
            def get(self,k,*a):
                if k=='mapping':raise AssertionError('NON_DEV body accessed')
                return super().get(k,*a)
        self.write([self.n]);files=core.inventory(self.root,self.out)
        with patch.object(core.json,'loads',return_value=[Guard(self.n)]):
            self.assertEqual(core.titles(self.root,self.out,files),[])
            self.assertEqual(core.extract(self.root,self.out,files),[])
        with self.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_title_result_independent_of_body(self):
        files=core.inventory(self.root,self.out);core.titles(self.root,self.out,files)
        with core.connect(self.out/'catalog.sqlite3') as db:
            rows=list(db.execute('SELECT title,category FROM classifications'))
            self.assertEqual({x[0]:x[1] for x in rows},{'React 状態 오류'.replace('状態','상태'):'DEV','제주 여행 일정':'NON_DEV','문제 해결':'AMBIGUOUS'})

    def test_nondev_absent_from_search_and_mixed_text_held(self):
        self.passes()
        with self.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM conversations WHERE conversation_id='private-id'").fetchone())
            self.assertEqual(core.search(db,'PRIVATE_NON_DEV_SENTINEL'),[])
            self.assertEqual(core.search(db,'김치찌개'),[])
            self.assertTrue(core.search(db,'React'))

    def test_branch_nodes_preserved(self):
        self.passes()
        with self.db() as db:
            conv=db.execute("SELECT id FROM conversations WHERE conversation_id='same-id'").fetchone()[0]
            node=db.execute("SELECT * FROM nodes WHERE conversation=? AND node_id='u'",(conv,)).fetchone()
            self.assertEqual(json.loads(node['derived_children']),['a','b'])
            self.assertEqual(db.execute('SELECT count(*) FROM nodes WHERE conversation=?',(conv,)).fetchone()[0],4)

    def test_parent_orphan_retained(self):
        self.c1['mapping']['u']['parent']='missing';self.write([self.c1]);files=self.passes()
        self.assertEqual(report.validate(self.root,self.out,files)['orphan_parent_refs_preserved'],1)

    def test_identity_account_separation(self):
        other=self.raw.parent/'physical_two';other.mkdir();self.write([self.c1],folder=other);self.passes()
        with self.db() as db:
            rows=db.execute("SELECT id,account,conversation_id FROM conversations WHERE conversation_id='same-id'").fetchall()
            self.assertEqual(len(rows),2);self.assertNotEqual(rows[0]['id'],rows[1]['id']);self.assertNotEqual(rows[0]['account'],rows[1]['account'])

    def test_idempotent_and_multifile(self):
        self.write([conversation('split','Python 함수','def synthetic():\n return 1')],name='conversations-001.json')
        files=self.passes();before=report.summary(self.out)['counts'];self.passes()
        self.assertEqual(before,report.summary(self.out)['counts']);self.assertEqual(len(files),2)

    def test_partial_failure_rollback_and_resume(self):
        files=core.inventory(self.root,self.out);core.titles(self.root,self.out,files)
        self.assertTrue(core.extract(self.root,self.out,files,fail_after=0))
        with self.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM conversations').fetchone()[0],0)
        with core.connect(self.out/'catalog.sqlite3') as db:self.assertEqual(db.execute("SELECT count(*) FROM classifications WHERE status='processed'").fetchone()[0],0)
        self.assertEqual(core.extract(self.root,self.out,files),[])
        self.assertGreater(report.summary(self.out)['counts']['segments'],0)

    def test_changed_input_is_rejected_before_body(self):
        files=core.inventory(self.root,self.out);core.titles(self.root,self.out,files);self.write([self.n])
        self.assertTrue(core.extract(self.root,self.out,files))
        self.assertEqual(report.summary(self.out)['counts']['messages'],0)

    def test_bad_split_does_not_erase_successful_split(self):
        (self.raw/'conversations-001.json').write_text('invalid JSON')
        files=core.inventory(self.root,self.out);self.assertEqual(len(core.titles(self.root,self.out,files)),1)
        self.assertEqual(core.extract(self.root,self.out,files),[])
        self.assertGreater(report.summary(self.out)['counts']['segments'],0)

    def test_summaries_and_assets_never_parsed(self):
        (self.raw/'conversation_summaries.json').write_text('not JSON, body poison')
        (self.raw/'file-secret.dat').write_bytes(b'private attachment')
        self.c1['mapping']['u']['message']['metadata']={'attachments':[{'id':'asset-1','name':'PRIVATE_NAME'}]};self.write([self.c1])
        self.passes()
        with self.db() as db:
            rows=db.execute('SELECT * FROM assets').fetchall();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['body_opened'],0)
            self.assertEqual(core.search(db,'PRIVATE_NAME'),[])

    def test_revision_and_duplicate_locations_retained(self):
        self.passes();self.write([self.c1],name='conversations-001.json');self.passes()
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM conversations WHERE conversation_id='same-id'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_locations l JOIN conversations c ON c.id=l.conversation WHERE c.conversation_id='same-id'").fetchone()[0],3)
        self.c1['mapping']['u']['message']['content']['parts'][0]='React newRevision() 코드';self.write([self.c1]);self.passes()
        with self.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM conversations WHERE conversation_id='same-id'").fetchone()[0],2)

    def test_missing_provider_id_marked_surrogate(self):
        self.c1.pop('id');self.c1.pop('conversation_id');self.c1['mapping']['u']['message'].pop('id');self.write([self.c1]);self.passes()
        with self.db() as db:self.assertEqual(db.execute("SELECT id_kind FROM messages WHERE node_id='u'").fetchone()[0],'node_surrogate')

    def test_exact_trace_and_json_pointer_escaping(self):
        self.c1['mapping']['u/~']=self.c1['mapping'].pop('u');self.write([self.c1]);self.passes()
        with self.db() as db:
            row=core.search(db,'useState')[0];t=core.trace(self.root,db,row['id'])
            self.assertTrue(t['matches']);self.assertIn('~1~0',t['pointer'])

    def test_substring_and_filters(self):
        self.c1['mapping']['u']['message']['content']['parts'][0]='React 바텀시트제스처 오류 influy';self.write([self.c1]);self.passes()
        with self.db() as db:
            rows=core.search(db,'텀시트',substring=True);self.assertTrue(rows)
            self.assertTrue(core.search(db,'React',account=rows[0]['account'],conversation_id='same-id',message_id='u1',project='influy',since='2024-01',until='2026-01'))
            self.assertEqual(core.search(db,'React',since='2030-01'),[])
            with self.assertRaises(ValueError):core.search(db,'ab',substring=True)

    def test_query_escaping(self):
        self.passes()
        with self.db() as db:
            self.assertEqual(core.search(db,'React" OR *'),[])

    def test_redaction_and_proposal_semantics(self):
        self.write([conversation('privacy','React API','React API key: sk-abcdefghijklmnopqrstuvwxyz email@example.com 010-1234-5678 /Users/synthetic/code')]);self.passes()
        with self.db() as db:
            text=' '.join(x[0] for x in db.execute('SELECT text FROM segments'))
            for private in ['sk-abcdefgh','email@example.com','010-1234-5678','/Users/synthetic']:self.assertNotIn(private,text)
            self.assertTrue(db.execute("SELECT 1 FROM segments WHERE semantic_kind='ai_proposal_unverified'").fetchone())

    def test_no_body_without_title_pass(self):
        files=core.inventory(self.root,self.out)
        with self.assertRaises(ValueError):core.extract(self.root,self.out,files)

    def test_read_only_connection(self):
        self.passes()
        with core.connect(self.out/'search.sqlite3',readonly=True) as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM segments')

    def test_unsafe_output_and_input_paths(self):
        with self.assertRaises(ValueError):core.connect(core.ROOT/'state/state.sqlite3')
        with self.assertRaises(ValueError):core.safe(self.root,'../escape')
        (self.root/'alias').symlink_to(self.raw)
        with self.assertRaises(ValueError):core.safe(self.root,'alias/file')

    def test_unapproved_account_root_is_rejected_before_hashing(self):
        other=self.raw.parent/'unapproved';other.mkdir();self.write([self.n],folder=other)
        with self.assertRaises(ValueError):core.inventory(self.root,self.out,allowed_accounts=[str(self.raw.relative_to(self.root))])
        with core.connect(self.out/'catalog.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],0)

    def test_schema_drift_rejected(self):
        with self.db() as db:db.execute("UPDATE meta SET value='bad' WHERE key='schema'")
        with self.assertRaises(ValueError):core.initialize(self.out)

    def test_source_hash_trace_drift(self):
        self.passes()
        with self.db() as db:
            row=core.search(db,'React')[0];self.write([self.n])
            with self.assertRaises(ValueError):core.trace(self.root,db,row['id'])

    def test_c01_projection_uses_existing_validator(self):
        self.passes();r=report.export_compatibility(self.out)
        self.assertEqual(r['c01_diagnostics'],[]);self.assertGreater(r['c01_valid_evidence'],0)
        self.assertFalse(r['operational_import_performed']);self.assertEqual(r['candidate_state_updates'],0)

    def test_projection_replay_is_stable(self):
        self.passes();report.export_compatibility(self.out);before=core.file_hash(self.out/'C02_COMPATIBILITY_PACKAGE.json')
        report.export_compatibility(self.out);self.assertEqual(before,core.file_hash(self.out/'C02_COMPATIBILITY_PACKAGE.json'))

    def test_concurrent_writer_lock_is_rejected(self):
        with core.run_lock(self.out):
            with self.assertRaises(BlockingIOError):
                with core.run_lock(self.out):pass

    def test_rule_drift_is_rejected(self):
        with patch.object(core,'TITLE_RULE','new-unapproved-rule'):
            with self.assertRaises(ValueError):core.initialize(self.out)

    def test_missing_timestamp_stays_pending(self):
        self.c1['mapping']['u']['message'].pop('create_time');self.write([self.c1]);self.passes()
        r=report.export_compatibility(self.out);self.assertEqual(r['pending_missing_metadata'],1)

    def test_real_invariants_on_synthetic(self):
        files=self.passes();r=report.validate(self.root,self.out,files)
        self.assertTrue(r['clean']);self.assertEqual(r['nondev_in_search'],0)

    def test_bidirectional_links_hashes_and_idempotency(self):
        self.write([conversation('link','React 코드','React SyntheticWidget.tsx useSyntheticValue()')]);self.passes()
        raw=self.root/'data/raw/github/INFLUY/Influy-FE/fixture.json';raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({'payload':[[{'id':123,'filename':'src/SyntheticWidget.tsx','patch':'+useSyntheticValue()','user':{'login':'synthetic'}}]]}))
        snapshot=self.root/'snapshot.sqlite3';src=sqlite3.connect(snapshot)
        src.executescript('CREATE TABLE source_versions(id,origin,source_key,raw_path,sha256);CREATE TABLE pr_inventory(id);INSERT INTO pr_inventory VALUES(1);')
        src.execute('INSERT INTO source_versions VALUES(?,?,?,?,?)',(1,'INFLUY/Influy-FE','pr:37:files',str(raw.relative_to(self.root)),core.file_hash(raw)));src.commit();src.close()
        original_hash=core.file_hash(snapshot)
        self.assertEqual(github.github_index(self.root,self.out,snapshot)['failures'],[])
        github.discover_links(self.out);before=report.summary(self.out)['counts'];github.discover_links(self.out)
        self.assertEqual(before,report.summary(self.out)['counts']);self.assertEqual(core.file_hash(snapshot),original_hash)
        with self.db() as db:
            self.assertEqual({x[0] for x in db.execute('SELECT DISTINCT direction FROM links')},{'github_to_chatgpt','chatgpt_to_github'})
            self.assertEqual({x[0] for x in db.execute('SELECT DISTINCT verification_status FROM links')},{'pending'})
            self.assertTrue(core.search(db,'',pr_number=37,repository='INFLUY/Influy-FE'))
            self.assertEqual(core.search(db,'',pr_number=38,repository='INFLUY/Influy-FE'),[])
        report.export_compatibility(self.out)
        link=json.loads((self.out/'FOR_CODEX_B.jsonl').read_text().splitlines()[0])
        self.assertEqual(link['github']['sha256'],core.file_hash(raw));self.assertTrue(link['chatgpt']['locations'][0]['sha256'])

    def test_supplemental_compare_commits_keep_exact_selector(self):
        raw=self.root/'data/raw/github/INFLUY/Influy-FE/large.json';raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({'payload':{'commits':[{'sha':'1'*40,'commit':{'message':'fix SyntheticWidget'}}]}}))
        snapshot=self.root/'large-snapshot.sqlite3';src=sqlite3.connect(snapshot)
        src.executescript('CREATE TABLE source_versions(id,origin,source_key,raw_path,sha256);CREATE TABLE pr_inventory(id);')
        src.execute('INSERT INTO source_versions VALUES(?,?,?,?,?)',(1,'INFLUY/Influy-FE','pr:97:compare-commits:1',str(raw.relative_to(self.root)),core.file_hash(raw)));src.commit();src.close()
        result=github.github_index(self.root,self.out,snapshot);self.assertEqual(result['failures'],[])
        with self.db() as db:
            r=db.execute('SELECT * FROM github_docs').fetchone();self.assertEqual(r['pointer'],'/payload/commits/0');self.assertEqual(r['object_id'],'1'*40);self.assertEqual(r['pr_number'],97)

    def test_historical_search_runs_kept_and_export_selects_current_corpus(self):
        self.passes();github.discover_links(self.out)
        first=report.summary(self.out)
        self.write([conversation('new','React 코드','React OtherSyntheticWidget.tsx')],name='conversations-001.json');self.passes();github.discover_links(self.out)
        final=report.summary(self.out)
        self.assertNotEqual(first['current_corpus'],final['current_corpus'])
        self.assertGreater(final['counts']['search_runs'],final['current_search_runs'])


if __name__=='__main__':unittest.main()
