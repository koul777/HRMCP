from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import export_interview_serving_db as serving_export  # noqa: E402


class ExportInterviewServingDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.db"
        self._create_source_fixture(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_source_fixture(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_code TEXT,
                    middle_code TEXT,
                    small_code TEXT,
                    sub_code TEXT
                );
                INSERT INTO classifications VALUES (1, '02', '02', '01', '01');

                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY,
                    classification_id INTEGER,
                    unit_name_raw TEXT
                );
                INSERT INTO competency_units VALUES ('02020101_01v1', 1, '인사기획');

                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY,
                    unit_code TEXT,
                    element_name_raw TEXT
                );
                INSERT INTO competency_elements VALUES (10, '02020101_01v1', '인사전략 수립');

                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    element_id INTEGER,
                    criteria_text_raw TEXT
                );
                INSERT INTO performance_criteria VALUES (100, 10, '환경을 분석할 수 있다.');

                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY,
                    element_id INTEGER,
                    ksa_type_name TEXT,
                    ksa_text_raw TEXT
                );
                INSERT INTO ksa_items VALUES (1000, 10, '지식', '인사환경 분석 지식');

                CREATE TABLE ncs_training_courses (
                    training_course_id INTEGER PRIMARY KEY,
                    compe_unit_name TEXT,
                    train_goal TEXT
                );
                INSERT INTO ncs_training_courses VALUES (2000, '인사기획', '인사환경을 분석한다.');

                CREATE TABLE ncs_query_aliases (
                    alias_id INTEGER PRIMARY KEY,
                    unit_code TEXT,
                    alias_text TEXT,
                    normalized_query TEXT
                );
                INSERT INTO ncs_query_aliases VALUES (1, '02020101_01v1', 'HR 기획', '인사기획');

                CREATE TABLE ontology_concepts (
                    concept_id INTEGER PRIMARY KEY,
                    concept_name TEXT,
                    normalized_key TEXT,
                    concept_type TEXT,
                    definition TEXT,
                    definition_source TEXT,
                    definition_status TEXT,
                    relation_status TEXT,
                    review_status TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                INSERT INTO ontology_concepts VALUES
                    (1, '채용', '채용', 'knowledge', '채용: 고정 정의', 'boilerplate',
                     'missing', 'linked', 'raw', '2026-01-01', '2026-01-02'),
                    (2, '면접', '면접', 'skill', '면접: 고정 정의', 'boilerplate',
                     'missing', 'linked', 'raw', '2026-01-03', '2026-01-04');

                CREATE TABLE ontology_concept_aliases (
                    alias_id INTEGER PRIMARY KEY,
                    concept_id INTEGER,
                    alias_text TEXT,
                    normalized_alias_key TEXT,
                    alias_source TEXT,
                    created_at TEXT
                );
                INSERT INTO ontology_concept_aliases VALUES
                    (1, 1, '채용', '채용', 'raw_ksa', '2026-01-01'),
                    (2, 1, '채용 업무', '채용', 'raw_ksa', '2026-01-01'),
                    (3, 1, '인재채용', '인재채용', 'raw_ksa', '2026-01-01'),
                    (4, 2, '면접', 'interview', 'raw_ksa', '2026-01-01'),
                    (5, 2, '구조화 면접', '구조화면접', 'raw_ksa', '2026-01-01');

                CREATE TABLE ksa_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    ksa_id INTEGER,
                    concept_id INTEGER,
                    link_status TEXT,
                    created_at TEXT
                );
                INSERT INTO ksa_concept_links VALUES
                    (1, 1000, 1, 'raw', '2026-01-01'),
                    (2, 1000, 2, 'raw', '2026-01-02');

                CREATE TABLE ncs_training_course_unit_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER,
                    unit_code TEXT,
                    link_method TEXT
                );
                INSERT INTO ncs_training_course_unit_links VALUES
                    (1, 2000, '02020101_01v1', 'ncs_cl_cd_exact');

                CREATE TABLE ncs_training_course_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER,
                    unit_code TEXT,
                    concept_id INTEGER,
                    link_method TEXT
                );
                INSERT INTO ncs_training_course_concept_links VALUES
                    (1, 2000, '02020101_01v1', 1, 'unit_ksa_concept_inherited'),
                    (2, 2000, '02020101_01v1', 2, 'training_goal_concept_text');

                CREATE TABLE ncs_training_course_element_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER,
                    unit_code TEXT,
                    element_id INTEGER,
                    link_method TEXT
                );
                INSERT INTO ncs_training_course_element_links VALUES
                    (1, 2000, '02020101_01v1', 10, 'ncs_unit_element');

                CREATE TABLE training_goal_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER,
                    unit_code TEXT,
                    element_id INTEGER,
                    concept_id INTEGER,
                    link_method TEXT
                );
                INSERT INTO training_goal_concept_links VALUES
                    (1, 2000, '02020101_01v1', 10, 1, 'training_goal_element_implied_concept'),
                    (2, 2000, '02020101_01v1', 10, 2, 'training_goal_concept_text');

                CREATE TABLE training_delivery_relations (
                    relation_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER,
                    relation_type TEXT,
                    relation_value TEXT
                );
                INSERT INTO training_delivery_relations VALUES (1, 2000, 'method', '실습');

                CREATE TABLE ncs_career_paths (
                    career_path_id INTEGER PRIMARY KEY,
                    major_code TEXT,
                    middle_code TEXT,
                    small_code TEXT,
                    sub_code TEXT,
                    matched_unit_code TEXT,
                    job_name TEXT
                );
                INSERT INTO ncs_career_paths VALUES
                    (1, '02', '02', '01', '01', '02020101_01v1', '인사담당자');

                CREATE TABLE ncs_qualification_items (
                    jm_cd TEXT PRIMARY KEY,
                    jm_nm TEXT,
                    exam_insti_nm TEXT
                );
                INSERT INTO ncs_qualification_items VALUES ('Q1', '인사 자격', '기관');

                CREATE TABLE ncs_unit_qualification_links (
                    link_id INTEGER PRIMARY KEY,
                    unit_code TEXT,
                    jm_cd TEXT,
                    link_method TEXT
                );
                INSERT INTO ncs_unit_qualification_links VALUES
                    (1, '02020101_01v1', 'Q1', 'ncs_cl_cd_exact');

                CREATE TABLE ncs_unit_standard_training (
                    unit_standard_id INTEGER PRIMARY KEY,
                    source_file TEXT,
                    source_row_number INTEGER,
                    unit_code_raw TEXT,
                    unit_name TEXT,
                    unit_level TEXT,
                    standard_training_hours REAL,
                    matched_unit_code TEXT,
                    match_status TEXT,
                    source_payload TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                INSERT INTO ncs_unit_standard_training VALUES
                    (1, 'unit-standard.csv', 2, '02020101_01v1', '인사기획', '6',
                     40.0, '02020101_01v1', 'matched_unit_exact', '{"source": "fixture"}',
                     '2026-01-01', '2026-01-02');

                CREATE TABLE ontology_concept_label_candidates (
                    label_id INTEGER PRIMARY KEY,
                    concept_id INTEGER,
                    label_text TEXT
                );
                INSERT INTO ontology_concept_label_candidates VALUES (1, 1, '채용');

                CREATE TABLE review_audit_log (
                    audit_id INTEGER PRIMARY KEY,
                    action TEXT
                );
                INSERT INTO review_audit_log VALUES (1, 'reviewed');
                """
            )
            for table in serving_export.VERCEL_ONTOLOGY_LIGHT_EMPTY_COMPATIBILITY_TABLES:
                conn.execute(
                    f'CREATE TABLE "{table}" (row_id INTEGER, payload TEXT)'
                )
                conn.execute(
                    f'INSERT INTO "{table}" VALUES (?, ?)',
                    (1, f"{table}-payload"),
                )
            conn.commit()

    def _table_names(self, conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    def _column_names(self, conn: sqlite3.Connection, table: str) -> list[str]:
        return [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]

    def test_vercel_ontology_light_projects_filters_and_compatibility_tables(self) -> None:
        destination = self.root / "vercel-light.db"
        report = serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_LIGHT,
        )

        with closing(sqlite3.connect(self.source)) as src, closing(
            sqlite3.connect(destination)
        ) as dst:
            tables = self._table_names(dst)
            self.assertNotIn("ontology_concept_label_candidates", tables)
            self.assertNotIn("review_audit_log", tables)
            for table in serving_export.VERCEL_ONTOLOGY_LIGHT_EMPTY_COMPATIBILITY_TABLES:
                self.assertIn(table, tables)
                self.assertEqual(0, dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                self.assertEqual(
                    self._column_names(src, table),
                    self._column_names(dst, table),
                )

            self.assertEqual(
                self._column_names(src, "ontology_concepts"),
                self._column_names(dst, "ontology_concepts"),
            )
            self.assertEqual(
                self._column_names(src, "ontology_concept_aliases"),
                self._column_names(dst, "ontology_concept_aliases"),
            )
            self.assertEqual(
                self._column_names(src, "ksa_concept_links"),
                self._column_names(dst, "ksa_concept_links"),
            )
            concepts = dst.execute(
                """
                SELECT concept_id, concept_name, normalized_key, concept_type,
                       definition, definition_source, definition_status,
                       relation_status, review_status, created_at, updated_at
                FROM ontology_concepts
                ORDER BY concept_id
                """
            ).fetchall()
            self.assertEqual(2, len(concepts))
            self.assertEqual((1, "채용", "채용", "knowledge"), concepts[0][:4])
            self.assertEqual((None, None), concepts[0][4:6])
            self.assertEqual(("missing", "linked", "raw"), concepts[0][6:9])
            self.assertEqual((None, None), concepts[0][9:11])

            self.assertEqual(
                [2, 3, 4, 5],
                [
                    row[0]
                    for row in dst.execute(
                        "SELECT alias_id FROM ontology_concept_aliases ORDER BY alias_id"
                    ).fetchall()
                ],
            )
            self.assertEqual(
                [(1, None), (2, None)],
                dst.execute(
                    "SELECT link_id, created_at FROM ksa_concept_links ORDER BY link_id"
                ).fetchall(),
            )
            self.assertEqual(
                [2],
                [
                    row[0]
                    for row in dst.execute(
                        "SELECT link_id FROM ncs_training_course_concept_links ORDER BY link_id"
                    ).fetchall()
                ],
            )
            self.assertEqual(
                [2],
                [
                    row[0]
                    for row in dst.execute(
                        "SELECT link_id FROM training_goal_concept_links ORDER BY link_id"
                    ).fetchall()
                ],
            )
            for table in (
                "ncs_training_course_unit_links",
                "ncs_training_course_element_links",
                "training_delivery_relations",
                "ncs_career_paths",
                "ncs_qualification_items",
                "ncs_unit_qualification_links",
                "ncs_unit_standard_training",
            ):
                self.assertEqual(1, dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            self.assertEqual(
                ("02020101_01v1", 40.0, "matched_unit_exact"),
                dst.execute(
                    """
                    SELECT matched_unit_code, standard_training_hours, match_status
                    FROM ncs_unit_standard_training
                    WHERE unit_standard_id = 1
                    """
                ).fetchone(),
            )

            indexes = {
                str(row[0])
                for row in dst.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "idx_serving_ksa_type",
                    "idx_serving_units_name",
                    "idx_serving_alias_unit",
                    "idx_serving_ont_concepts_key",
                    "idx_serving_ont_concepts_id",
                    "idx_serving_ont_alias_concept",
                    "idx_serving_ksa_concept_ksa",
                    "idx_serving_course_unit_unit",
                    "idx_serving_course_concept_linked",
                    "idx_serving_course_element_element",
                    "idx_serving_course_goal_concept",
                    "idx_serving_career_unit",
                    "idx_serving_qualification_name",
                    "idx_serving_unit_qualification_unit",
                    "idx_serving_unit_standard_matched",
                }.issubset(indexes)
            )
            concept_indexes = {
                str(row[1]): int(row[2])
                for row in dst.execute(
                    "PRAGMA index_list('ontology_concepts')"
                ).fetchall()
            }
            self.assertEqual(1, concept_indexes["idx_serving_ont_concepts_id"])

        self.assertEqual(
            serving_export.PROFILE_VERCEL_ONTOLOGY_LIGHT,
            report["profile"],
        )
        self.assertGreater(report["size_bytes"], 0)
        self.assertEqual(2, report["tables"]["ontology_concepts"])
        self.assertEqual(0, report["tables"]["ontology_concept_relations"])
        dispositions = report["omitted_or_empty_tables"]
        self.assertEqual(
            sorted(serving_export.VERCEL_ONTOLOGY_LIGHT_EMPTY_COMPATIBILITY_TABLES),
            dispositions["empty_compatibility"],
        )
        self.assertEqual([], dispositions["source_missing_compatibility"])
        self.assertIn("ontology_concept_label_candidates", dispositions["omitted"])
        self.assertIn("review_audit_log", dispositions["omitted"])

    def test_vercel_ontology_light_records_missing_optional_compatibility_table(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("DROP TABLE ncs_job_base_factors")
            conn.commit()

        destination = self.root / "vercel-light-missing-compatible.db"
        report = serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_LIGHT,
        )

        with closing(sqlite3.connect(destination)) as conn:
            self.assertNotIn("ncs_job_base_factors", self._table_names(conn))
        self.assertEqual(
            ["ncs_job_base_factors"],
            report["omitted_or_empty_tables"]["source_missing_compatibility"],
        )

    def test_default_mode_keeps_original_core_only_selection(self) -> None:
        destination = self.root / "default.db"
        report = serving_export.export_serving_db(self.source, destination)

        with closing(sqlite3.connect(destination)) as conn:
            self.assertEqual(
                set(serving_export.CORE_TABLES) | {"ncs_query_aliases"},
                self._table_names(conn),
            )
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0])
        self.assertEqual(serving_export.PROFILE_DEFAULT, report["profile"])
        self.assertIn(
            "ontology_concepts",
            report["omitted_or_empty_tables"]["omitted"],
        )

    def test_default_profile_include_flags_keep_legacy_unfiltered_rows(self) -> None:
        destination = self.root / "legacy-flags.db"
        serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_DEFAULT,
            include_training_links=True,
            include_ontology=True,
        )

        with closing(sqlite3.connect(destination)) as conn:
            self.assertEqual(
                ("채용: 고정 정의", "boilerplate", "2026-01-01", "2026-01-02"),
                conn.execute(
                    """
                    SELECT definition, definition_source, created_at, updated_at
                    FROM ontology_concepts WHERE concept_id = 1
                    """
                ).fetchone(),
            )
            self.assertEqual(5, conn.execute("SELECT COUNT(*) FROM ontology_concept_aliases").fetchone()[0])
            self.assertEqual(
                1,
                conn.execute(
                    """
                    SELECT COUNT(*) FROM ncs_training_course_concept_links
                    WHERE link_method = 'unit_ksa_concept_inherited'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute(
                    """
                    SELECT COUNT(*) FROM training_goal_concept_links
                    WHERE link_method = 'training_goal_element_implied_concept'
                    """
                ).fetchone()[0],
            )
            self.assertIn("ontology_concept_label_candidates", self._table_names(conn))

    def test_light_profile_rejects_separate_include_flags(self) -> None:
        for flag_name in (
            "include_training_links",
            "include_ontology",
            "include_task_ontology",
        ):
            with self.subTest(flag_name=flag_name):
                destination = self.root / f"invalid-{flag_name}.db"
                with self.assertRaisesRegex(
                    ValueError,
                    "already selects its complete table set",
                ):
                    serving_export.export_serving_db(
                        self.source,
                        destination,
                        profile=serving_export.PROFILE_VERCEL_ONTOLOGY_LIGHT,
                        **{flag_name: True},
                    )
                self.assertFalse(destination.exists())


class ExportVercelOntologyCompleteDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "complete-source.db"
        ExportInterviewServingDatabaseTests._create_source_fixture(self, self.source)
        self._upgrade_complete_fixture()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _upgrade_complete_fixture(self) -> None:
        recreated_tables = (
            "ontology_concept_relations",
            "ontology_concept_label_candidates",
            "ksa_atomic_items",
            "ksa_atomic_concept_links",
            "criteria_concept_links",
            "task_ksa_concept_relations",
            "task_similarity_links",
            "element_criteria_ksa_links",
            "quality_issues",
            "ncs_unit_job_base_links",
            "ncs_job_base_competencies",
            "ncs_job_base_factors",
            "ncs_external_training_zip_courses",
            "ncs_occupation_code_mappings",
        )
        with closing(sqlite3.connect(self.source)) as conn:
            for table in recreated_tables:
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.executescript(
                """
                INSERT INTO ontology_concepts VALUES
                    (3, 'workforce analytics', 'workforceanalytics', 'knowledge',
                     'A reviewed definition.', 'human_review', 'defined', 'linked',
                     'human_reviewed', '2026-01-05', '2026-01-06');
                INSERT INTO ontology_concept_aliases VALUES
                    (6, 1, 'existing reviewed label', 'existingreviewedlabel',
                     'raw_ksa', '2026-01-01');

                CREATE TABLE ontology_concept_relations (
                    relation_id INTEGER PRIMARY KEY,
                    source_concept_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_concept_id INTEGER NOT NULL,
                    relation_label TEXT,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO ontology_concept_relations VALUES
                    (1, 1, 'related_to', 2, 'related evidence', 'human_reviewed', '2026-01-01');

                CREATE TABLE ontology_concept_label_candidates (
                    label_id INTEGER PRIMARY KEY,
                    concept_id INTEGER NOT NULL,
                    source_ksa_id INTEGER,
                    source_atomic_id INTEGER,
                    source_scope_key TEXT NOT NULL,
                    concept_type TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    label_text TEXT NOT NULL,
                    normalized_label_key TEXT NOT NULL,
                    label_role TEXT NOT NULL,
                    source_method TEXT NOT NULL,
                    candidate_rank INTEGER NOT NULL,
                    evidence_text TEXT,
                    confidence_score REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ontology_concept_label_candidates VALUES
                    (1, 1, 1000, 5001, '02020101_01v1', 'knowledge',
                     'repeated source text', 'talent acquisition', 'talentacquisition',
                     'short_representative_label', 'human', 1, 'repeated evidence',
                     1.0, 'human_reviewed', '2026-01-01', '2026-01-02'),
                    (2, 1, 1000, 5001, '02020101_01v1', 'knowledge',
                     'repeated source text', 'talent acquisition', 'talentacquisition',
                     'short_representative_label', 'human', 2, 'duplicate evidence',
                     0.9, 'human_reviewed', '2026-01-01', '2026-01-02'),
                    (3, 1, 1000, 5001, '02020101_01v1', 'knowledge',
                     'repeated source text', 'existing reviewed label',
                     'existingreviewedlabel',
                     'short_representative_label', 'human', 3, 'already an alias',
                     1.0, 'human_reviewed', '2026-01-01', '2026-01-02'),
                    (4, 2, 1000, 5002, '02020101_01v1', 'skill',
                     'model source text', 'behavior interview', 'behaviorinterview',
                     'short_representative_label', 'model', 1, 'model evidence',
                     0.8, 'llm_reviewed', '2026-01-01', '2026-01-02');

                CREATE TABLE ksa_atomic_items (
                    atomic_id INTEGER PRIMARY KEY,
                    ksa_id INTEGER NOT NULL,
                    element_id INTEGER NOT NULL,
                    ksa_type_name TEXT NOT NULL,
                    atom_index INTEGER NOT NULL,
                    atom_text TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    split_method TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO ksa_atomic_items VALUES
                    (5001, 1000, 10, 'knowledge', 1, 'talent acquisition',
                     'talentacquisition', 'rule_based', 'human_reviewed', '2026-01-01'),
                    (5002, 1000, 10, 'skill', 2, 'behavior interview',
                     'behaviorinterview', 'rule_based', 'human_reviewed', '2026-01-01');

                CREATE TABLE ksa_atomic_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    atomic_id INTEGER NOT NULL,
                    concept_id INTEGER NOT NULL,
                    link_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO ksa_atomic_concept_links VALUES
                    (1, 5001, 1, 'human_reviewed', '2026-01-01'),
                    (2, 5002, 2, 'human_reviewed', '2026-01-01');

                CREATE TABLE criteria_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    criteria_id INTEGER NOT NULL,
                    concept_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    link_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO criteria_concept_links VALUES
                    (1, 100, 1, 'requires', 'human_reviewed', '2026-01-01'),
                    (2, 100, 2, 'requires', 'human_reviewed', '2026-01-01');

                CREATE TABLE task_ksa_concept_relations (
                    relation_id INTEGER PRIMARY KEY,
                    criteria_id INTEGER NOT NULL,
                    element_id INTEGER NOT NULL,
                    source_concept_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_concept_id INTEGER NOT NULL,
                    source_atomic_id INTEGER NOT NULL,
                    target_atomic_id INTEGER NOT NULL,
                    evidence_text TEXT,
                    confidence_score REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO task_ksa_concept_relations VALUES
                    (11, 100, 10, 1, 'knowledge_enables_skill', 2, 5001, 5002,
                     'duplicated task evidence', 0.62, 'human_reviewed', '2026-01-01'),
                    (12, 100, 10, 2, 'attitude_supports_skill', 1, 5002, 5001,
                     'duplicated task evidence', 0.58, 'candidate', '2026-01-01'),
                    (13, 100, 10, 1, 'knowledge_enables_skill', 2, 5001, 5002,
                     'duplicated task evidence', 0.62, 'candidate', '2026-01-01');

                CREATE TABLE task_similarity_links (
                    similarity_id INTEGER PRIMARY KEY,
                    source_criteria_id INTEGER NOT NULL,
                    target_criteria_id INTEGER NOT NULL,
                    source_element_id INTEGER NOT NULL,
                    target_element_id INTEGER NOT NULL,
                    source_unit_code TEXT NOT NULL,
                    target_unit_code TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    similarity_score REAL NOT NULL,
                    shared_concept_count INTEGER NOT NULL,
                    source_concept_count INTEGER NOT NULL,
                    target_concept_count INTEGER NOT NULL,
                    source_only_count INTEGER NOT NULL,
                    target_only_count INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO task_similarity_links VALUES
                    (1, 100, 100, 10, 10, '02020101_01v1', '02020101_01v1',
                     'same_task', 1.0, 2, 2, 2, 0, 0, '{"shared":[1,2]}',
                     'human_reviewed', '2026-01-01');

                CREATE TABLE element_criteria_ksa_links (
                    link_id INTEGER PRIMARY KEY,
                    raw_row_id INTEGER NOT NULL,
                    element_id INTEGER NOT NULL,
                    criteria_id INTEGER NOT NULL,
                    ksa_id INTEGER NOT NULL
                );
                INSERT INTO element_criteria_ksa_links VALUES (1, 1, 10, 100, 1000);

                CREATE TABLE quality_issues (
                    issue_id INTEGER PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    issue_detail TEXT NOT NULL,
                    suggested_action TEXT,
                    detected_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                INSERT INTO quality_issues VALUES
                    (1, 'concept', '2', 'needs_review', 'warning', 'Review wording.',
                     'Human review', '2026-01-01', NULL);

                CREATE TABLE ncs_job_base_competencies (
                    job_base_competency_id INTEGER PRIMARY KEY,
                    competency_name TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_job_base_competencies VALUES
                    (1, 'communication', 'communication', '2026-01-01', '2026-01-02');

                CREATE TABLE ncs_job_base_factors (
                    job_base_factor_id INTEGER PRIMARY KEY,
                    job_base_competency_id INTEGER NOT NULL,
                    factor_name TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_job_base_factors VALUES
                    (1, 1, 'listening', 'listening', '2026-01-01', '2026-01-02');

                CREATE TABLE ncs_unit_job_base_links (
                    link_id INTEGER PRIMARY KEY,
                    unit_code TEXT NOT NULL,
                    job_base_competency_id INTEGER NOT NULL,
                    job_base_factor_id INTEGER,
                    source_payload TEXT,
                    link_method TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    api_fetched_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_unit_job_base_links VALUES
                    (1, '02020101_01v1', 1, 1, '{"duplicate":"api"}', 'exact',
                     1.0, 'human_reviewed', '2026-01-01', '2026-01-01', '2026-01-02');

                CREATE TABLE ncs_external_training_zip_courses (
                    external_course_id INTEGER PRIMARY KEY,
                    course_name TEXT,
                    review_status TEXT,
                    source_payload TEXT
                );
                INSERT INTO ncs_external_training_zip_courses VALUES
                    (1, 'external HR course', 'candidate', '{"duplicate":"payload"}');

                CREATE TABLE ncs_occupation_code_mappings (
                    mapping_id INTEGER PRIMARY KEY,
                    unit_code TEXT,
                    occupation_code TEXT,
                    review_status TEXT,
                    source_payload TEXT
                );
                INSERT INTO ncs_occupation_code_mappings VALUES
                    (1, '02020101_01v1', 'HR01', 'human_reviewed', '{"duplicate":"payload"}');

                CREATE TABLE training_transition_gold_scenarios (
                    scenario_id INTEGER PRIMARY KEY,
                    scenario_name TEXT NOT NULL,
                    current_query TEXT NOT NULL,
                    target_query TEXT NOT NULL,
                    major_code TEXT,
                    expected_current_match_text TEXT,
                    expected_target_match_text TEXT,
                    expected_course_names_json TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO training_transition_gold_scenarios VALUES
                    (1, 'HR transition', 'recruiting', 'HR planning', '02', 'recruiting',
                     'HR planning', '["HR planning"]', 'human_reviewed',
                     '2026-01-01', '2026-01-02'),
                    (2, 'Interview transition', 'sourcing', 'interview', '02', 'sourcing',
                     'interview', '["structured interview"]', 'candidate',
                     '2026-01-01', '2026-01-02');

                CREATE TABLE training_transition_scenario_reviews (
                    review_id INTEGER PRIMARY KEY,
                    scenario_id INTEGER NOT NULL,
                    review_method TEXT NOT NULL,
                    source_review_status TEXT,
                    target_review_status TEXT NOT NULL,
                    applied INTEGER NOT NULL,
                    eligible INTEGER NOT NULL,
                    status_updated INTEGER NOT NULL,
                    blockers_json TEXT NOT NULL,
                    criteria_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    expected_course_hits_json TEXT NOT NULL,
                    recommended_courses_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO training_transition_scenario_reviews VALUES
                    (1, 1, 'human', 'candidate', 'human_reviewed', 1, 1, 1,
                     '[]', '{"ok":true}', '{"precision":1}', '["HR planning"]',
                     '["HR planning"]', '2026-01-03');
                """
            )
            conn.commit()

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    def test_complete_profile_preserves_full_counts_and_compact_relation_parity(self) -> None:
        destination = self.root / "vercel-complete.db"
        source_hash = self._sha256(self.source)
        report = serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        )

        self.assertEqual(source_hash, self._sha256(self.source))
        with closing(sqlite3.connect(self.source)) as src, closing(
            sqlite3.connect(destination)
        ) as dst:
            for table in serving_export.VERCEL_ONTOLOGY_COMPLETE_DIRECT_TABLES:
                source_count = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                destination_count = dst.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                if table == "ontology_concept_aliases":
                    self.assertEqual(source_count + 1, destination_count)
                else:
                    self.assertEqual(source_count, destination_count, table)

            source_relation_count = src.execute(
                "SELECT COUNT(*) FROM task_ksa_concept_relations"
            ).fetchone()[0]
            self.assertEqual(
                source_relation_count,
                dst.execute(
                    "SELECT COUNT(*) FROM task_ksa_relations_compact"
                ).fetchone()[0],
            )
            self.assertEqual(
                source_relation_count,
                dst.execute(
                    "SELECT COUNT(*) FROM task_ksa_concept_relations"
                ).fetchone()[0],
            )
            compact_columns = {
                row[1]: row for row in dst.execute(
                    "PRAGMA table_info('task_ksa_relations_compact')"
                ).fetchall()
            }
            self.assertEqual("INTEGER", compact_columns["relation_id"][2])
            self.assertEqual(1, compact_columns["relation_id"][5])
            self.assertEqual(
                src.execute(
                    """
                    SELECT relation_id, criteria_id, source_atomic_id,
                           target_atomic_id, relation_type, confidence_score,
                           review_status
                    FROM task_ksa_concept_relations ORDER BY relation_id
                    """
                ).fetchall(),
                dst.execute(
                    """
                    SELECT relation_id, criteria_id, source_atomic_id,
                           target_atomic_id, relation_type, confidence_score,
                           review_status
                    FROM task_ksa_concept_relations ORDER BY relation_id
                    """
                ).fetchall(),
            )

            self.assertEqual(
                2,
                dst.execute(
                    "SELECT COUNT(*) FROM training_transition_gold_scenarios"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                dst.execute(
                    "SELECT COUNT(*) FROM training_transition_scenario_reviews"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                dst.execute(
                    """
                    SELECT COUNT(*) FROM ncs_training_course_concept_links
                    WHERE link_method = 'unit_ksa_concept_inherited'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                dst.execute(
                    """
                    SELECT COUNT(*) FROM training_goal_concept_links
                    WHERE link_method = 'training_goal_element_implied_concept'
                    """
                ).fetchone()[0],
            )

        self.assertEqual(
            serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
            report["profile"],
        )
        self.assertEqual(
            "task_ksa_relations_compact + compatibility view",
            report["omitted_or_empty_tables"]["replaced"][
                "task_ksa_concept_relations"
            ],
        )

    def test_complete_profile_merges_only_new_human_reviewed_label_aliases(self) -> None:
        destination = self.root / "vercel-complete-labels.db"
        report = serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        )

        with closing(sqlite3.connect(destination)) as conn:
            self.assertEqual(
                [(1, "talent acquisition", "talentacquisition")],
                conn.execute(
                    """
                    SELECT concept_id, alias_text, normalized_alias_key
                    FROM ontology_concept_aliases
                    WHERE alias_source = 'ontology_label_human_reviewed'
                    """
                ).fetchall(),
            )
            self.assertEqual(
                ["human_reviewed", "human_reviewed", "human_reviewed", "llm_reviewed"],
                [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT review_status
                        FROM ontology_concept_label_candidates
                        ORDER BY label_id
                        """
                    ).fetchall()
                ],
            )
            self.assertEqual(
                [(None, None)] * 4,
                conn.execute(
                    """
                    SELECT source_text, evidence_text
                    FROM ontology_concept_label_candidates
                    ORDER BY label_id
                    """
                ).fetchall(),
            )
            self.assertEqual(
                (None, None, "missing", "raw"),
                conn.execute(
                    """
                    SELECT definition, definition_source, definition_status, review_status
                    FROM ontology_concepts WHERE concept_id = 1
                    """
                ).fetchone(),
            )
            self.assertEqual(
                ("A reviewed definition.", "human_review", "defined", "human_reviewed"),
                conn.execute(
                    """
                    SELECT definition, definition_source, definition_status, review_status
                    FROM ontology_concepts WHERE concept_id = 3
                    """
                ).fetchone(),
            )
        self.assertEqual(
            1,
            report["profile_metrics"]["human_reviewed_label_aliases_merged"],
        )

    def test_complete_profile_has_no_default_seed_or_unrelated_table_contamination(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("DROP TABLE ncs_query_aliases")
            conn.commit()
        destination = self.root / "vercel-complete-no-seeds.db"
        serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        )

        with closing(sqlite3.connect(destination)) as conn:
            tables = self._table_names(conn)
            self.assertNotIn("review_audit_log", tables)
            self.assertIn("learning_module_concept_links", tables)
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM learning_module_concept_links"
                ).fetchone()[0],
            )
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM ncs_query_aliases").fetchone()[0])
            self.assertEqual(
                serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
                conn.execute(
                    """
                    SELECT manifest_value FROM serving_snapshot_manifest
                    WHERE manifest_key = 'profile'
                    """
                ).fetchone()[0],
            )

        self.assertIn(
            "learning_module_concept_links",
            serving_export.VERCEL_ONTOLOGY_COMPLETE_EMPTY_COMPATIBILITY_TABLES,
        )

    def test_complete_profile_is_reproducible_and_rejects_include_flags(self) -> None:
        first = self.root / "complete-first.db"
        second = self.root / "complete-second.db"
        serving_export.export_serving_db(
            self.source,
            first,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        )
        serving_export.export_serving_db(
            self.source,
            second,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        )
        self.assertEqual(self._sha256(first), self._sha256(second))

        for flag_name in (
            "include_training_links",
            "include_ontology",
            "include_task_ontology",
        ):
            with self.subTest(flag_name=flag_name):
                with self.assertRaisesRegex(
                    ValueError,
                    "already selects its complete table set",
                ):
                    serving_export.export_serving_db(
                        self.source,
                        self.root / f"invalid-complete-{flag_name}.db",
                        profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPLETE,
                        **{flag_name: True},
                    )


class ExportVercelOntologyCompactDatabaseTests(
    ExportVercelOntologyCompleteDatabaseTests
):
    def setUp(self) -> None:
        super().setUp()
        with closing(sqlite3.connect(self.source)) as conn:
            conn.executescript(
                """
                UPDATE ontology_concept_relations SET review_status = 'candidate';
                UPDATE criteria_concept_links SET link_status = 'raw';
                UPDATE ksa_atomic_items SET review_status = 'raw';
                UPDATE ksa_atomic_concept_links SET link_status = 'raw';

                ALTER TABLE classifications ADD COLUMN major_name TEXT;
                ALTER TABLE classifications ADD COLUMN middle_name TEXT;
                ALTER TABLE classifications ADD COLUMN small_name TEXT;
                ALTER TABLE classifications ADD COLUMN sub_name TEXT;
                UPDATE classifications
                SET major_name = 'business', middle_name = 'HR',
                    small_name = 'HR management', sub_name = 'HR planning';

                UPDATE ksa_items
                SET ksa_type_name = 'knowledge', ksa_text_raw = 'talent acquisition'
                WHERE ksa_id = 1000;
                INSERT INTO ksa_items(ksa_id, element_id, ksa_type_name, ksa_text_raw)
                VALUES (1001, 10, 'skill', 'raw skill phrase');
                UPDATE ontology_concepts
                SET concept_name = 'talent acquisition',
                    normalized_key = 'talentacquisition'
                WHERE concept_id = 1;
                UPDATE ontology_concepts
                SET concept_name = 'behavior interview',
                    normalized_key = 'behaviorinterview'
                WHERE concept_id = 2;
                UPDATE ksa_atomic_items
                SET ksa_id = 1000, element_id = 10, ksa_type_name = 'knowledge',
                    atom_text = 'talent acquisition',
                    normalized_key = 'talentacquisition', split_method = 'rule_based'
                WHERE atomic_id = 5001;
                UPDATE ksa_atomic_items
                SET ksa_id = 1001, element_id = 10, ksa_type_name = 'skill',
                    atom_text = 'behavior interview',
                    normalized_key = 'behaviorinterview', split_method = 'manual_override'
                WHERE atomic_id = 5002;

                DROP TABLE ncs_training_course_concept_links;
                CREATE TABLE ncs_training_course_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER NOT NULL,
                    unit_code TEXT,
                    concept_id INTEGER NOT NULL,
                    link_method TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    evidence_text TEXT,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_training_course_concept_links VALUES
                    (1, 2000, '02020101_01v1', 1, 'unit_ksa_concept_inherited',
                     0.55, 'large inherited evidence', 'auto_linked',
                     '2026-01-01', '2026-01-02'),
                    (2, 2000, '02020101_01v1', 2, 'training_goal_concept_text',
                     1.0, 'large direct evidence\nsecond line "quoted"', 'auto_linked',
                     '2026-01-01', '2026-01-02');

                DROP TABLE ncs_training_course_element_links;
                CREATE TABLE ncs_training_course_element_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER NOT NULL,
                    unit_code TEXT NOT NULL,
                    element_id INTEGER NOT NULL,
                    link_method TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    evidence_text TEXT,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_training_course_element_links VALUES
                    (1, 2000, '02020101_01v1', 10, 'unit_element_coverage',
                     0.8, 'element evidence', 'auto_linked',
                     '2026-01-01', '2026-01-02');

                DROP TABLE training_goal_concept_links;
                CREATE TABLE training_goal_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER NOT NULL,
                    unit_code TEXT,
                    element_id INTEGER,
                    concept_id INTEGER NOT NULL,
                    link_method TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    evidence_text TEXT,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO training_goal_concept_links VALUES
                    (1, 2000, '02020101_01v1', 10, 1,
                     'training_goal_element_implied_concept', 0.6,
                     'goal evidence one', 'auto_linked',
                     '2026-01-01', '2026-01-02'),
                    (2, 2000, '02020101_01v1', NULL, 2,
                     'training_goal_concept_text', 1.0,
                     'goal evidence two', 'auto_linked',
                     '2026-01-01', '2026-01-02');

                DROP TABLE training_delivery_relations;
                CREATE TABLE training_delivery_relations (
                    relation_id INTEGER PRIMARY KEY,
                    training_course_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    relation_value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    numeric_value REAL,
                    evidence_text TEXT,
                    confidence_score REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO training_delivery_relations VALUES
                    (1, 2000, 'uses_facility', 'computer lab', 'computerlab',
                     NULL, 'delivery evidence', 1.0, 'auto_linked',
                     '2026-01-01', '2026-01-02');

                DROP TABLE ncs_unit_job_base_links;
                CREATE TABLE ncs_unit_job_base_links (
                    link_id INTEGER PRIMARY KEY,
                    unit_code TEXT NOT NULL,
                    job_base_competency_id INTEGER NOT NULL,
                    job_base_factor_id INTEGER,
                    ncs_lclas_cd TEXT,
                    ncs_lclas_cdnm TEXT,
                    ncs_mclas_cd TEXT,
                    ncs_mclas_cdnm TEXT,
                    ncs_sclas_cd TEXT,
                    ncs_sclas_cdnm TEXT,
                    ncs_subd_cd TEXT,
                    ncs_subd_cdnm TEXT,
                    compe_unit_name TEXT,
                    link_method TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    source_payload TEXT,
                    api_fetched_at TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO ncs_unit_job_base_links VALUES
                    (1, '02020101_01v1', 1, 1,
                     '02', 'business', '02', 'HR', '01', 'HR management',
                     '01', 'HR planning', 'source-specific unit name',
                     'ncs_cl_cd_exact', 1.0, '{"source":"fixture"}',
                     '2026-01-05', 'auto_linked', '2026-01-01', '2026-01-05');
                """
            )
            conn.commit()

    def test_compact_profile_preserves_evidence_with_lossless_postings(self) -> None:
        destination = self.root / "vercel-compact.db"
        source_hash = self._sha256(self.source)
        report = serving_export.export_serving_db(
            self.source,
            destination,
            profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPACT,
        )

        self.assertEqual(source_hash, self._sha256(self.source))
        self.assertLess(
            report["size_bytes"],
            serving_export.VERCEL_COMPACT_MAX_BYTES,
        )
        with closing(sqlite3.connect(self.source)) as src, closing(
            sqlite3.connect(destination)
        ) as dst:
            object_types = {
                str(row[0]): str(row[1])
                for row in dst.execute(
                    """
                    SELECT name, type FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            self.assertNotIn(
                "task_ksa_concept_relations",
                self._table_names(dst),
            )
            self.assertNotIn("task_similarity_links", self._table_names(dst))
            self.assertNotIn("criteria_concept_links", self._table_names(dst))
            self.assertNotIn("ontology_concept_relations", self._table_names(dst))
            self.assertNotIn("ncs_external_training_zip_courses", object_types)
            self.assertNotIn("ncs_occupation_code_mappings", object_types)
            for view_name in (
                "ksa_atomic_items",
                "ksa_atomic_concept_links",
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
                "ncs_unit_job_base_links",
            ):
                self.assertEqual("view", object_types[view_name], view_name)
                self.assertEqual(
                    [row[1] for row in src.execute(f"PRAGMA table_info('{view_name}')")],
                    [row[1] for row in dst.execute(f"PRAGMA table_info('{view_name}')")],
                    view_name,
                )
            self.assertEqual(
                src.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0],
                dst.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0],
            )
            self.assertEqual(
                src.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0],
                dst.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0],
            )
            self.assertEqual(
                src.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0],
                dst.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0],
            )
            self.assertEqual(
                src.execute(
                    """
                    SELECT atomic_id, ksa_id, element_id, ksa_type_name,
                           atom_index, atom_text, normalized_key, split_method,
                           review_status
                    FROM ksa_atomic_items ORDER BY atomic_id
                    """
                ).fetchall(),
                dst.execute(
                    """
                    SELECT atomic_id, ksa_id, element_id, ksa_type_name,
                           atom_index, atom_text, normalized_key, split_method,
                           review_status
                    FROM ksa_atomic_items ORDER BY atomic_id
                    """
                ).fetchall(),
            )
            self.assertEqual(
                [("raw", "2026-01-01"), ("raw", "2026-01-01")],
                dst.execute(
                    "SELECT review_status, created_at FROM ksa_atomic_items ORDER BY atomic_id"
                ).fetchall(),
            )
            self.assertEqual(
                [("raw", "2026-01-01"), ("raw", "2026-01-01")],
                dst.execute(
                    "SELECT link_status, created_at FROM ksa_atomic_concept_links ORDER BY link_id"
                ).fetchall(),
            )
            self.assertEqual(
                1,
                dst.execute(
                    "SELECT COUNT(atom_text_override) FROM ksa_atomic_facts_compact"
                ).fetchone()[0],
            )
            for table, id_column in (
                ("ncs_training_course_concept_links", "link_id"),
                ("ncs_training_course_element_links", "link_id"),
                ("training_goal_concept_links", "link_id"),
                ("training_delivery_relations", "relation_id"),
            ):
                with self.subTest(training_table=table):
                    self.assertEqual(
                        src.execute(f"SELECT * FROM {table} ORDER BY {id_column}").fetchall(),
                        dst.execute(f"SELECT * FROM {table} ORDER BY {id_column}").fetchall(),
                    )
                    source_text = src.execute(
                        f"SELECT evidence_text, created_at, updated_at "
                        f"FROM {table} ORDER BY {id_column} LIMIT 1"
                    ).fetchone()
                    destination_text = dst.execute(
                        f"SELECT evidence_text, created_at, updated_at "
                        f"FROM {table} ORDER BY {id_column} LIMIT 1"
                    ).fetchone()
                    self.assertEqual(
                        tuple(value.encode("utf-8") for value in source_text),
                        tuple(value.encode("utf-8") for value in destination_text),
                    )
            job_base_serving_columns = (
                "link_id, unit_code, job_base_competency_id, job_base_factor_id, "
                "ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm, "
                "ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm, "
                "compe_unit_name, link_method, confidence_score, review_status"
            )
            self.assertEqual(
                src.execute(
                    f"SELECT {job_base_serving_columns} "
                    "FROM ncs_unit_job_base_links ORDER BY link_id"
                ).fetchall(),
                dst.execute(
                    f"SELECT {job_base_serving_columns} "
                    "FROM ncs_unit_job_base_links ORDER BY link_id"
                ).fetchall(),
            )
            self.assertEqual(
                [(None, None, None, None)],
                dst.execute(
                    """
                    SELECT source_payload, api_fetched_at, created_at, updated_at
                    FROM ncs_unit_job_base_links ORDER BY link_id
                    """
                ).fetchall(),
            )
            self.assertEqual(
                1,
                dst.execute(
                    """
                    SELECT COUNT(*) FROM ncs_unit_job_base_links_compact
                    WHERE override_mask <> 0
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                "auto_linked",
                dst.execute(
                    "SELECT review_status FROM ncs_unit_job_base_links"
                ).fetchone()[0],
            )
            self.assertEqual(
                3,
                dst.execute(
                    """
                    SELECT COUNT(*)
                    FROM ontology_concept_label_candidates
                    WHERE review_status = 'human_reviewed'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                dst.execute(
                    """
                    SELECT COUNT(*)
                    FROM ontology_concept_label_candidates
                    WHERE review_status <> 'human_reviewed'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                dst.execute(
                    "SELECT SUM(target_count) FROM ontology_relation_outgoing"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                dst.execute(
                    "SELECT SUM(source_count) FROM ontology_relation_incoming"
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                dst.execute(
                    "SELECT SUM(concept_count) FROM criteria_concept_forward"
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                dst.execute(
                    "SELECT SUM(criteria_count) FROM criteria_concept_inverse"
                ).fetchone()[0],
            )
            counts = {
                (str(row[2]), str(row[0])): int(row[1])
                for row in dst.execute(
                    """
                    SELECT object_name, row_count, count_kind
                    FROM serving_snapshot_table_counts
                    """
                ).fetchall()
            }
            self.assertEqual(1, counts[("logical", "ontology_concept_relations")])
            self.assertEqual(2, counts[("logical", "criteria_concept_links_enriched")])
            self.assertEqual(2, counts[("physical", "ksa_items")])
            self.assertEqual(2, counts[("physical", "ksa_atomic_facts_compact")])
            self.assertEqual(1, counts[("physical", "ksa_atomic_timestamps")])
            self.assertEqual(6, counts[("physical", "training_link_evidence")])
            self.assertEqual(2, counts[("physical", "training_link_timestamps")])
            self.assertEqual(2, counts[("servable", "ksa_atomic_items")])
            self.assertEqual(
                2,
                counts[("servable", "ncs_training_course_concept_links")],
            )
            self.assertEqual(1, counts[("servable", "ncs_unit_job_base_links")])
            self.assertNotIn(("logical", "ksa_atomic_items"), counts)
            self.assertNotIn(
                ("physical", "job_base_source_payloads"),
                counts,
            )
            self.assertNotIn(
                ("physical", "job_base_link_timestamps"),
                counts,
            )
            view_names = {
                name for name, object_type in object_types.items()
                if object_type == "view"
            }
            indexed_objects = {
                str(row[0])
                for row in dst.execute(
                    "SELECT DISTINCT tbl_name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertFalse(view_names & indexed_objects)
            index_names = {
                str(row[0])
                for row in dst.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertFalse(
                {
                    "idx_serving_ksa_type",
                    "idx_serving_units_name",
                    "idx_serving_alias_unit",
                }
                & index_names
            )
            self.assertIn("idx_serving_ont_concepts_type", index_names)
            self.assertEqual(
                serving_export.VERCEL_COMPACT_SCHEMA,
                dst.execute(
                    """
                    SELECT manifest_value FROM serving_snapshot_manifest
                    WHERE manifest_key = 'schema'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                "source_payload,api_fetched_at,created_at,updated_at",
                dst.execute(
                    """
                    SELECT manifest_value FROM serving_snapshot_manifest
                    WHERE manifest_key = 'job_base_omitted_internal_columns'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                serving_export._stable_ksa_sha256(src),
                serving_export._stable_ksa_sha256(dst),
            )

        self.assertEqual(
            1,
            report["profile_metrics"]["logical_counts"][
                "ontology_concept_relations"
            ],
        )
        self.assertEqual(
            2,
            report["profile_metrics"]["logical_counts"][
                "criteria_concept_links_enriched"
            ],
        )
        self.assertIn(
            "ncs_external_training_zip_courses",
            report["omitted_or_empty_tables"]["omitted"],
        )
        self.assertIn(
            "ncs_occupation_code_mappings",
            report["omitted_or_empty_tables"]["omitted"],
        )
        self.assertEqual(
            ["source_payload", "api_fetched_at", "created_at", "updated_at"],
            report["profile_metrics"]["job_base_compaction"][
                "omitted_internal_columns"
            ],
        )

    def test_compact_profile_refuses_to_collapse_review_states(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "UPDATE ontology_concept_relations SET review_status = 'human_reviewed'"
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "candidate-only"):
            serving_export.export_serving_db(
                self.source,
                self.root / "invalid-review-collapse.db",
                profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPACT,
            )

    def test_compact_profile_refuses_unsafe_atomic_state_and_derivation(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "UPDATE ksa_atomic_items SET review_status = 'human_reviewed' "
                "WHERE atomic_id = 5001"
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "raw-only source states"):
            serving_export.export_serving_db(
                self.source,
                self.root / "invalid-atomic-status.db",
                profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPACT,
            )

        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "UPDATE ksa_atomic_items SET review_status = 'raw', "
                "element_id = 999 WHERE atomic_id = 5001"
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "atomic derivation guard"):
            serving_export.export_serving_db(
                self.source,
                self.root / "invalid-atomic-derivation.db",
                profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPACT,
            )

    def test_compact_profile_refuses_orphaned_job_base_reference(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "UPDATE ncs_unit_job_base_links SET unit_code = 'missing-unit'"
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "job-base reconstruction guard"):
            serving_export.export_serving_db(
                self.source,
                self.root / "invalid-job-base-reference.db",
                profile=serving_export.PROFILE_VERCEL_ONTOLOGY_COMPACT,
            )


if __name__ == "__main__":
    unittest.main()
