"""Audit collected pages and index discussion authors without inferring contribution."""
import hashlib
import json
import sqlite3
from collections import defaultdict

from state import DB, ROOT

APPROVAL = "pr-discussions"


def run():
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT q.*,s.raw_path,s.sha256 FROM queue_units q
            LEFT JOIN unit_sources u ON u.unit_id=q.id
            LEFT JOIN source_versions s ON s.id=u.version_id
            WHERE q.approval_id=? AND q.operation!='commits-full' ORDER BY q.id""", (APPROVAL,)).fetchall()
        groups = defaultdict(dict)
        own = []
        totals = defaultdict(lambda:defaultdict(int))
        hash_errors = []
        absent_patches = []
        for row in rows:
            number,kind=row["unit_key"].split(":")
            group=groups[(row["origin"],number)]
            group[kind] = None
            if row["state"]!="complete":
                continue
            path=ROOT/row["raw_path"]
            content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=row["sha256"]:
                hash_errors.append(row["raw_path"])
                continue
            envelope=json.loads(content)
            payload=envelope["payload"]
            records=[payload] if kind=="detail" else [item for page in payload for item in page]
            if kind=="files":
                for item in records:
                    if "patch" not in item:
                        absent_patches.append({"repository":row["origin"],"pr":int(number),
                            "filename":item["filename"],"status":item["status"],
                            "reason":"API patch absent, binary or truncation not yet determined",
                            "source":row["raw_path"]})
            result=json.loads(row["result"])
            if kind=="files":
                result["unique"]=len({item["filename"] for item in records})
            group[kind]=result
            totals[row["origin"]][kind]+=len(records)
            if kind in ("reviews","comments","issue_comments"):
                for item in records:
                    if (item.get("user") or {}).get("login","").lower()=="xseojungx":
                        own.append({"repository":row["origin"],"pr":int(number),"kind":kind,
                            "id":item["id"],"url":item.get("html_url"),"author":item["user"]["login"],
                            "in_reply_to_id":item.get("in_reply_to_id"),"review_id":item.get("pull_request_review_id"),
                            "source":row["raw_path"]})
        gaps=[]
        for (origin,number),kinds in groups.items():
            missing=[k for k,v in kinds.items() if v is None]
            if missing:
                gaps.append({"repository":origin,"pr":int(number),"missing":missing})
                continue
            supplement=db.execute("SELECT result FROM queue_units WHERE origin=? AND unit_key=? AND operation='commits-full' AND state='complete'",(origin,number)).fetchone()
            if supplement:
                corrected=json.loads(supplement[0])
                totals[origin]["commits"]+=corrected["count"]-kinds["commits"]["count"]
                kinds["commits"]=corrected
            expected=kinds["detail"]["expected"]
            differences={}
            for kind,field in (("issue_comments","comments"),("comments","review_comments"),
                               ("commits","commits"),("files","changed_files")):
                if kinds[kind]["count"]!=expected[field]:
                    differences[kind]={"expected":expected[field],"received":kinds[kind]["count"]}
            duplicates={k:v["count"]-v["unique"] for k,v in kinds.items() if v["count"]!=v["unique"]}
            if differences or duplicates:
                gaps.append({"repository":origin,"pr":int(number),"differences":differences,"duplicates":duplicates})
        summary={"prs":len(groups),"complete_units":sum(r["state"]=="complete" for r in rows),
            "total_units":len(rows),"counts":dict(totals),"hash_errors":hash_errors,"gaps":gaps,
            "limits":"REST files/commits limits and omitted patches require explicit follow-up when counts differ or patch is absent.",
            "own_discussion_records":len(own),"absent_patch_count":len(absent_patches)}
        (ROOT/"state/absent-patches.json").write_text(json.dumps(absent_patches,ensure_ascii=False,indent=2)+"\n")
        (ROOT/"state/pr-details-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
        (ROOT/"state/own-discussion-index.json").write_text(json.dumps(own,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({k:v for k,v in summary.items() if k!="gaps"},ensure_ascii=False))
        print("gap_prs",len(gaps))


if __name__ == "__main__":
    run()
