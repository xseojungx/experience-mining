"""Common collection/analysis queue. Each unit_key identifies one approved page or batch.

enqueue takes approval_id, project, origin, operation, unit_key. finish reads JSON
from stdin: {"sources":[{"source_key":...,"location":...,"raw_path":...,"sha256":...}],
"result":{...},"next_unit":null}. raw_path is workspace-relative under data/raw.
next_unit is the next page key within the same approved scope.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

try:
    from . import execution_control as control
    from .state import DB, ROOT
except ImportError:  # historical script entry points
    from state import DB, ROOT
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from tools import execution_control as control
import time

NOW = "CAST(strftime('%s','now') AS INTEGER)"
ELIGIBLE = f"(state='pending' OR (state='running' AND lease_until <= {NOW}))"


def enqueue(db, scope, key, *, contract=None, grant_id=None, dependencies=(), root=None):
    control.atomic(db)
    old = db.execute("SELECT id,state FROM queue_units WHERE approval_id=? AND project=? AND origin=? AND operation=? AND unit_key=?", (*scope,key)).fetchone()
    # Historical completed collection registration is an exact no-op only.
    if old and old[1]=='complete' and contract is None:
        return old[0]
    if contract is None:
        if old:
            control.check(db,old[0],root or ROOT,dependencies=False)
            return old[0]
        raise ValueError('new/resumed work requires explicit grant and execution contract')
    return control.register(db,scope,key,contract,grant_id,root or ROOT,dependencies=dependencies)


def claim(db, *, approval_id=None, unit_id=None, root=None, lease_seconds=900, now=None):
    control.atomic(db)
    if type(lease_seconds) is not int or not 1<=lease_seconds<=3600:
        raise ValueError('lease must be 1..3600 seconds')
    now=int(time.time()) if now is None else now
    query="SELECT id FROM queue_units WHERE (state='pending' OR (state='running' AND lease_until<=?))"
    params=[now]
    if approval_id is not None:query+=' AND approval_id=?';params.append(approval_id)
    if unit_id is not None:query+=' AND id=?';params.append(unit_id)
    for ident, in db.execute(query+' ORDER BY attempts,id',params).fetchall():
        # Fail closed on absent contracts; do not infer old approval prose.
        q,j,c=control.check(db,ident,root or ROOT,now=now,dependencies=False)
        try:
            for pred, in db.execute('SELECT predecessor FROM ctl_dependencies WHERE unit_id=?',(ident,)):
                control.audit_completion(db,pred,root or ROOT)
        except (ValueError,OSError):
            if unit_id is not None:raise
            continue
        if j['disposition']!='active' or j['next_attempt']>now:continue
        if q['attempts']>=j['max_attempts']:
            db.execute("UPDATE ctl_jobs SET disposition='failed',resume_condition='explicit retry policy review required' WHERE unit_id=?",(ident,))
            db.execute("UPDATE queue_units SET state='pending',token=NULL,lease_until=NULL,last_error='retry limit exhausted' WHERE id=?",(ident,))
            control.event(db,ident,'exhausted',{'reason':'expired claim/retry limit'})
            continue
        token=uuid.uuid4().hex
        db.execute("UPDATE queue_units SET state='running',token=?,lease_until=?,attempts=attempts+1 WHERE id=?",(token,now+lease_seconds,ident))
        control.event(db,ident,'claimed',{'token':token,'lease_until':now+lease_seconds,'recovered_expired':q['state']=='running'})
        return control.rowdict(db.execute('SELECT * FROM queue_units WHERE id=?',(ident,)))
    return None


def renew(db, ident, token, *, root=None, seconds=900, now=None):
    control.active(db,ident,token,root or ROOT,now=now)
    if type(seconds) is not int or not 1<=seconds<=3600:raise ValueError('invalid lease duration')
    now=int(time.time()) if now is None else now
    db.execute('UPDATE queue_units SET lease_until=? WHERE id=?',(now+seconds,ident))
    control.event(db,ident,'renewed',{'lease_until':now+seconds})


def retry(db, ident, token, reason, *, root=None, permanent=False, hold=False, resume_condition=None, now=None):
    q,j,c=control.active(db,ident,token,root or ROOT,now=now)
    if not reason.strip() or ((hold or permanent) and not resume_condition):raise ValueError('reason/resume condition required')
    now=int(time.time()) if now is None else now
    disposition='held' if hold else ('failed' if permanent or q['attempts']>=j['max_attempts'] else 'active')
    delay=min(3600,10*2**min(q['attempts']-1,9))
    db.execute("UPDATE queue_units SET state='pending',token=NULL,lease_until=NULL,last_error=? WHERE id=?",(reason,ident))
    db.execute('UPDATE ctl_jobs SET disposition=?,next_attempt=?,resume_condition=? WHERE unit_id=?',(disposition,now+delay,resume_condition or ('retry limit review required' if disposition=='failed' else None),ident))
    control.event(db,ident,'failed' if disposition=='failed' else 'interrupted',{'reason':reason,'disposition':disposition,'retry_at':now+delay,'resume_condition':resume_condition})


def resume(db, ident, reason, *, root=None):
    control.atomic(db)
    q,j,c=control.check(db,ident,root or ROOT)
    if q['state']=='complete' or j['disposition']!='held' or not reason.strip():raise ValueError('only an explicitly resolved hold can resume')
    db.execute("UPDATE ctl_jobs SET disposition='active',resume_condition=NULL,next_attempt=0 WHERE unit_id=?",(ident,))
    control.event(db,ident,'resumed',{'reason':reason})


def record_failure(db,row,error,*,root=None):
    """Durable failure cause even if the lease or authorization became invalid.
    Caller owns a new transaction after rolling back any partial result writes.
    Never release another worker's token or infer permission to rerun.
    """
    control.atomic(db)
    if not db.execute('SELECT 1 FROM ctl_jobs WHERE unit_id=?',(row['id'],)).fetchone():raise ValueError('unregistered failed work')
    control.event(db,row['id'],'worker_error',{'token':row.get('token') if isinstance(row,dict) else row['token'],'reason':str(error)})
    try:
        retry(db,row['id'],row['token'],str(error),root=root)
    except (ValueError,OSError):
        pass  # expiry, changed inputs, revocation or a new owner: preserve state


def checked_sources(report, *, root=None):
    if not isinstance(report, dict) or not isinstance(report.get("sources"), list):
        raise ValueError("sources array required")
    if not isinstance(report.get("result"), dict) or "next_unit" not in report:
        raise ValueError("result object and next_unit required")
    for record in report["sources"]:
        for field in ("source_key", "location", "raw_path", "sha256"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"missing source field: {field}")
        raw = Path(record["raw_path"])
        path = ((root or ROOT) / raw).resolve()
        if raw.is_absolute() or ((root or ROOT) / "data/raw").resolve() not in path.parents:
            raise ValueError("expected a snapshot under data/raw")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise ValueError(f"snapshot hash mismatch: {raw}")


def finish(db, row, report, *, root=None):
    q,j,c=control.active(db,row['id'],row['token'],root or ROOT)
    if c['domain']!='collection':raise ValueError('analysis requires controlled completion')
    checked_sources(report,root=root)
    if not report['sources']:raise ValueError('collection requires snapshots')
    for rec in report['sources']:
        if not Path(rec['raw_path']).is_relative_to(Path('data/raw/github')) and not Path(rec['raw_path']).is_relative_to(Path('data/raw/git')):
            raise ValueError('collection source boundary')
    grant=control.rowdict(db.execute('SELECT scope FROM ctl_grants WHERE id=?',(j['grant_id'],)))
    output_roots=json.loads(grant['scope'])['output_roots']
    for rec in report['sources']:
        raw=Path(rec['raw_path'])
        if not any(raw.is_relative_to(Path(p)) for p in output_roots) or not raw.is_relative_to(Path('data/raw/github')/q['origin']) and not raw.is_relative_to(Path('data/raw/git')/q['origin']):
            raise ValueError('snapshot output outside exact approved repository/root')
        control.file_path(root or ROOT,rec['raw_path'])
    row=q
    changed_sources=[]
    for record in report["sources"]:
        identity = (row["origin"], record["source_key"], record["sha256"])
        changed_sources.extend(r[0] for r in db.execute('SELECT id FROM source_versions WHERE origin=? AND source_key=? AND sha256<>?',identity))
        prior=db.execute('SELECT raw_path,location FROM source_versions WHERE origin=? AND source_key=? AND sha256=?',identity).fetchone()
        if prior is not None and tuple(prior)!=(record['raw_path'],record['location']):raise ValueError('source identity/path conflict')
        db.execute("""INSERT INTO source_versions(origin,source_key,sha256,location,raw_path)
                      VALUES (?,?,?,?,?) ON CONFLICT(origin,source_key,sha256) DO NOTHING""",
                   (*identity, record["location"], record["raw_path"]))
        db.execute("""INSERT INTO unit_sources(unit_id,version_id)
                      SELECT ?,id FROM source_versions WHERE origin=? AND source_key=? AND sha256=?
                      ON CONFLICT(unit_id,version_id) DO NOTHING""", (row["id"], *identity))
    if changed_sources:
        from tools.analysis_runtime import invalidate_source
        for sid in set(changed_sources):invalidate_source(db,sid,'source:'+str(row['id'])+':'+str(sid),'new registered source version')
    if report["next_unit"] is not None:
        scope = tuple(row[field] for field in ("approval_id", "project", "origin", "operation"))
        if report["next_unit"] == row["unit_key"]:
            raise ValueError("next page key repeats the current page")
        enqueue(db, scope, report['next_unit'],contract=c,grant_id=j['grant_id'],root=root)
    artifacts={r['raw_path']:r['sha256'] for r in report['sources']}
    # Collection files are immutable raw snapshots, not analysis output paths.
    payload={'project':row['project'],'candidate_id':c['target'],'target_revision':c['target_revision'],
             'observations':[],'hypotheses':[],'evidence_ids':[r['source_key'] for r in report['sources']],
             'unknowns':[],'next_tasks':[]}
    control.handoff(c,row['project'],payload)
    db.execute('INSERT INTO ctl_checkpoints(unit_id,result_digest,artifacts,handoff) VALUES(?,?,?,?)',
               (row['id'],control.digest(report['result']),control.canonical(artifacts),control.canonical(payload)))
    db.execute("UPDATE queue_units SET state='complete',token=NULL,lease_until=NULL,result=?,last_error=NULL WHERE id=?",(control.canonical(report['result']),row['id']))
    control.event(db,row['id'],'complete',{'sources':artifacts})



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enqueue")
    for name in ("approval_id", "project", "origin", "operation", "unit_key"):
        add.add_argument(name)
    add.add_argument('--contract', type=Path, required=True, help='JSON {contract,grant_id,dependencies}; never grants authorization')
    commands.add_parser("claim", help="claim one unit for 15 minutes")
    renew_parser=commands.add_parser('renew')
    renew_parser.add_argument('id',type=int);renew_parser.add_argument('token')
    commands.add_parser('audit',help='read-only artifact/checkpoint reconciliation')
    resume_parser=commands.add_parser('resume')
    resume_parser.add_argument('id',type=int);resume_parser.add_argument('reason')
    analysis=commands.add_parser('analysis-finish')
    analysis.add_argument('id',type=int);analysis.add_argument('token')
    for name in ("finish", "retry"):
        sub = commands.add_parser(name)
        sub.add_argument("id", type=int)
        sub.add_argument("token")
        if name == "retry":
            sub.add_argument("reason")
    args = parser.parse_args()
    if args.command=='audit':
        from tools.analysis_runtime import audit_files
        with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as audit_db:
            audit_db.execute('BEGIN')
            findings=audit_files(audit_db,ROOT,include_collection=True)
            for ident, in audit_db.execute('SELECT unit_id FROM ctl_checkpoints'):
                try:control.audit_completion(audit_db,ident,ROOT)
                except (ValueError,OSError) as error:findings.append({'unit_id':ident,'error':str(error)})
            print(json.dumps({'findings':findings,'clean':not findings},ensure_ascii=False))
        return int(bool(findings))
    report = json.load(sys.stdin) if args.command in ("finish","analysis-finish") else None
    with closing(sqlite3.connect(DB.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone():
            from tools.analysis_runtime import reconcile
            reconcile(db,ROOT)
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        if args.command == "enqueue":
            cfg=json.loads(args.contract.read_text())
            enqueue(db, (args.approval_id, args.project, args.origin, args.operation), args.unit_key,**cfg)
        elif args.command == 'resume':
            resume(db,args.id,args.reason)
        elif args.command == "claim":
            print_result = claim(db)
            db.commit()
            print(json.dumps(print_result, ensure_ascii=False))
        else:
            row = db.execute(f"""SELECT * FROM queue_units WHERE id=? AND token=?
                                 AND state='running' AND lease_until > {NOW}""", (args.id, args.token)).fetchone()
            if row is None:
                raise ValueError("no active claim for this id and token")
            try:
                if args.command == 'renew':
                    renew(db,args.id,args.token)
                elif args.command == 'analysis-finish':
                    from tools.analysis_runtime import complete
                    complete(db,args.id,args.token,report['request'],report['handoff'],ROOT)
                elif args.command == "finish":
                    checked_sources(report)
                    finish(db, row, report)
                else:
                    retry(db,args.id,args.token,args.reason)
            except Exception as error:
                db.rollback()
                db.execute('BEGIN IMMEDIATE')
                record_failure(db,row,error)
                db.commit()
                raise



if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error) as error:
        sys.exit(str(error))
