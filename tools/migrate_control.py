"""Explicit additive C03+C04 operational control schema installer.

Reuse C02 transaction, quiescence, immutable byte backup, integrity, durable
reporting and restore mechanisms. Never enroll candidates or register grants.
"""
import argparse
import fcntl
import json
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from .analysis_store import ROOT, canonical, digest, migrate_connection, transaction
from .audit_state import audit_canonical, sha256
from .execution_control import SQL, migrate
from .migrate_analysis import check_quiescent, copy_backup, durable_json, integrity, no_sidecars

REPORT='checkpoints/c03-c04'


def preserved(db):
    schema=list(map(tuple,db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ctl_%' ORDER BY name")))
    tables={}
    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ctl_%' ORDER BY name").fetchall():
        rows=list(map(tuple,db.execute('SELECT * FROM "'+name+'"')))
        tables[name]={'count':len(rows),'sha256':digest(sorted(rows,key=canonical))}
    return {'schema':digest(schema),'tables':tables}


def apply(db,*,fail_at=None):
    before=preserved(db)
    # Reuse v2 DDL validation; ctl_* has a separate exact DDL ledger.
    migrate_connection(db)
    added=migrate(db)
    if fail_at=='after_schema':raise RuntimeError('synthetic schema interruption')
    if preserved(db)!=before:raise ValueError('existing logical data/schema changed')
    integrity(db)
    return {'added':added,'preserved':before}


def rehearse(backup):
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp).resolve()/'rehearsal.sqlite3';shutil.copyfile(backup,path)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            before=preserved(db)
            with transaction(db):one=apply(db)
            with transaction(db):two=apply(db)
            if two['added'] or preserved(db)!=before:raise ValueError('schema replay/preservation failed')
            # Restore after a committed migration; verify byte and logical backup.
            with closing(sqlite3.connect(backup)) as src:src.backup(db)
            if preserved(db)!=before:raise ValueError('restoration failed')
            integrity(db)
            # Failure injection only when backup has no controls yet.
            if one['added']:
                try:
                    with transaction(db):apply(db,fail_at='after_schema')
                except RuntimeError:pass
                else:raise ValueError('failure injection not reached')
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone():raise ValueError('partial schema persisted')
                with transaction(db):apply(db)
            with closing(sqlite3.connect(backup)) as src:src.backup(db)
        # SQLite backup restores logical content; exact file restoration is a
        # separate, closed-connection copy (SQLite may change header counters).
        shutil.copyfile(backup,path)
        if sha256(path)!=sha256(backup):raise ValueError('restore byte mismatch')
    return {'isolated_apply':True,'replay':True,'rollback':True,'restore_logical':True,'restore_byte_identical':True,'temporary_directory_removed':True}


def authorize(root):
    p=Path(root)/REPORT
    auth=json.loads((p/'AUTHORIZATION.json').read_text());inputs=json.loads((p/'INPUT_MANIFEST.json').read_text())
    if auth.get('task_id')!='C03+C04' or auth.get('approved') is not True or 'conditional additive control schema migration after backup/exclusive/rehearsal/restore verification' not in auth['allowed'] or not auth['source']:
        raise ValueError('exact C03+C04 operational authorization required')
    if audit_canonical(Path(root)):raise ValueError('canonical mismatch')
    for f,h in auth['spec_sha256'].items():
        if sha256(Path(root)/f)!=h:raise ValueError('spec changed')
    return inputs


def run(root=ROOT,*,fail_postcommit=False):
    root=Path(root).resolve();inputs=authorize(root);path=root/'state/state.sqlite3';out=root/REPORT
    no_sidecars(path)
    opened=subprocess.run(['/usr/sbin/lsof','-t','--',str(path)],capture_output=True,text=True)
    if opened.returncode!=1 or opened.stdout.strip():raise ValueError('writers/readers present or quiescence could not be established')
    lock=out/'migration.lock';backup=out/'backups/state.pre-c03-c04.sqlite3'
    with lock.open('a+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=0)) as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA locking_mode=EXCLUSIVE');db.execute('BEGIN EXCLUSIVE')
            check_quiescent(db)
            is_applied=db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone() is not None
            if not is_applied and sha256(path)!=inputs['db_sha256']:raise ValueError('operational input DB changed')
            before=preserved(db)
            if not is_applied:
                copied=copy_backup(path,backup)
                with closing(sqlite3.connect(backup)) as saved:integrity(saved)
                rehearsal=rehearse(backup)
            else:
                if not backup.is_file() or sha256(backup)!=inputs['db_sha256']:raise ValueError('verified original backup missing')
                copied=sha256(backup);rehearsal=rehearse(backup)
            durable_json(out/'SCHEMA_OPERATION.json',{'stage':'precommit','backup_sha256':copied,'rehearsal':rehearsal})
            committed=False
            try:
                result=apply(db);db.commit();committed=True
                if fail_postcommit:raise ValueError('synthetic postcommit failure')
                integrity(db)
                if preserved(db)!=before:raise ValueError('postcommit preservation failure')
                db.execute('BEGIN EXCLUSIVE');replay=apply(db);db.commit()
                if replay['added']:raise ValueError('replay added schema twice')
                result.update({'backup':str(backup),'backup_sha256':copied,'rehearsal':rehearsal,
                    'v2_unchanged':True,'postcommit_integrity':'ok','foreign_key_errors':0,'replay_noop':True,
                    'db_sha256_after':sha256(path),'control_sql_sha256':sha256(SQL),'grants_registered':0,'candidates_enrolled':0})
                durable_json(out/'SCHEMA_OPERATION.json',{'stage':'verified','result':result})
                return result
            except BaseException as exc:
                db.rollback()
                if committed and not is_applied:
                    with closing(sqlite3.connect(backup)) as src:src.backup(db)
                    integrity(db)
                    if preserved(db)!=before or db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone():raise ValueError('postcommit recovery failed; manual restore from retained backup required') from exc
                    durable_json(out/'SCHEMA_OPERATION.json',{'stage':'restored','backup_sha256':copied})
                raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply-c03-c04',action='store_true',required=True)
    parser.parse_args();print(canonical(run()))
