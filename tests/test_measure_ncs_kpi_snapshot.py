from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc

SCRIPT = ROOT / "scripts" / "measure_ncs_kpi_snapshot.py"
spec = importlib.util.spec_from_file_location("measure_ncs_kpi_snapshot", SCRIPT)
assert spec is not None and spec.loader is not None
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)

DEFINITION = json.loads((ROOT / "docs" / "ncs_kpi_guardrails.v1.json").read_text(encoding="utf-8"))
METRICS = {item["id"]: item for item in DEFINITION["metrics"]}


def seed(conn, units: dict[str, str | None]) -> None:
    """units maps unit_code -> qualification collection status (None = never attempted)."""
    timestamp = now_utc()
    for major in sorted({code[:2] for code in units}):
        conn.execute(
            """
            INSERT INTO classifications(major_code, major_name, middle_code, middle_name,
                                        small_code, small_name, sub_code, sub_name)
            VALUES (?, 'm', '01', 'm', '01', 's', '01', 's')
            """,
            (major,),
        )
    for code, status in units.items():
        classification_id = conn.execute(
            "SELECT classification_id FROM classifications WHERE major_code = ?", (code[:2],)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO competency_units(unit_code, base_unit_code, unit_version, unit_name_raw, unit_level_raw,
                                         classification_id, api_definition, api_match_status, created_at, updated_at)
            VALUES (?, ?, '23v1', ?, '3', ?, '', 'matched', ?, ?)
            """,
            (code, code.split("_")[0], code, classification_id, timestamp, timestamp),
        )
        if status:
            conn.execute(
                "INSERT INTO ncs_qualification_collection_status(unit_code, collection_status, updated_at) VALUES (?, ?, ?)",
                (code, status, timestamp),
            )
    conn.commit()


class KpiSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = connect(self.root / "ncs.db")
        initialize_database(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_ratio_bands_follow_contract(self) -> None:
        k10 = METRICS["K10"]
        self.assertEqual(snapshot.ratio_status(0.95, k10), "pass")
        self.assertEqual(snapshot.ratio_status(0.4838, k10), "warn")
        self.assertEqual(snapshot.ratio_status(0.05, k10), "fail")
        self.assertEqual(snapshot.ratio_status(None, k10), "unknown")

    def test_k10_counts_collected_and_empty_but_not_error_or_unattempted(self) -> None:
        seed(self.conn, {"0101_23v1": "collected", "0102_23v1": "empty", "0103_23v1": "error",
                         "0201_23v1": None})
        result = snapshot.measure_k10(self.conn, METRICS["K10"])
        self.assertEqual((result["numerator"], result["denominator"]), (2, 4))
        self.assertEqual(result["value"], 0.5)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["status_counts"], {"collected": 1, "empty": 1, "error": 1, "not_attempted": 1})
        self.assertEqual(result["attempted_ratio"], 0.75)
        self.assertEqual(result["per_major"]["02"]["value"], 0.0)

    def test_direct_slot_needs_valid_unique_course_and_goal_text_hit(self) -> None:
        seen: set[int] = set()
        valid = {1, 2}
        self.assertTrue(snapshot.slot_is_direct({"training_course_id": 1, "goal_direct_concept_hits": 2}, valid, seen))
        self.assertFalse(snapshot.slot_is_direct({"training_course_id": 1, "goal_direct_concept_hits": 2}, valid, seen))
        self.assertFalse(snapshot.slot_is_direct({"training_course_id": 3, "goal_direct_concept_hits": 2}, valid, seen))
        self.assertFalse(snapshot.slot_is_direct({"training_course_id": 2, "goal_direct_concept_hits": 0,
                                                  "goal_token_concept_hits": 5}, valid, seen))

    def test_g03_flags_unknown_ids_and_unsupported_primary_slots(self) -> None:
        slots = [
            {"training_course_id": 1, "tier": "primary", "direct_unit_evidence": True, "scope_alignment": "direct"},
            {"training_course_id": 9, "tier": "adjacent", "direct_unit_evidence": False, "scope_alignment": "distant"},
            {"training_course_id": 2, "tier": "primary", "direct_unit_evidence": False, "scope_alignment": "adjacent"},
            {"training_course_id": 2, "tier": "supplemental", "direct_unit_evidence": False, "scope_alignment": None},
        ]
        self.assertEqual(snapshot.g03_violations(slots, {1, 2}), {
            "invalid_source_id": 1, "primary_without_direct_unit_evidence": 1, "primary_outside_target_scope": 1})
        self.assertEqual(snapshot.g03_violations(slots[:1], {1}), {})

    def test_g10_reads_only_secret_like_env_values_and_reports_paths(self) -> None:
        env = self.root / ".env"
        env.write_text("NCS_SERVICE_KEY=\"abcdefgh1234\"\n# OTHER_KEY=commented-out-value\nNCS_DB_PATH=data/ncs.db\n"
                       "SHORT_TOKEN=abc\n", encoding="utf-8")
        values = snapshot.env_secret_values(env)
        self.assertEqual(values, ["abcdefgh1234"])
        clean, leaked = self.root / "clean.py", self.root / "leaked.md"
        clean.write_text("key = os.environ['NCS_SERVICE_KEY']\n", encoding="utf-8")
        leaked.write_text("serviceKey=abcdefgh1234\n", encoding="utf-8")
        self.assertEqual(snapshot.files_containing(values, [clean, leaked, self.root / "missing.txt"]), [str(leaked)])

    def test_scenario_bootstrap_is_seeded_and_brackets_mean(self) -> None:
        ratios = [0.0, 1 / 3, 1.0, 0.0, 2 / 3]
        first = snapshot.scenario_bootstrap(ratios, resamples=500)
        self.assertEqual(first, snapshot.scenario_bootstrap(ratios, resamples=500))
        self.assertLessEqual(first["lower"], sum(ratios) / len(ratios))
        self.assertGreaterEqual(first["upper"], sum(ratios) / len(ratios))
        self.assertIsNone(snapshot.scenario_bootstrap([]))

    def test_g01_reports_unknown_without_baseline_then_counts_changes(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = OFF")  # manifest rows only; no element lineage needed
        self.conn.execute("INSERT INTO ksa_items(ksa_id, element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw) "
                          "VALUES (1, 1, '01', 'knowledge', '1', 'a'), (2, 1, '01', 'knowledge', '2', 'b')")
        first_path = self.root / "first.tsv.gz"
        first = snapshot.measure_g01(self.conn, None, first_path)
        self.assertEqual((first["status"], first["violation_count"], first["row_count"]), ("unknown", None, 2))
        same = snapshot.measure_g01(self.conn, first_path, None)
        self.assertEqual((same["status"], same["violation_count"]), ("pass", 0))
        self.conn.execute("UPDATE ksa_items SET ksa_text_raw = 'changed' WHERE ksa_id = 1")
        self.conn.execute("DELETE FROM ksa_items WHERE ksa_id = 2")
        self.conn.execute("INSERT INTO ksa_items(ksa_id, element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw) "
                          "VALUES (3, 1, '01', 'knowledge', '3', 'c')")
        changed = snapshot.measure_g01(self.conn, first_path, None)
        self.assertEqual((changed["changed"], changed["deleted"], changed["added"]), (1, 1, 1))
        self.assertEqual((changed["violation_count"], changed["status"]), (2, "fail"))

    def test_statement_audit_classifies_legacy_framework_and_write_sql(self) -> None:
        audit = snapshot.StatementAudit(self.conn)
        self.assertIn("ncs_learning_modules", audit.legacy_tables)
        self.assertTrue(any(name.startswith("sqf_") for name in audit.legacy_tables))
        self.assertTrue(all(name.startswith("ncs_reference_") for name in audit.framework_tables))
        for sql in ("SELECT * FROM ncs_training_courses WHERE train_goal LIKE '%sqf%'",
                    "SELECT * FROM ncs_learning_modules", "SELECT * FROM ncs_reference_chunks",
                    "CREATE TEMP TABLE temp_rank_candidate_concepts(concept_id INTEGER PRIMARY KEY)",
                    "INSERT OR IGNORE INTO temp_rank_candidate_concepts(concept_id) VALUES (1)",
                    "  insert into training_recommendations(query) values ('x')"):
            audit(sql)
        self.assertEqual(audit.counts, {"statements": 6, "legacy": 1, "framework": 1, "temp_writes": 2, "writes": 1})

    def test_serving_guardrails_hold_framework_access_for_review(self) -> None:
        audit = snapshot.StatementAudit(self.conn)
        clean = snapshot.serving_guardrails(audit, Counter())
        self.assertEqual((clean["G04"]["status"], clean["G04"]["violation_count"]), ("pass", 0))
        self.assertEqual((clean["G08"]["status"], clean["G08"]["violation_count"]), ("pass", 0))
        framework = snapshot.serving_guardrails(audit, Counter(plans_touching_framework_tables=2))
        self.assertEqual((framework["G04"]["status"], framework["G04"]["violation_count"]), ("review_required", 0))
        bad = snapshot.serving_guardrails(audit, Counter(scenarios_declaring_legacy_use=1, readonly_write_errors=1,
                                                         plans_with_writes=1))
        self.assertEqual((bad["G04"]["status"], bad["G04"]["violation_count"]), ("fail", 1))
        self.assertEqual((bad["G08"]["status"], bad["G08"]["violation_count"]), ("fail", 2))
        self.assertEqual(snapshot.serving_guardrails(None, Counter())["G08"]["status"], "unknown")
        self.assertNotIn("plan_ncs_education_path", snapshot.serving_guardrails(audit, Counter(), False)["G04"]["scope"])

    def test_g06_accepts_consistent_rows_and_flags_unsupported_fit_or_ready(self) -> None:
        from ncs_mcp.training_recommendation import (
            _delivery_constraint_fit_summary, _facility_constraint_fit, _method_constraint_fit, _time_constraint_fit)

        def row(course: dict, review: dict) -> dict:
            method = _method_constraint_fit(course, ["집체훈련"])
            time = _time_constraint_fit(course, 24)
            facility = _facility_constraint_fit(course, ["강의실"])
            summary = _delivery_constraint_fit_summary(method_fit=method, time_fit=time, facility_fit=facility)
            return {"course_fit": course, "facility_constraint_fit": facility, "human_review": review,
                    "delivery_operation": {"method_constraint_fit": method, "time_constraint_fit": time,
                                           "constraint_fit": summary}}

        fit = row({"hours": 16, "methods": ["집체훈련"], "facilities": ["강의실"]}, {"severity": "ready", "flags": []})
        self.assertEqual(snapshot.g06_row_violations(fit), [])
        over = row({"hours": 40, "methods": ["집체훈련"], "facilities": []},
                   {"severity": "needs_review", "flags": ["delivery:time_over", "delivery:facility_unknown"]})
        self.assertEqual(snapshot.g06_row_violations(over), [])
        over["human_review"] = {"severity": "ready", "flags": ["delivery:time_over"]}
        self.assertEqual(snapshot.g06_row_violations(over),
                         ["unfit_dimension_marked_ready", "missing_review_flag:facility_unknown"])
        fit["facility_constraint_fit"] = {**fit["facility_constraint_fit"], "missing": ["실습실"]}
        fit["delivery_operation"]["time_constraint_fit"]["actual_hours"] = 30.0
        self.assertEqual(snapshot.g06_row_violations(fit), ["time_fit_unsupported", "facility_fit_unsupported"])

    def test_g02_separates_packet_backed_dashboard_and_unbacked_trusted_rows(self) -> None:
        from unittest import mock

        now = now_utc()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        for alias_id, status in ((9001, "human_reviewed"), (9002, "human_reviewed"), (9003, "candidate")):
            self.conn.execute("INSERT INTO ncs_query_aliases(alias_id, alias_text, normalized_query, review_status, "
                              "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (alias_id, f"a{alias_id}", f"q{alias_id}", status, now, now))
        self.conn.execute("INSERT INTO ontology_concept_label_candidates(label_id, concept_id, concept_type, source_text, "
                          "label_text, normalized_label_key, source_method, review_status, created_at, updated_at) "
                          "VALUES (7, 1, 'knowledge', 's', 'l', 'l', 'm', 'human_reviewed', ?, ?)", (now, now))
        self.conn.execute("INSERT INTO learning_module_unit_links(learn_module_seq, unit_code, link_method, review_status, "
                          "created_at, updated_at) VALUES (1, '0101_23v1', 'm', 'reviewed', ?, ?)", (now, now))
        self.conn.execute("INSERT INTO ontology_concepts(concept_name, normalized_key, concept_type, definition, "
                          "definition_status, relation_status, review_status, created_at, updated_at) VALUES "
                          "('인력 계획', '인력계획', 'knowledge', ?, 'defined', 'unlinked', 'raw', ?, ?)",
                          ("인력 계획: 업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.", now, now))
        packet = self.root / "decision_audit.json"
        packet.write_text("{}", encoding="utf-8")
        from ncs_mcp.review_safety import review_packet_sha256
        digest = "sha256:" + review_packet_sha256(packet)
        audit_sql = ("INSERT INTO review_audit_log(entity_type, entity_id, action, new_status, reviewer_id, "
                     "source_decision_packet, source_artifact_hash, rationale, evidence_refs_json, created_at) "
                     "VALUES (?, ?, 'approve', 'human_reviewed', ?, ?, ?, ?, ?, ?)")
        self.conn.execute(audit_sql, ("ncs_query_alias", "9001", "reviewer", "reports/decision_audit.json", digest,
                                      "checked", '["alias_id:9001"]', now))
        self.conn.execute(audit_sql, ("ncs_query_alias", "9002", "codex_report_review", None, None, None, None, now))
        self.conn.execute(audit_sql, ("ontology_concept_label_candidate", "7", "reviewer",
                                      "ksa_review_dashboard_one_click_v1:label:7:approve", "sha256:" + "0" * 64,
                                      "one_click_label_approve", '["label_id:7"]', now))
        with mock.patch("ncs_mcp.quality_gates._resolve_review_packet_artifact",
                        side_effect=lambda ref: packet if ref == "reports/decision_audit.json" else None):
            report = snapshot.measure_g02(self.conn)
        self.assertEqual(report["packet_backed_rows"], 1)
        self.assertEqual(report["unbacked_trusted_rows_by_table"],
                         {"ncs_query_aliases": 1, "ontology_concept_label_candidates": 1})
        self.assertEqual(report["unbacked_by_reason"],
                         {"dashboard_decision_without_packet_file": 1, "missing_packet_fields": 1})
        self.assertEqual(report["legacy_trusted_rows_not_counted"], {"learning_module_unit_links": 1})
        self.assertEqual(report["promoted_boilerplate_definitions"], 1)
        self.assertEqual((report["violation_count"], report["status"]), (3, "fail"))

    def test_static_proof_gaps_count_missing_empty_and_bad_role_artifacts(self) -> None:
        snapshots = {"readiness_json": {"exists": True, "non_empty": True, "role_contract_ok": True},
                     "demo_json": {"exists": True, "non_empty": False, "role_contract_ok": True},
                     "queue_run_json": {"exists": False, "non_empty": False, "role_contract_ok": False},
                     "demo_html": {"exists": True, "non_empty": True, "role_contract_ok": None}}

        class Harness:
            @staticmethod
            def default_aihr_dashboard_static_artifacts():
                return {name: self.root / f"{name}.json" for name in snapshots}

            @staticmethod
            def _static_artifact_snapshot(name, path):
                return snapshots[name]

        gaps, summary = snapshot.static_proof_gaps(Harness())
        self.assertEqual([item["name"] for item in gaps], ["demo_html", "demo_json", "queue_run_json"])
        self.assertEqual(summary["artifacts_checked"], 4)
        self.assertEqual(summary["readiness_json"], str(self.root / "readiness_json.json"))

    def test_g09_reports_stale_and_missing_deploy_copies(self) -> None:
        def write(relative: str, text: str) -> None:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        for name in ("db.py", "search/core.py", "server.py"):
            write(f"src/ncs_mcp/{name}", name)
        write("deploy/vercel_mcp_app/src/ncs_mcp/db.py", "db.py")
        write("deploy/vercel_mcp_app/src/ncs_mcp/search/core.py", "old")
        write("deploy/vercel_mcp_app/src/ncs_mcp/only_in_deploy.py", "x")
        for name in snapshot.DEPLOY_API_ENTRYPOINTS:
            write(f"api/{name}", name)
            write(f"deploy/vercel_mcp_app/api/{name}", name)
        report = snapshot.measure_g09_source_identity(self.root)
        self.assertEqual(report["files_checked"], 3 + len(snapshot.DEPLOY_API_ENTRYPOINTS))
        self.assertEqual(report["stale_copies"], ["deploy/vercel_mcp_app/src/ncs_mcp/search/core.py"])
        self.assertEqual(report["missing_files"], ["deploy/vercel_mcp_app/src/ncs_mcp/only_in_deploy.py"])
        self.assertEqual((report["violation_count"], report["status"]), (2, "fail"))

    def test_evaluation_identity_reads_contract_and_newest_deployed_release(self) -> None:
        (self.root / "mcp").mkdir()
        (self.root / "mcp" / "ncs-tool-contract.json").write_text(json.dumps({
            "surface": {"mode": "public"}, "tools": [{"name": "ncs_search"}, {"name": "ncs_analysis"}],
            "advanced_tools_available": ["plan_ncs_education_path"]}), encoding="utf-8")
        for version, status, updated in (("v1", "deployed", "2026-10-11"), ("v2", "failed", "2026-10-13"),
                                          ("v3", "deployed", "2026-10-12")):
            folder = self.root / ".state" / "ncs-data-builder" / "versions" / version
            folder.mkdir(parents=True)
            (folder / "release.json").write_text(json.dumps({
                "status": status, "version": version, "updated_at": updated, "source_commit": f"c-{version}",
                "production_after_promotion": {"deployment_id": f"dpl_{version}"}}), encoding="utf-8")
        identity = snapshot.evaluation_identity(self.root)
        self.assertEqual(identity["public_contract"]["active_tools"], ["ncs_analysis", "ncs_search"])
        self.assertEqual(identity["public_contract"]["advanced_tools_not_active"], ["plan_ncs_education_path"])
        self.assertEqual((identity["latest_builder_release"]["version"],
                          identity["latest_builder_release"]["production_deployment_id"]), ("v3", "dpl_v3"))
        self.assertEqual(snapshot.evaluation_identity(self.root / "missing")["latest_builder_release"], None)

    def test_synthetic_packet_prefixes_match_the_dashboard(self) -> None:
        dashboard = (ROOT / "scripts" / "ncs_dashboard.py").read_text(encoding="utf-8")
        block = dashboard.split("SYNTHETIC_REVIEW_PACKET_PREFIXES = (", 1)[1].split(")", 1)[0]
        self.assertEqual(tuple(item.strip().strip('"') for item in block.split(",") if item.strip()),
                         snapshot.SYNTHETIC_PACKET_PREFIXES)

    def test_readonly_write_errors_are_named_and_plan_gaps_use_harness_validators(self) -> None:
        self.assertEqual(snapshot.exception_code(sqlite3.OperationalError("attempt to write a readonly database")),
                         "exception:readonly_write")
        self.assertEqual(snapshot.exception_code(ValueError("x")), "exception:ValueError")
        harness = snapshot.load_harness()
        gaps = snapshot.plan_contract_gaps(harness, {"ok": True, "training_system_matrix": []})
        self.assertIn("training_system_matrix.nonempty_list_of_rows", gaps["matrix"])
        self.assertTrue(gaps["plan"] and gaps["guide_trace"] and gaps["query_route"])


if __name__ == "__main__":
    unittest.main()
