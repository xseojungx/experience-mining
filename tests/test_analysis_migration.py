"""C02-2 atomic migration/recovery; synthetic data and temp SQLite only."""
import copy
import json
import shutil
import sqlite3
from unittest import mock

import test_analysis_storage as storage_fixtures

from tools.analysis_store import ROOT, connection, contract_package
from tools.migrate_analysis import (
    INPUTS,
    RESULT,
    apply_connection,
    baseline,
    check_quiescent,
    copy_backup,
    durable_json,
    integrity,
    operation_status,
    rehearse,
    run_production,
    source_report,
)


# Reuse setup helpers, without inheriting/re-running the C02-1 test cases.
class MigrationTests(__import__('unittest').TestCase):
    write=storage_fixtures.StorageTests.write
    legacy=storage_fixtures.StorageTests.legacy

    def setUp(self):
        storage_fixtures.StorageTests.setUp(self)
        (self.root/'tests/fixtures').mkdir()
        shutil.copyfile(ROOT/'tests/fixtures/source.txt',self.root/'tests/fixtures/source.txt')
        self.p=contract_package(self.bundle)
        self.write('tests/result.json',self.p)

    def test_ddl_import_and_commit_share_atomic_transaction(self):
        for failure in ['ddl','after_schema','import','before_commit']:
            with self.subTest(failure=failure),connection(self.db) as db:
                before=baseline(db);db.execute('BEGIN EXCLUSIVE')
                with self.assertRaises(RuntimeError):apply_connection(db,self.root,self.p,self.inputs,'tests/result.json',fail_at=failure)
                db.rollback();self.assertEqual(before,baseline(db))
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_migrations'").fetchone())
        with connection(self.db) as db:
            db.execute('BEGIN EXCLUSIVE');r=apply_connection(db,self.root,self.p,self.inputs,'tests/result.json');db.commit()
            before=baseline(db);counts=list(db.iterdump())
            db.execute('BEGIN EXCLUSIVE');r2=apply_connection(db,self.root,self.p,self.inputs,'tests/result.json');db.commit()
            self.assertEqual(r['run_id'],r2['run_id']);self.assertEqual(counts,list(db.iterdump()));self.assertEqual(before,baseline(db))

    def test_existing_pr_inventory_rows_and_schema_are_preserved(self):
        import ast
        tree=ast.parse((ROOT/'tools/collect_prs.py').read_text())
        sql=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.strip().startswith('CREATE TABLE IF NOT EXISTS pr_inventory ('))
        with connection(self.db) as db:
            db.executescript(sql)
            db.execute("INSERT INTO pr_inventory(origin,pr_id,number,url,state) VALUES('synthetic',404,1,'fixture:pr','closed')");db.commit()
            before=baseline(db);self.assertEqual(before['tables']['pr_inventory']['count'],1)
            db.execute('BEGIN EXCLUSIVE');apply_connection(db,self.root,self.p,self.inputs,'tests/result.json');db.commit()
            self.assertEqual(before,baseline(db))

    def test_real_sqlite_writer_lock_contention(self):
        with connection(self.db) as one,connection(self.db) as two:
            one.execute('BEGIN EXCLUSIVE');two.execute('PRAGMA busy_timeout=0')
            with self.assertRaises(sqlite3.OperationalError):two.execute('BEGIN IMMEDIATE')
            one.rollback();two.execute('BEGIN EXCLUSIVE');two.rollback()

    def test_retained_exclusive_lock_across_commit(self):
        with connection(self.db) as one,connection(self.db) as two:
            one.execute('PRAGMA locking_mode=EXCLUSIVE');one.execute('BEGIN EXCLUSIVE');one.commit()
            two.execute('PRAGMA busy_timeout=0')
            with self.assertRaises(sqlite3.OperationalError):two.execute('BEGIN IMMEDIATE')

    def test_backup_rehearsal_including_postcommit_restore(self):
        backup=self.root/'backups/pre.sqlite3'
        with connection(self.db) as db:
            db.execute('BEGIN EXCLUSIVE');h=copy_backup(self.db,backup)
            report=rehearse(backup,self.root,self.p,self.inputs,'tests/result.json')
            self.assertTrue(report['restored_backup_byte_identical']);self.assertTrue(report['temporary_directory_removed'])
            self.assertEqual(len(report['cases']),5);db.rollback()
        with backup.open('ab') as f:f.write(b'bad')
        with self.assertRaises(ValueError):copy_backup(self.db,backup)

    def test_source_unique_ambiguous_unregistered_and_incomplete(self):
        with connection(self.db) as db:
            self.assertEqual(source_report(db,self.root,self.p)['snapshot_counts'],{'unique':1})
            db.execute("INSERT INTO source_versions(origin,source_key,location,sha256,raw_path) SELECT 'synthetic','other',location,sha256,raw_path FROM source_versions WHERE id=1")
            self.assertEqual(source_report(db,self.root,self.p)['snapshot_counts'],{'ambiguous':1})
            db.execute('DELETE FROM source_versions')
            self.assertEqual(source_report(db,self.root,self.p)['snapshot_counts'],{'unregistered':1})
            p=copy.deepcopy(self.p);p['locations'][0]['snapshot_sha256']=None
            self.assertEqual(source_report(db,self.root,p)['snapshot_counts'],{'insufficient':1})

    def test_bad_source_hash_and_path_abort_entire_transaction(self):
        for mode in ['hash','path']:
            p=copy.deepcopy(self.p)
            if mode=='hash':p['locations'][0]['snapshot_sha256']='0'*64
            else:p['locations'][0]['raw_path']='../escape'
            with self.subTest(mode=mode),connection(self.db) as db:
                db.execute('BEGIN EXCLUSIVE')
                with self.assertRaises(ValueError):apply_connection(db,self.root,p,self.inputs,'tests/result.json')
                db.rollback();self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_runs'").fetchone())

    def test_running_queue_blocks(self):
        with connection(self.db) as db:
            db.execute("INSERT INTO queue_scopes VALUES('synthetic','influy','synthetic','read')")
            db.execute("INSERT INTO queue_units(approval_id,project,origin,operation,unit_key,state) VALUES('synthetic','influy','synthetic','read','one','running')")
            with self.assertRaises(ValueError):check_quiescent(db)

    def test_orphans_and_partial_states(self):
        with connection(self.db) as db:
            self.assertEqual(operation_status(db,self.root,self.p,'tests/result.json',self.root)['state'],'not_applied')
            (self.root/'REPORT.tmp-orphan').write_text('partial')
            self.assertTrue(operation_status(db,self.root,self.p,'tests/result.json',self.root)['orphan_temp_files'])
            from tools.analysis_store import migrate_connection
            db.execute('BEGIN EXCLUSIVE');migrate_connection(db);db.commit()
            self.assertEqual(operation_status(db,self.root,self.p,'tests/result.json',self.root)['state'],'schema_only')
            (self.root/'tests/result.json').unlink()
            self.assertEqual(operation_status(db,self.root,self.p,'tests/result.json',self.root)['state'],'artifact_missing')

    def test_replay_detects_corrupted_normalized_graph(self):
        with connection(self.db) as db:
            db.execute('BEGIN EXCLUSIVE');apply_connection(db,self.root,self.p,self.inputs,'tests/result.json');db.commit()
            db.execute("DELETE FROM ana_edges WHERE relation='context'");db.commit()
            db.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(ValueError):apply_connection(db,self.root,self.p,self.inputs,'tests/result.json')
            db.rollback()

    def test_sqlite_backup_restore_with_exclusive_destination(self):
        backup=self.root/'pre.sqlite3'
        with connection(self.db) as db:
            db.execute('PRAGMA locking_mode=EXCLUSIVE');db.execute('BEGIN EXCLUSIVE');before=baseline(db)
            copy_backup(self.db,backup)
            apply_connection(db,self.root,self.p,self.inputs,'tests/result.json');db.commit()
            with sqlite3.connect(backup) as src:src.backup(db)
            self.assertEqual(baseline(db),before);integrity(db)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_runs'").fetchone())

    def production_fixture(self):
        # Fixed path/authorization mock is limited to this synthetic root.
        (self.root/'state').mkdir();shutil.copyfile(self.db,self.root/'state/state.sqlite3')
        (self.root/'checkpoints/c02-1').mkdir(parents=True);(self.root/'checkpoints/c02-2').mkdir()
        from tools.legacy_adapter import adapt_legacy
        paths,raw=self.legacy()
        # No raw evidence: identity deliberately insufficient, not invented.
        raw[1]['claim_index']['old-f']['claim']['evidence']=[]
        for dst,value in zip(INPUTS,raw):self.write(dst,value)
        self.write(RESULT,adapt_legacy(self.root))
        from tools.audit_state import sha256
        return {'db_sha256_before':sha256(self.root/'state/state.sqlite3')}

    def test_production_flow_replay_and_lost_postcommit_journal(self):
        record=self.production_fixture()
        no_opens=__import__('subprocess').CompletedProcess([],1,'','')
        with mock.patch('tools.migrate_analysis.authorize',return_value=record),mock.patch('tools.migrate_analysis.subprocess.run',return_value=no_opens):
            r=run_production(self.root);self.assertTrue(r['postcommit_verified']);self.assertTrue(r['replay_same_run'])
            durable_json(self.root/'checkpoints/c02-2/OPERATION.json',{'stage':'precommit'})
            r2=run_production(self.root);self.assertEqual(r['run_id'],r2['run_id']);self.assertEqual(r2['initial_state']['state'],'registered')

    def test_postcommit_verification_failure_restores_original(self):
        record=self.production_fixture();target=self.root/'state/state.sqlite3'
        with sqlite3.connect(target) as db:before=baseline(db)
        from tools.migrate_analysis import verify_run
        calls=[0]
        def check(*args,**kwargs):
            calls[0]+=1
            # rehearse has ten precommit calls, production apply is eleventh.
            # Trigger only postcommit by checking the active transaction flag.
            if not args[0].in_transaction:raise ValueError('synthetic postcommit validation failure')
            return verify_run(*args,**kwargs)
        no_opens=__import__('subprocess').CompletedProcess([],1,'','')
        with mock.patch('tools.migrate_analysis.authorize',return_value=record),mock.patch('tools.migrate_analysis.subprocess.run',return_value=no_opens),mock.patch('tools.migrate_analysis.verify_run',side_effect=check):
            with self.assertRaises(ValueError):run_production(self.root)
        with sqlite3.connect(target) as db:
            self.assertEqual(before,baseline(db));self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_runs'").fetchone());integrity(db)
        self.assertEqual(json.loads((self.root/'checkpoints/c02-2/OPERATION.json').read_text())['stage'],'restored_after_postcommit_failure')

    def test_open_connections_and_authorization_fail_closed(self):
        self.production_fixture()
        record={'db_sha256_before':'unused'}
        with mock.patch('tools.migrate_analysis.authorize',return_value=record),mock.patch('tools.migrate_analysis.subprocess.run',return_value=__import__('subprocess').CompletedProcess([],0,'p123\ncwriter\n','')):
            with self.assertRaises(ValueError):run_production(self.root)
        from tools.migrate_analysis import authorize
        with self.assertRaises((ValueError,FileNotFoundError)):authorize(self.root,'checkpoints/c02-2/AUTHORIZATION.json')
