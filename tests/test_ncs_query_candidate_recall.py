from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from ncs_mcp.search import core


class NcsTaskQueryEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT, major_name TEXT, middle_code TEXT, middle_name TEXT,
                small_code TEXT, small_name TEXT, sub_code TEXT, sub_name TEXT,
                duty_order TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY, unit_name_raw TEXT, api_definition TEXT,
                unit_level_raw TEXT, classification_id INTEGER
            );
            CREATE TABLE ncs_query_aliases (
                unit_code TEXT, alias_text TEXT, normalized_query TEXT
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY, element_name_raw TEXT, unit_code TEXT
            );
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY, criteria_text_raw TEXT,
                criteria_text_refined TEXT, element_id INTEGER
            );
            CREATE TABLE ksa_items (
                ksa_id INTEGER PRIMARY KEY, ksa_type_name TEXT, ksa_text_raw TEXT,
                ksa_text_refined TEXT, element_id INTEGER
            );
            INSERT INTO classifications VALUES
                (1, '01', 'first', '01', 'section', '01', 'group', '01', 'scope', ''),
                (2, '02', 'second', '01', 'section', '01', 'group', '01', 'scope', '');
            INSERT INTO competency_units VALUES
                ('TARGET', 'task coordination', 'alpha readiness', '4', 1),
                ('NAME_ONLY', 'alpha showcase', 'alpha examples', '4', 2),
                ('BETA_ONLY', 'beta roster', 'beta examples', '4', 2),
                ('GAMMA_ONLY', 'gamma agenda', 'gamma examples', '4', 2);
            INSERT INTO competency_elements VALUES (1, 'task evidence', 'TARGET');
            INSERT INTO performance_criteria VALUES (1, 'alpha beta gamma', NULL, 1);
        """)
        self.conn.commit()
        self.statements: list[str] = []
        self.conn.set_trace_callback(self.statements.append)
        self.conn.execute("PRAGMA query_only=ON")
        self.runtime = patch.multiple(
            core,
            _OPEN_DB_FACTORY=self._open_db,
            _CLAMP_LIMIT=lambda value: max(1, min(int(value), 500)),
            _UNIT_PATH=lambda row: {"unit_code": row["unit_code"]},
            _TIER_PREDICATES=None,
            _TIER_EXECUTOR=None,
            _TOKEN_EXPANDER=None,
            _SEMANTIC_PROVIDER=None,
        )
        self.runtime.start()

    def tearDown(self) -> None:
        self.runtime.stop()
        self.conn.close()

    @contextmanager
    def _open_db(self):
        yield self.conn

    def test_complete_three_term_task_beats_one_name_token(self) -> None:
        result = core.search_ncs("alpha beta gamma", scope="unit", limit=3)
        self.assertEqual(result["match_mode"], "token_or")
        self.assertEqual(result["results"][2]["id"], "TARGET")
        self.assertEqual([row["id"] for row in result["results"][:2]], ["BETA_ONLY", "GAMMA_ONLY"])
        evidence_queries = [sql for sql in self.statements if "AS evidence_text" in sql]
        self.assertEqual(len(evidence_queries), 1)
        self.assertIn("WHERE ce.unit_code IN (", evidence_queries[0])
        self.assertLessEqual(evidence_queries[0].count("'TARGET'"), 4)

    def test_partial_task_coverage_keeps_existing_supporting_weight(self) -> None:
        scores = core._ncs_search_unit_task_ksa_scores(
            self.conn, ["TARGET"], ["alpha", "beta", "missing"]
        )
        self.assertEqual(scores["TARGET"], 2 * core._NCS_SEARCH_TASK_KSA_WEIGHT)

    def test_two_term_pair_keeps_existing_supporting_weight(self) -> None:
        scores = core._ncs_search_unit_task_ksa_scores(
            self.conn, ["TARGET"], ["alpha", "beta"]
        )
        self.assertEqual(scores["TARGET"], 2 * core._NCS_SEARCH_TASK_KSA_WEIGHT)

    def test_duplicate_query_terms_cannot_create_joint_evidence(self) -> None:
        scores = core._ncs_search_unit_task_ksa_scores(
            self.conn, ["TARGET"], ["alpha", "alpha", "alpha"]
        )
        self.assertEqual(scores["TARGET"], 0.0)

    def test_prefix_variants_of_one_source_word_cannot_create_joint_evidence(self) -> None:
        self.conn.execute("PRAGMA query_only=OFF")
        self.conn.execute("UPDATE performance_criteria SET criteria_text_raw='alphabet'")
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        joint: dict[str, float] = {}
        scores = core._ncs_search_unit_task_ksa_scores(
            self.conn, ["TARGET"], ["ＡＬＰＨＡ", "alphabet"], joint_scores=joint,
        )
        self.assertEqual(scores["TARGET"], 2 * core._NCS_SEARCH_TASK_KSA_WEIGHT)
        self.assertEqual(joint["TARGET"], 0.0)

    def test_unresolved_extra_word_does_not_block_resolved_task_coverage(self) -> None:
        joint: dict[str, float] = {}
        scores = core._ncs_search_unit_task_ksa_scores(
            self.conn,
            ["TARGET"],
            ["alpha", "beta", "gamma", "unknown"],
            joint_tokens=["alpha", "beta", "gamma"],
            joint_scores=joint,
        )
        self.assertEqual(scores["TARGET"], 3 * core._NCS_SEARCH_TASK_KSA_WEIGHT)
        self.assertGreater(joint["TARGET"], 0)

    def test_shared_classification_term_preserves_source_major(self) -> None:
        def item(code: str, major: str, classification: str, score: float):
            return {
                "id": code,
                "_classification_codes": {"major_code": major},
                "_search_fields": {"unit_name": "", "classification": classification},
            }, score

        pairs = [
            item("HEAD_ONE", "01", "alpha", 10.0),
            item("HEAD_TWO", "01", "alpha", 9.0),
            item("THIRD", "01", "alpha", 8.0),
            item("OTHER_MAJOR", "02", "beta", 7.0),
        ]
        candidates = [row for row, _ in pairs]
        scores = {row["id"]: score for row, score in pairs}
        ranked = core._rerank_ncs_unit_task_ksa_candidates(
            candidates, scores, ["alpha", "beta", "gamma"], {}, {},
            joint_scores={"OTHER_MAJOR": 100.0},
            joint_matches={"OTHER_MAJOR": {"beta", "gamma"}},
        )
        self.assertEqual([row["id"] for row in ranked], [row["id"] for row in candidates])

    def test_repeated_task_words_do_not_displace_direct_definition_evidence(self) -> None:
        def item(code: str, name: str, definition: str = ""):
            return {"id": code, "_search_fields": {"unit_name": name, "definition": definition}}

        candidates = [
            item("HEAD_ONE", "leading option"),
            item("HEAD_TWO", "second option"),
            item("THIRD", "alpha operation", "beta acquisition"),
            item("TAIL", "alpha analysis"),
        ]
        ranked = core._rerank_ncs_unit_task_ksa_candidates(
            candidates,
            {"HEAD_ONE": 20.0, "HEAD_TWO": 19.0, "THIRD": 0.0, "TAIL": 0.5},
            ["alpha", "beta", "gamma"], {}, {},
            joint_scores={"TAIL": 2.0},
            joint_matches={"TAIL": {"alpha", "beta"}},
        )
        self.assertEqual([row["id"] for row in ranked], [row["id"] for row in candidates])

    def test_synonym_covered_definition_is_not_new_task_evidence(self) -> None:
        candidates = [
            {"id": "HEAD_ONE", "_search_fields": {}},
            {"id": "HEAD_TWO", "_search_fields": {}},
            {"id": "THIRD", "_search_fields": {
                "unit_name": "alpha operation", "definition": "delta acquisition",
            }},
            {"id": "TAIL", "_search_fields": {"unit_name": "alpha analysis"}},
        ]
        ranked = core._rerank_ncs_unit_task_ksa_candidates(
            candidates,
            {"HEAD_ONE": 20.0, "HEAD_TWO": 19.0, "THIRD": 0.0, "TAIL": 1.0},
            ["alpha", "beta", "gamma"], {"beta": ["delta"]}, {},
            joint_scores={"TAIL": 2.0},
            joint_matches={"TAIL": {"alpha", "beta"}},
        )
        self.assertEqual([row["id"] for row in ranked], [row["id"] for row in candidates])

    def test_name_compound_covered_terms_are_not_new_task_evidence(self) -> None:
        candidates = [
            {"id": "HEAD_ONE", "_search_fields": {}},
            {"id": "HEAD_TWO", "_search_fields": {}},
            {"id": "THIRD", "_search_fields": {"unit_name": "급여명세서 발급"}},
            {"id": "TAIL", "_search_fields": {"unit_name": "급여 운영"}},
        ]
        ranked = core._rerank_ncs_unit_task_ksa_candidates(
            candidates,
            {"HEAD_ONE": 20.0, "HEAD_TWO": 19.0, "THIRD": 0.0, "TAIL": 1.0},
            ["급여", "명세서", "입력"], {}, {},
            compound_subphrase_expansions={"급여": ["급여명세서"], "명세서": ["급여명세서"]},
            joint_scores={"TAIL": 2.0},
            joint_matches={"TAIL": {"급여", "명세서"}},
        )
        self.assertEqual([row["id"] for row in ranked], [row["id"] for row in candidates])

    def test_source_names_and_codes_preserve_exact_priority(self) -> None:
        for query in ("alpha showcase", "NAME_ONLY"):
            with self.subTest(query=query):
                self.statements.clear()
                result = core.search_ncs(query, scope="unit", limit=1)
                self.assertEqual(result["results"][0]["id"], "NAME_ONLY")
                self.assertEqual(result["match_mode"], "phrase")
                self.assertFalse(any("AS evidence_text" in sql for sql in self.statements))

    def test_task_rerank_respects_hard_classification_scope(self) -> None:
        result = core.search_ncs(
            "alpha beta gamma", scope="unit", limit=3,
            classification_filter={"major_code": "02"},
        )
        self.assertNotIn("TARGET", [row["id"] for row in result["results"]])

    def test_task_rerank_is_stable_before_pagination(self) -> None:
        full = core.search_ncs("alpha beta gamma", scope="unit", limit=50)
        ids = [row["id"] for row in full["results"]]
        for offset in range(len(ids)):
            page = core.search_ncs("alpha beta gamma", scope="unit", limit=1, offset=offset)
            self.assertEqual([row["id"] for row in page["results"]], ids[offset:offset + 1])

    def test_unique_source_name_typo_is_visible_without_rewriting_input(self) -> None:
        query = "task coordiantion"
        result = core.search_ncs(query, scope="unit", limit=3)
        self.assertEqual(result["query"], query)
        self.assertEqual(result["normalized_query"], query)
        self.assertEqual(result["results"][0]["id"], "TARGET")
        self.assertEqual(result["unit_query_terms"]["resolved_from"]["coordiantion"], "coordination")

    def test_typo_cannot_escape_hard_classification_filter(self) -> None:
        result = core.search_ncs(
            "task coordiantion", scope="unit", limit=3,
            classification_filter={"major_code": "02"},
        )
        self.assertNotIn("TARGET", [row["id"] for row in result["results"]])
        self.assertNotIn("coordination", result.get("unit_query_terms", {}).get("terms", []))

    def test_unique_typo_works_inside_single_unit_scope(self) -> None:
        result = core.search_ncs(
            "task coordiantion", scope="unit", limit=1,
            classification_filter={"major_code": "01"},
        )
        self.assertEqual(result["results"][0]["id"], "TARGET")
        self.assertEqual(result["unit_query_terms"]["resolved_from"]["coordiantion"], "coordination")

    def test_valid_word_and_valid_prefix_do_not_get_typo_correction(self) -> None:
        for query in ("readiness", "coor"):
            with self.subTest(query=query):
                result = core.search_ncs(query, scope="unit", limit=3)
                self.assertEqual(result["results"][0]["id"], "TARGET")
                self.assertNotIn("unit_query_terms", result)

    def test_unknown_word_typo_requires_same_unit_context_evidence(self) -> None:
        core._register_ncs_search_udfs(self.conn)
        self.statements.clear()
        corrections = core._ncs_search_unit_typo_terms(
            self.conn,
            ["coordiantion"],
            {"coordiantion": [], "beta": []},
            {},
            normalized=False,
            context_words=["beta", "coordiantion"],
        )
        # Beta exists in TARGET's criterion, but typo decisions only inspect
        # bounded names, definitions and element names, never the KSA corpus.
        self.assertEqual(corrections, {})
        self.assertFalse(any("FROM ksa_items" in sql or "JOIN ksa_items" in sql for sql in self.statements))
        self.assertFalse(any("FROM performance_criteria" in sql or "JOIN performance_criteria" in sql for sql in self.statements))

    def test_hangul_particle_typo_uses_corpus_name_and_preserves_source(self) -> None:
        self.conn.execute("PRAGMA query_only=OFF")
        self.conn.execute("UPDATE competency_units SET unit_name_raw='인력채용' WHERE unit_code='TARGET'")
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        result = core.search_ncs("인력채옹을", scope="unit", limit=1)
        self.assertEqual(result["results"][0]["text"], "인력채용")
        self.assertEqual(result["unit_query_terms"]["resolved_from"]["인력채옹을"], "인력채용")
        self.assertEqual(self.conn.execute("SELECT criteria_text_raw FROM performance_criteria").fetchone()[0], "alpha beta gamma")


if __name__ == "__main__":
    unittest.main()
