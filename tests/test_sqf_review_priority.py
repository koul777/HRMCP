from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.sqf_review_priority import (
    parse_level,
    prioritize_sqf_claim_candidates,
    prioritize_sqf_claim_candidates_from_file,
    write_sqf_review_priority_json,
    write_sqf_review_priority_markdown,
)


def claim_fixture(
    claim_id: str,
    *,
    relation: str = "closeMatch",
    evidence_relation: str | None = "strongEvidence",
    major_code: str = "02",
    sqf_level: object = 6,
    ncs_level: object = "6",
    sqf_job_name: str = "인사",
    evidence_keyword: str = "인사",
    statement: str = "SQF 인사 duty may align with NCS 인사기획.",
    unit_name: str = "인사기획",
    priority_score: float = 10.0,
) -> dict[str, object]:
    evidence = []
    if evidence_relation is not None:
        evidence.append(
            {
                "evidence_ref_id": f"{claim_id}:evidence:1",
                "relation": evidence_relation,
                "score": 30.0,
                "document": {"title": "SQF report", "page_start": 1, "page_end": 1},
                "matched_terms": {"exact": [evidence_keyword], "support": ["기획"]},
                "keyword_hits": [evidence_keyword],
            }
        )
    return {
        "record_type": "sqf_report_claim_candidate",
        "format_version": "ncs-sqf-report-claim-candidate-v1",
        "claim_id": claim_id,
        "sequence": int(claim_id.rsplit("-", 1)[-1]),
        "claim_type": "sqf_ncs_alignment",
        "claim_status": "candidate_requires_human_review",
        "claim_statement": statement,
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "priority_score": priority_score,
        "basis_strength": {
            "mapping_relation": relation,
            "report_evidence_count": len(evidence),
            "max_report_score": 30.0 if evidence else None,
        },
        "sqf": {
            "job_name": sqf_job_name,
            "duty_name": f"{sqf_job_name}({sqf_level})",
            "sqf_level": sqf_level,
            "sector": {"major_code": "02"},
        },
        "ncs_candidate": {
            "unit_code": f"{major_code}02020101_23v3",
            "unit_name": unit_name,
            "unit_level": ncs_level,
            "classification": {"major_code": major_code, "sub_name": unit_name},
        },
        "sqf_ncs_match": {"relation": relation, "score": 20.0},
        "report_evidence": evidence,
    }


class SqfReviewPriorityTests(unittest.TestCase):
    def test_parse_level_handles_sqf_and_ncs_level_strings(self) -> None:
        self.assertEqual(parse_level("SQF L6"), 6)
        self.assertEqual(parse_level("NCS 5수준"), 5)
        self.assertEqual(parse_level("인사(7)"), 7)
        self.assertIsNone(parse_level(""))

    def test_prioritizes_p0_and_preserves_report_only_guardrails(self) -> None:
        report = prioritize_sqf_claim_candidates(
            {
                "batch": {
                    "format_version": "ncs-sqf-report-claim-candidate-v1",
                    "claim_batch_id": "batch-1",
                    "claim_count": 1,
                    "used_for_scoring": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                },
                "claims": [claim_fixture("claim-1")],
            }
        )

        self.assertTrue(report["ok"])
        self.assertFalse(report["used_for_scoring"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["approval_claim"])
        item = report["items"][0]
        self.assertEqual(item["priority"], "P0")
        self.assertFalse(item["used_for_scoring"])
        self.assertFalse(item["status_update_allowed"])
        self.assertFalse(item["approval_claim"])
        self.assertEqual(item["level_fit"]["level_gap"], 0)
        action_bundle = item["review_action_bundle"]
        self.assertEqual(action_bundle["claim_id"], "claim-1")
        self.assertEqual(action_bundle["ncs_scope"]["major_code"], "02")
        self.assertEqual(action_bundle["evidence_strength"], "strong")
        self.assertEqual(action_bundle["review_risk_flags"], [])
        self.assertIn("approve_for_reference", action_bundle["decision_facets"])
        self.assertFalse(action_bundle["blocking_rules"]["status_update_allowed"])
        self.assertFalse(action_bundle["blocking_rules"]["mutates_scoring"])
        self.assertFalse(action_bundle["blocking_rules"]["saves_review_state"])
        self.assertEqual(report["summary"]["priority_counts"]["P0"], 1)

    def test_prioritizes_p1_for_partial_or_supporting_evidence_with_small_level_gap(self) -> None:
        partial = claim_fixture(
            "claim-1",
            relation="partiallyCovers",
            evidence_relation="strongEvidence",
            sqf_level="SQF L6",
            ncs_level="NCS 5수준",
        )
        supporting = claim_fixture(
            "claim-2",
            relation="closeMatch",
            evidence_relation="supportingEvidence",
            sqf_level=6,
            ncs_level=6,
        )

        report = prioritize_sqf_claim_candidates({"claims": [partial, supporting]})

        self.assertEqual([item["priority"] for item in report["items"]], ["P1", "P1"])
        self.assertEqual(report["summary"]["priority_counts"]["P1"], 2)

    def test_rejects_cross_major_no_evidence_and_related_only_claims(self) -> None:
        cross_major = claim_fixture("claim-1", major_code="03")
        no_evidence = claim_fixture("claim-2", evidence_relation=None)
        related_only = claim_fixture("claim-3", relation="related", evidence_relation="related")

        report = prioritize_sqf_claim_candidates({"claims": [cross_major, no_evidence, related_only]})

        self.assertEqual(report["summary"]["priority_counts"]["reject_review"], 3)
        reasons = {item["claim_id"]: item["priority_reasons"][0] for item in report["items"]}
        self.assertEqual(reasons["claim-1"], "target_major_not_02")
        self.assertEqual(reasons["claim-2"], "no_report_evidence")
        self.assertEqual(reasons["claim-3"], "related_only_mapping_or_evidence")

    def test_prioritizes_p2_and_p3_lower_review_buckets(self) -> None:
        p2 = claim_fixture(
            "claim-1",
            relation="closeMatch",
            evidence_relation="supportingEvidence",
            sqf_level=6,
            ncs_level=3,
            statement="재무 adjacent context with evidence.",
            unit_name="재무관리",
        )
        p3 = claim_fixture(
            "claim-2",
            relation="closeMatch",
            evidence_relation="strongEvidence",
            sqf_level="",
            ncs_level="",
            sqf_job_name="경영",
            evidence_keyword="경영",
            statement="경영관리 broad reference.",
            unit_name="경영관리",
        )

        report = prioritize_sqf_claim_candidates({"claims": [p2, p3]})

        priorities = {item["claim_id"]: item["priority"] for item in report["items"]}
        self.assertEqual(priorities["claim-1"], "P2")
        self.assertEqual(priorities["claim-2"], "P3")

    def test_file_loader_and_writers_are_report_only(self) -> None:
        payload = {"claims": [claim_fixture("claim-1")]}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "claims.json"
            json_out = tmp_path / "priority.json"
            md_out = tmp_path / "priority.md"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            report = prioritize_sqf_claim_candidates_from_file(input_path)
            write_sqf_review_priority_json(report, json_out)
            write_sqf_review_priority_markdown(report, md_out)

            loaded = json.loads(json_out.read_text(encoding="utf-8"))
            markdown = md_out.read_text(encoding="utf-8")

        self.assertEqual(loaded["schema"], "ncs_sqf_review_priority_v1")
        self.assertFalse(loaded["review_policy"]["db_writes"])
        self.assertIn("# SQF Review Priority", markdown)
        self.assertIn("status_update_allowed: false", markdown)
        self.assertIn("approval_claim: false", markdown)
        self.assertIn("review_risk_flags", markdown)

    def test_source_recommended_priority_cannot_override_local_classification(self) -> None:
        tampered = claim_fixture("claim-1", major_code="03")
        tampered["recommended_priority"] = "P0"

        report = prioritize_sqf_claim_candidates({"claims": [tampered]})

        self.assertTrue(report["ok"])
        item = report["items"][0]
        self.assertEqual(item["priority"], "reject_review")
        self.assertIn("target_major_not_02", item["priority_reasons"])
        self.assertIn("source_claim_recommended_priority_ignored:P0", item["priority_reasons"])

    def test_source_recommended_priority_can_only_downgrade_review_priority(self) -> None:
        conservative = claim_fixture("claim-1")
        conservative["recommended_priority"] = "reject_review"

        report = prioritize_sqf_claim_candidates({"claims": [conservative]})

        self.assertTrue(report["ok"])
        item = report["items"][0]
        self.assertEqual(item["priority"], "reject_review")
        self.assertIn("source_claim_recommended_priority_downgraded:reject_review", item["priority_reasons"])

    def test_source_guardrail_issues_make_priority_report_not_ok(self) -> None:
        tampered = claim_fixture("claim-1")
        tampered["used_for_scoring"] = True

        report = prioritize_sqf_claim_candidates({"claims": [tampered]})

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["source_guardrail_issue_count"], 1)
        self.assertIn("claim:claim-1.used_for_scoring_not_false", report["summary"]["source_guardrail_issues"])


if __name__ == "__main__":
    unittest.main()
