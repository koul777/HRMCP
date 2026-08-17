from __future__ import annotations

import hashlib
import tempfile
import unittest
import sys
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.quality_gates import (
    FAIL,
    PASS,
    TRUSTED_LABEL_REVIEW_STATUSES,
    WARN,
    _add_qualification_gates,
    _add_transition_evaluation_gates,
    _transition_recommendation_signal_summary,
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

    def _write_non_hr_surface_report(
        self,
        path: Path,
        *,
        schema: str,
        education_plan: bool = False,
        ok: bool = True,
        source_payload_exposed: bool = False,
        sqf_used: bool = False,
        learning_modules_used: bool = False,
    ) -> None:
        row = {
            "id": "case1",
            "ok": ok,
            "db_writes": False,
            "status_update_allowed": False,
            "approval_claim": False,
            "sqf_used": sqf_used,
            "learning_modules_used": learning_modules_used,
        }
        if education_plan:
            row.update(
                {
                    "plan_ok": ok,
                    "matrix_rows": 1,
                    "recommended_path_stage_count": 4,
                    "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                    "guide_trace_check_count": 6,
                    "guide_trace_check_codes": [
                        "job_scope",
                        "task_ksa",
                        "course_link",
                        "required_optional",
                        "level_delivery",
                        "human_review",
                    ],
                    "guide_workflow_stage_codes": [
                        "C1-1",
                        "C1-2",
                        "C2-1",
                        "C2-2",
                    ],
                    "query_route_schema": "ncs_query_route_v1",
                    "query_route_tool": "plan_ncs_education_path",
                    "query_route_fingerprint": "route-fp",
                    "query_route_expected_tool_chain": [
                        "plan_ncs_education_path",
                        "recommend_training_transition",
                    ],
                    "query_route_contract_schema": "ncs_query_route_v1",
                    "query_route_contract_primary_tool": "plan_ncs_education_path",
                    "query_route_contract_fingerprint": "route-fp",
                    "missing_matrix_fields": [],
                    "missing_plan_fields": [],
                    "missing_guide_trace_fields": [],
                    "missing_query_route_fields": [],
                }
            )
        payload = {
            "schema": schema,
            "ok": ok,
            "report_only": True,
            "db_writes": False,
            "status_update_allowed": False,
            "approval_claim": False,
            "human_decision_required": False,
            "review_policy": {
                "report_only_smoke_check": True,
                "uses_readonly_sqlite_connection": True,
                "recommendation_calls_use_save_false": True,
                "do_not_write_human_reviewed_accepted_reviewed": True,
                "recommendations_are_not_official_approval": True,
                "sqf_and_learning_modules_must_not_be_active_sources": True,
                "raw_ncs_tables_not_mutated": True,
            },
            "case_count": 1,
            "ok_count": 1 if ok else 0,
            "failed_count": 0 if ok else 1,
            "source_payload_exposed": source_payload_exposed,
            "sensitive_markers": ["source_payload"] if source_payload_exposed else [],
            "rows": [row],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
            self.assertIn(
                "ontology_concept_label_candidates",
                result["evidence"]["ontology"]["counts"],
            )
            self.assertIn(
                "label_candidates_missing_provenance",
                result["evidence"]["ontology"]["metrics"],
            )

    def test_non_hr_surface_smoke_artifacts_add_pass_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            query_path = tmp_path / "non_hr_query_smoke.json"
            transition_path = tmp_path / "non_hr_transition_smoke.json"
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                query_path,
                schema="ncs_non_hr_query_smoke_v1",
            )
            self._write_non_hr_surface_report(
                transition_path,
                schema="ncs_non_hr_transition_smoke_v1",
            )
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
            )

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_query_smoke": query_path,
                    "non_hr_transition_smoke": transition_path,
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            self.assertEqual(gates["non_hr_surface:non_hr_query_smoke"]["status"], PASS)
            self.assertEqual(gates["non_hr_surface:non_hr_transition_smoke"]["status"], PASS)
            self.assertEqual(gates["non_hr_surface:non_hr_education_plan_smoke"]["status"], PASS)
            self.assertTrue(result["report_only"])
            self.assertFalse(result["db_writes"])
            self.assertFalse(result["status_update_allowed"])
            self.assertFalse(result["approval_claim"])
            self.assertTrue(
                result["evidence"]["non_hr_surface_smoke"]["non_hr_education_plan_smoke"]["ok"]
            )

    def test_non_hr_surface_smoke_artifacts_fail_on_sensitive_or_legacy_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
                source_payload_exposed=True,
                sqf_used=True,
            )

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            gate = gates["non_hr_surface:non_hr_education_plan_smoke"]
            self.assertEqual(gate["status"], FAIL)
            self.assertIn("sensitive_markers", gate["details"]["issues"])
            self.assertIn("row_1:sqf_used:True", gate["details"]["issues"])
            self.assertEqual(gates["non_hr_surface:non_hr_query_smoke"]["status"], FAIL)

    def test_non_hr_surface_smoke_artifacts_independently_scan_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
            )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["source_payload_exposed"] = False
            payload["sensitive_markers"] = []
            payload["rows"][0]["debug"] = {
                "raw_payload": {"serviceKey": "SHOULD_NOT_PASS"}
            }
            plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            gate = gates["non_hr_surface:non_hr_education_plan_smoke"]
            self.assertEqual(gate["status"], FAIL)
            self.assertTrue(
                any(
                    issue.startswith("artifact_sensitive_markers:")
                    for issue in gate["details"]["issues"]
                )
            )

    def test_non_hr_surface_smoke_artifacts_require_review_policy_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
            )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["human_decision_required"] = True
            payload["review_policy"].pop("do_not_write_human_reviewed_accepted_reviewed")
            plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            issues = gates["non_hr_surface:non_hr_education_plan_smoke"]["details"]["issues"]
            self.assertIn("human_decision_required:True", issues)
            self.assertIn(
                "review_policy.do_not_write_human_reviewed_accepted_reviewed:None",
                issues,
            )

    def test_non_hr_surface_smoke_artifacts_fail_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            plan_path.write_text("{not valid json", encoding="utf-8")

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            gate = gates["non_hr_surface:non_hr_education_plan_smoke"]
            self.assertEqual(gate["status"], FAIL)
            self.assertEqual(
                result["evidence"]["non_hr_surface_smoke"]["non_hr_education_plan_smoke"]["ok"],
                False,
            )
            self.assertIn("error", gate["details"])

    def test_non_hr_education_plan_surface_requires_route_contract_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
            )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["rows"][0].pop("query_route_contract_schema")
            payload["rows"][0]["guide_trace_check_count"] = 1
            plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            issues = gates["non_hr_surface:non_hr_education_plan_smoke"]["details"]["issues"]
            self.assertIn("row_1:query_route_contract_schema:None", issues)

    def test_non_hr_education_plan_surface_rejects_malformed_guide_trace_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            self._init_db(db_path)
            plan_path = tmp_path / "non_hr_education_plan_smoke.json"
            self._write_non_hr_surface_report(
                plan_path,
                schema="ncs_non_hr_education_plan_smoke_v1",
                education_plan=True,
            )
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            payload["rows"][0]["guide_trace_check_codes"] = [
                "duplicate",
                "duplicate",
                "duplicate",
                "duplicate",
                "duplicate",
                "duplicate",
            ]
            payload["rows"][0]["guide_workflow_stage_codes"] = ["C1-1"]
            plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = evaluate_quality_gates(
                db_path,
                non_hr_surface_artifact_paths={
                    "non_hr_education_plan_smoke": plan_path,
                },
            )

            gates = {gate["name"]: gate for gate in result["gates"]}
            issues = gates["non_hr_surface:non_hr_education_plan_smoke"]["details"]["issues"]
            self.assertIn("row_1:guide_trace_check_codes_missing:job_scope", issues)
            self.assertIn("row_1:guide_workflow_stage_codes_missing:C2-2", issues)

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

    def test_api_element_collection_failures_are_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('14', 'Construction', '14', 'Construction', '14', 'Construction', '01', 'Construction planning')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('1401010101_25v1', '1401010101', '25v1', 'Construction planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw, api_match_status
                    ) VALUES ('1401010101_25v1', '1', 'E1', 'Plan construction work', '5', 'api_failed')
                    """
                )
                element_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('element', ?, 'api_element_unmatched', 'warning',
                              'NCS006 request failed after retries.', 'Retry later', ?)
                    """,
                    (str(element_id), timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = evaluate_quality_gates(db_path)
            gates = {gate["name"]: gate for gate in result["gates"]}

        self.assertEqual(gates["quality_issues:api_element_unmatched"]["value"], 0)
        self.assertEqual(gates["quality_issues:api_element_collection_failure"]["value"], 1)
        self.assertEqual(gates["quality_issues:api_element_collection_failure"]["status"], "pass")

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

    def test_audited_human_reviewed_label_candidate_is_not_ontology_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                ts = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '4', ?, ?, ?)
                    """,
                    (classification_id, ts, ts),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'Workforce plan', '4')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'workforce planning source')
                    """,
                    (element_id,),
                )
                ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('workforce planning source', 'workforceplanningsource',
                              'knowledge', 'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (ts, ts),
                )
                concept_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_scope_key, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                              'workforce planning source', 'workforce planning',
                              'workforceplanning', 'short_representative_label',
                              'rule_based_short_label_candidate', 1, 0.8,
                              'human_reviewed', ?, ?)
                    """,
                    (concept_id, ksa_id, ts, ts),
                )
                label_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status,
                        new_status, reviewer_id, notes, created_at
                    ) VALUES ('ontology_concept_label_candidate', ?, 'ksa_label_approve',
                              'candidate', 'human_reviewed', 'tester',
                              'human checked source and label', ?)
                    """,
                    (str(label_id), ts),
                )
                conn.commit()
            finally:
                conn.close()

            result = evaluate_quality_gates(db_path)

        checks = {issue["check"] for issue in result["evidence"]["ontology"]["issues"]}
        metrics = result["evidence"]["ontology"]["metrics"]
        self.assertNotIn("ontology_concept_label_candidates.review_status", checks)
        self.assertEqual(metrics["trusted_label_candidate_statuses"], 1)
        self.assertEqual(metrics["audited_trusted_label_candidate_statuses"], 1)
        self.assertEqual(metrics["unaudited_trusted_label_candidate_statuses"], 0)

    def test_llm_reviewed_label_candidate_is_not_human_trusted_review(self) -> None:
        self.assertNotIn("llm_reviewed", TRUSTED_LABEL_REVIEW_STATUSES)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                ts = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '4', ?, ?, ?)
                    """,
                    (classification_id, ts, ts),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'Workforce plan', '4')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'workforce planning source')
                    """,
                    (element_id,),
                )
                ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('workforce planning source', 'workforceplanningsource',
                              'knowledge', 'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (ts, ts),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_scope_key, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                              'workforce planning source', 'workforce planning',
                              'workforceplanning', 'short_representative_label',
                              'rule_based_short_label_candidate', 1, 0.8,
                              'llm_reviewed', ?, ?)
                    """,
                    (concept_id, ksa_id, ts, ts),
                )
                for meaning_role, source_method, review_status in (
                    ("term_definition_candidate", "term_definition_template", "llm_reviewed"),
                    ("task_knowledge_significance", "task_context_template", "needs_review"),
                    ("task_knowledge_significance", "unlinked_concept_fallback", "candidate"),
                ):
                    conn.execute(
                        """
                        INSERT INTO ksa_meaning_candidates(
                            concept_id, concept_type, meaning_role, meaning_text,
                            source_method, evidence_text, confidence_score,
                            review_status, created_at, updated_at
                        ) VALUES (?, 'knowledge', ?, 'workforce planning meaning',
                                  ?, 'unit: HR planning', 0.72, ?, ?, ?)
                        """,
                        (concept_id, meaning_role, source_method, review_status, ts, ts),
                    )
                conn.commit()
            finally:
                conn.close()

            result = evaluate_quality_gates(db_path)

        checks = {issue["check"] for issue in result["evidence"]["ontology"]["issues"]}
        metrics = result["evidence"]["ontology"]["metrics"]
        self.assertNotIn("ontology_concept_label_candidates.review_status", checks)
        self.assertEqual(metrics["trusted_label_candidate_statuses"], 0)
        self.assertEqual(metrics["llm_reviewed_label_candidate_statuses"], 1)
        self.assertEqual(metrics["llm_reviewed_meaning_candidate_statuses"], 1)
        self.assertEqual(metrics["needs_review_meaning_candidate_statuses"], 1)
        self.assertEqual(metrics["candidate_meaning_candidate_statuses"], 1)
        self.assertEqual(metrics["audited_trusted_label_candidate_statuses"], 0)
        self.assertEqual(metrics["unaudited_trusted_label_candidate_statuses"], 0)

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

    def test_transition_gates_surface_quality_penalty_and_job_base_signals(self) -> None:
        gates: list[dict] = []

        evaluation = {
            "ok": True,
            "scenario_count": 1,
            "current_scope_accuracy": 1.0,
            "target_scope_accuracy": 1.0,
            "expected_course_recall_at_k": 1.0,
            "top1_expected_hit_rate": 1.0,
            "precision_at_k": 1.0,
            "precision_at_k_upper_bound": 1.0,
            "precision_at_k_relative_to_upper_bound": 1.0,
            "ndcg_at_k": 1.0,
            "recommended_course_total": 1,
            "expected_course_total": 1,
            "possible_expected_course_hit_count": 1,
            "cases": [
                {
                    "ok": True,
                    "recommended_course_evidence": [
                        {
                            "course_name": "인사기획",
                            "quality_issue_penalty": {
                                "applied": True,
                                "issue_types": ["short_ksa", "broad_generic_ksa"],
                            },
                            "job_base_signal": {
                                "status": "gap_bridge",
                                "target_hit_count": 1,
                                "gap_hit_count": 2,
                            },
                            "review_flags": [
                                "quality_issue:short_ksa",
                                "quality_issue:broad_generic_ksa",
                            ],
                        }
                    ],
                }
            ],
        }

        _add_transition_evaluation_gates(gates, evaluation)

        by_name = {gate["name"]: gate for gate in gates}
        penalty_gate = by_name["transition_eval:quality_issue_penalty_review_surface"]
        self.assertEqual(penalty_gate["status"], WARN)
        self.assertEqual(penalty_gate["value"], 1)
        self.assertEqual(penalty_gate["details"]["quality_issue_counts"]["short_ksa"], 1)
        self.assertEqual(
            penalty_gate["details"]["quality_review_flag_counts"]["quality_issue:broad_generic_ksa"],
            1,
        )
        self.assertEqual(penalty_gate["details"]["course_names"], ["인사기획"])

        job_base_gate = by_name["transition_eval:job_base_signal_surface"]
        self.assertEqual(job_base_gate["status"], "pass")
        self.assertEqual(job_base_gate["value"], 1)
        self.assertEqual(job_base_gate["details"]["job_base_status_counts"]["gap_bridge"], 1)
        self.assertEqual(job_base_gate["details"]["job_base_gap_hit_count"], 2)

    def test_transition_recommendation_signal_summary_warns_when_job_base_missing(self) -> None:
        summary = _transition_recommendation_signal_summary(
            {
                "cases": [
                    {
                        "ok": True,
                        "recommended_course_evidence": [
                            {
                                "course_name": "과정A",
                                "quality_issue_penalty": {},
                                "review_flags": [],
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(summary["recommended_course_evidence_count"], 1)
        self.assertEqual(summary["job_base_signal_course_count"], 0)

    def test_transition_job_base_signal_summary_ignores_placeholder_signal(self) -> None:
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
                "precision_at_k": 1.0,
                "precision_at_k_upper_bound": 1.0,
                "precision_at_k_relative_to_upper_bound": 1.0,
                "ndcg_at_k": 1.0,
                "recommended_course_total": 1,
                "expected_course_total": 1,
                "possible_expected_course_hit_count": 1,
                "cases": [
                    {
                        "ok": True,
                        "recommended_course_evidence": [
                            {
                                "course_name": "과정A",
                                "job_base_signal": {
                                    "status": "not_available",
                                    "target_hit_count": 0,
                                    "gap_hit_count": 0,
                                },
                            }
                        ],
                    }
                ],
            },
        )

        by_name = {gate["name"]: gate for gate in gates}
        gate = by_name["transition_eval:job_base_signal_surface"]
        self.assertEqual(gate["status"], WARN)
        self.assertEqual(gate["value"], 0)
        self.assertEqual(gate["details"]["job_base_signal_field_count"], 1)
        self.assertEqual(gate["details"]["job_base_status_counts"]["not_available"], 1)

    def test_quality_gate_transition_scenario_limit_is_forwarded(self) -> None:
        reports_root = ROOT / "reports"
        reports_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            prefix="quality_gate_packet_", dir=reports_root
        ) as packet_tmp:
            db_path = Path(tmp) / "ncs.db"
            packet_path = Path(packet_tmp) / "review_packet.md"
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path.write_text("# review packet\n", encoding="utf-8")
            packet_hash = "sha256:" + hashlib.sha256(packet_path.read_bytes()).hexdigest()
            conn = connect(db_path)
            try:
                initialize_database(conn)
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_id, scenario_name, current_query, target_query, major_code,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES (1001, 'trusted_case', 'current', 'target', '02', '[]',
                              'reviewed', '2026-06-16T00:00:00Z', '2026-06-16T00:00:00Z')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, source_artifact_hash, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1001',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?, ?,
                        'explicit scenario approval rationale', '["scenario:1001"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path), packet_hash),
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
            self.assertEqual(mocked.call_args.kwargs["scenario_ids"], [1001])

    def test_quality_gate_requires_single_packet_backed_human_audit_row(self) -> None:
        reports_root = ROOT / "reports"
        reports_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(
            prefix="quality_gate_packet_", dir=reports_root
        ) as packet_tmp:
            db_path = Path(tmp) / "ncs.db"
            packet_path = Path(packet_tmp) / "transition_review_packet.md"
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path.write_text("# transition review packet\n", encoding="utf-8")
            packet_hash = "sha256:" + hashlib.sha256(packet_path.read_bytes()).hexdigest()
            off_repo_packet = Path(tmp) / "external" / "reports" / "off_repo_packet.md"
            off_repo_packet.parent.mkdir(parents=True, exist_ok=True)
            off_repo_packet.write_text("# off repo packet\n", encoding="utf-8")
            off_repo_packet_hash = "sha256:" + hashlib.sha256(off_repo_packet.read_bytes()).hexdigest()
            conn = connect(db_path)
            try:
                initialize_database(conn)
                for scenario_id in (
                    1001,
                    1003,
                    1004,
                    1005,
                    1006,
                    1007,
                    1008,
                    1009,
                    1010,
                    1011,
                    1012,
                    1013,
                    1014,
                    1015,
                ):
                    conn.execute(
                        """
                        INSERT INTO training_transition_gold_scenarios(
                            scenario_id, scenario_name, current_query, target_query,
                            major_code, expected_course_names_json, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'current', 'target', '02', '[]',
                                  'reviewed', '2026-06-16T00:00:00Z',
                                  '2026-06-16T00:00:00Z')
                        """,
                        (scenario_id, f"case_{scenario_id}"),
                    )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1001',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'explicit scenario approval rationale', '["scenario:1001"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1003',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?, '', '',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1003',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        '', '',
                        'rationale split across rows is not packet-backed',
                        '["scenario:1003"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1004',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', 'reports/missing_transition_packet.md',
                        'missing packet must not be trusted', '["scenario:1004"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1005',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'automation', ?,
                        'automated reviewer must not be trusted',
                        '["scenario:1005"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1006',
                        'packet_backed_human_review', 'candidate', 'candidate',
                        'human_reviewer', ?,
                        'status mismatch must not be trusted',
                        '["scenario:1006"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1007',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'parseable non-list evidence must not be trusted', '{}',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1008',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'empty evidence list must not be trusted', '[]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1009',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'automation', ?,
                        'automated reviewer is an independent blocker',
                        '["scenario:1009"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1009',
                        'packet_backed_human_review', 'candidate', 'candidate',
                        'human_reviewer', ?,
                        'status mismatch is an independent blocker',
                        '["scenario:1009"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1010',
                        'note_added', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'non-approval audit actions must not be trusted',
                        '["scenario:1010"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1011',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'off-repo reports paths must not be trusted',
                        '["scenario:1011"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(off_repo_packet),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1012',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'numeric evidence refs must not be trusted', '[1]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1013',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'null evidence refs must not be trusted', '[null]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, rationale,
                        evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1014',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'blank evidence refs must not be trusted', '[""]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
                )
                conn.execute(
                    """
                    UPDATE review_audit_log
                    SET source_artifact_hash = ?
                    WHERE source_decision_packet = ?
                    """,
                    (packet_hash, str(packet_path)),
                )
                conn.execute(
                    """
                    UPDATE review_audit_log
                    SET source_artifact_hash = ?
                    WHERE source_decision_packet = ?
                    """,
                    (off_repo_packet_hash, str(off_repo_packet)),
                )
                conn.execute(
                    """
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status,
                        reviewer_id, source_decision_packet, source_artifact_hash,
                        rationale, evidence_refs_json, created_by_tool, created_at
                    ) VALUES (
                        'training_transition_gold_scenario', '1015',
                        'packet_backed_human_review', 'candidate', 'reviewed',
                        'human_reviewer', ?,
                        'sha256:0000000000000000000000000000000000000000000000000000000000000000',
                        'hash mismatch must not be trusted', '["scenario:1015"]',
                        'manual_review_tool', '2026-06-16T00:00:00Z'
                    )
                    """,
                    (str(packet_path),),
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

                result = evaluate_quality_gates(
                    db_path,
                    include_transition_evaluation=True,
                    transition_limit=3,
                    transition_scenario_limit=5,
                )

            self.assertEqual(mocked.call_args.kwargs["scenario_ids"], [1001])
            provenance = result["evidence"]["transition_evaluation"][
                "trusted_scenario_provenance"
            ]
            self.assertEqual(provenance["packet_backed_scenario_ids"], [1001])
            self.assertEqual(
                provenance["legacy_trusted_scenario_ids"],
                [1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014, 1015],
            )
            missing_counts = provenance["missing_packet_backed_field_counts"]
            self.assertGreaterEqual(missing_counts["rationale"], 1)
            self.assertEqual(missing_counts["same_audit_row"], 1)
            self.assertGreaterEqual(missing_counts["packet_backed_human_review_action"], 1)
            self.assertGreaterEqual(
                missing_counts["source_decision_packet_resolves_to_reports_artifact"],
                2,
            )
            self.assertGreaterEqual(missing_counts["human_reviewer_id"], 1)
            self.assertGreaterEqual(missing_counts["new_status_matches_scenario"], 1)
            self.assertGreaterEqual(missing_counts["evidence_refs_json_valid"], 5)
            self.assertGreaterEqual(
                missing_counts["source_artifact_hash_matches_reports_artifact"],
                1,
            )

    def test_quality_gate_excludes_legacy_trusted_transition_scenarios_without_packet_backing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_id, scenario_name, current_query, target_query, major_code,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES (1002, 'legacy_case', 'current', 'target', '02', '[]',
                              'reviewed', '2026-06-16T00:00:00Z', '2026-06-16T00:00:00Z')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("ncs_mcp.quality_gates.evaluate_training_transition_scenarios") as mocked:
                result = evaluate_quality_gates(
                    db_path,
                    include_transition_evaluation=True,
                    transition_limit=3,
                    transition_scenario_limit=2,
                )

            mocked.assert_not_called()
            gate = next(
                gate
                for gate in result["gates"]
                if gate["name"] == "transition_eval:trusted_scenarios"
            )
            provenance = gate["details"]["trusted_scenario_provenance"]
            self.assertEqual(gate["status"], WARN)
            self.assertEqual(gate["value"], 0)
            self.assertEqual(provenance["raw_trusted_scenario_count"], 1)
            self.assertEqual(provenance["packet_backed_scenario_count"], 0)
            self.assertEqual(provenance["legacy_trusted_scenario_count"], 1)
            self.assertEqual(provenance["legacy_trusted_scenario_ids"], [1002])
            self.assertEqual(
                result["evidence"]["transition_evaluation"]["skip_reason"],
                "no_packet_backed_trusted_transition_gold_scenarios",
            )

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

    def test_quality_gate_markdown_includes_job_base_signal_details_when_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "quality_gates.md"
            write_quality_gate_markdown(
                {
                    "status": PASS,
                    "ok": True,
                    "summary": {"fail_count": 0, "warn_count": 0, "pass_count": 1},
                    "gates": [
                        {
                            "status": PASS,
                            "name": "transition_eval:job_base_signal_surface",
                            "message": "Job-base competency auxiliary signals are surfaced.",
                            "value": 1,
                            "threshold": "> 0",
                            "details": {"job_base_status_counts": {"gap_bridge": 1}},
                        }
                    ],
                },
                markdown_path,
            )

            text = markdown_path.read_text(encoding="utf-8")

        self.assertIn("job_base_status_counts", text)


if __name__ == "__main__":
    unittest.main()
