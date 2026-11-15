"""Local workspace state. init records the approved baseline; status is read-only."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
DB = STATE / "state.sqlite3"
MANIFEST = STATE / "projects.json"
PRIOR = "checkpoints/2026-11-12-workflow-guidance.md"
OUTPUTS = ["state/projects.json", "state/schema.sql", "state/state.sqlite3", "tools/state.py"]


def initialize():
    with closing(sqlite3.connect(DB)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("BEGIN IMMEDIATE;\n" + (STATE / "schema.sql").read_text())
        if db.execute("SELECT 1 FROM checkpoints LIMIT 1").fetchone():
            return
        db.execute("INSERT INTO approvals VALUES (?, ?, ?)", (
            "stage1", "워크스페이스 state/ 및 tools/ 내 로컬 상태 관리 구현",
            "2026-11-12 계획 채팅, ‘1단계 로컬 상태 구현 승인’ 안내 직후 사용자 답변 ‘승인.’",
        ))
        db.executemany("INSERT INTO work_items VALUES (?, ?, ?, ?)", [
            ("local-state", "로컬 상태 기반 구현", "complete", "stage1"),
            ("project-inputs", "프로젝트별 저장소·기간·별칭 등 입력 확인", "pending", None),
        ])
        inputs = ["AGENTS.md", "spec/MASTER_PLAN.md", "spec/NOTE_TEMPLATES.md", PRIOR, OUTPUTS[0]]
        details = {
            "stage": "로컬 상태 기반 구현 완료",
            "prior_checkpoint": PRIOR,
            "state_version": 1,
            "input_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in inputs},
            "analysis_version": None, "model_version": None, "prompt_version": None,
            "source_ids": [], "source_locations": [], "source_hashes": [], "analysis_results": [],
            "outputs": OUTPUTS, "cursor": None, "failures": [],
            "open_inputs": ["저장소 URL", "spitching 경로 또는 URL", "기간·계정·커밋 별칭",
                            "프로젝트별 주의사항", "ChatGPT 내보내기 위치·읽기 범위"],
            "resume_condition": "사용자 제공 입력을 확인한다. 원본 열람·수집·분석·실험은 해당 범위 승인 후 진행한다.",
        }
        db.execute("INSERT INTO checkpoints(completed_id, next_id, details) VALUES (?, ?, ?)",
                   ("local-state", "project-inputs", json.dumps(details, ensure_ascii=False)))


def status():
    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        checkpoint = dict(db.execute("SELECT * FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone())
        checkpoint["details"] = json.loads(checkpoint["details"])
        controls = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ctl_migrations'").fetchone():
            controls = {
                'version': [dict(r) for r in db.execute('SELECT * FROM ctl_migrations')],
                'job_dispositions': {r[0]:r[1] for r in db.execute('SELECT disposition,count(*) FROM ctl_jobs GROUP BY disposition')},
                'revalidation': {r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM ctl_revalidation GROUP BY state')},
                'stale_heads': db.execute('SELECT count(*) FROM ctl_heads WHERE stale=1').fetchone()[0],
                'current_publications': db.execute('SELECT count(*) FROM ctl_publications WHERE valid=1').fetchone()[0],
                'uncontracted_incomplete_units': db.execute("SELECT count(*) FROM queue_units q LEFT JOIN ctl_jobs j ON j.unit_id=q.id WHERE q.state<>'complete' AND j.unit_id IS NULL").fetchone()[0],
            }
        return {
            'execution_controls': controls,
            "manifest": json.loads(MANIFEST.read_text()),
            "approvals": [dict(row) for row in db.execute("SELECT * FROM approvals ORDER BY id")],
            "work_items": [dict(row) for row in db.execute("SELECT * FROM work_items ORDER BY id")],
            "queue_counts": {row["state"]: row["n"] for row in db.execute(
                "SELECT state, COUNT(*) AS n FROM queue_units GROUP BY state")},
            "next_queue_unit": next((dict(row) for row in db.execute("""
                SELECT id,project,origin,operation,unit_key,attempts,last_error FROM queue_units
                WHERE state='pending' OR (state='running' AND lease_until <= CAST(strftime('%s','now') AS INTEGER))
                ORDER BY attempts,id LIMIT 1""")), None),
            "registered_versions": db.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0],
            "checkpoint": checkpoint,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "status"])
    if parser.parse_args().command == "init":
        initialize()
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
