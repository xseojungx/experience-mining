"""C02-1 synthetic fixtures only; all migration writes use TemporaryDirectory."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.analysis_contract import validate_bundle
from tools.analysis_store import (
    BASE,
    MIGRATION,
    ROOT,
    artifact,
    canonical,
    connection,
    contract_package,
    digest,
    load_bundle,
    migrate,
    restore_sandbox,
    store,
)
from tools.legacy_adapter import adapt_legacy


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root/'tests').mkdir()
        self.db=self.root/'scratch.sqlite3'
        with sqlite3.connect(self.db) as db:
            db.executescript(BASE.read_text())
            db.execute("INSERT INTO approvals VALUES('synthetic','test','fixture')")
            db.execute("INSERT INTO work_items VALUES('old','test','complete',NULL)")
            db.execute("INSERT INTO source_versions(id,origin,source_key,location,sha256,raw_path) VALUES(1,'synthetic','synthetic:1','fixture:source:line1',?,'tests/fixtures/source.txt')",('8972722fd0a94b6d472091361837e1cea76072d65ba1065f95581049046dcfac',))
        self.bundle=json.loads((ROOT/'tests/fixtures/contract-valid.json').read_text())
        self.write('tests/input.json',self.bundle)
        self.inputs=[artifact(self.root,'tests/input.json','synthetic-v1')]

    def write(self,path,value):
        (self.root/path).write_text(canonical(value)+'\n')

    def package(self,bundle=None):
        p=contract_package(self.bundle if bundle is None else bundle)
        self.write('tests/result.json',p)
        return p

    def save(self,p,**kw):
        return store(self.db,self.root,p,self.inputs,'tests/result.json',**kw)

    def count(self,table):
        with connection(self.db) as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def legacy(self):
        c=[{'id':'old-c','project':'influy','state':'INVESTIGATED','recorded_fact':'Synthetic fact',
             'ai_inference':{'text':'Synthetic inference','alternative_explanations':['Synthetic alternative']},
             'published':False,'independent_verification':'pending'}]
        a={'analysis_version':'fixture-v1','assessment_version':'a-v1','source_hashes':{'tests/source.json':'a'*64},
           'claim_index':{'old-f':{'report':'tests/report.json','claim':{'id':'old-f','text':'Synthetic fact',
             'evidence':[{'source':'tests/source.json','sha':'b'*40,'path':'x.ts','lines':[1,2],
                          'content_sha256':'c'*64}]}}},
           'assessments':[{'candidate_id':'old-c','scores':copy.deepcopy(self.bundle['assessments'][0]['scores']),'total':0}]}
        for s in a['assessments'][0]['scores']:
            s['evidence_claim_ids']=['old-f']
        q={'analysis_version':'fixture-v1','items':[{'candidate_id':'old-c','source_claim_ids':['old-f'],'status':'pending'}]}
        paths=['tests/legacy-c.json','tests/legacy-a.json','tests/legacy-q.json']
        for path,value in zip(paths,[c,a,q]):
            self.write(path,value)
        return paths,[c,a,q]

    def test_additive_idempotent_migration_and_restore(self):
        with connection(self.db) as db:
            before=list(db.iterdump())
        self.assertTrue(migrate(self.db)); self.assertFalse(migrate(self.db))
        p=self.package();self.save(p)
        restore_sandbox(self.db)
        with connection(self.db) as db:
            self.assertEqual(before,list(db.iterdump()))
            self.assertEqual(db.execute('SELECT approval_id FROM work_items').fetchone(),(None,))
        self.assertTrue(migrate(self.db))

    def test_interrupted_migration_resumes(self):
        with self.assertRaises(RuntimeError):migrate(self.db,fail_after=4)
        with connection(self.db) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ana_%'").fetchall(),[])
        self.assertTrue(migrate(self.db))

    def test_history_and_extension_drift_stop(self):
        migrate(self.db)
        with connection(self.db) as db:
            db.execute("UPDATE ana_migrations SET sha256=?",('0'*64,));db.commit()
        with self.assertRaises(ValueError):migrate(self.db)
        with connection(self.db) as db:
            from tools.audit_state import sha256
            db.execute('UPDATE ana_migrations SET sha256=?',(sha256(MIGRATION),));db.execute('DROP VIEW ana_note_revisions');db.commit()
        with self.assertRaises(ValueError):migrate(self.db)

    def test_base_schema_drift_stops(self):
        with connection(self.db) as db:db.execute('CREATE TABLE alien(x)');db.commit()
        with self.assertRaises(ValueError):migrate(self.db)

    def test_production_and_symlink_paths_refused(self):
        for path in [ROOT/'state/state.sqlite3',ROOT/'state/fake.sqlite3']:
            with self.assertRaises(ValueError):migrate(path)
            with self.assertRaises(ValueError):restore_sandbox(path)
        link=self.root/'link.sqlite3';link.symlink_to(self.db)
        with self.assertRaises(ValueError):migrate(link)

    def test_valid_roundtrip_and_source_link(self):
        migrate(self.db);p=self.package();run=self.save(p)
        self.assertEqual(load_bundle(self.db,run),self.bundle)
        self.assertEqual(validate_bundle(load_bundle(self.db,run)),[])
        with connection(self.db) as db:
            self.assertEqual(db.execute('SELECT source_version_id FROM ana_locations').fetchone(),(1,))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT approval_id FROM work_items').fetchone(),(None,))

    def test_replay_safe(self):
        migrate(self.db);p=self.package();run=self.save(p)
        counts=[self.count(t) for t in ['ana_runs','ana_revisions','ana_edges','ana_artifacts']]
        self.assertEqual(run,self.save(p))
        self.assertEqual(counts,[self.count(t) for t in ['ana_runs','ana_revisions','ana_edges','ana_artifacts']])

    def test_interrupted_import_atomic_resume(self):
        migrate(self.db);p=self.package()
        with self.assertRaises(RuntimeError):self.save(p,fail_after=3)
        for t in ['ana_runs','ana_revisions','ana_artifacts','ana_objects']:
            self.assertEqual(self.count(t),0)
        run=self.save(p);self.assertEqual(load_bundle(self.db,run),self.bundle)

    def test_input_result_missing_hash_or_mismatch(self):
        migrate(self.db);p=self.package()
        self.write('tests/input.json',{})
        with self.assertRaises(ValueError):self.save(p)
        self.write('tests/input.json',self.bundle)
        self.write('tests/result.json',{})
        with self.assertRaises(ValueError):self.save(p)
        (self.root/'tests/input.json').unlink()
        with self.assertRaises(OSError):self.save(p)
        self.assertEqual(self.count('ana_runs'),0)

    def test_c01_invalid_duplicate_and_dangling_rejected(self):
        for mutation in ['missing','duplicate','reference','stale_review','wrong_state']:
            b=copy.deepcopy(self.bundle)
            if mutation=='missing':del b['claims'][0]['actor']
            elif mutation=='duplicate':b['claims'].append(copy.deepcopy(b['claims'][0]))
            elif mutation=='reference':b['claims'][0]['evidence_ids']=['absent']
            elif mutation=='stale_review':b['claims'][0]['text']='Changed synthetic text'
            else:b['candidates'][0]['state']='PASS'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):contract_package(b)

    def test_relations_foreign_keys_and_revision_constraints(self):
        migrate(self.db);p=self.package()
        p['edges'] += [['claim','f1','supports','evidence','e1'],['claim','f2','refutes','evidence','e1'],
                       ['claim','f2','depends_on','claim','f1'],['candidate','c1','affected_by','evidence','e1']]
        self.write('tests/result.json',p);run=self.save(p)
        with connection(self.db) as db:
            rev=db.execute("SELECT revision FROM ana_run_revisions WHERE kind='claim' AND object_id='f1'").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE ana_revisions SET logical_revision='rewrite'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO ana_edges VALUES(?,?,?,?,?,?,?,?)',(run,'claim','f1',rev,'supports','evidence','absent','0'*64))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO ana_revisions VALUES(?,?,?,?,?,?)',('claim','f1','a'*64,None,'b'*64,canonical({'id':'f1'})))
            db.rollback()

    def test_new_revision_preserves_previous(self):
        migrate(self.db);p=self.package();self.save(p)
        previous=digest(self.bundle['candidates'][0])
        b=copy.deepcopy(self.bundle);b['candidates'][0]['state']='INVESTIGATED';b['candidates'][0]['history'].pop()
        b['verifications'][0]['status']='superseded'
        p=self.package(b);run=self.save(p,predecessors={('candidate','c1'):previous})
        with connection(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ana_candidate_revisions").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT previous_revision FROM ana_revisions WHERE revision=?',(digest(b['candidates'][0]),)).fetchone()[0],previous)
        self.assertEqual(load_bundle(self.db,run),b)

    def test_bad_predecessor_rolls_back(self):
        migrate(self.db);p=self.package()
        with self.assertRaises(sqlite3.IntegrityError):self.save(p,predecessors={('candidate','c1'):'a'*64})
        self.assertEqual(self.count('ana_runs'),0)

    def test_legacy_lossless_ids_hashes_unknowns_scores(self):
        paths,raw=self.legacy();p=adapt_legacy(self.root,paths)
        self.assertEqual([x['payload'] for x in p['legacy_inputs']],raw)
        self.assertEqual(p['grade'],'legacy_incomplete')
        self.assertTrue(p['diagnostics']['c01_projection'])
        self.assertEqual(p['bundle']['candidates'][0]['state'],'INVESTIGATED')
        f=next(x for x in p['bundle']['claims'] if x['id']=='old-f')
        self.assertIsNone(f['author']);self.assertIsNone(f['kind']);self.assertIsNone(f['actor'])
        e=p['bundle']['evidence'][0];self.assertEqual(e['sha256'],'a'*64)
        self.assertEqual(e['legacy_payload']['content_sha256'],'c'*64)
        self.assertEqual(e['provenance']['json_pointer'],'/claim_index/old-f/claim/evidence/0')
        self.assertEqual(p['bundle']['assessments'][0]['scores'],raw[1]['assessments'][0]['scores'])
        self.assertEqual(p,adapt_legacy(self.root,paths))
        migrate(self.db);self.inputs=[artifact(self.root,x,'legacy-fixture-v1') for x in paths]
        self.write('tests/result.json',p);run=self.save(p)
        self.assertEqual(load_bundle(self.db,run),p['bundle']);self.assertEqual(run,self.save(p))

    def test_legacy_no_fake_pass_or_metadata_repair(self):
        paths,raw=self.legacy();p=adapt_legacy(self.root,paths);migrate(self.db)
        self.inputs=[artifact(self.root,x,'legacy-fixture-v1') for x in paths]
        p['bundle']['verifications'][0]['status']='passed'
        self.write('tests/result.json',p)
        with self.assertRaises(ValueError):self.save(p)
        self.assertEqual(self.count('ana_runs'),0)

    def test_legacy_duplicate_missing_reference_and_candidate_set(self):
        for mode in ['duplicate','reference','set','score','passed']:
            paths,raw=self.legacy()
            if mode=='duplicate':raw[0].append(copy.deepcopy(raw[0][0]))
            elif mode=='reference':raw[2]['items'][0]['source_claim_ids']=['missing']
            elif mode=='set':raw[2]['items']=[]
            elif mode=='score':raw[1]['assessments'][0]['total']=999
            else:raw[2]['items'][0]['status']='passed'
            for path,value in zip(paths,raw):self.write(path,value)
            with self.subTest(mode=mode),self.assertRaises(ValueError):adapt_legacy(self.root,paths)

    def test_legacy_original_id_collision_is_rejected(self):
        paths,raw=self.legacy()
        raw[0][0]['ai_inference']={'id':'old-f','text':'Different synthetic inference'}
        self.write(paths[0],raw[0])
        with self.assertRaises(ValueError):adapt_legacy(self.root,paths)

    def test_missing_required_edge_rejected(self):
        migrate(self.db);p=self.package();p['edges'].pop();self.write('tests/result.json',p)
        with self.assertRaises(ValueError):self.save(p)

    def test_duplicate_edges_rejected_atomically(self):
        migrate(self.db);p=self.package();p['edges'].append(p['edges'][0]);self.write('tests/result.json',p)
        with self.assertRaises(sqlite3.IntegrityError):self.save(p)
        self.assertEqual(self.count('ana_runs'),0)

    def test_locations_missing_evidence_rejected_atomically(self):
        migrate(self.db);p=self.package();p['locations'][0]['evidence_id']='absent';self.write('tests/result.json',p)
        with self.assertRaises(ValueError):self.save(p)
        self.assertEqual(self.count('ana_runs'),0)

    def test_issue_and_note_revision_roundtrip(self):
        b=copy.deepcopy(self.bundle)
        b['candidates'][0]['state']='INVESTIGATED';b['candidates'][0]['history'].pop()
        b['verifications'][0]['status']='pending'
        b['verifications'][0]['issues']=[{'id':'issue1','description':'Synthetic unresolved attribution',
                                         'blocking':True,'status':'open'}]
        b['candidates'][0]['note_ids']=['n1']
        b['notes']=[{'id':'n1','candidate_id':'c1','revision_digest':'placeholder',
          'path':'vault/log/influy/synthetic.md','template_sha256':'200d182ef85d4221116a25d7728d3baadfbeb0d9410a1f608222877f26756c5a',
          'kind':'log','publication_status':'draft'}]
        from tools.analysis_contract import revision_digest
        b['notes'][0]['revision_digest']=revision_digest(b,'c1')
        migrate(self.db);p=self.package(b);run=self.save(p)
        self.assertEqual(load_bundle(self.db,run),b)
        self.assertEqual(self.count('ana_issues'),1);self.assertEqual(self.count('ana_note_revisions'),1)
        b['notes'][0]['kind']='experience'
        with self.assertRaises(ValueError):contract_package(b)

    def test_source_hash_metadata_cannot_be_rewritten(self):
        migrate(self.db);p=self.package();p['locations'][0]['snapshot_sha256']='0'*64
        self.write('tests/result.json',p)
        with self.assertRaises(ValueError):self.save(p)
        self.assertEqual(self.count('ana_runs'),0)


if __name__=='__main__':unittest.main()
