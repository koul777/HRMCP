from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.ontology_refresh_builder import _select_strategy, build_ontology_refresh


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OntologyRefreshBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _db(self, name: str, *, source_row: bool = True) -> Path:
        path = self.root / name
        conn = connect(path)
        initialize_database(conn)
        if source_row:
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('01','major','01','middle','01','small','01','sub')
                """
            )
            classification_id = int(
                conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES ('U1','U1','v1','unit one','3',?,'now','now')
                """,
                (classification_id,),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('U1','1','E1','element one','3')
                """
            )
            element_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO performance_criteria(element_id,criteria_no,criteria_text_raw) VALUES (?,'1','criterion')",
                (element_id,),
            )
            criteria_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?,'K','지식','1','source KSA')
                """,
                (element_id,),
            )
            ksa_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO raw_excel_rows(
                    raw_row_id,source_file,sheet_name,sheet_row_number,
                    major_code,major_name,middle_code,middle_name,small_code,small_name,
                    sub_code,sub_name,unit_code,unit_name,unit_level,element_code,
                    element_name,element_level,criteria_no,criteria_text,ksa_type_code,
                    ksa_type_name,ksa_no,ksa_text,loaded_at
                ) VALUES (
                    1,'test','sheet',1,'01','major','01','middle','01','small',
                    '01','sub','U1','unit one','3','E1','element one','3','1',
                    'criterion','K','지식','1','source KSA','now'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO element_criteria_ksa_links(raw_row_id,element_id,criteria_id,ksa_id)
                VALUES (1,?,?,?)
                """,
                (element_id, criteria_id, ksa_id),
            )
        conn.commit()
        conn.close()
        return path

    def _append_ksa(self, path: Path, text: str = "new skill") -> None:
        with closing(connect(path)) as conn:
            element_id = int(
                conn.execute(
                    "SELECT element_id FROM competency_elements LIMIT 1"
                ).fetchone()[0]
            )
            criteria_id = int(
                conn.execute(
                    "SELECT criteria_id FROM performance_criteria LIMIT 1"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO ksa_items(element_id,ksa_type_code,ksa_type_name,ksa_no,ksa_text_raw)
                VALUES (?,'S','기술','2',?)
                """,
                (element_id, text),
            )
            ksa_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO raw_excel_rows(
                    raw_row_id,source_file,sheet_name,sheet_row_number,
                    major_code,major_name,middle_code,middle_name,small_code,small_name,
                    sub_code,sub_name,unit_code,unit_name,unit_level,element_code,
                    element_name,element_level,criteria_no,criteria_text,ksa_type_code,
                    ksa_type_name,ksa_no,ksa_text,loaded_at
                ) VALUES (
                    2,'test','sheet',2,'01','major','01','middle','01','small',
                    '01','sub','U1','unit one','3','E1','element one','3','1',
                    'criterion','S','기술','2',?,'now'
                )
                """,
                (text,),
            )
            conn.execute(
                """
                INSERT INTO element_criteria_ksa_links(raw_row_id,element_id,criteria_id,ksa_id)
                VALUES (2,?,?,?)
                """,
                (element_id, criteria_id, ksa_id),
            )
            conn.commit()

    def _prepared_baseline(
        self, source_name: str, baseline_name: str
    ) -> tuple[Path, Path]:
        source = self._db(source_name)
        baseline = self.root / baseline_name
        report = build_ontology_refresh(
            source,
            state_dir=self.root / f"state-{baseline_name}",
            prepared_output=baseline,
            apply=True,
        )
        self.assertTrue(report["ok"])
        self.assertTrue(report["validation"]["ok"])
        return source, baseline

    def test_plan_only_bootstrap_does_not_create_output(self) -> None:
        candidate = self._db("candidate.db")
        output = self.root / "prepared.db"
        before = _sha256(candidate)

        report = build_ontology_refresh(
            candidate,
            state_dir=self.root / "state",
            prepared_output=output,
        )

        self.assertEqual(report["selected_strategy"], "bootstrap_additive_build")
        self.assertEqual(report["status"], "planned")
        self.assertIsNone(report["next_publisher_command"])
        self.assertFalse(output.exists())
        self.assertEqual(_sha256(candidate), before)

    def test_bootstrap_apply_builds_prepared_copy_without_mutating_source(self) -> None:
        candidate = self._db("candidate.db")
        output = self.root / "prepared.db"
        before = _sha256(candidate)

        report = build_ontology_refresh(
            candidate,
            state_dir=self.root / "state",
            prepared_output=output,
            apply=True,
        )

        self.assertEqual(report["status"], "completed")
        self.assertTrue(output.is_file())
        self.assertEqual(_sha256(candidate), before)
        self.assertTrue(report["safety"]["raw_ksa_preserved"])
        self.assertEqual(report["publisher_source"]["path"], str(output.resolve()))
        self.assertTrue(
            report["validation"]["required_table_counts"]["ontology_concepts"][
                "nonempty"
            ]
        )
        with closing(sqlite3.connect(output)) as conn:
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0], 0
            )

    def test_no_change_skips_copy_and_ontology_rebuild(self) -> None:
        candidate, baseline = self._prepared_baseline("candidate.db", "baseline.db")
        output = self.root / "prepared.db"

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=output,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["stages"], [])
        self.assertFalse(output.exists())
        self.assertEqual(report["publisher_source"]["path"], str(baseline.resolve()))
        self.assertIn(str(baseline.resolve()), report["next_publisher_command"])
        self.assertNotEqual(
            report["publisher_source"]["sha256"], report["source"]["sha256"]
        )
        self.assertTrue(report["validation"]["ok"])

    def test_legacy_state_dir_reports_no_baseline_rule_fingerprint(self) -> None:
        candidate, baseline = self._prepared_baseline("candidate.db", "prepared.db")
        state = self.root / "legacy-state"
        state.mkdir()
        shutil.copy2(baseline, state / "baseline.db")
        self.assertFalse((state / "current.json").exists())

        report = build_ontology_refresh(
            candidate,
            state_dir=state,
            prepared_output=self.root / "legacy-output.db",
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        # A legacy directory keeps working, and the report says outright that
        # the baseline carries no rule fingerprint to compare against.
        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertIsNone(report["baseline_rule_fingerprint"])
        self.assertEqual(report["rule_fingerprint"][:7], "sha256:")

    def test_no_change_blocks_when_managed_baseline_has_empty_derived_tables(
        self,
    ) -> None:
        baseline = self._db("baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["publisher_source"])
        self.assertIn(
            "managed_baseline_derived_ontology_validation_failed",
            report["strategy_reasons"],
        )
        self.assertEqual(
            report["validation"]["empty_required_derived_tables"],
            ["ksa_atomic_items", "ksa_concept_links", "ontology_concepts"],
        )

    def test_small_append_only_change_runs_incremental_build_on_copy(self) -> None:
        baseline = self._db("baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        self._append_ksa(candidate)
        output = self.root / "prepared.db"
        source_before = _sha256(candidate)
        baseline_before = _sha256(baseline)

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=output,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(report["selected_strategy"], "incremental_core_append")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(_sha256(candidate), source_before)
        self.assertEqual(_sha256(baseline), baseline_before)
        self.assertTrue(report["safety"]["raw_ksa_preserved"])
        self.assertIn(
            "preprocess_ksa_atomic_items", [stage["name"] for stage in report["stages"]]
        )
        with closing(sqlite3.connect(output)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0], 2
            )

    def test_update_or_delete_is_blocked_as_full_rebuild(self) -> None:
        baseline = self._db("baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        with closing(sqlite3.connect(candidate)) as conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='changed' WHERE ksa_no='1'")
            conn.commit()
        output = self.root / "prepared.db"

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=output,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(report["selected_strategy"], "full_rebuild_required")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(output.exists())
        self.assertIn(
            "source_update_or_delete_requires_destructive_reconciliation",
            report["strategy_reasons"],
        )

    def test_trusted_similarity_conflict_blocks_incremental_rebuild(self) -> None:
        baseline = self._db("baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        self._append_ksa(candidate)
        with closing(sqlite3.connect(candidate)) as conn:
            row = conn.execute(
                "SELECT criteria_id, element_id FROM performance_criteria LIMIT 1"
            ).fetchone()
            conn.executemany(
                """
                INSERT INTO ontology_concepts(
                    concept_name,normalized_key,concept_type,created_at,updated_at
                ) VALUES (?,?,?,?,?)
                """,
                [
                    ("concept one", "conceptone", "knowledge", "now", "now"),
                    ("concept two", "concepttwo", "skill", "now", "now"),
                ],
            )
            concept_ids = [
                value[0]
                for value in conn.execute(
                    "SELECT concept_id FROM ontology_concepts ORDER BY concept_id"
                )
            ]
            conn.execute(
                """
                INSERT INTO ontology_concept_relations(
                    source_concept_id,relation_type,target_concept_id,
                    relation_label,review_status,created_at
                ) VALUES (?, 'co_required_in_element', ?, 'trusted', 'accepted', 'now')
                """,
                (concept_ids[0], concept_ids[1]),
            )
            conn.execute(
                """
                INSERT INTO task_similarity_links(
                    source_criteria_id,target_criteria_id,source_element_id,target_element_id,
                    source_unit_code,target_unit_code,relation_type,similarity_score,
                    shared_concept_count,source_concept_count,target_concept_count,
                    source_only_count,target_only_count,evidence_json,review_status,created_at
                ) VALUES (?,?,?,?, 'U1','U1','upskilling_same_unit_task',1,1,1,1,0,0,'{}','human_reviewed','now')
                """,
                (row[0], row[0] + 1000, row[1], row[1]),
            )
            conn.commit()

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=self.root / "prepared.db",
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(
            report["selected_strategy"], "incremental_blocked_trusted_rows"
        )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["safety"]["trusted_row_conflicts"]["task_similarity_links"], 1
        )
        self.assertEqual(
            report["safety"]["trusted_row_conflicts"]["co_required_in_element"], 1
        )

    def test_supporting_only_change_prepares_copy_without_core_stages(self) -> None:
        _source, baseline = self._prepared_baseline("source.db", "baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        with closing(sqlite3.connect(candidate)) as conn:
            conn.execute(
                """
                INSERT INTO ncs_qualification_items(jm_cd,jm_nm,api_fetched_at)
                VALUES ('Q1','qualification','now')
                """
            )
            conn.commit()
        output = self.root / "prepared.db"

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=output,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        self.assertEqual(report["selected_strategy"], "supporting_evidence_refresh")
        self.assertEqual(report["stages"], [])
        self.assertTrue(output.is_file())

    def test_supporting_evidence_update_does_not_force_ontology_rebuild(self) -> None:
        _source, baseline = self._prepared_baseline("source.db", "baseline.db")
        with closing(sqlite3.connect(baseline)) as conn:
            conn.execute(
                """
                INSERT INTO ncs_qualification_items(jm_cd,jm_nm,api_fetched_at)
                VALUES ('Q1','old name','now')
                """
            )
            conn.commit()
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        with closing(sqlite3.connect(candidate)) as conn:
            conn.execute(
                "UPDATE ncs_qualification_items SET jm_nm='new name' WHERE jm_cd='Q1'"
            )
            conn.commit()

        report = build_ontology_refresh(
            candidate,
            baseline_db=baseline,
            prepared_output=self.root / "prepared.db",
            apply=True,
            full_rebuild_change_ratio_threshold=0.0,
            per_table_change_ratio_threshold=0.0,
            minimum_table_changes_for_fallback=1,
        )

        self.assertEqual(report["selected_strategy"], "supporting_evidence_refresh")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["stages"], [])

    def test_online_backup_includes_committed_wal_content(self) -> None:
        candidate = self._db("candidate.db")
        writer = sqlite3.connect(candidate)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO ncs_qualification_items(jm_cd,jm_nm,api_fetched_at) "
                "VALUES ('WAL1','committed in wal','now')"
            )
            writer.commit()
            self.assertTrue(candidate.with_name(candidate.name + "-wal").exists())

            output = self.root / "prepared.db"
            report = build_ontology_refresh(
                candidate,
                state_dir=self.root / "wal-state",
                prepared_output=output,
                apply=True,
            )

            self.assertTrue(report["ok"])
            with closing(sqlite3.connect(output)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT jm_nm FROM ncs_qualification_items WHERE jm_cd='WAL1'"
                    ).fetchone()[0],
                    "committed in wal",
                )
        finally:
            writer.close()


class StrategyRuleFingerprintTests(unittest.TestCase):
    """The rule fingerprint is the only signal that rebuilds on a rule change.

    The source projection is identical in every case here, so the fingerprint
    alone decides whether the ontology is rebuilt.
    """

    UNCHANGED_PLAN = {
        "tables": [],
        "full_rebuild_required": False,
        "full_rebuild_recommended": False,
    }
    CURRENT_RULES = "sha256:" + "a" * 64
    EARLIER_RULES = "sha256:" + "b" * 64

    def _strategy(self, baseline_rule_fingerprint: str | None) -> tuple[str, list[str]]:
        strategy, reasons, _ = _select_strategy(
            self.UNCHANGED_PLAN,
            baseline_exists=True,
            baseline_rule_fingerprint=baseline_rule_fingerprint,
            rule_fingerprint=self.CURRENT_RULES,
        )
        return strategy, reasons

    def test_matching_rule_fingerprint_skips_the_rebuild(self) -> None:
        strategy, reasons = self._strategy(self.CURRENT_RULES)

        self.assertEqual(strategy, "no_rebuild")
        self.assertEqual(reasons, ["source_projection_unchanged"])

    def test_changed_rule_fingerprint_forces_a_full_rebuild(self) -> None:
        strategy, reasons = self._strategy(self.EARLIER_RULES)

        self.assertEqual(strategy, "full_rebuild_required")
        self.assertIn("ontology_rule_fingerprint_changed", reasons)

    def test_unparsable_lineage_sidecar_forces_a_full_rebuild(self) -> None:
        strategy, reasons = self._strategy("invalid")

        self.assertEqual(strategy, "full_rebuild_required")
        self.assertIn("ontology_rule_fingerprint_changed", reasons)

    def test_absent_fingerprint_does_not_force_a_rebuild(self) -> None:
        # An absent fingerprint is deliberately not treated as a rule change.
        # Only a promotion writes the lineage sidecar, so a baseline supplied
        # through baseline_db never has one, and a legacy state directory keeps
        # working without one -- see
        # test_legacy_baseline_db_resolution_remains_supported.
        #
        # The cost is that a pointerless state directory cannot notice a rule
        # change: the source projection stays equal, so this returns no_rebuild
        # and the ontology keeps whatever rules built it.
        strategy, reasons = self._strategy(None)

        self.assertEqual(strategy, "no_rebuild")
        self.assertEqual(reasons, ["source_projection_unchanged"])

    def test_missing_baseline_still_bootstraps(self) -> None:
        strategy, reasons, _ = _select_strategy(
            None,
            baseline_exists=False,
            baseline_rule_fingerprint=None,
            rule_fingerprint=self.CURRENT_RULES,
        )

        self.assertEqual(strategy, "bootstrap_additive_build")
        self.assertEqual(reasons, ["managed_baseline_missing"])


if __name__ == "__main__":
    unittest.main()
