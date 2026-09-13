import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import (
    connect,
    initialize_database,
    now_utc,
    recommend_task_transitions as recommend_task_transitions_from_db,
)
from ncs_mcp.training_recommendation import (
    recommend_training_for_task,
    recommend_training_transition,
    resolve_ncs_query_scope,
)


class TrainingScopeAmbiguityTests(unittest.TestCase):
    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        conn = connect(Path(tmp.name) / "ncs.db")
        initialize_database(conn)
        stamp = now_utc()

        def add_scope(major, unit_code, unit_name, *, sub_name="Unique scope"):
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, '01', 'Middle', '01', 'Small', '01', ?)
                """,
                (major, f"Major {major}", sub_name),
            )
            classification_id = conn.execute(
                "SELECT classification_id FROM classifications ORDER BY classification_id DESC LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES (?, ?, '23v1', ?, '3', ?, ?, ?)
                """,
                (unit_code, unit_code.split("_", 1)[0], unit_name, classification_id, stamp, stamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES (?, '1', ?, ?, '3')
                """,
                (unit_code, f"{unit_code} 1", f"{unit_name} task"),
            )
            element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = ?", (unit_code,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, '1', ?)",
                (element_id, f"Perform {unit_name}"),
            )

        add_scope("01", "0101010101_23v1", "Shared unit")
        add_scope("02", "0201010101_23v1", "Shared unit")
        add_scope("03", "0301010101_23v1", "Normalization-Unit", sub_name="Normalization scope")
        conn.commit()
        return tmp, conn

    def test_cross_major_same_unit_name_fails_closed_for_task(self):
        tmp, conn = self._db()
        try:
            result = recommend_training_for_task(conn, query="Shared unit", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "needs_clarification")
        self.assertTrue(result["needs_clarification"])
        self.assertNotIn("recommendations", result)
        self.assertLessEqual(len(result["clarification"]["candidates"]), 8)

    def test_unit_and_sub_classification_collision_fails_closed(self):
        tmp, conn = self._db()
        try:
            conn.execute("UPDATE classifications SET sub_name = 'Collision scope' WHERE major_code = '01'")
            conn.execute("UPDATE competency_units SET unit_name_raw = 'Collision scope' WHERE unit_code = '0201010101_23v1'")
            conn.commit()
            resolution = resolve_ncs_query_scope(conn, "Collision scope")
            result = recommend_training_for_task(conn, query="Collision scope", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertTrue(resolution["needs_clarification"])
        self.assertEqual(resolution["ambiguity_reason"], "unit_classification_collision")
        self.assertEqual(result["error"]["code"], "needs_clarification")
        self.assertNotIn("training_system_matrix", result)
        self.assertNotIn("recommended_path", result)

    def test_cross_major_element_and_unit_name_collision_fails_closed(self):
        tmp, conn = self._db()
        try:
            conn.execute(
                "UPDATE competency_elements SET element_name_raw = 'Normalization-Unit' "
                "WHERE unit_code = '0101010101_23v1'"
            )
            conn.commit()
            resolution = resolve_ncs_query_scope(conn, "Normalization-Unit")
            result = recommend_training_for_task(
                conn, query="Normalization-Unit", save=False
            )
        finally:
            conn.close()
            tmp.cleanup()
        self.assertTrue(resolution["needs_clarification"])
        self.assertEqual(
            resolution["ambiguity_reason"], "cross_type_scope_collision"
        )
        self.assertEqual(result["error"]["code"], "needs_clarification")
        self.assertNotIn("recommendations", result)

    def test_element_inside_same_named_unit_can_select_deepest_task(self):
        tmp, conn = self._db()
        try:
            conn.execute(
                "UPDATE competency_elements SET element_name_raw = 'Normalization-Unit' "
                "WHERE unit_code = '0301010101_23v1'"
            )
            conn.commit()
            resolution = resolve_ncs_query_scope(conn, "Normalization-Unit")
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(resolution["needs_clarification"])
        self.assertEqual(
            resolution["selected_candidate"]["candidate_type"], "element"
        )
        self.assertEqual(
            resolution["selected_candidate"]["unit_code"], "0301010101_23v1"
        )

    def test_candidate_alias_is_review_evidence_not_auto_scope(self):
        tmp, conn = self._db()
        try:
            stamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, unit_code, confidence_score,
                    source_method, review_status, created_at, updated_at
                ) VALUES ('friendly label', 'Normalization-Unit', '0301010101_23v1',
                          0.99, 'test', 'candidate', ?, ?)
                """,
                (stamp, stamp),
            )
            conn.commit()
            resolution = resolve_ncs_query_scope(conn, "friendly label")
            result = recommend_training_for_task(conn, query="friendly label", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertTrue(resolution["needs_clarification"])
        self.assertEqual(resolution["ambiguity_reason"], "candidate_alias_scope_requires_review")
        self.assertEqual(result["error"]["code"], "needs_clarification")
        self.assertNotIn("recommendations", result)

    def test_trusted_alias_selects_verified_unit_when_label_differs(self):
        tmp, conn = self._db()
        try:
            stamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code,
                    small_code, sub_code, unit_code, confidence_score,
                    source_method, review_status, created_at, updated_at
                ) VALUES ('trusted role label', 'Canonical label', '03', '01',
                          '01', '01', '0301010101_23v1', 0.99,
                          'test', 'accepted', ?, ?)
                """,
                (stamp, stamp),
            )
            conn.commit()
            resolution = resolve_ncs_query_scope(conn, "trusted role label")
            result = recommend_training_for_task(conn, query="trusted role label", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(resolution["needs_clarification"])
        self.assertEqual(resolution["selected_candidate"]["unit_code"], "0301010101_23v1")
        self.assertNotEqual(result.get("error", {}).get("code"), "needs_clarification")

    def test_whitespace_and_punctuation_normalization_uses_canonical_unit(self):
        tmp, conn = self._db()
        try:
            resolution = resolve_ncs_query_scope(conn, "  Normalization Unit  ")
            result = recommend_training_for_task(conn, query="  Normalization Unit  ", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(resolution["needs_clarification"])
        self.assertEqual(resolution["selected_candidate"]["unit_code"], "0301010101_23v1")
        # The fixture intentionally has no course rows; the relevant contract
        # here is that normalization selects the canonical unit rather than
        # producing an ambiguity or selecting another major.
        self.assertNotEqual(result.get("error", {}).get("code"), "needs_clarification")
        if result.get("ok"):
            self.assertEqual(result["source_task"]["unit_code"], "0301010101_23v1")

    def test_transition_fails_closed_when_target_scope_is_ambiguous(self):
        tmp, conn = self._db()
        try:
            result = recommend_training_transition(
                conn,
                current_query="Normalization Unit",
                target_query="Shared unit",
                save=False,
            )
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "needs_clarification")
        self.assertEqual(result["error"]["field"], "target_query")
        self.assertNotIn("recommendations", result)
        self.assertNotIn("recommended_path", result)

    def test_non_exact_task_query_does_not_fall_through_to_like_first_row(self):
        tmp, conn = self._db()
        try:
            result = recommend_training_for_task(conn, query="Shared uni", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["clarification"]["reason"], "non_exact_scope_requires_clarification")
        self.assertTrue(result["query_resolution"]["needs_clarification"])
        self.assertIsNone(result["query_resolution"]["selected_candidate"])
        self.assertNotIn("source_task", result)
        self.assertNotIn("recommendations", result)

    def test_task_transition_non_exact_query_does_not_select_like_first_row(self):
        tmp, conn = self._db()
        try:
            result = recommend_task_transitions_from_db(conn, query="Shared uni")
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(
            result["error"]["reason"], "non_exact_scope_requires_clarification"
        )
        self.assertNotIn("source_task", result)
        self.assertNotIn("recommendations", result)

    def test_task_transition_duplicate_exact_query_fails_closed(self):
        tmp, conn = self._db()
        try:
            result = recommend_task_transitions_from_db(
                conn, query="Perform Shared unit"
            )
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["reason"], "multiple_same_name_source_tasks"
        )
        self.assertGreaterEqual(len(result["clarification"]["candidates"]), 2)

    def test_task_transition_explicit_criteria_id_remains_canonical(self):
        tmp, conn = self._db()
        try:
            criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria ORDER BY criteria_id LIMIT 1"
            ).fetchone()[0]
            result = recommend_task_transitions_from_db(
                conn, criteria_id=criteria_id
            )
        finally:
            conn.close()
            tmp.cleanup()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_task"]["criteria_id"], criteria_id)

    def test_criteria_fallback_normalizes_tabs_and_punctuation_across_majors(self):
        tmp, conn = self._db()
        try:
            first_criteria = conn.execute(
                """
                SELECT pc.criteria_id
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                WHERE ce.unit_code = '0101010101_23v1'
                """
            ).fetchone()[0]
            conn.execute(
                "UPDATE performance_criteria SET criteria_text_raw = 'Perform\tShared-unit' WHERE criteria_id = ?",
                (first_criteria,),
            )
            conn.commit()
            # The cheap whitespace-only lookup finds the other plain row. The
            # punctuation pass must still reveal this normalized duplicate.
            result = recommend_training_for_task(conn, query="Perform Shared unit", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["clarification"]["reason"], "multiple_same_name_source_tasks")
        self.assertGreaterEqual(len(result["clarification"]["candidates"]), 2)
        self.assertTrue(all(item.get("criteria_id") for item in result["clarification"]["candidates"][:2]))

    def test_criteria_fallback_normalizes_fullwidth_punctuation_before_selection(self):
        tmp, conn = self._db()
        try:
            rows = conn.execute(
                """
                SELECT pc.criteria_id
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                WHERE ce.unit_code IN ('0101010101_23v1', '0201010101_23v1')
                ORDER BY ce.unit_code
                """
            ).fetchall()
            conn.execute(
                "UPDATE performance_criteria SET criteria_text_raw = 'Execute Safety Check' WHERE criteria_id = ?",
                (rows[0][0],),
            )
            conn.execute(
                "UPDATE performance_criteria SET criteria_text_raw = 'Execute Safety－Check' WHERE criteria_id = ?",
                (rows[1][0],),
            )
            conn.commit()
            result = recommend_training_for_task(
                conn, query="Execute Safety Check", save=False
            )
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["clarification"]["reason"], "multiple_same_name_source_tasks"
        )
        self.assertGreaterEqual(len(result["clarification"]["candidates"]), 2)

    def test_duplicate_exact_criteria_candidates_include_canonical_ids_beyond_display_prefix(self):
        tmp, conn = self._db()
        try:
            unit_code = "0301010101_23v1"
            for index in range(101):
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES (?, ?, ?, 'Other element', '3')
                    """,
                    (unit_code, str(index + 2), f"{unit_code} {index + 2}"),
                )
                element_id = conn.execute("SELECT last_insert_rowid() ").fetchone()[0]
                conn.execute(
                    "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, '1', 'Duplicate exact task')",
                    (element_id,),
                )
            conn.commit()
            result = recommend_training_for_task(conn, query="Duplicate exact task", save=False)
        finally:
            conn.close()
            tmp.cleanup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["clarification"]["reason"], "multiple_same_name_source_tasks")
        self.assertTrue(result["query_resolution"]["needs_clarification"])
        self.assertIsNone(result["query_resolution"]["selected_candidate"])
        candidates = result["clarification"]["candidates"]
        self.assertGreaterEqual(len(candidates), 2)
        self.assertTrue(all(item.get("criteria_id") for item in candidates[:8]))
        self.assertNotIn("recommended_path", result)


if __name__ == "__main__":
    unittest.main()
