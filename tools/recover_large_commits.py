"""Read all commits of the PR whose REST list hit its limit."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import subprocess

from tools.collect_pr_details import APPROVAL, connect
from tools.collect_prs import snapshot
from tools.work_queue import checked_sources, claim, enqueue, finish, record_failure

QUERY="""query($cursor:String){repository(owner:"INFLUY",name:"Influy-FE"){
 pullRequest(number:97){commits(first:100,after:$cursor){totalCount
 pageInfo{hasNextPage endCursor}
 nodes{commit{oid url message committedDate authoredDate
 author{name email user{login}} committer{name email user{login}}}}}}}}"""
def run():
 with connect() as db:
  scope=(APPROVAL,"influy","INFLUY/Influy-FE","commits-full")
  db.execute("INSERT OR IGNORE INTO queue_scopes VALUES (?,?,?,?)",scope)
  enqueue(db,scope,"97")
  db.commit()
  row=db.execute("SELECT * FROM queue_units WHERE approval_id=? AND operation='commits-full'",(APPROVAL,)).fetchone()
  if row["state"]!="complete":
   db.execute('BEGIN IMMEDIATE')
   row=claim(db,unit_id=row['id'],lease_seconds=3600)
   db.commit()
   if row is None:return
   try:
    detail=db.execute("SELECT s.raw_path FROM queue_units q JOIN unit_sources u ON u.unit_id=q.id JOIN source_versions s ON s.id=u.version_id WHERE q.origin='INFLUY/Influy-FE' AND q.unit_key='97:detail' AND q.approval_id=?",(APPROVAL,)).fetchone()
    from tools.execution_control import active, file_path
    from tools.state import ROOT
    db.execute('BEGIN IMMEDIATE')
    _,_,contract=active(db,row['id'],row['token'],ROOT)
    if detail is None or detail[0] not in contract['inputs']:
     raise ValueError('PR detail input is outside authorized recovery contract')
    pr=json.loads(file_path(ROOT,detail[0]).read_text())['payload']
    db.commit()
    sources=[]
    ids=[]
    page=1
    while True:
     endpoint=f"repos/INFLUY/Influy-FE/compare/{pr['base']['sha']}...{pr['head']['sha']}?per_page=100&page={page}"
     response=subprocess.run(["gh","api",endpoint],capture_output=True,text=True,timeout=90)
     if response.returncode: raise RuntimeError(response.stderr)
     payload=json.loads(response.stdout)
     source=snapshot("INFLUY/Influy-FE","pr:97:compare-commits:"+str(page),payload)
     source["location"]="https://api.github.com/"+endpoint
     sources.append(source)
     ids.extend(n["sha"] for n in payload["commits"])
     if not payload["commits"]: break
     page+=1
    if len(set(ids))!=pr["commits"] or payload["total_commits"]!=pr["commits"]:
     raise ValueError(f"compare count mismatch: {len(set(ids))} versus {pr['commits']}")
    report={"sources":sources,"result":{"count":len(ids),"unique":len(set(ids)),"page_count":len(sources),"pagination_completed":True},"next_unit":None}
    checked_sources(report)
    db.execute('BEGIN IMMEDIATE')
    finish(db,row,report)
    db.commit()
    print(report["result"])
   except Exception as error:
    db.rollback()
    db.execute('BEGIN IMMEDIATE')
    record_failure(db,row,error)
    db.commit()
    raise



if __name__=="__main__":
 run()
