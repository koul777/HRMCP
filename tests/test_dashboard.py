from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.agent_queue import build_agent_queue_status_from_file

from ncs_harness import AIHR_DASHBOARD_STATIC_ARTIFACT_ENV

from ncs_dashboard import (
    DashboardHandler,
    HTML,
    KSA_LABEL_AUTO_TRIAGE_TABLES,
    apply_aihr_artifact_overrides,
    build_aihr_live_plan,
    connect_db_readonly,
    get_api_orphans,
    get_classifications,
    get_item_detail,
    get_items,
    get_ksa_definitions,
    get_ksa_label_auto_triage,
    get_ksa_label_patterns,
    get_issues,
    get_concepts,
    get_ontology,
    get_ontology_status,
    get_progress,
    get_ksa_preprocessing_review_status,
    get_status,
    get_taxonomy,
    get_unit_detail,
    get_units,
    get_workbench,
    get_query_router_samples,
    aihr_agent_queue_run_artifact_issues,
    is_dashboard_loopback_host,
    render_aihr_live_html,
    render_aihr_training_system_builder_html,
    render_aihr_agent_queue_html,
    render_aihr_agent_queue_run_html,
    render_aihr_agent_queue_status_html,
    render_aihr_review_board_html,
    render_aihr_provenance_reconfirmation_html,
    render_aihr_readiness_html,
    render_ksa_definition_dashboard_html,
    render_ksa_label_auto_triage_html,
    render_ksa_label_patterns_html,
    render_ksa_review_dashboard_html,
    render_ksa_preprocessing_dashboard_html,
    render_ontology_review_board_html,
    render_query_router_samples_html,
    is_actual_aihr_agent_queue_run,
    aihr_agent_queue_expected_globs,
    load_review_seedpack_payload,
    public_aihr_dashboard_payload,
    public_aihr_provenance_reconfirmation_payload,
    resolve_ksa_review_html_path,
    resolve_aihr_agent_queue_json_path,
    resolve_aihr_agent_queue_run_json_path,
    resolve_aihr_agent_queue_status_json_path,
    resolve_aihr_demo_html_path,
    resolve_aihr_provenance_reconfirmation_json_path,
    resolve_aihr_readiness_json_path,
    resolve_aihr_review_triage_json_path,
    resolve_review_seedpack_jsonl_path,
    validate_dashboard_bind_host,
    validate_dashboard_port_identity,
    probe_dashboard_http_identity,
    DASHBOARD_ROOT_IDENTITY_MARKERS,
    KSA_LABEL_NEEDS_REVIEW_HTML_GLOB,
    KSA_MEANING_NEEDS_REVIEW_HTML_GLOB,
    KSA_MEANING_MISSING_SCOPED_HTML_GLOB,
    KSA_PREPROCESSING_PIPELINE_HTML_GLOB,
    review_mapping_candidate,
    edit_ksa_label_candidate,
    review_ksa_label_candidate,
    review_ksa_meaning_candidate,
    review_refinement_job,
    save_manual_preprocess,
    sanitize_aihr_agent_queue_run_payload,
    sanitize_aihr_agent_queue_public_paths,
    _label_review_progress_from_counts,
)

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, insert_quality_issue, now_utc
from ncs_mcp.server import trusted_review_provenance_blockers


def canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


AIHR_TEST_ISOLATION_ENV_VARS = tuple(
    dict.fromkeys(
        [
            "NCS_AIHR_AGENT_QUEUE_JSON_PATH",
            *AIHR_DASHBOARD_STATIC_ARTIFACT_ENV.values(),
        ]
    )
)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_aihr_env = {
            key: os.environ.pop(key, None) for key in AIHR_TEST_ISOLATION_ENV_VARS
        }

    def tearDown(self) -> None:
        for key in AIHR_TEST_ISOLATION_ENV_VARS:
            os.environ.pop(key, None)
        for key, value in self._saved_aihr_env.items():
            if value is not None:
                os.environ[key] = value

    def test_trusted_review_provenance_requires_repo_local_reports_packet(self) -> None:
        reports_dir = ROOT / "reports" / "_test_review_packets"
        reports_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=reports_dir) as repo_packet_tmp:
            self.addCleanup(shutil.rmtree, repo_packet_tmp, ignore_errors=True)
            repo_packet = Path(repo_packet_tmp) / "review_packet.md"
            repo_packet.write_text("# review packet\n", encoding="utf-8")
            repo_hash = "sha256:" + hashlib.sha256(repo_packet.read_bytes()).hexdigest()

            self.assertEqual(
                trusted_review_provenance_blockers(
                    review_status="human_reviewed",
                    reviewer_id="tester",
                    source_decision_packet=str(repo_packet),
                    source_artifact_hash=repo_hash,
                    rationale="human decision rationale",
                ),
                [],
            )

        with tempfile.TemporaryDirectory() as tmp:
            off_repo_packet = Path(tmp) / "reports" / "review_packet.md"
            off_repo_packet.parent.mkdir(parents=True, exist_ok=True)
            off_repo_packet.write_text("# off repo packet\n", encoding="utf-8")
            off_repo_hash = "sha256:" + hashlib.sha256(off_repo_packet.read_bytes()).hexdigest()

            blockers = trusted_review_provenance_blockers(
                review_status="human_reviewed",
                reviewer_id="tester",
                source_decision_packet=str(off_repo_packet),
                source_artifact_hash=off_repo_hash,
                rationale="human decision rationale",
            )

        self.assertIn(
            "trusted_status_requires_packet_backed_source_decision_packet",
            blockers,
        )

    def _write_review_packet(
        self,
        tmp: str,
        filename: str,
        reference: str,
        *,
        extra: str = "",
        reviewer_id: str = "tester",
        decision: str = "approve",
    ) -> tuple[str, str]:
        reports_dir = ROOT / "reports" / "_test_review_packets" / Path(tmp).name
        reports_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, reports_dir, ignore_errors=True)
        requested = Path(filename)
        stem = requested.stem
        if "decision_audit" not in stem:
            stem = f"{stem}_decision_audit"
        packet_path = reports_dir / f"{stem}.json"
        payload = {
            "schema": "ncs_dashboard_review_decision_audit_v1",
            "ok": True,
            "human_decision_required": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "trusted_status_write_allowed": False,
            "row_count": 1,
            "completed_decision_count": 1,
            "invalid_decision_count": 0,
            "unsafe_flag_count": 0,
            "policy": {
                "report_only": True,
                "requires_explicit_human_decision": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            },
            "rows": [
                {
                    "reference": reference,
                    "extra": extra,
                    "decision": decision,
                    "reviewer_id": reviewer_id,
                    "reviewed_at": "2026-06-29T00:00:00+00:00",
                    "rationale": "test human decision fixture",
                    "completed": True,
                    "valid": True,
                    "action_eligible": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                }
            ],
        }
        packet_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        packet_path.write_bytes(packet_bytes)
        return (
            f"{packet_path}#{reference}",
            "sha256:" + hashlib.sha256(packet_bytes).hexdigest(),
        )

    def _write_raw_review_packet(
        self,
        tmp: str,
        filename: str,
        text: str,
        reference: str,
    ) -> tuple[str, str]:
        reports_dir = ROOT / "reports" / "_test_review_packets" / Path(tmp).name
        reports_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, reports_dir, ignore_errors=True)
        packet_path = reports_dir / filename
        packet_bytes = text.encode("utf-8")
        packet_path.write_bytes(packet_bytes)
        return (
            f"{packet_path}#{reference}",
            "sha256:" + hashlib.sha256(packet_bytes).hexdigest(),
        )

    def _empty_dashboard_db(self, tmp: str) -> Path:
        db_path = Path(tmp) / "dashboard.db"
        conn = connect(db_path)
        try:
            initialize_database(conn)
        finally:
            conn.close()
        return db_path

    def test_label_review_progress_distinguishes_human_and_triage_actioned(self) -> None:
        progress = _label_review_progress_from_counts(
            {
                "human_reviewed": 1,
                "llm_reviewed": 1,
                "needs_review": 1,
                "candidate": 1,
            }
        )

        self.assertEqual(progress["total"], 4)
        self.assertEqual(progress["human_reviewed"], 1)
        self.assertEqual(progress["llm_reviewed"], 1)
        self.assertEqual(progress["needs_review"], 1)
        self.assertEqual(progress["pending"], 1)
        self.assertEqual(progress["checked"], 1)
        self.assertEqual(progress["human_checked"], 1)
        self.assertEqual(progress["machine_screened"], 1)
        self.assertEqual(progress["automated_actioned"], 2)
        self.assertEqual(progress["actioned"], 2)
        self.assertEqual(progress["human_reviewed_percent"], 25.0)
        self.assertEqual(progress["checked_percent"], 25.0)
        self.assertEqual(progress["human_checked_percent"], 25.0)
        self.assertEqual(progress["machine_screened_percent"], 25.0)
        self.assertEqual(progress["automated_actioned_percent"], 50.0)
        self.assertEqual(progress["actioned_percent"], 50.0)
        self.assertEqual(progress["coverage_percent"], 50.0)

    def test_ksa_label_patterns_use_audited_major_seed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()

            def add_label(
                major_code: str,
                label_text: str,
                normalized_key: str,
                review_status: str,
                *,
                source_method: str = "rule_based_short_label_candidate",
                unit_suffix: str,
                audited: bool = False,
            ) -> int:
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES (?, ?, ?, 'Middle', '01', 'Small', '01', 'Sub')
                    """,
                    (major_code, f"Major {major_code}", unit_suffix),
                )
                classification_id = cur.lastrowid
                unit_code = f"{major_code}010101{unit_suffix}_26v1"
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES (?, ?, '26v1', ?, '4', ?, ?, ?)
                    """,
                    (
                        unit_code,
                        unit_code[:10],
                        f"Unit {unit_suffix}",
                        classification_id,
                        timestamp,
                        timestamp,
                    ),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES (?, '1', '01', 'Element', '4')
                    """,
                    (unit_code,),
                )
                element_id = cur.lastrowid
                source_text = f"{label_text} source evidence"
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', ?)
                    """,
                    (element_id, source_text),
                )
                ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, 'knowledge', 'candidate', 'linked',
                              'model_preprocessed', ?, ?)
                    """,
                    (source_text, f"{normalized_key}{unit_suffix}", timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                        concept_type, source_text, label_text, normalized_label_key,
                        label_role, source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, 'knowledge', ?, ?, ?,
                              'short_representative_label', ?, 1, 0.8,
                              ?, ?, ?)
                    """,
                    (
                        concept_id,
                        ksa_id,
                        f"{major_code}:01:01:01",
                        source_text,
                        label_text,
                        normalized_key,
                        source_method,
                        review_status,
                        timestamp,
                        timestamp,
                    ),
                )
                label_id = cur.lastrowid
                if audited:
                    conn.execute(
                        """
                        INSERT INTO review_audit_log(
                            entity_type, entity_id, action, previous_status, new_status,
                            reviewer_id, notes, source_decision_packet,
                            source_artifact_hash, rationale, evidence_refs_json,
                            created_by_tool, run_artifact, created_at
                        ) VALUES (
                            'ontology_concept_label_candidate', ?, 'ksa_label_approve',
                            'llm_reviewed', ?, 'tester', 'seed approved',
                            'seed-packet', 'sha256:test', 'seed approved',
                            '[]', 'test', '/ksa-review-dashboard', ?
                        )
                        """,
                        (str(label_id), review_status, timestamp),
                    )
                return label_id

            add_label("01", "Project planning", "projectplanning", "human_reviewed", unit_suffix="01", audited=True)
            add_label("01", "Review", "review", "needs_review", unit_suffix="02")
            add_label("02", "Project planning", "projectplanning", "llm_reviewed", unit_suffix="03")
            add_label("02", "Review", "review", "llm_reviewed", unit_suffix="04")
            add_label("02", "관리", "관리", "llm_reviewed", unit_suffix="05")
            add_label(
                "02",
                "Document writing",
                "documentwriting",
                "llm_reviewed",
                source_method="already_short_label",
                unit_suffix="06",
            )
            add_label("17", "Chemical process", "chemicalprocess", "llm_reviewed", unit_suffix="07")
            conn.commit()
            conn.close()

            result = get_ksa_label_patterns(db_path, {"seed_major_code": ["01"], "limit": ["2"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema"], "ncs_ksa_label_pattern_groups_v1")
        self.assertFalse(result["policy"]["db_writes"])
        self.assertFalse(result["policy"]["status_update_allowed"])
        self.assertTrue(result["policy"]["seed_human_requires_audit_log"])
        self.assertEqual(result["seed_summary"]["audited_human_label_count"], 1)
        self.assertIn("not an approval signal", result["seed_summary"]["audited_human_label_note"])
        groups = {group["id"]: group for group in result["groups"]}
        self.assertEqual(groups["already_human_reviewed"]["label_count"], 1)
        self.assertEqual(groups["already_human_reviewed"]["existing_trusted_status_count"], 1)
        self.assertIn(
            "not an approval signal",
            groups["already_human_reviewed"]["existing_trusted_status_note"],
        )
        self.assertEqual(groups["seed_approved_same_label"]["label_count"], 1)
        self.assertEqual(groups["current_needs_review"]["label_count"], 1)
        self.assertEqual(groups["seed_hold_same_label"]["label_count"], 1)
        self.assertEqual(groups["generic_or_short"]["label_count"], 1)
        self.assertEqual(groups["low_risk_already_short"]["label_count"], 1)
        self.assertEqual(groups["domain_review_first"]["label_count"], 1)
        self.assertLessEqual(len(groups["seed_approved_same_label"]["samples"]), 2)

    def test_ksa_label_patterns_html_exposes_readonly_menu(self) -> None:
        html = render_ksa_label_patterns_html()

        self.assertIn("KSA 라벨 유형 분류", html)
        self.assertIn("/api/ksa-label-patterns", html)
        self.assertIn("status_update_allowed=false", html)
        self.assertIn("감사기록", html)
        self.assertIn("/ksa-review-dashboard", html)
        self.assertIn("/ksa-label-auto-triage", html)

    def test_ksa_label_auto_triage_api_and_html_are_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "dashboard.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', 'Plan HR', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                    VALUES (?, '01', 'knowledge', '1', 'workforce planning knowledge')
                    """,
                    (element_id,),
                )
                ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition_status,
                        relation_status, review_status, created_at, updated_at
                    ) VALUES ('workforce planning knowledge', 'workforceplanningknowledge',
                              'knowledge', 'candidate', 'none', 'model_preprocessed', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, 'candidate', ?)
                    """,
                    (ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                        concept_type, source_text, label_text, normalized_label_key,
                        label_role, source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, '02:02:02:01', 'knowledge',
                              'workforce planning knowledge', 'workforce planning',
                              'workforceplanning', 'short_representative_label',
                              'rule_based_short_label_candidate', 1, 0.91,
                              'human_reviewed', ?, ?)
                    """,
                    (concept_id, ksa_id, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()
            before_mtime_ns = db_path.stat().st_mtime_ns

            result = get_ksa_label_auto_triage(
                db_path,
                {
                    "major_code": ["02"],
                    "middle_code": ["02"],
                    "small_code": ["02"],
                    "trusted_major_code": ["02"],
                    "trusted_middle_code": ["02"],
                    "trusted_small_code": ["02"],
                    "limit": ["5"],
                },
            )
            html = render_ksa_label_auto_triage_html()
            after_mtime_ns = db_path.stat().st_mtime_ns

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema"], "ksa_label_auto_triage_report_v1")
        self.assertEqual(after_mtime_ns, before_mtime_ns)
        self.assertFalse(result["status_update_allowed"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertFalse(result["safety"]["human_reviewed_written_by_report"])
        self.assertIn("classification_bucket_counts", result)
        self.assertEqual(
            result["classification_v2_schema"],
            "ksa_label_auto_triage_policy_classification_v1",
        )
        self.assertEqual(
            result["classification_v2_counts"]["human-sample-needed"],
            result["bucket_counts"]["human_sample_required"],
        )
        self.assertEqual(
            result["classification_v2_map"]["human_sample_required"]["classification_v2"],
            "human-sample-needed",
        )
        self.assertEqual(result["decision_summary"]["full_scope_decision_row_count"], 1)
        self.assertEqual(result["decision_summary"]["emitted_decision_row_count"], 1)
        self.assertEqual(result["decision_summary"]["full_scope_orphan_raw_concept_backlog_count"], 0)
        self.assertEqual(result["counts"]["full_scope_decision_rows"], 1)
        self.assertEqual(result["bucket_counts"]["auto_pass_candidate"], 0)
        self.assertEqual(result["bucket_counts"]["human_sample_required"], 1)
        self.assertTrue(result["scope_policy"]["target_scope_is_filtered"])
        self.assertTrue(result["scope_policy"]["scoped_counts_are_local_view"])
        self.assertFalse(result["scope_policy"]["scoped_report_is_canonical_bulk_plan"])
        self.assertTrue(result["scope_policy"]["all_scope_required_for_bulk_planning"])
        self.assertTrue(result["safety"]["all_scope_required_for_bulk_planning"])
        self.assertEqual(result["rows"][0]["recommendation_rule"], "trusted_status_missing_audit")
        self.assertEqual(result["rows"][0]["classification_v2"], "human-sample-needed")
        self.assertTrue(result["rows"][0]["requires_human_sample"])
        self.assertFalse(result["rows"][0]["requires_domain_expert"])
        self.assertFalse(result["rows"][0]["audited_trusted_review"])
        self.assertTrue(result["rows"][0]["human_approval_missing"])
        self.assertFalse(result["rows"][0]["recommendation_is_human_approval"])
        self.assertIn("/api/ksa-label-auto-triage", html)
        self.assertIn("not human approval", html)
        self.assertIn("operator decisions", html)
        self.assertIn("orphan raw backlog", html)
        self.assertIn("all-scope policy-v2 report", html)
        self.assertIn("scoped local view", html)
        self.assertIn("02 sample basis only", html)
        self.assertIn("audited existing rows", html)
        self.assertIn("Operator Path", html)
        self.assertIn("ksa-label-policy-v2-sampling-plan", html)
        self.assertIn("ksa_label_policy_v2_operator_handoff_index", html)
        self.assertIn("bucketFilter", html)
        self.assertIn("classificationFilter", html)
        self.assertIn("classification_v2", html)
        self.assertIn("v2 human sample", html)
        self.assertIn("domain-expert-needed", html)
        self.assertIn("domain_expert_required", html)
        self.assertIn("input, select", html)
        self.assertIn("normalizeStaticLabels", html)
        self.assertIn("let latestRows = []", html)
        self.assertIn("catch (err)", html)
        self.assertIn("function errorMessage", html)

    def test_ksa_label_auto_triage_error_payload_keeps_safety_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "empty.db"
            connect(db_path).close()

            result = get_ksa_label_auto_triage(db_path, {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["schema"], "ksa_label_auto_triage_report_v1")
        self.assertEqual(result["error"]["code"], "schema_incomplete")
        self.assertIn("ontology_concept_label_candidates", result["error"]["missing_tables"])
        self.assertFalse(result["status_update_allowed"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertFalse(result["human_reviewed_written_by_report"])
        self.assertFalse(result["safety"]["status_update_allowed"])
        self.assertFalse(result["safety"]["db_writes"])
        self.assertFalse(result["safety"]["approval_claim"])
        self.assertFalse(result["safety"]["human_reviewed_written_by_report"])
        self.assertFalse(result["safety"]["accepted_written_by_report"])
        self.assertFalse(result["safety"]["reviewed_written_by_report"])
        self.assertFalse(result["safety"]["llm_reviewed_is_human_approval"])
        self.assertTrue(result["safety"]["trusted_sample_scope_is_not_approval"])
        self.assertTrue(result["safety"]["trusted_sample_requires_audited_human_review"])
        self.assertTrue(result["safety"]["auto_pass_candidate_is_not_human_approval"])

    def test_ksa_label_auto_triage_partial_schema_returns_safe_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "partial.db"
            conn = sqlite3.connect(db_path)
            try:
                for table in KSA_LABEL_AUTO_TRIAGE_TABLES:
                    conn.execute(f"CREATE TABLE {table}(id INTEGER)")
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_label_auto_triage(db_path, {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["schema"], "ksa_label_auto_triage_report_v1")
        self.assertEqual(result["error"]["code"], "schema_incomplete")
        self.assertIsInstance(result["error"]["detail"], str)
        self.assertFalse(result["status_update_allowed"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertFalse(result["human_reviewed_written_by_report"])
        self.assertFalse(result["safety"]["human_reviewed_written_by_report"])
        self.assertTrue(result["safety"]["auto_pass_candidate_is_not_human_approval"])

    def test_ksa_label_auto_triage_query_failure_uses_distinct_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "dashboard.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
            finally:
                conn.close()

            with patch(
                "ncs_dashboard.build_ksa_label_auto_triage_report",
                side_effect=sqlite3.DatabaseError("interrupted"),
            ):
                result = get_ksa_label_auto_triage(db_path, {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["schema"], "ksa_label_auto_triage_report_v1")
        self.assertEqual(result["error"]["code"], "read_query_failed")
        self.assertNotEqual(result["error"]["code"], "schema_incomplete")
        self.assertFalse(result["status_update_allowed"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertFalse(result["human_reviewed_written_by_report"])
        self.assertTrue(result["safety"]["auto_pass_candidate_is_not_human_approval"])

    def test_ksa_review_status_uses_pattern_report_safety_without_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            label_pattern_path = reports / "ksa_short_label_pattern_report_20260626.json"
            label_pattern_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_pattern_report_v1",
                        "candidate_count": 1,
                        "pattern_count": 1,
                        "emitted_pattern_count": 1,
                        "estimated_first_pass_review_unit_count": 1,
                        "row_to_first_pass_reduction_percent": 0.0,
                        "review_unit_model": "needs_review_rows_grouped_by_transform_pattern",
                        "top_patterns": [],
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        self.assertTrue(status["ok"])
        self.assertFalse(status["safety"]["status_update_allowed"])
        self.assertFalse(status["safety"]["db_writes"])
        self.assertFalse(status["safety"]["approval_claim"])

    def test_ksa_review_status_surfaces_unsafe_definition_packet_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            backlog_path = reports / "human_review_backlog_20260626.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_human_review_backlog_v1",
                        "seedpack_safety": {
                            "all_seedpacks_safe": False,
                            "total_review_items": 25,
                            "total_nonblank_decision_items": 0,
                            "total_trusted_status_proposals": 0,
                            "total_status_update_allowed_violations": 0,
                            "ksa_definition_review_operator_packet": {
                                "safety_ok": False,
                                "source_payload_exposed": True,
                                "status_update_allowed": False,
                                "db_writes": False,
                                "approval_claim": False,
                                "trusted_status_write_allowed": False,
                                "raw_source_mutation_allowed": False,
                                "review_pack_row_count": 25,
                                "decision_blank_count": 25,
                                "pending_decision_count": 25,
                                "completed_decision_count": 0,
                                "sidecar_safety": {
                                    "safety_ok": False,
                                    "consistency_issues": [
                                        "action_plan_action_count_mismatch"
                                    ],
                                },
                            },
                        },
                        "blockers": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        definition_packet = status["backlog"]["ksa_definition_review_operator_packet"]
        self.assertFalse(status["ok"])
        self.assertFalse(status["backlog"]["all_seedpacks_safe"])
        self.assertTrue(definition_packet["available"])
        self.assertFalse(definition_packet["safety_ok"])
        self.assertTrue(definition_packet["source_payload_exposed"])
        self.assertFalse(definition_packet["status_update_allowed"])
        self.assertFalse(definition_packet["db_writes"])
        self.assertFalse(definition_packet["approval_claim"])
        self.assertEqual(definition_packet["review_pack_row_count"], 25)
        self.assertEqual(definition_packet["decision_blank_count"], 25)
        self.assertFalse(definition_packet["sidecar_safety_ok"])
        self.assertEqual(
            definition_packet["sidecar_consistency_issues"],
            ["action_plan_action_count_mismatch"],
        )

    def test_ksa_review_status_surfaces_optional_llm_preprocessing_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_backlog_path = reports / "llm_preprocessing_backlog_map_20260629_8h.json"
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_backlog_map_v1",
                        "ok": True,
                        "status": "review_planning_only",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "blocker_count": 0,
                        "source_issues": [],
                        "review_status_policy": {
                            "human_decision_required_for_status_update": True,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                            "non_approval_statuses": [
                                "llm_reviewed",
                                "model_preprocessed",
                                "auto_linked",
                                "auto-pass-candidate",
                            ],
                        },
                        "summary": {
                            "raw_ksa_rows": 574279,
                            "label_candidate_rows": 475425,
                            "human_reviewed_label_rows": 755,
                            "pending_label_rows_not_trusted": 474670,
                            "distinct_normalized_label_keys": 373694,
                            "distinct_concepts_with_label_candidates": 413143,
                            "ontology_concepts": 533909,
                            "ontology_concepts_human_reviewed": 0,
                            "meaning_candidate_rows": 826286,
                            "task_ksa_concept_relation_rows": 14475815,
                            "training_goal_concept_link_rows": 348877,
                            "training_course_concept_link_rows": 479583,
                        },
                        "policy_snapshot": {
                            "auto_triage": {
                                "classification_v2_counts": {
                                    "human-sample-needed": 355154,
                                },
                            },
                            "sampling_plan": {
                                "recommended_sample_rows_total": 3705,
                                "estimated_click_reduction_ratio": 0.992195,
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            llm_work_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_work_plan_v1",
                        "ok": True,
                        "status": "ready_for_llm_preprocessing",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "source_backlog_map": str(llm_backlog_path),
                        "source_artifact_hash": "a" * 64,
                        "next_action": "run_report_only_track_artifacts",
                        "source_issues": [],
                        "safety_contract": {
                            "trusted_status_write_allowed": False,
                            "raw_source_mutation_allowed": False,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                        "artifact_policy": {
                            "db_apply_allowed": False,
                            "guarded_collection_allowed": False,
                            "operator_decision_fields_auto_filled": False,
                        },
                        "input_summary": {
                            "label_candidate_rows": 475425,
                            "recommended_sample_rows_total": 3705,
                            "estimated_click_reduction_ratio": 0.992195,
                        },
                        "work_tracks": [
                            {
                                "priority": "P0",
                                "track": "label_policy_triage_and_sampling",
                                "input_rows": 474670,
                                "human_gate": "operator sample decisions required",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        snapshot = status["llm_preprocessing_backlog"]
        work_plan = status["llm_preprocessing_work_plan"]
        self.assertTrue(status["ok"])
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["safety_ok"])
        self.assertFalse(snapshot["db_writes"])
        self.assertEqual(snapshot["label_candidate_rows"], 475425)
        self.assertEqual(snapshot["pending_label_rows_not_trusted"], 474670)
        self.assertEqual(snapshot["recommended_sample_rows_total"], 3705)
        self.assertIn("auto-pass-candidate", snapshot["non_approval_statuses"])
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_backlog"],
            str(llm_backlog_path),
        )
        self.assertTrue(work_plan["available"])
        self.assertTrue(work_plan["safety_ok"])
        self.assertFalse(work_plan["operator_decision_fields_auto_filled"])
        self.assertEqual(work_plan["next_action"], "run_report_only_track_artifacts")
        self.assertEqual(work_plan["track_count"], 1)
        self.assertEqual(
            work_plan["tracks"][0]["track"],
            "label_policy_triage_and_sampling",
        )
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_work_plan"],
            str(llm_work_plan_path),
        )

    def test_ksa_review_status_marks_unsafe_llm_preprocessing_backlog_unhealthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_backlog_path = reports / "llm_preprocessing_backlog_map_20260629_8h.json"
            llm_backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_backlog_map_v1",
                        "ok": False,
                        "status": "blocked_unsafe_source_artifact",
                        "report_only": True,
                        "status_update_allowed": True,
                        "db_writes": False,
                        "approval_claim": False,
                        "blocker_count": 1,
                        "source_issues": [
                            {
                                "severity": "blocker",
                                "code": "sidecar_safety_flag_not_false",
                                "field": "status_update_allowed",
                            }
                        ],
                        "review_status_policy": {
                            "human_decision_required_for_status_update": True,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                            "non_approval_statuses": ["llm_reviewed"],
                        },
                        "summary": {
                            "label_candidate_rows": 475425,
                            "pending_label_rows_not_trusted": 474670,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        snapshot = status["llm_preprocessing_backlog"]
        self.assertFalse(status["ok"])
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["safety_ok"])
        self.assertTrue(snapshot["status_update_allowed"])
        self.assertEqual(snapshot["source_issue_count"], 1)
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_backlog"],
            str(llm_backlog_path),
        )

    def test_ksa_review_status_marks_malformed_llm_artifacts_unhealthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_backlog_path = reports / "llm_preprocessing_backlog_map_20260629_8h.json"
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_backlog_path.write_bytes(b"\xff\xfe\xfa")
            llm_work_plan_path.write_text("{bad json", encoding="utf-8")

            status = get_ksa_preprocessing_review_status(reports)

        backlog = status["llm_preprocessing_backlog"]
        work_plan = status["llm_preprocessing_work_plan"]
        self.assertFalse(status["ok"])
        self.assertTrue(backlog["available"])
        self.assertFalse(backlog["safety_ok"])
        self.assertEqual(backlog["read_error"]["code"], "invalid_utf8_input")
        self.assertTrue(work_plan["available"])
        self.assertFalse(work_plan["safety_ok"])
        self.assertEqual(work_plan["read_error"]["code"], "malformed_json_input")
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_backlog"],
            str(llm_backlog_path),
        )
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_work_plan"],
            str(llm_work_plan_path),
        )

    def test_ksa_review_status_marks_blocked_llm_backlog_unhealthy_even_without_source_issues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_backlog_path = reports / "llm_preprocessing_backlog_map_20260629_8h.json"
            llm_backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_backlog_map_v1",
                        "ok": False,
                        "status": "blocked_unsafe_source_artifact",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "blocker_count": 1,
                        "source_issues": [],
                        "review_status_policy": {
                            "human_decision_required_for_status_update": True,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                            "non_approval_statuses": ["llm_reviewed"],
                        },
                        "summary": {
                            "label_candidate_rows": 475425,
                            "pending_label_rows_not_trusted": 474670,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        snapshot = status["llm_preprocessing_backlog"]
        self.assertFalse(status["ok"])
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["safety_ok"])
        self.assertFalse(snapshot["ok"])

    def test_ksa_review_status_marks_unsafe_llm_work_plan_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_work_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_work_plan_v1",
                        "ok": False,
                        "status": "blocked_unsafe_source_artifact",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "next_action": "fix_source_backlog_map",
                        "source_issues": [],
                        "safety_contract": {
                            "trusted_status_write_allowed": True,
                            "raw_source_mutation_allowed": False,
                        },
                        "artifact_policy": {
                            "db_apply_allowed": False,
                            "guarded_collection_allowed": False,
                        },
                        "input_summary": {"label_candidate_rows": 475425},
                        "work_tracks": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        work_plan = status["llm_preprocessing_work_plan"]
        self.assertFalse(status["ok"])
        self.assertTrue(work_plan["available"])
        self.assertFalse(work_plan["safety_ok"])
        self.assertTrue(work_plan["trusted_status_write_allowed"])
        self.assertEqual(
            status["source_paths"]["llm_preprocessing_work_plan"],
            str(llm_work_plan_path),
        )

    def test_ksa_review_status_marks_work_plan_without_forbidden_statuses_unhealthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_work_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_work_plan_v1",
                        "ok": True,
                        "status": "ready_for_llm_preprocessing",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "next_action": "run_report_only_track_artifacts",
                        "source_issues": [],
                        "safety_contract": {
                            "trusted_status_write_allowed": False,
                            "raw_source_mutation_allowed": False,
                        },
                        "artifact_policy": {
                            "db_apply_allowed": False,
                            "guarded_collection_allowed": False,
                        },
                        "input_summary": {"label_candidate_rows": 475425},
                        "work_tracks": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        work_plan = status["llm_preprocessing_work_plan"]
        self.assertFalse(status["ok"])
        self.assertTrue(work_plan["available"])
        self.assertFalse(work_plan["safety_ok"])
        self.assertEqual(work_plan["forbidden_automatic_statuses"], [])

    def test_ksa_review_status_accepts_utf8_sig_llm_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_backlog_path = reports / "llm_preprocessing_backlog_map_20260629_8h.json"
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_backlog_map_v1",
                        "ok": True,
                        "status": "review_planning_only",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "source_issues": [],
                        "review_status_policy": {
                            "human_decision_required_for_status_update": True,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )
            llm_work_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_work_plan_v1",
                        "ok": True,
                        "status": "ready_for_llm_preprocessing",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "source_issues": [],
                        "safety_contract": {
                            "trusted_status_write_allowed": False,
                            "raw_source_mutation_allowed": False,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                        "artifact_policy": {
                            "db_apply_allowed": False,
                            "guarded_collection_allowed": False,
                            "operator_decision_fields_auto_filled": False,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )

            status = get_ksa_preprocessing_review_status(reports)

        self.assertTrue(status["ok"])
        self.assertTrue(status["llm_preprocessing_backlog"]["safety_ok"])
        self.assertTrue(status["llm_preprocessing_work_plan"]["safety_ok"])

    def test_ksa_review_status_marks_operator_auto_filled_work_plan_unhealthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            llm_work_plan_path = reports / "llm_preprocessing_next_8h_work_plan_20260629_8h.json"
            llm_work_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_work_plan_v1",
                        "ok": True,
                        "status": "ready_for_llm_preprocessing",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required_for_approval": True,
                        "source_issues": [],
                        "safety_contract": {
                            "trusted_status_write_allowed": False,
                            "raw_source_mutation_allowed": False,
                            "forbidden_automatic_statuses": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                        "artifact_policy": {
                            "db_apply_allowed": False,
                            "guarded_collection_allowed": False,
                            "operator_decision_fields_auto_filled": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        work_plan = status["llm_preprocessing_work_plan"]
        self.assertFalse(status["ok"])
        self.assertFalse(work_plan["safety_ok"])
        self.assertTrue(work_plan["operator_decision_fields_auto_filled"])

    def test_ksa_review_status_marks_top_level_safety_flags_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            readiness_path = reports / "ksa_term_review_operator_packet_20260629_readiness.json"
            readiness_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "status_update_allowed": True,
                        "summary": {"concept_review_group_count": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        self.assertFalse(status["ok"])
        self.assertFalse(status["safety"]["safety_ok"])
        self.assertTrue(status["safety"]["status_update_allowed"])

    def test_ksa_review_status_prefers_all_scope_label_reports_over_major_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            canonical_pattern = reports / "ksa_short_label_pattern_report_20260626.json"
            major_pattern = reports / "ksa_short_label_pattern_report_major02_20260626.json"
            canonical_family = reports / "ksa_short_label_family_report_20260626.json"
            major_family = reports / "ksa_short_label_family_report_major02_20260626.json"
            canonical_pattern.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_pattern_report_v1",
                        "candidate_count": 3877,
                        "pattern_count": 15,
                        "emitted_pattern_count": 15,
                        "estimated_first_pass_review_unit_count": 15,
                        "row_to_first_pass_reduction_percent": 99.613,
                        "review_unit_model": "needs_review_rows_grouped_by_transform_pattern",
                        "top_patterns": [],
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            canonical_family.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_family_report_v1",
                        "candidate_count": 475425,
                        "label_family_count": 382556,
                        "emitted_first_pass_family_count": 200,
                        "estimated_first_pass_review_unit_count": 206,
                        "row_to_first_pass_reduction_percent": 99.957,
                        "top_families": [],
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            major_pattern.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_pattern_report_v1",
                        "candidate_count": 137,
                        "pattern_count": 9,
                        "emitted_pattern_count": 9,
                        "estimated_first_pass_review_unit_count": 9,
                        "row_to_first_pass_reduction_percent": 93.431,
                        "top_patterns": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            major_family.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_family_report_v1",
                        "candidate_count": 10683,
                        "label_family_count": 9961,
                        "estimated_first_pass_review_unit_count": 105,
                        "top_families": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.utime(major_pattern, None)
            os.utime(major_family, None)

            with patch.dict(
                os.environ,
                {
                    "NCS_KSA_SHORT_LABEL_PATTERN_JSON_PATH": "",
                    "NCS_KSA_SHORT_LABEL_FAMILY_JSON_PATH": "",
                },
            ):
                status = get_ksa_preprocessing_review_status(reports)

        self.assertEqual(status["source_paths"]["short_label_pattern"], str(canonical_pattern))
        self.assertEqual(status["source_paths"]["short_label_family"], str(canonical_family))
        self.assertEqual(status["label_pattern"]["candidate_count"], 3877)
        self.assertEqual(status["label_family"]["candidate_count"], 475425)

    def test_ksa_review_status_includes_definition_family_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            family_path = reports / "ksa_definition_candidate_family_report_20260626.json"
            label_family_path = reports / "ksa_short_label_family_report_20260626.json"
            label_pattern_path = reports / "ksa_short_label_pattern_report_20260626.json"
            family_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ncs_ksa_definition_candidate_family_report_v1",
                        "candidate_count": 413143,
                        "definition_family_count": 15,
                        "estimated_review_unit_count": 18,
                        "risk_candidate_count": 735,
                        "risk_flag_family_count": 15,
                        "row_to_estimated_review_unit_reduction_percent": 99.996,
                        "review_unit_model": "definition_family_plus_risk_samples",
                        "review_status_counts": {"candidate": 413143},
                        "risk_flag_counts": {"overlong_term": 305},
                        "top_families": [
                            {
                                "family_key": "knowledge_general_meaning",
                                "family_label": "의미·적용조건·판단기준 지식",
                                "concept_type": "knowledge",
                                "candidate_count": 129157,
                                "risk_count": 194,
                                "risk_flag_counts": {"overlong_term": 2},
                                "recommended_review_level": "sample_risk_rows",
                                "samples": [
                                    {
                                        "meaning_text": "전략적 인적자원관리: 전략적 인적자원관리의 목적, 범위, 실행 조건을 이해하여 과업 방향을 정하는 지식.",
                                    }
                                ],
                                "risk_samples": [
                                    {
                                        "meaning_text": "긴 후보: 긴 후보를 과업 상황에 맞게 실행하거나 적용하는 능력.",
                                        "risk_flags": ["overlong_term"],
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            label_family_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_family_report_v1",
                        "candidate_count": 475425,
                        "label_family_count": 395372,
                        "risk_label_family_count": 79260,
                        "emitted_first_pass_family_count": 200,
                        "estimated_first_pass_review_unit_count": 206,
                        "row_to_first_pass_reduction_percent": 99.957,
                        "review_unit_model": "top_risk_label_families_plus_quality_flag_buckets",
                        "review_status_counts": {"llm_reviewed": 396165, "needs_review": 79260},
                        "quality_flag_counts": {"generic_or_low_specificity": 12},
                        "risk_level_counts": {"critical_label_family_review": 1},
                        "top_families": [
                            {
                                "family_key": "skill:컴퓨터활용",
                                "representative_label": "컴퓨터 활용",
                                "concept_type": "skill",
                                "row_count": 239,
                                "concept_count": 60,
                                "scope_count": 177,
                                "risk_score": 96,
                                "risk_level": "critical_label_family_review",
                                "risk_reasons": ["same_label_maps_to_many_concepts"],
                                "review_status_counts": {"llm_reviewed": 239},
                                "quality_flag_counts": {},
                                "samples": [
                                    {
                                        "source_text": "컴퓨터 활용 능력",
                                    }
                                ],
                                "risk_samples": [
                                    {
                                        "source_text": "컴퓨터 활용 능력",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            label_pattern_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ksa_short_label_pattern_report_v1",
                        "candidate_count": 79260,
                        "pattern_count": 9,
                        "emitted_pattern_count": 9,
                        "estimated_first_pass_review_unit_count": 9,
                        "row_to_first_pass_reduction_percent": 99.989,
                        "review_unit_model": "needs_review_rows_grouped_by_transform_pattern",
                        "concept_type_counts": {"attitude": 43335},
                        "quality_flag_counts": {"changed_near_full_length": 75673},
                        "top_patterns": [
                            {
                                "pattern_key": "attitude:near_full_attitude_word_normalization",
                                "pattern_name": "near_full_attitude_word_normalization",
                                "concept_type": "attitude",
                                "row_count": 42892,
                                "concept_count": 30000,
                                "label_family_count": 28000,
                                "collision_label_family_count": 12,
                                "max_collision_concept_count": 4,
                                "collision_risk_hint": "label_family_collision_in_pattern",
                                "scope_count": 9000,
                                "risk_score": 75,
                                "risk_level": "high_pattern_review",
                                "recommended_handling": "Spot-check wording such as 의지/자세/노력 -> 태도.",
                                "automation_recommendation": "rule_tuning_candidate",
                                "minimum_review_unit": "pattern_samples_only",
                                "operator_decision_hint": "Usually residual wording cleanup; inspect samples before adding another rule.",
                                "decision_options": [
                                    "add_rule_and_regenerate",
                                    "accept_machine_screened_pattern",
                                    "keep_needs_review",
                                ],
                                "quality_flag_counts": {"changed_near_full_length": 42892},
                                "samples": [
                                    {
                                        "source_text": "사업을 성공적으로 완료시키려는 의지",
                                        "label_text": "사업을 성공적으로 완료시키려는 태도",
                                        "method_details": "compact_attitude_action_phrase",
                                        "short_label_removed_char_count": 2,
                                        "short_label_length_ratio": 0.91,
                                        "collision_risk": "pattern_label_collision",
                                        "label_family_pattern_concept_count": 4,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = get_ksa_preprocessing_review_status(reports)

        self.assertTrue(status["ok"])
        self.assertEqual(
            status["source_paths"]["definition_candidate_family"],
            str(family_path),
        )
        self.assertEqual(status["source_paths"]["short_label_family"], str(label_family_path))
        self.assertEqual(status["source_paths"]["short_label_pattern"], str(label_pattern_path))
        self.assertEqual(status["definition_family"]["candidate_count"], 413143)
        self.assertEqual(status["definition_family"]["definition_family_count"], 15)
        self.assertEqual(status["definition_family"]["estimated_review_unit_count"], 18)
        self.assertEqual(
            status["definition_family"]["row_to_estimated_review_unit_reduction_percent"],
            99.996,
        )
        self.assertEqual(status["definition_family"]["top_families"][0]["candidate_count"], 129157)
        self.assertEqual(
            status["definition_family"]["top_families"][0]["risk_sample_flags"],
            ["overlong_term"],
        )
        self.assertEqual(status["label_family"]["candidate_count"], 475425)
        self.assertEqual(status["label_family"]["estimated_first_pass_review_unit_count"], 206)
        self.assertEqual(status["label_family"]["top_families"][0]["representative_label"], "컴퓨터 활용")
        self.assertEqual(status["label_family"]["top_families"][0]["risk_score"], 96)
        self.assertEqual(status["label_pattern"]["candidate_count"], 79260)
        self.assertEqual(status["label_pattern"]["estimated_first_pass_review_unit_count"], 9)
        self.assertEqual(status["label_pattern"]["top_patterns"][0]["collision_label_family_count"], 12)
        self.assertEqual(
            status["label_pattern"]["top_patterns"][0]["sample_method_details"],
            "compact_attitude_action_phrase",
        )
        self.assertEqual(status["label_pattern"]["top_patterns"][0]["sample_length_ratio"], 0.91)
        self.assertEqual(
            status["label_pattern"]["top_patterns"][0]["sample_collision_risk"],
            "pattern_label_collision",
        )
        self.assertEqual(
            status["label_pattern"]["top_patterns"][0]["pattern_name"],
            "near_full_attitude_word_normalization",
        )
        self.assertEqual(status["label_pattern"]["top_patterns"][0]["sample_label_text"], "사업을 성공적으로 완료시키려는 태도")
        self.assertTrue(
            any(
                action["title"] == "정의 문장 후보는 개별 클릭이 아니라 정의 패밀리 단위로 확인"
                for action in status["ontology_next_actions"]
            )
        )
        self.assertTrue(
            any(
                action["title"] == "단어형 라벨 후보는 행 클릭이 아니라 라벨 패밀리 first-pass 큐로 확인"
                for action in status["ontology_next_actions"]
            )
        )
        self.assertTrue(
            any(
                action["title"] == "needs_review 라벨은 행이 아니라 변환 패턴 단위로 확인"
                for action in status["ontology_next_actions"]
            )
        )

    def test_dashboard_status_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = get_status(self._empty_dashboard_db(tmp))
        self.assertIn("counts", status)
        self.assertIn("element_progress", status)
        self.assertIn("sqf", status)
        self.assertIn("ontology", status)
        self.assertIn("open_by_severity", status["quality"])
        self.assertIn("info_issues", status["quality"])
        self.assertIn("actionable_issues", status["quality"])
        self.assertIn("human_review_required_issues", status["quality"])
        self.assertIn("api_issues", status["quality"])
        self.assertGreaterEqual(status["counts"]["competency_units"], 0)

    def test_dashboard_status_quality_split_counts_seeded_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_dashboard_db(tmp)
            conn = connect(db_path)
            try:
                insert_quality_issue(
                    conn,
                    target_type="ksa",
                    target_id="1",
                    issue_type="duplicate_text",
                    severity="info",
                    issue_detail="duplicate source text",
                )
                insert_quality_issue(
                    conn,
                    target_type="element",
                    target_id="2",
                    issue_type="api_element_unmatched",
                    severity="warning",
                    issue_detail="API element was not matched",
                )
                insert_quality_issue(
                    conn,
                    target_type="concept",
                    target_id="3",
                    issue_type="ontology_core_concept_human_review_required",
                    severity="high",
                    issue_detail="concept needs human review",
                )
                insert_quality_issue(
                    conn,
                    target_type="goal_link",
                    target_id="4",
                    issue_type="hr_training_goal_link_human_review_required",
                    severity="medium",
                    issue_detail="goal link needs human review",
                )
                insert_quality_issue(
                    conn,
                    target_type="unit",
                    target_id="5",
                    issue_type="api_value_mismatch",
                    severity="warning",
                    issue_detail="resolved API mismatch",
                )
                conn.execute(
                    "UPDATE quality_issues SET resolved_at = ? WHERE issue_type = 'api_value_mismatch'",
                    (now_utc(),),
                )
                conn.commit()
            finally:
                conn.close()

            status = get_status(db_path)

        self.assertEqual(status["quality"]["open_issues"], 4)
        self.assertEqual(status["quality"]["resolved_issues"], 1)
        self.assertEqual(status["quality"]["info_issues"], 1)
        self.assertEqual(status["quality"]["actionable_issues"], 3)
        self.assertEqual(status["quality"]["human_review_required_issues"], 2)
        self.assertEqual(status["quality"]["api_issues"], 1)
        self.assertEqual(
            status["quality"]["open_by_severity"],
            {"high": 1, "info": 1, "medium": 1, "warning": 1},
        )
        self.assertEqual(
            status["issue_types"],
            [
                "api_element_unmatched",
                "duplicate_text",
                "hr_training_goal_link_human_review_required",
                "ontology_core_concept_human_review_required",
            ],
        )

    def test_dashboard_api_status_route_returns_quality_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_dashboard_db(tmp)
            conn = connect(db_path)
            try:
                insert_quality_issue(
                    conn,
                    target_type="element",
                    target_id="1",
                    issue_type="api_element_unmatched",
                    severity="warning",
                    issue_detail="API element was not matched",
                )
                insert_quality_issue(
                    conn,
                    target_type="ksa",
                    target_id="2",
                    issue_type="short_ksa",
                    severity="info",
                    issue_detail="short KSA text",
                )
                conn.commit()
            finally:
                conn.close()

            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = db_path
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urlopen(base_url + "/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(payload["quality"]["open_issues"], 2)
        self.assertEqual(payload["quality"]["resolved_issues"], 0)
        self.assertEqual(payload["quality"]["info_issues"], 1)
        self.assertEqual(payload["quality"]["actionable_issues"], 1)
        self.assertEqual(payload["quality"]["api_issues"], 1)

    def test_quality_workbench_scope_includes_ontology_relation_and_goal_link_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._empty_dashboard_db(tmp)
            conn = connect(db_path)
            try:
                timestamp = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'Planning')
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
                    (classification_id, timestamp, timestamp),
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
                    INSERT INTO performance_criteria(
                        element_id, criteria_no, criteria_text_raw
                    ) VALUES (?, '1', 'Design workforce interview criteria.')
                    """,
                    (element_id,),
                )
                criteria_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', '핵심역량의 개념')
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
                    ) VALUES ('핵심역량의 개념', '핵심역량의개념', 'knowledge',
                              'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                    (ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO criteria_concept_links(
                        criteria_id, concept_id, relation_type, link_status, created_at
                    ) VALUES (?, ?, 'related', 'candidate', ?)
                    """,
                    (criteria_id, concept_id, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                        normalized_key, split_method, review_status, created_at
                    ) VALUES (?, ?, 'knowledge', 1, 'core competency concept',
                              'corecompetencyconcept', 'single', 'raw', ?)
                    """,
                    (ksa_id, element_id, timestamp),
                )
                atomic_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, confidence_score, review_status, created_at
                    ) VALUES (?, ?, ?, 'co_required_in_element',
                              ?, ?, ?, 'scope evidence', 0.7, 'candidate', ?)
                    """,
                    (
                        criteria_id,
                        element_id,
                        concept_id,
                        concept_id,
                        atomic_id,
                        atomic_id,
                        timestamp,
                    ),
                )
                relation_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ncs_training_courses(
                        training_course_id, ncs_cl_cd, compe_unit_name,
                        ncs_lclas_cd, ncs_lclas_cdnm, train_goal, api_fetched_at
                    ) VALUES (1, '0202020101', 'HR planning', '02', 'Business',
                              'goal evidence', ?)
                    """,
                    (timestamp,),
                )
                cur = conn.execute(
                    """
                    INSERT INTO training_goal_concept_links(
                        training_course_id, unit_code, element_id, concept_id,
                        link_method, confidence_score, evidence_text, review_status,
                        created_at, updated_at
                    ) VALUES (1, '0202020101_26v1', ?, ?, 'training_goal_concept_text',
                              0.8, 'goal evidence', 'auto_linked', ?, ?)
                    """,
                    (element_id, concept_id, timestamp, timestamp),
                )
                goal_link_id = cur.lastrowid
                insert_quality_issue(
                    conn,
                    target_type="ontology_concept",
                    target_id=concept_id,
                    issue_type="ontology_core_concept_human_review_required",
                    severity="high",
                    issue_detail="concept requires review",
                )
                insert_quality_issue(
                    conn,
                    target_type="task_ksa_concept_relation",
                    target_id=relation_id,
                    issue_type="ontology_task_ksa_relation_human_review_required",
                    severity="high",
                    issue_detail="relation requires review",
                )
                insert_quality_issue(
                    conn,
                    target_type="training_goal_concept_link",
                    target_id=goal_link_id,
                    issue_type="ontology_training_goal_link_human_review_required",
                    severity="high",
                    issue_detail="goal link requires review",
                )
                conn.commit()
            finally:
                conn.close()

            workbench = get_workbench(
                db_path,
                {"kind": ["quality"], "major_code": ["02"], "limit": ["10"]},
            )
            items = get_items(
                db_path,
                {"kind": ["quality"], "major_code": ["02"], "limit": ["10"]},
            )

        quality_card = next(card for card in workbench["cards"] if card["kind"] == "quality")
        self.assertEqual(quality_card["count"], 3)
        self.assertEqual(items["total"], 3)
        self.assertEqual(
            {item["context"] for item in items["items"]},
            {
                "ontology_core_concept_human_review_required",
                "ontology_task_ksa_relation_human_review_required",
                "ontology_training_goal_link_human_review_required",
            },
        )

    def test_dashboard_issues_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = get_issues(self._empty_dashboard_db(tmp), {"limit": ["5"]})
        self.assertIn("issues", result)
        self.assertIsInstance(result["issues"], list)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_status_does_not_modify_generated_db(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        before_mtime_ns = db_path.stat().st_mtime_ns

        get_status(db_path)

        self.assertEqual(db_path.stat().st_mtime_ns, before_mtime_ns)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_issues_do_not_modify_generated_db(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        before_mtime_ns = db_path.stat().st_mtime_ns

        get_issues(db_path, {"limit": ["5"]})

        self.assertEqual(db_path.stat().st_mtime_ns, before_mtime_ns)

    def test_dashboard_read_endpoints_do_not_create_missing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.db"

            status = get_status(db_path)
            issues = get_issues(db_path, {"limit": ["5"]})
            definitions = get_ksa_definitions(db_path, {"limit": ["5"]})

            self.assertFalse(status["ok"])
            self.assertEqual(status["error"]["code"], "database_missing")
            self.assertIn("counts", status)
            self.assertEqual(status["quality"]["open_issues"], 0)
            self.assertEqual(status["quality"]["resolved_issues"], 0)
            self.assertEqual(status["quality"]["actionable_issues"], 0)
            self.assertEqual(status["element_progress"]["percent"], 0)
            self.assertFalse(issues["ok"])
            self.assertEqual(issues["error"]["code"], "database_missing")
            self.assertFalse(definitions["ok"])
            self.assertEqual(definitions["error"]["code"], "database_missing")
            self.assertFalse(db_path.exists())
            self.assertFalse(Path(str(db_path) + "-wal").exists())
            self.assertFalse(Path(str(db_path) + "-shm").exists())

    def test_dashboard_read_endpoints_do_not_initialize_partial_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "partial.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE raw_excel_rows (row_id INTEGER PRIMARY KEY)")
                conn.commit()
                before_tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                ).fetchone()[0]
            finally:
                conn.close()

            status = get_status(db_path)
            issues = get_issues(db_path, {"limit": ["5"]})
            definitions = get_ksa_definitions(db_path, {"limit": ["5"]})

            conn = sqlite3.connect(db_path)
            try:
                after_tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"]["code"], "schema_incomplete")
            self.assertIn("counts", status)
            self.assertEqual(status["quality"]["open_issues"], 0)
            self.assertEqual(status["quality"]["resolved_issues"], 0)
            self.assertEqual(status["quality"]["actionable_issues"], 0)
            self.assertEqual(status["element_progress"]["percent"], 0)
            self.assertFalse(issues["ok"])
            self.assertEqual(issues["error"]["code"], "schema_incomplete")
            self.assertFalse(definitions["ok"])
            self.assertEqual(definitions["error"]["code"], "schema_incomplete")
            self.assertEqual(after_tables, before_tables)
            self.assertFalse(Path(str(db_path) + "-wal").exists())
            self.assertFalse(Path(str(db_path) + "-shm").exists())

    def test_dashboard_readonly_connection_uses_ro_without_immutable_cache_hint(self) -> None:
        fake_conn = MagicMock()
        with patch("ncs_dashboard.sqlite3.connect", return_value=fake_conn) as connect_mock:
            conn = connect_db_readonly(Path("ncs.db"))

        self.assertIs(conn, fake_conn)
        uri = connect_mock.call_args.args[0]
        self.assertIn("?mode=ro", uri)
        self.assertNotIn("immutable=1", uri)
        self.assertTrue(connect_mock.call_args.kwargs["uri"])
        self.assertEqual(connect_mock.call_args.kwargs["timeout"], 30)
        self.assertIs(fake_conn.row_factory, sqlite3.Row)

    def test_dashboard_html_has_lookup_and_large_editor(self) -> None:
        self.assertIn("NCS-SQF 온톨로지 워크벤치", HTML)
        self.assertIn("온톨로지 준비 전처리 단계", HTML)
        self.assertIn("경영지원 MVP", HTML)
        self.assertIn("setManagementSupportMvp()", HTML)
        self.assertIn("/api/preprocess", HTML)
        self.assertIn("manualSourcePacket", HTML)
        self.assertIn("manualSourceHash", HTML)
        self.assertIn("source_artifact_hash: q('manualSourceHash').value", HTML)
        self.assertIn("min-height:210px", HTML)
        self.assertIn("min-width:420px", HTML)
        self.assertIn('id="majorCode" class="code" value=""', HTML)
        self.assertIn("setHrScope()", HTML)
        self.assertIn("scheduleAutoRefresh", HTML)
        self.assertIn("actionable", HTML)
        self.assertIn("자동갱신 30초", HTML)
        self.assertIn("/aihr-live", HTML)
        self.assertIn("/aihr-training-system-builder", HTML)
        self.assertIn("Training Builder", HTML)
        self.assertIn("AI-HR Live", HTML)
        self.assertIn("/aihr-plan-demo", HTML)
        self.assertIn("AI-HR 데모", HTML)
        self.assertIn("/aihr-readiness", HTML)
        self.assertIn("AI-HR 준비도", HTML)
        self.assertIn("/aihr-review-board", HTML)
        self.assertIn("AI-HR 검토보드", HTML)
        self.assertIn("/aihr-provenance-reconfirmation", HTML)
        self.assertIn("Provenance Reconfirm", HTML)
        self.assertIn("/aihr-query-router", HTML)
        self.assertIn("쿼리 라우터", HTML)
        self.assertIn("/aihr-agent-queue", HTML)
        self.assertIn("Agent Queue", HTML)
        self.assertIn("/aihr-agent-queue-status", HTML)
        self.assertIn("Queue Status", HTML)
        self.assertIn("/aihr-agent-queue-run", HTML)
        self.assertIn("Queue Run", HTML)
        self.assertIn("/ksa-review-dashboard", HTML)
        self.assertIn("KSA Review", HTML)
        self.assertIn("/ksa-preprocessing-dashboard", HTML)
        self.assertIn("KSA Preprocessing", HTML)

    def test_ksa_definition_dashboard_html_has_readonly_api_surface(self) -> None:
        html = render_ksa_definition_dashboard_html()
        self.assertNotIn("\ufffd", html)
        self.assertIn("KSA Definition Dashboard", html)
        self.assertIn("/api/ksa-definitions", html)
        self.assertNotIn("${esc(item.concept_type)} #${esc(item.concept_id)}", html)
        self.assertNotIn("#${esc(item.concept_id)}", html)
        self.assertNotIn("renderCountMap('concept_type'", html)
        self.assertNotIn("countMap('concept_type'", html)
        self.assertIn("분류별 KSA 리뷰 탐색", html)
        self.assertIn("reviewScopeProgress", html)
        self.assertIn("loadKsaTaxonomy", html)
        self.assertIn("selectKsaMajor", html)
        self.assertIn("selectKsaSub", html)
        self.assertIn("ensureReviewInputs", html)
        self.assertIn("window.prompt('검토자 ID를 입력하세요.", html)
        self.assertIn("reviewSourcePacket", html)
        self.assertIn("reviewSourceHash", html)
        self.assertIn("source_artifact_hash", html)
        self.assertNotIn("reviewDecisionPacket", html)
        self.assertNotIn("reports/ksa_definition_dashboard_review_packet_", html)
        self.assertNotIn("ksa_definition_dashboard_review_v1:", html)
        self.assertIn("window.confirm(kind === 'meaning'", html)
        self.assertIn("사람확인", html)
        self.assertIn("수정필요", html)
        self.assertIn("거절", html)
        self.assertIn("/api/ksa-label-review", html)
        self.assertIn("/api/ksa-meaning-review", html)
        self.assertIn("/ksa-preprocessing-dashboard", html)
        self.assertNotIn('<section class="pipeline" id="pipeline"></section>', html)
        self.assertNotIn('<section class="summary" id="summary"></section>', html)
        review_html = render_ksa_review_dashboard_html()
        self.assertIn("KSA Review Dashboard", review_html)
        self.assertIn("/api/ksa-definitions", review_html)
        self.assertIn("/api/ksa-label-review", review_html)
        self.assertIn("/api/ksa-label-edit", review_html)
        self.assertNotIn("/api/ksa-meaning-review", review_html)
        self.assertNotIn("/api/ksa-review-status", review_html)
        self.assertNotIn("정의 문장 후보", review_html)
        self.assertNotIn("definitionCandidateNotice", review_html)
        self.assertNotIn("meaning_review_progress", review_html)
        self.assertNotIn("reviewMeaningCandidate", review_html)
        self.assertNotIn("setDefinitionPendingOnly", review_html)
        self.assertIn("label_review_scope_progress", review_html)
        self.assertIn("label_review_progress", review_html)
        self.assertIn("단어형 KSA 라벨 후보만 검토", review_html)
        self.assertIn("<th style=\"width:320px;\">단어형 KSA</th>", review_html)
        self.assertIn("<th style=\"width:620px;\">원문 KSA</th>", review_html)
        self.assertIn("사람확인 버튼", review_html)
        self.assertIn("majorIcons", review_html)
        self.assertIn("setLabelPendingOnly", review_html)
        self.assertIn("setNeedsReviewOnly", review_html)
        self.assertNotIn('data-decision="needs_revision"', review_html)
        self.assertNotIn("사람 수정필요", review_html)
        self.assertIn("if (!hasExplicitScope) setScope('02', '02', '02', '01');", review_html)
        self.assertIn("refreshReviewDashboard", review_html)
        self.assertIn("oneClickPayload", review_html)
        self.assertNotIn("oneClickPacket", review_html)
        self.assertNotIn("sha256Hex", review_html)
        self.assertIn("reviewSourcePacket", review_html)
        self.assertIn("reviewSourceHash", review_html)
        self.assertIn("reviewNote", review_html)
        self.assertIn("source_decision_packet", review_html)
        self.assertIn("source_artifact_hash", review_html)
        self.assertNotIn("reports/ksa_review_dashboard_decision_packet_", review_html)
        self.assertNotIn("ksa_review_dashboard_one_click_v1:", review_html)
        self.assertIn("raw_to_label_checked = true", review_html)
        self.assertIn('class="label-edit-box"', review_html)
        self.assertIn("data-edit-label-id", review_html)
        self.assertIn("editShortLabel", review_html)
        self.assertNotIn("raw_to_meaning_checked", review_html)
        self.assertNotIn("API 수집", review_html)
        return
        self.assertIn("KSA Review Dashboard", review_html)
        self.assertIn("/api/ksa-definitions", review_html)
        self.assertIn("/api/ksa-label-review", review_html)
        self.assertIn("/api/ksa-meaning-review", review_html)
        self.assertIn("LLM 검토를 통과한 단어형 KSA 라벨 후보", review_html)
        self.assertNotIn("/api/ksa-review-status", review_html)
        self.assertNotIn("우선 검토 개념", review_html)
        self.assertNotIn("최소 리뷰 큐", review_html)
        self.assertNotIn("focusReviewConcept", review_html)
        self.assertNotIn("목록 보기", review_html)
        self.assertNotIn("유형", review_html)
        self.assertNotIn("검토 방향", review_html)
        self.assertNotIn("검토 방식", review_html)
        self.assertNotIn("우선도", review_html)
        self.assertNotIn("위험 점수", review_html)
        self.assertNotIn("priority", review_html)
        self.assertNotIn("훈련 연결", review_html)
        self.assertNotIn("훈련", review_html)
        self.assertNotIn("training_course", review_html)
        self.assertNotIn("course_link", review_html)
        self.assertIn("majorIcons", review_html)
        self.assertIn('<option value="all" selected>전체</option>', review_html)
        self.assertIn('<option value="llm_reviewed" selected>LLM 단어형 검토됨</option>', review_html)
        self.assertIn('<option value="candidate">정의 문장 후보</option>', review_html)
        self.assertIn("setDefinitionPendingOnly", review_html)
        self.assertIn("setLabelPendingOnly", review_html)
        self.assertIn("if (!hasExplicitScope) setScope('02', '02', '02', '01');", review_html)
        self.assertIn("refreshReviewDashboard", review_html)
        self.assertIn("oneClickPayload", review_html)
        self.assertNotIn("oneClickPacket", review_html)
        self.assertNotIn("sha256Hex", review_html)
        self.assertIn("source_decision_packet", review_html)
        self.assertIn("source_artifact_hash", review_html)
        self.assertNotIn("local_operator", review_html)
        self.assertIn("정의 리뷰율", review_html)
        self.assertNotIn("운영 리뷰 현황", review_html)
        self.assertNotIn("라벨 first-pass", review_html)
        self.assertNotIn("라벨 축소율", review_html)
        self.assertNotIn("needs_review 패턴", review_html)
        self.assertNotIn("패턴 축소율", review_html)
        self.assertNotIn("개별 클릭 대신 위험 라벨 패밀리 확인", review_html)
        self.assertNotIn("행별 검토 대신 변환 패턴 확인", review_html)
        self.assertNotIn("labelFamilyQueue", review_html)
        self.assertNotIn("라벨 패밀리 first-pass 리뷰 단위", review_html)
        self.assertNotIn("labelPatternQueue", review_html)
        self.assertNotIn("needs_review 변환 패턴 first-pass 리뷰 단위", review_html)
        self.assertNotIn("정의 리뷰 단위", review_html)
        self.assertNotIn("정의 축소율", review_html)
        self.assertNotIn("개별 클릭 대신 패밀리/위험샘플 확인", review_html)
        self.assertNotIn("definitionFamilyQueue", review_html)
        self.assertIn("자동 추출 결과(미승인)", review_html)
        self.assertNotIn("정의 패밀리", review_html)
        self.assertNotIn("패밀리 대표 + 위험샘플만 확인", review_html)
        self.assertIn("meaning_review_progress", review_html)
        self.assertIn("labelCandidateNotice", review_html)
        self.assertIn("단어형 KSA 라벨 후보", review_html)
        self.assertIn("LLM 단어형 검토는 후보가 자동 점검을 통과했다는 뜻입니다.", review_html)
        self.assertNotIn("label_id ${esc", review_html)
        self.assertNotIn("meaning_id ${esc", review_html)
        self.assertNotIn("confidence", review_html)
        self.assertNotIn("source text:", review_html)
        self.assertIn("LLM 단어형 검토됨", review_html)
        self.assertIn("LLM 검토를 통과한 단어형 라벨 자동 추출 결과입니다", review_html)
        self.assertIn("라벨 사람확인", review_html)
        self.assertIn("라벨 수정필요", review_html)
        self.assertIn("정의 사람확인", review_html)
        self.assertIn("사람확인 버튼", review_html)
        self.assertIn("정의 문장 후보", review_html)
        self.assertIn("KSA 정의 문장 후보", review_html)
        self.assertNotIn("term_definition_template", review_html)
        self.assertNotIn("전처리 근거 보기", review_html)
        self.assertNotIn("과업 맥락 후보", review_html)
        self.assertIn("definitionCandidateNotice", review_html)
        self.assertIn("단어형 LLM review 상태는 왼쪽 라벨 후보 상태에서 확인합니다.", review_html)
        self.assertIn("이미 확인한 항목은 정의 리뷰 필터를 전체 또는 사람확인으로 바꾸면", review_html)
        self.assertIn("<th style=\"width:300px;\">단어형 KSA</th>", review_html)
        self.assertIn("<th style=\"width:340px;\">원문 KSA</th>", review_html)
        self.assertIn("<summary>KSA 정의 문장 후보 보기</summary>", review_html)
        self.assertNotIn("llm_reviewed는", review_html)
        self.assertNotIn("품질검토", review_html)
        self.assertNotIn("quality review", review_html)
        self.assertNotIn("범용어", review_html)
        self.assertNotIn("medium", review_html)
        self.assertNotIn("definition 없음", review_html)
        self.assertNotIn('id="reviewerId" type="hidden" value="dashboard_click"', review_html)
        self.assertIn("raw_to_label_checked = true", review_html)
        self.assertIn("raw_to_meaning_checked = true", review_html)
        self.assertNotIn("API 수집", review_html)
        self.assertNotIn("API 수집률", review_html)
        self.assertNotIn("요소 API", review_html)
        self.assertNotIn("Main Dashboard", review_html)
        self.assertNotIn("API 100.0%", review_html)
        self.assertNotIn("element_percent", review_html)
        preprocessing_html = render_ksa_preprocessing_dashboard_html()
        self.assertIn("KSA 전처리 현황", preprocessing_html)
        self.assertIn("휴먼 리뷰 화면과 분리", preprocessing_html)
        self.assertNotIn("countMap('concept_type'", preprocessing_html)
        self.assertIn("loadPreprocessing", preprocessing_html)
        self.assertIn("majorIcons", preprocessing_html)
        self.assertIn("preprocessMajorTiles", preprocessing_html)
        self.assertIn("selectPreprocessMajor", preprocessing_html)
        self.assertIn("stage-icon", preprocessing_html)
        self.assertIn("핵심 진행률", preprocessing_html)
        self.assertIn("상세 카운트 보기", preprocessing_html)
        self.assertIn("/ksa-definitions", preprocessing_html)
        self.assertIn("llmBacklogPanel", preprocessing_html)
        self.assertIn("llmBacklogCards", preprocessing_html)
        self.assertIn("/api/ksa-review-status", preprocessing_html)
        self.assertIn("Non-approval statuses", preprocessing_html)
        self.assertIn("Work plan", preprocessing_html)
        self.assertIn("next_action", preprocessing_html)
        return
        self.assertIn("Where the word-style preprocessing appears", html)
        self.assertIn("Row evidence map", html)
        self.assertIn("1 Raw KSA source", html)
        self.assertIn("2 Atomic KSA candidate", html)
        self.assertIn("3 Representative concept name", html)
        self.assertIn("4 Short label candidate", html)
        self.assertIn("5 Term definition candidate", html)
        self.assertIn("6 Task evidence links", html)
        self.assertIn("Row evidence summary", html)
        self.assertIn("<b>Raw</b>", html)
        self.assertIn("<b>Atomic</b>", html)
        self.assertIn("<b>Concept</b>", html)
        self.assertIn("<b>Short Label</b>", html)
        self.assertIn("Ontology Build Chain", html)
        self.assertIn("Atomic KSA", html)
        self.assertIn("Short Label Candidate", html)
        self.assertIn("raw KSA, atomic KSA, short label, concept", html)
        self.assertIn(
            "Raw KSA -&gt; Atomic KSA -&gt; Representative Concept -&gt; Short Label Candidate",
            html,
        )
        self.assertIn("quality review", html)
        self.assertIn("단어형 대표 라벨 후보", html)
        self.assertIn("review-only compact label", html)
        self.assertIn("Label State", html)
        self.assertIn("단어형 라벨 상태", html)
        self.assertIn("Shortened candidate", html)
        self.assertIn("단어형 압축됨", html)
        self.assertIn("Collision review", html)
        self.assertIn("Quality review", html)
        self.assertIn("step 4 단어형 대표 라벨 후보", html)
        self.assertIn("generic_or_low_specificity", html)
        self.assertIn("short_acronym_needs_context", html)
        self.assertIn("very_low_label_source_ratio", html)
        self.assertIn("changed_near_full_length", html)
        self.assertIn("unbalanced_parentheses", html)
        self.assertIn("label_quality_flags", html)
        self.assertIn("labelActioned", html)
        self.assertIn("labelAutomatedActioned", html)
        self.assertIn("labelHumanReviewCoverage", html)
        self.assertIn("automated triage actioned labels", html)
        self.assertIn("label triage progress", html)
        self.assertIn("human review progress", html)
        self.assertIn("llm reviewed meaning concepts", html)
        self.assertIn("needs review meaning concepts", html)
        self.assertIn("LLM/rule preprocessed meaning candidates", html)
        self.assertIn("Meaning Review / definition candidate state", html)
        self.assertIn("meaningReviewStatus", html)
        self.assertIn("meaning_review_status", html)
        self.assertIn("applyInitialQueryParams", html)
        self.assertIn("new URLSearchParams(window.location.search)", html)
        self.assertIn("['majorCode', 'major_code']", html)
        self.assertIn("['labelReviewStatus', 'label_review_status']", html)
        self.assertIn("['meaningReviewStatus', 'meaning_review_status']", html)
        self.assertLess(
            html.rindex("applyInitialQueryParams();"),
            html.rindex("loadDefinitions();"),
        )
        self.assertLess(
            html.index("stage('3. Representative Concept'"),
            html.index("stage('4. Short Label Candidate"),
        )
        self.assertIn("Representative Concept", html)
        self.assertIn("Term Definition Candidate", html)
        self.assertIn("Criteria/Task Evidence Links", html)
        self.assertIn("Ontology Payload", html)
        self.assertIn("ontology_concept_label_candidates.label_text", html)
        self.assertIn("ksa_items.ksa_text_raw", html)
        self.assertIn("short label candidate concepts / 단어형 라벨 후보", html)
        self.assertIn("shortened label candidates / 단어형 압축 후보", html)
        self.assertIn("missing label candidates / 후보 없음", html)
        self.assertIn("quality review label candidates / 품질 검토 필요", html)
        self.assertIn("source-missing label anomalies / 출처 없음", html)
        self.assertIn("candidate only; not applied to concept_name", html)
        self.assertIn("human-confirmed label anomalies / human approval should be 0", html)
        self.assertIn("llm reviewed label candidates / review context only", html)
        self.assertIn("llm-or-human reviewed label candidates / not approval", html)
        self.assertIn("ksa_atomic_items.atom_text", html)
        self.assertIn("ontology_concepts.definition", html)
        self.assertIn(
            "ksa_meaning_candidates.meaning_text where source_method='term_definition_template'",
            html,
        )
        self.assertIn("term_definition_candidate", html)
        self.assertIn("review-only label candidate table", html)
        self.assertIn("term definition candidate source", html)
        self.assertIn("function hasScopedMeaningEvidence", html)
        self.assertIn("Scoped evidence warning", html)
        self.assertIn("버튼은 막지 않고 사람 판단으로 처리합니다.", html)
        self.assertIn("Label review", html)
        self.assertIn("Concept review", html)
        self.assertIn("Definition status", html)
        self.assertIn("status_update_allowed", html)
        self.assertIn("/api/ksa-label-review", html)
        self.assertIn("/api/ksa-meaning-review", html)
        self.assertIn("Human-review surface", html)
        self.assertIn("Manual review packs", html)
        self.assertIn("/ksa-label-needs-review-seedpack", html)
        self.assertIn("/ksa-meaning-needs-review-seedpack", html)
        self.assertIn("/ksa-meaning-missing-scoped-seedpack", html)
        self.assertIn("Meaning missing scoped pack", html)
        self.assertIn("automated judge refused to promote", html)
        self.assertIn("Reviewer ID", html)
        self.assertIn("Review Note", html)
        self.assertIn("Review Check", html)
        self.assertIn("labelRawToLabelChecked", html)
        self.assertIn("Meaning Check", html)
        self.assertIn("meaningRawToMeaningChecked", html)
        self.assertIn(
            "Raw KSA -&gt; Atomic KSA -&gt; Representative Concept -&gt; Short Label checked",
            html,
        )
        self.assertIn(
            "Raw KSA -&gt; Term Definition Candidate -&gt; Criteria/Task Evidence checked",
            html,
        )
        self.assertIn("사람확인", html)
        self.assertIn("Pending queue", html)
        self.assertIn("Quality queue", html)
        self.assertIn("Missing queue", html)
        self.assertIn("setMissingLabelQueue", html)
        missing_queue_js = html[
            html.index("function setMissingLabelQueue()"):
            html.index("function setNeedsRevisionQueue()")
        ]
        self.assertIn("ensureReviewQueueScope();", missing_queue_js)
        self.assertIn("q('labelState').value = 'missing';", missing_queue_js)
        self.assertIn("q('labelReviewStatus').value = 'all';", missing_queue_js)
        self.assertIn("q('limit').value = '100';", missing_queue_js)
        self.assertIn("Needs revision queue", html)
        self.assertIn("Meaning needs_review queue", html)
        self.assertIn("Missing meaning queue", html)
        self.assertIn("setMeaningNeedsReviewQueue", html)
        self.assertIn("setMissingMeaningQueue", html)
        self.assertIn("ensureReviewQueueScope", html)
        self.assertIn(
            "const scopeIds = ['majorCode', 'middleCode', 'smallCode', 'subCode', 'keyword'];",
            html,
        )
        self.assertIn("Latest label review audit", html)
        self.assertIn("No label review audit yet", html)
        self.assertIn("reviewMeaningCandidate", html)
        self.assertIn("Latest meaning review audit", html)
        self.assertIn("No meaning review audit yet", html)
        self.assertIn("review-audit", html)
        self.assertIn("transform state", html)
        self.assertIn("comparison:", html)
        self.assertIn("review priority", html)
        self.assertIn("short_label_review_priority", html)
        self.assertIn("Label Review / 사람 확인 상태", html)
        self.assertIn("label_review_status", html)
        self.assertIn("Pending candidate / 미확인", html)
        self.assertIn("label triage progress", html)
        self.assertIn("human review progress", html)
        self.assertIn("label progress unit / 집계 단위", html)
        self.assertIn("labelProgressUnit", html)
        self.assertIn("pending labels / 미확인 단어형 후보", html)
        self.assertIn("needs revision labels / 수정필요", html)
        self.assertIn("rejected labels / 거절", html)
        self.assertIn("missing labels / 후보 없음", html)
        self.assertIn("원문 KSA", html)
        self.assertIn("전처리 단계", html)
        self.assertIn("단어형 전처리 결과", html)
        self.assertIn("/ksa-preprocessing-pipeline-status", html)
        self.assertIn("Preprocessing pipeline status", html)

    def test_ksa_review_html_resolver_uses_latest_seedpack_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            old_label = reports / "ksa_label_needs_review_seedpack_20260621.html"
            new_label = reports / "ksa_label_needs_review_seedpack_20260622.html"
            meaning = reports / "ksa_meaning_needs_review_seedpack_20260622.html"
            missing_scoped = reports / "ksa_meaning_missing_scoped_seedpack_20260622.html"
            pipeline = reports / "ksa_preprocessing_pipeline_status_20260622.html"
            old_label.write_text("<html>old</html>", encoding="utf-8")
            new_label.write_text("<html>new</html>", encoding="utf-8")
            meaning.write_text("<html>meaning</html>", encoding="utf-8")
            missing_scoped.write_text("<html>missing</html>", encoding="utf-8")
            pipeline.write_text("<html>pipeline</html>", encoding="utf-8")

            self.assertEqual(
                resolve_ksa_review_html_path(KSA_LABEL_NEEDS_REVIEW_HTML_GLOB, reports),
                new_label,
            )
            self.assertEqual(
                resolve_ksa_review_html_path(KSA_MEANING_NEEDS_REVIEW_HTML_GLOB, reports),
                meaning,
            )
            self.assertEqual(
                resolve_ksa_review_html_path(KSA_MEANING_MISSING_SCOPED_HTML_GLOB, reports),
                missing_scoped,
            )
            self.assertEqual(
                resolve_ksa_review_html_path(KSA_PREPROCESSING_PIPELINE_HTML_GLOB, reports),
                pipeline,
            )

    def test_ksa_definition_dashboard_surfaces_linked_concept_definitions(self) -> None:
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("02", "Business", "02", "HR", "02", "People", "01", "Recruiting"),
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "0202020101_26v1",
                        "0202020101",
                        "26v1",
                        "Recruitment planning",
                        "4",
                        classification_id,
                        timestamp,
                        timestamp,
                    ),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("0202020101_26v1", "1", "01", "Interview design", "4"),
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO performance_criteria(
                        element_id, criteria_no, criteria_text_raw
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        element_id,
                        "1",
                        "Design structured interview questions and scoring rubrics.",
                    ),
                )
                criteria_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (element_id, "S", "skill", "1", "structured interview skill"),
                )
                defined_ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition,
                        definition_source, definition_status, relation_status,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "structured interview skill",
                        "structured_interview_skill",
                        "skill",
                        "Ability to design and run structured interviews.",
                        "test",
                        "defined",
                        "linked",
                        "human_reviewed",
                        timestamp,
                        timestamp,
                    ),
                )
                defined_concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (defined_ksa_id, defined_concept_id, "human_reviewed", timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO criteria_concept_links(
                        criteria_id, concept_id, relation_type, link_status, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (criteria_id, defined_concept_id, "requires_skill", "candidate", timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                        normalized_key, split_method, review_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        defined_ksa_id,
                        element_id,
                        "skill",
                        1,
                        "structured interview skill",
                        "structured_interview_skill",
                        "single_item",
                        "candidate",
                        timestamp,
                    ),
                )
                defined_atomic_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_concept_links(
                        atomic_id, concept_id, link_status, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (defined_atomic_id, defined_concept_id, "candidate", timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        defined_concept_id,
                        defined_ksa_id,
                        defined_atomic_id,
                        "skill",
                        "structured interview skill",
                        "people ops",
                        "peopleops",
                        "short_representative_label",
                        "rule_based_short_label_candidate",
                        1,
                        0.83,
                        "candidate",
                        timestamp,
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_meaning_candidates(
                        concept_id, concept_type, meaning_role, meaning_text,
                        source_method, unit_code, element_id, criteria_id, ksa_id,
                        confidence_score, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        defined_concept_id,
                        "skill",
                        "definition_candidate",
                        "Design interview questions and scoring rubrics.",
                        "task_context",
                        "0202020101_26v1",
                        element_id,
                        criteria_id,
                        defined_ksa_id,
                        0.91,
                        "candidate",
                        timestamp,
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_meaning_candidates(
                        concept_id, concept_type, meaning_role, meaning_text,
                        source_method, evidence_text, unit_code, element_id, criteria_id,
                        ksa_id, confidence_score, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        defined_concept_id,
                        "skill",
                        "term_definition_candidate",
                        "Concise term definition candidate.",
                        "term_definition_template",
                        "unit: Recruitment planning | KSA: structured interview skill",
                        "0202020101_26v1",
                        element_id,
                        criteria_id,
                        defined_ksa_id,
                        0.72,
                        "candidate",
                        timestamp,
                        timestamp,
                    ),
                )
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (element_id, "K", "knowledge", "2", "selection compliance knowledge"),
                )
                missing_ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "selection compliance knowledge",
                        "selection_compliance_knowledge",
                        "knowledge",
                        "missing",
                        "linked",
                        "raw",
                        timestamp,
                        timestamp,
                    ),
                )
                missing_concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (missing_ksa_id, missing_concept_id, "raw", timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        element_id,
                        "S",
                        "skill",
                        "3",
                        "candidate interview definition text",
                    ),
                )
                candidate_ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition,
                        definition_source, definition_status, relation_status,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "candidate interview definition text",
                        "candidate_interview_definition_text",
                        "skill",
                        "Model-preprocessed candidate definition.",
                        "test",
                        "candidate",
                        "linked",
                        "model_preprocessed",
                        timestamp,
                        timestamp,
                    ),
                )
                candidate_concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (candidate_ksa_id, candidate_concept_id, "candidate", timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            defined = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "definition_state": ["defined"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(defined["ok"])
            self.assertEqual(defined["schema"], "ncs_ksa_definition_dashboard_v1")
            self.assertEqual(defined["summary"]["matching_ksa"], 1)
            self.assertEqual(defined["summary"]["defined_concepts"], 1)
            self.assertEqual(defined["summary"]["atomic_preprocessed_ksa"], 1)
            self.assertEqual(defined["summary"]["atomic_concept_linked_ksa"], 1)
            self.assertEqual(defined["summary"]["label_candidate_concepts"], 1)
            self.assertEqual(defined["summary"]["shortened_label_candidate_concepts"], 1)
            self.assertEqual(defined["summary"]["human_confirmed_label_candidate_anomalies"], 0)
            self.assertEqual(defined["summary"]["llm_or_human_reviewed_label_candidate_concepts"], 0)
            self.assertNotIn("trusted_label_candidate_concepts", defined["summary"])
            self.assertNotIn("trusted_label_candidate_anomalies", defined["summary"])
            self.assertEqual(defined["summary"]["task_context_evidence_concepts"], 1)
            self.assertFalse(defined["policy"]["status_update_allowed"])
            self.assertEqual(len(defined["items"]), 1)
            self.assertEqual(
                defined["items"][0]["atomic_ksa_sample"],
                "structured interview skill",
            )
            self.assertEqual(defined["items"][0]["ksa_text_raw"], "structured interview skill")
            self.assertEqual(defined["items"][0]["atomic_ksa_count"], 1)
            self.assertEqual(defined["items"][0]["atomic_concept_link_count"], 1)
            self.assertEqual(
                defined["items"][0]["concept_name"],
                "structured interview skill",
            )
            self.assertEqual(defined["items"][0]["short_label_candidate"], "people ops")
            self.assertEqual(
                defined["items"][0]["short_label_source_text"],
                "structured interview skill",
            )
            self.assertIsInstance(defined["items"][0]["short_label_id"], int)
            self.assertEqual(defined["items"][0]["short_label_source_scope_key"], "unknown")
            self.assertEqual(
                defined["items"][0]["short_label_source_method"],
                "rule_based_short_label_candidate",
            )
            self.assertEqual(defined["items"][0]["short_label_review_status"], "candidate")
            self.assertEqual(defined["items"][0]["short_label_review_priority"], "low")
            self.assertEqual(defined["items"][0]["short_label_review_reason"], "pending_candidate")
            self.assertEqual(defined["label_review_progress"]["total"], 1)
            self.assertEqual(defined["label_review_progress"]["pending"], 1)
            self.assertEqual(defined["label_review_progress"]["human_reviewed"], 0)
            self.assertEqual(defined["label_review_progress"]["coverage_percent"], 0.0)
            self.assertEqual(defined["label_review_progress"]["unit"], "filtered_ksa_rows")
            self.assertIn("meaning_review_status_counts", defined)
            self.assertIn("meaning_review_progress", defined)
            self.assertEqual(defined["meaning_review_progress"]["total"], 1)
            self.assertEqual(defined["meaning_review_progress"]["pending"], 1)
            self.assertEqual(defined["meaning_review_progress"]["human_reviewed"], 0)
            self.assertEqual(defined["meaning_review_progress"]["human_reviewed_percent"], 0.0)
            self.assertEqual(
                defined["meaning_review_progress"]["unit"],
                "filtered_term_definition_candidates",
            )
            self.assertIn("candidate", defined["meaning_review_status_counts"])
            self.assertEqual(defined["summary"]["llm_reviewed_meaning_concepts"], 0)
            self.assertEqual(defined["summary"]["needs_review_meaning_concepts"], 0)
            self.assertEqual(defined["summary"]["candidate_meaning_concepts"], 1)
            self.assertEqual(defined["items"][0]["short_label_quality_flags"], [])
            self.assertEqual(defined["items"][0]["short_label_quality_flag_count"], 0)
            self.assertEqual(
                defined["items"][0]["definition"],
                "Ability to design and run structured interviews.",
            )
            self.assertEqual(
                defined["items"][0]["term_definition_candidate"],
                "Concise term definition candidate.",
            )
            self.assertFalse(defined["items"][0]["definition_is_machine_draft"])
            self.assertTrue(defined["items"][0]["definition_is_human_approved"])
            self.assertFalse(defined["items"][0]["short_label_is_machine_screened"])
            self.assertFalse(defined["items"][0]["short_label_is_human_approved"])
            self.assertTrue(defined["items"][0]["term_definition_candidate_is_machine_draft"])
            self.assertEqual(
                defined["items"][0]["term_definition_evidence"],
                "unit: Recruitment planning | KSA: structured interview skill",
            )
            self.assertEqual(
                defined["items"][0]["term_definition_role"],
                "term_definition_candidate",
            )
            self.assertEqual(defined["items"][0]["term_definition_review_status"], "candidate")
            self.assertEqual(defined["items"][0]["term_definition_confidence"], 0.72)
            self.assertEqual(
                defined["items"][0]["meaning_candidate"],
                "Design interview questions and scoring rubrics.",
            )
            self.assertEqual(defined["items"][0]["atomic_label_candidate_count"], 1)
            self.assertEqual(
                defined["items"][0]["atomic_label_candidates"][0]["atom_text"],
                "structured interview skill",
            )
            self.assertEqual(
                defined["items"][0]["atomic_label_candidates"][0]["concept_name"],
                "structured interview skill",
            )
            self.assertEqual(
                defined["items"][0]["atomic_label_candidates"][0]["label_text"],
                "people ops",
            )
            self.assertGreaterEqual(defined["items"][0]["task_evidence_count"], 2)
            self.assertIn(criteria_id, defined["items"][0]["criteria_ids"])
            self.assertIn(
                "Design structured interview questions and scoring rubrics.",
                defined["items"][0]["criteria_text_preview"],
            )
            self.assertTrue(
                any(
                    "criteria_concept_links.requires_skill" in item
                    for item in defined["items"][0]["task_evidence_refs"]
                )
            )
            self.assertTrue(defined["items"][0]["task_evidence_preview"])

            shortened = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "label_state": ["shortened"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(shortened["ok"])
            self.assertEqual(shortened["filters"]["label_state"], "shortened")
            self.assertEqual(shortened["summary"]["matching_ksa"], 1)
            self.assertEqual(shortened["items"][0]["short_label_candidate"], "people ops")

            label_keyword = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["people ops"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(label_keyword["ok"])
            self.assertEqual(label_keyword["summary"]["matching_ksa"], 1)
            self.assertEqual(label_keyword["items"][0]["short_label_candidate"], "people ops")

            pending_label = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "label_review_status": ["candidate"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(pending_label["ok"])
            self.assertEqual(pending_label["filters"]["label_review_status"], "candidate")
            self.assertEqual(pending_label["summary"]["matching_ksa"], 1)
            self.assertEqual(pending_label["items"][0]["short_label_review_status"], "candidate")

            human_checked_label = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "label_review_status": ["human_reviewed"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(human_checked_label["ok"])
            self.assertEqual(human_checked_label["summary"]["matching_ksa"], 0)

            update_conn = connect(db_path)
            try:
                update_conn.execute(
                    """
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'llm_reviewed', updated_at = ?
                    WHERE concept_id = ?
                    """,
                    (timestamp, defined_concept_id),
                )
                update_conn.commit()
            finally:
                update_conn.close()
            llm_checked_label = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "label_review_status": ["llm_reviewed"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(llm_checked_label["ok"])
            self.assertEqual(llm_checked_label["filters"]["label_review_status"], "llm_reviewed")
            self.assertEqual(llm_checked_label["summary"]["matching_ksa"], 1)
            self.assertEqual(
                llm_checked_label["summary"]["llm_or_human_reviewed_label_candidate_concepts"],
                1,
            )
            self.assertNotIn("trusted_label_candidate_concepts", llm_checked_label["summary"])
            self.assertEqual(
                llm_checked_label["items"][0]["short_label_review_status"],
                "llm_reviewed",
            )
            self.assertEqual(
                llm_checked_label["items"][0]["short_label_review_priority"],
                "machine_reviewed",
            )
            self.assertEqual(
                llm_checked_label["items"][0]["short_label_review_reason"],
                "automated_llm_reviewed_not_approval",
            )

            update_conn = connect(db_path)
            try:
                update_conn.execute(
                    """
                    UPDATE ontology_concepts
                    SET definition_source = 'ksa_meaning_candidate_promotion',
                        review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE concept_id = ?
                    """,
                    (timestamp, defined_concept_id),
                )
                update_conn.commit()
            finally:
                update_conn.close()
            promoted_machine_draft = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "definition_state": ["defined"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(promoted_machine_draft["ok"])
            self.assertTrue(promoted_machine_draft["items"][0]["definition_is_machine_draft"])
            self.assertFalse(promoted_machine_draft["items"][0]["definition_is_human_approved"])

            pending_meaning = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "meaning_review_status": ["candidate"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(pending_meaning["ok"])
            self.assertEqual(pending_meaning["filters"]["meaning_review_status"], "candidate")
            self.assertEqual(pending_meaning["summary"]["matching_ksa"], 1)
            self.assertEqual(pending_meaning["items"][0]["meaning_review_status"], "candidate")

            llm_checked_meaning = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "meaning_review_status": ["llm_reviewed"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(llm_checked_meaning["ok"])
            self.assertEqual(llm_checked_meaning["filters"]["meaning_review_status"], "llm_reviewed")
            self.assertEqual(llm_checked_meaning["summary"]["matching_ksa"], 0)

            unchanged = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["interview"],
                    "label_state": ["unchanged"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(unchanged["ok"])
            self.assertEqual(unchanged["summary"]["matching_ksa"], 0)

            missing = get_ksa_definitions(
                db_path,
                {"definition_state": ["missing"], "limit": ["5"]},
            )
            self.assertTrue(missing["ok"])
            self.assertEqual(missing["summary"]["missing_definition_concepts"], 1)
            self.assertEqual(missing["items"][0]["definition_status"], "missing")

    def test_ksa_definition_dashboard_filters_quality_review_labels(self) -> None:
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
                    ) VALUES ('02', 'Business', '04', 'Production',
                              '01', 'Management', '04', 'SCM')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0204010403_26v1', '0204010403', '26v1',
                              'Demand planning', '6', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0204010403_26v1', '1', '01', 'Demand plan', '6')
                    """
                )
                element_id = cur.lastrowid
                source_text = "MPS(Master Planning and Scheduling)"
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', ?)
                    """,
                    (element_id, source_text),
                )
                ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES (?, 'mpsmasterplanningandscheduling', 'knowledge',
                              'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (source_text, timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                    (ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, 'knowledge', ?, 'MPS', 'mps',
                              'short_representative_label',
                              'rule_based_short_label_candidate', 1, 0.73,
                              'candidate', ?, ?)
                    """,
                    (concept_id, ksa_id, source_text, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "label_state": ["quality_review"], "limit": ["5"]},
            )
            broad = get_ksa_definitions(db_path, {"limit": ["5"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["filters"]["label_state"], "quality_review")
        self.assertEqual(result["summary"]["matching_ksa"], 1)
        self.assertEqual(result["summary"]["quality_review_label_candidate_concepts"], 1)
        self.assertEqual(result["label_quality_flag_counts"]["generic_or_low_specificity"], 1)
        self.assertEqual(result["label_quality_flag_counts"]["short_acronym_needs_context"], 1)
        self.assertEqual(result["label_quality_flag_counts"]["very_low_label_source_ratio"], 1)
        self.assertEqual(result["items"][0]["short_label_candidate"], "MPS")
        self.assertIn("short_acronym_needs_context", result["items"][0]["short_label_quality_flags"])
        self.assertGreater(result["items"][0]["short_label_quality_flag_count"], 0)
        self.assertTrue(broad["ok"])
        self.assertIsNone(broad["summary"]["quality_review_label_candidate_concepts"])
        self.assertEqual(broad["label_quality_flag_counts"], {})

    def test_ksa_definition_dashboard_quality_review_displays_matching_label_candidate(self) -> None:
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
                    ) VALUES ('02', 'Business', '04', 'Production',
                              '01', 'Management', '04', 'SCM')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0204010404_26v1', '0204010404', '26v1',
                              'Supply planning', '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0204010404_26v1', '1', '01', 'Plan flow', '5')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'symbol-heavy label source')
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
                    ) VALUES ('symbol-heavy label source', 'symbolheavylabelsource',
                              'knowledge', 'candidate', 'linked',
                              'model_preprocessed', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                    (ksa_id, concept_id, timestamp),
                )
                for label_text, key, rank, confidence, review_status in (
                    ("planning flow", "planningflow", 1, 0.95, "candidate"),
                    ("A/B-C/D", "abcd", 2, 0.30, "needs_review"),
                ):
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id, concept_type,
                            source_text, label_text, normalized_label_key, label_role,
                            source_method, candidate_rank, confidence_score,
                            review_status, created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', 'symbol-heavy label source',
                                  ?, ?, 'short_representative_label',
                                  'rule_based_short_label_candidate', ?, ?,
                                  ?, ?, ?)
                        """,
                        (
                            concept_id,
                            ksa_id,
                            label_text,
                            key,
                            rank,
                            confidence,
                            review_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            before_quality_mtime = db_path.stat().st_mtime_ns
            all_rows = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})
            quality_rows = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "label_state": ["quality_review"], "limit": ["5"]},
            )
            after_quality_mtime = db_path.stat().st_mtime_ns
            keyword_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "limit": ["5"]},
            )
            keyword_quality_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "label_state": ["quality_review"], "limit": ["5"]},
            )
            keyword_concept_type_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "concept_type": ["knowledge"], "limit": ["5"]},
            )
            keyword_wrong_concept_type_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "concept_type": ["skill"], "limit": ["5"]},
            )
            keyword_definition_state_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "definition_state": ["candidate"], "limit": ["5"]},
            )
            keyword_shortened_rows = get_ksa_definitions(
                db_path,
                {"keyword": ["symbol-heavy"], "label_state": ["shortened"], "limit": ["5"]},
            )
            after_keyword_mtime = db_path.stat().st_mtime_ns

        self.assertTrue(all_rows["ok"])
        self.assertEqual(all_rows["items"][0]["short_label_candidate"], "planning flow")
        self.assertEqual(all_rows["items"][0]["short_label_quality_flags"], [])
        self.assertTrue(quality_rows["ok"])
        self.assertEqual(quality_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(quality_rows["items"][0]["short_label_candidate"], "A/B-C/D")
        self.assertEqual(quality_rows["items"][0]["short_label_review_status"], "needs_review")
        self.assertEqual(quality_rows["items"][0]["short_label_review_priority"], "high")
        self.assertEqual(
            quality_rows["items"][0]["short_label_review_reason"],
            "auto_quality_needs_review",
        )
        self.assertEqual(quality_rows["label_review_status_counts"], {"needs_review": 1})
        self.assertIn("symbol_heavy", quality_rows["items"][0]["short_label_quality_flags"])
        self.assertEqual(quality_rows["label_quality_flag_counts"]["symbol_heavy"], 1)
        self.assertEqual(after_quality_mtime, before_quality_mtime)
        self.assertTrue(keyword_rows["ok"])
        self.assertEqual(keyword_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(keyword_rows["items"][0]["short_label_candidate"], "planning flow")
        self.assertTrue(keyword_quality_rows["ok"])
        self.assertEqual(keyword_quality_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(keyword_quality_rows["items"][0]["short_label_candidate"], "A/B-C/D")
        self.assertEqual(keyword_quality_rows["items"][0]["short_label_review_status"], "needs_review")
        self.assertTrue(keyword_concept_type_rows["ok"])
        self.assertEqual(keyword_concept_type_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(keyword_concept_type_rows["concept_type_counts"], {"knowledge": 1})
        self.assertEqual(keyword_concept_type_rows["items"][0]["short_label_candidate"], "planning flow")
        self.assertTrue(keyword_wrong_concept_type_rows["ok"])
        self.assertEqual(keyword_wrong_concept_type_rows["summary"]["matching_ksa"], 0)
        self.assertEqual(keyword_wrong_concept_type_rows["items"], [])
        self.assertTrue(keyword_definition_state_rows["ok"])
        self.assertEqual(keyword_definition_state_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(keyword_definition_state_rows["definition_status_counts"], {"candidate": 1})
        self.assertTrue(keyword_shortened_rows["ok"])
        self.assertEqual(keyword_shortened_rows["summary"]["matching_ksa"], 1)
        self.assertEqual(keyword_shortened_rows["items"][0]["short_label_candidate"], "planning flow")
        self.assertEqual(after_keyword_mtime, before_quality_mtime)

    def test_ksa_definition_dashboard_broad_label_review_counts_representative_label(self) -> None:
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
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'HR')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'HR strategy', '5')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name,
                        ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'multi label concept')
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
                    ) VALUES ('multi label concept', 'multilabelconcept', 'knowledge',
                              'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                    (ksa_id, concept_id, timestamp),
                )
                for label_text, key, rank, status in (
                    ("representative label", "representativelabel", 1, "candidate"),
                    ("lower reviewed label", "lowerreviewedlabel", 2, "human_reviewed"),
                ):
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id, concept_type,
                            source_text, label_text, normalized_label_key, label_role,
                            source_method, candidate_rank, confidence_score,
                            review_status, created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', 'multi label concept',
                                  ?, ?, 'short_representative_label',
                                  'rule_based_short_label_candidate', ?, 0.8,
                                  ?, ?, ?)
                        """,
                        (concept_id, ksa_id, label_text, key, rank, status, timestamp, timestamp),
                    )
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('off dashboard label concept', 'offdashboardlabelconcept', 'knowledge',
                              'candidate', 'linked', 'model_preprocessed', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                off_dashboard_concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, 'knowledge', 'off dashboard source',
                              'off dashboard label', 'offdashboardlabel',
                              'short_representative_label',
                              'rule_based_short_label_candidate', 1, 0.8,
                              'candidate', ?, ?)
                    """,
                    (off_dashboard_concept_id, ksa_id, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(db_path, {"limit": ["5"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["concepts"], 1)
        self.assertEqual(result["summary"]["label_candidate_concepts"], 1)
        self.assertLessEqual(
            result["label_review_progress"]["total"],
            result["summary"]["concepts"],
        )
        self.assertEqual(result["items"][0]["short_label_candidate"], "representative label")
        self.assertEqual(result["label_review_status_counts"].get("candidate"), 1)
        self.assertEqual(result["label_review_status_counts"].get("human_reviewed", 0), 0)
        self.assertEqual(result["label_review_progress"]["pending"], 1)
        self.assertEqual(result["label_review_progress"]["human_reviewed"], 0)
        self.assertEqual(result["label_review_progress"]["total"], 1)
        self.assertEqual(
            result["label_review_progress"]["unit"],
            "representative_concepts_broad_summary",
        )

    def test_ksa_definition_dashboard_scopes_short_labels_by_source_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for major_code, major_name, unit_code, unit_name, element_name, raw_text in (
                    ("02", "Business", "0202020101_26v1", "HR planning", "HR element", "shared HR KSA"),
                    ("99", "Other", "9901010101_26v1", "Other planning", "Other element", "cross major source only"),
                ):
                    cur = conn.execute(
                        """
                        INSERT INTO classifications(
                            major_code, major_name, middle_code, middle_name,
                            small_code, small_name, sub_code, sub_name
                        ) VALUES (?, ?, '01', 'Middle', '01', 'Small', '01', 'Sub')
                        """,
                        (major_code, major_name),
                    )
                    classification_id = cur.lastrowid
                    conn.execute(
                        """
                        INSERT INTO competency_units(
                            unit_code, base_unit_code, unit_version, unit_name_raw,
                            unit_level_raw, classification_id, created_at, updated_at
                        ) VALUES (?, ?, '26v1', ?, '4', ?, ?, ?)
                        """,
                        (unit_code, unit_code[:10], unit_name, classification_id, timestamp, timestamp),
                    )
                    cur = conn.execute(
                        """
                        INSERT INTO competency_elements(
                            unit_code, element_no, element_code_raw,
                            element_name_raw, element_level_raw
                        ) VALUES (?, '1', '01', ?, '4')
                        """,
                        (unit_code, element_name),
                    )
                    element_id = cur.lastrowid
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', '1', ?)
                        """,
                        (element_id, raw_text),
                    )
                    if major_code == "02":
                        hr_ksa_id = cur.lastrowid
                    else:
                        other_ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('shared concept', 'sharedconcept', 'knowledge',
                              'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                    (hr_ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                    (other_ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, 'knowledge', 'cross major source only',
                              'cross major label', 'crossmajorlabel',
                              'short_representative_label', 'test_cross_major',
                              1, 0.4, 'candidate', ?, ?)
                    """,
                    (concept_id, other_ksa_id, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            hr = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})
            other = get_ksa_definitions(db_path, {"major_code": ["99"], "limit": ["5"]})

        self.assertTrue(hr["ok"])
        self.assertEqual(hr["summary"]["matching_ksa"], 1)
        self.assertEqual(hr["summary"]["label_candidate_concepts"], 0)
        self.assertIsNone(hr["items"][0]["short_label_candidate"])
        self.assertTrue(other["ok"])
        self.assertEqual(other["summary"]["label_candidate_concepts"], 1)
        self.assertEqual(other["items"][0]["short_label_candidate"], "cross major label")

    def test_ksa_definition_dashboard_uses_current_ksa_label_provenance(self) -> None:
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
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'HR')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'HR strategy', '5')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('shared concept', 'sharedconcept', 'knowledge',
                              'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                ksa_ids = []
                for ksa_no, raw_text, label_text, review_status in (
                    ("1", "first raw KSA text", "first short label", "candidate"),
                    ("2", "second raw KSA text", "second short label", "needs_review"),
                ):
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', ?, ?)
                        """,
                        (element_id, ksa_no, raw_text),
                    )
                    ksa_id = cur.lastrowid
                    ksa_ids.append(ksa_id)
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id,
                            concept_type, source_text, label_text,
                            normalized_label_key, label_role, source_method,
                            candidate_rank, confidence_score, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', ?, ?,
                                  ?, 'short_representative_label',
                                  'rule_based_short_label_candidate', 1, 0.8,
                                  ?, ?, ?)
                        """,
                        (
                            concept_id,
                            ksa_id,
                            raw_text,
                            label_text,
                            label_text.replace(" ", ""),
                            review_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["matching_ksa"], 2)
        self.assertEqual([item["ksa_id"] for item in result["items"]], ksa_ids)
        self.assertEqual(
            [item["short_label_candidate"] for item in result["items"]],
            ["first short label", "second short label"],
        )
        self.assertEqual(
            [item["short_label_source_text"] for item in result["items"]],
            ["first raw KSA text", "second raw KSA text"],
        )
        self.assertEqual(
            [item["short_label_source_ksa_id"] for item in result["items"]],
            ksa_ids,
        )
        self.assertEqual(
            [item["short_label_provenance_match"] for item in result["items"]],
            ["source_ksa_id", "source_ksa_id"],
        )
        self.assertEqual(result["items"][0]["short_label_candidate_count"], 1)
        self.assertEqual(result["items"][1]["short_label_candidate_count"], 1)
        self.assertEqual(result["label_review_status_counts"].get("candidate"), 1)
        self.assertEqual(result["label_review_status_counts"].get("needs_review"), 1)

    def test_ksa_definition_dashboard_scopes_meanings_by_source_ksa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                entries = []
                for major_code, major_name, unit_code, unit_name, raw_text in (
                    ("02", "Business", "0202020101_26v1", "HR planning", "hr shared KSA"),
                    ("99", "Other", "9901010101_26v1", "Other planning", "other shared KSA"),
                ):
                    cur = conn.execute(
                        """
                        INSERT INTO classifications(
                            major_code, major_name, middle_code, middle_name,
                            small_code, small_name, sub_code, sub_name
                        ) VALUES (?, ?, '01', 'Middle', '01', 'Small', '01', 'Sub')
                        """,
                        (major_code, major_name),
                    )
                    classification_id = cur.lastrowid
                    conn.execute(
                        """
                        INSERT INTO competency_units(
                            unit_code, base_unit_code, unit_version, unit_name_raw,
                            unit_level_raw, classification_id, created_at, updated_at
                        ) VALUES (?, ?, '26v1', ?, '4', ?, ?, ?)
                        """,
                        (unit_code, unit_code[:10], unit_name, classification_id, timestamp, timestamp),
                    )
                    cur = conn.execute(
                        """
                        INSERT INTO competency_elements(
                            unit_code, element_no, element_code_raw,
                            element_name_raw, element_level_raw
                        ) VALUES (?, '1', '01', 'Element', '4')
                        """,
                        (unit_code,),
                    )
                    element_id = cur.lastrowid
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', '1', ?)
                        """,
                        (element_id, raw_text),
                    )
                    entries.append(
                        {
                            "major_code": major_code,
                            "unit_code": unit_code,
                            "element_id": element_id,
                            "ksa_id": cur.lastrowid,
                        }
                    )
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('shared meaning concept', 'sharedmeaningconcept',
                              'knowledge', 'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                for entry in entries:
                    conn.execute(
                        """
                        INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                        VALUES (?, ?, 'raw', ?)
                        """,
                        (entry["ksa_id"], concept_id, timestamp),
                    )
                other_entry = entries[1]
                conn.execute(
                    """
                    INSERT INTO ksa_meaning_candidates(
                        concept_id, concept_type, meaning_role, meaning_text,
                        source_method, unit_code, element_id, ksa_id,
                        confidence_score, review_status, created_at, updated_at
                    ) VALUES (?, 'knowledge', 'task_knowledge_significance',
                              'Other major meaning only.',
                              'task_context_template', ?, ?, ?, 0.9,
                              'needs_review', ?, ?)
                    """,
                    (
                        concept_id,
                        other_entry["unit_code"],
                        other_entry["element_id"],
                        other_entry["ksa_id"],
                        timestamp,
                        timestamp,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            hr = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})
            hr_missing = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "meaning_review_status": ["missing"], "limit": ["5"]},
            )
            hr_needs_review = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "meaning_review_status": ["needs_review"], "limit": ["5"]},
            )
            other = get_ksa_definitions(db_path, {"major_code": ["99"], "limit": ["5"]})
            other_needs_review = get_ksa_definitions(
                db_path,
                {"major_code": ["99"], "meaning_review_status": ["needs_review"], "limit": ["5"]},
            )
            broad = get_ksa_definitions(db_path, {"limit": ["5"]})

        self.assertTrue(hr["ok"])
        self.assertEqual(hr["summary"]["matching_ksa"], 1)
        self.assertIsNone(hr["items"][0]["meaning_candidate"])
        self.assertEqual(hr["meaning_review_status_counts"].get("missing"), 1)
        self.assertEqual(hr["meaning_review_status_counts"].get("needs_review", 0), 0)
        self.assertTrue(hr_missing["ok"])
        self.assertEqual(hr_missing["summary"]["matching_ksa"], 1)
        self.assertTrue(hr_needs_review["ok"])
        self.assertEqual(hr_needs_review["summary"]["matching_ksa"], 0)
        self.assertTrue(other["ok"])
        self.assertEqual(other["items"][0]["meaning_candidate"], "Other major meaning only.")
        self.assertEqual(other["meaning_review_status_counts"].get("needs_review"), 1)
        self.assertTrue(other_needs_review["ok"])
        self.assertEqual(other_needs_review["summary"]["matching_ksa"], 1)
        self.assertTrue(broad["ok"])
        self.assertEqual(broad["meaning_review_status_counts"].get("missing"), 1)
        self.assertEqual(broad["meaning_review_status_counts"].get("needs_review"), 1)

    def test_ksa_definition_dashboard_ignores_unit_only_meaning_candidates(self) -> None:
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
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'HR')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'HR strategy', '5')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('same unit concept', 'sameunitconcept', 'knowledge',
                              'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                for ksa_no, raw_text in (("1", "first same unit KSA"), ("2", "second same unit KSA")):
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', ?, ?)
                        """,
                        (element_id, ksa_no, raw_text),
                    )
                    conn.execute(
                        """
                        INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                        VALUES (?, ?, 'raw', ?)
                        """,
                        (cur.lastrowid, concept_id, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO ksa_meaning_candidates(
                        concept_id, concept_type, meaning_role, meaning_text,
                        source_method, unit_code, confidence_score, review_status,
                        created_at, updated_at
                    ) VALUES (?, 'knowledge', 'task_knowledge_significance',
                              'Unit-only legacy meaning should not display.',
                              'task_context_template', '0202020101_26v1',
                              0.9, 'candidate', ?, ?)
                    """,
                    (concept_id, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["matching_ksa"], 2)
        self.assertEqual(result["summary"]["concepts_with_meaning_candidates"], 0)
        self.assertEqual(result["meaning_review_status_counts"].get("missing"), 2)
        self.assertEqual(result["meaning_review_status_counts"].get("candidate", 0), 0)
        self.assertEqual([item["meaning_candidate"] for item in result["items"]], [None, None])

    def test_ksa_definition_dashboard_does_not_display_provenance_less_short_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'HR')
                    """
                )
                classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                              'HR planning', '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '0202020101_26v1 1', 'HR strategy', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'organization diagnosis method')
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
                    ) VALUES ('organization diagnosis method', 'organizationdiagnosismethod',
                              'knowledge', 'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, 'raw', ?)
                    """,
                    (ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, concept_type,
                        source_text, label_text, normalized_label_key, label_role,
                        source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, NULL, NULL, 'knowledge', 'legacy orphan',
                              'orphan label', 'orphanlabel',
                              'short_representative_label', 'legacy_orphan',
                              1, 0.2, 'candidate', ?, ?)
                    """,
                    (concept_id, timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["label_candidate_concepts"], 0)
        self.assertEqual(result["summary"]["provenance_missing_label_candidate_concepts"], 1)
        self.assertEqual(result["summary"]["missing_label_candidate_concepts"], 1)
        self.assertIsNone(result["items"][0]["short_label_candidate"])

    def test_ksa_definition_dashboard_scopes_collision_labels_to_current_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                entries = []
                for major_code, major_name, unit_code, text, concept_name in (
                    ("02", "Business", "0202020101_26v1", "hr source KSA", "hr concept"),
                    ("99", "Other", "9901010101_26v1", "other source KSA", "other concept"),
                ):
                    cur = conn.execute(
                        """
                        INSERT INTO classifications(
                            major_code, major_name, middle_code, middle_name,
                            small_code, small_name, sub_code, sub_name
                        ) VALUES (?, ?, '01', 'Middle', '01', 'Small', '01', 'Sub')
                        """,
                        (major_code, major_name),
                    )
                    classification_id = cur.lastrowid
                    conn.execute(
                        """
                        INSERT INTO competency_units(
                            unit_code, base_unit_code, unit_version, unit_name_raw,
                            unit_level_raw, classification_id, created_at, updated_at
                        ) VALUES (?, ?, '26v1', ?, '4', ?, ?, ?)
                        """,
                        (unit_code, unit_code[:10], f"{major_name} unit", classification_id, timestamp, timestamp),
                    )
                    cur = conn.execute(
                        """
                        INSERT INTO competency_elements(
                            unit_code, element_no, element_code_raw,
                            element_name_raw, element_level_raw
                        ) VALUES (?, '1', '01', ?, '4')
                        """,
                        (unit_code, f"{major_name} element"),
                    )
                    element_id = cur.lastrowid
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', '1', ?)
                        """,
                        (element_id, text),
                    )
                    ksa_id = cur.lastrowid
                    cur = conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, relation_status, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'knowledge', 'missing', 'linked', 'raw', ?, ?)
                        """,
                        (concept_name, concept_name.replace(" ", ""), timestamp, timestamp),
                    )
                    concept_id = cur.lastrowid
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id, concept_type,
                            source_text, label_text, normalized_label_key, label_role,
                            source_method, candidate_rank, confidence_score,
                            review_status, created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', ?, 'shared label',
                                  'sharedlabel', 'short_representative_label',
                                  'rule_based_short_label_candidate', 1, 0.8,
                                  'candidate', ?, ?)
                        """,
                        (concept_id, ksa_id, text, timestamp, timestamp),
                    )
                    entries.append((major_code, ksa_id, concept_id))
                conn.commit()
            finally:
                conn.close()

            hr_all = get_ksa_definitions(db_path, {"major_code": ["02"], "limit": ["5"]})
            hr_collision = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "label_state": ["collision"], "limit": ["5"]},
            )

        self.assertTrue(hr_all["ok"])
        self.assertEqual(hr_all["summary"]["matching_ksa"], 1)
        self.assertEqual(hr_all["summary"]["collision_label_candidate_concepts"], 0)
        self.assertTrue(hr_collision["ok"])
        self.assertEqual(hr_collision["summary"]["matching_ksa"], 0)
        self.assertEqual(hr_collision["items"], [])

    def test_ksa_definition_dashboard_collision_displays_matching_label_candidate(self) -> None:
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
                    ) VALUES ('02', 'Business', '01', 'Middle', '01', 'Small', '01', 'Sub')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0201010101_26v1', '0201010101', '26v1',
                              'HR unit', '4', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0201010101_26v1', '1', '01', 'HR element', '4')
                    """
                )
                element_id = cur.lastrowid
                concept_ids = []
                for index, concept_name in enumerate(("first concept", "second concept"), start=1):
                    cur = conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name,
                            ksa_no, ksa_text_raw
                        ) VALUES (?, 'K', 'knowledge', ?, ?)
                        """,
                        (element_id, str(index), f"{concept_name} source text"),
                    )
                    ksa_id = cur.lastrowid
                    cur = conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, relation_status, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'knowledge', 'missing', 'linked', 'raw', ?, ?)
                        """,
                        (concept_name, concept_name.replace(" ", ""), timestamp, timestamp),
                    )
                    concept_id = cur.lastrowid
                    concept_ids.append(concept_id)
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                    if index == 1:
                        conn.execute(
                            """
                            INSERT INTO ontology_concept_label_candidates(
                                concept_id, source_ksa_id, source_atomic_id,
                                concept_type, source_text, label_text,
                                normalized_label_key, label_role, source_method,
                                candidate_rank, confidence_score, review_status,
                                created_at, updated_at
                            ) VALUES (?, ?, NULL, 'knowledge', ?, 'clean top label',
                                      'cleantoplabel', 'short_representative_label',
                                      'rule_based_short_label_candidate', 1, 0.9,
                                      'candidate', ?, ?)
                            """,
                            (concept_id, ksa_id, f"{concept_name} source text", timestamp, timestamp),
                        )
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id,
                            concept_type, source_text, label_text,
                            normalized_label_key, label_role, source_method,
                            candidate_rank, confidence_score, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', ?, 'shared label',
                                  'sharedlabel', 'short_representative_label',
                                  'rule_based_short_label_candidate', 2, 0.8,
                                  'candidate', ?, ?)
                        """,
                        (concept_id, ksa_id, f"{concept_name} source text", timestamp, timestamp),
                    )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "label_state": ["collision"], "limit": ["5"]},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["matching_ksa"], 2)
        self.assertEqual(result["summary"]["collision_label_candidate_concepts"], 2)
        self.assertEqual([item["short_label_candidate"] for item in result["items"]], ["shared label", "shared label"])

    def test_ksa_definition_dashboard_review_label_filters_require_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
            finally:
                conn.close()

            results = [
                get_ksa_definitions(db_path, {"label_state": [label_state], "limit": ["5"]})
                for label_state in ("quality_review", "collision")
            ]
            results.extend(
                get_ksa_definitions(db_path, {"label_review_status": [review_status], "limit": ["5"]})
                for review_status in ("candidate", "llm_reviewed", "human_reviewed", "needs_review", "missing")
            )
            results.extend(
                get_ksa_definitions(
                    db_path,
                    {
                        "label_review_status": ["candidate"],
                        scope_key: [scope_value],
                        "limit": ["5"],
                    },
                )
                for scope_key, scope_value in (
                    ("concept_type", "knowledge"),
                    ("definition_state", "candidate"),
                )
            )

        for result in results:
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "scope_required")
            self.assertEqual(result["items"], [])

    def test_ksa_definition_dashboard_missing_label_excludes_unlinked_ksa(self) -> None:
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
                    ) VALUES ('02', 'Business', '01', 'Middle', '01', 'Small', '01', 'Sub')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0201010101_26v1', '0201010101', '26v1',
                              'HR unit', '4', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0201010101_26v1', '1', '01', 'HR element', '4')
                    """
                )
                element_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'K', 'knowledge', '1', 'linked concept without label')
                    """,
                    (element_id,),
                )
                linked_ksa_id = cur.lastrowid
                cur = conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, relation_status, review_status,
                        created_at, updated_at
                    ) VALUES ('linked concept without label', 'linkedconceptwithoutlabel',
                              'knowledge', 'missing', 'linked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, 'raw', ?)
                    """,
                    (linked_ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'S', 'skill', '2', 'unlinked KSA row')
                    """,
                    (element_id,),
                )
                conn.commit()
            finally:
                conn.close()

            result = get_ksa_definitions(
                db_path,
                {"major_code": ["02"], "label_state": ["missing"], "limit": ["5"]},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["matching_ksa"], 1)
        self.assertEqual(result["summary"]["concepts"], 1)
        self.assertEqual(result["summary"]["missing_label_candidate_concepts"], 1)
        self.assertEqual(result["items"][0]["ksa_text_raw"], "linked concept without label")

    def test_aihr_demo_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_DEMO_HTML_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_path = tmp_path / "aihr_plan_demo_20260101.html"
            new_path = tmp_path / "aihr_plan_demo_20260617.html"
            configured_path = tmp_path / "custom_demo.html"
            old_path.write_text("old", encoding="utf-8")
            new_path.write_text("new", encoding="utf-8")
            configured_path.write_text("configured", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            try:
                os.environ.pop("NCS_AIHR_DEMO_HTML_PATH", None)
                self.assertEqual(resolve_aihr_demo_html_path(tmp_path), new_path)
                os.environ["NCS_AIHR_DEMO_HTML_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_demo_html_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_DEMO_HTML_PATH"] = str(tmp_path / "missing.html")
                self.assertIsNone(resolve_aihr_demo_html_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_DEMO_HTML_PATH", None)
                else:
                    os.environ["NCS_AIHR_DEMO_HTML_PATH"] = previous

    def test_aihr_readiness_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_path = tmp_path / "aihr_release_readiness_20260101.json"
            new_path = tmp_path / "aihr_release_readiness_20260617.json"
            configured_path = tmp_path / "custom_readiness.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            try:
                os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                self.assertEqual(resolve_aihr_readiness_json_path(tmp_path), new_path)
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_readiness_json_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(tmp_path / "missing.json")
                self.assertIsNone(resolve_aihr_readiness_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous

    def test_aihr_review_triage_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_path = tmp_path / "aihr_review_triage_20260101.json"
            new_path = tmp_path / "aihr_review_triage_20260617.json"
            configured_path = tmp_path / "custom_triage.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            try:
                os.environ.pop("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH", None)
                self.assertEqual(resolve_aihr_review_triage_json_path(tmp_path), new_path)
                os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_review_triage_json_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = str(tmp_path / "missing.json")
                self.assertIsNone(resolve_aihr_review_triage_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = previous

    def test_aihr_provenance_reconfirmation_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readonly_path = tmp_path / "overnight_sessions" / "readonly_refresh"
            readonly_path.mkdir(parents=True)
            old_path = tmp_path / "aihr_human_review_provenance_reconfirmation_packet_20260101.json"
            new_path = tmp_path / "aihr_human_review_provenance_reconfirmation_packet_20260617.json"
            unprefixed_new_path = (
                tmp_path / "human_review_provenance_reconfirmation_packet_20260618.json"
            )
            session_new_path = (
                readonly_path / "human_review_provenance_reconfirmation_packet_20260619_7h.json"
            )
            configured_path = tmp_path / "custom_reconfirm.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            unprefixed_new_path.write_text("{}", encoding="utf-8")
            session_new_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            os.utime(unprefixed_new_path, (1_900_000_000, 1_900_000_000))
            os.utime(session_new_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                self.assertEqual(
                    resolve_aihr_provenance_reconfirmation_json_path(tmp_path),
                    session_new_path,
                )
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(configured_path)
                self.assertEqual(
                    resolve_aihr_provenance_reconfirmation_json_path(tmp_path),
                    configured_path,
                )
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    tmp_path / "missing.json"
                )
                self.assertIsNone(resolve_aihr_provenance_reconfirmation_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous

    def test_aihr_provenance_reconfirmation_not_found_hint_uses_proofset(self) -> None:
        previous = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    tmp_path / "missing.json"
                )
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                for route in [
                    "/aihr-provenance-reconfirmation",
                    "/api/aihr-provenance-reconfirmation",
                ]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 404)
                    payload = json.loads(raised.exception.read().decode("utf-8"))
                    self.assertEqual(
                        payload["error"],
                        "aihr_provenance_reconfirmation_not_found",
                    )
                    self.assertIn(
                        "export-human-review-provenance-reconfirmation-proofset",
                        payload["hint"],
                    )
                    self.assertNotIn(
                        "export-human-review-provenance-reconfirmation-packet first",
                        payload["hint"],
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous

    def test_aihr_agent_queue_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_current_path = tmp_path / "aihr_agent_queue_20260101.json"
            same_date_legacy_path = tmp_path / "aihr_agent_work_queue_20260617.json"
            new_current_path = tmp_path / "aihr_agent_queue_20260617.json"
            session_current_path = tmp_path / "aihr_agent_queue_20260618_8h.json"
            newer_status_path = tmp_path / "aihr_agent_queue_status_20260618.json"
            newer_run_path = tmp_path / "aihr_agent_queue_run_20260618.json"
            configured_path = tmp_path / "custom_queue.json"
            old_current_path.write_text("{}", encoding="utf-8")
            same_date_legacy_path.write_text("{}", encoding="utf-8")
            new_current_path.write_text("{}", encoding="utf-8")
            session_current_path.write_text("{}", encoding="utf-8")
            newer_status_path.write_text("{}", encoding="utf-8")
            newer_run_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_current_path, (1_700_000_000, 1_700_000_000))
            os.utime(same_date_legacy_path, (1_900_000_000, 1_900_000_000))
            os.utime(new_current_path, (1_800_000_000, 1_800_000_000))
            os.utime(session_current_path, (1_850_000_000, 1_850_000_000))
            os.utime(newer_status_path, (2_000_000_000, 2_000_000_000))
            os.utime(newer_run_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                self.assertEqual(resolve_aihr_agent_queue_json_path(tmp_path), session_current_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_agent_queue_json_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = str(tmp_path / "missing.json")
                self.assertIsNone(resolve_aihr_agent_queue_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous

    def test_aihr_agent_queue_path_uses_legacy_when_it_has_latest_date(self) -> None:
        previous = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            current_path = tmp_path / "aihr_agent_queue_20260101.json"
            legacy_path = tmp_path / "aihr_agent_work_queue_20260617.json"
            current_path.write_text("{}", encoding="utf-8")
            legacy_path.write_text("{}", encoding="utf-8")
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                self.assertEqual(resolve_aihr_agent_queue_json_path(tmp_path), legacy_path)
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous

    def test_aihr_agent_queue_path_prefers_readiness_declared_queue(self) -> None:
        previous_queue = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            declared_legacy_path = tmp_path / "aihr_agent_work_queue_20260617.json"
            newer_current_path = tmp_path / "aihr_agent_queue_20260624.json"
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            declared_legacy_path.write_text('{"schema":"aihr_agent_work_queue_v1"}', encoding="utf-8")
            newer_current_path.write_text('{"schema":"aihr_agent_work_queue_v1"}', encoding="utf-8")
            readiness_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "agent_work_queue_path": "reports/aihr_agent_work_queue_20260617.json",
                    }
                ),
                encoding="utf-8-sig",
            )
            os.utime(newer_current_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                self.assertEqual(resolve_aihr_agent_queue_json_path(tmp_path), declared_legacy_path)
            finally:
                if previous_queue is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous_queue
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness

    def test_aihr_agent_queue_path_does_not_fallback_when_readiness_declared_queue_missing(
        self,
    ) -> None:
        previous_queue = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            newer_current_path = tmp_path / "aihr_agent_queue_20260624.json"
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            newer_current_path.write_text('{"schema":"aihr_agent_work_queue_v1"}', encoding="utf-8")
            readiness_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "agent_work_queue_path": "reports/missing_agent_queue_20260617.json",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(newer_current_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                self.assertIsNone(resolve_aihr_agent_queue_json_path(tmp_path))
            finally:
                if previous_queue is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous_queue
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness

    def test_aihr_agent_queue_status_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_path = tmp_path / "aihr_agent_queue_status_20260101.json"
            new_path = tmp_path / "aihr_agent_queue_status_20260617.json"
            latest_date_path = tmp_path / "aihr_agent_queue_status_20260618.json"
            configured_path = tmp_path / "custom_queue_status.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            latest_date_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            os.utime(latest_date_path, (1_600_000_000, 1_600_000_000))
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                self.assertEqual(resolve_aihr_agent_queue_status_json_path(tmp_path), latest_date_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_agent_queue_status_json_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(tmp_path / "missing.json")
                self.assertIsNone(resolve_aihr_agent_queue_status_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous

    def test_aihr_artifact_cli_overrides_feed_live_resolvers(self) -> None:
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_custom.json"
            status_path = tmp_path / "aihr_agent_queue_status_custom.json"
            run_path = tmp_path / "aihr_agent_queue_run_custom.json"
            for path in [readiness_path, status_path, run_path]:
                path.write_text("{}", encoding="utf-8")
            try:
                apply_aihr_artifact_overrides(
                    SimpleNamespace(
                        aihr_readiness_json=readiness_path,
                        aihr_agent_queue_status_json=status_path,
                        aihr_agent_queue_run_json=run_path,
                    )
                )
                self.assertEqual(resolve_aihr_readiness_json_path(tmp_path), readiness_path)
                self.assertEqual(resolve_aihr_agent_queue_status_json_path(tmp_path), status_path)
                self.assertEqual(resolve_aihr_agent_queue_run_json_path(tmp_path), run_path)
            finally:
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_status
                if previous_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_run

    def test_dashboard_remote_bind_requires_explicit_override(self) -> None:
        self.assertTrue(is_dashboard_loopback_host("127.0.0.1"))
        self.assertTrue(is_dashboard_loopback_host("localhost"))
        self.assertTrue(is_dashboard_loopback_host("::1"))
        self.assertFalse(is_dashboard_loopback_host("0.0.0.0"))
        self.assertFalse(is_dashboard_loopback_host("192.168.0.10"))

        validate_dashboard_bind_host("127.0.0.1")
        with self.assertRaises(ValueError):
            validate_dashboard_bind_host("0.0.0.0")
        validate_dashboard_bind_host("0.0.0.0", allow_remote_bind=True)

    def test_validate_dashboard_port_identity_rejects_foreign_root(self) -> None:
        with patch(
            "ncs_dashboard.probe_dashboard_http_identity",
            return_value="foreign",
        ):
            with self.assertRaisesRegex(ValueError, "different web app"):
                validate_dashboard_port_identity("127.0.0.1", 8765)

    def test_validate_dashboard_port_identity_allows_ncs_dashboard_or_unreachable(self) -> None:
        with patch(
            "ncs_dashboard.probe_dashboard_http_identity",
            return_value="ncs_dashboard",
        ):
            validate_dashboard_port_identity("127.0.0.1", 8765)
        with patch(
            "ncs_dashboard.probe_dashboard_http_identity",
            return_value=None,
        ):
            validate_dashboard_port_identity("127.0.0.1", 8765)

    def test_dashboard_root_identity_markers_present_in_html(self) -> None:
        for marker in DASHBOARD_ROOT_IDENTITY_MARKERS:
            self.assertIn(marker, HTML)

    def test_aihr_queue_status_and_run_paths_prefer_readiness_static_artifacts(self) -> None:
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            declared_status_path = tmp_path / "aihr_agent_queue_status_20260617_release.json"
            declared_run_path = tmp_path / "aihr_agent_queue_run_20260617_release.json"
            newer_status_path = tmp_path / "aihr_agent_queue_status_20260624.json"
            newer_run_path = tmp_path / "aihr_agent_queue_run_20260624.json"
            for path in [declared_status_path, declared_run_path, newer_status_path, newer_run_path]:
                path.write_text("{}", encoding="utf-8")
            readiness_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "dashboard_surface_contract": {
                            "artifact": {
                                "static_artifacts": [
                                    {
                                        "name": "queue_status_json",
                                        "path": "reports/aihr_agent_queue_status_20260617_release.json",
                                    },
                                    {
                                        "name": "queue_run_json",
                                        "path": "reports/aihr_agent_queue_run_20260617_release.json",
                                    },
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.utime(newer_status_path, (2_000_000_000, 2_000_000_000))
            os.utime(newer_run_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                self.assertEqual(
                    resolve_aihr_agent_queue_status_json_path(tmp_path),
                    declared_status_path,
                )
                self.assertEqual(resolve_aihr_agent_queue_run_json_path(tmp_path), declared_run_path)
            finally:
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_status
                if previous_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_run

    def test_aihr_queue_status_and_run_paths_do_not_fallback_when_readiness_paths_missing(
        self,
    ) -> None:
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            newer_status_path = tmp_path / "aihr_agent_queue_status_20260624.json"
            newer_run_path = tmp_path / "aihr_agent_queue_run_20260624.json"
            newer_status_path.write_text("{}", encoding="utf-8")
            newer_run_path.write_text("{}", encoding="utf-8")
            readiness_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "dashboard_surface_contract": {
                            "artifact": {
                                "static_artifacts": [
                                    {
                                        "name": "queue_status_json",
                                        "path": "reports/missing_queue_status_20260617.json",
                                    },
                                    {
                                        "name": "queue_run_json",
                                        "path": "reports/missing_queue_run_20260617.json",
                                    },
                                ]
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.utime(newer_status_path, (2_000_000_000, 2_000_000_000))
            os.utime(newer_run_path, (2_000_000_000, 2_000_000_000))
            try:
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                self.assertIsNone(resolve_aihr_agent_queue_status_json_path(tmp_path))
                self.assertIsNone(resolve_aihr_agent_queue_run_json_path(tmp_path))
            finally:
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_status
                if previous_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_run

    def test_aihr_agent_queue_run_path_resolves_latest_or_configured_artifact(self) -> None:
        previous = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_path = tmp_path / "aihr_agent_queue_run_20260101.json"
            new_path = tmp_path / "aihr_agent_queue_run_20260617.json"
            latest_date_path = tmp_path / "aihr_agent_queue_run_20260618.json"
            dryrun_path = tmp_path / "aihr_agent_queue_run_dryrun_20260618.json"
            configured_path = tmp_path / "custom_queue_run.json"
            old_path.write_text("{}", encoding="utf-8")
            new_path.write_text("{}", encoding="utf-8")
            latest_date_path.write_text("{}", encoding="utf-8")
            dryrun_path.write_text("{}", encoding="utf-8")
            configured_path.write_text("{}", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            os.utime(latest_date_path, (1_600_000_000, 1_600_000_000))
            os.utime(dryrun_path, (1_900_000_000, 1_900_000_000))
            try:
                os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                self.assertEqual(resolve_aihr_agent_queue_run_json_path(tmp_path), latest_date_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(configured_path)
                self.assertEqual(resolve_aihr_agent_queue_run_json_path(tmp_path), configured_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(tmp_path / "missing.json")
                self.assertIsNone(resolve_aihr_agent_queue_run_json_path(tmp_path))
            finally:
                if previous is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous

    def test_render_aihr_readiness_html_shows_blockers_and_demo_contract(self) -> None:
        html = render_aihr_readiness_html(
            {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "ok_meaning": "report_generated_and_contract_checks_evaluated",
                "release_ready": False,
                "release_decision": {
                    "status": "blocked_until_requirements_met",
                    "release_ready": False,
                    "approval_claim": False,
                    "human_decision_required_for_release_claim": True,
                    "blocked_by": ["trusted_transition_scenarios"],
                },
                "approval_claim": False,
                "engineering_hygiene_ok": True,
                "blockers": [
                    {
                        "category": "human_review",
                        "name": "trusted_transition_scenarios",
                        "message": "too sparse",
                        "value": 1,
                        "threshold": ">= 10",
                    }
                ],
                "warnings": [],
                "next_actions": [
                    {
                        "owner": "evaluation-agent",
                        "blocker": "trusted_transition_scenarios",
                        "action": "Review transition scenarios.",
                        "command": "python scripts\\ncs_harness.py export-transition-scenario-seedpack --limit 100",
                    }
                ],
                "checks": {
                    "mcp_contract": [
                        {
                            "name": "Query router present",
                            "ok": True,
                            "detail": "scenario_count=7",
                        }
                    ]
                },
                "demo_contract": {
                    "ok": True,
                    "json_artifacts": [
                        {
                            "path": "reports/demo.json",
                            "ok": True,
                            "view": "ncs_education_plan",
                            "matrix_rows": 3,
                        }
                    ],
                    "html_artifact": {"path": "reports/demo.html", "ok": True, "length": 100},
                },
                "dashboard_surface_contract": {
                    "ok": True,
                    "artifact": {
                        "path": "reports/dashboard.json",
                        "scenario_count": 2,
                        "queue_status_summary": {"blocked_count": 0},
                        "review_chain_safety_summary": {
                            "contract_ok": True,
                            "schema": "aihr_plan_review_workflow_handoff_v1",
                            "source_payload_exposed": False,
                            "learning_module_visible_items": 3,
                            "ncs_report_visible_items": 59,
                            "ocr_context_card_count": 15,
                            "blocked_automation_actions": [
                                "auto_approve",
                                "write_human_reviewed_accepted_or_reviewed",
                            ],
                            "issues": [],
                        },
                        "static_artifacts": [
                            {
                                "name": "demo_json",
                                "path": "reports/demo.json",
                                "exists": True,
                                "non_empty": True,
                                "size_bytes": 512,
                            },
                            {
                                "name": "ncs006_element_api_checkpoint_json",
                                "path": "reports/checkpoint_ncs006_element_api_status_20260620.json",
                                "exists": True,
                                "non_empty": True,
                                "size_bytes": 4096,
                                "checkpoint": {
                                    "contract_ok": True,
                                    "schema": "ncs006_element_api_checkpoint_v1",
                                    "matched": 11537,
                                    "total": 47620,
                                    "not_collected": 33561,
                                    "active_batch_monitor_status": "within_child_timeout",
                                },
                            }
                        ],
                        "live_plan_summaries": [
                            {
                                "name": "baseline",
                                "ok": True,
                                "matrix_rows": 3,
                                "training_necessity_review_summary": {
                                    "schema": "aihr_training_necessity_review_v1",
                                    "guide_stage": "C1-2",
                                    "row_count": 3,
                                    "review_required_rows": 3,
                                    "approval_blocked_rows": 3,
                                    "approval_claim_safe": True,
                                },
                                "annual_operation_plan_summary": {
                                    "schema": "aihr_annual_operation_plan_seed_v1",
                                    "guide_stage": "C2-2",
                                    "row_count": 3,
                                    "estimated_total_hours": 36,
                                    "pending_human_decision_rows": 3,
                                    "approval_claim_safe": True,
                                },
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_matrix_fields": [],
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": [],
                            }
                        ],
                    },
                },
            },
            Path("reports/aihr_release_readiness_20260617.json"),
            triage_payload={
                "summary": {
                    "review_issue_type_counts": {
                        "hr_training_goal_link_human_review_required": 2
                    },
                    "transition_attention_count": 2,
                    "transition_seedpack_item_count": 5,
                    "transition_seedpack_id": "transition-seedpack-test",
                    "transition_status_snapshot": {
                        "trusted_review_status_count": 1,
                    },
                    "source_paths": {
                        "transition_seedpack": "reports/transition_scenario_seedpack.jsonl"
                    },
                }
            },
            triage_path=Path("reports/aihr_review_triage_20260617.json"),
        )

        self.assertIn("AI-HR 준비도", html)
        self.assertIn("Release Decision Contract", html)
        self.assertIn("aihr_release_readiness_v1", html)
        self.assertIn("report_generated_and_contract_checks_evaluated", html)
        self.assertIn("blocked_until_requirements_met", html)
        self.assertIn("approval_claim", html)
        self.assertIn("human_decision_required_for_release_claim", html)
        self.assertIn("trusted_transition_scenarios", html)
        self.assertIn("reports/demo.json", html)
        self.assertIn("demo_contract", html)
        self.assertIn("MCP Contract Checks", html)
        self.assertIn("Query router present", html)
        self.assertIn("Next Actions", html)
        self.assertIn("evaluation-agent", html)
        self.assertIn("export-transition-scenario-seedpack", html)
        self.assertIn("dashboard_surface", html)
        self.assertIn("review_chain_safety", html)
        self.assertIn("AI-HR Review Chain Safety", html)
        self.assertIn("Source Payload Exposed", html)
        self.assertIn("Learning Module Items", html)
        self.assertIn("NCS Report Items", html)
        self.assertIn("auto_approve", html)
        self.assertIn("write_human_reviewed_accepted_or_reviewed", html)
        self.assertIn("Sensitive Markers", html)
        self.assertIn("C1-2 Review Required", html)
        self.assertIn("C2-2 Pending", html)
        self.assertIn("C2-2 Approval Safe", html)
        self.assertIn("[]", html)
        self.assertIn("AI-HR Dashboard Static Artifacts", html)
        self.assertIn("Checkpoint OK", html)
        self.assertIn("ncs006_element_api_checkpoint_v1", html)
        self.assertIn("matched=11537/47620", html)
        self.assertIn("reports/dashboard.json", html)
        self.assertIn("AI-HR Review Triage", html)
        self.assertIn("transition_attention", html)
        self.assertIn("transition_seedpack_items", html)
        self.assertIn("trusted_in_seedpack", html)
        self.assertIn("transition-seedpack-test", html)
        self.assertIn("reports/transition_scenario_seedpack.jsonl", html)
        self.assertIn("hr_training_goal_link_human_review_required", html)

    def test_public_aihr_dashboard_payload_sanitizes_local_paths(self) -> None:
        payload = {
            "workspace": "C:/workspace/NCS_MCP",
            "dashboard_surface_contract": {
                "artifact": {
                    "path": "C:/workspace/NCS_MCP/reports/aihr_dashboard.json",
                    "static_artifacts": [
                        {
                            "name": "demo_json",
                            "path": "C:/workspace/NCS_MCP/reports/demo.json",
                        },
                        {
                            "name": "local_db",
                            "path": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                        },
                    ],
                }
            },
            "summary": {
                "source_paths": {
                    "transition_seedpack": "C:/workspace/NCS_MCP/reports/seedpack.jsonl",
                    "database": "data/processed/ncs.db",
                    "external": "D:/operator/private/input.json",
                }
            },
        }

        public_payload = public_aihr_dashboard_payload(payload)
        public_text = json.dumps(public_payload, ensure_ascii=False)

        self.assertNotIn("workspace", public_payload)
        self.assertEqual(public_payload["workspace_ref"], "configured_workspace")
        self.assertEqual(
            public_payload["dashboard_surface_contract"]["artifact"]["path"],
            "reports/aihr_dashboard.json",
        )
        self.assertEqual(
            public_payload["dashboard_surface_contract"]["artifact"]["static_artifacts"][0]["path"],
            "reports/demo.json",
        )
        self.assertEqual(
            public_payload["dashboard_surface_contract"]["artifact"]["static_artifacts"][1]["path"],
            "configured_ncs_database",
        )
        self.assertEqual(
            public_payload["summary"]["source_paths"]["transition_seedpack"],
            "reports/seedpack.jsonl",
        )
        self.assertEqual(
            public_payload["summary"]["source_paths"]["database"],
            "configured_ncs_database",
        )
        self.assertEqual(public_payload["summary"]["source_paths"]["external"], "input.json")
        self.assertNotIn("C:/workspace", public_text)
        self.assertNotIn("data/processed/ncs.db", public_text)

    def test_render_aihr_readiness_html_sanitizes_static_and_triage_paths(self) -> None:
        html = render_aihr_readiness_html(
            {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "engineering_hygiene_ok": True,
                "blockers": [],
                "warnings": [],
                "demo_contract": {
                    "ok": True,
                    "json_artifacts": [
                        {
                            "path": "C:/workspace/NCS_MCP/reports/demo.json",
                            "ok": True,
                            "view": "ncs_education_plan",
                            "matrix_rows": 1,
                        }
                    ],
                    "html_artifact": {
                        "path": "C:/workspace/NCS_MCP/reports/demo.html",
                        "ok": True,
                        "length": 100,
                    },
                },
                "dashboard_surface_contract": {
                    "ok": True,
                    "artifact": {
                        "path": "C:/workspace/NCS_MCP/reports/dashboard.json",
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": [
                            {
                                "name": "demo_json",
                                "path": "C:/workspace/NCS_MCP/reports/demo.json",
                                "exists": True,
                                "non_empty": True,
                                "size_bytes": 512,
                            },
                            {
                                "name": "local_db_marker",
                                "path": "data/processed/ncs.db",
                                "exists": True,
                                "non_empty": True,
                                "size_bytes": 12,
                            },
                        ],
                        "live_plan_summaries": [],
                    },
                },
            },
            Path("C:/workspace/NCS_MCP/reports/aihr_release_readiness_20260617.json"),
            triage_payload={
                "summary": {
                    "source_paths": {
                        "transition_seedpack": "C:/workspace/NCS_MCP/reports/seedpack.jsonl",
                        "database": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                    }
                }
            },
            triage_path=Path("C:/workspace/NCS_MCP/reports/aihr_review_triage_20260617.json"),
        )

        self.assertIn("reports/demo.json", html)
        self.assertIn("reports/seedpack.jsonl", html)
        self.assertIn("reports/aihr_review_triage_20260617.json", html)
        self.assertIn("configured_ncs_database", html)
        self.assertNotIn("C:/workspace", html)
        self.assertNotIn("C:\\workspace", html)
        self.assertNotIn("data/processed/ncs.db", html)

    def test_render_aihr_review_board_html_sanitizes_source_paths(self) -> None:
        html = render_aihr_review_board_html(
            {
                "schema": "ncs_review_triage_v1",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "summary": {
                    "quality_warning_count": 0,
                    "review_priority_item_count": 0,
                    "transition_seedpack_item_count": 0,
                    "transition_attention_count": 0,
                    "transition_trust_review_candidate_count": 0,
                    "transition_status_snapshot": {
                        "trusted_review_status_count": 0,
                        "actual_review_status_counts": {},
                        "requested_review_statuses": [],
                        "missing_requested_review_statuses": [],
                    },
                    "review_issue_type_counts": {},
                    "source_paths": {
                        "transition_seedpack": "C:/workspace/NCS_MCP/reports/seedpack.jsonl",
                        "database": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                        "external": "D:/operator/private/source.json",
                    },
                },
                "quality_warnings": [],
                "transition_review_priorities": [],
                "transition_trust_review_candidates": [],
                "review_priority_items": [],
                "focus_review_priority_overlays": [],
                "cross_checks": [],
                "operator_constraints": [],
            },
            Path("C:/workspace/NCS_MCP/reports/aihr_review_triage_20260617.json"),
        )

        self.assertIn("reports/seedpack.jsonl", html)
        self.assertIn("configured_ncs_database", html)
        self.assertIn("source.json", html)
        self.assertNotIn("C:/workspace", html)
        self.assertNotIn("C:\\workspace", html)
        self.assertNotIn("D:/operator", html)
        self.assertNotIn("data/processed/ncs.db", html)

    def test_render_aihr_agent_queue_html_shows_commands_and_guardrails(self) -> None:
        html = render_aihr_agent_queue_html(
            {
                "schema": "aihr_agent_work_queue_v1",
                "release_ready": False,
                "engineering_hygiene_ok": True,
                "item_count": 1,
                "global_guardrails": ["Do not auto-approve human review."],
                "items": [
                    {
                        "priority": 3,
                        "owner": "ontology-review-agent",
                        "agent_file": ".agents/ontology-review-agent.md",
                        "blocker": "review_debt:human_reviewed_concepts",
                        "blocker_category": "human_review",
                        "mutation_policy": "regenerate_reports_only",
                        "auto_runnable": True,
                        "requires_human_decision": True,
                        "action": "Prepare review artifacts.",
                        "command": (
                            "python scripts\\ncs_harness.py review-priority "
                            "--out C:/workspace/NCS_MCP/reports/aihr_review_priority_20260617.json "
                            "--db C:/workspace/NCS_MCP/data/processed/ncs.db"
                        ),
                        "prerequisite_artifacts": ["reports/aihr_quality_gates_with_transition_20260617.json"],
                        "prerequisite_commands": [
                            (
                                "python scripts\\ncs_harness.py export-transition-scenario-seedpack "
                                "--source C:/workspace/NCS_MCP/reports/input.json "
                                "--db data/processed/ncs.db --scenario-limit 20"
                            )
                        ],
                        "expected_artifacts": [
                            "reports/aihr_review_priority_20260617.json",
                            "C:/workspace/NCS_MCP/data/processed/ncs.db",
                        ],
                        "acceptance_checks": ["No status mutation."],
                    }
                ],
            },
            Path("reports/aihr_agent_work_queue_20260617.json"),
        )

        self.assertIn("AI-HR Agent Work Queue", html)
        self.assertIn("ontology-review-agent", html)
        self.assertIn("review-priority", html)
        self.assertIn("Prerequisites", html)
        self.assertIn("Prerequisite Commands", html)
        self.assertIn("aihr_quality_gates_with_transition_20260617.json", html)
        self.assertIn("export-transition-scenario-seedpack", html)
        self.assertIn("reports/aihr_review_priority_20260617.json", html)
        self.assertIn("reports/input.json", html)
        self.assertIn("No status mutation.", html)
        self.assertIn("Do not auto-approve human review.", html)
        self.assertIn("configured_ncs_database", html)
        self.assertNotIn("C:/workspace/NCS_MCP", html)
        self.assertNotIn("C:\\workspace\\NCS_MCP", html)
        self.assertNotIn("data/processed/ncs.db", html)

    def test_render_aihr_agent_queue_status_html_shows_preflight_states(self) -> None:
        item = {
            "id": "aihr-01",
            "priority": 3,
            "owner": "ontology-review-agent",
            "agent_file": ".agents/ontology-review-agent.md",
            "covered_blockers": ["review_debt:human_reviewed_concepts"],
            "mutation_policy": "regenerate_reports_only",
            "requires_human_decision": False,
            "command": (
                "python scripts\\ncs_harness.py review-priority "
                "--out C:/workspace/NCS_MCP/reports/aihr_review_priority_20260617.json "
                "--db C:/workspace/NCS_MCP/data/processed/ncs.db"
            ),
            "state": "ready_to_start",
            "preflight_ok": True,
            "can_start_automated": True,
            "prerequisite_artifacts": ["reports/aihr_transition_scenario_seedpack_20260617.jsonl"],
            "missing_prerequisite_artifacts": [],
            "existing_expected_artifacts": ["reports/aihr_review_priority_20260617.json"],
            "missing_expected_artifacts": ["data/processed/ncs.db"],
            "safety_violations": [],
            "acceptance_checks": ["No source mutation."],
        }
        manual_item = dict(item, id="aihr-02", state="manual_ready", can_start_automated=False)
        blocked_item = dict(
            item,
            id="aihr-03",
            state="blocked_safety",
            can_start_automated=False,
            safety_violations=["missing_guard_flag:--request-delay"],
        )
        html = render_aihr_agent_queue_status_html(
            {
                "ok": False,
                "schema": "aihr_agent_queue_status_v1",
                "source_queue_path": "reports/aihr_agent_work_queue_20260617.json",
                "summary": {
                    "item_count": 3,
                    "auto_startable_count": 1,
                    "manual_ready_count": 1,
                    "manual_human_decision_count": 1,
                    "guarded_manual_count": 1,
                    "blocked_count": 1,
                    "state_counts": {"ready_to_start": 1, "manual_ready": 1, "blocked_safety": 1},
                },
                "execution_order": [
                    {
                        "priority": 3,
                        "owner": "ontology-review-agent",
                        "mutation_policy": "regenerate_reports_only",
                        "requires_human_decision": False,
                        "command": (
                            "python scripts\\ncs_harness.py review-priority "
                            "--out C:/workspace/NCS_MCP/reports/aihr_review_priority_20260617.json "
                            "--db data/processed/ncs.db"
                        ),
                    }
                ],
                "manual_queue": [manual_item],
                "blocked_queue": [blocked_item],
                "items": [item, manual_item, blocked_item],
                "global_guardrails": ["Do not auto-approve human review."],
            },
            Path("reports/aihr_agent_queue_status_20260617.json"),
        )

        self.assertIn("AI-HR Agent Queue Status", html)
        self.assertIn("Automated Start Order", html)
        self.assertIn("manual_ready=1", html)
        self.assertIn("manual_human_decision", html)
        self.assertIn("guarded_manual", html)
        self.assertIn("blocked_safety=1", html)
        self.assertIn("Prereqs", html)
        self.assertIn("required: reports/aihr_transition_scenario_seedpack_20260617.jsonl", html)
        self.assertIn("review-priority", html)
        self.assertIn("reports/aihr_review_priority_20260617.json", html)
        self.assertIn("missing_guard_flag:--request-delay", html)
        self.assertIn("No source mutation.", html)
        self.assertIn("Do not auto-approve human review.", html)
        self.assertIn("configured_ncs_database", html)
        self.assertNotIn("C:/workspace/NCS_MCP", html)
        self.assertNotIn("C:\\workspace\\NCS_MCP", html)
        self.assertNotIn("data/processed/ncs.db", html)

    def test_render_aihr_agent_queue_run_html_shows_execution_evidence(self) -> None:
        html = render_aihr_agent_queue_run_html(
            {
                "ok": True,
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": "reports/aihr_agent_work_queue_20260617.json",
                "summary": {
                    "selected_count": 1,
                    "succeeded_count": 1,
                    "failed_count": 0,
                    "skipped_unsafe_count": 0,
                },
                "queue_status_summary": {"auto_startable_count": 1},
                "runs": [
                    {
                        "order": 1,
                        "id": "aihr-01",
                        "status": "succeeded",
                        "exit_code": 0,
                        "owner": "ontology-review-agent",
                        "mutation_policy": "regenerate_reports_only",
                        "started_at": "2026-06-17T00:00:00+00:00",
                        "duration_seconds": 1.2,
                        "command": "python scripts\\ncs_harness.py review-priority",
                        "validation_errors": [],
                        "stdout_tail": '{"ok": true}',
                        "stdout_original_chars": 40,
                        "stdout_tail_chars": 12,
                        "stdout_truncated": True,
                        "stdout_redacted": True,
                        "stdout_redaction_count": 2,
                        "stderr_tail": "",
                        "stderr_original_chars": 0,
                        "stderr_tail_chars": 0,
                        "stderr_truncated": False,
                        "stderr_redacted": False,
                        "stderr_redaction_count": 0,
                    }
                ],
            },
            Path("reports/aihr_agent_queue_run_20260617.json"),
        )

        self.assertIn("AI-HR Agent Queue Run", html)
        self.assertIn("Execution evidence", html)
        self.assertIn("ncs_harness:review-priority", html)
        self.assertNotIn("python scripts\\ncs_harness.py review-priority", html)
        self.assertIn("succeeded", html)
        self.assertIn("truncated 12/40 chars", html)
        self.assertIn("redacted=True", html)
        self.assertIn("redactions=2", html)
        self.assertIn("complete 0/0 chars", html)
        self.assertNotIn('{"ok": true}', html)

    def test_sanitize_aihr_agent_queue_run_payload_suppresses_tail_bodies(self) -> None:
        payload = {
            "ok": True,
            "schema": "aihr_agent_queue_run_v1",
            "workspace": "C:/workspace/NCS_MCP",
            "source_queue_path": "C:/workspace/NCS_MCP/reports/aihr_agent_queue_20260624.json",
            "summary": {"dry_run": False, "succeeded_count": 1},
            "runs": [
                {
                    "id": "aihr-01",
                    "status": "succeeded",
                    "command": "python scripts\\ncs_harness.py review-priority --out reports\\x.json",
                    "checkpoint_path": "C:/workspace/NCS_MCP/reports/checkpoint.json",
                    "stdout_tail": "secret-ish debug body",
                    "stdout_original_chars": 22,
                    "stdout_tail_chars": 22,
                    "stdout_truncated": False,
                    "stderr_tail": "error body",
                    "stderr_original_chars": 10,
                    "stderr_tail_chars": 10,
                    "stderr_truncated": False,
                }
            ],
        }

        sanitized = sanitize_aihr_agent_queue_run_payload(payload)

        self.assertTrue(sanitized["output_tails_suppressed"])
        self.assertNotIn("workspace", sanitized)
        self.assertEqual(sanitized["workspace_ref"], "configured_workspace")
        self.assertEqual(sanitized["source_queue_path"], "reports/aihr_agent_queue_20260624.json")
        self.assertEqual(sanitized["runs"][0]["checkpoint_path"], "reports/checkpoint.json")
        self.assertNotIn("stdout_tail", sanitized["runs"][0])
        self.assertNotIn("stderr_tail", sanitized["runs"][0])
        self.assertNotIn("command", sanitized["runs"][0])
        self.assertEqual(sanitized["runs"][0]["command_label"], "ncs_harness:review-priority")
        self.assertTrue(sanitized["runs"][0]["stdout_tail_suppressed"])
        self.assertTrue(sanitized["runs"][0]["stderr_tail_suppressed"])
        self.assertEqual(payload["runs"][0]["stdout_tail"], "secret-ish debug body")

    def test_sanitize_aihr_agent_queue_public_paths_strips_workspace_and_absolute_paths(self) -> None:
        payload = {
            "schema": "aihr_agent_queue_status_v1",
            "workspace": "C:/workspace/NCS_MCP",
            "source_queue_path": "C:/workspace/NCS_MCP/reports/queue.json",
            "items": [
                {
                    "command": (
                        "python scripts\\ncs_harness.py review-priority "
                        "--out C:/workspace/NCS_MCP/reports/out.json "
                        "--db data/processed/ncs.db"
                    ),
                    "prerequisite_commands": [
                        "python scripts\\ncs_harness.py review-priority --out D:/private/out.json"
                    ],
                    "operational_guard": {
                        "checkpoint_path": "C:/workspace/NCS_MCP/reports/checkpoint.json"
                    }
                },
                {"source_path": "D:/external/queue.json"},
            ],
        }

        sanitized = sanitize_aihr_agent_queue_public_paths(payload)

        self.assertIs(sanitized, payload)
        self.assertNotIn("workspace", sanitized)
        self.assertEqual(sanitized["workspace_ref"], "configured_workspace")
        self.assertEqual(sanitized["source_queue_path"], "reports/queue.json")
        self.assertEqual(
            sanitized["items"][0]["command"],
            "python scripts\\ncs_harness.py review-priority --out reports/out.json --db configured_ncs_database",
        )
        self.assertEqual(
            sanitized["items"][0]["prerequisite_commands"][0],
            "python scripts\\ncs_harness.py review-priority --out out.json",
        )
        self.assertEqual(
            sanitized["items"][0]["operational_guard"]["checkpoint_path"],
            "reports/checkpoint.json",
        )
        self.assertEqual(sanitized["items"][1]["source_path"], "queue.json")
        self.assertNotIn("C:/workspace", json.dumps(sanitized, ensure_ascii=False))
        self.assertNotIn("data/processed/ncs.db", json.dumps(sanitized, ensure_ascii=False))

    def test_is_actual_aihr_agent_queue_run_requires_executed_status(self) -> None:
        base_payload = {
            "ok": True,
            "schema": "aihr_agent_queue_run_v1",
            "summary": {"dry_run": False, "dry_run_count": 0, "selected_count": 1},
        }

        self.assertFalse(
            is_actual_aihr_agent_queue_run(
                base_payload | {"schema": "unexpected_schema", "runs": [{"status": "succeeded"}]}
            )
        )
        self.assertFalse(is_actual_aihr_agent_queue_run(base_payload | {"runs": []}))
        self.assertFalse(
            is_actual_aihr_agent_queue_run(base_payload | {"runs": [{"status": "skipped_unsafe"}]})
        )
        self.assertTrue(
            is_actual_aihr_agent_queue_run(
                base_payload
                | {
                    "runs": [
                        {
                            "status": "succeeded",
                            "stdout_original_chars": 0,
                            "stdout_tail_chars": 0,
                            "stdout_truncated": False,
                            "stderr_original_chars": 0,
                            "stderr_tail_chars": 0,
                            "stderr_truncated": False,
                        }
                    ]
                }
            )
        )

    def test_aihr_agent_queue_run_artifact_issues_detect_source_queue_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text('{"schema":"aihr_agent_work_queue_v1","items":[]}\n', encoding="utf-8")
            original_hash = "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()
            queue_path.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[{"id":"changed"}]}\n',
                encoding="utf-8",
            )
            status_hash = canonical_json_sha256(
                build_agent_queue_status_from_file(queue_path, workspace=ROOT)
            )
            run_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            payload = {
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": str(queue_path),
                "source_queue_sha256": original_hash,
                "queue_status_snapshot_sha256": status_hash,
                "summary": {"dry_run": False, "dry_run_count": 0},
                "runs": [{"status": "succeeded"}],
            }

            issues = aihr_agent_queue_run_artifact_issues(payload, run_path)

        self.assertIn("source_queue_hash_mismatch", issues)

    def test_aihr_agent_queue_run_artifact_issues_detect_missing_lineage_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text('{"schema":"aihr_agent_work_queue_v1","items":[]}\n', encoding="utf-8")
            run_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            payload = {
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": str(queue_path),
                "summary": {"dry_run": False, "dry_run_count": 0},
                "runs": [{"status": "succeeded"}],
            }

            issues = aihr_agent_queue_run_artifact_issues(payload, run_path)

        self.assertIn("source_queue_sha256_missing", issues)
        self.assertIn("queue_status_snapshot_sha256_missing", issues)

    def test_aihr_agent_queue_run_artifact_issues_detect_invalid_lineage_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text('{"schema":"aihr_agent_work_queue_v1","items":[]}\n', encoding="utf-8")
            run_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            payload = {
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": str(queue_path),
                "source_queue_sha256": "not-a-sha",
                "queue_status_snapshot_sha256": "sha256:nothex",
                "summary": {"dry_run": False, "dry_run_count": 0},
                "runs": [{"status": "succeeded"}],
            }

            issues = aihr_agent_queue_run_artifact_issues(payload, run_path)

        self.assertIn("source_queue_sha256_invalid", issues)
        self.assertIn("queue_status_snapshot_sha256_invalid", issues)

    def test_aihr_agent_queue_run_artifact_issues_detect_status_snapshot_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[]}\n',
                encoding="utf-8",
            )
            run_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            payload = {
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": str(queue_path),
                "source_queue_sha256": (
                    "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()
                ),
                "queue_status_snapshot_sha256": (
                    "sha256:"
                    "2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "summary": {"dry_run": False, "dry_run_count": 0},
                "runs": [{"status": "succeeded"}],
            }

            issues = aihr_agent_queue_run_artifact_issues(payload, run_path)

        self.assertIn("queue_status_snapshot_sha256_mismatch", issues)

    def test_aihr_agent_queue_run_artifact_issues_detect_stale_public_private_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            public_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            private_path = tmp_path / "aihr_agent_queue_run_20260624.json"
            public_path.write_text("{}", encoding="utf-8")
            private_path.write_text("{}", encoding="utf-8")
            os.utime(public_path, (1704067200, 1704067200))
            os.utime(private_path, (1704067300, 1704067300))

            issues = aihr_agent_queue_run_artifact_issues({}, public_path)

        self.assertIn("public_stale_private_newer", issues)

    def test_render_aihr_live_html_exposes_form_and_api(self) -> None:
        html = render_aihr_live_html()

        self.assertIn("AI-HR Live Planner", html)
        self.assertIn("currentQuery", html)
        self.assertIn("targetQuery", html)
        self.assertIn("preferredFacilities", html)
        self.assertIn("/api/aihr-plan", html)
        self.assertIn("Recommended Path", html)
        self.assertIn("Training-System Matrix", html)
        self.assertIn("2026 Guide Trace", html)
        self.assertIn("Route Evidence", html)
        self.assertIn("route_fingerprint", html)
        self.assertIn("mapping_strength_warning", html)
        self.assertIn("Mapping strength", html)
        self.assertIn("evidence_chain", html)
        self.assertIn("Course Intake Requirements", html)
        self.assertIn("course_intake_requirements", html)
        self.assertIn("aihr_course_intake_requirements_v1", html)
        self.assertIn("Training Course Inventory Template", html)
        self.assertIn("training_course_inventory_template", html)
        self.assertIn("aihr_training_course_inventory_template_v1", html)
        self.assertIn("Training Necessity Review", html)
        self.assertIn("training_necessity_review", html)
        self.assertIn("aihr_training_necessity_review_v1", html)
        self.assertIn("Annual Operation Plan Seed", html)
        self.assertIn("annual_operation_plan", html)
        self.assertIn("aihr_annual_operation_plan_seed_v1", html)
        self.assertIn("decision_state", html)
        self.assertIn("pending_human_decision", html)
        self.assertIn("2026 NCS", html)
        self.assertIn("applyInitialPlannerQueryParams", html)
        self.assertIn("new URLSearchParams(window.location.search)", html)
        self.assertIn("['currentQuery', ['current_query', 'currentQuery']]", html)
        self.assertIn("['targetQuery', ['target_query', 'targetQuery']]", html)
        self.assertIn("['preferredMethods', ['preferred_methods', 'preferredMethods']]", html)
        self.assertIn("['preferredMaxHours', ['preferred_max_hours', 'preferredMaxHours']]", html)
        self.assertLess(
            html.rindex("function applyInitialPlannerQueryParams()"),
            html.rindex("applyInitialPlannerQueryParams();"),
        )
        self.assertLess(
            html.rindex("applyInitialPlannerQueryParams();"),
            html.rindex("</script>"),
        )

    def test_render_aihr_training_system_builder_html_exposes_guide_workflow(self) -> None:
        html = render_aihr_training_system_builder_html()

        self.assertIn("AI-HR Training System Builder", html)
        self.assertIn("2026 Guide Workflow", html)
        self.assertIn("Job Scope", html)
        self.assertIn("Task and KSA", html)
        self.assertIn("Course Map", html)
        self.assertIn("Course Intake Requirements", html)
        self.assertIn("course_intake_requirements", html)
        self.assertIn("aihr_course_intake_requirements_v1", html)
        self.assertIn("Training Course Inventory Template", html)
        self.assertIn("training_course_inventory_template", html)
        self.assertIn("aihr_training_course_inventory_template_v1", html)
        self.assertIn("Training Necessity Review", html)
        self.assertIn("training_necessity_review", html)
        self.assertIn("aihr_training_necessity_review_v1", html)
        self.assertIn("Required / Optional", html)
        self.assertIn("Level / Delivery", html)
        self.assertIn("Human Review", html)
        self.assertIn("facility_constraint_fit", html)
        self.assertIn("mapping_strength_warning", html)
        self.assertIn("Evidence Chain", html)
        self.assertIn("evidence_chain", html)
        self.assertIn("Decision State", html)
        self.assertIn("decision_state", html)
        self.assertIn("Annual Operation Plan", html)
        self.assertIn("annual_operation_plan", html)
        self.assertIn("aihr_annual_operation_plan_seed_v1", html)
        self.assertIn("/api/aihr-plan", html)

    def test_build_aihr_live_plan_requires_queries(self) -> None:
        result = build_aihr_live_plan(Path("missing.db"), {"current_query": "노무관리"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "missing_required_query")

    def test_build_aihr_live_plan_uses_no_save_and_public_payload(self) -> None:
        fake_conn = MagicMock()
        transition = {"ok": True, "view": "compact_training_transition"}
        compact_plan = {
            "ok": True,
            "view": "ncs_education_plan",
            "training_system_guide_trace": {"schema": "aihr_training_system_guide_trace_v1", "checks": []},
            "training_system_matrix": [],
            "audit": {"sqf_used": False, "learning_modules_used": False},
            "source_payload": {"secret": "hidden"},
        }
        with patch("ncs_dashboard.connect_db_readonly", return_value=fake_conn) as connect_mock, patch(
            "ncs_dashboard.recommend_training_transition",
            return_value=transition,
        ) as recommend_mock, patch(
            "ncs_dashboard.compact_ncs_education_plan_response",
            return_value=compact_plan,
        ) as compact_mock:
            result = build_aihr_live_plan(
                Path("test.db"),
                {
                    "current_query": "노무관리",
                    "target_query": "인사기획",
                    "target_population": "인사담당자",
                    "scenario": "직무전환",
                    "preferred_methods": "집체훈련, 온라인",
                    "preferred_facilities": "HRD 실습실, 강의실",
                    "preferred_max_hours": "24",
                    "limit": "3",
                },
            )

        connect_mock.assert_called_once_with(Path("test.db"))
        fake_conn.close.assert_called_once()
        recommend_kwargs = recommend_mock.call_args.kwargs
        self.assertFalse(recommend_kwargs["save"])
        self.assertEqual(recommend_kwargs["preferred_methods"], ["집체훈련", "온라인"])
        self.assertEqual(recommend_kwargs["preferred_facilities"], ["HRD 실습실", "강의실"])
        self.assertEqual(recommend_kwargs["limit"], 3)
        compact_mock.assert_called_once()
        self.assertEqual(result["live_runner_schema"], "aihr_live_plan_v1")
        self.assertEqual(result["run_mode"], "live_no_save")
        self.assertEqual(result["public_demo_schema"], "aihr_public_demo_v1")
        self.assertEqual(result["training_system_guide_trace"]["schema"], "aihr_training_system_guide_trace_v1")
        self.assertEqual(result["query_route"]["schema"], "ncs_query_route_v1")
        self.assertEqual(result["query_route"]["tool"], "plan_ncs_education_path")
        self.assertIn("recommend_training_transition", result["query_route"]["expected_tool_chain"])
        self.assertEqual(result["route_contract_schema"], "ncs_query_route_v1")
        self.assertTrue(result["route_fingerprint"])
        self.assertEqual(result["requested_input"]["preferred_facilities"], ["HRD 실습실", "강의실"])
        self.assertNotIn("source_payload", result)

    def test_build_aihr_live_plan_fails_when_route_contract_missing(self) -> None:
        fake_conn = MagicMock()
        transition = {"ok": True, "view": "compact_training_transition"}
        compact_plan = {
            "ok": True,
            "view": "ncs_education_plan",
            "training_system_guide_trace": {
                "schema": "aihr_training_system_guide_trace_v1",
                "checks": [],
            },
            "training_system_matrix": [{"course": "partial"}],
            "recommended_path": {"core_gap_training": ["partial"]},
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }
        with patch("ncs_dashboard.connect_db_readonly", return_value=fake_conn), patch(
            "ncs_dashboard.recommend_training_transition",
            return_value=transition,
        ), patch(
            "ncs_dashboard.compact_ncs_education_plan_response",
            return_value=compact_plan,
        ), patch(
            "ncs_dashboard._aihr_live_route_evidence",
            return_value={"tool": "plan_ncs_education_path"},
        ):
            result = build_aihr_live_plan(
                Path("test.db"),
                {
                    "current_query": "노무관리",
                    "target_query": "인사기획",
                },
            )

        fake_conn.close.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["route_contract_schema"])
        self.assertEqual(result["error"]["code"], "missing_query_route_contract")
        self.assertIn("query_route.schema", result["missing_query_route_fields"])
        self.assertIn("query_route.scenario:None", result["missing_query_route_fields"])
        self.assertIn("query_route.available:None", result["missing_query_route_fields"])
        self.assertIn("query_route.route_contract", result["missing_query_route_fields"])
        self.assertNotIn("view", result)
        self.assertNotIn("training_system_matrix", result)
        self.assertNotIn("recommended_path", result)
        self.assertNotIn("training_system_guide_trace", result)
        self.assertNotIn("source_payload", result)

    def test_build_aihr_live_plan_uses_readonly_connection_and_does_not_mutate_recommendation_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "ncs.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE education_recommendation_runs(id INTEGER)")
            conn.commit()
            conn.close()
            transition = {"ok": True, "view": "compact_training_transition"}
            compact_plan = {
                "ok": True,
                "view": "ncs_education_plan",
                "training_system_guide_trace": {
                    "schema": "aihr_training_system_guide_trace_v1",
                    "checks": [],
                },
                "training_system_matrix": [],
                "audit": {"sqf_used": False, "learning_modules_used": False},
            }

            def fake_recommend(conn, **kwargs):
                self.assertFalse(kwargs["save"])
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO education_recommendation_runs(id) VALUES (1)")
                return transition

            with patch(
                "ncs_dashboard.recommend_training_transition",
                side_effect=fake_recommend,
            ), patch(
                "ncs_dashboard.compact_ncs_education_plan_response",
                return_value=compact_plan,
            ):
                result = build_aihr_live_plan(
                    db_path,
                    {
                        "current_query": "노무관리",
                        "target_query": "인사기획",
                    },
                )

            verify_conn = sqlite3.connect(db_path)
            try:
                count = verify_conn.execute(
                    "SELECT COUNT(*) FROM education_recommendation_runs"
                ).fetchone()[0]
            finally:
                verify_conn.close()
            self.assertTrue(result["ok"])
            self.assertEqual(result["run_mode"], "live_no_save")
            self.assertEqual(count, 0)

    def test_build_aihr_live_plan_does_not_create_missing_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_db = Path(temp_dir) / "missing.db"
            result = build_aihr_live_plan(
                missing_db,
                {
                    "current_query": "labor management",
                    "target_query": "HR planning",
                },
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "database_unavailable")
            self.assertEqual(result["live_runner_schema"], "aihr_live_plan_v1")
            self.assertFalse(missing_db.exists())

    def test_render_aihr_review_board_html_shows_review_priorities(self) -> None:
        html = render_aihr_review_board_html(
            {
                "schema": "ncs_review_triage_v1",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "summary": {
                    "quality_warning_count": 5,
                    "review_priority_item_count": 20,
                    "review_issue_type_counts": {
                        "hr_training_goal_link_human_review_required": 3,
                        "ontology_task_ksa_relation_human_review_required": 2,
                    },
                    "transition_seedpack_item_count": 10,
                    "transition_attention_count": 8,
                    "transition_trust_review_candidate_count": 1,
                    "transition_seedpack_id": "transition-scenario-seedpack-test",
                    "transition_status_snapshot": {
                        "requested_review_statuses": ["candidate", "candidate_auto"],
                        "actual_review_status_counts": {"candidate": 7, "candidate_auto": 1},
                        "missing_requested_review_statuses": [],
                        "trusted_review_status_count": 0,
                    },
                    "source_paths": {
                        "quality_report": "reports/quality_gates_with_transition.json",
                        "transition_seedpack": "reports/transition_scenario_seedpack.jsonl",
                    },
                },
                "operator_constraints": ["Do not auto-promote."],
                "quality_warnings": [
                    {
                        "category": "human_review",
                        "name": "review_debt:human_reviewed_concepts",
                        "message": "zero",
                        "value": 0,
                        "action": "review",
                    }
                ],
                "transition_review_priorities": [
                    {
                        "rank": 1,
                        "scenario_name": "labor_management_to_hr_planning",
                        "review_status": "candidate",
                        "current_query": "노무관리",
                        "target_query": "인사기획",
                        "expected_recall_at_k": 0.25,
                        "precision_at_k": 0.2,
                        "course_scope_fit_relation_counts": {"same_middle_classification": 1},
                        "course_scope_review_required_count": 1,
                        "flags": ["low_expected_recall"],
                    }
                ],
                "transition_trust_review_candidates": [
                    {
                        "rank": 1,
                        "scenario_name": "trust_candidate_one",
                        "review_status": "candidate",
                        "candidate_score": 82.5,
                        "review_readiness": "needs_careful_review",
                        "expected_recall_at_k": 0.75,
                        "precision_at_k": 0.6,
                        "top1_expected_hit": True,
                        "direct_or_near_course_ratio": 0.8,
                        "course_scope_review_required_count": 0,
                        "decision_policy": "Report-only candidate; do not promote without human review.",
                    }
                ],
                "review_priority_items": [
                    {
                        "rank": 1,
                        "issue_type": "hr_training_goal_link_human_review_required",
                        "target_type": "training_goal_concept_link",
                        "target_id": "1",
                        "severity": "high",
                        "priority_score": 100,
                        "priority_reason": "Training-goal concept links directly affect recommendation ranking.",
                        "context_excerpt": "Course goal | KSA concept | direct text evidence",
                        "suggested_action": "Confirm link.",
                    }
                ],
                "focus_review_priority_overlays": [
                    {
                        "code": "aihr_demo_major_02",
                        "label": "AI-HR demo focus",
                        "major_code": "02",
                        "reason": "current demo",
                        "item_count": 1,
                        "items": [
                            {
                                "issue_type": "hr_training_goal_link_human_review_required",
                                "context_excerpt": "HR planning | Course goal",
                            }
                        ],
                    }
                ],
                "cross_checks": [
                    {
                        "name": "trusted_transition_scenarios",
                        "status": "warn",
                        "value": 1,
                        "threshold": ">= 10",
                        "message": "Trusted transition scenarios are below the release-readiness target.",
                    }
                ],
            },
            Path("reports/aihr_review_triage_20260617.json"),
        )

        self.assertIn("AI-HR 검토보드", html)
        self.assertIn("transition attention", html)
        self.assertIn("transition-scenario-seedpack-test", html)
        self.assertIn("Transition Review Batch", html)
        self.assertIn("Safety Contract", html)
        self.assertIn("report_only", html)
        self.assertIn("status_update_allowed", html)
        self.assertIn("db_writes", html)
        self.assertIn("approval_claim", html)
        self.assertIn("safety_issues", html)
        self.assertIn("none", html)
        self.assertIn("candidate_auto", html)
        self.assertIn("Source Artifacts", html)
        self.assertIn("reports/transition_scenario_seedpack.jsonl", html)
        self.assertIn("Review Issue Type Counts", html)
        self.assertIn("ontology_task_ksa_relation_human_review_required", html)
        self.assertIn("Training-goal concept links directly affect recommendation ranking.", html)
        self.assertIn("Course goal | KSA concept | direct text evidence", html)
        self.assertIn("labor_management_to_hr_planning", html)
        self.assertIn("Course Scope Fit", html)
        self.assertIn("same_middle_classification", html)
        self.assertIn("trust review candidates", html)
        self.assertIn("Transition Trust Review Candidates", html)
        self.assertIn("trust_candidate_one", html)
        self.assertIn("needs_careful_review", html)
        self.assertIn("Report-only candidate", html)
        self.assertIn("hr_training_goal_link_human_review_required", html)
        self.assertIn("Focus Review Priority Overlays", html)
        self.assertIn("AI-HR demo focus", html)
        self.assertIn("Cross Checks", html)
        self.assertIn("trusted_transition_scenarios", html)
        self.assertIn("&gt;= 10", html)
        self.assertIn("Do not auto-promote.", html)

    def test_render_aihr_review_board_html_sanitizes_source_paths(self) -> None:
        html = render_aihr_review_board_html(
            {
                "schema": "ncs_review_triage_v1",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "summary": {
                    "quality_warning_count": 0,
                    "review_priority_item_count": 0,
                    "transition_seedpack_item_count": 0,
                    "transition_attention_count": 0,
                    "transition_trust_review_candidate_count": 0,
                    "transition_status_snapshot": {
                        "actual_review_status_counts": {},
                        "requested_review_statuses": [],
                        "missing_requested_review_statuses": [],
                        "trusted_review_status_count": 0,
                    },
                    "source_paths": {
                        "quality_report": "C:/workspace/NCS_MCP/reports/quality.json",
                        "database": "data/processed/ncs.db",
                        "external": "D:/operator/private/triage.json",
                    },
                },
                "operator_constraints": [],
                "quality_warnings": [],
                "transition_review_priorities": [],
                "transition_trust_review_candidates": [],
                "review_priority_items": [],
            },
            Path("C:/workspace/NCS_MCP/reports/aihr_review_triage_20260617.json"),
        )

        self.assertIn("reports/quality.json", html)
        self.assertIn("configured_ncs_database", html)
        self.assertIn("triage.json", html)
        self.assertNotIn("C:/workspace", html)
        self.assertNotIn("C:\\workspace", html)
        self.assertNotIn("data/processed/ncs.db", html)

    def test_ontology_review_board_renders_seedpack_for_scroll_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seedpack = Path(tmp) / "aihr_review_seedpack_20260620.jsonl"
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "review-seedpack-test",
                "allowed_decisions": ["approve", "reject", "defer", "accepted"],
                "item_count": 1,
                "review_status": "human_reviewed",
                "source_payload": {"db_path": "data/processed/ncs.db"},
                "db_path": "data/processed/ncs.db",
                "notes": "C:/workspace/NCS_MCP/reports/private.json source_payload human_reviewed",
            }
            item = {
                "record_type": "review_item",
                "sequence": 1,
                "issue_type": "ontology_training_goal_link_human_review_required",
                "target_type": "training_goal_concept_link",
                "target_id": "42",
                "priority_score": 125,
                "priority_reason": "Training-goal concept links directly affect recommendation ranking.",
                "current_review_status": "human_reviewed",
                "decision": "approve",
                "reviewer_id": "human-reviewer-01",
                "reviewed_at": "2026-06-20T10:00:00+09:00",
                "rationale": "source packet rationale should stay out of the public board payload",
                "source_context_excerpt": "인사기획 | 교육목표 | 관리회계",
                "issue_detail": "Weak training-goal to KSA link needs review.",
                "suggested_action": "Confirm whether the training goal directly covers the KSA.",
                "target_snapshot_hash": "abc123",
                "context": {
                    "compe_unit_name": "인사기획",
                    "concept_name": "관리회계",
                    "concept_type": "knowledge",
                    "concept_review_status": "accepted",
                    "review_status": "reviewed",
                    "confidence_score": 0.52,
                    "train_goal": "인사전략을 수립하고 인건비 운영계획을 수립하는 능력",
                },
            }
            item["source_context_excerpt"] = (
                "Course goal | C:/workspace/NCS_MCP/reports/seed.json | "
                "data/processed/ncs.db | human_reviewed | source_payload"
            )
            item["issue_detail"] = (
                "Weak training-goal to KSA link needs review. reviewed data/processed/ncs.db"
            )
            item["suggested_action"] = (
                "Confirm whether the training goal directly covers the KSA. "
                "accepted C:/workspace/NCS_MCP/private"
            )
            seedpack.write_text(
                json.dumps(batch, ensure_ascii=False) + "\n"
                + json.dumps(item, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            payload = load_review_seedpack_payload(seedpack)
            html = render_ontology_review_board_html(payload, seedpack)

        self.assertEqual(payload["schema"], "ontology_review_board_seedpack_v1")
        self.assertEqual(payload["source_path"], seedpack.name)
        self.assertTrue(payload["batch"]["public_metadata_only"])
        self.assertTrue(payload["batch"]["private_metadata_suppressed"])
        self.assertEqual(payload["allowed_decisions"], ["approve", "reject", "defer"])
        self.assertEqual(payload["item_count"], 1)
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["status_update_allowed"])
        self.assertTrue(payload["public_api_contract"]["raw_review_status_suppressed"])
        self.assertTrue(payload["public_api_contract"]["decision_metadata_suppressed"])
        self.assertFalse(payload["public_api_contract"]["operator_decision_metadata_public"])
        self.assertFalse(payload["public_api_contract"]["trusted_status_claim_allowed"])
        self.assertEqual(payload["decision_metadata_suppressed_count"], 1)
        self.assertTrue(payload["items"][0]["raw_review_status_suppressed"])
        self.assertTrue(payload["items"][0]["decision_metadata_suppressed"])
        self.assertEqual(payload["items"][0]["review_gate_status"], "pending_human_decision")
        self.assertEqual(payload["items"][0]["decision"], "")
        self.assertEqual(payload["items"][0]["reviewer_id"], "")
        self.assertEqual(payload["items"][0]["reviewed_at"], "")
        self.assertEqual(payload["items"][0]["rationale"], "")
        self.assertNotIn("current_review_status", payload["items"][0])
        self.assertNotIn("concept_review_status", dict(payload["items"][0]["context_pairs"]))
        self.assertNotIn("review_status", dict(payload["items"][0]["context_pairs"]))
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(seedpack), serialized_payload)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized_payload)
        self.assertNotIn("C:\\workspace\\NCS_MCP", serialized_payload)
        self.assertNotIn("data/processed/ncs.db", serialized_payload)
        self.assertNotIn("source_payload", serialized_payload)
        self.assertNotIn('"review_status":', serialized_payload)
        self.assertNotIn('"db_path":', serialized_payload)
        self.assertNotIn("human-reviewer-01", serialized_payload)
        self.assertNotIn("source packet rationale should stay out", serialized_payload)
        self.assertIn("Ontology Review Board", html)
        self.assertIn("review-seedpack-test", html)
        self.assertIn("ontology_training_goal_link_human_review_required", html)
        self.assertIn("인사기획", html)
        self.assertIn("관리회계", html)
        self.assertIn("pending_human_decision", html)
        self.assertNotIn("human_reviewed", html)
        self.assertNotIn("accepted", html)
        self.assertNotIn("source_payload", html)
        self.assertNotIn("C:/workspace/NCS_MCP", html)
        self.assertNotIn("C:\\workspace\\NCS_MCP", html)
        self.assertNotIn("data/processed/ncs.db", html)
        self.assertNotIn("reviewed</td>", html)
        self.assertNotIn("human-reviewer-01", html)
        self.assertNotIn("source packet rationale should stay out", html)
        self.assertIn("Export decisions JSONL", html)
        self.assertIn("no raw KSA, concept, link, or review status is changed", html)

    def test_ontology_review_board_json_route_sanitizes_seedpack_source_path(self) -> None:
        previous_seedpack = os.environ.get("NCS_REVIEW_SEEDPACK_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            seedpack = Path(tmp) / "aihr_review_seedpack_20260620.jsonl"
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "review-seedpack-route-test",
                "allowed_decisions": ["approve", "reject", "defer", "accepted"],
                "item_count": 1,
                "review_status": "human_reviewed",
                "source_payload": {"db_path": "data/processed/ncs.db"},
                "db_path": "data/processed/ncs.db",
                "notes": "C:/workspace/NCS_MCP/reports/private.json source_payload human_reviewed",
            }
            item = {
                "record_type": "review_item",
                "sequence": 1,
                "issue_type": "hr_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "101",
                "source_context_excerpt": (
                    "C:/workspace/NCS_MCP/reports/seed.json data/processed/ncs.db "
                    "human_reviewed source_payload"
                ),
                "issue_detail": "reviewed data/processed/ncs.db",
                "suggested_action": "accepted C:/workspace/NCS_MCP/private",
                "context": {
                    "concept_name": "노무관리",
                    "review_status": "human_reviewed",
                    "concept_review_status": "accepted",
                },
            }
            seedpack.write_text(
                json.dumps(batch, ensure_ascii=False) + "\n"
                + json.dumps(item, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = Path(tmp) / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_REVIEW_SEEDPACK_PATH"] = str(seedpack)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urlopen(base_url + "/api/ontology-review-board", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_seedpack is None:
                    os.environ.pop("NCS_REVIEW_SEEDPACK_PATH", None)
                else:
                    os.environ["NCS_REVIEW_SEEDPACK_PATH"] = previous_seedpack

        serialized_payload = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["source_path"], seedpack.name)
        self.assertTrue(payload["batch"]["public_metadata_only"])
        self.assertTrue(payload["batch"]["private_metadata_suppressed"])
        self.assertEqual(payload["allowed_decisions"], ["approve", "reject", "defer"])
        self.assertNotIn(str(seedpack), serialized_payload)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized_payload)
        self.assertNotIn("C:\\workspace\\NCS_MCP", serialized_payload)
        self.assertNotIn("data/processed/ncs.db", serialized_payload)
        self.assertNotIn("source_payload", serialized_payload)
        self.assertNotIn('"db_path":', serialized_payload)
        self.assertNotIn('"review_status":', serialized_payload)
        self.assertNotIn('"concept_review_status":', serialized_payload)
        self.assertNotIn('"current_review_status":', serialized_payload)
        self.assertNotIn("human_reviewed", serialized_payload)
        self.assertNotIn("accepted", serialized_payload)
        self.assertTrue(payload["items"][0]["raw_review_status_suppressed"])
        self.assertEqual(payload["items"][0]["review_gate_status"], "pending_human_decision")

    def test_ontology_review_board_not_found_error_uses_public_expected_globs(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        server.db_path = Path("unused.db")
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            with patch("ncs_dashboard.resolve_review_seedpack_jsonl_path", return_value=None):
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with self.assertRaises(HTTPError) as raised:
                    urlopen(base_url + "/api/ontology-review-board", timeout=5)
                body = raised.exception.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        payload = json.loads(body)
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(payload["error"], "review_seedpack_not_found")
        self.assertTrue(payload["expected_globs"])
        self.assertTrue(all(value.startswith("reports/") for value in payload["expected_globs"]))
        self.assertNotIn(str(ROOT), serialized_payload)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized_payload)
        self.assertNotIn("C:\\workspace\\NCS_MCP", serialized_payload)

    def test_aihr_agent_queue_expected_globs_are_public_paths(self) -> None:
        globs = aihr_agent_queue_expected_globs()

        self.assertTrue(globs)
        self.assertTrue(all(value.startswith("reports/") for value in globs))
        serialized = json.dumps(globs, ensure_ascii=False)
        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized)
        self.assertNotIn("C:\\workspace\\NCS_MCP", serialized)

    def test_ontology_review_board_loads_utf8_sig_seedpack_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seedpack = Path(tmp) / "aihr_review_seedpack_20260620.jsonl"
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "review-seedpack-bom",
                "allowed_decisions": ["approve", "reject", "defer"],
                "item_count": 1,
            }
            item = {
                "record_type": "review_item",
                "sequence": 1,
                "issue_type": "hr_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "101",
                "context": {"concept_name": "근로기준법"},
            }
            seedpack.write_text(
                json.dumps(batch, ensure_ascii=False) + "\n"
                + json.dumps(item, ensure_ascii=False) + "\n",
                encoding="utf-8-sig",
            )

            payload = load_review_seedpack_payload(seedpack)

        self.assertEqual(payload["batch"]["seedpack_id"], "review-seedpack-bom")
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["parse_errors"], [])

    def test_render_aihr_provenance_reconfirmation_html_shows_legacy_display_status(self) -> None:
        html = render_aihr_provenance_reconfirmation_html(
            {
                "ok": True,
                "row_count": 1,
                "legacy_status_needs_reconfirmation_count": 1,
                "status_update_allowed": False,
                "source_audit_summary": {"rows_packet_backed": 0},
                "review_status_display_counts": {
                    "legacy_status_needs_reconfirmation:human_reviewed": 1
                },
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "raw_review_status": "human_reviewed",
                        "review_status_display": "legacy_status_needs_reconfirmation:human_reviewed",
                        "status_trust": "not_trusted_until_packet_backed_reconfirmation",
                        "provenance_state": "audit_log_without_packet",
                        "source_decision_packet_available": False,
                        "rationale_available": False,
                        "evidence_refs_available": False,
                        "display": "\uc778\uc0ac / \uc9c1\ubb34\uad00\ub9ac -> \uc9c1\ubb34\uad00\ub9ac",
                        "requested_decision": "reconfirm | downgrade_to_review_required | defer",
                    }
                ],
            },
            Path("reports/aihr_human_review_provenance_reconfirmation_packet_20260619.json"),
        )

        self.assertIn("AI-HR Provenance Reconfirmation", html)
        self.assertIn("legacy_status_needs_reconfirmation", html)
        self.assertNotIn("legacy_status_needs_reconfirmation:human_reviewed", html)
        self.assertIn("not_trusted_until_packet_backed_reconfirmation", html)
        self.assertIn("evidence_refs=False", html)
        self.assertIn("raw status suppressed", html)
        self.assertNotIn("raw: human_reviewed", html)
        self.assertNotIn("<td>human_reviewed</td>", html)
        self.assertIn("No DB writes", html)

    def test_public_aihr_provenance_reconfirmation_payload_suppresses_raw_status(self) -> None:
        payload = {
            "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
            "db_path": "C:/workspace/NCS_MCP/data/processed/ncs.db",
            "rows": [
                {
                    "target_id": "146",
                    "current_review_status": "human_reviewed",
                    "raw_review_status": "human_reviewed",
                    "review_status_display": "legacy_status_needs_reconfirmation:human_reviewed",
                    "status_trust": "not_trusted_until_packet_backed_reconfirmation",
                }
            ],
        }

        public_payload = public_aihr_provenance_reconfirmation_payload(payload)

        self.assertNotIn("db_path", public_payload)
        self.assertEqual(public_payload["source_database_ref"], "configured_ncs_database")
        self.assertNotIn(
            "C:/workspace/NCS_MCP/data/processed/ncs.db",
            json.dumps(public_payload, ensure_ascii=False),
        )
        row = public_payload["rows"][0]
        self.assertNotIn("current_review_status", row)
        self.assertNotIn("raw_review_status", row)
        self.assertTrue(row["raw_review_status_suppressed"])
        self.assertEqual(
            row["review_status_display"],
            "legacy_status_needs_reconfirmation",
        )
        self.assertFalse(
            public_payload["public_api_contract"]["trusted_status_claim_allowed"]
        )

    def test_query_router_samples_include_transition_and_risk_cases(self) -> None:
        samples = get_query_router_samples()
        scenarios = {str(item["scenario"]) for item in samples}
        self.assertIn("education_system_design", scenarios)

        transition = next(item for item in samples if item["label"] == "Education-system transition")
        self.assertEqual(transition["tool"], "plan_ncs_education_path")
        self.assertEqual(transition["available"], True)
        self.assertIn("route_fingerprint", transition)
        self.assertGreater(float(transition["confidence"]), 0)
        self.assertIn("plan_ncs_education_path", transition["expected_tool_chain"])
        self.assertIn("recommend_training_transition", [item["tool"] for item in transition["pipeline"]])

        risk = next(item for item in samples if item["label"] == "Official-claim risk")
        risk_codes = {item["code"] for item in risk["risk_flags"]}
        self.assertIn("official_or_legal_claim_risk", risk_codes)

        operator_route = next(item for item in samples if item["label"] == "Operator review gated route")
        self.assertEqual(operator_route["scenario"], "operator_review")
        self.assertEqual(operator_route["tool"], "get_quality_issues")
        self.assertEqual(operator_route["available"], False)
        self.assertEqual(operator_route["params"]["target_type"], "training_goal_concept_link")
        self.assertEqual(operator_route["missing_params"], [])
        guard_codes = {item["code"] for item in operator_route["guard_flags"]}
        self.assertIn("operator_review_route", guard_codes)
        self.assertIn("route_tool_unavailable", guard_codes)

    def test_render_query_router_samples_html_shows_router_fields(self) -> None:
        html = render_query_router_samples_html()
        self.assertIn("NCS Query Router Samples", html)
        self.assertIn("Education-system transition", html)
        self.assertIn("education_system_design", html)
        self.assertIn("plan_ncs_education_path", html)
        self.assertIn("Official-claim risk", html)
        self.assertIn("Operator review gated route", html)
        self.assertIn("official_or_legal_claim_risk", html)
        self.assertIn("Missing Params", html)
        self.assertIn("Confidence", html)
        self.assertIn("Expected Tool Chain", html)
        self.assertIn("Guard Flags", html)
        self.assertIn("Route Fingerprint", html)
        self.assertIn("route_tool_unavailable", html)
        self.assertIn("Matched Signals", html)
        self.assertIn("Pipeline", html)

    def test_aihr_routes_serve_configured_artifacts(self) -> None:
        previous_demo = os.environ.get("NCS_AIHR_DEMO_HTML_PATH")
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_triage = os.environ.get("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH")
        previous_reconfirm = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        previous_queue = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        previous_queue_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            demo_path = tmp_path / "custom_demo.html"
            readiness_path = tmp_path / "custom_readiness.json"
            triage_path = tmp_path / "custom_triage.json"
            reconfirm_path = tmp_path / "custom_reconfirm.json"
            queue_path = tmp_path / "custom_queue.json"
            queue_status_path = tmp_path / "custom_queue_status.json"
            queue_run_path = tmp_path / "custom_queue_run.json"
            demo_path.write_text("<html><body>custom demo route</body></html>", encoding="utf-8")
            readiness_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blockers": [{"name": "trusted_transition_scenarios"}],
                        "warnings": [],
                        "demo_contract": {"ok": True, "json_artifacts": [], "html_artifact": {}},
                        "dashboard_surface_contract": {
                            "ok": True,
                            "artifact": {
                                "path": "reports/custom_dashboard_verification.json",
                                "queue_status_summary": {"blocked_count": 0},
                                "static_artifacts": [
                                    {
                                        "name": "demo_json",
                                        "path": "reports/custom_demo.json",
                                        "exists": True,
                                        "non_empty": True,
                                        "size_bytes": 1234,
                                    },
                                    {
                                        "name": "local_db_marker",
                                        "path": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                                        "exists": True,
                                        "non_empty": True,
                                        "size_bytes": 999,
                                    }
                                ],
                                "live_plan_summaries": [
                                    {
                                        "name": "baseline",
                                        "ok": True,
                                        "matrix_rows": 1,
                                        "training_necessity_review_summary": {
                                            "schema": "aihr_training_necessity_review_v1",
                                            "guide_stage": "C1-2",
                                            "row_count": 1,
                                            "review_required_rows": 1,
                                            "approval_blocked_rows": 1,
                                            "approval_claim_safe": True,
                                        },
                                        "annual_operation_plan_summary": {
                                            "schema": "aihr_annual_operation_plan_seed_v1",
                                            "guide_stage": "C2-2",
                                            "row_count": 1,
                                            "estimated_total_hours": 12,
                                            "pending_human_decision_rows": 1,
                                            "approval_claim_safe": True,
                                        },
                                        "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                        "missing_matrix_fields": [],
                                        "missing_guide_trace_fields": [],
                                        "sensitive_markers": [],
                                    }
                                ],
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "quality_warning_count": 1,
                            "review_issue_type_counts": {"hr_training_goal_link_human_review_required": 1},
                            "transition_seedpack_item_count": 1,
                            "transition_attention_count": 1,
                            "transition_seedpack_id": "custom-transition-seedpack",
                            "transition_status_snapshot": {
                                "requested_review_statuses": ["candidate"],
                                "actual_review_status_counts": {"candidate": 1},
                                "missing_requested_review_statuses": [],
                                "trusted_review_status_count": 0,
                            },
                            "source_paths": {
                                "transition_seedpack": "reports/custom_transition_seedpack.jsonl",
                                "database": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                            },
                        },
                        "operator_constraints": ["review only"],
                        "quality_warnings": [],
                        "transition_review_priorities": [{"scenario_name": "scenario_one"}],
                        "review_priority_items": [
                            {
                                "issue_type": "hr_training_goal_link_human_review_required",
                                "priority_reason": "Visible recommendation evidence needs review.",
                                "context_excerpt": "custom course | custom KSA",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            reconfirm_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                        "row_count": 1,
                        "legacy_status_needs_reconfirmation_count": 1,
                        "status_update_allowed": False,
                        "source_audit_summary": {"rows_packet_backed": 0},
                        "review_status_display_counts": {
                            "legacy_status_needs_reconfirmation:human_reviewed": 1
                        },
                        "rows": [
                            {
                                "order": 1,
                                "surface": "ncs_career_paths",
                                "target_table": "ncs_career_paths",
                                "target_id": "146",
                                "raw_review_status": "human_reviewed",
                                "review_status_display": (
                                    "legacy_status_needs_reconfirmation:human_reviewed"
                                ),
                                "status_trust": (
                                    "not_trusted_until_packet_backed_reconfirmation"
                                ),
                                "provenance_state": "audit_log_without_packet",
                                "source_decision_packet_available": False,
                                "rationale_available": False,
                                "evidence_refs_available": False,
                                "display": "career path needs reconfirmation",
                                "requested_decision": (
                                    "reconfirm | downgrade_to_review_required | defer"
                                ),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "item_count": 1,
                        "global_guardrails": ["queue route guardrail"],
                        "items": [{"owner": "evaluation-agent", "command": "python scripts\\ncs_harness.py inspect"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_hash = "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()
            queue_status_hash = canonical_json_sha256(
                build_agent_queue_status_from_file(queue_path, workspace=ROOT)
            )
            queue_status_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_agent_queue_status_v1",
                        "source_queue_path": str(queue_path),
                        "summary": {
                            "item_count": 1,
                            "auto_startable_count": 1,
                            "manual_ready_count": 0,
                            "blocked_count": 0,
                            "state_counts": {"ready_to_start": 1},
                        },
                        "execution_order": [
                            {
                                "priority": 3,
                                "owner": "evaluation-agent",
                                "mutation_policy": "regenerate_reports_only",
                                "requires_human_decision": False,
                                "command": "python scripts\\ncs_harness.py review-priority",
                            }
                        ],
                        "manual_queue": [],
                        "blocked_queue": [],
                        "items": [
                            {
                                "id": "aihr-01",
                                "priority": 3,
                                "owner": "evaluation-agent",
                                "agent_file": ".agents/evaluation-agent.md",
                                "covered_blockers": ["transition_eval:trusted_scenarios"],
                                "mutation_policy": "regenerate_reports_only",
                                "requires_human_decision": False,
                                "command": "python scripts\\ncs_harness.py review-priority",
                                "state": "ready_to_start",
                                "preflight_ok": True,
                                "can_start_automated": True,
                                "missing_prerequisite_artifacts": [],
                                "existing_expected_artifacts": [],
                                "missing_expected_artifacts": [],
                                "safety_violations": [],
                                "acceptance_checks": ["status route acceptance"],
                            }
                        ],
                        "global_guardrails": ["status route guardrail"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_run_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_agent_queue_run_v1",
                        "source_queue_path": str(queue_path),
                        "source_queue_sha256": queue_hash,
                        "queue_status_snapshot_sha256": queue_status_hash,
                        "summary": {
                            "selected_count": 1,
                            "succeeded_count": 1,
                            "failed_count": 0,
                            "skipped_unsafe_count": 0,
                        },
                        "queue_status_summary": {"auto_startable_count": 1},
                        "runs": [
                            {
                                "order": 1,
                                "id": "aihr-01",
                                "status": "succeeded",
                                "exit_code": 0,
                                "owner": "evaluation-agent",
                                "mutation_policy": "regenerate_reports_only",
                                "command": "python scripts\\ncs_harness.py review-priority",
                                "validation_errors": [],
                                "stdout_tail": "queue run stdout",
                                "stdout_original_chars": 16,
                                "stdout_tail_chars": 16,
                                "stdout_truncated": False,
                                "stdout_redacted": False,
                                "stdout_redaction_count": 0,
                                "stderr_tail": "",
                                "stderr_original_chars": 0,
                                "stderr_tail_chars": 0,
                                "stderr_truncated": False,
                                "stderr_redacted": False,
                                "stderr_redaction_count": 0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_DEMO_HTML_PATH"] = str(demo_path)
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = str(triage_path)
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(reconfirm_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = str(queue_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(queue_status_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                with urlopen(base_url + "/aihr-plan-demo", timeout=5) as response:
                    demo_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-live", timeout=5) as response:
                    live_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-training-system-builder", timeout=5) as response:
                    builder_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-readiness", timeout=5) as response:
                    readiness_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/api/aihr-readiness", timeout=5) as response:
                    readiness_api = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-review-board", timeout=5) as response:
                    review_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/api/aihr-review-board", timeout=5) as response:
                    review_api = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-provenance-reconfirmation", timeout=5) as response:
                    reconfirm_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/api/aihr-provenance-reconfirmation", timeout=5) as response:
                    reconfirm_api = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-query-router", timeout=5) as response:
                    router_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-agent-queue", timeout=5) as response:
                    queue_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-agent-queue-status", timeout=5) as response:
                    queue_status_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/api/aihr-agent-queue-status", timeout=5) as response:
                    queue_status_api = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/aihr-agent-queue-run", timeout=5) as response:
                    queue_run_body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                with urlopen(base_url + "/api/aihr-agent-queue-run", timeout=5) as response:
                    queue_run_api = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)

                self.assertIn("custom demo route", demo_body)
                self.assertIn("AI-HR Live Planner", live_body)
                self.assertIn("/api/aihr-plan", live_body)
                self.assertIn("AI-HR Training System Builder", builder_body)
                self.assertIn("2026 Guide Workflow", builder_body)
                self.assertIn("/api/aihr-plan", builder_body)
                self.assertIn("AI-HR 준비도", readiness_body)
                self.assertIn("trusted_transition_scenarios", readiness_body)
                self.assertIn("queue_blocked", readiness_body)
                self.assertIn("reports/custom_demo.json", readiness_body)
                self.assertIn("reports/custom_dashboard_verification.json", readiness_body)
                self.assertIn("AI-HR Review Triage", readiness_body)
                self.assertIn("custom-transition-seedpack", readiness_body)
                self.assertIn("reports/custom_transition_seedpack.jsonl", readiness_body)
                self.assertIn("configured_ncs_database", readiness_body)
                self.assertNotIn("C:/workspace/NCS_MCP", readiness_body)
                self.assertNotIn("data/processed/ncs.db", readiness_body)
                self.assertEqual(
                    readiness_api["dashboard_surface_contract"]["artifact"]["static_artifacts"][1]["path"],
                    "configured_ncs_database",
                )
                self.assertNotIn(
                    "C:/workspace/NCS_MCP",
                    json.dumps(readiness_api, ensure_ascii=False),
                )
                self.assertIn("hr_training_goal_link_human_review_required", readiness_body)
                self.assertIn("transition_seedpack_items", readiness_body)
                self.assertIn("trusted_in_seedpack", readiness_body)
                self.assertIn("AI-HR 검토보드", review_body)
                self.assertIn("scenario_one", review_body)
                self.assertIn("custom-transition-seedpack", review_body)
                self.assertIn("reports/custom_transition_seedpack.jsonl", review_body)
                self.assertIn("configured_ncs_database", review_body)
                self.assertNotIn("C:/workspace/NCS_MCP", review_body)
                self.assertNotIn("data/processed/ncs.db", review_body)
                self.assertEqual(
                    review_api["summary"]["source_paths"]["database"],
                    "configured_ncs_database",
                )
                self.assertNotIn(
                    "C:/workspace/NCS_MCP",
                    json.dumps(review_api, ensure_ascii=False),
                )
                self.assertIn("Review Issue Type Counts", review_body)
                self.assertIn("hr_training_goal_link_human_review_required", review_body)
                self.assertIn("Visible recommendation evidence needs review.", review_body)
                self.assertIn("custom course | custom KSA", review_body)
                self.assertIn("AI-HR Provenance Reconfirmation", reconfirm_body)
                self.assertIn("legacy_status_needs_reconfirmation", reconfirm_body)
                self.assertNotIn("legacy_status_needs_reconfirmation:human_reviewed", reconfirm_body)
                self.assertIn("raw status suppressed", reconfirm_body)
                self.assertEqual(
                    reconfirm_api["schema"],
                    "aihr_human_review_provenance_reconfirmation_packet_v1",
                )
                self.assertEqual(reconfirm_api["legacy_status_needs_reconfirmation_count"], 1)
                self.assertNotIn("current_review_status", reconfirm_api["rows"][0])
                self.assertNotIn("raw_review_status", reconfirm_api["rows"][0])
                self.assertTrue(reconfirm_api["rows"][0]["raw_review_status_suppressed"])
                self.assertEqual(
                    reconfirm_api["rows"][0]["review_status_display"],
                    "legacy_status_needs_reconfirmation",
                )
                self.assertIn("NCS Query Router Samples", router_body)
                self.assertIn("education_system_design", router_body)
                self.assertIn("official_or_legal_claim_risk", router_body)
                self.assertIn("AI-HR Agent Work Queue", queue_body)
                self.assertIn("queue route guardrail", queue_body)
                self.assertIn("evaluation-agent", queue_body)
                self.assertIn("AI-HR Agent Queue Status", queue_status_body)
                self.assertIn("status route guardrail", queue_status_body)
                self.assertEqual(queue_status_api["schema"], "aihr_agent_queue_status_v1")
                self.assertEqual(queue_status_api["summary"]["auto_startable_count"], 1)
                self.assertIn("AI-HR Agent Queue Run", queue_run_body)
                self.assertIn("redacted=False", queue_run_body)
                self.assertIn("redactions=0", queue_run_body)
                self.assertNotIn("queue run stdout", queue_run_body)
                self.assertEqual(queue_run_api["schema"], "aihr_agent_queue_run_v1")
                self.assertEqual(queue_run_api["summary"]["succeeded_count"], 1)
                self.assertTrue(queue_run_api["output_tails_suppressed"])
                self.assertNotIn("stdout_tail", queue_run_api["runs"][0])
                self.assertTrue(queue_run_api["runs"][0]["stdout_tail_suppressed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_demo is None:
                    os.environ.pop("NCS_AIHR_DEMO_HTML_PATH", None)
                else:
                    os.environ["NCS_AIHR_DEMO_HTML_PATH"] = previous_demo
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_triage is None:
                    os.environ.pop("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = previous_triage
                if previous_reconfirm is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous_reconfirm
                if previous_queue is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous_queue
                if previous_queue_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_queue_status
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run

    def test_aihr_agent_queue_run_routes_reject_configured_dryrun_artifact(self) -> None:
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_run_path = tmp_path / "custom_queue_run.json"
            queue_run_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_agent_queue_run_v1",
                        "summary": {
                            "selected_count": 1,
                            "dry_run": True,
                            "dry_run_count": 1,
                            "succeeded_count": 0,
                        },
                        "runs": [{"status": "dry_run", "id": "aihr-01"}],
                    }
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route in ["/aihr-agent-queue-run", "/api/aihr-agent-queue-run"]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 409)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn("aihr_agent_queue_run_not_actual", body)
                    self.assertIn("custom_queue_run.json", body)
                    self.assertNotIn(str(tmp_path), body)
                    self.assertNotIn("C:/workspace", body)
                    self.assertNotIn("C:\\workspace", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run

    def test_aihr_agent_queue_run_routes_reject_source_queue_hash_mismatch(self) -> None:
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text('{"schema":"aihr_agent_work_queue_v1","items":[]}\n', encoding="utf-8")
            original_hash = "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()
            queue_path.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[{"id":"changed"}]}\n',
                encoding="utf-8",
            )
            status_hash = canonical_json_sha256(
                build_agent_queue_status_from_file(queue_path, workspace=ROOT)
            )
            queue_run_path = tmp_path / "custom_queue_run.json"
            queue_run_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_agent_queue_run_v1",
                        "source_queue_path": str(queue_path),
                        "source_queue_sha256": original_hash,
                        "queue_status_snapshot_sha256": status_hash,
                        "summary": {
                            "selected_count": 1,
                            "dry_run": False,
                            "dry_run_count": 0,
                            "succeeded_count": 1,
                        },
                        "runs": [{"status": "succeeded", "id": "aihr-01"}],
                    }
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route in ["/aihr-agent-queue-run", "/api/aihr-agent-queue-run"]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 409)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn("aihr_agent_queue_run_stale_or_unlinked", body)
                    self.assertIn("source_queue_hash_mismatch", body)
                    self.assertNotIn(str(tmp_path), body)
                    self.assertNotIn("C:/workspace", body)
                    self.assertNotIn("C:\\workspace", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run

    def test_aihr_agent_queue_run_routes_reject_missing_lineage_hashes(self) -> None:
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_path = tmp_path / "aihr_agent_queue_20260624.json"
            queue_path.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[]}\n',
                encoding="utf-8",
            )
            queue_run_path = tmp_path / "custom_queue_run.json"
            queue_run_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_agent_queue_run_v1",
                        "source_queue_path": str(queue_path),
                        "summary": {
                            "selected_count": 1,
                            "dry_run": False,
                            "dry_run_count": 0,
                            "succeeded_count": 1,
                        },
                        "runs": [{"status": "succeeded", "id": "aihr-01"}],
                    }
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route in ["/aihr-agent-queue-run", "/api/aihr-agent-queue-run"]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 409)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn("aihr_agent_queue_run_stale_or_unlinked", body)
                    self.assertIn("source_queue_sha256_missing", body)
                    self.assertIn("queue_status_snapshot_sha256_missing", body)
                    self.assertNotIn(str(tmp_path), body)
                    self.assertNotIn("C:/workspace", body)
                    self.assertNotIn("C:\\workspace", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run

    def test_aihr_agent_queue_run_routes_reject_skipped_only_artifact(self) -> None:
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_run_path = tmp_path / "custom_queue_run.json"
            queue_run_path.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "schema": "aihr_agent_queue_run_v1",
                        "summary": {
                            "selected_count": 1,
                            "dry_run": False,
                            "dry_run_count": 0,
                            "succeeded_count": 0,
                            "skipped_unsafe_count": 1,
                        },
                        "runs": [{"status": "skipped_unsafe", "id": "aihr-01"}],
                    }
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route in ["/aihr-agent-queue-run", "/api/aihr-agent-queue-run"]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 409)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn("aihr_agent_queue_run_not_actual", body)
                    self.assertIn("custom_queue_run.json", body)
                    self.assertNotIn(str(tmp_path), body)
                    self.assertNotIn("C:/workspace", body)
                    self.assertNotIn("C:\\workspace", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run

    def test_aihr_json_routes_return_stable_error_for_malformed_artifacts(self) -> None:
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_triage = os.environ.get("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH")
        previous_queue = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        previous_queue_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        previous_reconfirm = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "bad_readiness.json"
            triage_path = tmp_path / "bad_triage.json"
            queue_path = tmp_path / "bad_queue.json"
            queue_status_path = tmp_path / "bad_queue_status.json"
            queue_run_path = tmp_path / "bad_queue_run.json"
            reconfirm_path = tmp_path / "bad_reconfirm.json"
            for path in [
                readiness_path,
                triage_path,
                queue_path,
                queue_status_path,
                queue_run_path,
                reconfirm_path,
            ]:
                path.write_text("{not-json", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = str(triage_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = str(queue_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(queue_status_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    reconfirm_path
                )
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route, error_code in [
                    ("/aihr-readiness", "aihr_readiness_invalid"),
                    ("/aihr-review-board", "aihr_review_triage_invalid"),
                    (
                        "/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    (
                        "/api/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    ("/aihr-agent-queue", "aihr_agent_queue_invalid"),
                    ("/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/api/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                    ("/api/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                ]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 400)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn(error_code, body)
                    self.assertNotIn('"error": "JSONDecodeError"', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_triage is None:
                    os.environ.pop("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = previous_triage
                if previous_queue is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous_queue
                if previous_queue_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_queue_status
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run
                if previous_reconfirm is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous_reconfirm

    def test_aihr_json_routes_return_stable_error_for_wrong_type_artifacts(self) -> None:
        previous_readiness = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
        previous_triage = os.environ.get("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH")
        previous_queue = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
        previous_queue_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        previous_reconfirm = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "wrong_type_readiness.json"
            triage_path = tmp_path / "wrong_type_triage.json"
            queue_path = tmp_path / "wrong_type_queue.json"
            queue_status_path = tmp_path / "wrong_type_queue_status.json"
            queue_run_path = tmp_path / "wrong_type_queue_run.json"
            reconfirm_path = tmp_path / "wrong_type_reconfirm.json"
            for path in [
                readiness_path,
                triage_path,
                queue_path,
                queue_status_path,
                queue_run_path,
                reconfirm_path,
            ]:
                path.write_text("[]", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_READINESS_JSON_PATH"] = str(readiness_path)
                os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = str(triage_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = str(queue_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(queue_status_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    reconfirm_path
                )
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route, error_code in [
                    ("/aihr-readiness", "aihr_readiness_invalid"),
                    ("/aihr-review-board", "aihr_review_triage_invalid"),
                    (
                        "/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    (
                        "/api/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    ("/aihr-agent-queue", "aihr_agent_queue_invalid"),
                    ("/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/api/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                    ("/api/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                ]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 400)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn(error_code, body)
                    self.assertIn("expected JSON object", body)
                    self.assertNotIn("AttributeError", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_readiness is None:
                    os.environ.pop("NCS_AIHR_READINESS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_READINESS_JSON_PATH"] = previous_readiness
                if previous_triage is None:
                    os.environ.pop("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_REVIEW_TRIAGE_JSON_PATH"] = previous_triage
                if previous_queue is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_JSON_PATH"] = previous_queue
                if previous_queue_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_queue_status
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run
                if previous_reconfirm is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous_reconfirm

    def test_aihr_json_routes_return_stable_error_for_invalid_utf8_artifacts(self) -> None:
        previous_queue_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_queue_run = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
        previous_reconfirm = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_status_path = tmp_path / "bad_queue_status.json"
            queue_run_path = tmp_path / "bad_queue_run.json"
            reconfirm_path = tmp_path / "bad_reconfirm.json"
            queue_status_path.write_bytes(b"\xff\xfe\x00")
            queue_run_path.write_bytes(b"\xff\xfe\x00")
            reconfirm_path.write_bytes(b"\xff\xfe\x00")
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(queue_status_path)
                os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = str(queue_run_path)
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    reconfirm_path
                )
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                for route, error_code in [
                    (
                        "/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    (
                        "/api/aihr-provenance-reconfirmation",
                        "aihr_provenance_reconfirmation_invalid",
                    ),
                    ("/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/api/aihr-agent-queue-status", "aihr_agent_queue_status_invalid"),
                    ("/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                    ("/api/aihr-agent-queue-run", "aihr_agent_queue_run_invalid"),
                ]:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(base_url + route, timeout=5)
                    self.assertEqual(raised.exception.code, 400)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn(error_code, body)
                    self.assertNotIn('"error": "UnicodeDecodeError"', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_queue_status
                if previous_queue_run is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH"] = previous_queue_run
                if previous_reconfirm is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous_reconfirm

    def test_aihr_json_routes_accept_utf8_sig_artifacts(self) -> None:
        previous_queue_status = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
        previous_reconfirm = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_status_path = tmp_path / "queue_status.json"
            reconfirm_path = tmp_path / "reconfirm.json"
            queue_status_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_queue_status_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    }
                ),
                encoding="utf-8-sig",
            )
            reconfirm_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required": True,
                        "rows": [],
                    }
                ),
                encoding="utf-8-sig",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = str(queue_status_path)
                os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = str(
                    reconfirm_path
                )
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"

                queue_body = urlopen(
                    base_url + "/api/aihr-agent-queue-status",
                    timeout=5,
                ).read().decode("utf-8")
                reconfirm_body = urlopen(
                    base_url + "/api/aihr-provenance-reconfirmation",
                    timeout=5,
                ).read().decode("utf-8")
                self.assertIn('"schema": "aihr_agent_queue_status_v1"', queue_body)
                self.assertIn(
                    '"schema": "aihr_human_review_provenance_reconfirmation_packet_v1"',
                    reconfirm_body,
                )
                self.assertNotIn("aihr_agent_queue_status_invalid", queue_body)
                self.assertNotIn("aihr_provenance_reconfirmation_invalid", reconfirm_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if previous_queue_status is None:
                    os.environ.pop("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH"] = previous_queue_status
                if previous_reconfirm is None:
                    os.environ.pop("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH", None)
                else:
                    os.environ["NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH"] = previous_reconfirm

    def test_aihr_plan_post_returns_stable_error_for_invalid_json_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            server.db_path = tmp_path / "unused.db"
            thread = Thread(target=server.serve_forever, daemon=True)
            try:
                thread.start()
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                cases = [
                    Request(
                        base_url + "/api/aihr-plan",
                        data=b"{bad-json}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    Request(
                        base_url + "/api/aihr-plan",
                        data=b"[]",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    Request(
                        base_url + "/api/aihr-plan",
                        data=b"\xff\xfe",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                ]
                for request in cases:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(request, timeout=5)
                    self.assertEqual(raised.exception.code, 400)
                    body = raised.exception.read().decode("utf-8")
                    self.assertIn("invalid_json_body", body)
                    self.assertNotIn('"error": "JSONDecodeError"', body)
                    self.assertNotIn('"error": "UnicodeDecodeError"', body)
                    self.assertNotIn('"error": "AttributeError"', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_dashboard_html_keeps_javascript_newline_escapes(self) -> None:
        escaped_newline_join = "join('" + "\\n" + "')"
        literal_newline_join = "join('" + "\n" + "')"
        self.assertIn(escaped_newline_join, HTML)
        self.assertNotIn(literal_newline_join, HTML)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_lookup_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        before_mtime_ns = db_path.stat().st_mtime_ns
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        classifications = get_classifications(db_path, params)
        self.assertIn("classifications", classifications)
        self.assertGreaterEqual(len(classifications["classifications"]), 1)

        units = get_units(db_path, params)
        self.assertIn("units", units)
        self.assertGreaterEqual(len(units["units"]), 1)
        self.assertLessEqual(units["units"][0]["element_matched"], units["units"][0]["element_count"])

        detail = get_unit_detail(db_path, {"unit_code": [units["units"][0]["unit_code"]]})
        self.assertIn("unit", detail)
        self.assertIn("elements", detail)

        api_orphans = get_api_orphans(db_path, {"limit": ["5"]})
        self.assertIn("api_orphans", api_orphans)
        self.assertIsInstance(api_orphans["api_orphans"], list)
        self.assertEqual(db_path.stat().st_mtime_ns, before_mtime_ns)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_workbench_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        before_mtime_ns = db_path.stat().st_mtime_ns
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        progress = get_progress(db_path, params)
        self.assertIn("phases", progress)
        self.assertGreaterEqual(len(progress["phases"]), 1)

        taxonomy = get_taxonomy(db_path, {"level": ["major"], "limit": ["30"]})
        self.assertIn("nodes", taxonomy)
        self.assertGreaterEqual(len(taxonomy["nodes"]), 1)
        self.assertIn("element_percent", taxonomy["nodes"][0])

        ontology = get_ontology(db_path, params)
        self.assertIn("units", ontology)
        self.assertGreaterEqual(len(ontology["units"]), 1)
        self.assertIn("elements", ontology["units"][0])

        ontology_status = get_ontology_status(db_path, params)
        self.assertIn("statuses", ontology_status)
        self.assertEqual(
            {item["concept_type"] for item in ontology_status["statuses"]},
            {"knowledge", "skill", "attitude"},
        )

        concepts = get_concepts(db_path, {**params, "concept_type": ["knowledge"]})
        self.assertIn("concepts", concepts)

        ksa_definitions = get_ksa_definitions(db_path, {**params, "limit": ["5"]})
        self.assertTrue(ksa_definitions["ok"])
        self.assertIn("summary", ksa_definitions)
        self.assertIn("items", ksa_definitions)

        workbench = get_workbench(db_path, params)
        self.assertIn("cards", workbench)
        self.assertGreaterEqual(len(workbench["cards"]), 1)

        items = get_items(db_path, {**params, "kind": ["element"], "state": ["api_matched"]})
        self.assertIn("items", items)
        self.assertGreaterEqual(len(items["items"]), 1)

        detail = get_item_detail(
            db_path,
            {"kind": ["element"], "id": [str(items["items"][0]["id"])]},
        )
        self.assertIn("item", detail)
        self.assertEqual(detail["item"]["kind"], "element")

        sqf_items = get_items(
            db_path,
            {"kind": ["sqf"], "state": ["mvp"], "limit": ["10"]},
        )
        self.assertIn("items", sqf_items)
        self.assertGreaterEqual(sqf_items["total"], 0)
        if sqf_items["items"]:
            sqf_detail = get_item_detail(
                db_path,
                {"kind": ["sqf"], "id": [str(sqf_items["items"][0]["id"])]},
            )
            self.assertIn("item", sqf_detail)
            self.assertEqual(sqf_detail["item"]["kind"], "sqf")
        self.assertEqual(db_path.stat().st_mtime_ns, before_mtime_ns)

    def test_dashboard_review_mapping_requires_human_decision_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_duties(
                    source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                    job_name, duty_name, duty_level, source_payload, api_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "02", "경영·회계·사무", "경영관리", "경영지원", "사무행정(2)", "2", "{}", timestamp),
            )
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "ncs_competency_unit", "0202030201_22v3", "partiallyCovers", 8, "lexical", "test", "candidate", timestamp, timestamp),
            )
            match_id = conn.execute("SELECT match_id FROM sqf_ncs_matches").fetchone()["match_id"]
            conn.commit()
            conn.close()

            result = review_mapping_candidate(
                db_path,
                {"match_id": match_id, "action": "accept", "reviewer_id": "tester", "notes": "ok"},
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "sqf_mapping_review_requires_human_decision_packet")
            self.assertIn("mapping_review_requires_source_decision_packet", result["blockers"])
            self.assertIn("mapping_review_requires_evidence_refs", result["blockers"])

            conn = connect(db_path)
            initialize_database(conn)
            status = conn.execute(
                "SELECT review_status FROM sqf_ncs_matches WHERE match_id = ?",
                (match_id,),
            ).fetchone()["review_status"]
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            self.assertEqual(status, "candidate")
            self.assertEqual(audit_count, 0)
            conn.close()

    def test_dashboard_review_mapping_with_human_packet_creates_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_duties(
                    source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                    job_name, duty_name, duty_level, source_payload, api_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "02", "Business", "Management", "Support", "HR(2)", "2", "{}", timestamp),
            )
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "ncs_competency_unit", "0202030201_22v3", "partiallyCovers", 8, "lexical", "test", "candidate", timestamp, timestamp),
            )
            match_id = conn.execute("SELECT match_id FROM sqf_ncs_matches").fetchone()["match_id"]
            conn.commit()
            conn.close()

            packet, packet_hash = self._write_review_packet(
                tmp,
                "sqf_decision_sheet_test.csv",
                f"match_id:{match_id}",
                extra="decision,accept\n",
                reviewer_id="human_reviewer_01",
                decision="accept",
            )
            result = review_mapping_candidate(
                db_path,
                {
                    "match_id": match_id,
                    "action": "accept",
                    "reviewer_id": "human_reviewer_01",
                    "notes": "Scope and level evidence checked.",
                    "rationale": "SQF duty and NCS unit describe the same HR support work scope.",
                    "source_decision_packet": packet,
                    "source_artifact_hash": packet_hash,
                    "evidence_refs": ["sqf-claim:test:evidence:1"],
                    "run_artifact": "reports/sqf_decision_sheet_test.csv",
                },
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["new_status"], "accepted")

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute(
                """
                SELECT m.review_status, a.source_decision_packet, a.rationale,
                       a.evidence_refs_json, a.created_by_tool
                FROM sqf_ncs_matches m
                JOIN review_audit_log a ON a.entity_type = 'sqf_ncs_match'
                 AND a.entity_id = CAST(m.match_id AS TEXT)
                WHERE m.match_id = ?
                """,
                (match_id,),
            ).fetchone()
            self.assertEqual(row["review_status"], "accepted")
            self.assertEqual(row["source_decision_packet"], packet)
            self.assertIn("same HR support work scope", row["rationale"])
            self.assertIn("sqf-claim:test:evidence:1", row["evidence_refs_json"])
            self.assertEqual(row["created_by_tool"], "ncs_dashboard.review_mapping_candidate")
            conn.close()

    def test_ksa_label_candidate_review_requires_human_input_and_audits_status_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'Planning')
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
                (classification_id, timestamp, timestamp),
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
            source_text = "한국의 협력대상국 지원 실적 및 사례현황"
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name,
                    ksa_no, ksa_text_raw
                ) VALUES (?, 'K', 'knowledge', '1', ?)
                """,
                (element_id, source_text),
            )
            ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES (?, 'koreasupportcases', 'knowledge',
                          'candidate', 'linked', 'model_preprocessed', ?, ?)
                """,
                (source_text, timestamp, timestamp),
            )
            concept_id = cur.lastrowid
            conn.execute(
                "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                (ksa_id, concept_id, timestamp),
            )
            cur = conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                    concept_type, source_text, label_text, normalized_label_key,
                    label_role, source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, NULL, '02:02:02:01', 'knowledge', ?,
                          '협력대상국 지원 실적 및 사례', 'supportcases',
                          'short_representative_label',
                          'rule_based_short_label_candidate', 1, 0.82,
                          'candidate', ?, ?)
                """,
                (concept_id, ksa_id, source_text, timestamp, timestamp),
            )
            label_id = cur.lastrowid
            conn.commit()
            conn.close()

            blocked = review_ksa_label_candidate(
                db_path,
                {"label_id": label_id, "decision": "approve", "reviewer_id": "dashboard", "notes": ""},
            )
            self.assertFalse(blocked["ok"])
            self.assertIn("label_review_requires_explicit_human_reviewer_id", blocked["blockers"])
            self.assertIn("label_review_requires_human_note", blocked["blockers"])
            self.assertIn("label_review_approve_requires_source_decision_packet", blocked["blockers"])
            self.assertIn("label_review_approve_requires_source_artifact_hash", blocked["blockers"])
            self.assertIn("label_review_approve_requires_run_artifact", blocked["blockers"])
            self.assertIn("label_review_requires_raw_to_label_check", blocked["blockers"])

            dashboard_click_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "dashboard_click",
                    "notes": "clicked",
                    "rationale": "clicked",
                    "source_decision_packet": "ksa_review_dashboard_one_click_v1:label:test",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(dashboard_click_blocked["ok"])
            self.assertIn(
                "label_review_requires_explicit_human_reviewer_id",
                dashboard_click_blocked["blockers"],
            )
            self.assertIn(
                "label_review_approve_requires_packet_backed_source_decision_packet",
                dashboard_click_blocked["blockers"],
            )

            local_operator_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "local_operator",
                    "notes": "clicked",
                    "rationale": "clicked",
                    "source_decision_packet": "ksa_review_dashboard_one_click_v1:label:test",
                    "source_artifact_hash": "sha256:test",
                    "run_artifact": "/ksa-review-dashboard",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(local_operator_blocked["ok"])
            self.assertIn(
                "label_review_requires_explicit_human_reviewer_id",
                local_operator_blocked["blockers"],
            )
            self.assertIn(
                "label_review_approve_requires_packet_backed_source_decision_packet",
                local_operator_blocked["blockers"],
            )

            string_false_check = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "raw_to_label_checked": "false",
                },
            )
            self.assertFalse(string_false_check["ok"])
            self.assertIn("label_review_approve_requires_source_decision_packet", string_false_check["blockers"])
            self.assertIn("label_review_approve_requires_source_artifact_hash", string_false_check["blockers"])
            self.assertIn("label_review_approve_requires_run_artifact", string_false_check["blockers"])
            self.assertIn("label_review_requires_raw_to_label_check", string_false_check["blockers"])

            injected_status_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "needs_revision",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "raw_to_label_checked": True,
                    "review_status": "human_reviewed",
                    "new_status": "accepted",
                    "approval_claim": True,
                },
            )
            self.assertFalse(injected_status_blocked["ok"])
            self.assertIn(
                "label_review_rejects_status_override_review_status",
                injected_status_blocked["blockers"],
            )
            self.assertIn(
                "label_review_rejects_status_override_new_status",
                injected_status_blocked["blockers"],
            )
            self.assertIn(
                "label_review_rejects_approval_claim_approval_claim",
                injected_status_blocked["blockers"],
            )
            conn = connect(db_path)
            initialize_database(conn)
            unchanged_label_status = conn.execute(
                "SELECT review_status FROM ontology_concept_label_candidates WHERE label_id = ?",
                (label_id,),
            ).fetchone()["review_status"]
            audit_count = conn.execute(
                "SELECT COUNT(*) AS count FROM review_audit_log WHERE entity_type = 'ontology_concept_label_candidate'"
            ).fetchone()["count"]
            conn.close()
            self.assertEqual(unchanged_label_status, "candidate")
            self.assertEqual(audit_count, 0)

            synthetic_packet_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "source_decision_packet": (
                        f"ksa_review_dashboard_one_click_v1:label:{label_id}:approve:02-02-02-01"
                    ),
                    "source_artifact_hash": "sha256:" + ("0" * 64),
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(synthetic_packet_blocked["ok"])
            self.assertEqual(
                synthetic_packet_blocked["blockers"],
                ["label_review_approve_requires_packet_backed_source_decision_packet"],
            )

            forged_packet, forged_hash = self._write_raw_review_packet(
                tmp,
                "forged_label_decision_audit.json",
                json.dumps(
                    {
                        "schema": "test_review_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "rows": [
                            {
                                "reference": f"label:{label_id}:approve",
                                "decision": "approve",
                                "reviewer_id": "tester",
                                "completed": True,
                                "valid": True,
                                "action_eligible": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                f"label:{label_id}:approve",
            )
            forged_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "source_decision_packet": forged_packet,
                    "source_artifact_hash": forged_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(forged_blocked["ok"])
            self.assertIn(
                "label_review_approve_requires_audited_human_decision_artifact",
                forged_blocked["blockers"],
            )

            reviewer_mismatch_packet, reviewer_mismatch_hash = self._write_review_packet(
                tmp,
                "label_reviewer_mismatch_decision_audit.json",
                f"label:{label_id}:approve",
                reviewer_id="other_human_reviewer",
            )
            reviewer_mismatch_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "source_decision_packet": reviewer_mismatch_packet,
                    "source_artifact_hash": reviewer_mismatch_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(reviewer_mismatch_blocked["ok"])
            self.assertIn(
                "label_review_approve_requires_audited_human_decision_artifact",
                reviewer_mismatch_blocked["blockers"],
            )

            unsafe_packet, unsafe_hash = self._write_raw_review_packet(
                tmp,
                "unsafe_label_decision_audit.json",
                json.dumps(
                    {
                        "schema": "ncs_dashboard_review_decision_audit_v1",
                        "ok": True,
                        "human_decision_required": True,
                        "status_update_allowed": True,
                        "db_writes": False,
                        "approval_claim": False,
                        "rows": [
                            {
                                "reference": f"label:{label_id}:approve",
                                "decision": "approve",
                                "reviewer_id": "tester",
                                "completed": True,
                                "valid": True,
                                "action_eligible": True,
                                "status_update_allowed": False,
                                "db_writes": False,
                                "approval_claim": False,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                f"label:{label_id}:approve",
            )
            unsafe_blocked = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "source_decision_packet": unsafe_packet,
                    "source_artifact_hash": unsafe_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(unsafe_blocked["ok"])
            self.assertIn(
                "label_review_approve_requires_audited_human_decision_artifact",
                unsafe_blocked["blockers"],
            )

            label_packet, label_hash = self._write_review_packet(
                tmp,
                "ksa_label_review_decision_sheet_test.csv",
                f"label:{label_id}:approve",
            )
            approved = review_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "원문과 단어형 후보를 화면에서 확인함",
                    "rationale": "원문 핵심 의미가 보존됨",
                    "source_decision_packet": label_packet,
                    "source_artifact_hash": label_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02&middle_code=02&small_code=02&sub_code=01",
                    "raw_to_label_checked": True,
                },
            )
            self.assertTrue(approved["ok"])
            self.assertEqual(approved["previous_status"], "candidate")
            self.assertEqual(approved["new_status"], "human_reviewed")
            self.assertTrue(approved["raw_ksa_preserved"])
            self.assertTrue(approved["concept_name_preserved"])
            self.assertTrue(approved["raw_to_label_checked"])

            conn = connect(db_path)
            initialize_database(conn)
            label_row = conn.execute(
                "SELECT review_status FROM ontology_concept_label_candidates WHERE label_id = ?",
                (label_id,),
            ).fetchone()
            ksa_row = conn.execute(
                "SELECT ksa_text_raw, review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            concept_row = conn.execute(
                "SELECT concept_name, review_status FROM ontology_concepts WHERE concept_id = ?",
                (concept_id,),
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, previous_status, new_status,
                       reviewer_id, notes, source_decision_packet, rationale,
                       evidence_refs_json, created_by_tool
                FROM review_audit_log
                WHERE entity_type = 'ontology_concept_label_candidate'
                """
            ).fetchone()
            conn.close()

            self.assertEqual(label_row["review_status"], "human_reviewed")
            self.assertEqual(ksa_row["ksa_text_raw"], source_text)
            self.assertEqual(ksa_row["review_status"], "raw")
            self.assertEqual(concept_row["concept_name"], source_text)
            self.assertEqual(concept_row["review_status"], "model_preprocessed")
            self.assertEqual(audit_row["entity_id"], str(label_id))
            self.assertEqual(audit_row["action"], "ksa_label_approve")
            self.assertEqual(audit_row["previous_status"], "candidate")
            self.assertEqual(audit_row["new_status"], "human_reviewed")
            self.assertEqual(audit_row["reviewer_id"], "tester")
            self.assertEqual(audit_row["rationale"], "원문 핵심 의미가 보존됨")
            self.assertEqual(
                audit_row["source_decision_packet"],
                label_packet,
            )
            evidence_refs = json.loads(audit_row["evidence_refs_json"])
            self.assertIn(f"source_ksa_id:{ksa_id}", evidence_refs)
            self.assertIn("label_text:협력대상국 지원 실적 및 사례", evidence_refs)
            self.assertIn("raw_to_label_checked:true", evidence_refs)
            self.assertEqual(audit_row["created_by_tool"], "ncs_dashboard.review_ksa_label_candidate")

            reviewed_rows = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["협력대상국"],
                    "label_review_status": ["human_reviewed"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(reviewed_rows["ok"])
            self.assertEqual(reviewed_rows["summary"]["matching_ksa"], 1)
            self.assertEqual(reviewed_rows["label_review_progress"]["total"], 1)
            self.assertEqual(reviewed_rows["label_review_progress"]["human_reviewed"], 1)
            self.assertEqual(reviewed_rows["label_review_progress"]["pending"], 0)
            self.assertEqual(reviewed_rows["label_review_progress"]["human_reviewed_percent"], 100.0)
            self.assertEqual(reviewed_rows["items"][0]["short_label_review_status"], "human_reviewed")
            self.assertEqual(reviewed_rows["items"][0]["short_label_last_reviewer_id"], "tester")
            self.assertEqual(
                reviewed_rows["items"][0]["short_label_last_review_note"],
                "원문과 단어형 후보를 화면에서 확인함",
            )
            self.assertEqual(
                reviewed_rows["items"][0]["short_label_last_review_rationale"],
                "원문 핵심 의미가 보존됨",
            )
            self.assertEqual(reviewed_rows["items"][0]["short_label_last_review_action"], "ksa_label_approve")
            self.assertEqual(reviewed_rows["items"][0]["short_label_last_review_status"], "human_reviewed")
            self.assertTrue(reviewed_rows["items"][0]["short_label_last_reviewed_at"])
            self.assertEqual(reviewed_rows["items"][0]["short_label_transform_state"], "shortened")
            self.assertEqual(reviewed_rows["items"][0]["short_label_source_length"], len(source_text))
            self.assertEqual(
                reviewed_rows["items"][0]["short_label_label_length"],
                len("협력대상국 지원 실적 및 사례"),
            )
            self.assertEqual(
                reviewed_rows["items"][0]["short_label_removed_char_count"],
                len(source_text) - len("협력대상국 지원 실적 및 사례"),
            )
            self.assertAlmostEqual(
                reviewed_rows["items"][0]["short_label_length_ratio"],
                round(len("협력대상국 지원 실적 및 사례") / len(source_text), 3),
            )
            self.assertEqual(
                reviewed_rows["items"][0]["short_label_candidate"],
                "협력대상국 지원 실적 및 사례",
            )

            fake_edit_blocked = edit_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "corrected_label_text": "support cases",
                    "reviewer_id": "tester",
                    "notes": "edit review note",
                    "rationale": "edit review rationale",
                    "source_decision_packet": (
                        f"{Path(tmp) / 'reports' / 'missing_edit_packet.csv'}#label:{label_id}:edit_approve"
                    ),
                    "source_artifact_hash": "sha256:" + ("1" * 64),
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertFalse(fake_edit_blocked["ok"])
            self.assertIn(
                "label_edit_requires_packet_backed_source_decision_packet",
                fake_edit_blocked["blockers"],
            )

            conn = connect(db_path)
            initialize_database(conn)
            label_text_before_blocked_edit = conn.execute(
                "SELECT label_text FROM ontology_concept_label_candidates WHERE label_id = ?",
                (label_id,),
            ).fetchone()["label_text"]
            conn.close()
            injected_edit_status_blocked = edit_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "corrected_label_text": "support cases",
                    "reviewer_id": "tester",
                    "notes": "edit review note",
                    "rationale": "edit review rationale",
                    "source_decision_packet": label_packet,
                    "source_artifact_hash": label_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                    "target_review_status": "human_reviewed",
                    "accepted": True,
                },
            )
            self.assertFalse(injected_edit_status_blocked["ok"])
            self.assertIn(
                "label_edit_rejects_status_override_target_review_status",
                injected_edit_status_blocked["blockers"],
            )
            self.assertIn(
                "label_edit_rejects_approval_claim_accepted",
                injected_edit_status_blocked["blockers"],
            )
            conn = connect(db_path)
            initialize_database(conn)
            label_after_blocked_edit = conn.execute(
                "SELECT label_text FROM ontology_concept_label_candidates WHERE label_id = ?",
                (label_id,),
            ).fetchone()["label_text"]
            conn.close()
            self.assertEqual(label_after_blocked_edit, label_text_before_blocked_edit)

            edit_packet, edit_hash = self._write_review_packet(
                tmp,
                "ksa_label_edit_decision_sheet_test.csv",
                f"label:{label_id}:edit_approve",
            )
            edited = edit_ksa_label_candidate(
                db_path,
                {
                    "label_id": label_id,
                    "corrected_label_text": "support cases",
                    "reviewer_id": "tester",
                    "notes": "edit review note",
                    "rationale": "edit review rationale",
                    "source_decision_packet": edit_packet,
                    "source_artifact_hash": edit_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_label_checked": True,
                },
            )
            self.assertTrue(edited["ok"])
            self.assertEqual(edited["new_status"], "human_reviewed")
            self.assertEqual(edited["corrected_label_text"], "support cases")

    def test_ksa_meaning_candidate_review_requires_human_input_and_preserves_concept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'Planning')
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
                (classification_id, timestamp, timestamp),
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
                INSERT INTO performance_criteria(
                    element_id, criteria_no, criteria_text_raw
                ) VALUES (?, '1', 'Design workforce interview criteria.')
                """,
                (element_id,),
            )
            criteria_id = cur.lastrowid
            source_text = "structured interview skill"
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name,
                    ksa_no, ksa_text_raw
                ) VALUES (?, 'S', 'skill', '1', ?)
                """,
                (element_id, source_text),
            )
            ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition,
                    definition_source, definition_status, relation_status,
                    review_status, created_at, updated_at
                ) VALUES (?, 'structuredinterviewskill', 'skill',
                          'Candidate definition text.',
                          'ksa_meaning_candidates.term_definition_template',
                          'candidate', 'linked', 'model_preprocessed', ?, ?)
                """,
                (source_text, timestamp, timestamp),
            )
            concept_id = cur.lastrowid
            conn.execute(
                "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'candidate', ?)",
                (ksa_id, concept_id, timestamp),
            )
            cur = conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'skill', 'term_definition_candidate',
                          'Concise definition for structured interview skill.',
                          'term_definition_template',
                          'raw KSA and criteria evidence visible',
                          '0202020101_26v1', ?, ?, ?, 0.91,
                          'candidate', ?, ?)
                """,
                (concept_id, element_id, criteria_id, ksa_id, timestamp, timestamp),
            )
            meaning_id = cur.lastrowid
            conn.commit()
            conn.close()

            blocked = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "dashboard",
                    "notes": "",
                },
            )
            self.assertFalse(blocked["ok"])
            self.assertIn("meaning_review_requires_explicit_human_reviewer_id", blocked["blockers"])
            self.assertIn("meaning_review_requires_human_note", blocked["blockers"])
            self.assertIn("meaning_review_approve_requires_source_decision_packet", blocked["blockers"])
            self.assertIn("meaning_review_approve_requires_source_artifact_hash", blocked["blockers"])
            self.assertIn("meaning_review_approve_requires_run_artifact", blocked["blockers"])
            self.assertIn("meaning_review_requires_raw_to_meaning_check", blocked["blockers"])

            dashboard_click_blocked = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "dashboard_click",
                    "notes": "clicked",
                    "rationale": "clicked",
                    "source_decision_packet": "ksa_review_dashboard_one_click_v1:meaning:test",
                    "raw_to_meaning_checked": True,
                },
            )
            self.assertFalse(dashboard_click_blocked["ok"])
            self.assertIn(
                "meaning_review_requires_explicit_human_reviewer_id",
                dashboard_click_blocked["blockers"],
            )
            self.assertIn(
                "meaning_review_approve_requires_packet_backed_source_decision_packet",
                dashboard_click_blocked["blockers"],
            )

            local_operator_blocked = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "local_operator",
                    "notes": "clicked",
                    "rationale": "clicked",
                    "source_decision_packet": "ksa_review_dashboard_one_click_v1:meaning:test",
                    "source_artifact_hash": "sha256:test",
                    "run_artifact": "/ksa-review-dashboard",
                    "raw_to_meaning_checked": True,
                },
            )
            self.assertFalse(local_operator_blocked["ok"])
            self.assertIn(
                "meaning_review_requires_explicit_human_reviewer_id",
                local_operator_blocked["blockers"],
            )
            self.assertIn(
                "meaning_review_approve_requires_packet_backed_source_decision_packet",
                local_operator_blocked["blockers"],
            )

            synthetic_packet_blocked = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "source_decision_packet": (
                        f"ksa_review_dashboard_one_click_v1:meaning:{meaning_id}:approve:02-02-02-01"
                    ),
                    "source_artifact_hash": "sha256:" + ("0" * 64),
                    "run_artifact": "/ksa-review-dashboard?major_code=02",
                    "raw_to_meaning_checked": True,
                },
            )
            self.assertFalse(synthetic_packet_blocked["ok"])
            self.assertEqual(
                synthetic_packet_blocked["blockers"],
                ["meaning_review_approve_requires_packet_backed_source_decision_packet"],
            )

            conn = connect(db_path)
            initialize_database(conn)
            cur = conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, ksa_id, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, 'skill', 'term_definition_candidate',
                          'Unscoped definition candidate.',
                          'term_definition_template_unscoped', '', ?, 0.5,
                          'candidate', ?, ?)
                """,
                (concept_id, ksa_id, timestamp, timestamp),
            )
            unscoped_meaning_id = cur.lastrowid
            conn.commit()
            conn.close()
            unscoped_packet, unscoped_hash = self._write_review_packet(
                tmp,
                "ksa_meaning_review_unscoped_decision_sheet_test.csv",
                f"meaning:{unscoped_meaning_id}:approve",
            )
            unscoped_blocked = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": unscoped_meaning_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "clicked",
                    "rationale": "clicked",
                    "source_decision_packet": unscoped_packet,
                    "source_artifact_hash": unscoped_hash,
                    "run_artifact": "/ksa-review-dashboard",
                    "raw_to_meaning_checked": True,
                },
            )
            self.assertFalse(unscoped_blocked["ok"])
            self.assertIn(
                "meaning_review_approve_requires_task_scope",
                unscoped_blocked["blockers"],
            )

            string_false_check = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "review note",
                    "rationale": "review rationale",
                    "raw_to_meaning_checked": "false",
                },
            )
            self.assertFalse(string_false_check["ok"])
            self.assertIn("meaning_review_approve_requires_source_decision_packet", string_false_check["blockers"])
            self.assertIn("meaning_review_approve_requires_source_artifact_hash", string_false_check["blockers"])
            self.assertIn("meaning_review_approve_requires_run_artifact", string_false_check["blockers"])
            self.assertIn("meaning_review_requires_raw_to_meaning_check", string_false_check["blockers"])

            meaning_packet, meaning_hash = self._write_review_packet(
                tmp,
                "ksa_meaning_review_decision_sheet_test.csv",
                f"meaning:{meaning_id}:approve",
            )
            approved = review_ksa_meaning_candidate(
                db_path,
                {
                    "meaning_id": meaning_id,
                    "decision": "approve",
                    "reviewer_id": "tester",
                    "notes": "raw KSA, term definition, and criteria evidence checked",
                    "rationale": "definition preserves the source KSA meaning",
                    "source_decision_packet": meaning_packet,
                    "source_artifact_hash": meaning_hash,
                    "run_artifact": "/ksa-review-dashboard?major_code=02&middle_code=02&small_code=02&sub_code=01",
                    "raw_to_meaning_checked": True,
                },
            )
            self.assertTrue(approved["ok"])
            self.assertEqual(approved["previous_status"], "candidate")
            self.assertEqual(approved["new_status"], "human_reviewed")
            self.assertTrue(approved["raw_ksa_preserved"])
            self.assertTrue(approved["concept_definition_status_preserved"])

            conn = connect(db_path)
            initialize_database(conn)
            meaning_row = conn.execute(
                "SELECT review_status FROM ksa_meaning_candidates WHERE meaning_id = ?",
                (meaning_id,),
            ).fetchone()
            ksa_row = conn.execute(
                "SELECT ksa_text_raw, review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            concept_row = conn.execute(
                "SELECT definition_status, review_status FROM ontology_concepts WHERE concept_id = ?",
                (concept_id,),
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, previous_status, new_status,
                       reviewer_id, notes, source_decision_packet, rationale,
                       evidence_refs_json, created_by_tool
                FROM review_audit_log
                WHERE entity_type = 'ksa_meaning_candidate'
                """
            ).fetchone()
            conn.close()

            self.assertEqual(meaning_row["review_status"], "human_reviewed")
            self.assertEqual(ksa_row["ksa_text_raw"], source_text)
            self.assertEqual(ksa_row["review_status"], "raw")
            self.assertEqual(concept_row["definition_status"], "candidate")
            self.assertEqual(concept_row["review_status"], "model_preprocessed")
            self.assertEqual(audit_row["entity_id"], str(meaning_id))
            self.assertEqual(audit_row["action"], "ksa_meaning_approve")
            self.assertEqual(audit_row["previous_status"], "candidate")
            self.assertEqual(audit_row["new_status"], "human_reviewed")
            self.assertEqual(audit_row["reviewer_id"], "tester")
            self.assertEqual(audit_row["rationale"], "definition preserves the source KSA meaning")
            self.assertEqual(
                audit_row["source_decision_packet"],
                meaning_packet,
            )
            evidence_refs = json.loads(audit_row["evidence_refs_json"])
            self.assertIn(f"meaning_id:{meaning_id}", evidence_refs)
            self.assertIn(f"ksa_id:{ksa_id}", evidence_refs)
            self.assertIn("raw_to_meaning_checked:true", evidence_refs)
            self.assertEqual(audit_row["created_by_tool"], "ncs_dashboard.review_ksa_meaning_candidate")

            reviewed_rows = get_ksa_definitions(
                db_path,
                {
                    "keyword": ["structured interview"],
                    "meaning_review_status": ["human_reviewed"],
                    "limit": ["5"],
                },
            )
            self.assertTrue(reviewed_rows["ok"])
            self.assertEqual(reviewed_rows["summary"]["matching_ksa"], 1)
            self.assertEqual(reviewed_rows["items"][0]["term_definition_review_status"], "human_reviewed")
            self.assertEqual(reviewed_rows["items"][0]["term_definition_meaning_id"], meaning_id)
            self.assertEqual(reviewed_rows["items"][0]["term_definition_last_reviewer_id"], "tester")
            self.assertEqual(
                reviewed_rows["items"][0]["term_definition_last_review_note"],
                "raw KSA, term definition, and criteria evidence checked",
            )
            self.assertEqual(
                reviewed_rows["items"][0]["term_definition_last_review_rationale"],
                "definition preserves the source KSA meaning",
            )

    def test_manual_preprocess_creates_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사", "raw"),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.commit()
            conn.close()

            packet, packet_hash = self._write_review_packet(
                tmp,
                "manual_classification_preprocess_packet.jsonl",
                f"classification:{classification_id}",
                extra="decision,manual_preprocess\n",
            )
            result = save_manual_preprocess(
                db_path,
                {
                    "kind": "classification",
                    "id": classification_id,
                    "body_refined": "인사 직무 정의",
                    "reviewer_id": "tester",
                    "notes": "manual review",
                    "source_decision_packet": packet,
                    "source_artifact_hash": packet_hash,
                    "rationale": "직무 정의와 교육체계 보고서 기준을 사람이 확인함",
                    "evidence_refs": ["classification:02-02-02-01", "guide:C1-1"],
                    "run_artifact": "reports/manual_preprocess_run.json",
                },
            )
            self.assertTrue(result["ok"])

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute(
                "SELECT duty_def_refined, review_status FROM classifications WHERE classification_id = ?",
                (classification_id,),
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, previous_status, new_status, reviewer_id, notes,
                       source_decision_packet, source_artifact_hash, rationale, evidence_refs_json,
                       created_by_tool, run_artifact
                FROM review_audit_log
                WHERE action = 'save_manual_preprocess'
                """
            ).fetchone()
            conn.close()

            self.assertEqual(row["duty_def_refined"], "인사 직무 정의")
            self.assertEqual(row["review_status"], "human_reviewed")
            self.assertEqual(audit_row["entity_type"], "classification")
            self.assertEqual(audit_row["entity_id"], str(classification_id))
            self.assertEqual(audit_row["previous_status"], "raw")
            self.assertEqual(audit_row["new_status"], "human_reviewed")
            self.assertEqual(audit_row["reviewer_id"], "tester")
            self.assertEqual(audit_row["notes"], "manual review")
            self.assertEqual(audit_row["source_decision_packet"], packet)
            self.assertEqual(audit_row["source_artifact_hash"], packet_hash)
            self.assertEqual(audit_row["rationale"], "직무 정의와 교육체계 보고서 기준을 사람이 확인함")
            self.assertEqual(
                json.loads(audit_row["evidence_refs_json"]),
                ["classification:02-02-02-01", "guide:C1-1"],
            )
            self.assertEqual(audit_row["created_by_tool"], "ncs_dashboard.save_manual_preprocess")
            self.assertEqual(audit_row["run_artifact"], "reports/manual_preprocess_run.json")

    def test_manual_preprocess_blocks_human_reviewed_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사", "raw"),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.commit()
            conn.close()

            result = save_manual_preprocess(
                db_path,
                {
                    "kind": "classification",
                    "id": classification_id,
                    "body_refined": "인사 직무 정의",
                },
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "trusted_status_requires_provenance")
            self.assertIn("trusted_status_requires_explicit_human_reviewer_id", result["blockers"])
            self.assertIn("trusted_status_requires_source_decision_packet", result["blockers"])
            self.assertIn("trusted_status_requires_rationale", result["blockers"])

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute(
                "SELECT duty_def_refined, review_status FROM classifications WHERE classification_id = ?",
                (classification_id,),
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()

            self.assertIsNone(row["duty_def_refined"])
            self.assertEqual(row["review_status"], "raw")
            self.assertEqual(audit_count, 0)

    def test_manual_preprocess_blocks_packet_without_target_token_for_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "business", "02", "hr", "02", "organization", "01", "hr", "raw"),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.commit()
            conn.close()

            unrelated_packet, unrelated_packet_hash = self._write_review_packet(
                tmp,
                "manual_classification_unrelated_packet.jsonl",
                "classification:999999",
                extra="decision,manual_preprocess\n",
            )
            result = save_manual_preprocess(
                db_path,
                {
                    "kind": "classification",
                    "id": classification_id,
                    "body_refined": "HR job definition",
                    "reviewer_id": "tester",
                    "source_decision_packet": unrelated_packet,
                    "source_artifact_hash": unrelated_packet_hash,
                    "rationale": "human checked classification definition",
                },
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "trusted_status_requires_provenance")
            self.assertIn("trusted_status_requires_packet_row_for_decision", result["blockers"])

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute(
                "SELECT duty_def_refined, review_status FROM classifications WHERE classification_id = ?",
                (classification_id,),
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()

            self.assertIsNone(row["duty_def_refined"])
            self.assertEqual(row["review_status"], "raw")
            self.assertEqual(audit_count, 0)

    def test_manual_preprocess_ksa_requires_packet_backed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "business", "02", "hr", "02", "organization", "01", "hr"),
            )
            classification_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "0202020101_26v1",
                    "0202020101",
                    "26v1",
                    "HR planning",
                    "4",
                    classification_id,
                    "matched",
                    timestamp,
                    timestamp,
                ),
            )
            cur = conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw,
                    element_name_raw, element_level_raw
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("0202020101_26v1", "1", "01", "Workforce plan", "4"),
            )
            element_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name,
                    ksa_no, ksa_text_raw, review_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (element_id, "K", "knowledge", "1", "workforce planning law", "raw"),
            )
            ksa_id = cur.lastrowid
            conn.commit()
            conn.close()

            missing_packet = save_manual_preprocess(
                db_path,
                {
                    "kind": "ksa",
                    "id": ksa_id,
                    "title_refined": "workforce planning law",
                    "body_refined": "Reviewed definition.",
                    "reviewer_id": "tester",
                    "source_decision_packet": f"reports/missing_packet.jsonl#ksa_id:{ksa_id}",
                    "source_artifact_hash": "sha256:" + ("0" * 64),
                    "rationale": "human checked KSA definition and relation",
                },
            )
            self.assertFalse(missing_packet["ok"])
            self.assertIn(
                "ksa_manual_preprocess_requires_packet_backed_source_decision_packet",
                missing_packet["blockers"],
            )

            packet, packet_hash = self._write_review_packet(
                tmp,
                "manual_ksa_preprocess_packet.jsonl",
                f"ksa_id:{ksa_id}",
                extra="decision,manual_preprocess\n",
            )
            hash_mismatch = save_manual_preprocess(
                db_path,
                {
                    "kind": "ksa",
                    "id": ksa_id,
                    "title_refined": "workforce planning law",
                    "body_refined": "Reviewed definition.",
                    "reviewer_id": "tester",
                    "source_decision_packet": packet,
                    "source_artifact_hash": "sha256:" + ("1" * 64),
                    "rationale": "human checked KSA definition and relation",
                },
            )
            self.assertFalse(hash_mismatch["ok"])
            self.assertIn(
                "ksa_manual_preprocess_requires_matching_source_artifact_hash",
                hash_mismatch["blockers"],
            )

            conn = connect(db_path)
            initialize_database(conn)
            unchanged = conn.execute(
                "SELECT review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()
            self.assertEqual(unchanged["review_status"], "raw")
            self.assertEqual(audit_count, 0)

            result = save_manual_preprocess(
                db_path,
                {
                    "kind": "ksa",
                    "id": ksa_id,
                    "title_refined": "workforce planning law",
                    "body_refined": "Reviewed definition.",
                    "reviewer_id": "tester",
                    "notes": "manual KSA review",
                    "source_decision_packet": packet,
                    "source_artifact_hash": packet_hash,
                    "rationale": "human checked KSA definition and relation",
                    "evidence_refs": [f"ksa_id:{ksa_id}"],
                },
            )
            self.assertTrue(result["ok"])

            conn = connect(db_path)
            initialize_database(conn)
            reviewed = conn.execute(
                "SELECT ksa_text_refined, review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, source_decision_packet, source_artifact_hash,
                       rationale, evidence_refs_json
                FROM review_audit_log
                WHERE action = 'save_manual_preprocess'
                """
            ).fetchone()
            conn.close()

            self.assertEqual(reviewed["ksa_text_refined"], "workforce planning law")
            self.assertEqual(reviewed["review_status"], "human_reviewed")
            self.assertEqual(audit_row["entity_type"], "ksa_item")
            self.assertEqual(audit_row["entity_id"], str(ksa_id))
            self.assertEqual(audit_row["source_decision_packet"], packet)
            self.assertEqual(audit_row["source_artifact_hash"], packet_hash)
            self.assertEqual(audit_row["rationale"], "human checked KSA definition and relation")
            self.assertEqual(json.loads(audit_row["evidence_refs_json"]), [f"ksa_id:{ksa_id}"])

    def test_dashboard_refinement_review_applies_to_refined_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사"),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()["classification_id"]
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("0202020101_23v3", "0202020101", "23v3", "인사기획", "6", classification_id, "matched", timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(unit_code, element_no, element_code_raw, element_name_raw, element_level_raw)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("0202020101_23v3", "1", "0202020101_23v3 1", "인사전략 수립하기", "6"),
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, ?, ?)",
                (element_id, "1", "인사전략  환경을 분석할 수 있다"),
            )
            criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()["criteria_id"]
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=criteria_id,
                issue_type="double_space",
                severity="info",
                issue_detail="공백",
            )
            issue_id = conn.execute("SELECT issue_id FROM quality_issues").fetchone()["issue_id"]
            conn.execute(
                """
                INSERT INTO refinement_jobs(
                    target_type, target_id, source_issue_id, model_name, prompt_version,
                    input_hash, raw_text, refined_text, rationale, confidence,
                    output_text, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "criteria",
                    str(criteria_id),
                    issue_id,
                    "jsonl-import",
                    "test",
                    "hash",
                    "인사전략  환경을 분석할 수 있다",
                    "인사전략 환경을 분석할 수 있다.",
                    "공백과 문장부호 보정",
                    0.8,
                    "{}",
                    "review_required",
                    timestamp,
                ),
            )
            job_id = conn.execute("SELECT job_id FROM refinement_jobs").fetchone()["job_id"]
            conn.commit()
            conn.close()

            blocked = review_refinement_job(
                db_path,
                {"job_id": job_id, "action": "approve_refined", "reviewer_id": "tester"},
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"], "refinement_review_requires_human_decision_packet")
            self.assertIn("trusted_status_requires_source_decision_packet", blocked["blockers"])
            self.assertIn("refinement_review_requires_source_artifact_hash", blocked["blockers"])
            self.assertIn("trusted_status_requires_rationale", blocked["blockers"])
            self.assertFalse(blocked["status_update_allowed"])

            conn = connect(db_path)
            initialize_database(conn)
            unchanged = conn.execute(
                "SELECT criteria_text_refined, review_status FROM performance_criteria"
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()

            self.assertIsNone(unchanged["criteria_text_refined"])
            self.assertEqual(unchanged["review_status"], "raw")
            self.assertEqual(audit_count, 0)

            packet_without_target, packet_without_target_hash = self._write_review_packet(
                tmp,
                "refinement_decision_packet_without_target.jsonl",
                f"refinement_job:{job_id}",
                extra="decision,approve_refined\n",
            )
            target_blocked = review_refinement_job(
                db_path,
                {
                    "job_id": job_id,
                    "action": "approve_refined",
                    "reviewer_id": "tester",
                    "notes": "manual refinement approval",
                    "source_decision_packet": packet_without_target,
                    "source_artifact_hash": packet_without_target_hash,
                    "rationale": "source evidence and refined text were manually checked",
                    "run_artifact": "reports/refinement_review_run.json",
                },
            )
            self.assertFalse(target_blocked["ok"])
            self.assertIn(
                "refinement_review_requires_packet_row_for_criteria_decision",
                target_blocked["blockers"],
            )
            self.assertFalse(target_blocked["status_update_allowed"])

            conn = connect(db_path)
            initialize_database(conn)
            unchanged = conn.execute(
                "SELECT criteria_text_refined, review_status FROM performance_criteria"
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()

            self.assertIsNone(unchanged["criteria_text_refined"])
            self.assertEqual(unchanged["review_status"], "raw")
            self.assertEqual(audit_count, 0)

            packet, packet_hash = self._write_review_packet(
                tmp,
                "refinement_decision_packet.jsonl",
                f"refinement_job:{job_id}",
                extra=f"criteria:{criteria_id}\ndecision,approve_refined\n",
            )
            result = review_refinement_job(
                db_path,
                {
                    "job_id": job_id,
                    "action": "approve_refined",
                    "reviewer_id": "tester",
                    "notes": "manual refinement approval",
                    "source_decision_packet": packet,
                    "source_artifact_hash": packet_hash,
                    "rationale": "source evidence and refined text were manually checked",
                    "evidence_refs": ["criteria:manual-check", "refinement_job:test"],
                    "run_artifact": "reports/refinement_review_run.json",
                },
            )
            self.assertEqual(result["new_status"], "applied")
            self.assertTrue(result["status_update_allowed"])
            self.assertFalse(result["automatic_status_update_allowed"])
            self.assertTrue(result["human_decision_packet_required"])

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute("SELECT criteria_text_refined, review_status FROM performance_criteria").fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, previous_status, new_status, reviewer_id, notes,
                       source_decision_packet, source_artifact_hash, rationale, evidence_refs_json,
                       created_by_tool, run_artifact
                FROM review_audit_log
                WHERE action = 'approve_refined'
                """
            ).fetchone()
            self.assertEqual(row["criteria_text_refined"], "인사전략 환경을 분석할 수 있다.")
            self.assertEqual(row["review_status"], "human_reviewed")
            self.assertEqual(audit_row["entity_type"], "refinement_job")
            self.assertEqual(audit_row["entity_id"], str(job_id))
            self.assertEqual(audit_row["previous_status"], "review_required")
            self.assertEqual(audit_row["new_status"], "applied")
            self.assertEqual(audit_row["reviewer_id"], "tester")
            self.assertEqual(audit_row["notes"], "manual refinement approval")
            self.assertEqual(audit_row["source_decision_packet"], packet)
            self.assertEqual(audit_row["source_artifact_hash"], packet_hash)
            self.assertEqual(audit_row["rationale"], "source evidence and refined text were manually checked")
            self.assertEqual(
                json.loads(audit_row["evidence_refs_json"]),
                ["criteria:manual-check", "refinement_job:test"],
            )
            self.assertEqual(audit_row["created_by_tool"], "ncs_dashboard.review_refinement_job")
            self.assertEqual(audit_row["run_artifact"], "reports/refinement_review_run.json")
            conn.close()

    def test_dashboard_refinement_review_ksa_requires_target_packet_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "business", "02", "hr", "02", "organization", "01", "hr"),
            )
            classification_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "0202020101_26v1",
                    "0202020101",
                    "26v1",
                    "HR planning",
                    "4",
                    classification_id,
                    "matched",
                    timestamp,
                    timestamp,
                ),
            )
            cur = conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw,
                    element_name_raw, element_level_raw
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("0202020101_26v1", "1", "01", "Workforce plan", "4"),
            )
            element_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name,
                    ksa_no, ksa_text_raw, review_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (element_id, "K", "knowledge", "1", "workforce planning law", "raw"),
            )
            ksa_id = cur.lastrowid
            insert_quality_issue(
                conn,
                target_type="ksa",
                target_id=ksa_id,
                issue_type="short_ksa",
                severity="info",
                issue_detail="short KSA",
            )
            issue_id = conn.execute("SELECT issue_id FROM quality_issues").fetchone()["issue_id"]
            conn.execute(
                """
                INSERT INTO refinement_jobs(
                    target_type, target_id, source_issue_id, model_name, prompt_version,
                    input_hash, raw_text, refined_text, rationale, confidence,
                    output_text, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ksa",
                    str(ksa_id),
                    issue_id,
                    "jsonl-import",
                    "test",
                    "hash",
                    "workforce planning law",
                    "workforce planning law knowledge",
                    "human-review required",
                    0.8,
                    "{}",
                    "review_required",
                    timestamp,
                ),
            )
            job_id = conn.execute("SELECT job_id FROM refinement_jobs").fetchone()["job_id"]
            conn.commit()
            conn.close()

            packet_without_target, packet_without_target_hash = self._write_review_packet(
                tmp,
                "ksa_refinement_without_target.jsonl",
                f"refinement_job:{job_id}",
                extra="decision,approve_refined\n",
            )
            blocked = review_refinement_job(
                db_path,
                {
                    "job_id": job_id,
                    "action": "approve_refined",
                    "reviewer_id": "tester",
                    "notes": "manual refinement approval",
                    "source_decision_packet": packet_without_target,
                    "source_artifact_hash": packet_without_target_hash,
                    "rationale": "source evidence and refined text were manually checked",
                    "run_artifact": "reports/refinement_review_run.json",
                },
            )
            self.assertFalse(blocked["ok"])
            self.assertIn(
                "refinement_review_requires_packet_row_for_ksa_decision",
                blocked["blockers"],
            )
            self.assertFalse(blocked["status_update_allowed"])

            conn = connect(db_path)
            initialize_database(conn)
            unchanged = conn.execute(
                "SELECT ksa_text_refined, review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            conn.close()
            self.assertIsNone(unchanged["ksa_text_refined"])
            self.assertEqual(unchanged["review_status"], "raw")
            self.assertEqual(audit_count, 0)

            packet, packet_hash = self._write_review_packet(
                tmp,
                "ksa_refinement_decision_packet.jsonl",
                f"ksa_id:{ksa_id}",
                extra=f"refinement_job:{job_id}\ndecision,approve_refined\n",
            )
            result = review_refinement_job(
                db_path,
                {
                    "job_id": job_id,
                    "action": "approve_refined",
                    "reviewer_id": "tester",
                    "notes": "manual KSA refinement approval",
                    "source_decision_packet": packet,
                    "source_artifact_hash": packet_hash,
                    "rationale": "source KSA and refined label were manually checked",
                    "evidence_refs": [f"ksa_id:{ksa_id}", f"refinement_job:{job_id}"],
                    "run_artifact": "reports/refinement_review_run.json",
                },
            )
            self.assertEqual(result["new_status"], "applied")
            self.assertTrue(result["status_update_allowed"])
            self.assertFalse(result["automatic_status_update_allowed"])

            conn = connect(db_path)
            initialize_database(conn)
            reviewed = conn.execute(
                "SELECT ksa_text_refined, review_status FROM ksa_items WHERE ksa_id = ?",
                (ksa_id,),
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, source_decision_packet,
                       source_artifact_hash, evidence_refs_json
                FROM review_audit_log
                WHERE action = 'approve_refined'
                """
            ).fetchone()
            conn.close()

            self.assertEqual(reviewed["ksa_text_refined"], "workforce planning law knowledge")
            self.assertEqual(reviewed["review_status"], "human_reviewed")
            self.assertEqual(audit_row["entity_type"], "refinement_job")
            self.assertEqual(audit_row["entity_id"], str(job_id))
            self.assertEqual(audit_row["source_decision_packet"], packet)
            self.assertEqual(audit_row["source_artifact_hash"], packet_hash)
            self.assertEqual(json.loads(audit_row["evidence_refs_json"]), [f"ksa_id:{ksa_id}", f"refinement_job:{job_id}"])


if __name__ == "__main__":
    unittest.main()
