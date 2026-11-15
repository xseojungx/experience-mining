"""C02-2 explicit production migration, backup/restore and isolated rehearsal.

No queue/review engine. The C02-1 package is immutable and reused, so SQL never
commits before its external artifact exists. All DB changes share one transaction.
"""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

from .analysis_store import (
    ADAPTER_VERSION,
    KINDS,
    ROOT,
    TABLES,
    artifact,
    canonical,
    digest,
    migrate_connection,
    store_connection,
)
from .audit_state import audit_canonical, safe_path, sha256

VERSION = 'c02-2-v1'
RESULT = 'checkpoints/c02-1/LEGACY_COMPATIBILITY.json'
INPUTS = ['state/influy-pilot-candidates.json','state/influy-pilot-assessment.json',
          'state/influy-pilot-verification-queue.json']
BASE_TABLES = ('approvals','work_items','checkpoints','queue_scopes','queue_units','source_versions','unit_sources')


def durable_json(path, data):
    """Replace one managed report atomically, fsync both file and directory."""
    path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.tmp-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(canonical(data)+'\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()


def integrity(db):
    if list(db.execute('PRAGMA integrity_check')) != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('SQLite integrity/foreign key failure')


def baseline(db):
    """DDL and every legacy row (typed JSON) independent of SQLite page layout."""
    schema=list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ana_%' ORDER BY name"))
    tables={}
    names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ana_%' ORDER BY name")]
    if not set(BASE_TABLES)<=set(names):raise ValueError('legacy table missing')
    for table in names:
        columns=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
        rows=list(db.execute('SELECT * FROM '+table))
        tables[table]={'columns':columns,'count':len(rows),'sha256':digest(sorted(rows,key=canonical))}
    return {'schema_sha256':digest(schema),'tables':tables}


def no_sidecars(path):
    found=[str(path)+s for s in ('-wal','-shm','-journal') if Path(str(path)+s).exists()]
    if found:raise ValueError('SQLite sidecars present: '+str(found))


def check_quiescent(db):
    if db.execute('PRAGMA journal_mode').fetchone()[0]!='delete':
        raise ValueError('require pre-existing DELETE journal mode')
    if db.execute("SELECT count(*) FROM queue_units WHERE state='running'").fetchone()[0]:
        raise ValueError('running queue units; stop writers before migration')
    integrity(db)


def source_report(db,root,package):
    """Snapshot identity and selector checks are separate, without interpretation."""
    evidence={e['id']:e for e in package['bundle']['evidence']}
    cache={};rows=[]
    for loc in package['locations']:
        raw,h=loc.get('raw_path'),loc.get('snapshot_sha256')
        row={**loc,'matched_source_ids':[],'source_version_id':None,
             'snapshot_status':'insufficient','position_status':'pending','position_reason':'exact selector absent'}
        if raw and h:
            file=safe_path(Path(root),raw)
            if sha256(file)!=h:raise ValueError('raw snapshot hash mismatch: '+raw)
            ids=[x[0] for x in db.execute('SELECT id FROM source_versions WHERE raw_path=? AND sha256=?',(raw,h))]
            row['matched_source_ids']=ids
            row['snapshot_status']='unique' if len(ids)==1 else ('ambiguous' if ids else 'unregistered')
            if len(ids)==1:row['source_version_id']=ids[0]
            if raw not in cache:
                try:cache[raw]=json.loads(file.read_text())
                except (json.JSONDecodeError, UnicodeError):cache[raw]=None
            original=cache[raw];s=loc['selector'];legacy=evidence[loc['evidence_id']].get('legacy_payload',{})
            if isinstance(original,dict) and s.get('sha') and s.get('path'):
                snapshots=original.get('versions',original.get('snapshots',[]))
                matches=[f for snap in snapshots if snap.get('sha')==s['sha']
                         for f in snap.get('files',[]) if f.get('path')==s['path']]
                row['position_reason']='version/file not uniquely found in snapshot'
                if len(matches)==1:
                    f=matches[0];lines=s.get('lines');content=f.get('content')
                    hashes=[legacy[k] for k in ('sha256','content_sha256') if k in legacy]
                    blob_ok='git_blob' not in legacy or legacy['git_blob']==f.get('git_blob')
                    content_ok=isinstance(content,str) and all(v==hashlib.sha256(content.encode()).hexdigest() for v in hashes)
                    lines_ok=isinstance(lines,list) and len(lines)==2 and all(type(n) is int for n in lines) and isinstance(content,str) and 1<=lines[0]<=lines[1]<=len(content.splitlines())
                    if blob_ok and content_ok and lines_ok:
                        row.update(position_status='checked',position_reason='commit/file/range/blob/content metadata match')
                    else:row['position_reason']='range/blob/content missing or conflicting; no repair'
            elif isinstance(original,dict) and s.get('id') is not None:
                pages=original.get('payload',[])
                comments=[v for page in pages for v in (page if isinstance(page,list) else [page]) if isinstance(v,dict) and v.get('id')==s['id']]
                row['position_reason']='comment ID not uniquely found'
                if len(comments)==1:
                    c=comments[0]
                    if (not s.get('url') or s['url']==c.get('html_url')) and (not s.get('original_commit') or s['original_commit']==c.get('original_commit_id')):
                        row.update(position_status='checked',position_reason='comment ID/URL/original_commit match')
                    else:row['position_reason']='comment metadata conflicts; no repair'
        rows.append(row)
    return {'rows':rows,'snapshot_counts':dict(Counter(r['snapshot_status'] for r in rows)),
            'position_counts':dict(Counter(r['position_status'] for r in rows)),
            'policy':'FK confirms snapshot identity only. Pending selectors/identity stay legacy_incomplete; missing files, bad hashes, illegal paths or structural conflicts abort registration.'}


def verify_run(db,root,package,run,links,inputs,result_path):
    row=db.execute('SELECT grade,bundle,diagnostics,result_artifact FROM ana_runs WHERE id=?',(run,)).fetchone()
    if row is None or row[:3]!=(package['grade'],canonical(package['bundle']),canonical(package['diagnostics'])):
        raise ValueError('stored run differs from package')
    result=artifact(root,result_path,ADAPTER_VERSION)
    if db.execute('SELECT path,sha256,version FROM ana_artifacts WHERE id=?',(row[3],)).fetchone() != (result['path'],result['sha256'],result['version']):
        raise ValueError('result artifact link mismatch')
    actual_inputs=set(db.execute('SELECT a.path,a.sha256,a.version FROM ana_inputs i JOIN ana_artifacts a ON a.id=i.artifact_id WHERE i.run_id=?',(run,)))
    if actual_inputs!={(r['path'],r['sha256'],r['version']) for r in inputs}:
        raise ValueError('input artifact links mismatch')
    expected={(kind,obj['id'],digest(obj),canonical(obj)) for table,kind in zip(TABLES,KINDS) for obj in package['bundle'][table]}
    actual=set(db.execute('SELECT m.kind,m.object_id,m.revision,r.payload FROM ana_run_revisions m JOIN ana_revisions r ON r.kind=m.kind AND r.object_id=m.object_id AND r.revision=m.revision WHERE m.run_id=?',(run,)))
    if actual!=expected:raise ValueError('stored revisions differ')
    edges={tuple(e) for e in package['edges']}
    actual=set(db.execute('SELECT from_kind,from_id,relation,to_kind,to_id FROM ana_edges WHERE run_id=?',(run,)))
    if actual!=edges:raise ValueError('stored graph differs')
    locations={r['evidence_id']:(r['raw_path'],r['snapshot_sha256'],r['source_version_id'],canonical(r['selector'])) for r in links['rows']}
    actual={r[0]:tuple(r[1:]) for r in db.execute('SELECT evidence_id,raw_path,snapshot_sha256,source_version_id,selector FROM ana_locations WHERE run_id=?',(run,))}
    if actual!=locations:raise ValueError('stored locations differ')
    for path,h,version in db.execute('SELECT path,sha256,version FROM ana_artifacts'):
        if artifact(root,path,version)['sha256']!=h:raise ValueError('registered artifact changed/missing: '+path)
    integrity(db)


def apply_connection(db,root,package,inputs,result_path,*,fail_at=None):
    """Caller holds a single exclusive transaction; no commits here."""
    before=baseline(db)
    migrated=migrate_connection(db,fail_after=3 if fail_at=='ddl' else None)
    if fail_at=='after_schema':raise RuntimeError('synthetic stop after schema')
    links=source_report(db,root,package)
    run=store_connection(db,root,package,inputs,result_path,fail_after=3 if fail_at=='import' else None)
    if baseline(db)!=before:raise ValueError('legacy rows/DDL changed')
    verify_run(db,root,package,run,links,inputs,result_path)
    if fail_at=='before_commit':raise RuntimeError('synthetic stop before commit')
    return {'run_id':run,'schema_added':migrated,'source_links':links,'legacy_baseline':before}


def copy_backup(dbpath,backup_path):
    """Byte copy under exclusive lock BEFORE any SQL mutation (DELETE/no sidecars)."""
    dbpath=Path(dbpath);backup_path=Path(backup_path)
    no_sidecars(dbpath)
    if not backup_path.exists():
        backup_path.parent.mkdir(parents=True,exist_ok=True)
        with dbpath.open('rb') as src,backup_path.open('xb') as dst:
            shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
        fd=os.open(backup_path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    if sha256(backup_path)!=sha256(dbpath):raise ValueError('backup does not match pre-migration DB')
    with closing(sqlite3.connect(backup_path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as db:integrity(db)
    return sha256(backup_path)


def rehearse(backup,root,package,inputs,result_path):
    results=[]
    with tempfile.TemporaryDirectory() as temp:
        target=Path(temp).resolve()/'restore.sqlite3'
        for failure in ['ddl','after_schema','import','before_commit',None]:
            shutil.copyfile(backup,target)
            with closing(sqlite3.connect(target)) as db:
                db.execute('PRAGMA foreign_keys=ON');before=baseline(db);db.execute('BEGIN EXCLUSIVE')
                try:
                    apply_connection(db,root,package,inputs,result_path,fail_at=failure)
                    db.commit()
                except RuntimeError:
                    db.rollback()
                    if failure is None:raise
                if baseline(db)!=before:raise ValueError('rehearsal legacy mismatch')
                if failure:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_migrations'").fetchone():raise ValueError('partial migration survived rollback')
                    db.execute('BEGIN EXCLUSIVE');apply_connection(db,root,package,inputs,result_path);db.commit()
                counts=list(db.execute('SELECT (SELECT count(*) FROM ana_runs),(SELECT count(*) FROM ana_revisions),(SELECT count(*) FROM ana_edges)'))
                db.execute('BEGIN EXCLUSIVE');apply_connection(db,root,package,inputs,result_path);db.commit()
                if counts!=list(db.execute('SELECT (SELECT count(*) FROM ana_runs),(SELECT count(*) FROM ana_revisions),(SELECT count(*) FROM ana_edges)')):raise ValueError('rehearsal replay duplicated rows')
                integrity(db)
            # Restoring the exact backup after a COMMITTED migration is rehearsed.
            shutil.copyfile(backup,target)
            if sha256(target)!=sha256(backup):raise ValueError('restore hash mismatch')
            with closing(sqlite3.connect(target)) as db:
                integrity(db)
                if baseline(db)!=before:raise ValueError('rehearsal legacy mismatch')
            results.append({'failure':failure,'rollback_resume_replay_restore':'passed'})
    return {'cases':results,'restored_backup_byte_identical':True,'temporary_directory_removed':not Path(temp).exists()}


def operation_status(db,root,package,result_path,directory):
    temps=[p.name for p in Path(directory).glob('*.tmp-*')]
    present=db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_runs'").fetchone() is not None
    file=safe_path(Path(root),result_path)
    if not file.is_file():return {'state':'artifact_missing','orphan_temp_files':temps}
    if json.loads(file.read_text())!=package:return {'state':'artifact_changed','orphan_temp_files':temps}
    if not present:return {'state':'not_applied','orphan_temp_files':temps}
    runs=[r[0] for r in db.execute('SELECT r.id FROM ana_runs r JOIN ana_artifacts a ON a.id=r.result_artifact WHERE a.path=? AND a.sha256=?',(result_path,sha256(file)))]
    return {'state':'registered' if len(runs)==1 else ('schema_only' if not runs else 'ambiguous_runs'),
            'run_ids':runs,'orphan_temp_files':temps}


def authorize(root,record_path):
    errors=audit_canonical(root)
    if errors:raise ValueError('canonical mismatch: '+canonical(errors))
    record=json.loads(safe_path(root,record_path).read_text())
    if record.get('task_id')!='C02-2' or record.get('approved') is not True or record.get('operations')!=['code','tests','backup_restore','production_migration']:
        raise ValueError('C02-2 exact approval scope required')
    if record.get('db')!='state/state.sqlite3':raise ValueError('approval DB path differs')
    if record.get('spec_version')!='v2026-11-14-c00-a1' or set(record.get('spec_sha256',{}))!={'AGENTS.md','spec/MASTER_PLAN.md','spec/NOTE_TEMPLATES.md'}:
        raise ValueError('approval version/spec scope mismatch')
    if record.get('package')!=RESULT or sha256(safe_path(root,RESULT))!=record.get('package_sha256'):
        raise ValueError('approved package changed')
    if set(record.get('input_sha256',{}))!=set(INPUTS):raise ValueError('approval input scope mismatch')
    for path,h in record['input_sha256'].items():
        if sha256(safe_path(root,path))!=h:raise ValueError('approved input changed')
    for path,h in record['spec_sha256'].items():
        if sha256(safe_path(root,path))!=h:raise ValueError('approval spec hash mismatch')
    return record


def run_production(root=ROOT):
    """One deliberately fixed production target and existing C02-1 result."""
    root=Path(root).resolve();record=authorize(root,'checkpoints/c02-2/AUTHORIZATION.json')
    directory=safe_path(root,'checkpoints/c02-2');directory.mkdir(exist_ok=True)
    dbpath=safe_path(root,'state/state.sqlite3');no_sidecars(dbpath)
    journal=directory/'OPERATION.json';backup=directory/'backups/state.pre-c02-2.sqlite3'
    package=json.loads(safe_path(root,RESULT).read_text());inputs=[artifact(root,p,'legacy-file-snapshot') for p in INPUTS]
    def event(stage,**kw):durable_json(journal,{'task_id':'C02-2','version':VERSION,'stage':stage,**kw})
    with (directory/'migration.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        probe=subprocess.run(['/usr/sbin/lsof','-nP','-Fpcn','--',str(dbpath)],capture_output=True,text=True)
        if probe.returncode not in (0,1) or probe.stderr.strip() or probe.stdout.strip():
            raise ValueError('open DB connection or lsof uncertainty; stop: '+probe.stdout+probe.stderr)
        before_hash=sha256(dbpath)
        with closing(sqlite3.connect(dbpath.as_uri()+'?mode=rw',uri=True,timeout=0)) as db:
            db.execute('PRAGMA foreign_keys=ON');check_quiescent(db)
            db.execute('PRAGMA locking_mode=EXCLUSIVE');db.execute('BEGIN EXCLUSIVE')
            try:
                no_sidecars(dbpath)
                if sha256(dbpath)!=before_hash:raise ValueError('DB changed while acquiring lock')
                state=operation_status(db,root,package,RESULT,directory)
                if state['orphan_temp_files'] or state['state'] not in ('not_applied','registered'):
                    raise ValueError('partial/orphan state requires recovery review: '+canonical(state))
                before=baseline(db)
                # Replays require an existing, independently validated backup/rehearsal.
                if state['state']=='registered':
                    previous=json.loads((directory/'BACKUP_RESULT.json').read_text())
                    if sha256(backup)!=previous['backup_sha256']:raise ValueError('backup changed')
                    if before!=previous['baseline']:raise ValueError('legacy data changed since backup')
                else:
                    if before_hash!=record['db_sha256_before']:raise ValueError('approved starting DB hash changed')
                    event('locked_before_backup',db_sha256=before_hash)
                    backup_hash=copy_backup(dbpath,backup)
                    rehearsal=rehearse(backup,root,package,inputs,RESULT)
                    durable_json(directory/'BACKUP_RESULT.json',{'backup_path':str(backup),'backup_sha256':backup_hash,'baseline':before,'rehearsal':rehearsal,'copy_method':'byte copy under EXCLUSIVE lock before first SQL change; fsync; SHA-256; immutable integrity/FK check'})
                    event('backup_and_restore_verified',backup_sha256=backup_hash)
                event('precommit',db_sha256=before_hash,initial_state=state)
                result=apply_connection(db,root,package,inputs,RESULT)
                # Re-hash immutable inputs/registered output immediately before commit.
                for r in inputs:
                    if artifact(root,r['path'],r['version'])!=r:raise ValueError('input changed before commit')
                db.commit()
            except BaseException:
                db.rollback();event('rolled_back',db_sha256=sha256(dbpath));raise
            # Exclusive locking mode retains the lock across commit and replay.
            try:
                event('committed',run_id=result['run_id'])
                verify_run(db,root,package,result['run_id'],result['source_links'],inputs,RESULT)
                if baseline(db)!=before:raise ValueError('postcommit legacy mismatch')
                counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['ana_runs','ana_revisions','ana_edges','ana_locations']}
                db.execute('BEGIN EXCLUSIVE');replay=apply_connection(db,root,package,inputs,RESULT);db.commit()
                if counts!={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in counts}:raise ValueError('replay duplicated rows')
                result.update(version=VERSION,db_sha256_before=before_hash,db_sha256_after=sha256(dbpath),counts=counts,
                    postcommit_verified=True,replay_same_run=replay['run_id']==result['run_id'],initial_state=state)
                durable_json(directory/'SOURCE_LINK_RESULT.json',result['source_links'])
                durable_json(directory/'MIGRATION_RESULT.json',result)
                event('verified',run_id=result['run_id'],db_sha256=sha256(dbpath))
            except BaseException as exc:
                # Retain exclusive connection while restoring logical DB from the
                # verified backup using SQLite backup API; no inode swap/lock gap.
                db.rollback()
                previous=json.loads((directory/'BACKUP_RESULT.json').read_text())
                if sha256(backup)!=previous['backup_sha256']:raise RuntimeError('postcommit failure; backup mismatch; restoration blocked') from exc
                with closing(sqlite3.connect(backup.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as src:
                    src.backup(db)
                integrity(db)
                if baseline(db)!=previous['baseline']:raise RuntimeError('restore verification failed') from exc
                event('restored_after_postcommit_failure',db_sha256=sha256(dbpath));raise
    no_sidecars(dbpath)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-c02-2',action='store_true',required=True)
    parser.parse_args()
    print(canonical(run_production()))
