"""C01 pure, versioned analysis contract; no I/O, migration or publication.

Bundle: contract_version/spec_version/analysis_version plus arrays candidates,
claims, evidence, assessments, verifications, notes. IDs are unique per array.
All relations are explicit; legacy JSON must be diagnosed, never guessed into
this contract. Unknown extension fields survive and participate in revision hashes.
See tests/fixtures/contract-valid.json for a complete synthetic example.

revision_digest binds the candidate's content (not workflow state/history), all
its claims, their evidence, and assessments to the spec/analysis versions. A new
source hash, claim, score, author or rule version invalidates earlier verification.
Independent-review attestations are necessary metadata, not proof of truth.
"""
import hashlib
import json
import math
import re
from pathlib import PurePosixPath

CONTRACT_VERSION = "c01-v1"
SPEC_VERSION = "v2026-11-14-c00-a1"
STAGES = ("DISCOVERED", "INGESTED", "MINED", "LINKED", "INVESTIGATED", "VERIFIED", "PUBLISHED")
RUBRIC = {"judgment": 25, "distinctiveness": 20, "contribution": 20,
          "importance": 10, "execution": 10, "evidence": 10, "reflection": 5}
KINDS = ("recorded_fact", "ai_inference", "user_confirmation")
DECISIONS = {f"ARCH-0{i}" for i in range(1, 7)} | {f"DATA-0{i}" for i in range(1, 4)} | {
    "OPS-01", "GOV-01", "GOV-02", "GOV-03", "NOTE-01", "SAFE-01"}
CORE = {"situation", "action", "contribution", "judgment"}
SHA = re.compile(r"[0-9a-f]{64}\Z")


def issue(code, path, message, severity="error"):
    return {"code": code, "path": path, "message": message, "severity": severity}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value, nonempty=True):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(text(x) for x in value) and len(value) == len(set(value)))


def revision_digest(bundle, candidate_id):
    """Deterministic content revision; accepts only structurally valid bundles."""
    c = next(c for c in bundle["candidates"] if c["id"] == candidate_id)
    claims = [x for x in bundle["claims"] if x["candidate_id"] == candidate_id]
    ids = {e for x in claims for e in x["evidence_ids"]}
    payload = {"contract_version": bundle["contract_version"], "spec_version": bundle["spec_version"],
               "analysis_version": bundle["analysis_version"],
               "candidate": {k: v for k, v in c.items() if k not in ("state", "history", "disposition", "hold", "closure_reason", "verification_ids", "note_ids")},
               "claims": sorted(claims, key=lambda x: x["id"]),
               "evidence": sorted([e for e in bundle["evidence"] if e["id"] in ids], key=lambda x: x["id"]),
               "assessments": sorted([a for a in bundle["assessments"] if a["candidate_id"] == candidate_id], key=lambda x: x["id"])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_transition(domain, before, after, *, reason=None, mode="advance"):
    """PR/candidate stage machines are separate from queue/work-item states.

    held/closed are dispositions, never invented stages. Stage skipping is only
    allowed for optional INVESTIGATED, with a reason. Revalidation regresses to
    the affected pre-verification stage, with a reason. No mutation is performed.
    """
    p = f"transition.{domain}"
    if domain in ("candidate", "pr"):
        if before not in STAGES or after not in STAGES:
            return [issue("state", p, "unknown analysis stage")]
        if domain == "pr" and (before == "PUBLISHED" or after == "PUBLISHED"):
            return [issue("transition", p, "publication belongs to candidates, not PR collection")]
        a, b = STAGES.index(before), STAGES.index(after)
        valid = (mode == "advance" and b == a + 1)
        valid |= mode == "skip_optional" and before == "LINKED" and after == "VERIFIED" and text(reason)
        valid |= mode == "revalidate" and b < a and b < STAGES.index("VERIFIED") and text(reason)
    elif domain == "queue":
        valid = mode == "advance" and (before, after) in {
            ("pending", "running"), ("running", "complete"), ("running", "pending")}
        if (before, after) == ("running", "pending"):
            valid = valid and text(reason)
    elif domain == "work_item":
        valid = mode == "advance" and (before, after) == ("pending", "complete")
    else:
        valid = False
    return [] if valid else [issue("transition", p, f"invalid {mode}: {before} -> {after}")]


class _Check:
    def __init__(self):
        self.errors = []

    def require(self, condition, code, path, message):
        if not condition:
            self.errors.append(issue(code, path, message))

    def fields(self, row, path, fields):
        for f in fields:
            self.require(text(row.get(f)), "required", f"{path}.{f}", "nonempty string required")


def _validate_bundle(bundle):
    """Return structured diagnostics for arbitrary JSON, without filling values."""
    v = _Check()
    if not isinstance(bundle, dict):
        return [issue("shape", "$", "object required; legacy candidate arrays need explicit migration")]
    v.require(bundle.get("contract_version") == CONTRACT_VERSION, "version", "contract_version", CONTRACT_VERSION)
    v.require(bundle.get("spec_version") == SPEC_VERSION, "version", "spec_version", SPEC_VERSION)
    v.fields(bundle, "$", ["analysis_version"])
    tables = {}
    for name in ("candidates", "claims", "evidence", "assessments", "verifications", "notes"):
        rows = bundle.get(name)
        v.require(isinstance(rows, list), "shape", name, "array required")
        table = tables[name] = {}
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            p = f"{name}[{i}]"
            if not isinstance(row, dict):
                v.require(False, "shape", p, "object required")
                continue
            ident = row.get("id")
            v.require(text(ident), "required", p + ".id", "string ID required")
            if text(ident):
                v.require(ident not in table, "duplicate", p + ".id", ident)
                table[ident] = row
    candidates, claims, evidence, assessments, verifications, notes = (tables[k] for k in tables)

    def refs(row, field, table, p, *, allow_empty=False, owner=None):
        ids = row.get(field)
        v.require(strings(ids, not allow_empty), "references", p + "." + field, "unique string IDs required")
        for ident in ids if isinstance(ids, list) else []:
            if not text(ident):
                continue
            v.require(ident in table, "dangling", p + "." + field, ident)
            if owner and ident in table:
                v.require(table[ident].get("candidate_id") == owner, "ownership", p + "." + field, ident)

    for ident, e in evidence.items():
        p = "evidence." + ident
        v.fields(e, p, ["source_id", "origin", "location", "raw_path", "version", "author", "created_at", "collected_at"])
        raw = e.get("raw_path")
        v.require(text(raw) and not PurePosixPath(raw).is_absolute() and ".." not in PurePosixPath(raw).parts,
                  "source_path", p + ".raw_path", "workspace-relative source path required")
        if e.get("origin") in ("github", "git"):
            v.require(isinstance(e.get("version"), str) and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", e["version"])),
                      "source_version", p + ".version", "immutable commit or snapshot digest required")
        v.require(bool(SHA.fullmatch(e.get("sha256", ""))) if isinstance(e.get("sha256"), str) else False,
                  "hash", p + ".sha256", "SHA-256 required")
        if e.get("metric_kind") is not None:
            v.require(e["metric_kind"] in ("historical", "remeasured"), "metric", p, "distinguish historical and remeasured")
            v.fields(e, p, ["measurement_conditions", "measured_at"])

    for ident, c in candidates.items():
        p = "candidates." + ident
        v.fields(c, p, ["project", "revision", "author"])
        v.require(c.get("project") in ("influy", "만수무강", "ON", "spitching"), "project", p, "unknown project")
        v.require(c.get("state") in STAGES, "state", p, "unknown candidate state")
        refs(c, "claim_ids", claims, p, allow_empty=c.get("state") in STAGES[:2], owner=ident)
        refs(c, "assessment_ids", assessments, p, allow_empty=True, owner=ident)
        refs(c, "verification_ids", verifications, p, allow_empty=True, owner=ident)
        refs(c, "note_ids", notes, p, allow_empty=True, owner=ident)
        refs(c, "decision_ids", {d: {} for d in DECISIONS}, p)
        v.require(c.get("disposition") in ("active", "held", "closed"), "disposition", p, "active/held/closed required")
        if c.get("disposition") == "held":
            hold = c.get("hold")
            if isinstance(hold, dict):
                v.fields(hold, p + ".hold", ["missing_evidence", "question", "resume_condition"])
            else:
                v.require(False, "hold", p, "hold details required")
        if c.get("disposition") == "closed":
            v.fields(c, p, ["closure_reason"])
        history = c.get("history")
        v.require(isinstance(history, list), "history", p, "history array required")
        state = "DISCOVERED"
        for h in history if isinstance(history, list) else []:
            if not isinstance(h, dict):
                v.require(False, "history", p, "transition object required")
                continue
            v.require(h.get("from") == state, "history", p, "discontinuous history")
            v.errors.extend(validate_transition("candidate", h.get("from"), h.get("to"),
                                               reason=h.get("reason"), mode=h.get("mode", "advance")))
            state = h.get("to")
        v.require(state == c.get("state"), "history", p, "history must end at current state")

    for ident, claim in claims.items():
        p = "claims." + ident
        v.fields(claim, p, ["candidate_id", "revision", "text", "author", "actor"])
        owner = claim.get("candidate_id")
        v.require(text(owner) and owner in candidates, "dangling", p, "candidate missing")
        if text(owner) and owner in candidates:
            v.require(ident in candidates[owner].get("claim_ids", []), "backlink", p, "candidate claim backlink missing")
        v.require(claim.get("kind") in KINDS, "kind", p, "fact/inference/confirmation required")
        refs(claim, "evidence_ids", evidence, p)
        if claim.get("kind") == "ai_inference":
            v.fields(claim, p, ["uncertainty"])
            v.require(strings(claim.get("alternative_explanations")), "inference", p, "alternative explanations required")
        if claim.get("kind") == "user_confirmation":
            v.fields(claim, p, ["question", "answer_verbatim", "confirmed_at"])
        v.require(isinstance(claim.get("core_aspects"), list) and all(x in CORE for x in claim.get("core_aspects", [])),
                  "core", p, "core_aspects array required")

    for ident, a in assessments.items():
        p = "assessments." + ident
        v.fields(a, p, ["candidate_id", "revision", "author"])
        owner = a.get("candidate_id")
        v.require(text(owner) and owner in candidates, "dangling", p, "candidate missing")
        if text(owner) and owner in candidates:
            v.require(ident in candidates[owner].get("assessment_ids", []), "backlink", p, "assessment backlink missing")
        scores = a.get("scores")
        v.require(isinstance(scores, list), "score", p, "scores array required")
        criteria, total = [], 0
        for s in scores if isinstance(scores, list) else []:
            if not isinstance(s, dict):
                v.require(False, "score", p, "score object required")
                continue
            criterion, score = s.get("criterion"), s.get("score")
            criteria.append(criterion)
            maximum = RUBRIC.get(criterion) if isinstance(criterion, str) else None
            ok = type(score) in (int, float) and math.isfinite(score) and maximum is not None and 0 <= score <= maximum
            v.require(ok and s.get("max") == maximum, "score", p, "score range/max mismatch")
            total += score if ok else 0
            v.fields(s, p + ".scores", ["reason"])
            refs(s, "evidence_claim_ids", claims, p + ".scores", owner=owner)
        v.require(len(criteria) == len(RUBRIC) and all(criteria.count(k) == 1 for k in RUBRIC), "rubric", p, "exact seven criteria required")
        v.require(type(a.get("total")) in (int, float) and a["total"] == total, "total", p, "score sum mismatch")

    # Stop before computing revision digests if shapes/relations are invalid.
    if v.errors:
        return v.errors

    valid_reviews = {}
    blocked_candidates = set()
    for ident, r in verifications.items():
        p = "verifications." + ident
        v.fields(r, p, ["candidate_id", "reviewer", "reviewed_at", "revision_digest"])
        owner = r.get("candidate_id")
        if owner not in candidates:
            v.require(False, "dangling", p, "candidate missing")
            continue
        c = candidates[owner]
        v.require(ident in c["verification_ids"], "backlink", p, "verification backlink missing")
        v.require(r.get("status") in ("pending", "passed", "failed", "superseded"), "verification", p, "invalid review status")
        refs(r, "claim_ids", claims, p, owner=owner)
        refs(r, "evidence_ids", evidence, p)
        issues = r.get("issues")
        v.require(isinstance(issues, list), "issues", p, "issues array required")
        blocked = not isinstance(issues, list)
        for i in issues if isinstance(issues, list) else []:
            ok = (isinstance(i, dict) and text(i.get("id")) and text(i.get("description"))
                  and type(i.get("blocking")) is bool and i.get("status") in ("open", "resolved"))
            v.require(ok, "issues", p, "explicit issue ID/description/blocking/status required")
            if not ok or (i["blocking"] and i["status"] != "resolved"):
                blocked = True
            if ok and i["status"] == "resolved":
                v.fields(i, p + ".issues", ["resolution"])
        if blocked:
            blocked_candidates.add(owner)
        digest_matches = r.get("revision_digest") == revision_digest(bundle, owner)
        if r.get("status") == "passed":
            v.require(digest_matches, "stale_verification", p, "review does not cover current content revision")
            authors = {c["author"]} | {x["author"] for x in claims.values() if x["candidate_id"] == owner} | {
                x["author"] for x in assessments.values() if x["candidate_id"] == owner}
            v.require(r.get("reviewer") not in authors, "independence", p, "reviewer authored reviewed analysis")
            checks = ("originals_reopened", "attribution_checked", "counterevidence_checked", "followups_checked")
            complete = all(r.get(k) is True for k in checks)
            v.require(complete, "verification", p, "original/attribution/counterevidence/followup checks required")
            covered = set(r.get("claim_ids", [])) == set(c["claim_ids"])
            expected_evidence = {e for claim_id in c["claim_ids"] for e in claims[claim_id]["evidence_ids"]}
            covered &= set(r.get("evidence_ids", [])) == expected_evidence
            v.require(covered, "coverage", p, "review must cover current claims and evidence")
            v.require(not blocked, "blocking_issue", p, "unresolved blocking issue")
            valid_reviews[ident] = digest_matches and complete and covered and not blocked and r.get("reviewer") not in authors
        else:
            valid_reviews[ident] = False

    for ident, c in candidates.items():
        if c["state"] in ("VERIFIED", "PUBLISHED"):
            p = "candidates." + ident
            v.require(ident not in blocked_candidates, "blocking_issue", p, "candidate has unresolved blocking issues")
            v.require(c["disposition"] == "active", "publication", p, "held/closed candidate cannot verify or publish")
            v.require(any(valid_reviews.get(r) for r in c["verification_ids"]), "verification_required", p, "current independent review required")
            v.require(bool(c["assessment_ids"]), "assessment_required", p, "separate assessment required")
            core = {aspect for claim_id in c["claim_ids"] if claims[claim_id]["kind"] != "ai_inference"
                    for aspect in claims[claim_id]["core_aspects"]}
            v.require(CORE <= core, "core_unconfirmed", p, "situation/action/contribution/judgment require evidence-backed fact or confirmation")
        if c["state"] == "PUBLISHED":
            v.require(bool(c["note_ids"]), "publication", "candidates." + ident, "published note record required")

    for ident, n in notes.items():
        p = "notes." + ident
        v.fields(n, p, ["candidate_id", "revision_digest", "path", "template_sha256"])
        owner = n.get("candidate_id")
        if owner not in candidates:
            v.require(False, "dangling", p, "candidate missing")
            continue
        c = candidates[owner]
        v.require(ident in c["note_ids"], "backlink", p, "note backlink missing")
        v.require(n.get("revision_digest") == revision_digest(bundle, owner), "stale_note", p, "note revision mismatch")
        v.require(n.get("template_sha256") == "200d182ef85d4221116a25d7728d3baadfbeb0d9410a1f608222877f26756c5a", "template", p, "template hash mismatch")
        v.require(n.get("kind") in ("log", "experience"), "note", p, "log/experience required")
        v.require(n.get("publication_status") in ("draft", "published"), "note", p, "publication status required")
        if n.get("publication_status") == "published":
            v.require(c["state"] == "PUBLISHED", "publication", p, "published note requires PUBLISHED candidate")
            v.require(isinstance(n.get("sha256"), str) and bool(SHA.fullmatch(n["sha256"])), "hash", p, "published note content hash required")
        if n.get("kind") == "experience":
            v.require(n.get("verified") is True and n.get("status") == "draft" and n.get("used_in") == [],
                      "publication", p, "experience frontmatter requires verified=true/status=draft/used_in=[]")
            v.require(c["state"] in ("VERIFIED", "PUBLISHED"), "publication", p, "experience requires verified candidate")
        v.require(not any(k in n for k in ("scores", "score", "total")), "note_score", p, "evaluation stays outside notes")
        if c["state"] == "PUBLISHED":
            v.require(n.get("publication_status") == "published", "publication", p, "stored publication record required")
    return v.errors


def validate_bundle(bundle):
    """Fail closed on malformed JSON types, returning diagnostics instead of raising."""
    try:
        return _validate_bundle(bundle)
    except (TypeError, KeyError, ValueError, OverflowError) as error:
        return [issue("malformed", "$", str(error))]
