from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ncs006_checkpoint = load_script_module(
    "checkpoint_ncs006_element_api_status",
    "scripts/checkpoint_ncs006_element_api_status.py",
)
human_review_checkpoint = load_script_module(
    "checkpoint_human_review_safe_ops",
    "scripts/checkpoint_human_review_safe_ops.py",
)
element_watchdog = load_script_module(
    "watch_element_api_collection",
    "scripts/watch_element_api_collection.py",
)
element_runner = load_script_module(
    "run_element_api_collection",
    "scripts/run_element_api_collection.py",
)
sqf_db_checkpoint = load_script_module(
    "checkpoint_sqf_db_readiness",
    "scripts/checkpoint_sqf_db_readiness.py",
)
overnight_checkpoint = load_script_module(
    "checkpoint_overnight_ncs_sqf_work",
    "scripts/checkpoint_overnight_ncs_sqf_work.py",
)
recent_review_audit = load_script_module(
    "audit_recent_review_status_writes",
    "scripts/audit_recent_review_status_writes.py",
)


def initialize_minimal_ncs006_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                major_code TEXT NOT NULL,
                major_name TEXT NOT NULL
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                classification_id INTEGER NOT NULL
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_code TEXT NOT NULL,
                api_match_status TEXT NOT NULL
            );
            INSERT INTO classifications(major_code, major_name) VALUES ('08', 'culture');
            INSERT INTO competency_units(unit_code, classification_id) VALUES ('0801010101_23v1', 1);
            INSERT INTO competency_elements(unit_code, api_match_status) VALUES
                ('0801010101_23v1', 'matched'),
                ('0801010101_23v1', 'not_collected'),
                ('0801010101_23v1', 'api_failed');
            """
        )
        conn.commit()
    finally:
        conn.close()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_recent_status_audit(
    path: Path,
    *,
    ok: bool = True,
    db_writes: bool = False,
    status_update_allowed: bool = False,
    approval_claim: bool = False,
) -> None:
    write_json(
        path,
        {
            "ok": ok,
            "schema": "aihr_recent_review_status_write_audit_v1",
            "generated_at": "2026-06-20T02:00:00+00:00",
            "read_only": True,
            "db_writes": db_writes,
            "status_update_allowed": status_update_allowed,
            "approval_claim": approval_claim,
            "cutoff": "2026-06-20T01:00:00+00:00",
            "trusted_status_values": ["accepted", "human_reviewed", "reviewed"],
            "monitored_non_trusted_status_values": ["candidate_auto"],
            "review_audit_log_exists": True,
            "review_audit_log_has_created_at": True,
            "recent_trusted_status_table_hit_count": 0 if ok else 1,
            "recent_trusted_audit_log_count": 0,
            "recent_monitored_non_trusted_status_table_hit_count": 0,
            "recent_monitored_non_trusted_audit_log_count": 0,
            "recent_audit_log_total_count": 0,
            "recent_unverifiable_generic_timestamp_count": 0,
            "recent_monitored_non_trusted_unverifiable_generic_timestamp_count": 0,
            "unverifiable_no_timestamp_table_count": 0,
            "monitored_non_trusted_unverifiable_no_timestamp_table_count": 0,
            "invalid_timestamp_table_row_count": 0,
        },
    )


def write_safe_human_review_checkpoint_inputs(
    root: Path,
    *,
    recent_status_kwargs: dict[str, bool] | None = None,
) -> Namespace:
    readiness = root / "readiness.json"
    decision_audit = root / "decision_audit.json"
    guarded_plan = root / "guarded_plan.json"
    provenance_audit = root / "provenance_audit.json"
    reconfirm_packet = root / "reconfirm_packet.json"
    reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
    reconfirm_decision_audit = root / "reconfirm_decision_audit.json"
    recent_status_audit = root / "recent_status_audit.json"

    write_json(
        readiness,
        {
            "ok": True,
            "allowed_use": "supplementary_review_context_only",
            "approval_ready": False,
            "db_writes": False,
            "status_update_allowed": False,
            "used_for_scoring": False,
        },
    )
    write_json(decision_audit, {"ok": True, "rows": []})
    write_json(
        guarded_plan,
        {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
    )
    write_json(
        provenance_audit,
        {
            "ok": True,
            "summary": {
                "row_count": 0,
                "rows_packet_backed": 0,
                "rows_without_packet_backed_provenance": 0,
                "provenance_gap_present": False,
                "db_writes": False,
            },
        },
    )
    write_json(
        reconfirm_packet,
        {
            "ok": True,
            "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
            "row_count": 0,
            "db_writes": False,
            "status_update_allowed": False,
        },
    )
    packet_ref = human_review_checkpoint.rel(reconfirm_packet)
    packet_sha = human_review_checkpoint.content_sha256(reconfirm_packet.read_bytes())
    write_json(
        reconfirm_decision_sheet,
        {
            "ok": True,
            "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
            "source_packet": packet_ref,
            "source_packet_sha256": packet_sha,
            "row_count": 0,
            "blank_decision_count": 0,
            "completed_decision_count": 0,
            "db_writes": False,
            "status_update_allowed": False,
            "approval_claim": False,
            "human_decision_required": True,
        },
    )
    write_json(
        reconfirm_decision_audit,
        {
            "ok": True,
            "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
            "csv": human_review_checkpoint.rel(reconfirm_decision_sheet.with_suffix(".csv")),
            "source_packet": packet_ref,
            "source_packet_sha256": packet_sha,
            "row_count": 0,
            "source_packet_row_count": 0,
            "pending_decision_count": 0,
            "completed_decision_count": 0,
            "invalid_decision_count": 0,
            "missing_required_field_row_count": 0,
            "source_mismatch_count": 0,
            "source_identity_mismatch_count": 0,
            "source_decision_packet_not_found_count": 0,
            "invalid_evidence_refs_json_count": 0,
            "unsafe_flag_count": 0,
            "duplicate_csv_key_count": 0,
            "missing_packet_row_count": 0,
            "unexpected_csv_row_count": 0,
            "missing_csv_columns": [],
            "action_eligible_count": 0,
            "db_writes": False,
            "status_update_allowed": False,
            "approval_claim": False,
            "guarded_apply_ready": False,
        },
    )
    write_recent_status_audit(recent_status_audit, **(recent_status_kwargs or {}))
    return Namespace(
        sqf_readiness=readiness,
        sqf_decision_audit=decision_audit,
        sqf_guarded_plan=guarded_plan,
        provenance_audit=provenance_audit,
        reconfirm_packet=reconfirm_packet,
        reconfirm_decision_sheet=reconfirm_decision_sheet,
        reconfirm_decision_audit=reconfirm_decision_audit,
        recent_status_audit=recent_status_audit,
    )


class Ncs006CheckpointTests(unittest.TestCase):
    def test_checkpoint_counts_and_incomplete_batch_are_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            log_path = root / "run.jsonl"
            summary_path = root / "summary.json"
            watch_log_path = root / "watch.jsonl"
            initialize_minimal_ncs006_db(db_path)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps({"event": "start", "timestamp": "2026-06-20T00:00:00+00:00"}),
                        json.dumps(
                            {
                                "event": "batch_start",
                                "timestamp": "2026-06-20T00:01:00+00:00",
                                "phase": "uncollected",
                                "major_code": "08",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            summary_path.write_text("{}", encoding="utf-8")
            watch_log_path.write_text(
                json.dumps(
                    {
                        "event": "rate_limit_pause_from_sweep",
                        "timestamp": "2026-06-20T00:10:00+00:00",
                        "cooldown_seconds": 3600,
                        "sweep": 1,
                        "returncode": 75,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            args = Namespace(
                db_path=db_path,
                log_path=log_path,
                summary_path=summary_path,
                watch_log_path=watch_log_path,
                child_timeout_seconds=900,
            )
            with (
                patch.object(ncs006_checkpoint, "process_snapshot", return_value=[]),
                patch.object(ncs006_checkpoint, "now_iso", return_value="2026-06-20T00:16:30+00:00"),
            ):
                checkpoint = ncs006_checkpoint.build_checkpoint(args)

            self.assertTrue(checkpoint["policy"]["read_only_checkpoint"])
            self.assertFalse(checkpoint["policy"]["db_writes"])
            self.assertEqual(checkpoint["element_api_status"]["totals"]["matched"], 1)
            self.assertEqual(checkpoint["element_api_status"]["totals"]["not_collected"], 1)
            self.assertEqual(checkpoint["element_api_status"]["totals"]["api_failed"], 1)
            self.assertEqual(checkpoint["totals"], checkpoint["element_api_status"]["totals"])
            self.assertEqual(
                checkpoint["process_roles"],
                checkpoint["collection_process_role_counts"],
            )
            self.assertEqual(
                checkpoint["element_api_status"]["by_major"][0]["major_name"],
                "culture",
            )
            self.assertEqual(
                checkpoint["element_api_status"]["by_major"][0]["major_name_display"],
                "문화·예술·디자인·방송",
            )
            self.assertEqual(
                checkpoint["run_log"]["active_or_incomplete_batch"]["major_code"],
                "08",
            )
            self.assertEqual(
                checkpoint["monitoring"]["active_batch_age_seconds"],
                930,
            )
            self.assertTrue(checkpoint["monitoring"]["timeout_exceeded"])
            self.assertEqual(
                checkpoint["monitoring"]["status"],
                "timeout_exceeded_inspect_child",
            )
            self.assertEqual(checkpoint["rate_limit_cooldown"]["status"], "cooldown_active")
            self.assertEqual(
                checkpoint["rate_limit_cooldown"]["cooldown_until"],
                "2026-06-20T01:10:00+00:00",
            )
            self.assertEqual(
                checkpoint["rate_limit_cooldown"]["cooldown_remaining_seconds"],
                3210,
            )
            self.assertEqual(
                checkpoint["next_safe_action"]["status"],
                "wait_for_rate_limit_cooldown",
            )
            self.assertFalse(checkpoint["next_safe_action"]["should_start_collector"])
            self.assertFalse(checkpoint["next_safe_action"]["api_call_allowed_now"])

    def test_checkpoint_writes_current_date_alias_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            log_path = root / "run.jsonl"
            summary_path = root / "summary.json"
            watch_log_path = root / "watch.jsonl"
            out_path = root / "checkpoint_ncs006_element_api_status_20260620.json"
            md_path = root / "checkpoint_ncs006_element_api_status_20260620.md"
            alias_out = ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624.json"
            alias_md = ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624.md"
            current_alias_out = ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624_current.json"
            current_alias_md = ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624_current.md"
            initialize_minimal_ncs006_db(db_path)
            log_path.write_text("", encoding="utf-8")
            summary_path.write_text("{}", encoding="utf-8")
            watch_log_path.write_text("", encoding="utf-8")

            args = Namespace(
                db_path=db_path,
                log_path=log_path,
                summary_path=summary_path,
                watch_log_path=watch_log_path,
                out=out_path,
                markdown_out=md_path,
                child_timeout_seconds=900,
            )
            with (
                patch.object(ncs006_checkpoint, "process_snapshot", return_value=[]),
                patch.object(ncs006_checkpoint, "now_iso", return_value="2026-06-24T00:00:00+00:00"),
                patch.object(ncs006_checkpoint, "current_date_stamp", return_value="20260624"),
            ):
                checkpoint = ncs006_checkpoint.build_checkpoint(args)
                out_path.write_text(ncs006_checkpoint.checkpoint_json_text(checkpoint), encoding="utf-8")
                ncs006_checkpoint.write_markdown(md_path, checkpoint)
                ncs006_checkpoint.write_alias_outputs(args, checkpoint)

            self.assertTrue(out_path.exists())
            self.assertTrue(md_path.exists())
            self.assertTrue(alias_out.exists())
            self.assertTrue(alias_md.exists())
            self.assertTrue(current_alias_out.exists())
            self.assertTrue(current_alias_md.exists())
            self.assertTrue(out_path.read_bytes().isascii())
            self.assertTrue(alias_out.read_bytes().isascii())
            self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["schema"], "ncs006_element_api_checkpoint_v1")
            self.assertEqual(json.loads(alias_out.read_text(encoding="utf-8"))["schema"], "ncs006_element_api_checkpoint_v1")
            alias_out.unlink(missing_ok=True)
            alias_md.unlink(missing_ok=True)

    def test_element_api_runner_and_watchdog_write_current_date_alias_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "element_api_collection_all_20260620.pid"
            log_path = root / "element_api_collection_all_20260620.jsonl"
            summary_path = root / "element_api_collection_all_20260620_summary.json"
            watchdog_pid_file = root / "element_api_collection_all_watchdog_20260620.pid"
            watchdog_log_path = root / "element_api_collection_all_watchdog_20260620.jsonl"
            run_log_path = root / "element_api_collection_all_20260620.jsonl"
            pid_file.write_text("1234", encoding="utf-8")
            log_path.write_text("runner-log", encoding="utf-8")
            summary_path.write_text("summary", encoding="utf-8")
            watchdog_pid_file.write_text("5678", encoding="utf-8")
            watchdog_log_path.write_text("watchdog-log", encoding="utf-8")

            runner_args = Namespace(pid_file=pid_file, log_path=log_path, summary_path=summary_path)
            watchdog_args = Namespace(
                pid_file=pid_file,
                watchdog_pid_file=watchdog_pid_file,
                log_path=watchdog_log_path,
                run_log_path=run_log_path,
                summary_path=summary_path,
            )
            with patch.object(element_runner, "current_date_stamp", return_value="20260625"):
                element_runner.write_alias_outputs(runner_args)
            with patch.object(element_watchdog, "current_date_stamp", return_value="20260625"):
                element_watchdog.write_alias_outputs(watchdog_args)

            self.assertTrue((ROOT / "reports" / "element_api_collection_all_20260625.pid").exists())
            self.assertTrue((ROOT / "reports" / "element_api_collection_all_20260625.jsonl").exists())
            self.assertTrue((ROOT / "reports" / "element_api_collection_all_20260625_summary.json").exists())
            self.assertTrue((ROOT / "reports" / "element_api_collection_all_watchdog_20260625.pid").exists())
            self.assertTrue((ROOT / "reports" / "element_api_collection_all_watchdog_20260625.jsonl").exists())

            for path in [
                ROOT / "reports" / "element_api_collection_all_20260625.pid",
                ROOT / "reports" / "element_api_collection_all_20260625.jsonl",
                ROOT / "reports" / "element_api_collection_all_20260625_summary.json",
                ROOT / "reports" / "element_api_collection_all_watchdog_20260625.pid",
                ROOT / "reports" / "element_api_collection_all_watchdog_20260625.jsonl",
            ]:
                path.unlink(missing_ok=True)

    def test_next_safe_collection_action_distinguishes_watchdog_and_retry_due(self) -> None:
        active = ncs006_checkpoint.next_safe_collection_action(
            {"parent_runner": 0, "child_collector": 1, "watchdog": 1},
            {"status": "within_child_timeout"},
            {"status": "no_rate_limit_cooldown"},
            {"not_collected": 10, "api_failed": 0},
        )
        self.assertEqual(active["status"], "collector_active_monitor_only")
        self.assertFalse(active["should_start_collector"])

        watchdog_due = ncs006_checkpoint.next_safe_collection_action(
            {"parent_runner": 0, "child_collector": 0, "watchdog": 1},
            {"status": "idle_or_between_batches"},
            {"status": "cooldown_elapsed_or_retry_due"},
            {"not_collected": 10, "api_failed": 0},
        )
        self.assertEqual(watchdog_due["status"], "watchdog_active_observe_next_sweep")
        self.assertFalse(watchdog_due["should_start_watchdog"])

        no_process_due = ncs006_checkpoint.next_safe_collection_action(
            {"parent_runner": 0, "child_collector": 0, "watchdog": 0},
            {"status": "idle_or_between_batches"},
            {"status": "cooldown_elapsed_or_retry_due"},
            {"not_collected": 10, "api_failed": 0},
        )
        self.assertEqual(no_process_due["status"], "start_guarded_watchdog_if_no_active_process")
        self.assertTrue(no_process_due["should_start_watchdog"])

    def test_rate_limit_cooldown_reports_elapsed_or_absent(self) -> None:
        absent = ncs006_checkpoint.rate_limit_cooldown_monitoring(
            [],
            "2026-06-20T00:16:30+00:00",
        )
        self.assertEqual(absent["status"], "no_rate_limit_cooldown")

        elapsed = ncs006_checkpoint.rate_limit_cooldown_monitoring(
            [
                {
                    "event": "rate_limit_pause_from_sweep",
                    "timestamp": "2026-06-20T00:10:00+00:00",
                    "cooldown_seconds": 60,
                    "sweep": 1,
                    "returncode": 75,
                }
            ],
            "2026-06-20T00:16:30+00:00",
        )

        self.assertEqual(elapsed["status"], "cooldown_elapsed_or_retry_due")
        self.assertEqual(elapsed["cooldown_remaining_seconds"], 0)

    def test_rate_limit_cooldown_reports_direct_runner_pause(self) -> None:
        cooldown = ncs006_checkpoint.rate_limit_cooldown_monitoring(
            [
                {
                    "event": "rate_limit_pause",
                    "timestamp": "2026-06-20T00:10:00+00:00",
                    "returncode": 75,
                }
            ],
            "2026-06-20T00:40:00+00:00",
        )

        self.assertEqual(cooldown["status"], "cooldown_active")
        self.assertEqual(cooldown["cooldown_seconds"], 3600)
        self.assertEqual(cooldown["cooldown_until"], "2026-06-20T01:10:00+00:00")
        self.assertEqual(cooldown["cooldown_remaining_seconds"], 1800)
        self.assertEqual(cooldown["latest_pause_returncode"], 75)

    def test_rate_limit_cooldown_reports_consumed_by_later_activity(self) -> None:
        consumed = ncs006_checkpoint.rate_limit_cooldown_monitoring(
            [
                {
                    "event": "rate_limit_pause_from_sweep",
                    "timestamp": "2026-06-20T00:10:00+00:00",
                    "cooldown_seconds": 3600,
                    "sweep": 1,
                    "returncode": 75,
                },
                {
                    "event": "full_sweep_start",
                    "timestamp": "2026-06-20T00:30:00+00:00",
                },
            ],
            "2026-06-20T00:40:00+00:00",
        )

        self.assertEqual(consumed["status"], "cooldown_consumed_by_later_activity")
        self.assertEqual(consumed["cooldown_remaining_seconds"], 0)
        self.assertEqual(consumed["latest_pause_at"], "2026-06-20T00:10:00+00:00")

    def test_recent_review_status_write_audit_detects_recent_trusted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ontology_concepts (
                        concept_id INTEGER PRIMARY KEY,
                        concept_name TEXT,
                        review_status TEXT,
                        updated_at TEXT,
                        reviewed_at TEXT
                    );
                    CREATE TABLE no_time_review (
                        id INTEGER PRIMARY KEY,
                        review_status TEXT
                    );
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL,
                        source_decision_packet TEXT,
                        source_artifact_hash TEXT,
                        rationale TEXT,
                        evidence_refs_json TEXT,
                        created_by_tool TEXT,
                        run_artifact TEXT
                    );
                    INSERT INTO ontology_concepts VALUES
                        (1, 'old trusted', 'human_reviewed', '2026-06-20T00:00:00+00:00', '2026-06-20T00:00:00+00:00'),
                        (2, 'recent candidate', 'candidate', '2026-06-20T02:00:00+00:00', NULL),
                        (3, 'recent trusted', 'reviewed', '2026-06-20T02:10:00+00:00', '2026-06-20T02:10:00+00:00');
                    INSERT INTO no_time_review VALUES (1, 'accepted');
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status, created_at
                    ) VALUES
                        ('ontology_concept', '3', 'status_update', 'candidate', 'reviewed', '2026-06-20T02:11:00+00:00'),
                        ('ontology_concept', '2', 'status_update', 'candidate', 'candidate', '2026-06-20T02:12:00+00:00');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["recent_trusted_status_table_hit_count"], 1)
            self.assertEqual(report["recent_trusted_audit_log_count"], 1)
            self.assertEqual(report["unverifiable_no_timestamp_table_count"], 1)
            self.assertEqual(report["recent_hits"][0]["identity"], {"concept_id": 3})

    def test_recent_review_status_write_audit_flags_generic_updated_at_as_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ontology_concepts (
                        concept_id INTEGER PRIMARY KEY,
                        concept_name TEXT,
                        review_status TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO ontology_concepts VALUES
                        (1, 'old trusted edited recently', 'human_reviewed', '2026-06-20T02:10:00+00:00');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["recent_trusted_status_table_hit_count"], 0)
            self.assertEqual(report["recent_unverifiable_generic_timestamp_count"], 1)
            self.assertEqual(report["recent_unverifiable_hits"][0]["identity"], {"concept_id": 1})

    def test_recent_review_status_write_audit_detects_recent_candidate_auto_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE training_transition_gold_scenarios (
                        scenario_id INTEGER PRIMARY KEY,
                        scenario_name TEXT,
                        review_status TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO training_transition_gold_scenarios VALUES
                        (1, 'recent automatic candidate', 'candidate_auto',
                         '2026-06-20T02:10:00+00:00', '2026-06-20T02:10:00+00:00'),
                        (2, 'recent plain candidate', 'candidate',
                         '2026-06-20T02:11:00+00:00', '2026-06-20T02:11:00+00:00');
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status, created_at
                    ) VALUES
                        ('training_transition_gold_scenario', '1',
                         'review_training_transition_scenarios',
                         'candidate', 'candidate_auto', '2026-06-20T02:12:00+00:00'),
                        ('training_transition_gold_scenario', '2',
                         'status_update', 'candidate', 'candidate', '2026-06-20T02:13:00+00:00');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["recent_trusted_status_table_hit_count"], 0)
            self.assertEqual(report["recent_monitored_non_trusted_status_table_hit_count"], 1)
            self.assertEqual(report["recent_monitored_non_trusted_audit_log_count"], 1)
            self.assertEqual(
                report["recent_monitored_non_trusted_hits"][0]["identity"],
                {"scenario_id": 1},
            )

    def test_recent_review_status_write_audit_detects_candidate_auto_delete_audit_log(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE training_transition_gold_scenarios (
                        scenario_id INTEGER PRIMARY KEY,
                        scenario_name TEXT,
                        review_status TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL,
                        created_by_tool TEXT
                    );
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status,
                        new_status, created_at, created_by_tool
                    ) VALUES (
                        'training_transition_gold_scenario', '1',
                        'generate_training_transition_eval_set_reset_auto',
                        'candidate_auto', 'candidate_auto',
                        '2026-06-20T02:12:00+00:00',
                        'ncs_harness.generate-training-transition-eval-set'
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["recent_monitored_non_trusted_status_table_hit_count"], 0)
            self.assertEqual(report["recent_monitored_non_trusted_audit_log_count"], 1)
            self.assertEqual(
                report["recent_audit_monitored_non_trusted_rows"][0]["action"],
                "generate_training_transition_eval_set_reset_auto",
            )

    def test_recent_review_status_write_audit_detects_previous_status_only_candidate_auto_log(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL,
                        created_by_tool TEXT
                    );
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status,
                        new_status, created_at, created_by_tool
                    ) VALUES (
                        'training_transition_gold_scenario', '1',
                        'delete_candidate_auto_scenario',
                        'candidate_auto', NULL,
                        '2026-06-20T02:12:00+00:00',
                        'ncs_harness.generate-training-transition-eval-set'
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["recent_monitored_non_trusted_audit_log_count"], 1)
            row = report["recent_audit_monitored_non_trusted_rows"][0]
            self.assertEqual(row["previous_status"], "candidate_auto")
            self.assertIsNone(row["new_status"])

    def test_recent_review_status_write_audit_requires_review_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ontology_concepts (
                        concept_id INTEGER PRIMARY KEY,
                        concept_name TEXT,
                        review_status TEXT,
                        updated_at TEXT
                    );
                    INSERT INTO ontology_concepts VALUES
                        (1, 'old trusted', 'human_reviewed', '2026-06-20T00:00:00+00:00');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertFalse(report["review_audit_log_exists"])
            self.assertFalse(report["review_audit_log_has_created_at"])

    def test_recent_review_status_write_audit_requires_review_audit_log_created_at(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )

            self.assertFalse(report["ok"])
            self.assertTrue(report["review_audit_log_exists"])
            self.assertFalse(report["review_audit_log_has_created_at"])

    def test_recent_review_status_write_audit_passes_when_no_recent_trusted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            markdown_path = root / "audit.md"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ontology_concepts (
                        concept_id INTEGER PRIMARY KEY,
                        concept_name TEXT,
                        review_status TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE review_audit_log (
                        id INTEGER PRIMARY KEY,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        previous_status TEXT,
                        new_status TEXT,
                        reviewer_id TEXT,
                        notes TEXT,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO ontology_concepts VALUES
                        (1, 'old trusted', 'human_reviewed', '2026-06-20T00:00:00+00:00'),
                        (2, 'recent candidate', 'candidate', '2026-06-20T02:00:00+00:00');
                    INSERT INTO review_audit_log(
                        entity_type, entity_id, action, previous_status, new_status, created_at
                    ) VALUES
                        ('ontology_concept', '2', 'status_update', 'candidate', 'candidate', '2026-06-20T02:12:00+00:00');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            report = recent_review_audit.build_audit(
                db_path,
                recent_review_audit.parse_iso_datetime("2026-06-20T01:00:00+00:00"),
            )
            recent_review_audit.write_markdown(report, markdown_path)

            self.assertTrue(report["ok"])
            self.assertTrue(report["report_only"])
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["db_writes"])
            self.assertFalse(report["approval_claim"])
            self.assertEqual(report["recent_trusted_status_table_hit_count"], 0)
            self.assertEqual(report["recent_trusted_audit_log_count"], 0)
            self.assertEqual(report["recent_monitored_non_trusted_status_table_hit_count"], 0)
            self.assertEqual(report["recent_monitored_non_trusted_audit_log_count"], 0)
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("Recent Review Status Write Audit", markdown)
            self.assertIn("Recent Monitored Non-Trusted Table Hits", markdown)
            self.assertIn("report_only", markdown)
            self.assertIn("- None", markdown)

    def test_process_snapshot_command_redacts_secret_values(self) -> None:
        command = (
            "python collect_api.py --service-key abc123 --api-key=def456 "
            "NCS_SERVICE_KEY=ghi789 --major-code 08"
        )

        redacted = ncs006_checkpoint.redact_command(command)

        self.assertNotIn("abc123", redacted)
        self.assertNotIn("def456", redacted)
        self.assertNotIn("ghi789", redacted)
        self.assertIn("--service-key [REDACTED]", redacted)
        self.assertIn("--api-key=[REDACTED]", redacted)
        self.assertIn("NCS_SERVICE_KEY=[REDACTED]", redacted)

    def test_progress_forecast_uses_recent_completed_batches(self) -> None:
        events = [
            {
                "event": "batch_complete",
                "delta_matched": 80,
                "batch_result": {
                    "elapsed_seconds": 120.0,
                    "summary": {
                        "elements_requested": 100,
                        "elements_rate_limited": 0,
                    },
                },
            },
            {
                "event": "batch_complete",
                "delta_matched": 70,
                "batch_result": {
                    "elapsed_seconds": 180.0,
                    "summary": {
                        "elements_requested": 100,
                        "elements_rate_limited": 1,
                    },
                },
            },
        ]

        forecast = ncs006_checkpoint.progress_forecast(
            events,
            {"not_collected": 150, "api_failed": 50},
        )

        self.assertEqual(forecast["sample_batch_count"], 2)
        self.assertEqual(forecast["average_elapsed_seconds"], 150.0)
        self.assertEqual(forecast["average_requested_per_batch"], 100.0)
        self.assertEqual(forecast["average_matched_per_batch"], 75.0)
        self.assertEqual(forecast["rate_limited_in_sample"], 1)
        self.assertEqual(forecast["estimated_remaining_batches"], 2)
        self.assertEqual(forecast["estimated_remaining_hours"], 0.083)

    def test_process_roles_classify_collection_processes(self) -> None:
        self.assertEqual(
            ncs006_checkpoint.process_role("python scripts\\run_element_api_collection.py"),
            "parent_runner",
        )
        self.assertEqual(
            ncs006_checkpoint.process_role("python scripts\\watch_element_api_collection.py"),
            "watchdog",
        )
        self.assertEqual(
            ncs006_checkpoint.process_role("python src\\ncs_mcp\\collect_api.py --mode elements"),
            "child_collector",
        )
        self.assertEqual(
            ncs006_checkpoint.process_role("python src\\ncs_mcp\\collect_api.py --mode training"),
            "collector_related",
        )
        self.assertEqual(
            ncs006_checkpoint.process_role_counts(
                [
                    {"role": "parent_runner"},
                    {"role": "watchdog"},
                    {"role": "child_collector"},
                    {"role": "child_collector"},
                ]
            ),
            {
                "parent_runner": 1,
                "watchdog": 1,
                "child_collector": 2,
                "collector_related": 0,
            },
        )


class ElementCollectionWatchdogTests(unittest.TestCase):
    def test_collection_process_alive_requires_collector_command_line(self) -> None:
        with patch.object(
            element_watchdog,
            "process_command_line",
            return_value="C:\\Windows\\System32\\notepad.exe",
        ):
            self.assertFalse(element_watchdog.collection_process_alive(123))

        with patch.object(
            element_watchdog,
            "process_command_line",
            return_value="python scripts\\run_element_api_collection.py",
        ):
            self.assertTrue(element_watchdog.collection_process_alive(456))

    def test_defer_while_active_collectors_records_guard_without_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "watch.jsonl"
            args = Namespace(log_path=log_path, poll_seconds=5)

            with (
                patch.object(
                    element_watchdog,
                    "active_collection_processes",
                    return_value=[{"pid": 1234, "role": "parent_runner"}],
                ),
                patch.object(element_watchdog.time, "sleep", return_value=None),
            ):
                deferred = element_watchdog.defer_while_active_collectors(
                    args,
                    element_watchdog.time.monotonic() + 0.001,
                    "test_missing_pid",
                )

            self.assertTrue(deferred)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(events[0]["event"], "active_collector_detected")
            self.assertEqual(events[0]["action"], "defer_sweep_no_duplicate_db_writer")


class ElementCollectionRunnerPidTests(unittest.TestCase):
    def test_pid_file_is_only_removed_by_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "collector.pid"

            element_runner.write_pid_file(pid_file, 123)
            element_runner.remove_pid_file_if_owned(pid_file, 456)
            self.assertTrue(pid_file.exists())

            element_runner.remove_pid_file_if_owned(pid_file, 123)
            self.assertFalse(pid_file.exists())

    def test_start_blockers_include_existing_pid_and_active_collectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "collector.pid"
            pid_file.write_text("123", encoding="utf-8")

            with (
                patch.object(
                    element_runner,
                    "process_command_line",
                    return_value="python scripts/run_element_api_collection.py",
                ),
                patch.object(
                    element_runner,
                    "active_collection_processes",
                    return_value=[{"pid": 456, "role": "child_collector"}],
                ),
            ):
                blockers = element_runner.start_blockers(pid_file, 999)

            self.assertEqual(
                blockers,
                [
                    {"pid": 123, "role": "pid_file_owner"},
                    {"pid": 456, "role": "child_collector"},
                ],
            )

    def test_start_blockers_ignore_reused_unrelated_pid_file_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "collector.pid"
            pid_file.write_text("123", encoding="utf-8")

            with (
                patch.object(
                    element_runner,
                    "process_command_line",
                    return_value="C:\\Windows\\System32\\notepad.exe",
                ),
                patch.object(element_runner, "active_collection_processes", return_value=[]),
            ):
                blockers = element_runner.start_blockers(pid_file, 999)

            self.assertEqual(blockers, [])

    def test_claim_pid_file_replaces_stale_unrelated_pid_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "collector.pid"
            pid_file.write_text("123", encoding="utf-8")

            with (
                patch.object(
                    element_runner,
                    "process_command_line",
                    return_value="C:\\Windows\\System32\\notepad.exe",
                ),
                patch.object(element_runner, "active_collection_processes", return_value=[]),
            ):
                blockers = element_runner.claim_pid_file(pid_file, 999)

            self.assertEqual(blockers, [])
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "999")

    def test_claim_pid_file_blocks_existing_collector_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "collector.pid"
            pid_file.write_text("123", encoding="utf-8")

            with (
                patch.object(
                    element_runner,
                    "process_command_line",
                    return_value="python scripts/run_element_api_collection.py",
                ),
                patch.object(element_runner, "active_collection_processes", return_value=[]),
            ):
                blockers = element_runner.claim_pid_file(pid_file, 999)

            self.assertEqual(blockers, [{"pid": 123, "role": "pid_file_owner"}])
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "123")


class HumanReviewSafeOpsCheckpointTests(unittest.TestCase):
    def test_checkpoint_latest_report_path_accepts_aihr_and_short_human_review_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readonly_root = root / "overnight_sessions" / "readonly_refresh"
            readonly_root.mkdir(parents=True)
            older = root / "aihr_human_review_provenance_reconfirmation_packet_20260620.json"
            newer = readonly_root / "human_review_provenance_reconfirmation_packet_20260621_followup.json"
            fallback = root / "fallback.json"
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")

            original_reports = human_review_checkpoint.REPORTS
            original_readonly = human_review_checkpoint.READONLY_REFRESH_REPORTS
            try:
                human_review_checkpoint.REPORTS = root
                human_review_checkpoint.READONLY_REFRESH_REPORTS = readonly_root
                self.assertEqual(
                    human_review_checkpoint._latest_report_path(
                        "aihr_human_review_provenance_reconfirmation_packet_20*.json",
                        "human_review_provenance_reconfirmation_packet_20*.json",
                        fallback=fallback,
                    ),
                    newer,
                )
            finally:
                human_review_checkpoint.REPORTS = original_reports
                human_review_checkpoint.READONLY_REFRESH_REPORTS = original_readonly

    def test_checkpoint_latest_report_path_accepts_session_recent_status_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_root = root / "overnight_sessions"
            readonly_root = session_root / "readonly_refresh"
            readonly_root.mkdir(parents=True)
            older = root / "review_status_recent_write_audit_20260620.json"
            newer = session_root / "recent_review_status_write_audit_20260707.json"
            fallback = root / "fallback.json"
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")

            original_reports = human_review_checkpoint.REPORTS
            original_readonly = human_review_checkpoint.READONLY_REFRESH_REPORTS
            try:
                human_review_checkpoint.REPORTS = root
                human_review_checkpoint.READONLY_REFRESH_REPORTS = readonly_root
                self.assertEqual(
                    human_review_checkpoint._latest_report_path(
                        "review_status_recent_write_audit_20*.json",
                        "recent_review_status_write_audit_20*.json",
                        fallback=fallback,
                    ),
                    newer,
                )
            finally:
                human_review_checkpoint.REPORTS = original_reports
                human_review_checkpoint.READONLY_REFRESH_REPORTS = original_readonly

    def test_checkpoint_default_output_is_derived_from_reconfirmation_packet_suffix(self) -> None:
        packet_path = Path(
            "reports/overnight_sessions/readonly_refresh/"
            "human_review_provenance_reconfirmation_packet_20260630_7h_extension.json"
        )

        self.assertEqual(
            human_review_checkpoint.default_output_path(packet_path, ".json"),
            human_review_checkpoint.REPORTS
            / "human_review_safe_ops_checkpoint_20260630_7h_extension.json",
        )

    def test_checkpoint_keeps_sqf_review_only_and_surfaces_provenance_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "approval_ready": False,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                    "summaries": {
                        "claim_queue": {"claim_count": 2},
                        "priority": {"priority_counts": {"P0": 1}},
                    },
                },
            )
            write_json(
                decision_audit,
                {
                    "ok": True,
                    "rows": [
                        {"decision": "blank", "guarded_import_candidate": False},
                        {"decision": "blank", "guarded_import_candidate": False},
                    ],
                },
            )
            write_json(
                guarded_plan,
                {
                    "ok": True,
                    "execution_allowed": False,
                    "planned_db_writes": 0,
                    "summary": {"pending_count": 2, "completed_decision_count": 0},
                },
            )
            write_json(
                provenance_audit,
                {
                    "ok": False,
                    "summary": {
                        "surface_count": 1,
                        "row_count": 3,
                        "rows_packet_backed": 0,
                        "rows_without_packet_backed_provenance": 3,
                        "provenance_gap_present": True,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 3,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "row_count": 3,
                    "blank_decision_count": 3,
                    "completed_decision_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(checkpoint["unresolved_provenance_gap"])
            self.assertTrue(checkpoint["sqf_review"]["safe_for_reviewer_evidence"])
            self.assertFalse(checkpoint["sqf_review"]["db_writes"])
            self.assertFalse(checkpoint["sqf_review"]["status_update_allowed"])
            self.assertEqual(checkpoint["sqf_review"]["claim_count"], 2)
            self.assertEqual(checkpoint["sqf_review"]["p0_count"], 1)
            self.assertTrue(
                checkpoint["legacy_trusted_status_provenance"]["provenance_gap_present"]
            )
            self.assertEqual(
                checkpoint["legacy_trusted_status_provenance"][
                    "rows_without_packet_backed_provenance"
                ],
                3,
            )
            self.assertEqual(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "blank_decision_count"
                ],
                3,
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "status_update_allowed"
                ]
            )
            self.assertTrue(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "human_decision_required"
                ]
            )
            self.assertTrue(checkpoint["recent_trusted_status_write_audit"]["ok"])

            self.assertTrue(checkpoint["recent_trusted_status_write_audit"]["schema_ok"])
            self.assertTrue(
                checkpoint["recent_trusted_status_write_audit"][
                    "trusted_status_values_complete"
                ]
            )
            self.assertEqual(
                checkpoint["recent_trusted_status_write_audit"][
                    "recent_trusted_status_table_hit_count"
                ],
                0,
            )
            policy = checkpoint["command_policy"]
            self.assertTrue(policy["safe_report_only_commands"])
            self.assertTrue(
                all(
                    command["db_writes"] is False
                    and command["status_update_allowed"] is False
                    for command in policy["safe_report_only_commands"]
                )
            )
            self.assertTrue(
                any(
                    "plan-sqf-guarded-import" in command["command"]
                    for command in policy["safe_report_only_commands"]
                )
            )
            self.assertTrue(
                all(
                    command["status_update_allowed"] is False
                    for command in policy["guarded_preprocessing_commands"]
                )
            )
            self.assertTrue(
                any(
                    "active recommendation scoring" in rule
                    for rule in policy["prohibited_without_explicit_human_authorization"]
                )
            )

    def test_checkpoint_reads_sqf_db_human_review_summary_counts(self) -> None:
        readiness = {
            "ok": True,
            "allowed_use": "supplementary_review_context_only",
            "approval_ready": False,
            "db_writes": False,
            "status_update_allowed": False,
            "used_for_scoring": False,
            "human_review_summary": {
                "claim_count": 80,
                "p0_count": 36,
            },
        }
        decision_audit = {
            "ok": True,
            "rows": [{"decision": "blank", "guarded_import_candidate": False}],
        }
        guarded_plan = {
            "ok": True,
            "execution_allowed": False,
            "planned_db_writes": 0,
            "summary": {"pending_count": 1, "completed_decision_count": 0},
        }

        summary = human_review_checkpoint.summarize_sqf(
            readiness,
            decision_audit,
            guarded_plan,
        )

        self.assertEqual(summary["claim_count"], 80)
        self.assertEqual(summary["p0_count"], 36)

    def test_checkpoint_is_not_safe_when_source_audits_are_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": False, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": True,
                    "summary": {
                        "row_count": 0,
                        "rows_packet_backed": 0,
                        "rows_without_packet_backed_provenance": 0,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "row_count": 0,
                    "blank_decision_count": 0,
                    "completed_decision_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertFalse(checkpoint["sqf_review"]["ok"])
            self.assertFalse(checkpoint["sqf_review"]["safe_for_reviewer_evidence"])
            self.assertFalse(checkpoint["sqf_review"]["source_decision_audit_ok"])

    def test_checkpoint_is_not_safe_when_provenance_sources_are_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": True, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": False,
                    "summary": {
                        "row_count": 0,
                        "rows_packet_backed": 0,
                        "rows_without_packet_backed_provenance": 0,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": False,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": False,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "row_count": 0,
                    "blank_decision_count": 0,
                    "completed_decision_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(checkpoint["sqf_review"]["safe_for_reviewer_evidence"])
            self.assertFalse(checkpoint["legacy_trusted_status_provenance"]["ok"])
            self.assertFalse(
                checkpoint["legacy_trusted_status_provenance"]["reconfirmation_packet"]["ok"]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"]["ok"]
            )

    def test_checkpoint_rejects_incomplete_recent_status_audit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": True, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": True,
                    "summary": {
                        "row_count": 0,
                        "rows_packet_backed": 0,
                        "rows_without_packet_backed_provenance": 0,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "row_count": 0,
                    "blank_decision_count": 0,
                    "completed_decision_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_json(
                recent_status_audit,
                {
                    "ok": True,
                    "schema": "aihr_recent_review_status_write_audit_v1",
                    "read_only": True,
                    "trusted_status_values": ["accepted"],
                    "recent_trusted_status_table_hit_count": 0,
                    "recent_trusted_audit_log_count": 0,
                    "recent_unverifiable_generic_timestamp_count": 0,
                    "unverifiable_no_timestamp_table_count": 0,
                    "invalid_timestamp_table_row_count": 0,
                },
            )

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertFalse(
                checkpoint["recent_trusted_status_write_audit"][
                    "trusted_status_values_complete"
                ]
            )
            self.assertFalse(
                checkpoint["recent_trusted_status_write_audit"][
                    "review_audit_log_exists"
                ]
            )

    def test_checkpoint_rejects_recent_status_audit_with_unsafe_policy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = write_safe_human_review_checkpoint_inputs(
                root,
                recent_status_kwargs={
                    "db_writes": True,
                    "status_update_allowed": True,
                    "approval_claim": True,
                },
            )

            checkpoint = human_review_checkpoint.build_checkpoint(args)
            recent_status = checkpoint["recent_trusted_status_write_audit"]

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(recent_status["source_ok"])
            self.assertFalse(recent_status["ok"])
            self.assertTrue(recent_status["policy_flags_present"])
            self.assertFalse(recent_status["policy_flags_ok"])
            self.assertTrue(recent_status["db_writes"])
            self.assertTrue(recent_status["status_update_allowed"])
            self.assertTrue(recent_status["approval_claim"])

    def test_checkpoint_requires_human_decision_required_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": True, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": True,
                    "summary": {
                        "row_count": 0,
                        "rows_packet_backed": 0,
                        "rows_without_packet_backed_provenance": 0,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "row_count": 0,
                    "blank_decision_count": 0,
                    "completed_decision_count": 0,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": False,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "human_decision_required"
                ]
            )

    def test_checkpoint_rejects_reconfirmation_decision_audit_packet_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            reconfirm_decision_audit = root / "reconfirm_decision_audit.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": True, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": True,
                    "summary": {
                        "row_count": 1,
                        "rows_packet_backed": 1,
                        "rows_without_packet_backed_provenance": 0,
                        "provenance_gap_present": False,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "source_packet": human_review_checkpoint.rel(reconfirm_packet),
                    "source_packet_sha256": "sha256:packet",
                    "row_count": 1,
                    "blank_decision_count": 0,
                    "completed_decision_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_json(
                reconfirm_decision_audit,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
                    "csv": str(reconfirm_decision_sheet.with_suffix(".csv")),
                    "source_packet": str(root / "different_packet.json"),
                    "source_packet_sha256": "sha256:packet",
                    "row_count": 1,
                    "source_packet_row_count": 1,
                    "pending_decision_count": 0,
                    "completed_decision_count": 1,
                    "invalid_decision_count": 0,
                    "missing_required_field_row_count": 0,
                    "source_mismatch_count": 0,
                    "source_identity_mismatch_count": 0,
                    "source_decision_packet_not_found_count": 0,
                    "invalid_evidence_refs_json_count": 0,
                    "unsafe_flag_count": 0,
                    "duplicate_csv_key_count": 0,
                    "missing_packet_row_count": 0,
                    "unexpected_csv_row_count": 0,
                    "missing_csv_columns": [],
                    "action_eligible_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "guarded_apply_ready": False,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                reconfirm_decision_audit=reconfirm_decision_audit,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "source_packet_binding_ok"
                ]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_audit"][
                    "source_packet_binding_ok"
                ]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_audit"][
                    "checkpoint_contract_ok"
                ]
            )

    def test_checkpoint_rejects_reconfirmation_packet_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readiness = root / "readiness.json"
            decision_audit = root / "decision_audit.json"
            guarded_plan = root / "guarded_plan.json"
            provenance_audit = root / "provenance_audit.json"
            reconfirm_packet = root / "reconfirm_packet.json"
            reconfirm_decision_sheet = root / "reconfirm_decision_sheet.json"
            reconfirm_decision_audit = root / "reconfirm_decision_audit.json"
            recent_status_audit = root / "recent_status_audit.json"

            write_json(
                readiness,
                {
                    "ok": True,
                    "allowed_use": "supplementary_review_context_only",
                    "db_writes": False,
                    "status_update_allowed": False,
                    "used_for_scoring": False,
                },
            )
            write_json(decision_audit, {"ok": True, "rows": []})
            write_json(
                guarded_plan,
                {"ok": True, "execution_allowed": False, "planned_db_writes": 0},
            )
            write_json(
                provenance_audit,
                {
                    "ok": True,
                    "summary": {
                        "row_count": 1,
                        "rows_packet_backed": 1,
                        "rows_without_packet_backed_provenance": 0,
                        "provenance_gap_present": False,
                        "db_writes": False,
                    },
                },
            )
            write_json(
                reconfirm_packet,
                {
                    "ok": True,
                    "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                    "row_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                },
            )
            packet_ref = human_review_checkpoint.rel(reconfirm_packet)
            write_json(
                reconfirm_decision_sheet,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "source_packet": packet_ref,
                    "source_packet_sha256": "sha256:wrong",
                    "row_count": 1,
                    "blank_decision_count": 0,
                    "completed_decision_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                },
            )
            write_json(
                reconfirm_decision_audit,
                {
                    "ok": True,
                    "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
                    "csv": human_review_checkpoint.rel(
                        reconfirm_decision_sheet.with_suffix(".csv")
                    ),
                    "source_packet": packet_ref,
                    "source_packet_sha256": "sha256:wrong",
                    "row_count": 1,
                    "source_packet_row_count": 1,
                    "pending_decision_count": 0,
                    "completed_decision_count": 1,
                    "invalid_decision_count": 0,
                    "missing_required_field_row_count": 0,
                    "source_mismatch_count": 0,
                    "source_identity_mismatch_count": 0,
                    "source_decision_packet_not_found_count": 0,
                    "invalid_evidence_refs_json_count": 0,
                    "unsafe_flag_count": 0,
                    "duplicate_csv_key_count": 0,
                    "missing_packet_row_count": 0,
                    "unexpected_csv_row_count": 0,
                    "missing_csv_columns": [],
                    "action_eligible_count": 1,
                    "db_writes": False,
                    "status_update_allowed": False,
                    "approval_claim": False,
                    "guarded_apply_ready": False,
                },
            )
            write_recent_status_audit(recent_status_audit)

            args = Namespace(
                sqf_readiness=readiness,
                sqf_decision_audit=decision_audit,
                sqf_guarded_plan=guarded_plan,
                provenance_audit=provenance_audit,
                reconfirm_packet=reconfirm_packet,
                reconfirm_decision_sheet=reconfirm_decision_sheet,
                reconfirm_decision_audit=reconfirm_decision_audit,
                recent_status_audit=recent_status_audit,
            )
            checkpoint = human_review_checkpoint.build_checkpoint(args)

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "source_packet_binding_ok"
                ]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"][
                    "source_packet_hash_ok"
                ]
            )
            self.assertTrue(
                checkpoint["legacy_trusted_status_reconfirmation_decision_audit"][
                    "source_packet_binding_ok"
                ]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_audit"][
                    "source_packet_hash_ok"
                ]
            )
            self.assertFalse(
                checkpoint["legacy_trusted_status_reconfirmation_decision_audit"][
                    "checkpoint_contract_ok"
                ]
            )


class SqfDbReadinessCheckpointTests(unittest.TestCase):
    def test_sqf_db_readiness_is_review_context_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            corpus = root / "corpus.json"
            safe_ops = root / "safe_ops.json"
            conn = sqlite3.connect(db_path)
            try:
                for table in sqf_db_checkpoint.SQF_TABLES:
                    conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
                    conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
                conn.commit()
            finally:
                conn.close()
            write_json(
                corpus,
                {
                    "ok": True,
                    "approval_ready": False,
                    "used_for_scoring": False,
                    "summary": {
                        "official_file_count": 1,
                        "official_downloaded_count": 1,
                        "document_count": 1,
                        "page_count": 2,
                        "chunk_count": 3,
                        "chunk_match_count": 4,
                        "sqf_ncs_candidate_count": 5,
                        "empty_document_count": 0,
                    },
                    "quality_gates": {
                        "official_files_downloaded_and_present": True,
                        "chunk_corpus_present": True,
                    },
                },
            )
            write_json(
                safe_ops,
                {
                    "ok": True,
                    "sqf_review": {
                        "safe_for_reviewer_evidence": True,
                        "allowed_use": "supplementary_review_context_only",
                        "approval_ready": False,
                        "used_for_scoring": False,
                        "status_update_allowed": False,
                        "claim_count": 10,
                        "p0_count": 3,
                        "pending_decision_count": 10,
                        "guarded_import_candidate_count": 0,
                        "planned_db_writes": 0,
                    },
                },
            )

            checkpoint = sqf_db_checkpoint.build_checkpoint(
                Namespace(db_path=db_path, corpus_audit=corpus, safe_ops=safe_ops)
            )

            self.assertTrue(checkpoint["ok"])
            self.assertEqual(checkpoint["status"], "usable_for_human_review")
            self.assertEqual(checkpoint["allowed_use"], "supplementary_review_context_only")
            self.assertFalse(checkpoint["approval_ready"])
            self.assertFalse(checkpoint["used_for_scoring"])
            self.assertFalse(checkpoint["status_update_allowed"])
            self.assertFalse(checkpoint["db_writes"])
            self.assertTrue(all(checkpoint["gates"].values()))
            self.assertEqual(checkpoint["human_review_summary"]["claim_count"], 10)

    def test_sqf_db_readiness_blocks_scoring_or_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            corpus = root / "corpus.json"
            safe_ops = root / "safe_ops.json"
            conn = sqlite3.connect(db_path)
            try:
                for table in sqf_db_checkpoint.SQF_TABLES:
                    conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
                    conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
                conn.commit()
            finally:
                conn.close()
            write_json(
                corpus,
                {
                    "approval_ready": True,
                    "used_for_scoring": True,
                    "summary": {
                        "official_file_count": 1,
                        "official_downloaded_count": 1,
                        "document_count": 1,
                        "page_count": 2,
                        "chunk_count": 3,
                        "chunk_match_count": 4,
                        "sqf_ncs_candidate_count": 5,
                        "empty_document_count": 0,
                    },
                    "quality_gates": {
                        "official_files_downloaded_and_present": True,
                        "chunk_corpus_present": True,
                    },
                },
            )
            write_json(
                safe_ops,
                {
                    "ok": True,
                    "sqf_review": {
                        "safe_for_reviewer_evidence": True,
                        "allowed_use": "supplementary_review_context_only",
                        "approval_ready": True,
                        "used_for_scoring": True,
                        "status_update_allowed": True,
                    },
                },
            )

            checkpoint = sqf_db_checkpoint.build_checkpoint(
                Namespace(db_path=db_path, corpus_audit=corpus, safe_ops=safe_ops)
            )

            self.assertFalse(checkpoint["ok"])
            self.assertFalse(checkpoint["gates"]["active_scoring_blocked"])
            self.assertFalse(checkpoint["gates"]["approval_not_auto_ready"])


class OvernightNcsSqfWorkCheckpointTests(unittest.TestCase):
    def test_overnight_checkpoint_combines_safe_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ncs006 = root / "ncs006.json"
            sqf_db = root / "sqf_db.json"
            human_review = root / "human_review.json"
            write_json(
                ncs006,
                {
                    "collection_processes": [{}, {}, {}],
                    "element_api_status": {
                        "totals": {
                            "matched": 100,
                            "total": 200,
                            "matched_ratio": 0.5,
                            "not_collected": 80,
                            "api_failed": 20,
                        },
                        "by_major": [
                            {
                                "major_code": "19",
                                "major_name_display": "전기·전자",
                                "not_collected": 80,
                                "api_failed": 20,
                                "matched": 100,
                                "total": 200,
                            }
                        ],
                    },
                    "run_log": {
                        "active_or_incomplete_batch": {
                            "major_code": "19",
                            "phase": "uncollected",
                        },
                        "completed_batches_since_latest_start": 5,
                        "rate_limited_since_latest_start": 0,
                    },
                    "monitoring": {
                        "status": "within_child_timeout",
                        "active_batch_age_seconds": 30,
                        "timeout_exceeded": False,
                    },
                    "throughput_forecast": {"estimated_remaining_hours": 1.5},
                },
            )
            write_json(
                sqf_db,
                {
                    "ok": True,
                    "status": "usable_for_human_review",
                    "allowed_use": "supplementary_review_context_only",
                    "approval_ready": False,
                    "used_for_scoring": False,
                    "status_update_allowed": False,
                    "corpus_summary": {
                        "official_downloaded_count": 105,
                        "official_file_count": 105,
                        "page_count": 24088,
                        "chunk_count": 9108,
                        "sqf_ncs_candidate_count": 22642,
                    },
                    "human_review_summary": {
                        "claim_count": 80,
                        "p0_count": 36,
                        "pending_decision_count": 80,
                        "guarded_import_candidate_count": 0,
                    },
                },
            )
            write_json(
                human_review,
                {
                    "ok": True,
                    "sqf_review": {
                        "safe_for_reviewer_evidence": True,
                        "pending_decision_count": 80,
                        "guarded_import_candidate_count": 0,
                        "planned_db_writes": 0,
                    },
                    "legacy_trusted_status_provenance": {
                        "legacy_trusted_status_rows_pending_reconfirmation": 34,
                        "rows_without_packet_backed_provenance": 0,
                        "provenance_gap_present": False,
                    },
                    "legacy_trusted_status_reconfirmation_decision_sheet": {
                        "blank_decision_count": 0,
                        "completed_decision_count": 34,
                    },
                    "reviewer_safe_artifacts": ["reports/reviewer.md"],
                },
            )

            checkpoint = overnight_checkpoint.build_checkpoint(
                Namespace(ncs006=ncs006, sqf_db=sqf_db, human_review=human_review)
            )

            self.assertTrue(checkpoint["ok"])
            self.assertEqual(checkpoint["ncs006_collection"]["active_major_name"], "전기·전자")
            self.assertEqual(checkpoint["sqf_db_readiness"]["allowed_use"], "supplementary_review_context_only")
            self.assertEqual(
                checkpoint["human_review_safe_ops"]["rows_without_packet_backed_provenance"],
                0,
            )
            self.assertFalse(checkpoint["human_review_safe_ops"]["unresolved_provenance_gap"])
            self.assertFalse(checkpoint["policy"]["db_writes"])

    def test_overnight_checkpoint_rejects_unresolved_human_review_provenance_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ncs006 = root / "ncs006.json"
            sqf_db = root / "sqf_db.json"
            human_review = root / "human_review.json"
            write_json(
                ncs006,
                {
                    "element_api_status": {
                        "totals": {"matched": 1, "total": 1, "matched_ratio": 1.0},
                        "by_major": [],
                    },
                    "monitoring": {"timeout_exceeded": False},
                },
            )
            write_json(
                sqf_db,
                {
                    "ok": True,
                    "approval_ready": False,
                    "used_for_scoring": False,
                    "status_update_allowed": False,
                },
            )
            write_json(
                human_review,
                {
                    "ok": True,
                    "sqf_review": {
                        "safe_for_reviewer_evidence": True,
                        "planned_db_writes": 0,
                    },
                    "legacy_trusted_status_provenance": {
                        "rows_without_packet_backed_provenance": 2,
                        "provenance_gap_present": True,
                    },
                    "legacy_trusted_status_reconfirmation_decision_sheet": {
                        "blank_decision_count": 2,
                    },
                },
            )

            checkpoint = overnight_checkpoint.build_checkpoint(
                Namespace(ncs006=ncs006, sqf_db=sqf_db, human_review=human_review)
            )

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(checkpoint["human_review_safe_ops"]["unresolved_provenance_gap"])

    def test_overnight_checkpoint_rejects_sqf_scoring_or_status_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ncs006 = root / "ncs006.json"
            sqf_db = root / "sqf_db.json"
            human_review = root / "human_review.json"
            write_json(
                ncs006,
                {
                    "element_api_status": {
                        "totals": {"matched": 1, "total": 1, "matched_ratio": 1.0},
                        "by_major": [],
                    },
                    "monitoring": {"timeout_exceeded": False},
                },
            )
            write_json(
                sqf_db,
                {
                    "ok": True,
                    "approval_ready": True,
                    "used_for_scoring": True,
                    "status_update_allowed": True,
                },
            )
            write_json(
                human_review,
                {
                    "ok": True,
                    "sqf_review": {
                        "safe_for_reviewer_evidence": True,
                        "planned_db_writes": 1,
                    },
                },
            )

            checkpoint = overnight_checkpoint.build_checkpoint(
                Namespace(ncs006=ncs006, sqf_db=sqf_db, human_review=human_review)
            )

            self.assertFalse(checkpoint["ok"])
            self.assertTrue(checkpoint["sqf_db_readiness"]["used_for_scoring"])
            self.assertEqual(checkpoint["human_review_safe_ops"]["sqf_planned_db_writes"], 1)


if __name__ == "__main__":
    unittest.main()
