from __future__ import annotations

from collections import Counter
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.review_priority import review_priority_summary, write_review_priority_markdown


class ReviewPriorityTests(unittest.TestCase):
    def test_review_priority_orders_high_impact_issues_and_adds_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
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
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', '.')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                    "criteria_id"
                ]
                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition_status,
                        relation_status, review_status, created_at, updated_at
                    ) VALUES ('workforce planning', 'workforceplanning', 'knowledge',
                              'missing', 'unlinked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()[
                    "concept_id"
                ]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('criteria', ?, 'criteria_format_issue', 'warning',
                              'Malformed criteria text', 'Review refined criteria', ?)
                    """,
                    (str(criteria_id), timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ontology_concept', ?, 'hr_core_concept_human_review_required',
                              'high', 'Core concept needs review', 'Define concept', ?)
                    """,
                    (str(concept_id), timestamp),
                )
                conn.commit()

                result = review_priority_summary(conn, limit=5, per_issue_type_limit=2)
            finally:
                conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(
                result["top_items"][0]["issue"]["issue_type"],
                "hr_core_concept_human_review_required",
            )
            self.assertEqual(result["top_items"][0]["context"]["concept_name"], "workforce planning")
            self.assertIn("criteria_format_issue", result["groups"])
            self.assertEqual(
                result["groups"]["criteria_format_issue"][0]["context"]["criteria_text_raw"],
                ".",
            )

    def test_review_priority_truncates_long_operator_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
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
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', ?)
                    """,
                    (element_id, "x" * 1500),
                )
                criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                    "criteria_id"
                ]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('criteria', ?, 'criteria_format_issue', 'warning',
                              ?, 'Review refined criteria', ?)
                    """,
                    (str(criteria_id), "y" * 1500, timestamp),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["criteria_format_issue"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        item = result["top_items"][0]
        self.assertLess(len(item["issue"]["issue_detail"]), 950)
        self.assertIn("issue_detail", item["issue"]["_truncated_fields"])
        self.assertLess(len(item["context"]["criteria_text_raw"]), 950)
        self.assertIn("criteria_text_raw", item["context"]["_truncated_fields"])

    def test_review_priority_top_items_respect_per_issue_type_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for issue_type in ("criteria_format_issue", "suspected_typo"):
                    for index in range(3):
                        conn.execute(
                            """
                            INSERT INTO quality_issues(
                                target_type, target_id, issue_type, severity,
                                issue_detail, suggested_action, detected_at
                            ) VALUES ('unit', ?, ?, 'warning', 'Needs review', 'Review', ?)
                            """,
                            (f"U-{issue_type}-{index}", issue_type, timestamp),
                        )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["criteria_format_issue", "suspected_typo"],
                    limit=10,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        counts = Counter(item["issue"]["issue_type"] for item in result["top_items"])
        self.assertEqual(counts["criteria_format_issue"], 1)
        self.assertEqual(counts["suspected_typo"], 1)
        self.assertEqual(len(result["top_items"]), 2)

    def test_review_priority_top_items_deduplicate_same_issue_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for detail in ("Level mismatch", "Name mismatch"):
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('unit', 'U-1', 'api_value_mismatch', 'warning', ?, 'Review', ?)
                        """,
                        (detail, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('unit', 'U-2', 'api_value_mismatch', 'warning', 'Other', 'Review', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["api_value_mismatch"],
                    limit=10,
                    per_issue_type_limit=10,
                )
            finally:
                conn.close()

        targets = [item["issue"]["target_id"] for item in result["top_items"]]
        self.assertEqual(targets, ["U-1", "U-2"])
        self.assertEqual(result["duplicate_target_items_skipped"], 1)

    def test_write_review_priority_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "review_priority.md"
            write_review_priority_markdown(
                {
                    "ok": True,
                    "issue_types": ["criteria_format_issue"],
                    "open_issue_counts": [
                        {"issue_type": "criteria_format_issue", "severity": "warning", "count": 1}
                    ],
                    "top_items": [
                        {
                            "priority_score": 80,
                            "priority_reason": "Criteria text quality affects task evidence shown to users.",
                            "issue": {
                                "issue_id": 1,
                                "issue_type": "criteria_format_issue",
                                "target_type": "criteria",
                                "target_id": "10",
                                "issue_detail": "Malformed criteria text",
                                "suggested_action": "Review refined criteria",
                            },
                            "context": {"criteria_text_raw": "."},
                        }
                    ],
                    "next_actions": ["Review"],
                },
                out_path,
            )

            text = out_path.read_text(encoding="utf-8")

        self.assertIn("# NCS Review Priority", text)
        self.assertIn("criteria_format_issue #1", text)
        self.assertIn("Malformed criteria text", text)


if __name__ == "__main__":
    unittest.main()
