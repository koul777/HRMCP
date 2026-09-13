from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "evaluate_ncs_official_compound_spacing.py"
)
SPEC = importlib.util.spec_from_file_location(
    "evaluate_ncs_official_compound_spacing", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class OfficialCompoundSpacingEvaluatorTests(unittest.TestCase):
    def test_sampling_is_deterministic_and_derived_from_official_names(self) -> None:
        names = ["인사관리", "교육운영", "노무업무", "공백 관리", "기타"]
        first, eligible = MODULE.build_spacing_cases(names, sample_size=2)
        second, repeated_eligible = MODULE.build_spacing_cases(
            list(reversed(names)), sample_size=2
        )

        self.assertEqual(first, second)
        self.assertEqual(eligible, 3)
        self.assertEqual(repeated_eligible, 3)
        self.assertEqual(
            [item.digest for item in first],
            sorted(hashlib.sha256(item.official_name.encode()).hexdigest() for item in first),
        )
        for item in first:
            self.assertEqual(item.query.replace(" ", ""), item.official_name)

    def test_metric_summary_is_exact(self) -> None:
        summary = MODULE.summarize_measurements(
            [
                MODULE.Measurement(10.0, 3, 1),
                MODULE.Measurement(20.0, 4, 2),
                MODULE.Measurement(30.0, 5, None),
                MODULE.Measurement(40.0, 4, 3),
            ]
        )

        self.assertEqual(summary["hit_at_1"], 0.25)
        self.assertEqual(summary["hit_at_3"], 0.75)
        self.assertEqual(summary["mrr_at_3"], 0.4583)
        self.assertEqual(summary["latency_ms"], {
            "median": 25.0, "p95": 40.0, "mean": 25.0,
        })
        self.assertEqual(summary["sql_statements"], {
            "min": 3, "median": 4.0, "max": 5, "mean": 4.0,
        })

    def test_report_and_markdown_cannot_leak_case_level_data(self) -> None:
        identity = {
            "resolved_path": "fixture.db",
            "size_bytes": 100,
            "mtime_ns": 200,
            "device": 1,
            "inode": 2,
            "sha256": None,
            "sha256_status": "not_requested",
        }
        factory = MODULE.ReadOnlyDbFactory(Path("fixture.db"))
        factory.open_count = 2
        factory.query_only_verified_count = 2
        aggregate = MODULE.summarize_measurements(
            [MODULE.Measurement(1.0, 3, 1)]
        )
        report = MODULE.build_report(
            db_path=Path("fixture.db"),
            suffixes=("관리",),
            eligible_count=10,
            evaluated_count=1,
            sample_size=1,
            limit=3,
            before_summary=aggregate,
            after_summary=aggregate,
            identity_before=identity,
            identity_after=dict(identity),
            expected_size=None,
            expected_sha256=None,
            factory=factory,
        )

        MODULE.assert_aggregate_only(report)
        serialized = json.dumps(report, ensure_ascii=False)
        markdown = MODULE.render_markdown(report)
        for secret in ("급여 지급", "급여지급", "UNIT_SECRET"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, markdown)
        for forbidden_key in MODULE.FORBIDDEN_REPORT_KEYS:
            self.assertNotIn(f'"{forbidden_key}"', serialized)

    def test_output_path_cannot_equal_database_path(self) -> None:
        db = Path("same.db")
        with self.assertRaisesRegex(ValueError, "must not be the evaluation DB"):
            MODULE.validate_output_paths(db, db, Path("report.md"))
        with self.assertRaisesRegex(ValueError, "must not be the evaluation DB"):
            MODULE.validate_output_paths(db, Path("report.json"), db)

    def test_read_only_factory_and_evaluation_preserve_database_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fixture.db"
            self._create_fixture_db(db_path)
            before = MODULE.db_identity(db_path, include_sha256=True)
            factory = MODULE.ReadOnlyDbFactory(db_path)
            with factory.open() as conn:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute(
                        "INSERT INTO competency_units "
                        "(unit_code, unit_name_raw, api_definition, unit_level_raw, classification_id) "
                        "VALUES ('WRITE', '금지관리', '', '4', 1)"
                    )

            with MODULE.configured_search(factory):
                cases, eligible = MODULE.build_spacing_cases(
                    MODULE.load_official_unit_names(factory),
                    suffixes=("지급",),
                    sample_size=1,
                )
                baseline, after = MODULE.evaluate_cases(factory, cases, limit=3)
            final = MODULE.db_identity(db_path, include_sha256=True)

            self.assertEqual(eligible, 1)
            self.assertTrue(MODULE.identities_equal(before, final))
            self.assertIsNone(baseline[0].rank)
            self.assertEqual(after[0].rank, 2)
            self.assertEqual(factory.open_count, factory.query_only_verified_count)

    @staticmethod
    def _create_fixture_db(path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_code TEXT, major_name TEXT,
                    middle_code TEXT, middle_name TEXT,
                    small_code TEXT, small_name TEXT,
                    sub_code TEXT, sub_name TEXT,
                    duty_order TEXT
                );
                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY,
                    unit_name_raw TEXT,
                    api_definition TEXT,
                    unit_level_raw TEXT,
                    classification_id INTEGER
                );
                CREATE TABLE ncs_query_aliases (
                    unit_code TEXT, alias_text TEXT, normalized_query TEXT
                );
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY,
                    element_name_raw TEXT,
                    unit_code TEXT
                );
                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    criteria_text_raw TEXT,
                    criteria_text_refined TEXT,
                    element_id INTEGER
                );
                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY,
                    ksa_type_name TEXT,
                    ksa_text_raw TEXT,
                    ksa_text_refined TEXT,
                    element_id INTEGER
                );
                INSERT INTO classifications VALUES (
                    1, '02', '경영', '02', '인사', '02', '인사관리', '01', '인사', '1'
                );
                INSERT INTO competency_units VALUES
                    ('JOINED', '급여지급', '', '4', 1),
                    ('SPACED', '급여 지급 안내', '', '4', 1),
                    ('DEFINITION', '기타수행', '급여지급', '4', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
