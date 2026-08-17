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

from ncs_mcp.sqf_human_review_readiness import (
    build_sqf_human_review_readiness,
    render_sqf_human_review_readiness_markdown,
    write_sqf_human_review_readiness_json,
    write_sqf_human_review_readiness_markdown,
)


FORBIDDEN_MARKERS = [
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
]


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clean_corpus() -> dict[str, object]:
    return {
        "ok": True,
        "status": "review_required",
        "approval_ready": False,
        "status_update_allowed": False,
        "used_for_scoring": False,
        "summary": {
            "official_file_count": 3,
            "official_downloaded_count": 2,
            "missing_official_downloaded_files": 0,
            "chunk_count": 12,
            "chunk_match_count": 5,
            "sqf_ncs_candidate_count": 4,
        },
        "matching": {
            "sqf_ncs_relation_counts": {"closeMatch": 2, "partiallyCovers": 2},
            "chunk_match_relation_counts": {"strongEvidence": 3, "supportingEvidence": 2},
        },
    }


def clean_claim_report() -> dict[str, object]:
    return {
        "ok": True,
        "batch": {
            "claim_count": 2,
            "status_update_allowed": False,
            "used_for_scoring": False,
            "approval_claim": False,
            "approval_ready": False,
            "summary": {
                "job_counts": {"HR": 1, "Accounting": 1},
                "source_counts": {"sqf_report": 2},
                "relation_counts": {"closeMatch": 1, "partiallyCovers": 1},
            },
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "status_update_allowed": False,
                "used_for_scoring": False,
                "approval_claim": False,
                "sqf": {"job_name": "HR"},
                "basis_strength": {"mapping_relation": "closeMatch"},
            },
            {
                "claim_id": "claim-2",
                "status_update_allowed": False,
                "used_for_scoring": False,
                "approval_claim": False,
                "sqf": {"job_name": "Accounting"},
                "basis_strength": {"mapping_relation": "partiallyCovers"},
            },
        ],
    }


def clean_priority() -> dict[str, object]:
    return {
        "ok": True,
        "status": "review_required",
        "approval_ready": False,
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "summary": {
            "claim_count": 2,
            "priority_counts": {"P0": 1, "P1": 0, "P2": 0, "P3": 0, "reject_review": 1},
            "source_guardrail_issue_count": 0,
            "source_guardrail_issues": [],
        },
    }


def clean_pending_decision_audit() -> dict[str, object]:
    return {
        "ok": True,
        "approval_ready": False,
        "db_writes": False,
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "summary": {
            "row_count": 2,
            "blank_count": 2,
            "completed_decision_count": 0,
            "invalid_count": 0,
        },
        "guardrail_issue_count": 0,
        "sensitive_reference_count": 0,
    }


class SqfHumanReviewReadinessTests(unittest.TestCase):
    def test_clean_pending_review_is_report_only_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifact_dir = tmp_path / "nested"
            artifact_dir.mkdir()
            corpus_path = artifact_dir / "corpus.json"
            claim_path = artifact_dir / "claims.json"
            priority_path = artifact_dir / "priority.json"
            decision_path = artifact_dir / "decision.json"
            json_out = artifact_dir / "readiness.json"
            md_out = artifact_dir / "readiness.md"
            write_json(corpus_path, clean_corpus())
            write_json(claim_path, clean_claim_report())
            write_json(priority_path, clean_priority())
            write_json(decision_path, clean_pending_decision_audit())

            report = build_sqf_human_review_readiness(
                corpus_audit_path=corpus_path.resolve(),
                claim_report_path=claim_path.resolve(),
                priority_report_path=priority_path.resolve(),
                decision_audit_path=decision_path.resolve(),
            )
            write_sqf_human_review_readiness_json(report, json_out)
            write_sqf_human_review_readiness_markdown(report, md_out)
            rendered = render_sqf_human_review_readiness_markdown(report)
            serialized = json.dumps(report, ensure_ascii=False)
            markdown = md_out.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertFalse(report["approval_ready"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["used_for_scoring"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["sources"]["claim_report"]["name"], "claims.json")
        self.assertNotIn(str(tmp_path), serialized)
        self.assertEqual(report["summaries"]["corpus"]["downloaded_file_count"], 2)
        self.assertEqual(report["summaries"]["corpus"]["match_counts"]["chunk_match_count"], 5)
        self.assertEqual(report["summaries"]["claim_queue"]["claim_count"], 2)
        self.assertTrue(report["summaries"]["claim_queue"]["human_review_required"])
        self.assertEqual(report["summaries"]["priority"]["priority_counts"]["P0"], 1)
        self.assertEqual(report["summaries"]["decision_audit"]["pending_blank_count"], 2)
        self.assertEqual(report["summaries"]["decision_audit"]["import_ready_count"], 0)
        self.assertIn("Review P0 SQF claims first", " ".join(report["next_actions"]))
        self.assertIn("Keep reject_review items out", " ".join(report["next_actions"]))
        self.assertIn("Collect explicit human", " ".join(report["next_actions"]))
        self.assertIn("# SQF Human Review Readiness", markdown)
        self.assertEqual(markdown, rendered)
        for forbidden in FORBIDDEN_MARKERS:
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, markdown)

    def test_invalid_decision_audit_blocks_ok_without_echoing_sensitive_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus_path = tmp_path / "corpus.json"
            claim_path = tmp_path / "claims.json"
            priority_path = tmp_path / "priority.json"
            decision_path = tmp_path / "decision.json"
            write_json(corpus_path, clean_corpus())
            write_json(claim_path, clean_claim_report())
            write_json(priority_path, clean_priority())
            invalid_decision = clean_pending_decision_audit()
            invalid_decision.update(
                {
                    "ok": False,
                    "summary": {
                        "row_count": 1,
                        "blank_count": 0,
                        "completed_decision_count": 1,
                        "invalid_count": 1,
                    },
                    "guardrail_issue_count": 1,
                    "sensitive_reference_count": 1,
                    "rows": [{"decision": "approve", "source_payload": {"secret": "hidden"}}],
                }
            )
            write_json(decision_path, invalid_decision)

            report = build_sqf_human_review_readiness(
                corpus_audit_path=corpus_path,
                claim_report_path=claim_path,
                priority_report_path=priority_path,
                decision_audit_path=decision_path,
            )
            markdown = render_sqf_human_review_readiness_markdown(report)
            serialized = json.dumps(report, ensure_ascii=False)

        self.assertFalse(report["ok"])
        self.assertEqual(report["source_issue_counts"]["input_not_ok_count"], 1)
        self.assertGreaterEqual(report["source_issue_counts"]["source_guardrail_issue_count"], 1)
        self.assertGreaterEqual(report["source_issue_counts"]["sensitive_reference_count"], 1)
        self.assertEqual(report["source_issue_counts"]["invalid_issue_count"], 1)
        self.assertEqual(report["summaries"]["decision_audit"]["invalid_count"], 1)
        self.assertEqual(report["summaries"]["decision_audit"]["import_ready_count"], 0)
        for forbidden in FORBIDDEN_MARKERS:
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, markdown)

    def test_missing_optional_artifact_is_finding_not_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            decision_path = tmp_path / "decision.json"
            write_json(decision_path, clean_pending_decision_audit())

            report = build_sqf_human_review_readiness(
                corpus_audit_path=tmp_path / "missing-corpus.json",
                decision_audit_path=decision_path,
            )

        self.assertTrue(report["ok"])
        severities = {finding["severity"] for finding in report["findings"]}
        self.assertIn("warning", severities)
        self.assertIn("info", severities)
        self.assertEqual(report["sources"]["corpus_audit"]["name"], "missing-corpus.json")
        self.assertFalse(report["sources"]["corpus_audit"]["loaded"])

    def test_additional_artifact_sensitive_marker_blocks_ok_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus_path = tmp_path / "corpus.json"
            claim_path = tmp_path / "claims.json"
            priority_path = tmp_path / "priority.json"
            decision_path = tmp_path / "decision.json"
            snapshot_path = tmp_path / "snapshot.json"
            write_json(corpus_path, clean_corpus())
            write_json(claim_path, clean_claim_report())
            write_json(priority_path, clean_priority())
            write_json(decision_path, clean_pending_decision_audit())
            write_json(snapshot_path, {"db_path": str(tmp_path / "ncs.db"), "db_exists": True})

            report = build_sqf_human_review_readiness(
                corpus_audit_path=corpus_path,
                claim_report_path=claim_path,
                priority_report_path=priority_path,
                decision_audit_path=decision_path,
                additional_artifact_paths=[snapshot_path],
            )
            markdown = render_sqf_human_review_readiness_markdown(report)
            serialized = json.dumps(report, ensure_ascii=False)

        self.assertFalse(report["ok"])
        self.assertEqual(report["summaries"]["additional_artifacts"]["provided_count"], 1)
        self.assertEqual(report["summaries"]["additional_artifacts"]["sensitive_reference_count"], 1)
        self.assertEqual(report["source_issue_counts"]["sensitive_reference_count"], 1)
        self.assertFalse(report["sources"]["additional_artifact_1"]["ok"])
        for forbidden in FORBIDDEN_MARKERS:
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, markdown)
        self.assertNotIn(str(tmp_path), serialized)


if __name__ == "__main__":
    unittest.main()
