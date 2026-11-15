"""Additive production installer behavior using synthetic temporary roots only."""
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

import test_execution_control as fixtures

from tools import migrate_control as migration
from tools.analysis_store import transaction
from tools.audit_state import sha256


class ControlMigrationTests(unittest.TestCase):
    setUp=fixtures.QueueTests.setUp

    def unextended_copy(self):
        # A distinct v2 DB, not the setup DB with active test grants.
        path=self.root/'state/state.sqlite3';path.parent.mkdir()
        from tools.analysis_store import BASE, migrate_connection
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON');db.executescript(BASE.read_text())
            with transaction(db):migrate_connection(db)
        out=self.root/migration.REPORT;out.mkdir(parents=True)
        return path,out

    def test_rehearsal_apply_restore_replay_byte_identity(self):
        path,out=self.unextended_copy();before=sha256(path)
        result=migration.rehearse(path)
        self.assertTrue(all(result.values()));self.assertEqual(before,sha256(path))

    def test_full_operational_flow_and_replay_preserves_v2(self):
        path,out=self.unextended_copy();record={'db_sha256':sha256(path)}
        no_opens=subprocess.CompletedProcess([],1,'','')
        with mock.patch.object(migration,'authorize',return_value=record),mock.patch.object(migration.subprocess,'run',return_value=no_opens):
            first=migration.run(self.root);self.assertTrue(first['added'])
            second=migration.run(self.root);self.assertFalse(second['added'])
            self.assertEqual(first['db_sha256_after'],second['db_sha256_after'])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT version FROM ana_migrations').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM ctl_grants').fetchone()[0],0)

    def test_postcommit_failure_restores_original(self):
        path,out=self.unextended_copy();before=sha256(path);record={'db_sha256':before}
        with mock.patch.object(migration,'authorize',return_value=record),mock.patch.object(migration.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
            with self.assertRaises(ValueError):migration.run(self.root,fail_postcommit=True)
        with sqlite3.connect(path) as db:self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone())
        self.assertEqual(json.loads((out/'SCHEMA_OPERATION.json').read_text())['stage'],'restored')

    def test_open_writer_or_changed_db_blocks(self):
        path,out=self.unextended_copy()
        with mock.patch.object(migration,'authorize',return_value={'db_sha256':sha256(path)}),mock.patch.object(migration.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'123','')):
            with self.assertRaises(ValueError):migration.run(self.root)
        with mock.patch.object(migration,'authorize',return_value={'db_sha256':'wrong'}),mock.patch.object(migration.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','')):
            with self.assertRaises(ValueError):migration.run(self.root)

    def test_missing_exact_authorization_blocks(self):
        self.unextended_copy()
        with self.assertRaises(OSError):migration.authorize(self.root)
