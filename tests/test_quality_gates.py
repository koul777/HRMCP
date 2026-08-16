from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.quality_gates import (
    FAIL,
    WARN,
    _add_qualification_gates,
    _add_transition_evaluation_gates,
    evaluate_quality_gates,
    write_quality_gate_markdown,
)


class QualityGateTests(unittest.TestCase):
    def _init_db(self, path: Path) -> None:
        conn = connect(path)
        try:
            initialize_database(conn)
        finally:
            conn.close()

    def test_empty_database_fails_core_data_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "quality_gates.json"
            markdown_path = Path(tmp) / "quality_gates.md"
            self._init_db(db_path)

            result = evaluate_quality_gates(
                db_path,
                out_path=out_path,
                markdown_path=markdown_path,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], FAIL)
            self.assertTrue(out_path.exists())
            self.assertTrue(markdown_path.exists())
            gates = {gate["name"]: gate for gate in result["gates"]}
            self.assertEqual(gates["core_data_present:competency_units"]["status"], FAIL)
            self.assertEqual(gates["job_base:job_base_competency_count"]["status"], FAIL)

    def test_empty_career_path_table_warns_transition_evidence_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            self._init_db(db_path)

            result = evaluate_quality_gates(db_path)

            gates = {gate["name"]: gate for gate in result["gates"]}
            gate = gates["transition_evidence:career_paths"]
            self.assertEqual(gate["status"], WARN)
            self.assertEqual(gate["value"], 0)
            self.assertEqual(gate["details"]["career_path_count"], 0)
            self.assertEqual(result["evidence"]["career_paths"]["career_path_count"], 0)

    def test_active_error_quality_issue_is_fail_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    )
                    VALUES ('unit', 'U1', 'missing_required_value', 'error',
                            'missing required test value', 'fill value',
                            '2026-06-16T00:00:00Z')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            result = evaluate_quality_gates(db_path)

            gates = {gate["name"]: gate for gate in result["gates"]}
            self.assertEqual(gates["quality_issues:active_errors"]["status"], FAIL)
            self.assertEqual(gates["quality_issues:active_errors"]["value"], 1)
            self.assertEqual(gates["quality_issues:missing_required_value"]["status"], FAIL)
            self.assertEqual(gates["quality_issues:missing_required_value"]["value"], 1)

    def test_orphan_training_goal_recommendation_evidence_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = "2026-06-16T00:00:00Z"
                conn.execute(
                    """
                    INSERT INTO education_recommendation_runs(
                        query, target_source_key, request_payload, target_payload,
                        summary_payload, audit_payload, created_at
                    ) VALUES ('인사기획', 'unit:U1', '{}', '{}', '{}', '{}', ?)
                    """,
                    (timestamp,),
                )
                run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO education_recommendation_items(
                        run_id, rank, learn_module_name, recommendation_payload,
                        confidence_score, confidence_grade, created_at
                    ) VALUES (?, 1, '인사기획', '{}', 1.0, 'high', ?)
                    """,
                    (run_id, timestamp),
                )
                item_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO education_recommendation_evidence(
                        run_id, item_id, evidence_type, source_table, source_id,
                        evidence_text, evidence_summary, confidence_score, created_at
                    ) VALUES (?, ?, 'training_goal_ksa', 'training_goal_concept_links',
                              '999999', 'missing link', 'missing link', 0.8, ?)
                    """,
                    (run_id, item_id, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = evaluate_quality_gates(db_path)

            gates = {gate["name"]: gate for gate in result["gates"]}
            gate = gates["recommendation_evidence:training_goal_link_references"]
            self.assertEqual(gate["status"], WARN)
            self.assertEqual(gate["value"], 1)
            self.assertEqual(gate["details"]["training_goal_link_evidence"], 1)

    def test_qualification_gate_does_not_pass_partial_collection(self) -> None:
        gates: list[dict] = []

        _add_qualification_gates(
            gates,
            {
                "collection_status": [
                    {"collection_status": "collected", "unit_count": 1},
                ]
            },
            total_unit_count=100,
        )

        by_name = {gate["name"]: gate for gate in gates}
        self.assertEqual(by_name["qualification:collection_coverage"]["status"], FAIL)
        self.assertEqual(by_name["qualification:collection_coverage"]["value"], 0.01)
        self.assertEqual(by_name["qualification:error_share"]["status"], "pass")

    def test_qualification_retry_metadata_gate_warns_on_gaps(self) -> None:
        gates: list[dict] = []

        _add_qualification_gates(
            gates,
            {
                "collection_status": [
                    {"collection_status": "error", "unit_count": 2},
                ]
            },
            total_unit_count=10,
            retry_hygiene={
                "metadata_gaps": {
                    "missing_error_type_count": 2,
                    "zero_attempt_count": 2,
                    "missing_next_retry_at_count": 2,
                    "invalid_next_retry_at_count": 0,
                },
                "retry_ready_unit_count": 2,
                "retry_waiting_unit_count": 0,
                "broad_retry_risk": "high",
            },
        )

        by_name = {gate["name"]: gate for gate in gates}
        self.assertEqual(by_name["qualification:retry_metadata"]["status"], WARN)
        self.assertEqual(by_name["qualification:retry_metadata"]["value"], 6)

    def test_qualification_retry_metadata_gate_passes_without_gaps(self) -> None:
        gates: list[dict] = []

        _add_qualification_gates(
            gates,
            {
                "collection_status": [
                    {"collection_status": "error", "unit_count": 2},
                ]
            },
            total_unit_count=10,
            retry_hygiene={
                "metadata_gaps": {
                    "missing_error_type_count": 0,
                    "zero_attempt_count": 0,
                    "missing_next_retry_at_count": 0,
                    "invalid_next_retry_at_count": 0,
                },
                "retry_ready_unit_count": 0,
                "retry_waiting_unit_count": 2,
                "broad_retry_risk": "high",
            },
        )

        by_name = {gate["name"]: gate for gate in gates}
        self.assertEqual(by_name["qualification:retry_metadata"]["status"], "pass")
        self.assertEqual(by_name["qualification:retry_metadata"]["value"], 0)

    def test_transition_gate_warns_when_no_trusted_scenarios_exist(self) -> None:
        gates: list[dict] = []

        _add_transition_evaluation_gates(
            gates,
            {
                "ok": True,
                "scenario_count": 0,
                "status_counts": {"candidate": 100},
            },
        )

        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["name"], "transition_eval:trusted_scenarios")
        self.assertEqual(gates[0]["status"], WARN)

    def test_transition_precision_gate_passes_sparse_label_upper_bound(self) -> None:
        gates: list[dict] = []

        _add_transition_evaluation_gates(
            gates,
            {
                "ok": True,
                "scenario_count": 1,
                "current_scope_accuracy": 1.0,
                "target_scope_accuracy": 1.0,
                "expected_course_recall_at_k": 1.0,
                "top1_expected_hit_rate": 1.0,
                "precision_at_k": 0.2,
                "precision_at_k_upper_bound": 0.2,
                "precision_at_k_relative_to_upper_bound": 1.0,
                "ndcg_at_k": 1.0,
                "recommended_course_total": 5,
                "expected_course_total": 1,
                "possible_expected_course_hit_count": 1,
            },
        )

        precision_gate = next(gate for gate in gates if gate["name"] == "transition_eval:precision_at_k")
        self.assertEqual(precision_gate["status"], "pass")
        self.assertEqual(precision_gate["details"]["precision_at_k_upper_bound"], 0.2)

    def test_transition_precision_gate_warns_below_sparse_label_upper_bound(self) -> None:
        gates: list[dict] = []

        _add_transition_evaluation_gates(
            gates,
            {
                "ok": True,
                "scenario_count": 2,
                "current_scope_accuracy": 1.0,
                "target_scope_accuracy": 1.0,
                "expected_course_recall_at_k": 0.5,
                "top1_expected_hit_rate": 0.5,
                "precision_at_k": 0.1,
                "precision_at_k_upper_bound": 0.2,
                "precision_at_k_relative_to_upper_bound": 0.5,
                "ndcg_at_k": 0.5,
                "recommended_course_total": 10,
                "expected_course_total": 2,
                "possible_expected_course_hit_count": 2,
            },
        )

        precision_gate = next(gate for gate in gates if gate["name"] == "transition_eval:precision_at_k")
        self.assertEqual(precision_gate["status"], WARN)

    def test_quality_gate_transition_scenario_limit_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('trusted_case', 'current', 'target', '02', '[]',
                              'reviewed', '2026-06-16T00:00:00Z', '2026-06-16T00:00:00Z')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("ncs_mcp.quality_gates.evaluate_training_transition_scenarios") as mocked:
                mocked.return_value = {
                    "ok": True,
                    "scenario_count": 1,
                    "current_scope_accuracy": 1.0,
                    "target_scope_accuracy": 1.0,
                    "expected_course_recall_at_k": 1.0,
                    "precision_at_k": 1.0,
                    "top1_expected_hit_rate": 1.0,
                    "mrr_at_k": 1.0,
                    "map_at_k": 1.0,
                    "ndcg_at_k": 1.0,
                }

                evaluate_quality_gates(
                    db_path,
                    include_transition_evaluation=True,
                    transition_limit=3,
                    transition_scenario_limit=2,
                )

            self.assertEqual(mocked.call_args.kwargs["limit"], 3)
            self.assertEqual(mocked.call_args.kwargs["scenario_limit"], 2)

    def test_quality_gate_markdown_includes_warning_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "quality_gates.md"
            write_quality_gate_markdown(
                {
                    "status": WARN,
                    "ok": True,
                    "summary": {"fail_count": 0, "warn_count": 1, "pass_count": 0},
                    "gates": [
                        {
                            "status": WARN,
                            "name": "transition_eval:trusted_scenarios",
                            "message": "No trusted scenarios.",
                            "value": 0,
                            "threshold": "> 0",
                            "details": {"status_counts": {"candidate": 3}},
                        }
                    ],
                },
                markdown_path,
            )

            text = markdown_path.read_text(encoding="utf-8")

        self.assertIn("details", text)
        self.assertIn("candidate", text)


if __name__ == "__main__":
    unittest.main()
