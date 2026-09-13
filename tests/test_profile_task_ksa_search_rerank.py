from __future__ import annotations

import copy
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_ksa_shadow", ROOT / "scripts/profile_task_ksa_search_rerank.py")
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


class TaskKsaShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "fixture.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE classifications(classification_id INTEGER PRIMARY KEY,
                  major_code TEXT,middle_code TEXT,small_code TEXT,sub_code TEXT,
                  major_name TEXT,middle_name TEXT,small_name TEXT,sub_name TEXT);
                CREATE TABLE competency_units(unit_code TEXT PRIMARY KEY, unit_name_raw TEXT, classification_id INTEGER);
                CREATE TABLE competency_elements(element_id INTEGER PRIMARY KEY, unit_code TEXT, element_name_raw TEXT);
                CREATE INDEX ce_unit ON competency_elements(unit_code);
                CREATE TABLE performance_criteria(criteria_id INTEGER PRIMARY KEY,element_id INTEGER,criteria_text_raw TEXT);
                CREATE INDEX pc_element ON performance_criteria(element_id);
                CREATE TABLE ksa_items(ksa_id INTEGER PRIMARY KEY,element_id INTEGER,ksa_text_raw TEXT);
                CREATE INDEX ki_element ON ksa_items(element_id);
                INSERT INTO classifications VALUES(1,'02','02','02','01','경영','인사','인사','인사');
                INSERT INTO classifications VALUES(2,'20','01','01','01','정보통신','정보','정보','정보');
                INSERT INTO competency_units VALUES('U1','채용 면접',1),('U2','채용 면접',2);
                INSERT INTO competency_elements VALUES(1,'U1','면접 기준'),(2,'U1','채용 준비'),(3,'U2','다른 범위');
                INSERT INTO performance_criteria VALUES(1,1,'채용 면접 기준'),(2,2,'채용 기준'),(3,3,'채용 면접 기준');
                INSERT INTO ksa_items VALUES(1,1,'채용 면접 지식'),(2,2,'분리된 지식'),(3,3,'채용 면접 지식');
            """)

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, rows=None, query="채용 면접"):
        return {"query": query, "classification_filter": {"major_code": "02"},
                "results": rows if rows is not None else [
                    {"type": "unit", "id": "U1", "match_mode": "token_or"},
                    {"type": "element", "id": 1, "path": {"unit_code": "U1"}, "match_mode": "token_or"},
                    {"type": "criteria", "id": 1, "path": {"unit_code": "U1", "element_id": 1}, "match_mode": "token_or"},
                    {"type": "ksa", "id": 1, "path": {"unit_code": "U1", "element_id": 1}, "match_mode": "token_or"}]}

    def test_one_sql_four_layers_without_mutation(self):
        result = self.result()
        before = copy.deepcopy(result)
        identity = profile.file_identity(self.db)
        shadow = profile.shadow_profile(self.db, result)
        self.assertEqual(1, shadow["additional_sql_count"])
        self.assertFalse(shadow["sql_budget_exceeded"])
        self.assertTrue(all(c["all_four_layers"] for c in shadow["candidates"]))
        self.assertTrue(all(c["shadow_score"] > 0 for c in shadow["candidates"]))
        self.assertEqual(before, result)
        self.assertEqual(identity, profile.file_identity(self.db))

    def test_bounded_evidence_and_immutable_source(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO ksa_items VALUES(?,?,?)",
                             [(i, 1, "채용 " * 300) for i in range(10, 110)])
        shadow = profile.shadow_profile(self.db, self.result())
        for candidate in shadow["candidates"][1:]:
            self.assertLessEqual(candidate["sample_counts"]["ksa"], profile.MAX_ROWS_PER_ELEMENT)
        self.assertGreater(shadow["text_truncated_rows"], 0)
        with profile.readonly_connection(self.db) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE ksa_items SET ksa_text_raw='bad'")

    def test_cross_element_words_do_not_combine(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE performance_criteria SET criteria_text_raw='채용' WHERE element_id=1")
            conn.execute("UPDATE ksa_items SET ksa_text_raw='채용' WHERE element_id=1")
            conn.execute("UPDATE performance_criteria SET criteria_text_raw='면접' WHERE element_id=2")
            conn.execute("UPDATE ksa_items SET ksa_text_raw='면접' WHERE element_id=2")
        shadow = profile.shadow_profile(self.db, self.result())
        self.assertTrue(all(c["shadow_score"] == 0 for c in shadow["candidates"]))

    def test_filter_mismatch_detected_without_retrieval_expansion(self):
        result = self.result([{"type": "unit", "id": "U2", "match_mode": "token_or"}])
        shadow = profile.shadow_profile(self.db, result)
        self.assertFalse(shadow["candidates"][0]["classification_filter_preserved"])
        self.assertEqual(["U2"], [c["id"] for c in shadow["candidates"]])

    def test_empty_no_sql_and_limit_rejected(self):
        shadow = profile.shadow_profile(self.db, self.result([]))
        self.assertEqual(0, shadow["additional_sql_count"])
        with self.assertRaises(ValueError):
            profile.evidence_sql(self.result()["results"] * 10)

    def test_timeout_returns_no_partial_score(self):
        # A negative deadline plus a sufficiently large candidate query reliably
        # exercises SQLite's progress interrupt, independent of wall clock speed.
        result = self.result(self.result()["results"] * 6)
        shadow = profile.shadow_profile(self.db, result, budget_ms=-1)
        self.assertTrue(shadow["sql_budget_exceeded"])
        self.assertEqual(0, shadow["evidence_rows"])
        self.assertTrue(all(c["shadow_score"] == 0 for c in shadow["candidates"]))

    def test_hypothetical_order_preserves_type_and_match_tier(self):
        result = self.result([
            {"type": "element", "id": 2, "path": {"unit_code": "U1"}, "match_mode": "token_or"},
            {"type": "unit", "id": "U1", "match_mode": "phrase"},
            {"type": "element", "id": 1, "path": {"unit_code": "U1"}, "match_mode": "token_or"},
            {"type": "element", "id": 2, "path": {"unit_code": "U1"}, "match_mode": "phrase"}])
        shadow = profile.shadow_profile(self.db, result)
        self.assertEqual([3, 2, 1, 4], [c["shadow_rank"] for c in shadow["candidates"]])

    def test_no_synthetic_quality_promotion(self):
        shadow = profile.shadow_profile(self.db, self.result())
        gate = profile.promotion_gate([{"shadow": shadow, "repeat_public_parity": True}])
        self.assertEqual("HOLD", gate["decision"])
        self.assertFalse(gate["checks"]["independent_relevance_validation"])

    def test_empty_controls_cannot_dilute_cost_gate(self):
        shadow = profile.shadow_profile(self.db, self.result())
        shadow["elapsed_ms"] = 20
        empty = profile.shadow_profile(self.db, self.result([]))
        runs = [{"shadow": empty, "repeat_public_parity": True}] * 20
        gate = profile.promotion_gate(runs + [{"shadow": shadow, "repeat_public_parity": True}])
        self.assertFalse(gate["checks"]["added_p50_at_most_10ms"])

    def test_unicode_normalization_shared_evidence(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE performance_criteria SET criteria_text_raw='cv 면접' WHERE element_id=1")
            conn.execute("UPDATE ksa_items SET ksa_text_raw='ＣＶ 면접' WHERE element_id=1")
        shadow = profile.shadow_profile(self.db, self.result(query="ＣＶ 면접"))
        self.assertTrue(all(c["shadow_score"] > 0 for c in shadow["candidates"]))


if __name__ == "__main__":
    unittest.main()
