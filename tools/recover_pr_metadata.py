"""Recover REST detail outages through GitHub's read-only GraphQL API."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import subprocess

from tools.collect_pr_details import APPROVAL, connect
from tools.collect_prs import snapshot
from tools.work_queue import checked_sources, claim, finish, record_failure, retry

QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
 id number url updatedAt changedFiles
 comments{totalCount} commits{totalCount} reviews{totalCount}
 reviewThreads(first:100){pageInfo{hasNextPage} nodes{comments{totalCount}}}
 }}}"""


def run():
    with connect() as db:
        rows=db.execute("SELECT * FROM queue_units WHERE approval_id=? AND state='pending' AND operation='detail'",(APPROVAL,)).fetchall()
        for old in rows:
            db.execute('BEGIN IMMEDIATE')
            row=claim(db,unit_id=old['id'],lease_seconds=600)
            db.commit()
            if row is None:continue
            try:
                owner,name=row["origin"].split("/")
                number=row["unit_key"].split(":")[0]
                response=subprocess.run(["gh","api","graphql","-f","query="+QUERY,"-f","owner="+owner,
                    "-f","name="+name,"-F","number="+number],capture_output=True,text=True,timeout=90)
                if response.returncode:
                    print("unresolved",row["origin"],number,response.stderr.strip())
                    db.execute('BEGIN IMMEDIATE')
                    retry(db,row['id'],row['token'],'GraphQL unavailable')
                    db.commit()
                    continue
                payload=json.loads(response.stdout)
                if payload.get("errors"):
                    print("unresolved",row["origin"],number,payload["errors"])
                    db.execute('BEGIN IMMEDIATE')
                    retry(db,row['id'],row['token'],'GraphQL unavailable')
                    db.commit()
                    continue
                pr=payload["data"]["repository"]["pullRequest"]
                threads=pr["reviewThreads"]
                source=snapshot(row["origin"],"graphql-detail:"+number,payload)
                source["location"]="https://api.github.com/graphql"
                result={"count":1,"unique":1,"page_count":1,"pagination_completed":True,
                    "provider":"GraphQL fallback after REST 503",
                    "expected":{"comments":pr["comments"]["totalCount"],"commits":pr["commits"]["totalCount"],
                        "changed_files":pr["changedFiles"],
                        "review_comments":None if threads["pageInfo"]["hasNextPage"] else sum(t["comments"]["totalCount"] for t in threads["nodes"])},
                    "updated_at":pr["updatedAt"]}
                report={"sources":[source],"result":result,"next_unit":None}
                checked_sources(report)
                db.execute("BEGIN IMMEDIATE")
                finish(db,row,report)
                db.commit()
                print("recovered",row["origin"],number,flush=True)
            except Exception as error:
                db.rollback()
                db.execute('BEGIN IMMEDIATE')
                record_failure(db,row,error)
                db.commit()
                raise



if __name__=="__main__":
    run()
