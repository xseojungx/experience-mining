"""C01 read-only diagnostics. Stdout only; never initializes/repairs a database.

python3 tools/audit_state.py [--root PATH] [--db state/state.sqlite3]
    [--bundle tests/fixtures/contract-valid.json]
Exit: 0 clean, 1 invalid/legacy-incompatible, 2 stopped (unsafe/unreadable input).
Only explicit local allowlisted files are opened. ChatGPT exports are never read.
SQLite immutable read-only mode is used ONLY without journal/WAL sidecars;
otherwise stop rather than ignore uncheckpointed data or create lock files.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

try:
    from .analysis_contract import RUBRIC, SHA, SPEC_VERSION, STAGES, issue, validate_bundle
except ImportError:
    from analysis_contract import RUBRIC, SHA, SPEC_VERSION, STAGES, issue, validate_bundle

CANONICAL = "spec/revisions/2026-11-14-c00-a1/OUTPUT_MANIFEST.json"
SPEC_FILES = ("AGENTS.md", "spec/MASTER_PLAN.md", "spec/NOTE_TEMPLATES.md")


class AuditStop(ValueError):
    pass


def safe_path(root, relative):
    """Lexical allowlist and resolved-path containment; reject symlinks too."""
    if not isinstance(relative, str):
        raise AuditStop("path must be a string")
    rel = Path(relative)
    parts = rel.parts
    allowed = (relative == "AGENTS.md" or (parts and parts[0] in ("spec", "state", "tools", "checkpoints", "tests", "vault", "results"))
               or parts[:3] in (("data", "raw", "github"), ("data", "raw", "git"), ("data", "raw", "user")))
    if rel.is_absolute() or ".." in parts or not allowed or any("chatgpt" in x.lower() for x in parts):
        raise AuditStop("unapproved path: " + relative)
    path = root / rel
    if not path.resolve().is_relative_to(root.resolve()):
        raise AuditStop("path escapes workspace: " + relative)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise AuditStop("symlink input refused: " + relative)
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(root, relative):
    return json.loads(safe_path(root, relative).read_text(encoding="utf-8"))


def check_hashes(root, ledger, *, severity="error"):
    findings = []
    if not isinstance(ledger, dict):
        return [issue("ledger", "$", "hash object required")]
    for relative, expected in ledger.items():
        path = safe_path(root, relative)
        digest = expected.get("sha256") if isinstance(expected, dict) else expected
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            findings.append(issue("hash_format", relative, "expected SHA-256"))
        elif not path.is_file():
            findings.append(issue("missing_file", relative, "referenced artifact absent", severity))
        elif sha256(path) != digest:
            findings.append(issue("hash_mismatch", relative, "bytes differ from recorded digest", severity))
    return findings


def audit_canonical(root):
    manifest = load_json(root, CANONICAL)
    if not isinstance(manifest, dict):
        raise AuditStop("canonical manifest object required")
    errors = []
    if manifest.get("spec_version") != SPEC_VERSION or manifest.get("canonical_applied") is not True:
        errors.append(issue("canonical", CANONICAL, "canonical version/application mismatch"))
    ledger = manifest.get("files")
    if not isinstance(ledger, dict) or not all(p in ledger for p in SPEC_FILES):
        raise AuditStop("canonical file ledger missing")
    errors.extend(check_hashes(root, ledger))
    checkpoint = manifest.get("checkpoint")
    if checkpoint not in ledger:
        errors.append(issue("checkpoint", CANONICAL, "checkpoint not bound to canonical manifest"))
    if SPEC_VERSION not in safe_path(root, "spec/MASTER_PLAN.md").read_text():
        errors.append(issue("canonical", "spec/MASTER_PLAN.md", "version marker missing"))
    return errors



def audit_authorization(root):
    """Check the local C01 authorization record, not the authenticity of chat text.

    This does not grant access or replace user approval, and cannot be reused for
    C02, migration, experiments, classification, publication or collection.
    """
    path = "checkpoints/c01/AUTHORIZATION.json"
    record = load_json(root, path)
    findings = []
    if not isinstance(record, dict):
        raise AuditStop("C01 authorization object required")
    required = {"implement_c01", "synthetic_tests", "temporary_sqlite_tests", "read_only_state_audit"}
    if (record.get("task_id") != "C01" or record.get("approved") is not True
            or record.get("spec_version") != SPEC_VERSION
            or not isinstance(record.get("source"), str) or not record["source"].strip()
            or not isinstance(record.get("operations"), list)
            or set(record["operations"]) != required):
        findings.append(issue("authorization", path, "C01 task/version/exact operation scope/source mismatch"))
    hashes = record.get("spec_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(SPEC_FILES):
        findings.append(issue("authorization", path, "approval must bind canonical spec/template hashes"))
    else:
        findings.extend(check_hashes(root, hashes))
    return findings

def audit_database(root, relative="state/state.sqlite3", expected=None):
    path = safe_path(root, relative)
    before = sha256(path)
    sidecars = [Path(str(path) + s) for s in ("-wal", "-shm", "-journal")]
    if any(p.exists() for p in sidecars):
        raise AuditStop("SQLite sidecar present; require a quiescent database before immutable audit")
    findings, snapshot = [], {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if [r[0] for r in db.execute("PRAGMA quick_check")] != ["ok"]:
                findings.append(issue("sqlite_integrity", relative, "quick_check failed"))
            for row in db.execute("PRAGMA foreign_key_check"):
                findings.append(issue("foreign_key", relative, str(tuple(row))))
            approvals = {r["id"]: dict(r) for r in db.execute("SELECT * FROM approvals")}
            work = {r["id"]: dict(r) for r in db.execute("SELECT * FROM work_items")}
            scopes = {tuple(r) for r in db.execute("SELECT approval_id,project,origin,operation FROM queue_scopes")}
            sources = {r["id"]: dict(r) for r in db.execute("SELECT * FROM source_versions")}
            links = {}
            for r in db.execute("SELECT * FROM unit_sources"):
                links.setdefault(r["unit_id"], []).append(r["version_id"])
            units = {r["id"]: dict(r) for r in db.execute("SELECT * FROM queue_units")}
            for ident, approval in approvals.items():
                if not all(isinstance(approval[k], str) and approval[k].strip() for k in ("scope", "source")):
                    findings.append(issue("approval", ident, "approval scope/source missing"))
            for row in work.values():
                if row["status"] not in ("pending", "complete"):
                    findings.append(issue("work_state", row["id"], "invalid work item state"))
                if row["status"] == "complete" and row["approval_id"] not in approvals:
                    findings.append(issue("approval", row["id"], "completed item has no linked approval; authorization cannot be established from this row"))
            for scope in scopes:
                if scope[0] not in approvals:
                    findings.append(issue("approval", str(scope), "scope lacks approval"))
            for ident, row in units.items():
                p = f"queue_units.{ident}"
                scope = tuple(row[k] for k in ("approval_id", "project", "origin", "operation"))
                if scope not in scopes:
                    findings.append(issue("scope", p, "exact approved queue scope missing"))
                state = row["state"]
                if state not in ("pending", "running", "complete"):
                    findings.append(issue("queue_state", p, "invalid queue state"))
                if state == "running" and (not row["token"] or not isinstance(row["lease_until"], int)):
                    findings.append(issue("lease", p, "running claim incomplete"))
                if state != "running" and (row["token"] is not None or row["lease_until"] is not None):
                    findings.append(issue("lease", p, "inactive queue unit has claim"))
                if state == "complete":
                    try:
                        result = json.loads(row["result"])
                        if not isinstance(result, dict):
                            raise ValueError("result object required")
                    except (ValueError, TypeError):
                        findings.append(issue("queue_result", p, "complete unit lacks valid result object"))
                    if not links.get(ident):
                        findings.append(issue("queue_source", p, "complete unit lacks source links"))
            for unit_id, source_ids in links.items():
                if unit_id not in units or any(s not in sources for s in source_ids):
                    findings.append(issue("source_link", str(unit_id), "dangling unit/source link"))
            # No raw JSON parsing: hash only the registered, allowlisted snapshots.
            for ident, source in sources.items():
                findings.extend(check_hashes(root, {source["raw_path"]: source["sha256"]}))
                if not all(isinstance(source[k], str) and source[k].strip() for k in ("origin", "source_key", "location")):
                    findings.append(issue("source_identity", str(ident), "incomplete source identity"))
            row = db.execute("SELECT * FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()
            if row is None:
                findings.append(issue("checkpoint", relative, "no checkpoint"))
            else:
                cp = dict(row)
                details = json.loads(cp.pop("details"))
                if not isinstance(details, dict):
                    raise AuditStop("checkpoint details object required")
                snapshot = {**cp, "analysis_version": details.get("analysis_version"), "cursor": details.get("cursor"),
                            "queue_counts": {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM queue_units GROUP BY state")},
                            "source_versions": len(sources), "approval_ids": sorted(approvals)}
                if cp["completed_id"] not in work or work[cp["completed_id"]]["status"] != "complete":
                    findings.append(issue("checkpoint", relative, "last completed work item disagrees"))
                if cp["next_id"] is not None and (cp["next_id"] not in work or work[cp["next_id"]]["status"] != "pending"):
                    findings.append(issue("checkpoint", relative, "next work item disagrees"))
                for f in details.get("outputs", []):
                    if not safe_path(root, f).is_file():
                        findings.append(issue("checkpoint_output", f, "checkpoint output absent"))
                findings.extend(check_hashes(root, details.get("output_sha256", {})))
                # Historical spec drift is expected after C00-A: preserve history,
                # report revalidation scope instead of pretending it is current.
                historical = details.get("input_sha256", {})
                if not isinstance(historical, dict):
                    raise AuditStop("checkpoint input ledger object required")
                for f, digest in historical.items():
                    findings.extend(check_hashes(root, {f: digest}, severity="warning" if f in SPEC_FILES or f in details.get("output_sha256", {}) else "error"))
                if expected:
                    for k, value in expected.items():
                        if snapshot.get(k) != value:
                            findings.append(issue("checkpoint_mismatch", k, f"expected {value!r}, found {snapshot.get(k)!r}"))
    finally:
        after = sha256(path)
        if after != before or any(p.exists() for p in sidecars):
            raise AuditStop("database or sidecar changed during audit")
    return {"sha256_before": before, "sha256_after": after, "unchanged": before == after,
            "snapshot": snapshot, "findings": findings}


def audit_legacy(candidates, assessment, review_queue):
    """Diagnose legacy shapes and cross-file references; never normalize values."""
    findings = []
    if not isinstance(candidates, list) or not isinstance(assessment, dict) or not isinstance(review_queue, dict):
        return [issue("legacy_shape", "$", "expected candidate array and assessment/review objects")]
    ids = set()
    claim_index = assessment.get("claim_index", {})
    if not isinstance(claim_index, dict):
        return [issue("legacy_shape", "claim_index", "object required")]
    for i, c in enumerate(candidates):
        p = f"legacy.candidates[{i}]"
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            findings.append(issue("legacy_shape", p, "candidate object/string ID required"))
            continue
        ident = c["id"]
        if ident in ids:
            findings.append(issue("duplicate", p, ident))
        ids.add(ident)
        if c.get("state") not in STAGES:
            findings.append(issue("state", p, "unknown stage"))
        missing = [k for k in ("revision", "author", "claim_ids", "assessment_ids", "verification_ids", "note_ids", "history", "decision_ids") if k not in c]
        if missing:
            findings.append(issue("legacy_missing_fields", p, ", ".join(missing)))
        if "recorded_fact" in c or "recorded_facts" in c:
            findings.append(issue("legacy_claim_shape", p, "singular/plural facts and nested evidence require explicit mapping"))
        if c.get("independent_verification") == "pending":
            findings.append(issue("verification_pending", p, "not eligible for VERIFIED/PUBLISHED", "warning"))
        if c.get("published") is True and (c.get("state") != "PUBLISHED" or c.get("independent_verification") != "passed"):
            findings.append(issue("publication", p, "legacy publication contradicts verification/state"))
    for ident, entry in claim_index.items():
        p = "legacy.claim_index." + str(ident)
        if not isinstance(entry, dict) or not isinstance(entry.get("claim"), dict):
            findings.append(issue("legacy_claim_metadata", p, "claim/report object missing"))
            continue
        claim = entry["claim"]
        if claim.get("id") != ident:
            findings.append(issue("legacy_claim_id", p, "index key and claim ID differ"))
        missing = [k for k in ("candidate_id", "revision", "kind", "author", "actor", "evidence_ids") if k not in claim]
        if missing:
            findings.append(issue("legacy_claim_metadata", p, ", ".join(missing)))
        for i, evidence in enumerate(claim.get("evidence", [])):
            if not isinstance(evidence, dict):
                findings.append(issue("legacy_evidence_metadata", p, "evidence object required"))
                continue
            missing = [k for k in ("source_id", "origin", "location", "version", "author", "collected_at") if k not in evidence]
            if missing:
                findings.append(issue("legacy_evidence_metadata", p + f".evidence[{i}]", ", ".join(missing) + "; blob/content hashes are not automatically snapshot hashes"))
    assessment_ids, review_ids = [], []
    for a in assessment.get("assessments", []):
        if not isinstance(a, dict):
            findings.append(issue("legacy_shape", "assessments", "object required")); continue
        ident = a.get("candidate_id")
        assessment_ids.append(ident)
        if ident not in ids:
            findings.append(issue("dangling", "legacy.assessments", str(ident)))
        criteria, total = [], 0
        for s in a.get("scores", []):
            if not isinstance(s, dict):
                findings.append(issue("score", str(ident), "score object required")); continue
            key, value = s.get("criterion"), s.get("score")
            criteria.append(key)
            maximum = RUBRIC.get(key) if isinstance(key, str) else None
            if type(value) is not int or maximum is None or not 0 <= value <= maximum or s.get("max") != maximum:
                findings.append(issue("score", str(ident), "invalid score/max"))
            else:
                total += value
            if not s.get("reason") or not s.get("evidence_claim_ids"):
                findings.append(issue("score_evidence", str(ident), "reason and evidence claims required"))
            for claim in s.get("evidence_claim_ids", []):
                if claim not in claim_index:
                    findings.append(issue("dangling", str(ident), "missing score claim " + str(claim)))
        if len(criteria) != 7 or any(criteria.count(k) != 1 for k in RUBRIC) or a.get("total") != total:
            findings.append(issue("rubric", str(ident), "criteria or total mismatch"))
    for item in review_queue.get("items", []):
        if not isinstance(item, dict):
            findings.append(issue("legacy_shape", "review_queue", "object required")); continue
        ident = item.get("candidate_id")
        review_ids.append(ident)
        for claim in item.get("source_claim_ids", []):
            if claim not in claim_index:
                findings.append(issue("dangling", str(ident), "missing review claim " + str(claim)))
        findings.append(issue("legacy_review_revision", str(ident), "review lacks current revision digest and independent reviewer attestations"))
    for name, values in (("assessments", assessment_ids), ("review_queue", review_ids)):
        if set(values) != ids or len(values) != len(set(values)):
            findings.append(issue("candidate_set", name, "candidate sets differ or duplicate"))
    if assessment.get("analysis_version") != review_queue.get("analysis_version"):
        findings.append(issue("analysis_version", "legacy", "assessment/review versions differ"))
    findings.append(issue("legacy_contract", "legacy", "no common contract envelope; no implicit migration performed"))
    return findings


def audit(root, db="state/state.sqlite3", bundle=None):
    root = Path(root).resolve()
    report = {"task_id": "C01", "spec_version": SPEC_VERSION, "read_only": True, "findings": []}
    try:
        report["findings"] += audit_canonical(root)
        if not report["findings"]:
            report["findings"] += audit_authorization(root)
        # Version/hash ambiguity is a hard stop before DB/raw or legacy access.
        if report["findings"]:
            report["status"] = "stopped"
            return report
        prior = load_json(root, "spec/revisions/2026-11-14-c00-a1/INPUT_MANIFEST.json")
        observation = prior["database_observation"]
        expected = {"seq": observation["checkpoint_seq"], "completed_id": observation["completed_id"],
                    "next_id": observation["next_id"], "analysis_version": observation["analysis_version"]}
        report["database"] = audit_database(root, db, expected)
        report["findings"] += report["database"]["findings"]
        candidates = load_json(root, "state/influy-pilot-candidates.json")
        assessment = load_json(root, "state/influy-pilot-assessment.json")
        review = load_json(root, "state/influy-pilot-verification-queue.json")
        report["findings"] += audit_legacy(candidates, assessment, review)
        report["findings"] += check_hashes(root, assessment.get("source_hashes", {}))
        # Bind legacy analysis files to the manifest observed before C00-A.
        protected = prior.get("protected_sha256", {})
        for f in ("state/influy-pilot-candidates.json", "state/influy-pilot-assessment.json", "state/influy-pilot-verification-queue.json"):
            if f not in protected:
                raise AuditStop("legacy baseline missing: " + f)
            report["findings"] += check_hashes(root, {f: protected[f]})
        if bundle:
            data = load_json(root, bundle)
            bundle_findings = validate_bundle(data)
            report["findings"] += bundle_findings
            if not bundle_findings:
                for e in data["evidence"]:
                    report["findings"] += check_hashes(root, {e["raw_path"]: e["sha256"]})
                for n in data["notes"]:
                    if n["publication_status"] == "published":
                        report["findings"] += check_hashes(root, {n["path"]: n["sha256"]})
        report["status"] = "invalid" if any(i["severity"] == "error" for i in report["findings"]) else "clean"
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        report["findings"].append(issue("audit_stopped", "$", str(error)))
        report["status"] = "stopped"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db", default="state/state.sqlite3")
    parser.add_argument("--bundle", help="explicit approved JSON file under the allowlist")
    args = parser.parse_args()
    report = audit(args.root, args.db, args.bundle)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"clean": 0, "invalid": 1, "stopped": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
