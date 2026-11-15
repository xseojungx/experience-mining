import copy
import json
import unittest
from pathlib import Path

from tools.analysis_contract import STAGES, revision_digest, validate_bundle, validate_transition

FIXTURES = Path(__file__).parent / "fixtures"


def fixture():
    return json.loads((FIXTURES / "contract-valid.json").read_text())


class ContractTests(unittest.TestCase):
    def assert_code(self, b, code):
        result = validate_bundle(b)
        self.assertIn(code, {x["code"] for x in result}, result)

    def test_complete_contract_is_valid_and_not_mutated(self):
        b = fixture()
        before = copy.deepcopy(b)
        self.assertEqual(validate_bundle(b), [])
        self.assertEqual(b, before)

    def test_every_missing_top_level_field_is_rejected(self):
        for key in fixture():
            with self.subTest(key=key):
                b = fixture(); del b[key]
                self.assertTrue(validate_bundle(b))

    def test_all_adjacent_candidate_stages(self):
        for a, b in zip(STAGES, STAGES[1:]):
            self.assertEqual(validate_transition("candidate", a, b), [])

    def test_skip_regression_and_domain_boundaries(self):
        bad = [("candidate", "DISCOVERED", "VERIFIED"), ("candidate", "VERIFIED", "LINKED"),
               ("candidate", "INVESTIGATED", "HELD"), ("queue", "pending", "complete"),
               ("candidate", "pending", "running"), ("unknown", "pending", "complete")]
        for args in bad:
            self.assertTrue(validate_transition(*args), args)
        self.assertTrue(validate_transition("candidate", "LINKED", "VERIFIED", mode="skip_optional"))
        self.assertEqual(validate_transition("candidate", "LINKED", "VERIFIED", mode="skip_optional", reason="simple candidate"), [])
        self.assertEqual(validate_transition("candidate", "PUBLISHED", "LINKED", mode="revalidate", reason="new source"), [])
        self.assertEqual(validate_transition("queue", "running", "pending", reason="failed request"), [])
        self.assertTrue(validate_transition("queue", "running", "pending"))

    def test_stale_verification_on_each_content_change(self):
        for mutate in [lambda b: b["claims"][0].update(text="corrected"),
                       lambda b: b["evidence"][0].update(sha256="f" * 64),
                       lambda b: b["candidates"][0].update(revision="r2"),
                       lambda b: b.update(analysis_version="v2"),
                       lambda b: b["assessments"][0]["scores"][0].update(reason="corrected")]:
            b = fixture(); mutate(b)
            self.assert_code(b, "stale_verification")

    def test_independence_covers_all_analysis_authors(self):
        for author in ("analyst", "score-author", "claim-author"):
            b = fixture()
            b["assessments"][0]["author"] = "score-author"
            b["claims"][1]["author"] = "claim-author"
            b["verifications"][0].update(reviewer=author, revision_digest=revision_digest(b, "c1"))
            self.assert_code(b, "independence")

    def test_review_attestations_and_coverage(self):
        for field, value, code in [("originals_reopened", False, "verification"),
                                   ("counterevidence_checked", False, "verification"),
                                   ("followups_checked", False, "verification"),
                                   ("claim_ids", ["f1"], "coverage"),
                                   ("status", "pending", "verification_required")]:
            b = fixture(); b["verifications"][0][field] = value
            self.assert_code(b, code)

    def test_blocker_cannot_be_bypassed_by_another_passed_review(self):
        b = fixture(); r = copy.deepcopy(b["verifications"][0]); r["id"] = "v2"
        r["status"] = "pending"
        r["issues"] = [{"id":"i1", "description":"attribution disputed", "blocking":True, "status":"open"}]
        b["verifications"].append(r); b["candidates"][0]["verification_ids"].append("v2")
        self.assert_code(b, "blocking_issue")
        r["issues"][0].update(status="resolved", resolution="synthetic evidence reconciled")
        self.assertEqual(validate_bundle(b), [])

    def test_claim_kinds_and_required_evidence(self):
        for idx, field in [(0,"actor"),(0,"evidence_ids"),(1,"uncertainty"),
                           (1,"alternative_explanations"),(2,"question"),(2,"answer_verbatim"),(2,"confirmed_at")]:
            b = fixture(); del b["claims"][idx][field]
            self.assertTrue(validate_bundle(b), (idx,field))
        b = fixture(); b["claims"][0]["evidence_ids"] = ["missing"]
        self.assert_code(b,"dangling")

    def test_rubric_range_duplicates_reason_evidence_total(self):
        for field,value,code in [("score",26,"score"),("score",True,"score"),("score",float("nan"),"score"),
                                 ("max",100,"score"),("reason","","required"),
                                 ("evidence_claim_ids",[],"references"),("criterion","job_fit","rubric")]:
            b = fixture(); b["assessments"][0]["scores"][0][field] = value
            self.assert_code(b,code)
        b = fixture(); b["assessments"][0]["total"] = 1
        self.assert_code(b,"total")
        b = fixture(); b["assessments"][0]["scores"][1] = b["assessments"][0]["scores"][0]
        self.assert_code(b,"rubric")

    def test_hold_and_revalidation(self):
        b = fixture(); c = b["candidates"][0]; c["disposition"] = "held"
        self.assert_code(b,"required") if "hold" in c else self.assert_code(b,"hold")
        c["hold"] = {"missing_evidence":"synthetic", "question":"why?", "resume_condition":"new evidence"}
        self.assert_code(b,"publication")
        c["state"] = "LINKED"; c["history"].append({"from":"VERIFIED", "to":"LINKED", "mode":"revalidate", "reason":"new evidence"})
        b["verifications"][0]["status"] = "pending"
        self.assertEqual(validate_bundle(b),[])

    def test_published_requires_current_review_and_stored_note(self):
        b=fixture(); c=b["candidates"][0]; c["state"]="PUBLISHED"
        c["history"].append({"from":"VERIFIED","to":"PUBLISHED"})
        self.assert_code(b,"publication")
        n={"id":"n1","candidate_id":"c1","revision_digest":revision_digest(b,"c1"),
           "path":"tests/fixtures/synthetic-note.md","sha256":"a"*64,
           "template_sha256":"200d182ef85d4221116a25d7728d3baadfbeb0d9410a1f608222877f26756c5a",
           "kind":"experience","publication_status":"published","verified":True,"status":"draft","used_in":[]}
        c["note_ids"]=["n1"]; b["notes"]=[n]
        # Workflow review/note links do not change the content revision.
        digest=revision_digest(b,"c1"); n["revision_digest"]=digest; b["verifications"][0]["revision_digest"]=digest
        self.assertEqual(validate_bundle(b),[])
        n["verified"]=False; self.assert_code(b,"publication")
        n["verified"]=True; n["total"]=100; self.assert_code(b,"note_score")

    def test_history_and_ownership(self):
        b=fixture(); b["candidates"][0]["history"]=[]; self.assert_code(b,"history")
        b=fixture(); c=copy.deepcopy(b["candidates"][0]); c["id"]="c2"; b["candidates"].append(c)
        self.assert_code(b,"ownership")
        b=fixture(); b["claims"].append(copy.deepcopy(b["claims"][0])); self.assert_code(b,"duplicate")

    def test_arbitrary_json_types_fail_closed(self):
        for value in (None, [], 3, "text", {}, {"candidates":[None]}):
            self.assertTrue(validate_bundle(value))
        for table in ("claims","candidates","evidence","assessments","verifications","notes"):
            for value in (None, [], {}, True):
                b=fixture(); b[table]=[value]
                self.assertTrue(validate_bundle(b))
        b=fixture(); b["claims"][0]["candidate_id"]={}; self.assertTrue(validate_bundle(b))

    def test_core_cannot_be_provided_only_by_inference(self):
        b=fixture();b["claims"][0]["core_aspects"]=[]
        b["claims"][1]["core_aspects"]=["situation","action","contribution","judgment"]
        b["verifications"][0]["revision_digest"]=revision_digest(b,"c1")
        self.assert_code(b,"core_unconfirmed")

    def test_archive_old_review_and_reverify_current_revision(self):
        b=fixture(); old=b["verifications"][0]; old["status"]="superseded"
        b["claims"][0]["text"]="corrected synthetic fact"
        current=copy.deepcopy(old);current.update(id="v2",status="passed",revision_digest=revision_digest(b,"c1"))
        b["verifications"].append(current);b["candidates"][0]["verification_ids"].append("v2")
        self.assertEqual(validate_bundle(b),[])

    def test_immutable_source_version_and_safe_relative_path(self):
        b=fixture();b["evidence"][0].update(origin="git",version="main")
        self.assert_code(b,"source_version")
        b=fixture();b["evidence"][0]["raw_path"]="../source"
        self.assert_code(b,"source_path")

    def test_metric_provenance(self):
        b=fixture(); b["evidence"][0]["metric_kind"]="remeasured"
        self.assert_code(b,"required")


if __name__ == "__main__":
    unittest.main()
