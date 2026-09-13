from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "task_ksa_relevance", ROOT / "scripts/profile_task_ksa_rerank_relevance.py")
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


class RelevanceGateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE classifications(classification_id INTEGER PRIMARY KEY,
              major_code TEXT,middle_code TEXT,small_code TEXT,sub_code TEXT,
              major_name TEXT,middle_name TEXT,small_name TEXT,sub_name TEXT);
            CREATE TABLE competency_units(unit_code TEXT PRIMARY KEY,unit_name_raw TEXT,classification_id INTEGER);
            CREATE TABLE competency_elements(element_id INTEGER PRIMARY KEY,unit_code TEXT,element_name_raw TEXT);
            CREATE TABLE performance_criteria(criteria_id INTEGER PRIMARY KEY,element_id INTEGER,criteria_text_raw TEXT);
            CREATE TABLE ksa_items(ksa_id INTEGER PRIMARY KEY,element_id INTEGER,ksa_text_raw TEXT);
            INSERT INTO classifications VALUES(1,'02','02','02','01','a','a','a','a'),(2,'20','01','01','01','b','b','b','b');
            INSERT INTO competency_units VALUES('U1','채용 면접',1),('U2','임금 계산',1),('U3','서버 보안',2),('U4','통신 설계',2);
            INSERT INTO competency_elements VALUES(1,'U1','채용 면접'),(2,'U1','면접 기준'),(3,'U2','임금 계산'),(4,'U3','서버 보안'),(5,'U4','통신 설계');
            INSERT INTO performance_criteria VALUES(1,1,'채용 면접 기준'),(2,2,'분리된 과업'),(3,3,'임금 계산 기준');
            INSERT INTO ksa_items VALUES(1,1,'채용 면접 지식'),(2,2,'분리된 지식'),(3,3,'임금 계산 지식');
        """)
        self.probe = {"id": "test-probe", "query": "채용 면접", "anchor_unit_code": "U1",
                      "anchor_element_id": 1, "major_code": "02"}

    def tearDown(self):
        self.conn.close()

    def public(self):
        return {"query": "채용 면접", "classification_filter": {"major_code": "02"},
                "results": [
                    {"type": "element", "id": 3, "match_mode": "token_or", "path": {"unit_code": "U2"}},
                    {"type": "element", "id": 1, "match_mode": "token_or", "path": {"unit_code": "U1"}},
                    {"type": "unit", "id": "U1", "match_mode": "token_or"}]}

    def test_labels_ignore_forged_return_path_and_score(self):
        item = {"type": "criteria", "id": 3, "path": {"unit_code": "U1", "element_id": 1}, "shadow_score": 100}
        self.assertEqual(0, profile.hierarchy_label(self.conn, item, self.probe)["grade"])
        item["id"] = 1
        item["shadow_score"] = 0
        self.assertEqual(3, profile.hierarchy_label(self.conn, item, self.probe)["grade"])
        self.assertEqual(1, profile.hierarchy_label(self.conn, {"type": "element", "id": 2}, self.probe)["grade"])

    def test_selection_is_deterministic_all_major_distinct_unit_and_relation_free(self):
        first, majors = profile.select_probes(self.conn)
        second, _ = profile.select_probes(self.conn)
        self.assertEqual(first, second)
        self.assertEqual(["02", "20"], majors)
        self.assertEqual(4, len(first))
        self.assertEqual(4, len({p["anchor_unit_code"] for p in first}))
        # No relation table is present in this fixture. Changing score text
        # cannot change either probe selection or source-FK labels.
        self.conn.execute("UPDATE ksa_items SET ksa_text_raw='changed'")
        self.assertEqual(first, profile.select_probes(self.conn)[0])

    def test_before_after_metrics_and_counterfactual_are_real(self):
        public = self.public()
        before = copy.deepcopy(public)
        case = profile.score_probe(self.conn, public, self.probe, 3)
        self.assertGreater(case["ndcg_delta"], 0)
        self.assertGreater(case["mrr_delta"], 0)
        self.assertEqual(2, case["supported_candidates"])
        self.assertEqual(2, case["counterfactual_rejected_candidates"])
        self.assertEqual(0, case["counterfactual_positive_candidates"])
        self.assertTrue(case["evidence_permutation_invariant"])
        self.assertEqual(before, public)
        self.assertGreater(case["title_echo_ablation"]["removed_rows"], 0)
        self.assertEqual(0, case["title_echo_ablation"]["remaining_supported_candidates"])

    def test_counterfactual_preserves_texts_counts_and_original(self):
        rows = [{"layer": "criteria", "element_id": 1, "evidence_text": "채용 면접"},
                {"layer": "ksa", "element_id": 1, "evidence_text": "채용 면접"}]
        original = copy.deepcopy(rows)
        split = profile.split_layer_counterfactual(rows)
        self.assertEqual(rows, original)
        self.assertEqual([r["evidence_text"] for r in rows], [r["evidence_text"] for r in split])
        self.assertNotEqual(split[0]["element_id"], split[1]["element_id"])

    def test_perfect_structural_result_cannot_promote_semantic_relevance(self):
        cases = [profile.score_probe(self.conn, self.public(), self.probe, 3) for _ in range(2)]
        gate = profile.promotion_gate(cases, ["02"])
        self.assertTrue(all(gate["checks"].values()))
        self.assertEqual("PASS", gate["structural_decision"])
        self.assertEqual("HOLD", gate["promotion_decision"])
        self.assertFalse(gate["independent_semantic_validation"])
        self.assertEqual(["independent_semantic_labels_absent"], gate["hold_reasons"])

    def test_empty_population_cannot_vacuously_pass(self):
        gate = profile.promotion_gate([], [])
        self.assertEqual("HOLD", gate["structural_decision"])
        self.assertFalse(gate["checks"]["counterfactual_rejection"])
        self.assertIsNone(gate["metrics"]["counterfactual_rejection_rate"])

    def test_structural_regression_holds_even_with_average_gain(self):
        good = profile.score_probe(self.conn, self.public(), self.probe, 3)
        bad = copy.deepcopy(good)
        bad["ndcg_delta"] = -0.01
        gate = profile.promotion_gate([good, bad], ["02"])
        self.assertGreater(gate["metrics"]["mean_ndcg_delta"], 0)
        self.assertFalse(gate["checks"]["zero_structural_per_query_regressions"])

    def test_missing_major_and_missing_identity_block(self):
        public = self.public()
        public["results"].append({"type": "element", "id": 9000, "path": {"unit_code": "U1"}})
        case = profile.score_probe(self.conn, public, self.probe, 3)
        gate = profile.promotion_gate([case, case], ["02", "20"])
        self.assertFalse(gate["checks"]["all_available_majors_sampled"])
        self.assertFalse(gate["checks"]["source_identities_resolved"])

    def test_metric_fixed_pool_and_empty_behavior(self):
        self.assertEqual(1, profile.metrics([3, 1, 0], 3)["ndcg"])
        self.assertEqual(0.5, profile.metrics([0, 3, 1], 3)["mrr"])
        self.assertEqual(0, profile.metrics([], 3)["ndcg"])
        self.assertFalse(profile.metrics([1, 0], 3)["anchor_hit"])


if __name__ == "__main__":
    unittest.main()
