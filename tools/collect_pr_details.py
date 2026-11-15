"""Read approved PR details using resumable collection units."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib
import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from tools.state import DB, ROOT
from tools.work_queue import checked_sources, claim, enqueue, finish, record_failure

APPROVAL = "pr-discussions"
KINDS = ("detail", "reviews", "comments", "issue_comments", "commits", "files")


def connect():
    db = sqlite3.connect(DB, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def collect(worker):
    with connect() as db:
        while True:
            db.execute("BEGIN IMMEDIATE")
            row = claim(db,approval_id=APPROVAL,lease_seconds=600)
            db.commit()
            if row is None:
                return
            number, kind = row["unit_key"].split(":")
            if not number.isdigit() or kind!=row['operation'] or kind not in KINDS:
                raise ValueError('unit route is outside authorized collection operation')
            endpoint = f"repos/{row['origin']}/pulls/{number}"
            if kind == "issue_comments":
                endpoint = f"repos/{row['origin']}/issues/{number}/comments"
            elif kind != "detail":
                endpoint += "/" + kind
            try:
                args = ["gh","api",endpoint]
                if kind != "detail":
                    args[-1] += "?per_page=100"
                    args += ["--paginate","--slurp"]
                response = subprocess.run(args,capture_output=True,text=True,timeout=240)
                if response.returncode:
                    raise RuntimeError(response.stderr.strip())
                payload = json.loads(response.stdout)
                if kind != "detail" and (not isinstance(payload,list) or any(not isinstance(p,list) for p in payload)):
                    raise ValueError("expected paginated arrays")
                entries = [payload] if kind=="detail" else [item for page in payload for item in page]
                identities = [item["filename"] if kind=="files" else item.get("id",item.get("sha")) for item in entries]
                result = {"count":len(entries),"unique":len(set(identities)),
                          "page_count":1 if kind=="detail" else len(payload),
                          "pagination_completed":True}
                if kind == "detail":
                    result["expected"] = {k:payload.get(k) for k in ("comments","review_comments","commits","changed_files")}
                    result["updated_at"] = payload.get("updated_at")
                envelope = {"endpoint":args[2],"fetched_at":datetime.now(timezone.utc).isoformat(),"payload":payload}
                content = json.dumps(envelope,ensure_ascii=False).encode()
                digest = hashlib.sha256(content).hexdigest()
                from tools.execution_control import file_path
                path=file_path(ROOT,str(Path('data/raw/github')/row['origin']/(digest+'.json')))
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(content)
                source = {"source_key":"pr:"+row["unit_key"],"location":"https://api.github.com/"+args[2],
                          "sha256":digest,"raw_path":str(path.relative_to(ROOT))}
                report = {"sources":[source],"result":result,"next_unit":None}
                checked_sources(report)
                db.execute("BEGIN IMMEDIATE")
                finish(db,row,report)
                db.commit()
                if row["id"] % 50 == 0:
                    print("saved",row["origin"],row["unit_key"],flush=True)
            except Exception as error:
                db.rollback()
                db.execute('BEGIN IMMEDIATE')
                record_failure(db,row,error)
                db.commit()
                print("retry",row["origin"],row["unit_key"],str(error)[:200],flush=True)
                time.sleep(10)


def run():
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO approvals VALUES (?,?,?)",(APPROVAL,
            "404개 PR 리뷰·라인 댓글·답글·일반 댓글·커밋 목록·파일 diff 읽기 및 로컬 보관·수집기 구현",
            "현재 계획 채팅 ‘그 다음이 리뷰·댓글 수집할 차례인가?’ 안내 후 사용자 ‘수집 진행해’"))
        for row in db.execute("""SELECT p.*,s.project FROM pr_inventory p JOIN queue_scopes s
             ON s.origin=p.origin AND s.approval_id='stage3-pr-inventory'""").fetchall():
            for kind in KINDS:
                scope=(APPROVAL,row["project"],row["origin"],kind)
                db.execute("INSERT OR IGNORE INTO queue_scopes VALUES (?,?,?,?)",scope)
                enqueue(db,scope,f"{row['number']}:{kind}")
        db.commit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(collect,range(4)))


if __name__ == "__main__":
    run()
