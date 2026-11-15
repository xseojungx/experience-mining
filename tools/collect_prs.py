"""Collect the approved five GitHub PR inventories, page by page."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone

from tools.state import DB, ROOT
from tools.work_queue import checked_sources, claim, enqueue, finish, record_failure

APPROVAL = "stage3-pr-inventory"
OPERATION = "github-pr-list"
QUERY = """query($owner:String!,$name:String!){
 repository(owner:$owner,name:$name){
 id nameWithOwner url isPrivate
 pullRequests { totalCount }
 }}"""


def fetch(arguments):
    result = subprocess.run(["gh", "api", *arguments], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def snapshot(origin, key, payload):
    envelope = {"fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    content = json.dumps(envelope, ensure_ascii=False, indent=2).encode()
    digest = hashlib.sha256(content).hexdigest()
    from tools.execution_control import file_path
    path = file_path(ROOT,str(Path('data/raw/github')/origin/(digest+'.json')))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
    location = "https://api.github.com/repos/" + origin
    return {"source_key": key, "location": location, "sha256": digest,
            "raw_path": str(path.relative_to(ROOT))}


def run():
    manifest = json.loads((ROOT / "state/projects.json").read_text())
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS pr_inventory (
            origin TEXT NOT NULL, pr_id INTEGER NOT NULL, number INTEGER NOT NULL,
            url TEXT NOT NULL, author TEXT, created_at TEXT, updated_at TEXT,
            state TEXT, merged_at TEXT, page_unit INTEGER REFERENCES queue_units(id),
            PRIMARY KEY(origin,pr_id))""")
        db.execute("INSERT OR IGNORE INTO approvals VALUES (?,?,?)", (
            APPROVAL, "5개 저장소 메타데이터·전 기간 전 작성자 PR 목록 읽기 및 로컬 보관·수집기 구현",
            "2026-11-12 계획 채팅 사용자 메시지 ‘5개 저장소 PR 목록 수집 승인’"))
        for project in manifest["projects"]:
            for repo in project["repositories"]:
                origin = repo["url"].removeprefix("https://github.com/").removesuffix(".git")
                scope = (APPROVAL, project["id"], origin, OPERATION)
                db.execute("INSERT OR IGNORE INTO queue_scopes VALUES (?,?,?,?)", scope)
                enqueue(db, scope, "metadata-before")
        db.commit()
        while True:
            db.execute("BEGIN IMMEDIATE")
            row = claim(db,approval_id=APPROVAL,lease_seconds=300)
            db.commit()
            if row is None:
                break
            try:
                origin, key = row["origin"], row["unit_key"]
                if key.startswith("metadata"):
                    owner, name = origin.split("/")
                    payload = fetch(["graphql", "-f", "query=" + QUERY, "-f", "owner=" + owner, "-f", "name=" + name])
                    if payload.get("errors") or not payload.get("data", {}).get("repository"):
                        raise ValueError("repository metadata unavailable")
                    result = payload["data"]["repository"]
                    next_key = "page:1" if key == "metadata-before" else None
                else:
                    page = int(key.split(":")[1])
                    payload = fetch([f"repos/{origin}/pulls?state=all&sort=created&direction=asc&per_page=100&page={page}"])
                    if not isinstance(payload, list):
                        raise ValueError("PR list expected")
                    result = {"count": len(payload), "ids": [p["id"] for p in payload], "terminal": not payload}
                    next_key = f"page:{page+1}" if payload else "metadata-after"
                source = snapshot(origin, key, payload)
                report = {"sources": [source], "result": result, "next_unit": next_key}
                checked_sources(report)
                db.execute("BEGIN IMMEDIATE")
                if key.startswith("page:"):
                    for pr in payload:
                        db.execute("""INSERT INTO pr_inventory VALUES (?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(origin,pr_id) DO UPDATE SET
                            state=excluded.state,merged_at=excluded.merged_at,
                            updated_at=excluded.updated_at,page_unit=excluded.page_unit""",
                            (origin,pr["id"],pr["number"],pr["html_url"],(pr.get("user") or {}).get("login"),
                             pr["created_at"],pr["updated_at"],pr["state"],pr.get("merged_at"),row["id"]))
                finish(db, row, report)
                db.commit()
                print(origin, key, "saved", flush=True)
            except Exception as error:
                db.rollback()
                db.execute('BEGIN IMMEDIATE')
                record_failure(db,row,error)
                db.commit()
                raise
        summary = []
        for record in db.execute("SELECT DISTINCT origin FROM queue_scopes WHERE approval_id=?", (APPROVAL,)):
            origin = record[0]
            units = list(db.execute("SELECT unit_key,result FROM queue_units WHERE origin=? AND approval_id=? AND state='complete'", (origin,APPROVAL)))
            results = {u[0]:json.loads(u[1]) for u in units}
            before = results["metadata-before"]["pullRequests"]["totalCount"]
            after = results["metadata-after"]["pullRequests"]["totalCount"]
            unique = db.execute("SELECT COUNT(*) FROM pr_inventory WHERE origin=?", (origin,)).fetchone()[0]
            listed = sum(r["count"] for k,r in results.items() if k.startswith("page:"))
            summary.append({"repository":origin,"before":before,"after":after,"unique":unique,
                            "duplicates":listed-unique,"counts_match":before==after==unique,
                            "terminal_page":any(r.get("terminal") for r in results.values())})
        path = ROOT / "state/pr-inventory-summary.json"
        path.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    run()
