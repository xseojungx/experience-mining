"""C03 synthetic common queue tests; never run collectors or remote calls."""
import copy
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tools import execution_control as ctl
from tools import work_queue as queue
from tools.analysis_store import BASE, ROOT, migrate_connection, transaction
from tools.audit_state import sha256


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.path=self.root/'test.sqlite3'
        for p in ['spec','tests','results','data/raw/github/synthetic']:(self.root/p).mkdir(parents=True,exist_ok=True)
        for p in ['AGENTS.md','spec/MASTER_PLAN.md','spec/NOTE_TEMPLATES.md']:
            shutil.copyfile(ROOT/p,self.root/p)
        (self.root/'tests/input.txt').write_text('synthetic source')
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON');self.db.executescript(BASE.read_text())
        self.db.execute("INSERT INTO approvals VALUES('test','synthetic only','human fixture')")
        self.db.execute("INSERT INTO queue_scopes VALUES('test','influy','synthetic','read')");self.db.commit()
        with transaction(self.db):migrate_connection(self.db);ctl.migrate(self.db)
        self.spec={p:sha256(self.root/p) for p in ['AGENTS.md','spec/MASTER_PLAN.md','spec/NOTE_TEMPLATES.md']}
        self.scope={'projects':['influy'],'origins':['synthetic'],'operations':['read'],'roles':['collection',*ctl.ROLES],
          'phases':['collect','enroll','analyze','verify','publish','change'],'targets':['fixture','c1'],
          'input_paths':['tests/input.txt','tests/fixtures/source.txt'],'output_roots':['results','vault/experience/influy','data/raw/github/synthetic'],
          'decision_ids':['ARCH-04','ARCH-05','ARCH-06','NOTE-01','GOV-03'],'spec_sha256':self.spec,'analysis_versions':['synthetic-v1'],'states':['collection',*ctl.STAGES]}
        until=int(time.time())+36000
        self.db.execute('UPDATE approvals SET scope=?',(ctl.canonical({'kind':'execution_authorization','scope':self.scope,'valid_from':0,'valid_until':until}),));self.db.commit()
        with transaction(self.db):ctl.grant(self.db,'grant','test',self.scope,'human fixture',valid_from=0,valid_until=until)
        self.contract={'domain':'collection','role':'collection','phase':'collect','target':'fixture','target_revision':'fixture-v1',
          'decision_ids':['ARCH-04'],'spec_version':ctl.SPEC_VERSION,'spec_sha256':self.spec,
          'inputs':{'tests/input.txt':sha256(self.root/'tests/input.txt')},'source_versions':[],'analysis_version':'synthetic-v1','outputs':[]}
        self.source=self.root/'data/raw/github/synthetic/source.json';self.source.write_text('{}')
        self.report={'sources':[{'source_key':'fixture','location':'fixture:1','raw_path':str(self.source.relative_to(self.root)),'sha256':sha256(self.source)}],
                     'result':{'count':1},'next_unit':None}

    def fixture_scope(self,scope):
        until=self.db.execute('SELECT valid_until FROM ctl_grants').fetchone()[0]
        approved={'kind':'execution_authorization','scope':scope,'valid_from':0,'valid_until':until}
        self.db.execute('UPDATE approvals SET scope=?',(ctl.canonical(approved),))
        approval=self.db.execute('SELECT * FROM approvals').fetchone()
        self.db.execute('UPDATE ctl_grants SET scope=?,approval_digest=?',(ctl.canonical(scope),ctl.digest(tuple(approval))))
        self.db.commit()

    def add(self,key='one',contract=None,deps=()):
        return ctl.register(self.db,('test','influy','synthetic','read'),key,contract or self.contract,'grant',self.root,dependencies=deps)

    def claim(self,unit=None,**kwargs):return queue.claim(self.db,unit_id=unit,root=self.root,**kwargs)
    def finish(self,row,report=None):return queue.finish(self.db,row,report or self.report,root=self.root)

    def test_additive_migration_replay_drift(self):
        with transaction(self.db):self.assertFalse(ctl.migrate(self.db))
        self.db.execute('DROP INDEX ctl_one_current_publication');self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):ctl.migrate(self.db)

    def test_exact_scope_rejects_wrong_project_role_phase_source(self):
        for field,value in [('role','note_writing'),('phase','unknown'),('target','other'),('analysis_version','changed')]:
            b=copy.deepcopy(self.contract);b[field]=value
            if field=='role':b['domain']='analysis'
            with self.subTest(field=field),self.assertRaises(ValueError),transaction(self.db):self.add(contract=b)
        b=copy.deepcopy(self.contract);b['inputs']['state/secret']='0'*64
        with self.assertRaises((ValueError,OSError)),transaction(self.db):self.add(contract=b)

    def test_approval_presence_not_permission(self):
        with self.assertRaises(ValueError),transaction(self.db):queue.enqueue(self.db,('test','influy','synthetic','read'),'one')
        self.db.execute("UPDATE ctl_grants SET revoked=1");self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):self.add()

    def test_duplicate_contract_and_completion(self):
        with transaction(self.db):u=self.add();self.assertEqual(u,self.add());row=self.claim();self.finish(row)
        with transaction(self.db):self.assertEqual(u,self.add());ctl.audit_completion(self.db,u,self.root)
        with self.assertRaises(ValueError),transaction(self.db):self.finish(row)
        b=copy.deepcopy(self.contract);b['target_revision']='other'
        with self.assertRaises(ValueError),transaction(self.db):self.add(contract=b)

    def test_concurrent_claim_is_serialized(self):
        with transaction(self.db):u=self.add();row=self.claim()
        two=sqlite3.connect(self.path,timeout=0);two.execute('PRAGMA foreign_keys=ON')
        try:
            with transaction(self.db):
                with self.assertRaises(sqlite3.OperationalError):two.execute('BEGIN IMMEDIATE')
            with transaction(two):self.assertIsNone(queue.claim(two,root=self.root))
        finally:two.close()

    def test_wrong_token_and_expiry_internal_finish(self):
        with transaction(self.db):u=self.add();row=self.claim()
        wrong={**row,'token':'wrong'}
        with self.assertRaises(ValueError),transaction(self.db):self.finish(wrong)
        self.db.execute('UPDATE queue_units SET lease_until=0');self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):self.finish(row)
        with transaction(self.db):new=self.claim();self.assertNotEqual(row['token'],new['token']);self.finish(new)

    def test_renewal_and_expired_renewal(self):
        with transaction(self.db):u=self.add();r=self.claim();queue.renew(self.db,u,r['token'],root=self.root,seconds=1200)
        self.db.execute('UPDATE queue_units SET lease_until=0');self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):queue.renew(self.db,u,r['token'],root=self.root)

    def test_retry_backoff_exhaustion_history(self):
        with transaction(self.db):u=self.add();r=self.claim();queue.retry(self.db,u,r['token'],'fixture failure',root=self.root);self.assertIsNone(self.claim())
        for n in range(3):
            self.db.execute('UPDATE ctl_jobs SET next_attempt=0');self.db.commit()
            with transaction(self.db):r=self.claim();queue.retry(self.db,u,r['token'],'fixture failure',root=self.root)
        with transaction(self.db):self.assertIsNone(self.claim())
        self.assertEqual(self.db.execute('SELECT disposition FROM ctl_jobs').fetchone()[0],'failed')
        self.assertEqual(self.db.execute("SELECT count(*) FROM ctl_events WHERE event IN ('interrupted','failed')").fetchone()[0],4)

    def test_hold_resume(self):
        with transaction(self.db):u=self.add();r=self.claim();queue.retry(self.db,u,r['token'],'hold',root=self.root,hold=True,resume_condition='new evidence');self.assertIsNone(self.claim())
        with transaction(self.db):queue.resume(self.db,u,'evidence supplied',root=self.root);self.assertIsNotNone(self.claim())

    def test_dependencies_and_checkpoint_actual_files(self):
        with transaction(self.db):u=self.add();v=self.add('next',deps=[u])
        with self.assertRaises(ValueError),transaction(self.db):self.claim(v)
        with transaction(self.db):r=self.claim(u);self.finish(r);self.assertIsNotNone(self.claim(v))
        self.source.unlink()
        with self.assertRaises(OSError):ctl.audit_completion(self.db,u,self.root)

    def test_stale_input_or_approval_or_spec_blocks_completion(self):
        with transaction(self.db):u=self.add();r=self.claim()
        for sql in ["UPDATE approvals SET scope='changed'", "UPDATE ctl_grants SET valid_until=1"]:
            with self.assertRaises(ValueError),transaction(self.db):self.db.execute(sql);self.finish(r)
        (self.root/'tests/input.txt').write_text('changed')
        with self.assertRaises(ValueError),transaction(self.db):self.finish(r)

    def test_bad_result_atomic_and_next_cursor(self):
        with transaction(self.db):u=self.add();r=self.claim()
        bad=copy.deepcopy(self.report);bad['next_unit']='one'
        with self.assertRaises(ValueError),transaction(self.db):self.finish(r,bad)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_versions').fetchone()[0],0)
        good=copy.deepcopy(self.report);good['next_unit']='two'
        with transaction(self.db):self.finish(r,good)
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue_units').fetchone()[0],2)

    def test_checkpoint_mismatch_not_success(self):
        with transaction(self.db):u=self.add();r=self.claim();self.finish(r)
        self.db.execute("UPDATE queue_units SET result='{}'");self.db.commit()
        with self.assertRaises(ValueError):ctl.audit_completion(self.db,u,self.root)

    def test_split_coverage_overlap_merge_dependencies(self):
        with transaction(self.db):u=self.add();v=self.add('two',deps=[u])
        m={'project':'influy','pr':'synthetic:1','axis':'thread','universe':['a','b'],'parts':{str(u):['a']},'unreviewed':['b']}
        with transaction(self.db):ctl.split(self.db,'g',m)
        with self.assertRaises(ValueError),transaction(self.db):ctl.merge_split(self.db,'g',v)
        m['parts'][str(u)].append('b');m['unreviewed']=[]
        with transaction(self.db):ctl.split(self.db,'g2',m);ctl.merge_split(self.db,'g2',v)
        m['parts']['999']=['b']
        with self.assertRaises(ValueError),transaction(self.db):ctl.split(self.db,'g3',m)

    def test_source_path_escape_and_chatgpt_block(self):
        for path in ['../escape','data/raw/chatgpt/account_A/x','/etc/passwd']:
            with self.assertRaises(ValueError):ctl.file_path(self.root,path)
        with self.assertRaises(ValueError):ctl.file_path(self.root,'state/result.json',output=True)

    def test_legacy_prose_and_scope_expansion_cannot_create_grant(self):
        self.db.execute("INSERT INTO approvals VALUES('past','implementation only','human fixture')");self.db.commit()
        until=self.db.execute('SELECT valid_until FROM ctl_grants').fetchone()[0]
        with self.assertRaises(ValueError),transaction(self.db):ctl.grant(self.db,'bad','past',self.scope,'human fixture',valid_from=0,valid_until=until)
        expanded=copy.deepcopy(self.scope);expanded['projects'].append('ON')
        with self.assertRaises(ValueError),transaction(self.db):ctl.grant(self.db,'bad','test',expanded,'human fixture',valid_from=0,valid_until=until)
        self.db.execute('UPDATE ctl_grants SET scope=?',(ctl.canonical(expanded),));self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):self.add()

    def test_registered_pending_route_reuses_exact_contract(self):
        with transaction(self.db):
            u=self.add()
            self.assertEqual(queue.enqueue(self.db,('test','influy','synthetic','read'),'one',root=self.root),u)
        self.db.execute('UPDATE ctl_grants SET revoked=1');self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):queue.enqueue(self.db,('test','influy','synthetic','read'),'one',root=self.root)

    def test_permanent_failure_never_automatically_resumes(self):
        with transaction(self.db):
            u=self.add();r=self.claim()
            queue.retry(self.db,u,r['token'],'permanent synthetic failure',root=self.root,permanent=True,resume_condition='new approval and task')
            self.assertIsNone(self.claim())
        with self.assertRaises(ValueError),transaction(self.db):queue.resume(self.db,u,'automatic reset',root=self.root)
