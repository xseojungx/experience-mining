"""Collectors exercised through mocked network and temporary queue/files only."""
import copy
import json
import sqlite3
import unittest
from unittest import mock

import test_execution_control as fixtures

from tools import collect_pr_details as collector
from tools import collect_prs, recover_pr_metadata
from tools import execution_control as ctl
from tools.analysis_store import transaction


class CollectorTests(unittest.TestCase):
    setUp=fixtures.QueueTests.setUp
    fixture_scope=fixtures.QueueTests.fixture_scope

    def setup_route(self,operation='detail',key='1:detail',origin='synthetic'):
        scope=copy.deepcopy(self.scope);scope['operations']=[operation];scope['targets']=['pr:1'];scope['origins']=[origin]
        self.fixture_scope(scope)
        self.db.execute('INSERT INTO queue_scopes VALUES(?,?,?,?)',('test','influy',origin,operation));self.db.commit()
        c={**self.contract,'target':'pr:1'}
        with transaction(self.db):
            return ctl.register(self.db,('test','influy',origin,operation),key,c,'grant',self.root)

    def connect(self):
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');return db

    def test_detail_collector_uses_claim_and_common_finish(self):
        u=self.setup_route()
        response=__import__('subprocess').CompletedProcess([],0,json.dumps({'id':1,'comments':0,'commits':0,'changed_files':0}), '')
        with mock.patch.object(collector,'connect',self.connect),mock.patch.object(collector,'APPROVAL','test'),mock.patch.object(collector,'ROOT',self.root),mock.patch('tools.work_queue.ROOT',self.root),mock.patch.object(collector.subprocess,'run',return_value=response) as remote:
            collector.collect(0)
        self.assertEqual(remote.call_count,1);self.assertEqual(self.db.execute('SELECT state FROM queue_units WHERE id=?',(u,)).fetchone()[0],'complete')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ctl_checkpoints').fetchone()[0],1)

    def test_collector_failure_logs_cause_and_backoff(self):
        self.setup_route();response=__import__('subprocess').CompletedProcess([],1,'','synthetic unavailable')
        with mock.patch.object(collector,'connect',self.connect),mock.patch.object(collector,'APPROVAL','test'),mock.patch.object(collector,'ROOT',self.root),mock.patch('tools.work_queue.ROOT',self.root),mock.patch.object(collector.subprocess,'run',return_value=response),mock.patch.object(collector.time,'sleep'):
            collector.collect(0)
        self.assertEqual(self.db.execute('SELECT state FROM queue_units').fetchone()[0],'pending')
        self.assertEqual(self.db.execute("SELECT count(*) FROM ctl_events WHERE event='worker_error'").fetchone()[0],1)

    def test_mismatched_operation_route_rejected_before_network(self):
        with self.assertRaises(ValueError):self.setup_route('detail','1:files')

    def test_recovery_modules_have_no_import_time_network_or_db_writes(self):
        import ast

        from tools.analysis_store import ROOT
        for name in ['recover_large_commits','recover_pr_metadata']:
            tree=ast.parse((ROOT/'tools'/f'{name}.py').read_text())
            for statement in tree.body:
                self.assertNotIsInstance(statement,(ast.With,ast.For,ast.While))

    def test_all_collectors_delegate_queue_mutations(self):
        import ast
        import re

        from tools.analysis_store import ROOT
        for name in ['collect_prs','collect_pr_details','recover_large_commits','recover_pr_metadata']:
            tree=ast.parse((ROOT/'tools'/f'{name}.py').read_text())
            literals=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
            self.assertFalse(any(re.search(r'UPDATE\s+queue_units',s,re.I) for s in literals),name)

    def test_metadata_recovery_uses_claim_and_common_finish(self):
        u=self.setup_route(origin='synthetic/repo')
        payload={'data':{'repository':{'pullRequest':{'comments':{'totalCount':0},'commits':{'totalCount':1},'changedFiles':1,
            'updatedAt':'fixture','reviewThreads':{'pageInfo':{'hasNextPage':False},'nodes':[]}}}}}
        response=__import__('subprocess').CompletedProcess([],0,json.dumps(payload),'')
        with mock.patch.object(recover_pr_metadata,'connect',self.connect),mock.patch.object(recover_pr_metadata,'APPROVAL','test'),mock.patch.object(collect_prs,'ROOT',self.root),mock.patch('tools.work_queue.ROOT',self.root),mock.patch.object(recover_pr_metadata.subprocess,'run',return_value=response) as remote:
            recover_pr_metadata.run()
        self.assertEqual(remote.call_count,1);self.assertEqual(self.db.execute('SELECT state FROM queue_units WHERE id=?',(u,)).fetchone()[0],'complete')
        self.assertEqual(self.db.execute("SELECT count(*) FROM ctl_events WHERE event='claimed'").fetchone()[0],1)
