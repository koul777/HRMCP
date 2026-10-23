from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, normalize_concept_key, now_utc
from ncs_mcp.training_recommendation import SHORT_KSA_PENALTY, _concept_quality_issue_penalty_map


class ConceptQualityIssueIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "ncs.db")
        initialize_database(self.conn)
        timestamp = now_utc()
        self.concept_ids = []
        for name in ("short concept", "legacy short concept", "resolved concept", "review concept"):
            self.conn.execute(
                "INSERT INTO ontology_concepts(concept_name, normalized_key, concept_type, definition_status, "
                "relation_status, review_status, created_at, updated_at) "
                "VALUES (?, ?, 'knowledge', 'missing', 'unlinked', 'raw', ?, ?)",
                (name, normalize_concept_key(name), timestamp, timestamp),
            )
            self.concept_ids.append(int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
        short_id, legacy_id, resolved_id, review_id = self.concept_ids
        issues = [
            ("ontology_concept", short_id, "short_ksa", None),
            ("concept", legacy_id, "short_ksa", None),
            ("ontology_concept", resolved_id, "short_ksa", timestamp),
            ("ontology_concept", review_id, "ontology_core_concept_human_review_required", None),
            ("ksa", short_id, "duplicate_text", None),
        ]
        for target_type, target_id, issue_type, resolved_at in issues:
            self.conn.execute(
                "INSERT INTO quality_issues(target_type, target_id, issue_type, severity, issue_detail, "
                "detected_at, resolved_at) VALUES (?, ?, ?, 'info', 'fixture', ?, ?)",
                (target_type, str(target_id), issue_type, timestamp, resolved_at),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_only_open_concept_short_issues_penalize(self) -> None:
        penalty_map = _concept_quality_issue_penalty_map(self.conn, set(self.concept_ids))
        short_id, legacy_id, _, _ = self.concept_ids
        self.assertEqual(sorted(penalty_map), [short_id, legacy_id])
        for concept_id in (short_id, legacy_id):
            self.assertEqual(
                penalty_map[concept_id],
                [{"concept_id": concept_id, "issue_type": "short_ksa", "penalty_multiplier": SHORT_KSA_PENALTY}],
            )

    def test_concept_issue_lookup_uses_the_target_index(self) -> None:
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            _concept_quality_issue_penalty_map(self.conn, set(self.concept_ids))
        finally:
            self.conn.set_trace_callback(None)
        query = next(sql for sql in statements if "target_type IN ('concept', 'ontology_concept')" in sql)
        plan = " ".join(row[3] for row in self.conn.execute(f"EXPLAIN QUERY PLAN {query}"))
        self.assertIn("idx_quality_target", plan)
        self.assertNotIn("idx_quality_type", plan)


if __name__ == "__main__":
    unittest.main()
