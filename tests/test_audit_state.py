import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.analysis_contract import SPEC_VERSION
from tools.audit_state import (
    CANONICAL,
    SPEC_FILES,
    AuditStop,
    audit,
    audit_authorization,
    audit_canonical,
    audit_database,
    audit_legacy,
    check_hashes,
    safe_path,
    sha256,
)

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=Path(__file__).parent/'fixtures'


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'state').mkdir()
        self.dbpath=self.root/'state/state.sqlite3'
        source=self.root/'tests/fixtures/source.txt'
        source.parent.mkdir(parents=True); source.write_text('synthetic source')
        with sqlite3.connect(self.dbpath) as db:
            db.executescript((ROOT/'state/schema.sql').read_text())
            db.execute("INSERT INTO approvals VALUES ('fixture','synthetic only','fixture approval')")
            db.execute("INSERT INTO work_items VALUES ('done','synthetic','complete','fixture')")
            db.execute("INSERT INTO work_items VALUES ('next','synthetic','pending',NULL)")
            db.execute("INSERT INTO queue_scopes VALUES ('fixture','influy','synthetic','read')")
            db.execute("INSERT INTO queue_units(id,approval_id,project,origin,operation,unit_key,state,result) VALUES (1,'fixture','influy','synthetic','read','1','complete','{}')")
            db.execute("INSERT INTO source_versions(id,origin,source_key,location,sha256,raw_path) VALUES (1,'synthetic','1','fixture:source',?,'tests/fixtures/source.txt')",(sha256(source),))
            db.execute("INSERT INTO unit_sources VALUES (1,1)")
            details={'analysis_version':'synthetic-v1','cursor':None,'outputs':['tests/fixtures/source.txt'],
                     'output_sha256':{'tests/fixtures/source.txt':sha256(source)},'input_sha256':{}}
            db.execute("INSERT INTO checkpoints(completed_id,next_id,details) VALUES ('done','next',?)",(json.dumps(details),))

    def mutate(self,sql,args=()):
        with sqlite3.connect(self.dbpath) as db:
            db.execute(sql,args)

    def codes(self, report):
        return {f['code'] for f in report['findings']}

    def canonical(self):
        for relative in SPEC_FILES:
            p=self.root/relative; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(SPEC_VERSION+' synthetic spec')
        checkpoint='checkpoints/synthetic.md'
        (self.root/'checkpoints').mkdir(exist_ok=True)
        (self.root/checkpoint).write_text(SPEC_VERSION+' synthetic checkpoint')
        ledger={p:{'sha256':sha256(self.root/p)} for p in (*SPEC_FILES,checkpoint)}
        m={'canonical_applied':True,'spec_version':SPEC_VERSION,'checkpoint':checkpoint,'files':ledger}
        path=self.root/CANONICAL; path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(m))
        return path,m

    def test_valid_database_and_no_writes_or_sidecars(self):
        before=sha256(self.dbpath)
        result=audit_database(self.root, expected={'seq':1,'completed_id':'done','next_id':'next','analysis_version':'synthetic-v1'})
        self.assertEqual(result['findings'],[])
        self.assertEqual(before,result['sha256_after'])
        self.assertEqual(sorted(p.name for p in (self.root/'state').iterdir()),['state.sqlite3'])

    def test_connection_itself_refuses_writes(self):
        connect=sqlite3.connect
        def verify(database_uri,**kwargs):
            self.assertIn('mode=ro&immutable=1',database_uri)
            db=connect(database_uri,**kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE work_items SET status='pending'")
            db.rollback()
            return db
        with patch('tools.audit_state.sqlite3.connect',side_effect=verify):
            self.assertEqual(audit_database(self.root)['findings'],[])

    def test_sidecars_stop_and_missing_db_does_not_initialize(self):
        for suffix in ('-wal','-journal','-shm'):
            side=Path(str(self.dbpath)+suffix); side.write_bytes(b'synthetic interrupted state')
            with self.assertRaises(AuditStop):audit_database(self.root)
            side.unlink()
        with self.assertRaises(OSError):audit_database(self.root,'state/missing.sqlite3')
        self.assertFalse((self.root/'state/missing.sqlite3').exists())

    def test_source_hash_change_and_absence(self):
        p=self.root/'tests/fixtures/source.txt';p.write_text('tampered')
        self.assertIn('hash_mismatch',self.codes(audit_database(self.root)))
        p.unlink();self.assertIn('missing_file',self.codes(audit_database(self.root)))

    def test_exact_scope_and_source_links(self):
        self.mutate("DELETE FROM queue_scopes")
        self.assertIn('scope',self.codes(audit_database(self.root)))
        self.mutate("DELETE FROM unit_sources")
        self.assertIn('queue_source',self.codes(audit_database(self.root)))

    def test_running_claim_and_invalid_result(self):
        self.mutate("UPDATE queue_units SET state='running',result=NULL")
        self.assertIn('lease',self.codes(audit_database(self.root)))
        self.mutate("UPDATE queue_units SET state='complete',result='not json'")
        self.assertIn('queue_result',self.codes(audit_database(self.root)))

    def test_checkpoint_output_next_and_expected_mismatch(self):
        self.mutate("UPDATE work_items SET status='complete' WHERE id='next'")
        report=audit_database(self.root,expected={'seq':12})
        self.assertIn('checkpoint',self.codes(report));self.assertIn('checkpoint_mismatch',self.codes(report))
        self.mutate("UPDATE checkpoints SET details=?",(json.dumps({'outputs':['state/missing.json']}),))
        self.assertIn('checkpoint_output',self.codes(audit_database(self.root)))

    def test_foreign_key_corruption_detected(self):
        self.mutate("UPDATE unit_sources SET version_id=999")
        codes=self.codes(audit_database(self.root));self.assertIn('foreign_key',codes);self.assertIn('source_link',codes)

    def test_paths_never_read_personal_exports_or_escape(self):
        for p in ('data/raw/chatgpt/account_A/conversations.json','../secret','/etc/passwd','sources/a','state/../data/a'):
            with self.assertRaises(AuditStop):safe_path(self.root,p)
        link=self.root/'state/link';link.symlink_to(self.root/'tests/fixtures/source.txt')
        with self.assertRaises(AuditStop):check_hashes(self.root,{'state/link':'0'*64})
        self.mutate("UPDATE source_versions SET raw_path='data/raw/chatgpt/account_A/conversations.json'")
        with self.assertRaises(AuditStop):audit_database(self.root)

    def test_canonical_version_hash_and_checkpoint(self):
        path,m=self.canonical();self.assertEqual(audit_canonical(self.root),[])
        (self.root/SPEC_FILES[0]).write_text('changed');self.assertIn('hash_mismatch',{x['code'] for x in audit_canonical(self.root)})
        m['canonical_applied']=False;m['checkpoint']='checkpoints/missing.md';path.write_text(json.dumps(m))
        codes={x['code'] for x in audit_canonical(self.root)}
        self.assertIn('canonical',codes);self.assertIn('checkpoint',codes)

    def test_authorization_exact_scope_and_bound_hashes(self):
        self.canonical()
        record={"task_id":"C01","approved":True,"spec_version":SPEC_VERSION,
                "source":"synthetic user approval", "operations":["implement_c01","synthetic_tests","temporary_sqlite_tests","read_only_state_audit"],
                "spec_sha256":{f:sha256(self.root/f) for f in SPEC_FILES}}
        path=self.root/"checkpoints/c01/AUTHORIZATION.json";path.parent.mkdir(parents=True);path.write_text(json.dumps(record))
        self.assertEqual(audit_authorization(self.root),[])
        record["operations"].append("publish");path.write_text(json.dumps(record))
        self.assertIn("authorization",{x["code"] for x in audit_authorization(self.root)})
        with patch('tools.audit_state.audit_database',side_effect=AssertionError('DB must not be opened')):
            self.assertEqual(audit(self.root)['status'],'stopped')

    def test_input_changed_with_matching_output_is_historical(self):
        p="tests/fixtures/source.txt"
        details={"input_sha256":{p:"a"*64},"output_sha256":{p:sha256(self.root/p)}}
        self.mutate("UPDATE checkpoints SET details=?",(json.dumps(details),))
        result=audit_database(self.root)
        self.assertEqual([x['severity'] for x in result['findings'] if x['code']=='hash_mismatch'],['warning'])
        (self.root/p).write_text('subsequent tampering')
        self.assertIn('error',[x['severity'] for x in audit_database(self.root)['findings']])

    def test_start_stops_before_db_access_on_spec_mismatch(self):
        self.canonical();(self.root/SPEC_FILES[1]).write_text('wrong version')
        with patch('tools.audit_state.audit_database',side_effect=AssertionError('DB must not be opened')):
            self.assertEqual(audit(self.root)['status'],'stopped')

    def test_legacy_reports_missing_fields_without_mutation(self):
        values=[json.loads((FIXTURES/f).read_text()) for f in ('legacy-candidates.json','legacy-assessment.json','legacy-review.json')]
        before=copy.deepcopy(values); findings=audit_legacy(*values)
        codes={x['code'] for x in findings}
        self.assertTrue({'legacy_missing_fields','legacy_claim_shape','legacy_review_revision','legacy_contract'}<=codes)
        self.assertEqual(values,before)
        self.assertFalse({'score','rubric','dangling','candidate_set'} & codes)
        values[2]['items'][0]['source_claim_ids']=['absent']
        self.assertIn('dangling',{x['code'] for x in audit_legacy(*values)})

    def test_legacy_cross_file_mismatch_and_score(self):
        values=[json.loads((FIXTURES/f).read_text()) for f in ('legacy-candidates.json','legacy-assessment.json','legacy-review.json')]
        values[1]['assessments'][0]['scores'][0]['score']=99
        values[2]['analysis_version']='other';values[2]['items']=[]
        codes={x['code'] for x in audit_legacy(*values)}
        self.assertTrue({'score','analysis_version','candidate_set'}<=codes)

    def test_current_spec_drift_is_reported_as_historical_warning(self):
        self.canonical()
        details={'input_sha256':{'spec/MASTER_PLAN.md':'a'*64}}
        self.mutate("UPDATE checkpoints SET details=?",(json.dumps(details),))
        report=audit_database(self.root)
        self.assertEqual([x['severity'] for x in report['findings'] if x['code']=='hash_mismatch'],['warning'])


if __name__=='__main__':
    unittest.main()
