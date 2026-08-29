from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_vercel_complete_snapshot as verifier  # noqa: E402


class VercelCompleteSnapshotVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "complete.db"
        self.expectations = verifier.SnapshotExpectations(
            exact_table_counts={
                "ksa_items": 2,
                "ksa_atomic_items": 2,
                "ontology_concepts": 2,
                "ksa_concept_links": 2,
                "ksa_atomic_concept_links": 2,
                "criteria_concept_links": 2,
                "element_criteria_ksa_links": 2,
                "ontology_concept_relations": 1,
                "task_similarity_links": 1,
                "training_transition_gold_scenarios": 2,
                "training_transition_scenario_reviews": 1,
                "task_ksa_relations_compact": 2,
                "learning_module_concept_links": 0,
            },
            manifest_values=verifier.DEFAULT_MANIFEST_VALUES,
            gold_status_counts={"candidate": 1, "reviewed": 1},
            min_human_reviewed_labels=2,
            min_merged_reviewed_aliases=1,
        )
        self._create_fixture(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _create_fixture(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE serving_snapshot_manifest (
                    manifest_key TEXT PRIMARY KEY,
                    manifest_value TEXT NOT NULL
                );
                INSERT INTO serving_snapshot_manifest VALUES
                    ('profile', 'vercel-ontology-complete'),
                    ('schema', 'ncs_vercel_ontology_complete_v1'),
                    ('task_ksa_storage', 'task_ksa_relations_compact'),
                    ('task_ksa_compatibility_view', 'task_ksa_concept_relations'),
                    ('label_candidate_scope', 'all_statuses'),
                    ('source_access', 'read_only_immutable');

                CREATE TABLE ksa_items (ksa_id INTEGER PRIMARY KEY);
                INSERT INTO ksa_items VALUES (1), (2);
                CREATE TABLE ksa_atomic_items (atomic_id INTEGER PRIMARY KEY);
                INSERT INTO ksa_atomic_items VALUES (10), (20);
                CREATE TABLE ontology_concepts (concept_id INTEGER PRIMARY KEY);
                INSERT INTO ontology_concepts VALUES (100), (200);
                CREATE TABLE ksa_concept_links (link_id INTEGER PRIMARY KEY);
                INSERT INTO ksa_concept_links VALUES (1), (2);
                CREATE TABLE ksa_atomic_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    atomic_id INTEGER,
                    concept_id INTEGER
                );
                INSERT INTO ksa_atomic_concept_links VALUES
                    (1, 10, 100), (2, 20, 200);
                CREATE TABLE criteria_concept_links (link_id INTEGER PRIMARY KEY);
                INSERT INTO criteria_concept_links VALUES (1), (2);
                CREATE TABLE element_criteria_ksa_links (link_id INTEGER PRIMARY KEY);
                INSERT INTO element_criteria_ksa_links VALUES (1), (2);
                CREATE TABLE ontology_concept_relations (relation_id INTEGER PRIMARY KEY);
                INSERT INTO ontology_concept_relations VALUES (1);
                CREATE TABLE task_similarity_links (similarity_id INTEGER PRIMARY KEY);
                INSERT INTO task_similarity_links VALUES (1);
                CREATE TABLE learning_module_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    concept_id INTEGER
                );

                CREATE TABLE training_transition_gold_scenarios (
                    scenario_id INTEGER PRIMARY KEY,
                    review_status TEXT NOT NULL
                );
                INSERT INTO training_transition_gold_scenarios VALUES
                    (1, 'candidate'), (2, 'reviewed');
                CREATE TABLE training_transition_scenario_reviews (
                    review_id INTEGER PRIMARY KEY
                );
                INSERT INTO training_transition_scenario_reviews VALUES (1);

                CREATE TABLE ontology_concept_label_candidates (
                    label_id INTEGER PRIMARY KEY,
                    concept_id INTEGER NOT NULL,
                    label_text TEXT NOT NULL,
                    normalized_label_key TEXT NOT NULL,
                    review_status TEXT NOT NULL
                );
                INSERT INTO ontology_concept_label_candidates VALUES
                    (1, 100, '채용', '채용', 'human_reviewed'),
                    (2, 200, '면접', '면접', 'human_reviewed');
                CREATE TABLE ontology_concept_aliases (
                    alias_id INTEGER PRIMARY KEY,
                    concept_id INTEGER NOT NULL,
                    alias_text TEXT NOT NULL,
                    normalized_alias_key TEXT NOT NULL,
                    alias_source TEXT NOT NULL
                );
                INSERT INTO ontology_concept_aliases VALUES
                    (1, 100, '채용', '채용', 'source'),
                    (2, 200, '면접', '면접', 'ontology_label_human_reviewed');

                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    element_id INTEGER NOT NULL
                );
                INSERT INTO performance_criteria VALUES (1000, 5000), (2000, 6000);
                CREATE TABLE task_ksa_relation_types (
                    relation_type_code INTEGER PRIMARY KEY,
                    relation_type TEXT NOT NULL UNIQUE
                );
                INSERT INTO task_ksa_relation_types VALUES
                    (1, 'knowledge_enables_skill'),
                    (2, 'attitude_supports_skill');
                CREATE TABLE task_ksa_review_statuses (
                    review_status_code INTEGER PRIMARY KEY,
                    review_status TEXT NOT NULL UNIQUE
                );
                INSERT INTO task_ksa_review_statuses VALUES (1, 'candidate');
                CREATE TABLE task_ksa_relations_compact (
                    relation_id INTEGER PRIMARY KEY,
                    criteria_id INTEGER NOT NULL,
                    source_atomic_id INTEGER NOT NULL,
                    target_atomic_id INTEGER NOT NULL,
                    relation_type_code INTEGER NOT NULL,
                    confidence_score REAL NOT NULL,
                    review_status_code INTEGER NOT NULL
                );
                INSERT INTO task_ksa_relations_compact VALUES
                    (1, 1000, 10, 20, 1, 0.75, 1),
                    (2, 2000, 20, 10, 2, 0.60, 1);
                CREATE VIEW task_ksa_concept_relations AS
                SELECT
                    relation.relation_id,
                    relation.criteria_id,
                    criteria.element_id,
                    source_link.concept_id AS source_concept_id,
                    relation_type.relation_type,
                    target_link.concept_id AS target_concept_id,
                    relation.source_atomic_id,
                    relation.target_atomic_id,
                    NULL AS evidence_text,
                    relation.confidence_score,
                    review_status.review_status,
                    NULL AS created_at
                FROM task_ksa_relations_compact AS relation
                JOIN task_ksa_relation_types AS relation_type
                  ON relation_type.relation_type_code = relation.relation_type_code
                JOIN task_ksa_review_statuses AS review_status
                  ON review_status.review_status_code = relation.review_status_code
                LEFT JOIN performance_criteria AS criteria
                  ON criteria.criteria_id = relation.criteria_id
                LEFT JOIN ksa_atomic_concept_links AS source_link
                  ON source_link.atomic_id = relation.source_atomic_id
                LEFT JOIN ksa_atomic_concept_links AS target_link
                  ON target_link.atomic_id = relation.target_atomic_id;
                """
            )
            conn.commit()

    @staticmethod
    def _check_map(report: dict[str, object]) -> dict[str, str]:
        return {
            str(item["id"]): str(item["status"])
            for item in report["checks"]  # type: ignore[index]
        }

    def test_valid_snapshot_passes_without_mutation(self) -> None:
        before_hash = self._sha256(self.db_path)
        report = verifier.verify_snapshot(
            self.db_path,
            expectations=self.expectations,
        )
        after_hash = self._sha256(self.db_path)

        self.assertTrue(report["ok"])
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(0, report["summary"]["failed_count"])
        self.assertEqual(
            "pass",
            self._check_map(report)["compact_relation_distribution_parity"],
        )
        self.assertEqual(
            0,
            report["metrics"]["human_reviewed_label_aliases"][
                "missing_alias_count"
            ],
        )

    def test_detects_manifest_alias_and_forbidden_contamination(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                UPDATE serving_snapshot_manifest
                SET manifest_value = 'vercel-ontology-light'
                WHERE manifest_key = 'profile'
                """
            )
            conn.execute(
                "DELETE FROM ontology_concept_aliases WHERE concept_id = 200"
            )
            conn.execute("CREATE TABLE review_audit_log (audit_id INTEGER)")
            conn.commit()

        report = verifier.verify_snapshot(
            self.db_path,
            run_quick_check=False,
            expectations=self.expectations,
        )
        checks = self._check_map(report)

        self.assertFalse(report["ok"])
        self.assertEqual("fail", checks["manifest_profile"])
        self.assertEqual("fail", checks["human_reviewed_alias_merge_minimum"])
        self.assertEqual("fail", checks["human_reviewed_label_alias_coverage"])
        self.assertEqual("fail", checks["no_default_seed_or_legacy_contamination"])
        self.assertEqual("skipped", checks["sqlite_quick_check"])

    def test_detects_compact_view_parity_drift(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DROP VIEW task_ksa_concept_relations")
            conn.execute(
                """
                CREATE VIEW task_ksa_concept_relations AS
                SELECT
                    relation.relation_id,
                    relation.criteria_id,
                    criteria.element_id,
                    100 AS source_concept_id,
                    relation_type.relation_type,
                    200 AS target_concept_id,
                    relation.source_atomic_id,
                    relation.target_atomic_id,
                    NULL AS evidence_text,
                    relation.confidence_score,
                    review_status.review_status,
                    NULL AS created_at
                FROM task_ksa_relations_compact AS relation
                JOIN task_ksa_relation_types AS relation_type
                  ON relation_type.relation_type_code = relation.relation_type_code
                JOIN task_ksa_review_statuses AS review_status
                  ON review_status.review_status_code = relation.review_status_code
                LEFT JOIN performance_criteria AS criteria
                  ON criteria.criteria_id = relation.criteria_id
                WHERE relation.relation_id = 1
                """
            )
            conn.commit()

        report = verifier.verify_snapshot(
            self.db_path,
            run_quick_check=False,
            expectations=self.expectations,
        )
        checks = self._check_map(report)

        self.assertFalse(report["ok"])
        self.assertEqual("fail", checks["compact_relation_projection_parity"])
        self.assertEqual("fail", checks["compact_relation_distribution_parity"])

    def test_main_writes_optional_report_and_returns_nonzero_on_bad_header(self) -> None:
        report_path = self.root / "verification.json"
        stdout = io.StringIO()
        with patch.object(verifier, "DEFAULT_EXPECTATIONS", self.expectations):
            with redirect_stdout(stdout):
                exit_code = verifier.main(
                    [
                        "--db",
                        str(self.db_path),
                        "--skip-quick-check",
                        "--out",
                        str(report_path),
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        self.assertTrue(json.loads(report_path.read_text(encoding="utf-8"))["ok"])

        invalid = self.root / "invalid.db"
        invalid.write_bytes(b"not a sqlite database")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = verifier.main(["--db", str(invalid)])
        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
