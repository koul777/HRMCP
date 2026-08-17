from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ncs_mcp.llm_preprocessing_backlog import (
    build_llm_preprocessing_backlog_map,
    build_llm_preprocessing_runbook,
    build_llm_preprocessing_work_plan,
    write_llm_preprocessing_backlog_markdown,
    write_llm_preprocessing_runbook_markdown,
    write_llm_preprocessing_work_plan_markdown,
)
from scripts.ncs_harness import main as harness_main  # noqa: E402


def _seed_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE classifications (
            major_code TEXT NOT NULL,
            major_name TEXT NOT NULL
        );
        CREATE TABLE ksa_items (
            ksa_id INTEGER PRIMARY KEY,
            review_status TEXT NOT NULL DEFAULT 'raw'
        );
        CREATE TABLE ksa_atomic_items (
            atomic_id INTEGER PRIMARY KEY,
            review_status TEXT NOT NULL DEFAULT 'raw'
        );
        CREATE TABLE ontology_concepts (
            concept_id INTEGER PRIMARY KEY,
            concept_name TEXT NOT NULL,
            definition_status TEXT NOT NULL,
            definition_source TEXT,
            review_status TEXT NOT NULL
        );
        CREATE TABLE ontology_concept_aliases (
            alias_id INTEGER PRIMARY KEY
        );
        CREATE TABLE criteria_concept_links (
            link_id INTEGER PRIMARY KEY
        );
        CREATE TABLE ksa_concept_links (
            link_id INTEGER PRIMARY KEY
        );
        CREATE TABLE ksa_atomic_concept_links (
            link_id INTEGER PRIMARY KEY
        );
        CREATE TABLE ontology_concept_label_candidates (
            label_id INTEGER PRIMARY KEY,
            concept_id INTEGER NOT NULL,
            source_scope_key TEXT NOT NULL,
            normalized_label_key TEXT NOT NULL,
            source_method TEXT NOT NULL,
            review_status TEXT
        );
        CREATE TABLE ksa_meaning_candidates (
            meaning_id INTEGER PRIMARY KEY,
            concept_id INTEGER NOT NULL,
            meaning_role TEXT NOT NULL,
            source_method TEXT NOT NULL,
            review_status TEXT NOT NULL
        );
        CREATE TABLE task_ksa_concept_relations (
            relation_id INTEGER PRIMARY KEY,
            relation_type TEXT NOT NULL,
            review_status TEXT NOT NULL
        );
        CREATE TABLE training_goal_concept_links (
            link_id INTEGER PRIMARY KEY,
            link_method TEXT NOT NULL,
            review_status TEXT NOT NULL
        );
        CREATE TABLE ncs_training_course_concept_links (
            link_id INTEGER PRIMARY KEY,
            link_method TEXT NOT NULL,
            review_status TEXT NOT NULL
        );
        CREATE TABLE quality_issues (
            issue_id INTEGER PRIMARY KEY,
            target_type TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE ncs_query_aliases (
            alias_id INTEGER PRIMARY KEY,
            source_method TEXT NOT NULL,
            review_status TEXT NOT NULL
        );
        CREATE TABLE review_audit_log (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL,
            action TEXT NOT NULL,
            new_status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO classifications(major_code, major_name) VALUES (?, ?)",
        [("01", "business"), ("02", "hr")],
    )
    conn.executemany(
        "INSERT INTO ksa_items(ksa_id, review_status) VALUES (?, ?)",
        [(1, "raw"), (2, "model_refined")],
    )
    conn.executemany(
        "INSERT INTO ksa_atomic_items(atomic_id, review_status) VALUES (?, ?)",
        [(1, "raw"), (2, "raw"), (3, "raw")],
    )
    conn.executemany(
        """
        INSERT INTO ontology_concepts(
            concept_id, concept_name, definition_status, definition_source, review_status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "concept one", "candidate", "ksa_meaning_candidates.term_definition_template", "model_preprocessed"),
            (2, "concept two", "missing", None, "raw"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO ontology_concept_label_candidates(
            label_id, concept_id, source_scope_key, normalized_label_key,
            source_method, review_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "01:01:01:01", "labelone", "already_short_label", "human_reviewed"),
            (2, 1, "01:01:01:01", "labelone", "already_short_label", "llm_reviewed"),
            (3, 2, "02:02:02:02", "labeltwo", "rule_based_short_label_candidate", "needs_review"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO ksa_meaning_candidates(
            meaning_id, concept_id, meaning_role, source_method, review_status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "term_definition_candidate", "term_definition_template", "needs_review"),
            (2, 1, "task_skill_significance", "task_context_template", "llm_reviewed"),
        ],
    )
    conn.executemany(
        "INSERT INTO task_ksa_concept_relations(relation_id, relation_type, review_status) VALUES (?, ?, ?)",
        [(1, "knowledge_enables_skill", "candidate")],
    )
    conn.executemany(
        "INSERT INTO training_goal_concept_links(link_id, link_method, review_status) VALUES (?, ?, ?)",
        [(1, "training_goal_concept_token", "auto_linked")],
    )
    conn.executemany(
        "INSERT INTO ncs_training_course_concept_links(link_id, link_method, review_status) VALUES (?, ?, ?)",
        [(1, "unit_ksa_concept_inherited", "auto_linked")],
    )
    conn.executemany(
        """
        INSERT INTO quality_issues(issue_id, target_type, issue_type, severity, resolved_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "ksa", "short_ksa", "info", None),
            (2, "unit", "resolved", "warning", "2026-01-01T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO ncs_query_aliases(alias_id, source_method, review_status) VALUES (?, ?, ?)",
        [(1, "seed", "candidate")],
    )
    conn.executemany(
        "INSERT INTO review_audit_log(id, entity_type, action, new_status) VALUES (?, ?, ?, ?)",
        [(1, "ontology_concept_label_candidate", "ksa_label_approve", "human_reviewed")],
    )
    conn.commit()
    return conn


def _safe_auto_triage_report(**overrides: object) -> dict:
    payload = {
        "schema": "ksa_label_auto_triage_report_v1",
        "ok": True,
        "status": "review_required",
        "report_only": True,
        "candidate_count": 3,
        "classification_v2_counts": {
            "auto-pass-candidate": 0,
            "modify-recommended": 1,
            "human-sample-needed": 1,
            "domain-expert-needed": 0,
            "already-trusted-review": 1,
            "missing-label-gap": 0,
        },
        "major_bucket_rollup": [],
        "full_scope_decision_row_count": 2,
        "full_scope_manual_review_recommended_count": 2,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "scope_policy": {
            "target_scope_is_filtered": False,
            "scoped_counts_are_local_view": False,
            "scoped_report_is_canonical_bulk_plan": False,
            "all_scope_required_for_bulk_planning": True,
            "operator_sampling_plan_required_before_bulk_use": True,
        },
    }
    payload.update(overrides)
    return payload


def _safe_sampling_plan(**overrides: object) -> dict:
    payload = {
        "schema": "ksa_label_policy_v2_operator_sampling_plan_v1",
        "ok": True,
        "status": "review_planning_only",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "source_issues": [],
        "summary": {
            "candidate_count": 3,
            "recommended_sample_rows_total": 1,
            "decision_rows_total_from_major_rollup": 2,
            "estimated_click_reduction_ratio": 0.5,
        },
    }
    payload.update(overrides)
    return payload


class LlmPreprocessingBacklogTests(unittest.TestCase):
    def run_harness_cli_json(self, argv: list[str]) -> dict:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        sys.argv = ["ncs_harness.py", *argv]
        try:
            with contextlib.redirect_stdout(stdout):
                harness_main()
        finally:
            sys.argv = previous_argv
        return json.loads(stdout.getvalue())

    def test_backlog_map_reports_counts_and_safety_policy(self) -> None:
        conn = _seed_db()
        try:
            report = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()

        self.assertEqual(report["schema"], "ncs_llm_preprocessing_backlog_map_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertTrue(report["human_decision_required_for_approval"])
        self.assertTrue(
            report["review_status_policy"]["human_decision_required_for_status_update"]
        )
        self.assertEqual(
            set(report["review_status_policy"]["forbidden_automatic_statuses"]),
            {"human_reviewed", "accepted", "reviewed"},
        )
        self.assertEqual(report["summary"]["raw_ksa_rows"], 2)
        self.assertEqual(report["summary"]["label_candidate_rows"], 3)
        self.assertEqual(report["summary"]["human_reviewed_label_rows"], 1)
        self.assertEqual(report["summary"]["pending_label_rows_not_trusted"], 2)
        self.assertEqual(report["summary"]["distinct_normalized_label_keys"], 2)
        self.assertEqual(report["summary"]["distinct_concepts_with_label_candidates"], 2)
        self.assertEqual(report["summary"]["meaning_candidate_rows"], 2)
        self.assertEqual(report["summary"]["unresolved_quality_issue_rows"], 1)
        self.assertEqual(report["label_backlog"]["major_progress"][0]["major_code"], "01")
        self.assertIn(
            "Do not set human_reviewed, accepted, or reviewed automatically.",
            report["forbidden_without_explicit_operator_approval"],
        )

    def test_policy_snapshot_and_markdown_are_report_only(self) -> None:
        conn = _seed_db()
        try:
            report = build_llm_preprocessing_backlog_map(
                conn,
                auto_triage_report=_safe_auto_triage_report(
                    classification_v2_counts={"human-sample-needed": 2}
                ),
                sampling_plan=_safe_sampling_plan(),
            )
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "backlog.md"
            write_llm_preprocessing_backlog_markdown(report, out_path)
            markdown = out_path.read_text(encoding="utf-8")

        self.assertEqual(
            report["policy_snapshot"]["auto_triage"]["candidate_count"],
            3,
        )
        self.assertEqual(
            report["policy_snapshot"]["sampling_plan"]["recommended_sample_rows_total"],
            1,
        )
        self.assertIn("report_only: `true`", markdown)
        self.assertIn("db_writes: `false`", markdown)
        self.assertIn("non_approval_statuses", markdown)
        self.assertIn("auto-pass-candidate", markdown)
        self.assertIn("Label candidate rows: 3", markdown)
        self.assertIn("Recommended sample rows total: 1", markdown)

    def test_backlog_map_counts_null_review_status_as_pending(self) -> None:
        conn = _seed_db()
        conn.execute(
            """
            INSERT INTO ontology_concept_label_candidates(
                label_id, concept_id, source_scope_key, normalized_label_key,
                source_method, review_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                4,
                2,
                "02:02:02:02",
                "labelthree",
                "rule_based_short_label_candidate",
                None,
            ),
        )
        conn.commit()
        try:
            report = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()

        self.assertEqual(report["summary"]["label_candidate_rows"], 4)
        self.assertEqual(report["summary"]["human_reviewed_label_rows"], 1)
        self.assertEqual(report["summary"]["pending_label_rows_not_trusted"], 3)

    def test_harness_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "ncs.db"
            memory_conn = _seed_db()
            file_conn = sqlite3.connect(db_path)
            try:
                memory_conn.backup(file_conn)
            finally:
                file_conn.close()
                memory_conn.close()
            before_mtime_ns = db_path.stat().st_mtime_ns
            json_path = tmp_path / "llm_backlog.json"
            markdown_path = tmp_path / "llm_backlog.md"

            with patch(
                "scripts.ncs_harness.load_settings",
                return_value=SimpleNamespace(db_path=db_path),
            ):
                result = self.run_harness_cli_json(
                    [
                        "llm-preprocessing-backlog-map",
                        "--out",
                        str(json_path),
                        "--markdown-out",
                        str(markdown_path),
                    ]
                )

            report = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "review_planning_only")
            self.assertTrue(result["report_only"])
            self.assertFalse(result["status_update_allowed"])
            self.assertFalse(result["db_writes"])
            self.assertFalse(result["approval_claim"])
            self.assertEqual(result["label_candidate_rows"], 3)
            self.assertEqual(report["summary"]["pending_label_rows_not_trusted"], 2)
            self.assertIn("report_only: `true`", markdown)
            self.assertEqual(db_path.stat().st_mtime_ns, before_mtime_ns)

    def test_unsafe_sidecar_blocks_report_and_cli(self) -> None:
        conn = _seed_db()
        try:
            report = build_llm_preprocessing_backlog_map(
                conn,
                auto_triage_report={
                    "schema": "ksa_label_auto_triage_report_v1",
                    "ok": True,
                    "status_update_allowed": True,
                    "db_writes": True,
                    "approval_claim": True,
                },
            )
        finally:
            conn.close()

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "blocked_unsafe_source_artifact")
        self.assertGreaterEqual(report["blocker_count"], 3)
        issue_fields = {issue["field"] for issue in report["source_issues"] if "field" in issue}
        self.assertEqual(
            {"status_update_allowed", "db_writes", "approval_claim"} & issue_fields,
            {"status_update_allowed", "db_writes", "approval_claim"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "ncs.db"
            memory_conn = _seed_db()
            file_conn = sqlite3.connect(db_path)
            try:
                memory_conn.backup(file_conn)
            finally:
                file_conn.close()
                memory_conn.close()
            bad_sidecar = tmp_path / "unsafe_auto_triage.json"
            bad_sidecar.write_text(
                json.dumps(
                    {
                        "schema": "ksa_label_auto_triage_report_v1",
                        "ok": True,
                        "status_update_allowed": True,
                        "db_writes": True,
                        "approval_claim": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            out_json = tmp_path / "llm_backlog.json"
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-backlog-map",
                "--auto-triage-report",
                str(bad_sidecar),
                "--out",
                str(out_json),
            ]
            try:
                with patch(
                    "scripts.ncs_harness.load_settings",
                    return_value=SimpleNamespace(db_path=db_path),
                ):
                    with contextlib.redirect_stdout(stdout):
                        with self.assertRaises(SystemExit) as raised:
                            harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            result = json.loads(stdout.getvalue())
            written = json.loads(out_json.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertFalse(written["ok"])
        self.assertEqual(written["status"], "blocked_unsafe_source_artifact")

    def test_blocked_sidecar_with_false_safety_flags_still_blocks(self) -> None:
        conn = _seed_db()
        try:
            report = build_llm_preprocessing_backlog_map(
                conn,
                auto_triage_report=_safe_auto_triage_report(
                    ok=False,
                    report_only=False,
                    status="blocked",
                ),
                sampling_plan=_safe_sampling_plan(
                    ok=False,
                    report_only=False,
                    status="invalid_source_report",
                    source_issues=["source_report_scope_filtered"],
                ),
            )
        finally:
            conn.close()

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "blocked_unsafe_source_artifact")
        issue_codes = {issue["code"] for issue in report["source_issues"]}
        self.assertIn("sidecar_safety_flag_not_true", issue_codes)
        self.assertIn("sampling_plan_source_issues_present", issue_codes)
        self.assertFalse(report["policy_snapshot"]["auto_triage"]["safety_ok"])
        self.assertFalse(report["policy_snapshot"]["sampling_plan"]["safety_ok"])

    def test_work_plan_splits_automation_from_human_gates(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(
                conn,
                auto_triage_report=_safe_auto_triage_report(
                    classification_v2_counts={
                        "modify-recommended": 1,
                        "human-sample-needed": 1,
                        "domain-expert-needed": 0,
                    }
                ),
                sampling_plan=_safe_sampling_plan(),
            )
        finally:
            conn.close()

        plan = build_llm_preprocessing_work_plan(backlog)
        self.assertEqual(plan["schema"], "ncs_llm_preprocessing_work_plan_v1")
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["report_only"])
        self.assertFalse(plan["status_update_allowed"])
        self.assertFalse(plan["db_writes"])
        self.assertFalse(plan["approval_claim"])
        self.assertFalse(plan["safety_contract"]["trusted_status_write_allowed"])
        self.assertFalse(plan["safety_contract"]["raw_source_mutation_allowed"])
        self.assertFalse(plan["artifact_policy"]["db_apply_allowed"])
        self.assertFalse(plan["artifact_policy"]["operator_decision_fields_auto_filled"])
        self.assertEqual(plan["next_action"], "run_report_only_track_artifacts")
        self.assertEqual(plan["input_summary"]["label_candidate_rows"], 3)
        self.assertEqual(plan["input_summary"]["recommended_sample_rows_total"], 1)
        self.assertGreaterEqual(len(plan["work_tracks"]), 4)
        self.assertIn(
            "writing human_reviewed, accepted, or reviewed statuses",
            plan["not_recommended_for_llm_run"],
        )
        first_track = plan["work_tracks"][0]
        self.assertEqual(first_track["track"], "label_policy_triage_and_sampling")
        self.assertIn("operator sample decisions", first_track["human_gate"])

        with tempfile.TemporaryDirectory() as tmpdir:
            markdown_path = Path(tmpdir) / "work_plan.md"
            write_llm_preprocessing_work_plan_markdown(plan, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("LLM Preprocessing Work Plan", markdown)
        self.assertIn("report_only: `true`", markdown)
        self.assertIn("trusted_status_write_allowed: `false`", markdown)
        self.assertIn("label_policy_triage_and_sampling", markdown)
        self.assertIn("Not Recommended For LLM Run", markdown)

    def test_work_plan_cli_writes_report_only_plan(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            backlog_path = tmp_path / "backlog.json"
            out_path = tmp_path / "work_plan.json"
            markdown_path = tmp_path / "work_plan.md"
            backlog_path.write_text(
                json.dumps(backlog, ensure_ascii=False),
                encoding="utf-8",
            )

            result = self.run_harness_cli_json(
                [
                    "llm-preprocessing-work-plan",
                    "--backlog-map",
                    str(backlog_path),
                    "--out",
                    str(out_path),
                    "--markdown-out",
                    str(markdown_path),
                ]
            )

            report = json.loads(out_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready_for_llm_preprocessing")
        self.assertTrue(result["report_only"])
        self.assertFalse(result["status_update_allowed"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertEqual(result["track_count"], len(report["work_tracks"]))
        self.assertEqual(report["source_schema"], "ncs_llm_preprocessing_backlog_map_v1")
        self.assertEqual(report["source_backlog_map"], str(backlog_path))
        self.assertRegex(report["source_artifact_hash"], r"^[0-9a-f]{64}$")
        self.assertIn("Eight-Hour Run Plan", markdown)

    def test_work_plan_cli_rejects_unsafe_backlog_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            backlog_path = tmp_path / "unsafe_backlog.json"
            out_path = tmp_path / "work_plan.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_llm_preprocessing_backlog_map_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": True,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-work-plan",
                "--backlog-map",
                str(backlog_path),
                "--out",
                str(out_path),
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            result = json.loads(stdout.getvalue())
            written = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertFalse(written["ok"])
        self.assertEqual(written["status"], "blocked_unsafe_source_artifact")

    def test_work_plan_rejects_backlog_without_human_gate_contract(self) -> None:
        plan = build_llm_preprocessing_work_plan(
            {
                "schema": "ncs_llm_preprocessing_backlog_map_v1",
                "ok": True,
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "summary": {"label_candidate_rows": 1},
            }
        )

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["status"], "blocked_unsafe_source_artifact")
        issue_codes = {issue["code"] for issue in plan["source_issues"]}
        self.assertIn("human_decision_gate_missing", issue_codes)
        self.assertIn("status_update_human_gate_missing", issue_codes)
        self.assertIn("forbidden_automatic_statuses_missing", issue_codes)

    def test_runbook_splits_report_only_commands_from_guarded_work(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()
        work_plan = build_llm_preprocessing_work_plan(backlog)
        runbook = build_llm_preprocessing_runbook(
            work_plan,
            artifact_suffix="20260629_8h",
            source_path="reports/llm_preprocessing_next_8h_work_plan_20260629_8h.json",
            source_artifact_hash="a" * 64,
        )

        self.assertTrue(runbook["ok"])
        self.assertTrue(runbook["report_only"])
        self.assertFalse(runbook["status_update_allowed"])
        self.assertFalse(runbook["db_writes"])
        self.assertFalse(runbook["approval_claim"])
        self.assertEqual(runbook["status"], "ready_to_run_report_only_commands")
        self.assertGreaterEqual(runbook["command_count"], 5)
        self.assertEqual(
            runbook["planned_work_track_count"],
            len(work_plan["work_tracks"]),
        )
        self.assertEqual(
            runbook["covered_work_track_count"],
            len(work_plan["work_tracks"]),
        )
        self.assertEqual(runbook["uncovered_work_tracks"], [])
        self.assertTrue(
            all(
                coverage["covered"]
                for coverage in runbook["track_coverage"].values()
            )
        )
        self.assertIn(
            "record-aihr-plan-review-decision",
            runbook["manual_or_guarded_exclusions"],
        )
        self.assertGreaterEqual(
            runbook["track_coverage"]["label_modify_pattern_cleanup"]["command_count"],
            1,
        )
        self.assertGreaterEqual(
            runbook["track_coverage"]["quality_issue_deduplication_plan"]["command_count"],
            1,
        )
        command_tokens = [
            token
            for command in runbook["commands"]
            for token in command.get("command", [])
        ]
        self.assertIn("agent-queue-status", command_tokens)
        self.assertEqual(
            runbook["agent_queue_path"],
            "reports\\aihr_agent_queue_20260629.json",
        )
        self.assertNotIn("reports\\aihr_agent_queue_20260629_8h.json", command_tokens)
        self.assertIn("reports\\aihr_agent_queue_20260629.json", command_tokens)
        self.assertIn("ksa-short-label-pattern-report", command_tokens)
        self.assertIn("quality-gates", command_tokens)
        self.assertIn("query-alias-candidate-packet", command_tokens)
        self.assertIn("--gap-report", command_tokens)
        self.assertIn("--decision-sheet-out", command_tokens)

        with tempfile.TemporaryDirectory() as tmpdir:
            markdown_path = Path(tmpdir) / "runbook.md"
            write_llm_preprocessing_runbook_markdown(runbook, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("LLM Preprocessing Runbook", markdown)
        self.assertIn("regenerate_reports_only", markdown)
        self.assertIn("guarded_collection_allowed: `false`", markdown)

    def test_runbook_can_target_readonly_refresh_artifact_dir(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()
        work_plan = build_llm_preprocessing_work_plan(backlog)
        runbook = build_llm_preprocessing_runbook(
            work_plan,
            artifact_suffix="20260630_7h_extension",
            artifact_dir="reports\\overnight_sessions\\readonly_refresh",
            source_path=(
                "reports\\overnight_sessions\\readonly_refresh\\"
                "llm_preprocessing_work_plan_20260630_7h_extension.json"
            ),
            source_artifact_hash="b" * 64,
            agent_queue_path=(
                "reports\\overnight_sessions\\readonly_refresh\\"
                "aihr_agent_queue_20260630_7h_extension.json"
            ),
            agent_queue_path_source="explicit_cli_argument",
        )

        self.assertTrue(runbook["ok"])
        self.assertEqual(
            runbook["artifact_dir"],
            "reports\\overnight_sessions\\readonly_refresh",
        )
        self.assertEqual(
            runbook["agent_queue_path"],
            (
                "reports\\overnight_sessions\\readonly_refresh\\"
                "aihr_agent_queue_20260630_7h_extension.json"
            ),
        )
        for command in runbook["commands"]:
            command_tokens = [str(token) for token in command.get("command", [])]
            output_tokens = [str(token) for token in command.get("expected_outputs", [])]
            for token in [*command_tokens, *output_tokens]:
                if token.startswith("reports\\") and "aihr_agent_queue_20260630_7h_extension.json" not in token:
                    self.assertTrue(
                        token.startswith("reports\\overnight_sessions\\readonly_refresh\\"),
                        token,
                    )

    def test_runbook_cli_writes_report_only_runbook(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(conn)
        finally:
            conn.close()
        work_plan = build_llm_preprocessing_work_plan(backlog)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            work_plan_path = tmp_path / "work_plan.json"
            out_path = tmp_path / "runbook.json"
            markdown_path = tmp_path / "runbook.md"
            work_plan_path.write_text(
                json.dumps(work_plan, ensure_ascii=False),
                encoding="utf-8",
            )

            result = self.run_harness_cli_json(
                [
                    "llm-preprocessing-runbook",
                    "--work-plan",
                    str(work_plan_path),
                    "--artifact-suffix",
                    "20260629_8h",
                    "--artifact-dir",
                    "reports\\overnight_sessions\\readonly_refresh",
                    "--queue",
                    "reports\\aihr_agent_queue_20260629_8h.json",
                    "--out",
                    str(out_path),
                    "--markdown-out",
                    str(markdown_path),
                ]
            )

            report = json.loads(out_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready_to_run_report_only_commands")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertEqual(result["command_count"], report["command_count"])
        self.assertEqual(
            result["artifact_dir"],
            "reports\\overnight_sessions\\readonly_refresh",
        )
        self.assertEqual(
            report["artifact_dir"],
            "reports\\overnight_sessions\\readonly_refresh",
        )
        self.assertEqual(
            result["agent_queue_path"],
            "reports\\aihr_agent_queue_20260629_8h.json",
        )
        self.assertIn(
            "reports\\overnight_sessions\\readonly_refresh\\aihr_agent_queue_status_llm_preprocessing_20260629_8h.json",
            report["commands"][0]["expected_outputs"],
        )
        self.assertEqual(
            report["commands"][0]["command"][4],
            "reports\\aihr_agent_queue_20260629_8h.json",
        )
        self.assertRegex(report["source_artifact_hash"], r"^[0-9a-f]{64}$")
        self.assertIn("LLM Preprocessing Runbook", markdown)

    def test_malformed_llm_cli_inputs_write_blocked_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            bad_sidecar = tmp_path / "bad_auto_triage.json"
            bad_sidecar.write_text("{bad json", encoding="utf-8")
            backlog_out = tmp_path / "blocked_backlog.json"
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-backlog-map",
                "--auto-triage-report",
                str(bad_sidecar),
                "--out",
                str(backlog_out),
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            result = json.loads(stdout.getvalue())
            written = json.loads(backlog_out.read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertFalse(written["ok"])
            self.assertEqual(written["status"], "blocked_malformed_input")
            self.assertEqual(written["source_issues"][0]["code"], "malformed_json_input")

            bad_utf8_backlog = tmp_path / "bad_utf8_backlog.json"
            bad_utf8_backlog.write_bytes(b"\xff\xfe\xfa")
            utf8_work_plan_out = tmp_path / "blocked_utf8_work_plan.json"
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-work-plan",
                "--backlog-map",
                str(bad_utf8_backlog),
                "--out",
                str(utf8_work_plan_out),
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            result = json.loads(stdout.getvalue())
            written = json.loads(utf8_work_plan_out.read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertFalse(written["ok"])
            self.assertEqual(written["status"], "blocked_malformed_input")
            self.assertEqual(written["source_issues"][0]["code"], "invalid_utf8_input")

            bad_backlog = tmp_path / "bad_backlog.json"
            bad_backlog.write_text("{bad json", encoding="utf-8")
            work_plan_out = tmp_path / "blocked_work_plan.json"
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-work-plan",
                "--backlog-map",
                str(bad_backlog),
                "--out",
                str(work_plan_out),
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            result = json.loads(stdout.getvalue())
            written = json.loads(work_plan_out.read_text(encoding="utf-8"))

            bad_work_plan = tmp_path / "bad_work_plan.json"
            bad_work_plan.write_bytes(b"\xff\xfe\xfa")
            runbook_out = tmp_path / "blocked_runbook.json"
            stdout = io.StringIO()
            previous_argv = sys.argv[:]
            sys.argv = [
                "ncs_harness.py",
                "llm-preprocessing-runbook",
                "--work-plan",
                str(bad_work_plan),
                "--artifact-suffix",
                "20260629_8h",
                "--out",
                str(runbook_out),
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as raised:
                        harness_main()
            finally:
                sys.argv = previous_argv

            self.assertEqual(raised.exception.code, 1)
            runbook_result = json.loads(stdout.getvalue())
            runbook_written = json.loads(runbook_out.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertFalse(written["ok"])
        self.assertEqual(written["status"], "blocked_malformed_input")
        self.assertEqual(written["source_issues"][0]["code"], "malformed_json_input")
        self.assertFalse(runbook_result["ok"])
        self.assertFalse(runbook_written["ok"])
        self.assertEqual(runbook_written["status"], "blocked_malformed_input")
        self.assertEqual(
            runbook_written["source_issues"][0]["code"],
            "invalid_utf8_input",
        )

    def test_llm_cli_accepts_utf8_sig_json_inputs(self) -> None:
        conn = _seed_db()
        try:
            backlog = build_llm_preprocessing_backlog_map(conn)
            work_plan = build_llm_preprocessing_work_plan(backlog)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            auto_triage_path = tmp_path / "auto_triage.json"
            auto_triage_path.write_text(
                json.dumps(
                    _safe_auto_triage_report(),
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )
            db_path = tmp_path / "ncs.db"
            memory_conn = _seed_db()
            file_conn = sqlite3.connect(db_path)
            try:
                memory_conn.backup(file_conn)
            finally:
                file_conn.close()
                memory_conn.close()
            backlog_out = tmp_path / "backlog_out.json"
            with patch(
                "scripts.ncs_harness.load_settings",
                return_value=SimpleNamespace(db_path=db_path),
            ):
                backlog_result = self.run_harness_cli_json(
                    [
                        "llm-preprocessing-backlog-map",
                        "--auto-triage-report",
                        str(auto_triage_path),
                        "--out",
                        str(backlog_out),
                    ]
                )

            backlog_path = tmp_path / "backlog_sig.json"
            backlog_path.write_text(
                json.dumps(backlog, ensure_ascii=False),
                encoding="utf-8-sig",
            )
            work_plan_out = tmp_path / "work_plan.json"
            work_plan_result = self.run_harness_cli_json(
                [
                    "llm-preprocessing-work-plan",
                    "--backlog-map",
                    str(backlog_path),
                    "--out",
                    str(work_plan_out),
                ]
            )

            work_plan_path = tmp_path / "work_plan_sig.json"
            work_plan_path.write_text(
                json.dumps(work_plan, ensure_ascii=False),
                encoding="utf-8-sig",
            )
            runbook_out = tmp_path / "runbook.json"
            runbook_result = self.run_harness_cli_json(
                [
                    "llm-preprocessing-runbook",
                    "--work-plan",
                    str(work_plan_path),
                    "--artifact-suffix",
                    "20260629_8h",
                    "--queue",
                    "reports\\aihr_agent_queue_20260629_8h.json",
                    "--out",
                    str(runbook_out),
                ]
            )

        self.assertTrue(backlog_result["ok"])
        self.assertTrue(work_plan_result["ok"])
        self.assertTrue(runbook_result["ok"])


if __name__ == "__main__":
    unittest.main()
