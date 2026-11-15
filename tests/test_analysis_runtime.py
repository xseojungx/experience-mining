"""C04 end-to-end synthetic analysis only, isolated DB/files."""
import copy
import json
import shutil
import unittest

import test_execution_control as fixtures

from tools import analysis_runtime as rt
from tools import execution_control as ctl
from tools import work_queue as queue
from tools.analysis_contract import revision_digest
from tools.analysis_store import ROOT, artifact, canonical, contract_package, store_connection, transaction
from tools.audit_state import sha256


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        fixtures.QueueTests.setUp(self)
        (self.root/'tests/fixtures').mkdir();shutil.copyfile(ROOT/'tests/fixtures/source.txt',self.root/'tests/fixtures/source.txt')
        self.bundle=json.loads((ROOT/'tests/fixtures/contract-valid.json').read_text())
        c=self.bundle['candidates'][0];c['state']='INVESTIGATED';c['history'].pop();c['verification_ids']=[];self.bundle['verifications']=[]
        self.inputs={'tests/fixtures/source.txt':sha256(self.root/'tests/fixtures/source.txt')}
        with transaction(self.db):
            self.db.execute("INSERT INTO source_versions(id,origin,source_key,location,sha256,raw_path) VALUES(1,'synthetic','synthetic:1','fixture:source:line1',?,'tests/fixtures/source.txt')",(self.inputs['tests/fixtures/source.txt'],))
            p=contract_package(self.bundle);(self.root/'tests/initial.json').write_text(canonical(p))
            run=store_connection(self.db,self.root,p,[artifact(self.root,'tests/fixtures/source.txt','synthetic-v1')],'tests/initial.json')
        self.n=0
        self.execute('enroll',{'run_id':run})

    fixture_scope=fixtures.QueueTests.fixture_scope

    def get(self):return rt.current(self.db,'c1')

    def job(self,action,*,deps=(),revision=None):
        self.n+=1
        role,phase={'enroll':('pr_mining','enroll'),'review':('independent_verification','verify'),
            'transition':('pr_mining','analyze'),'analyze':('code_evolution','analyze'),
            'change':('independent_verification','change'),'publish':('note_writing','publish')}[action]
        rev=revision or (self.get()[0]['revision'] if action!='enroll' else revision_digest(self.bundle,'c1'))
        c={**copy.deepcopy(self.contract),'domain':'analysis','role':role,'phase':phase,'target':'c1','target_revision':rev,
           'inputs':self.inputs,'source_versions':[{'id':1,'path':'tests/fixtures/source.txt','sha256':self.inputs['tests/fixtures/source.txt']}],
           'outputs':[f'results/{self.n}.json']}
        if action=='publish':c['outputs'].append(f'vault/experience/influy/Synthetic ({self.n}).md')
        with transaction(self.db):
            u=ctl.register(self.db,('test','influy','synthetic','read'),str(self.n),c,'grant',self.root,dependencies=deps)
            r=queue.claim(self.db,unit_id=u,root=self.root)
        h={'project':'influy','candidate_id':'c1','target_revision':rev,'observations':[],
           'hypotheses':[],'evidence_ids':['e1'],'unknowns':[],'next_tasks':[]}
        return u,r,c,h

    def execute(self,action,request=None,*,fail_at=None):
        u,r,c,h=self.job(action)
        req={'action':action,**(request or {})}
        if action=='publish':req.setdefault('path',c['outputs'][1]);req.setdefault('note_id',f'n{u}');req.setdefault('note',self.note())
        with transaction(self.db):result=rt.complete(self.db,u,r['token'],req,h,self.root,fail_at=fail_at)
        return result

    def record(self,verdict='PASS',ident=None):
        h,b,c=self.get()
        return {'id':ident or 'v'+str(self.n+1),'verdict':verdict,'reviewer':'independent-fixture-reviewer',
            'reviewed_at':'2026-11-15T00:00:00+09:00','revision_digest':h['revision'],'input_hashes':self.inputs,
            'claim_checks':[{'claim_id':i,'verdict':'SUPPORTED','attribution':'confirmed','adoption_checked':True,
                             'alternatives_checked':True,'reason':'synthetic original compared','counterevidence':[]} for i in c['claim_ids']],
            'original_refs':[{'evidence_id':e['id'],'location':e['location'],'raw_path':e['raw_path'],'sha256':e['sha256'],'version':e['version']} for e in b['evidence']],
            'review_scope':'all claims, attribution, alternatives, adoption, counterevidence and subsequent changes',
            'reason':'synthetic review','counterevidence':[],'issues':[], 'originals_reopened':True,
            'attribution_checked':True,'counterevidence_checked':True,'followups_checked':True,
            'resume_condition':'' if verdict=='PASS' else 'new source and new independent review'}

    def note(self):return {'title':'Synthetic','date':'2026-11-15','competencies':['설계·트레이드오프'],
                'question_types':['주도적 개선'],'sections':{k:['f1'] for k in ['summary','goal','constraints','role','judgment','result','speech']}}

    def verified(self):
        self.execute('review',{'record':self.record()});self.execute('transition',{'state':'VERIFIED'})

    def test_normal_review_verified_and_synthetic_publication(self):
        self.verified();r=self.execute('publish')
        self.assertEqual(self.get()[2]['state'],'PUBLISHED');self.assertEqual(rt.audit_files(self.db,self.root),[])
        n=self.get()[1]['notes'][0];text=(self.root/n['path']).read_text()
        self.assertIn('verified: true',text);self.assertNotIn('Synthetic interpretation',text)
        self.assertIn('## 판단',text);self.assertIn('## 예상 꼬리 질문',text)
        self.assertEqual(self.db.execute('SELECT valid FROM ctl_publications').fetchone()[0],1)

    def test_reviewer_author_separation(self):
        for author in ['analyst']:
            r=self.record();r['reviewer']=author
            with self.assertRaises(ValueError):self.execute('review',{'record':r})

    def test_claim_evidence_attribution_counterevidence_adoption(self):
        for key,value in [('attribution','incorrect'),('verdict','NEEDS_EVIDENCE'),('adoption_checked',False),('alternatives_checked',False),('counterevidence',['fixture contradiction'])]:
            r=self.record();r['claim_checks'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.execute('review',{'record':r})

    def test_partial_claim_review_and_stale_hash_or_target(self):
        for mutate in ['coverage','input','target','original','followups']:
            r=self.record()
            if mutate=='coverage':r['claim_checks'].pop()
            elif mutate=='input':r['input_hashes']={}
            elif mutate=='target':r['revision_digest']='0'*64
            elif mutate=='original':r['original_refs']=[]
            else:r['followups_checked']=False
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):self.execute('review',{'record':r})

    def test_nonpass_verdict_hold_and_resume_conditions(self):
        for verdict in ['NEEDS_EVIDENCE','CONTRADICTED','PENDING','HELD']:
            self.execute('review',{'record':self.record(verdict)})
            self.assertEqual(self.get()[2]['disposition'],'held')
            with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_reviews').fetchone()[0],4)

    def test_unresolved_blocker_blocks_verified_and_publication(self):
        r=self.record();r['issues']=[{'id':'x','description':'fixture attribution ambiguity','blocking':True,'status':'open'}]
        with self.assertRaises(ValueError):self.execute('review',{'record':r})
        r['verdict']='NEEDS_EVIDENCE';r['resume_condition']='resolve x';self.execute('review',{'record':r})
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})
        with self.assertRaises(ValueError):self.execute('publish')

    def test_superseding_failed_review_requires_new_independent_pass(self):
        self.execute('review',{'record':self.record('NEEDS_EVIDENCE','failed')})
        self.execute('review',{'record':self.record('PASS','fixed'),'supersedes':['failed']})
        self.execute('transition',{'state':'VERIFIED'});self.assertEqual(self.get()[2]['state'],'VERIFIED')

    def test_c01_core_facts_and_question_blocks(self):
        h,b,c=self.get();b['claims'][0]['core_aspects']=[]
        self.execute('analyze',{'bundle':b})
        self.execute('review',{'record':self.record()})
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})

    def test_unanswered_core_question_blocks(self):
        h,b,c=self.get();c['questions']=[{'core':True,'question':'who implemented?','answer':None}]
        self.execute('analyze',{'bundle':b});self.execute('review',{'record':self.record()})
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})

    def test_pass_then_candidate_change_stale_and_no_pass_reuse(self):
        self.verified();old=self.get()[0]['revision']
        self.execute('change',{'change_id':'ch1','kind':'user_correction','reason':'synthetic correction'})
        self.assertTrue(self.get()[0]['stale']);self.assertEqual(self.get()[2]['state'],'INVESTIGATED')
        self.assertEqual(self.db.execute('SELECT stale FROM ctl_reviews').fetchone()[0],1)
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})
        self.execute('review',{'record':self.record()});self.execute('transition',{'state':'VERIFIED'})
        self.assertEqual(self.get()[2]['state'],'VERIFIED')

    def test_all_change_event_types_create_intake_without_permission(self):
        for n,kind in enumerate(rt.CHANGE_KINDS):
            with transaction(self.db):rt.invalidate(self.db,str(n),kind,'synthetic change',candidate_ids=['c1'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_revalidation WHERE state="pending_approval"').fetchone()[0],len(rt.CHANGE_KINDS))
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_jobs').fetchone()[0],1)

    def test_source_graph_impact_and_authorized_revalidation_binding(self):
        self.verified()
        with transaction(self.db):self.assertEqual(rt.invalidate_source(self.db,1,'src-change','new source version'),['c1'])
        u,r,c,h=self.job('review')
        with transaction(self.db):rt.attach_revalidation(self.db,'src-change','c1',u,self.root)
        self.assertEqual(self.db.execute('SELECT state FROM ctl_revalidation').fetchone()[0],'queued')

    def test_published_impact_preserves_old_file_and_republication(self):
        self.verified();self.execute('publish');old=self.get()[1]['notes'][0];before=(self.root/old['path']).read_bytes()
        self.execute('change',{'change_id':'template-v2','kind':'template','reason':'synthetic change'})
        self.assertEqual(self.db.execute('SELECT valid FROM ctl_publications').fetchone()[0],0)
        self.execute('review',{'record':self.record()});self.execute('transition',{'state':'VERIFIED'});self.execute('publish')
        self.assertEqual((self.root/old['path']).read_bytes(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_publications').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT sum(valid) FROM ctl_publications').fetchone()[0],1)

    def test_expired_claim_and_changed_candidate_completion(self):
        u,r,c,h=self.job('transition')
        self.db.execute('UPDATE queue_units SET lease_until=0 WHERE id=?',(u,));self.db.commit()
        with self.assertRaises(ValueError),transaction(self.db):rt.complete(self.db,u,r['token'],{'action':'transition','state':'VERIFIED'},h,self.root)
        u,r,c,h=self.job('review');record=self.record()
        _,b,_=self.get();b['claims'][0]['text']='changed source interpretation';self.execute('analyze',{'bundle':b})
        with self.assertRaises(ValueError),transaction(self.db):rt.complete(self.db,u,r['token'],{'action':'review','record':record},h,self.root)

    def test_files_db_partial_failure_recovery_and_duplicate_completion(self):
        self.verified();u,r,c,h=self.job('publish');req={'action':'publish','path':c['outputs'][1],'note_id':'recovery','note':self.note()}
        for failure in ['after_files','before_checkpoint']:
            with self.subTest(failure=failure),self.assertRaises(RuntimeError),transaction(self.db):rt.complete(self.db,u,r['token'],req,h,self.root,fail_at=failure)
            self.assertEqual(self.get()[2]['state'],'VERIFIED');self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_publications').fetchone()[0],0)
        self.assertEqual(len(rt.audit_files(self.db,self.root)),2)
        with transaction(self.db):rt.complete(self.db,u,r['token'],req,h,self.root)
        self.assertEqual(rt.audit_files(self.db,self.root),[])
        with self.assertRaises(ValueError),transaction(self.db):rt.complete(self.db,u,r['token'],req,h,self.root)

    def test_corrupt_or_missing_completed_artifact(self):
        self.verified();u=self.execute('publish');last=self.db.execute('SELECT max(unit_id) FROM ctl_checkpoints').fetchone()[0]
        n=self.get()[1]['notes'][0];(self.root/n['path']).write_text('corrupt')
        self.assertIn('corrupt',{f['status'] for f in rt.audit_files(self.db,self.root)})
        with self.assertRaises(ValueError):ctl.audit_completion(self.db,last,self.root)
        (self.root/n['path']).unlink();self.assertIn('missing',{f['status'] for f in rt.audit_files(self.db,self.root)})

    def test_duplicate_publication_and_old_path_not_overwritten(self):
        self.verified();self.execute('publish')
        with self.assertRaises(ValueError):self.execute('publish')
        p=self.root/'vault/experience/influy/collision.md';p.write_text('original')
        with self.assertRaises(ValueError):rt.immutable_file(self.root,str(p.relative_to(self.root)),b'new')
        self.assertEqual(p.read_text(),'original')

    def test_template_vocabulary_and_inference_mixing_rejected(self):
        self.verified()
        for field,value in [('competencies',['invented']),('question_types',[]),('sections',{k:['f2'] for k in self.note()['sections']})]:
            options=self.note();options[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.execute('publish',{'note':options})
        (self.root/'spec/NOTE_TEMPLATES.md').write_text('changed')
        with self.assertRaises(ValueError):self.execute('publish')

    def test_analysis_cannot_forge_verification_or_state_or_project(self):
        for mode in ['pass','state','project']:
            _,b,c=self.get()
            if mode=='pass':b['verifications']=json.loads((ROOT/'tests/fixtures/contract-valid.json').read_text())['verifications']
            elif mode=='state':c['state']='VERIFIED'
            else:c['project']='ON'
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.execute('analyze',{'bundle':b})

    def test_no_direct_published_transition_or_role_bypass(self):
        with self.assertRaises(ValueError):self.execute('transition',{'state':'PUBLISHED'})
        u,r,c,h=self.job('transition')
        with self.assertRaises(ValueError),transaction(self.db):rt.complete(self.db,u,r['token'],{'action':'review','record':self.record()},h,self.root)

    def test_external_source_invalidation_then_review_and_republish(self):
        self.verified();self.execute('publish')
        with transaction(self.db):rt.invalidate_source(self.db,1,'new-source','synthetic upstream version')
        u,r,c,h=self.job('review');record=self.record()
        with transaction(self.db):
            rt.attach_revalidation(self.db,'new-source','c1',u,self.root)
            rt.complete(self.db,u,r['token'],{'action':'review','record':record},h,self.root)
        self.assertEqual(self.get()[2]['state'],'INVESTIGATED');self.assertFalse(self.get()[0]['stale'])
        self.assertEqual(self.db.execute('SELECT state FROM ctl_revalidation').fetchone()[0],'resolved')
        self.execute('transition',{'state':'VERIFIED'});self.execute('publish')

    def test_permitted_phase_does_not_grant_unapproved_candidate_stage(self):
        scope=copy.deepcopy(self.scope);scope['states']=['INVESTIGATED']
        self.fixture_scope(scope)
        self.execute('review',{'record':self.record()})
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})

    def test_checkpoint_detects_graph_tampering(self):
        self.verified()
        last=self.db.execute('SELECT max(unit_id) FROM ctl_checkpoints').fetchone()[0]
        run=self.get()[0]['run_id']
        self.db.execute("DELETE FROM ana_edges WHERE run_id=? AND relation='context'",(run,));self.db.commit()
        with self.assertRaises(ValueError):ctl.audit_completion(self.db,last,self.root)

    def test_explicit_blocker_resolution_keeps_old_revisions(self):
        r=self.record('NEEDS_EVIDENCE','blocked');r['issues']=[{'id':'x','description':'synthetic unknown attribution','blocking':True,'status':'open'}]
        self.execute('review',{'record':r})
        self.execute('review',{'record':self.record('PASS','resolved'),'supersedes':['blocked'],
            'resolutions':{'blocked:x':{'reason':'synthetic original confirms attribution','evidence_ids':['e1']}}})
        self.execute('transition',{'state':'VERIFIED'})
        original=self.db.execute("SELECT payload FROM ana_revisions WHERE kind='verification' AND object_id='blocked'").fetchall()
        self.assertIn('open',{json.loads(v[0])['issues'][0]['status'] for v in original})
        self.assertIn('resolved',{json.loads(v[0])['issues'][0]['status'] for v in original})

    def test_new_source_registration_automatically_revokes_publication(self):
        self.verified();self.execute('publish')
        with transaction(self.db):
            self.db.execute("INSERT INTO source_versions(origin,source_key,location,sha256,raw_path) VALUES('synthetic','synthetic:1','fixture:new',?,'tests/new.txt')",('a'*64,))
        self.assertTrue(self.get()[0]['stale']);self.assertEqual(self.db.execute('SELECT valid FROM ctl_publications').fetchone()[0],0)
        self.assertGreater(self.db.execute('SELECT count(*) FROM ctl_revalidation').fetchone()[0],0)
        with self.assertRaises(ValueError):self.execute('review',{'record':self.record()})

    def test_resume_reconciliation_invalidates_changed_template(self):
        self.verified();self.execute('publish')
        (self.root/'spec/NOTE_TEMPLATES.md').write_text('synthetic changed template')
        with transaction(self.db):self.assertEqual(len(rt.reconcile(self.db,self.root)),1)
        self.assertEqual(self.db.execute('SELECT valid FROM ctl_publications').fetchone()[0],0)
        with transaction(self.db):rt.reconcile(self.db,self.root)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_changes').fetchone()[0],1)

    def test_optional_deep_investigation_skip_requires_reason(self):
        self.execute('transition',{'state':'LINKED','mode':'revalidate','reason':'synthetic optional investigation review'})
        self.execute('review',{'record':self.record()})
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED','mode':'skip_optional'})
        self.execute('transition',{'state':'VERIFIED','mode':'skip_optional','reason':'simple synthetic case, reviewed all originals'})
        self.assertEqual(self.get()[2]['state'],'VERIFIED')

    def test_counterevidence_has_original_fk_and_refutes_edge(self):
        r=self.record('CONTRADICTED');r['counterevidence']=[{'claim_id':'f1','evidence_id':'e1','reason':'synthetic contradiction'}]
        r['claim_checks'][0]['verdict']='CONTRADICTED';self.execute('review',{'record':r})
        run=self.get()[0]['run_id']
        self.assertEqual(self.db.execute("SELECT count(*) FROM ana_edges WHERE run_id=? AND relation='refutes'",(run,)).fetchone()[0],1)
        with self.assertRaises(ValueError):self.execute('transition',{'state':'VERIFIED'})
