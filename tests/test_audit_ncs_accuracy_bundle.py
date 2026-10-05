"""Protect evidence boundaries rather than assert human relevance from fixtures."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("accuracy_bundle", ROOT / "scripts/audit_ncs_accuracy_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def source_report():
    metrics = {"case_count": 1, "search_error_count": 0, "hit_at_1": 0.5, "hit_at_3": 1.0, "mrr": 0.75}
    return {"evidence_complete": True,
            "available_major_codes": ["01"],
            "exact": {kind: {"overall": {"cases": 1}, "by_major": {"01": {}}, "cases": [{"rank": 1}]}
                      for kind in ("name", "code")},
            "reviewed_transition_sample": {"scenario_ids": [3], "diagnostic_evaluation": {"cases": [{"scenario_id": 3, "ok": True}]}},
            "database_before": {"path": "same.db", "sha256": "a", "bytes": 10},
            "output_contract": {"ok": True, "missing_plan_fields": []},
            "development": {name: {"fixture": {"sha256": name, "path": name},
                                   "evaluation": {"current": {"overall": metrics.copy(), "cases": [
                                       {"query": "query", "first_expected_rank": 2}]}}}
                            for name in bundle.FIXTURES}}


class AccuracyBundleTests(unittest.TestCase):
    def test_unchanged_duplicate_queries_do_not_invent_rank_changes(self):
        cases = [{"case_id": f"NL-{i}", "query": "same", "expected_unit_codes": [str(i)],
                  "category": "sample", "first_expected_rank": rank}
                 for i, rank in enumerate((1, 3), 1)]
        self.assertEqual(bundle.case_changes(cases, cases), [])

    def test_duplicate_query_changes_keep_each_fixture_position(self):
        before = [{"query": "same", "first_expected_rank": rank} for rank in (1, 3)]
        after = [{"query": "same", "first_expected_rank": rank} for rank in (2, 1)]
        changes = bundle.case_changes(before, after)
        self.assertEqual([(r["fixture_position"], r["before_rank"], r["after_rank"])
                          for r in changes], [(1, 1, 2), (2, 3, 1)])

    def test_case_identity_content_or_order_mismatch_is_explicit(self):
        cases = [{"case_id": f"NL-{i}", "query": "same", "expected_unit_codes": [str(i)],
                  "category": "sample", "first_expected_rank": 1} for i in (1, 2)]
        for field, value in (("query", "different"), ("case_id", "other"),
                             ("expected_unit_codes", ["99"]), ("category", "other")):
            after = json.loads(json.dumps(cases))
            after[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "identity_or_order_mismatch_at_position:1"):
                bundle.case_changes(cases, after)
        with self.assertRaisesRegex(ValueError, "identity_or_order_mismatch_at_position:1"):
            bundle.case_changes(cases, list(reversed(cases)))

    def test_case_count_and_malformed_identity_fail_explicitly(self):
        for before, after in (([], [{}]), ({}, []), ([None], [None]), ([{}], [{}])):
            with self.subTest(before=before), self.assertRaisesRegex(ValueError, "case_pairing:"):
                bundle.case_changes(before, after)

    def test_same_fixture_mismatched_cases_fail_closed_without_delta(self):
        before, after = source_report(), source_report()
        after["development"]["dev90"]["evaluation"]["current"]["cases"][0]["query"] = "changed"
        result = bundle.compare_reports(before, after, [])
        self.assertFalse(result["evidence_complete"])
        self.assertEqual(result["quality_assessment"]["status"], "not_evaluable")
        self.assertIn("dev90:case_pairing:identity_or_order_mismatch_at_position:1", result["failures"])
        comparison = result["development_comparison"]["dev90"]
        self.assertFalse(comparison["case_pairing_ok"])
        self.assertEqual(comparison["case_changes"], [])
        self.assertTrue(all(value is None for value in comparison["delta"].values()))

    def test_resource_edits_additions_and_removals_change_source_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            package = src / "ncs_mcp"
            package.mkdir()
            (package / "server.py").write_text("pass", encoding="utf-8")
            for suffix in (".json", ".yaml", ".toml", ".sql", ".txt", ".j2"):
                with self.subTest(suffix=suffix):
                    initial = bundle.tree_identity(src)
                    resource = package / ("resource" + suffix)
                    resource.write_text("original", encoding="utf-8")
                    added = bundle.tree_identity(src)
                    self.assertNotEqual(initial["sha256"], added["sha256"])
                    resource.write_text("modified", encoding="utf-8")
                    self.assertNotEqual(added["sha256"], bundle.tree_identity(src)["sha256"])
                    resource.unlink()
                    self.assertEqual(initial, bundle.tree_identity(src))

    def test_source_identity_is_deterministic_and_excludes_cache_private_and_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            package = src / "ncs_mcp"
            package.mkdir(parents=True)
            (package / "server.py").write_text("pass", encoding="utf-8")
            (package / "vocabulary.json").write_text("{}", encoding="utf-8")
            before = bundle.tree_identity(src)
            for relative in ("ncs_mcp/__pycache__/old.py", "ncs_mcp/cache/terms.json",
                             "ncs_mcp/.private/key.json", "ncs_mcp/store.db", "outside.json",
                             "ncs_mcp/temporary.pyc", "ncs_mcp/.env"):
                path = src / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("excluded", encoding="utf-8")
            (Path(tmp) / "workspace_private.json").write_text("private", encoding="utf-8")
            self.assertEqual(before, bundle.tree_identity(src))
            self.assertEqual(list(before["files"]), sorted(before["files"]))
            self.assertEqual(set(before["files"]), {"ncs_mcp/server.py", "ncs_mcp/vocabulary.json"})

    def test_resource_symlink_cannot_expand_source_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            package = src / "ncs_mcp"
            package.mkdir(parents=True)
            (package / "server.py").write_text("pass", encoding="utf-8")
            outside = Path(tmp) / "private.json"
            outside.write_text("private", encoding="utf-8")
            try:
                (package / "resource.json").symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "must stay inside source-root"):
                bundle.tree_identity(src)

    def test_resource_change_during_worker_stage_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = base / "src"
            package = src / "ncs_mcp"
            package.mkdir(parents=True)
            (package / "server.py").write_text("pass", encoding="utf-8")
            resource = package / "terms.json"
            resource.write_text("before", encoding="utf-8")
            db = base / "synthetic.db"
            with sqlite3.connect(db) as conn:
                conn.executescript("CREATE TABLE classifications(classification_id,major_code);"
                                   "CREATE TABLE competency_units(classification_id,unit_code,unit_name_raw);"
                                   "INSERT INTO classifications VALUES(1,'01');"
                                   "INSERT INTO competency_units VALUES(1,'01','unit');")
            conn.close()
            for relative in bundle.FIXTURES.values():
                path = base / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[]", encoding="utf-8")
            exact = types.SimpleNamespace(build_cases=lambda *a, **k: [{}],
                                          evaluate=lambda *a, **k: [{"rank": 1}],
                                          aggregate=lambda *a: {"overall": {"cases": 1}, "by_major": {"01": {}}})
            current = source_report()["development"]["dev90"]["evaluation"]["current"]
            def evaluate(**kwargs):
                resource.write_text("after", encoding="utf-8")
                return {"current": current}
            nl = types.SimpleNamespace(build_nl_evaluation_report=evaluate)
            harness = types.SimpleNamespace(**{name: lambda _: [] for name in
                ("_missing_aihr_matrix_fields", "_missing_aihr_plan_fields",
                 "_missing_aihr_guide_trace_fields", "_missing_aihr_query_route_fields")})
            fake_package = types.ModuleType("ncs_mcp")
            fake_package.server = types.SimpleNamespace(search_ncs=lambda *a: {},
                plan_ncs_education_path=lambda **k: {"ok": True, "training_system_matrix": [{}]})
            fake_package.training_recommendation = types.SimpleNamespace()
            fake_package.quality_gates = types.SimpleNamespace()
            args = types.SimpleNamespace(db=db, source_root=src, fixture_root=base, execution_nonce="test",
                                         per_major_limit=1, transition_limit=0, skip_plan=False)
            scripts = {"audit_ncs_exact_lookup": exact, "audit_ncs_search_precision": nl, "ncs_harness": harness}
            with patch.dict(sys.modules, {"ncs_mcp": fake_package}), patch.dict(os.environ), \
                 patch.object(sys, "path", sys.path.copy()), \
                 patch.object(bundle, "load_script", side_effect=lambda name: scripts[name]), \
                 patch.object(bundle, "runtime_isolation", return_value={"ok": True}), \
                 patch.object(bundle, "reviewed_transition_sample", return_value={"scenario_ids": [], "status_counts": {}}):
                report = bundle.worker(args)
            self.assertEqual(report["errors"], [])
            self.assertTrue(report["database_unchanged"])
            self.assertTrue(report["auditors_unchanged"])
            self.assertTrue(report["output_contract"]["ok"])
            self.assertFalse(report["source_unchanged"])
            self.assertEqual(report["stage_failures"], ["source:changed_during_stage"])
            self.assertFalse(report["evidence_complete"])
            # A producer's complete flag cannot hide its explicit instability.
            report["evidence_complete"] = True
            self.assertFalse(bundle.compare_reports(report, report, [])["evidence_complete"])

    def test_intentional_baseline_candidate_source_differences_are_allowed(self):
        before, after = source_report(), source_report()
        before["source_before"] = {"sha256": "baseline"}
        after["source_before"] = {"sha256": "candidate"}
        before["source_unchanged"] = after["source_unchanged"] = True
        result = bundle.compare_reports(before, after, [])
        self.assertTrue(result["evidence_complete"])
        self.assertNotEqual(result["source_identity"]["baseline"], result["source_identity"]["candidate"])

    def test_wal_change_is_part_of_database_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "source.db"
            db.write_bytes(b"stable main file")
            before = bundle.database_identity(db)
            Path(str(db) + "-wal").write_bytes(b"changed logical database")
            after = bundle.database_identity(db)
            self.assertEqual(before["sha256"], after["sha256"])
            self.assertNotEqual(before, after)

    def test_exact_miss_is_quality_warning_not_missing_evidence(self):
        before, after = source_report(), source_report()
        before["exact"]["name"]["overall"]["hit_at_1"] = 1.0
        after["exact"]["name"]["overall"]["hit_at_1"] = 0.9
        result = bundle.compare_reports(before, after, [])
        self.assertTrue(result["evidence_complete"])
        self.assertEqual(result["quality_assessment"]["status"], "regression")
        self.assertEqual(result["quality_assessment"]["regressions"][0]["dataset"], "exact_name")

    def test_empty_missing_and_nonlist_matrix_always_fail(self):
        harness = types.SimpleNamespace(**{name: lambda _: [] for name in
                    ("_missing_aihr_matrix_fields", "_missing_aihr_plan_fields", "_missing_aihr_guide_trace_fields", "_missing_aihr_query_route_fields")})
        for matrix in (None, [], {}, "rows", [None]):
            with self.subTest(matrix=matrix):
                self.assertFalse(bundle.output_contract(harness, {"ok": True, "training_system_matrix": matrix})["ok"])
        self.assertTrue(bundle.output_contract(harness, {"ok": True, "training_system_matrix": [{"course": 1}]})["ok"])

    def test_missing_or_empty_exact_is_not_complete_even_if_producer_claims_it(self):
        for section in (None, {}, {"overall": {"cases": 0}, "cases": [], "by_major": {"01": {}}}):
            with self.subTest(section=section):
                before, after = source_report(), source_report()
                if section is None:
                    after.pop("exact")
                else:
                    after["exact"]["name"] = section
                self.assertFalse(bundle.compare_reports(before, after, [])["evidence_complete"])

    def test_nl_search_errors_cannot_be_hidden_by_complete_flag(self):
        before, after = source_report(), source_report()
        after["development"]["dev90"]["evaluation"]["current"]["overall"]["search_error_count"] = 1
        result = bundle.compare_reports(before, after, [])
        self.assertFalse(result["evidence_complete"])
        self.assertIn("candidate:dev90:search_execution_error", result["failures"])

    def test_low_quality_is_visible_separately_from_complete_evidence(self):
        before, after = source_report(), source_report()
        for report in (before, after):
            report["development"]["dev_long"]["evaluation"]["current"]["overall"]["hit_at_3"] = 0.52
        result = bundle.compare_reports(before, after, [])
        self.assertTrue(result["evidence_complete"])
        self.assertEqual(result["quality_assessment"]["status"], "review_required")
        self.assertEqual(result["quality_assessment"]["warnings"][0]["observed"], 0.52)
        self.assertFalse(result["quality_assessment"]["evidence_completeness_is_accuracy_pass"])

    def test_same_db_changed_fixture_never_produces_delta(self):
        before, after = source_report(), source_report()
        after["development"]["dev90"]["fixture"]["sha256"] = "changed"
        result = bundle.compare_reports(before, after, [])
        self.assertFalse(result["evidence_complete"])
        self.assertIsNone(result["development_comparison"]["dev90"]["delta"]["mrr"])

    def test_same_fixture_improvement_and_regression_are_visible(self):
        before, after = source_report(), source_report()
        after["development"]["dev90"]["evaluation"]["current"]["overall"]["hit_at_1"] = 1.0
        after["development"]["dev_long"]["evaluation"]["current"]["overall"]["mrr"] = 0.5
        after["development"]["dev90"]["evaluation"]["current"]["cases"][0]["first_expected_rank"] = 1
        result = bundle.compare_reports(before, after, [])
        self.assertEqual(result["development_comparison"]["dev90"]["delta"]["hit_at_1"], 0.5)
        self.assertEqual(result["development_comparison"]["dev_long"]["delta"]["mrr"], -0.25)
        self.assertEqual(result["development_comparison"]["dev90"]["case_changes"][0]["after_rank"], 1)
        self.assertFalse(result["release_ready"])
        self.assertFalse(result["human_approval_claim"])

    def test_db_identity_mismatch_is_failure(self):
        before, after = source_report(), source_report()
        after["database_before"]["sha256"] = "different"
        result = bundle.compare_reports(before, after, [])
        self.assertFalse(result["same_database"])
        self.assertFalse(result["evidence_complete"])
        self.assertIsNone(result["development_comparison"]["dev90"]["delta"]["mrr"])

    def test_missing_empty_proofs_and_contract_are_not_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty, missing = Path(tmp) / "empty.json", Path(tmp) / "missing.json"
            empty.touch()
            proofs = bundle.artifact_proofs([empty, missing])
            report = source_report()
            report["output_contract"] = {"ok": False, "status": "not_evaluated"}
            result = bundle.compare_reports(report, source_report(), proofs)
            self.assertEqual(len(result["failures"]), 3)
            self.assertFalse(result["evidence_complete"])

    def test_output_directory_cannot_contain_db_or_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "input.db"
            db.touch()
            src = base / "src"
            src.mkdir()
            with self.assertRaises(ValueError):
                bundle.validate_outputs(db, [src], base)
            with self.assertRaises(ValueError):
                bundle.validate_outputs(db, [src], src / "reports")
            self.assertEqual(bundle.validate_outputs(db, [src], base / "reports"), base / "reports")

    def test_hardlink_output_does_not_overwrite_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db, src, out = base / "input.db", base / "src", base / "out"
            db.write_bytes(b"original")
            src.mkdir()
            out.mkdir()
            try:
                os.link(db, out / "baseline.json")
            except OSError:
                self.skipTest("hardlinks unavailable")
            with self.assertRaises(ValueError):
                bundle.validate_outputs(db, [src], out)
            self.assertEqual(db.read_bytes(), b"original")

    def test_import_leak_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = types.ModuleType("ncs_mcp.bundle_fake")
            fake.__file__ = str(Path(tmp) / "outside.py")
            with patch.dict(sys.modules, {fake.__name__: fake}):
                isolation = bundle.runtime_isolation(Path(tmp) / "source")
            self.assertFalse(isolation["ok"])
            self.assertIn(fake.__name__, isolation["leaked_modules"])

    def test_tree_digest_detects_nested_runtime_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            package = src / "ncs_mcp"
            package.mkdir()
            (package / "server.py").write_text("pass", encoding="utf-8")
            before = bundle.tree_identity(src)
            (package / "nested.py").write_text("changed", encoding="utf-8")
            self.assertNotEqual(before["sha256"], bundle.tree_identity(src)["sha256"])

    def test_no_review_labels_does_not_evaluate_untrusted_scenarios(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE training_transition_gold_scenarios(scenario_id,review_status)")
        conn.execute("INSERT INTO training_transition_gold_scenarios VALUES(1,'candidate')")
        conn.execute("CREATE TABLE training_transition_scenario_reviews(review_id)")
        training = types.SimpleNamespace(TRUSTED_TRANSITION_REVIEW_STATUSES=("reviewed",),
                                         evaluate_training_transition_scenarios=lambda *a, **kw: self.fail("untrusted eval"))
        quality = types.SimpleNamespace(_transition_packet_backed_trusted_scenario_provenance=lambda *a: {})
        sample = bundle.reviewed_transition_sample(conn, training, quality, 3)
        self.assertIsNone(sample["diagnostic_evaluation"])
        self.assertFalse(sample["precision_claim_allowed"])

    def test_labeled_sample_keeps_incomplete_label_and_provenance_limits(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE training_transition_gold_scenarios(scenario_id,review_status)")
        conn.execute("INSERT INTO training_transition_gold_scenarios VALUES(7,'reviewed')")
        conn.execute("CREATE TABLE training_transition_scenario_reviews(review_id)")
        training = types.SimpleNamespace(TRUSTED_TRANSITION_REVIEW_STATUSES=("reviewed",),
                                         evaluate_training_transition_scenarios=lambda *a, **kw: {"scenario_ids": kw["scenario_ids"], "precision_at_k": 1})
        quality = types.SimpleNamespace(_transition_packet_backed_trusted_scenario_provenance=lambda *a: {"packet_backed_scenario_count": 0})
        sample = bundle.reviewed_transition_sample(conn, training, quality, 1)
        self.assertEqual(sample["scenario_ids"], [7])
        self.assertFalse(sample["human_relevance_claim"])
        self.assertFalse(sample["expected_course_labels_complete"])
        self.assertFalse(sample["precision_claim_allowed"])

    def test_failed_retry_cannot_reuse_previous_worker_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, report = Path(tmp), source_report()
            db, src, out = base / "db", base / "src", base / "out"
            db.touch()
            src.mkdir()
            out.mkdir()
            for label in ("baseline", "candidate"):
                bundle.write_json(out / f"{label}.json", report)
            completed = subprocess.CompletedProcess([], 1, "", "bounded failure")
            with patch.object(bundle.subprocess, "run", return_value=completed) as run:
                code = bundle.main(["--db", str(db), "--baseline-source", str(src), "--candidate-source", str(src), "--out-dir", str(out)])
            self.assertEqual(code, 1)
            result = json.loads((out / "bundle.json").read_text(encoding="utf-8"))
            self.assertFalse(result["evidence_complete"])
            if os.name == "nt":
                self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertNotIn("bounded failure", (out / "bundle.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
