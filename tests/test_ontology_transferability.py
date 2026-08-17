from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.ontology_transferability import (
    MAJOR_RUN_SCHEMA_VERSION,
    build_ontology_adjusted_education_systems,
    build_ontology_transferability_artifact_audit,
    build_ontology_transferability_calibration,
    build_ontology_transferability_course_link_candidate_review,
    build_ontology_transferability_course_link_gap_diagnostic,
    build_ontology_transferability_education_system_audit,
    build_ontology_transferability_field_review,
    build_ontology_transferability_method_work_queue,
    build_ontology_transferability_release_gate,
    build_ontology_transferability_review_seedpack,
    build_ontology_transferability_spotcheck_plan,
    write_ontology_transferability_json,
    write_ontology_transferability_pairs_csv,
    write_ontology_transferability_review_seedpack_jsonl,
)


class OntologyTransferabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT,
                major_name TEXT,
                middle_code TEXT,
                middle_name TEXT,
                small_code TEXT,
                small_name TEXT,
                sub_code TEXT,
                sub_name TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                unit_level_raw TEXT,
                classification_id INTEGER
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY,
                unit_code TEXT
            );
            CREATE TABLE ksa_items (
                ksa_id INTEGER PRIMARY KEY,
                element_id INTEGER
            );
            CREATE TABLE ksa_concept_links (
                link_id INTEGER PRIMARY KEY,
                ksa_id INTEGER,
                concept_id INTEGER
            );
            CREATE TABLE ksa_atomic_items (
                atomic_id INTEGER PRIMARY KEY,
                element_id INTEGER
            );
            CREATE TABLE ksa_atomic_concept_links (
                link_id INTEGER PRIMARY KEY,
                atomic_id INTEGER,
                concept_id INTEGER
            );
            CREATE TABLE ontology_concept_relations (
                relation_id INTEGER PRIMARY KEY,
                source_concept_id INTEGER,
                target_concept_id INTEGER,
                review_status TEXT
            );
            CREATE TABLE task_similarity_links (
                similarity_id INTEGER PRIMARY KEY,
                source_unit_code TEXT,
                target_unit_code TEXT,
                similarity_score REAL,
                shared_concept_count INTEGER,
                review_status TEXT
            );
            CREATE TABLE ncs_unit_job_base_links (
                link_id INTEGER PRIMARY KEY,
                unit_code TEXT,
                job_base_competency_id INTEGER,
                job_base_factor_id INTEGER
            );
            CREATE TABLE ncs_unit_qualification_links (
                link_id INTEGER PRIMARY KEY,
                unit_code TEXT,
                jm_cd TEXT,
                organ_std_ver_cd TEXT,
                ablt_unit_typ_cd TEXT,
                min_edu_trng_tm TEXT
            );
            CREATE TABLE ncs_training_courses (
                training_course_id INTEGER PRIMARY KEY,
                compe_unit_name TEXT,
                train_time TEXT,
                meth_name TEXT,
                fac_name TEXT
            );
            CREATE TABLE ncs_training_course_unit_links (
                link_id INTEGER PRIMARY KEY,
                training_course_id INTEGER,
                unit_code TEXT,
                review_status TEXT
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO classifications
            VALUES (1, '99', 'Test Major', '01', 'Test Middle', '01', 'Test Small', '01', 'Test Sub')
            """
        )
        self.conn.executemany(
            "INSERT INTO competency_units VALUES (?, ?, ?, ?)",
            [
                ("U1", "Planning", "5", 1),
                ("U2", "Execution", "4", 1),
                ("U3", "Audit", "4", 1),
            ],
        )
        self.conn.executemany(
            "INSERT INTO competency_elements VALUES (?, ?)",
            [(1, "U1"), (2, "U2"), (3, "U3")],
        )
        self.conn.executemany(
            "INSERT INTO ksa_items VALUES (?, ?)",
            [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3)],
        )
        self.conn.executemany(
            "INSERT INTO ksa_concept_links VALUES (?, ?, ?)",
            [
                (1, 1, 100),
                (2, 2, 101),
                (3, 3, 100),
                (4, 4, 102),
                (5, 5, 103),
            ],
        )
        self.conn.executemany(
            "INSERT INTO ontology_concept_relations VALUES (?, ?, ?, ?)",
            [(1, 101, 102, "auto_linked"), (2, 103, 100, "rejected")],
        )
        self.conn.executemany(
            "INSERT INTO task_similarity_links VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "U1", "U2", 0.5, 1, "auto_linked"),
                (2, "U3", "U1", 1.0, 1, "auto_linked"),
                (3, "U3", "U2", 1.0, 1, "auto_linked"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO ncs_unit_job_base_links VALUES (?, ?, ?, ?)",
            [(1, "U1", 1, 10), (2, "U2", 1, 10), (3, "U3", 1, 10)],
        )
        self.conn.executemany(
            "INSERT INTO ncs_training_courses VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Execution Course", "8", "Classroom", "Room"),
                (2, "Audit Course", "6", "Classroom", "Room"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO ncs_training_course_unit_links VALUES (?, ?, ?, ?)",
            [(1, 1, "U2", "reviewed"), (2, 2, "U3", "reviewed")],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_builds_scope_matrix_with_adjusted_transferability(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )

        self.assertEqual(report["schema"], "ncs_ontology_adjusted_education_systems_v1")
        self.assertEqual(report["summary"]["scope_count"], 1)
        scope = report["scopes"][0]
        self.assertEqual(scope["directed_pair_count"], 6)
        self.assertEqual(len(scope["education_system"]["training_system_matrix"]), 3)

        rows = {row["unit_code"]: row for row in scope["education_system"]["training_system_matrix"]}
        self.assertEqual(rows["U2"]["course_link"]["linked_training_course_count"], 1)
        self.assertEqual(rows["U2"]["human_review"]["status"], "needs_review")
        self.assertEqual(rows["U3"]["required_optional_basis"]["code"], "recommended")
        self.assertIn("baseline_heavy", rows["U3"]["human_review"]["flags"])
        self.assertIn(
            "caveats",
            rows["U3"]["required_optional_basis"]["basis"],
        )
        path_by_role = {
            stage["role"]: stage
            for stage in scope["education_system"]["recommended_path"]
        }
        self.assertEqual(path_by_role["core_gap_training"]["unit_codes"], ["U2"])
        self.assertIn("U3", path_by_role["supporting_or_adjacent_training"]["unit_codes"])
        self.assertIn("U1", path_by_role["delivery_fit_review"]["unit_codes"])
        self.assertIn("baseline_dependency_ratio", rows["U1"]["task_ksa_basis"])
        self.assertIn("baseline_heavy_pair_ratio", scope["score_summary"])
        self.assertIn(
            "ontology_related_concepts",
            rows["U1"]["task_ksa_basis"]["basis_types"],
        )
        for row in rows.values():
            if row["required_optional_basis"]["code"] != "required":
                continue
            self.assertGreater(row["task_ksa_basis"]["average_exact_ksa_overlap"], 0.03)
            self.assertLess(row["task_ksa_basis"]["baseline_dependency_ratio"], 0.9)
            self.assertGreater(row["course_link"]["linked_training_course_count"], 0)
        self.assertGreater(
            scope["top_undirected_pairs"][0]["mean_adjusted"],
            scope["top_undirected_pairs"][-1]["mean_adjusted"],
        )

    def test_education_system_audit_summarizes_guide_readiness_without_approval(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            write_ontology_transferability_json(report, report_path)
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(report_path),
                                "summary": report["summary"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            audit = build_ontology_transferability_education_system_audit(run_path)

        self.assertEqual(
            audit["schema"],
            "ncs_ontology_transferability_education_system_audit_v1",
        )
        self.assertEqual(audit["source_run_resolved"], run_path.name)
        self.assertNotIn("resolved_path", audit["source_run_fingerprint"])
        self.assertEqual(audit["source_run_fingerprint"]["path"], run_path.name)
        self.assertFalse(audit["ok"])
        self.assertTrue(audit["contract_ok"])
        self.assertFalse(audit["approval_ready"])
        self.assertEqual(audit["status"], "review_required")
        aggregate = audit["aggregate"]
        self.assertEqual(aggregate["major_count"], 1)
        self.assertEqual(aggregate["scope_count"], 1)
        self.assertEqual(aggregate["matrix_row_count"], 3)
        self.assertEqual(aggregate["matrix_rows_with_course_links"], 2)
        self.assertEqual(aggregate["matrix_rows_without_course_links"], 1)
        self.assertEqual(aggregate["rows_requiring_human_review"], 3)
        self.assertFalse(aggregate["approval_claim"])
        self.assertFalse(aggregate["db_writes"])
        self.assertEqual(aggregate["guide_role"], "framework_reference")
        self.assertEqual(aggregate["unsafe_review_status_count"], 0)
        self.assertEqual(aggregate["invalid_review_status_count"], 0)
        self.assertGreaterEqual((aggregate["guide_stage_counts"] or {}).get("C1-1", 0), 1)
        self.assertGreaterEqual((aggregate["guide_stage_counts"] or {}).get("C1-2", 0), 1)
        self.assertEqual(audit["guide_alignment"]["C2-1"]["row_count"], 3)
        self.assertEqual(audit["guide_alignment"]["C2-2"]["delivery_rows"], 3)
        self.assertEqual(audit["review_gate"]["status"], "open")
        self.assertFalse(audit["review_gate"]["approval_claim"])
        self.assertIn(
            "Report-only audit",
            audit["non_mutation_note"],
        )
        self.assertTrue(audit["priority_scopes"])

    def test_education_system_audit_rejects_missing_review_gate_status(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )
        report["scopes"][0]["education_system"]["training_system_matrix"][0]["human_review"].pop("status")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            write_ontology_transferability_json(report, report_path)
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(report_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            audit = build_ontology_transferability_education_system_audit(run_path)

        self.assertFalse(audit["ok"])
        self.assertFalse(audit["contract_ok"])
        self.assertFalse(audit["approval_ready"])
        self.assertEqual(audit["status"], "contract_failed")
        self.assertEqual(audit["aggregate"]["invalid_review_status_count"], 1)
        self.assertIn("invalid_human_review_status", {item["code"] for item in audit["findings"]})

    def test_education_system_audit_rejects_wrong_major_artifact_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema": "wrong_schema",
                        "summary": {"scope_count": 1},
                        "scopes": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(report_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            audit = build_ontology_transferability_education_system_audit(run_path)

        self.assertFalse(audit["ok"])
        self.assertFalse(audit["contract_ok"])
        self.assertEqual(audit["status"], "contract_failed")
        self.assertEqual(audit["failed_major_count"], 1)
        self.assertIn("major_artifact_schema_mismatch", {item["code"] for item in audit["findings"]})

    def test_course_link_gap_diagnostic_finds_unlinked_exact_course_names(self) -> None:
        self.conn.execute(
            "INSERT INTO ncs_training_courses VALUES (?, ?, ?, ?, ?)",
            (3, "Unlinked Unit", "4", "Classroom", "Lab"),
        )
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cd TEXT")
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cdnm TEXT")
        self.conn.execute(
            "UPDATE ncs_training_courses SET ncs_lclas_cd = ?, ncs_lclas_cdnm = ? WHERE training_course_id = ?",
            ("15", "Machinery", 3),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            major_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            major_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ontology_adjusted_education_systems_v1",
                        "summary": {"scope_count": 1},
                        "scopes": [
                            {
                                "scope_label": "Test Major > Gap Scope",
                                "classification": {
                                    "classification_id": 2,
                                    "major_code": "99",
                                    "major_name": "Test Major",
                                },
                                "score_summary": {
                                    "avg_adjusted": 0.42,
                                    "avg_exact": 0.01,
                                    "baseline_heavy_pair_ratio": 0.3,
                                },
                                "education_system": {
                                    "training_system_matrix": [
                                        {
                                            "unit_code": "U9",
                                            "unit_name": "Unlinked Unit",
                                            "course_link": {
                                                "linked_training_course_count": 0,
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(major_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            diagnostic = build_ontology_transferability_course_link_gap_diagnostic(
                self.conn,
                run_path,
            )
            diagnostic_path = tmp_path / "course_gap.json"
            diagnostic_path.write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            candidate_review = build_ontology_transferability_course_link_candidate_review(
                self.conn,
                diagnostic_path,
            )

        self.assertTrue(diagnostic["ok"])
        self.assertEqual(diagnostic["scope_count"], 1)
        self.assertEqual(
            diagnostic["issue_type_counts"],
            {"cross_scope_name_only": 1},
        )
        scope = diagnostic["scopes"][0]
        self.assertIn("no_direct_training_course_links_in_matrix", scope["flags"])
        self.assertEqual(scope["issue_type"], "cross_scope_name_only")
        self.assertEqual(scope["cross_scope_course_name_hits"][0]["unit_name"], "Unlinked Unit")
        self.assertTrue(candidate_review["ok"])
        self.assertTrue(candidate_review["contract_ok"])
        self.assertFalse(candidate_review["approval_ready"])
        self.assertEqual(candidate_review["status"], "no_candidates")
        self.assertFalse(candidate_review["human_review_required"])
        self.assertEqual(candidate_review["schema"], "ncs_training_course_link_candidate_review_v1")
        self.assertEqual(candidate_review["scope_count"], 0)
        self.assertEqual(candidate_review["unit_candidate_count"], 0)
        self.assertEqual(candidate_review["course_candidate_count"], 0)
        self.assertEqual(candidate_review["scope_fit_status_counts"], {})
        self.assertEqual(
            candidate_review["non_mutation_note"],
            "Report-only candidate review. No training-course links, review statuses, source KSA text, or ontology definitions were modified.",
        )

    def test_course_link_candidate_review_keeps_same_major_exact_candidates(self) -> None:
        self.conn.execute(
            "INSERT INTO ncs_training_courses VALUES (?, ?, ?, ?, ?)",
            (3, "Unlinked Unit", "4", "Classroom", "Lab"),
        )
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cd TEXT")
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cdnm TEXT")
        self.conn.execute(
            "UPDATE ncs_training_courses SET ncs_lclas_cd = ?, ncs_lclas_cdnm = ? WHERE training_course_id = ?",
            ("99", "Test Major", 3),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            major_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            major_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ontology_adjusted_education_systems_v1",
                        "summary": {"scope_count": 1},
                        "scopes": [
                            {
                                "scope_label": "Test Major > Gap Scope",
                                "classification": {
                                    "classification_id": 2,
                                    "major_code": "99",
                                    "major_name": "Test Major",
                                },
                                "score_summary": {
                                    "avg_adjusted": 0.42,
                                    "avg_exact": 0.01,
                                    "baseline_heavy_pair_ratio": 0.3,
                                },
                                "education_system": {
                                    "training_system_matrix": [
                                        {
                                            "unit_code": "U9",
                                            "unit_name": "Unlinked Unit",
                                            "course_link": {
                                                "linked_training_course_count": 0,
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(major_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            diagnostic = build_ontology_transferability_course_link_gap_diagnostic(
                self.conn,
                run_path,
            )
            diagnostic_path = tmp_path / "course_gap.json"
            diagnostic_path.write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            candidate_review = build_ontology_transferability_course_link_candidate_review(
                self.conn,
                diagnostic_path,
            )

        self.assertEqual(diagnostic["issue_type_counts"], {"possible_unit_link_gap": 1})
        self.assertEqual(diagnostic["scopes"][0]["exact_course_name_hits"][0]["unit_name"], "Unlinked Unit")
        self.assertTrue(candidate_review["contract_ok"])
        self.assertFalse(candidate_review["approval_ready"])
        self.assertEqual(candidate_review["status"], "review_required")
        self.assertTrue(candidate_review["human_review_required"])
        self.assertFalse(candidate_review["approval_claim"])
        self.assertFalse(candidate_review["db_writes"])
        self.assertEqual(candidate_review["review_gate"]["status"], "open")
        self.assertEqual(candidate_review["scope_count"], 1)
        self.assertEqual(candidate_review["unit_candidate_count"], 1)
        self.assertEqual(candidate_review["course_candidate_count"], 1)
        candidate_unit = candidate_review["scopes"][0]["unit_candidates"][0]
        self.assertEqual(candidate_unit["unit_name"], "Unlinked Unit")
        candidate_group = candidate_unit["candidate_groups"][0]
        self.assertEqual(candidate_group["candidate_type"], "unit_name_exact")
        self.assertEqual(candidate_group["courses"][0]["training_course_id"], 3)
        self.assertEqual(candidate_group["courses"][0]["scope_fit"]["status"], "same_major_only")
        self.assertEqual(candidate_review["scope_fit_status_counts"], {"same_major_only": 1})

    def test_course_link_gap_diagnostic_finds_partial_matrix_course_gaps(self) -> None:
        self.conn.execute(
            "INSERT INTO ncs_training_courses VALUES (?, ?, ?, ?, ?)",
            (3, "Unlinked Unit", "4", "Classroom", "Lab"),
        )
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cd TEXT")
        self.conn.execute("ALTER TABLE ncs_training_courses ADD COLUMN ncs_lclas_cdnm TEXT")
        self.conn.execute(
            "UPDATE ncs_training_courses SET ncs_lclas_cd = ?, ncs_lclas_cdnm = ? WHERE training_course_id = ?",
            ("99", "Test Major", 3),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            major_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            major_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ontology_adjusted_education_systems_v1",
                        "summary": {"scope_count": 1},
                        "scopes": [
                            {
                                "scope_label": "Test Major > Partial Gap Scope",
                                "classification": {
                                    "classification_id": 2,
                                    "major_code": "99",
                                    "major_name": "Test Major",
                                },
                                "score_summary": {
                                    "avg_adjusted": 0.42,
                                    "avg_exact": 0.01,
                                    "baseline_heavy_pair_ratio": 0.1,
                                },
                                "education_system": {
                                    "training_system_matrix": [
                                        {
                                            "unit_code": "U2",
                                            "unit_name": "Execution",
                                            "course_link": {
                                                "linked_training_course_count": 1,
                                            },
                                        },
                                        {
                                            "unit_code": "U9",
                                            "unit_name": "Unlinked Unit",
                                            "course_link": {
                                                "linked_training_course_count": 0,
                                            },
                                        },
                                    ]
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(major_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            diagnostic = build_ontology_transferability_course_link_gap_diagnostic(
                self.conn,
                run_path,
            )

        self.assertTrue(diagnostic["ok"])
        self.assertEqual(diagnostic["scope_count"], 1)
        scope = diagnostic["scopes"][0]
        self.assertEqual(scope["linked_unit_count"], 1)
        self.assertEqual(scope["unlinked_unit_count"], 1)
        self.assertIn("partial_training_course_link_gap", scope["flags"])
        self.assertNotIn("no_direct_training_course_links_in_matrix", scope["flags"])
        self.assertEqual(scope["issue_type"], "possible_unit_link_gap")
        self.assertEqual(scope["sample_units"], [{"unit_code": "U9", "unit_name": "Unlinked Unit"}])

    def test_artifact_audit_rejects_core_path_with_non_required_rows(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )
        scope = report["scopes"][0]
        path_by_role = {
            stage["role"]: stage
            for stage in scope["education_system"]["recommended_path"]
        }
        path_by_role["core_gap_training"]["units"].append("Planning")
        path_by_role["core_gap_training"].setdefault("unit_codes", []).append("U1")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            write_ontology_transferability_json(report, report_path)
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "json_path": str(report_path),
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            audit = build_ontology_transferability_artifact_audit(run_path)

        self.assertFalse(audit["ok"])
        self.assertIn(
            "recommended_path_required_optional_mismatch",
            {issue["code"] for issue in audit["issues"]},
        )

    def test_field_review_and_seedpack_from_major_run(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            run_path = tmp_path / "major_run.json"
            seedpack_path = tmp_path / "seedpack.jsonl"
            calibration_path = tmp_path / "calibration.json"
            spotcheck_path = tmp_path / "spotcheck.json"
            course_gap_path = tmp_path / "course_gap.json"
            method_queue_path = tmp_path / "method_queue.json"
            audit_path = tmp_path / "artifact_audit.json"
            write_ontology_transferability_json(report, report_path)
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": True,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Test Major",
                                "returncode": 0,
                                "seconds": 0.1,
                                "json_path": str(report_path),
                                "summary": report["summary"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            field_review = build_ontology_transferability_field_review(run_path)
            seedpack = build_ontology_transferability_review_seedpack(run_path)
            calibration = build_ontology_transferability_calibration(run_path)
            calibration_path.write_text(
                json.dumps(calibration, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            write_ontology_transferability_review_seedpack_jsonl(seedpack, seedpack_path)
            spotcheck = build_ontology_transferability_spotcheck_plan(seedpack_path)
            spotcheck_path.write_text(
                json.dumps(spotcheck, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            course_gap = build_ontology_transferability_course_link_gap_diagnostic(
                self.conn,
                run_path,
            )
            course_gap_path.write_text(
                json.dumps(course_gap, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            method_queue = build_ontology_transferability_method_work_queue(
                calibration_path,
                seedpack_path,
                course_link_gap_diagnostic_path=course_gap_path,
            )
            method_queue_path.write_text(
                json.dumps(method_queue, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            method_queue_with_missing_optional = build_ontology_transferability_method_work_queue(
                calibration_path,
                seedpack_path,
                field_review_path=tmp_path / "missing_field_review.json",
            )
            audit = build_ontology_transferability_artifact_audit(
                run_path,
                seedpack_path=seedpack_path,
                spotcheck_plan_path=spotcheck_path,
                method_work_queue_path=method_queue_path,
            )
            audit_path.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            release_gate = build_ontology_transferability_release_gate(
                calibration_path,
                method_queue_path,
            )
            release_gate_with_audit = build_ontology_transferability_release_gate(
                calibration_path,
                method_queue_path,
                artifact_audit_path=audit_path,
            )
            records = [
                json.loads(line)
                for line in seedpack_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(field_review["ok"])
        self.assertEqual(field_review["major_count"], 1)
        self.assertEqual(field_review["totals"]["scope_count"], 1)
        self.assertEqual(seedpack["schema"], "ncs_ontology_transferability_review_seedpack_v1")
        self.assertGreaterEqual(seedpack["seed_count"], 2)
        self.assertEqual(calibration["schema"], "ncs_ontology_transferability_calibration_v1")
        self.assertEqual(calibration["totals"]["scope_count"], 1)
        self.assertIn("provisional_policy", calibration)
        self.assertEqual(spotcheck["schema"], "ncs_ontology_transferability_spotcheck_plan_v1")
        self.assertEqual(spotcheck["spotcheck_count"], seedpack["seed_count"])
        self.assertIn("search_queries", spotcheck["items"][0])
        self.assertEqual(method_queue["schema"], "ncs_ontology_transferability_method_work_queue_v1")
        self.assertGreater(method_queue["queue_count"], 0)
        self.assertFalse(
            any(
                item.get("track") == "role_overlay" and item.get("major_code") == "02"
                for item in method_queue["queue_items"]
            )
        )
        self.assertFalse(method_queue_with_missing_optional["ok"])
        self.assertEqual(
            method_queue_with_missing_optional["validation_issues"][0]["code"],
            "field_review_missing",
        )
        self.assertEqual(
            method_queue["seedpack_contract"]["seedpack_id"],
            seedpack["seedpack_id"],
        )
        self.assertIn(
            "course_link_gap_diagnostic",
            method_queue["source_artifacts"],
        )
        self.assertGreaterEqual(
            method_queue["course_link_gap_diagnostic_summary"]["scope_count"],
            1,
        )
        self.assertTrue(
            any(
                item.get("track") == "training_course_link_gap_diagnostic"
                for item in method_queue["queue_items"]
            )
        )
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["issue_count"], 0)
        self.assertFalse(release_gate["ok"])
        self.assertEqual(release_gate["status"], "blocked")
        self.assertIn(
            "artifact_audit_required",
            {check["code"] for check in release_gate["checks"] if check["status"] == "fail"},
        )
        self.assertNotIn(
            "artifact_audit_required",
            {check["code"] for check in release_gate_with_audit["checks"]},
        )
        self.assertFalse(release_gate_with_audit["ok"])
        self.assertEqual(release_gate_with_audit["status"], "blocked")
        self.assertEqual(audit["counts"]["seed_count"], seedpack["seed_count"])
        self.assertEqual(audit["counts"]["method_queue_count"], method_queue["queue_count"])
        self.assertEqual(records[0]["record_type"], "batch")
        self.assertIn("source_run_fingerprint", records[0])
        self.assertEqual(records[0]["allowed_decisions"], ["approve", "reject", "defer"])
        self.assertEqual(records[1]["decision"], "")
        self.assertEqual(records[1]["schema"], "ncs_ontology_transferability_review_seed_v1")
        self.assertEqual(records[1]["record_type"], "ontology_transferability_review_item")

    def test_rejects_empty_or_malformed_major_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            empty_run = tmp_path / "empty_run.json"
            bad_schema = tmp_path / "bad_schema.json"
            empty_run.write_text(
                json.dumps({"schema": MAJOR_RUN_SCHEMA_VERSION, "results": []}),
                encoding="utf-8",
            )
            bad_schema.write_text(
                json.dumps({"schema": "wrong", "results": [{"major_code": "99"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "non-empty results"):
                build_ontology_transferability_field_review(empty_run)
            with self.assertRaisesRegex(ValueError, "Invalid ontology transferability run schema"):
                build_ontology_transferability_review_seedpack(bad_schema)

    def test_partial_major_run_is_not_ok_for_review_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_path = tmp_path / "partial_run.json"
            run_path.write_text(
                json.dumps(
                    {
                        "schema": MAJOR_RUN_SCHEMA_VERSION,
                        "ok": False,
                        "results": [
                            {
                                "major_code": "99",
                                "major_name": "Missing Major",
                                "returncode": 1,
                                "json_path": str(tmp_path / "missing.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            field_review = build_ontology_transferability_field_review(run_path)
            seedpack = build_ontology_transferability_review_seedpack(run_path)
            calibration = build_ontology_transferability_calibration(run_path)

        self.assertFalse(field_review["ok"])
        self.assertFalse(seedpack["ok"])
        self.assertFalse(calibration["ok"])
        self.assertEqual(seedpack["failed_major_count"], 1)
        self.assertEqual(calibration["failed_major_count"], 1)

    def test_artifact_audit_rejects_same_basename_different_run_seedpack(self) -> None:
        report = build_ontology_adjusted_education_systems(
            self.conn,
            major_code="99",
            top_pairs=5,
            top_units=5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "major99.json"
            write_ontology_transferability_json(report, report_path)

            run_a_dir = tmp_path / "a"
            run_b_dir = tmp_path / "b"
            run_a_dir.mkdir()
            run_b_dir.mkdir()
            run_a = run_a_dir / "major_run.json"
            run_b = run_b_dir / "major_run.json"
            base_manifest = {
                "schema": MAJOR_RUN_SCHEMA_VERSION,
                "ok": True,
                "results": [
                    {
                        "major_code": "99",
                        "major_name": "Test Major",
                        "returncode": 0,
                        "seconds": 0.1,
                        "json_path": str(report_path),
                        "summary": report["summary"],
                    }
                ],
            }
            run_a.write_text(
                json.dumps({**base_manifest, "manifest_note": "a"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            run_b.write_text(
                json.dumps({**base_manifest, "manifest_note": "b"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            seedpack = build_ontology_transferability_review_seedpack(run_b)
            seedpack_path = tmp_path / "seedpack_from_b.jsonl"
            write_ontology_transferability_review_seedpack_jsonl(seedpack, seedpack_path)

            audit = build_ontology_transferability_artifact_audit(
                run_a,
                seedpack_path=seedpack_path,
            )

        self.assertFalse(audit["ok"])
        issue_codes = {issue["code"] for issue in audit["issues"]}
        self.assertIn("seedpack_source_run_mismatch", issue_codes)
        self.assertIn("seedpack_source_run_fingerprint_mismatch", issue_codes)

    def test_transferability_pairs_csv_escapes_formula_like_cells(self) -> None:
        report = {
            "scopes": [
                {
                    "scope_label": "=scope",
                    "classification": {"classification_id": "+classification"},
                    "education_system": {
                        "training_system_matrix": [
                            {"unit_code": "=A", "unit_name": "@source"},
                            {"unit_code": "B", "unit_name": "\t-target"},
                        ]
                    },
                    "top_undirected_pairs": [
                        {
                            "unit_a_code": "=A",
                            "unit_b_code": "B",
                            "mean_adjusted": "=1+1",
                            "mean_exact": "+2",
                            "mean_adjusted_minus_exact": "-3",
                            "mean_baseline_dependency_ratio": "@cmd",
                        }
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "pairs.csv"
            write_ontology_transferability_pairs_csv(report, csv_path)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["scope_label"], "'=scope")
        self.assertEqual(row["classification_id"], "'+classification")
        self.assertEqual(row["source_unit_code"], "'=A")
        self.assertEqual(row["source_unit_name"], "'@source")
        self.assertEqual(row["target_unit_name"], "'\t-target")
        self.assertEqual(row["ontology_adjusted_transferability_ratio"], "'=1+1")


if __name__ == "__main__":
    unittest.main()
