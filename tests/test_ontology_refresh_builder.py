from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch
from ncs_mcp.data_builder import BuilderError, DataBuilder
from ncs_mcp.builder_authorization import BuilderAuthorizationError, require_builder_context

from ncs_mcp.db import connect, initialize_database
from ncs_mcp import ontology_refresh_builder
from ncs_mcp.ontology_refresh_builder import (
    RefreshBuilderError,
    _run_training_pipeline,
    _select_strategy,
    _trusted_counts,
    _trusted_status_identity_digest,
    build_ontology_refresh,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OntologyRefreshBuilderTests(unittest.TestCase):
    def test_plan_cli_rejects_source_baseline_and_sidecar_report_aliases(self):
        source = self.root / "source.db"
        baseline = self.root / "baseline.db"
        source.write_bytes(b"immutable source")
        baseline.write_bytes(b"immutable baseline")
        script = str(Path(__file__).resolve().parents[1] / "scripts/refresh_ncs_ontology.py")
        for database in (source, baseline):
            for suffix in ("", "-wal", "-shm", "-journal"):
                target = Path(str(database) + suffix)
                if suffix:
                    target.write_bytes(b"immutable sidecar")
                alias = self.root / "report-alias.json"
                os.link(target, alias)
                try:
                    original = target.read_bytes()
                    result = subprocess.run([sys.executable, script, str(source), "--baseline", str(baseline),
                                             "--report", str(alias)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("report_path_conflicts", result.stdout)
                    self.assertEqual(original, target.read_bytes())
                finally:
                    alias.unlink()

    def test_lock_revoked_while_planning_prevents_snapshot(self):
        source = self._db("candidate.db")
        artifact = ontology_refresh_builder._artifact
        def revoke(path):
            value = artifact(path)
            (self.builder.state / "operation.lock").unlink(missing_ok=True)
            return value
        with patch.object(ontology_refresh_builder, "_artifact", side_effect=revoke), \
             patch.object(ontology_refresh_builder, "_sqlite_online_snapshot") as snapshot:
            with self.assertRaises(BuilderError) as raised:
                self._refresh(source, apply=True, state_dir=self.root / "state",
                              prepared_output=self.root / "output.db")
            self.assertIsInstance(raised.exception.__cause__, BuilderAuthorizationError)
        snapshot.assert_not_called()

    def test_lock_replaced_after_snapshot_prevents_pipeline_and_preserves_copy(self):
        source = self._db("candidate.db")
        output = self.root / "output.db"
        snapshot = ontology_refresh_builder._sqlite_online_snapshot
        lock = self.builder.state / "operation.lock"
        def revoke(*args, **kwargs):
            snapshot(*args, **kwargs)
            lock.write_text('{"owner": "replacement"}', encoding="utf-8")
        with patch.object(ontology_refresh_builder, "_sqlite_online_snapshot", side_effect=revoke), \
             patch.object(ontology_refresh_builder, "_run_pipeline") as pipeline:
            with self.assertRaises(BuilderError) as raised:
                self._refresh(source, apply=True, state_dir=self.root / "state", prepared_output=output)
            self.assertIsInstance(raised.exception.__cause__, BuilderAuthorizationError)
        pipeline.assert_not_called()
        self.assertTrue(output.exists())
        self.assertEqual(json.loads(lock.read_text())["owner"], "replacement")

    def test_each_pipeline_stage_rechecks_authority_before_next_mutation(self):
        names = ["ensure_ontology_seeded", "preprocess_ksa_atomic_items",
                 "build_task_ksa_concept_relations", "ensure_ncs_ontology_relations",
                 "build_task_similarity_links", "build_training_course_ontology_links"]
        lock = self.builder.state / "operation.lock"
        for revoked_at in range(len(names)):
            with self.subTest(stage=names[revoked_at]), ExitStack() as stack:
                context = stack.enter_context(self.builder.exclusive("build_delta", "a1"))
                stack.enter_context(patch.object(ontology_refresh_builder, "connect", return_value=Mock()))
                for reader in ("_raw_ksa_hash", "_trusted_counts", "_trusted_status_identity_digest"):
                    stack.enter_context(patch.object(ontology_refresh_builder, reader, return_value={}))
                mutations = [stack.enter_context(patch.object(ontology_refresh_builder, name, return_value={}))
                             for name in names]
                mutations[revoked_at].side_effect = lambda *a, **k: lock.unlink()
                with self.assertRaises(BuilderAuthorizationError):
                    ontology_refresh_builder._run_pipeline(
                        self.root / "output.db", bootstrap=True,
                        authorize=lambda: require_builder_context(context, action="build_delta"))
                for mutation in mutations[revoked_at + 1:]:
                    mutation.assert_not_called()

    def test_lock_revoked_in_validation_prevents_completion_evidence(self):
        source = self._db("candidate.db")
        output = self.root / "output.db"
        def revoke(path):
            (self.builder.state / "operation.lock").unlink()
            return {"ok": True}
        with patch.object(ontology_refresh_builder, "_run_pipeline", return_value=([], {})), \
             patch.object(ontology_refresh_builder, "_integrity", side_effect=revoke):
            with self.assertRaises(BuilderError) as raised:
                self._refresh(source, apply=True, state_dir=self.root / "state", prepared_output=output)
            self.assertIsInstance(raised.exception.__cause__, BuilderAuthorizationError)
        self.assertTrue(output.exists())

    def test_training_pipeline_revalidates_after_invariant_reads(self):
        with self.builder.exclusive("build_delta", "a1") as context, ExitStack() as stack:
            stack.enter_context(patch.object(ontology_refresh_builder, "connect", return_value=Mock()))
            stack.enter_context(patch.object(ontology_refresh_builder, "_raw_ksa_hash", return_value="hash"))
            stack.enter_context(patch.object(ontology_refresh_builder, "_trusted_counts", return_value={}))
            stack.enter_context(patch.object(ontology_refresh_builder, "_trusted_status_identity_digest",
                                             side_effect=lambda *a: (self.builder.state / "operation.lock").unlink()))
            mutate = stack.enter_context(patch.object(ontology_refresh_builder, "build_training_course_ontology_links"))
            with self.assertRaises(BuilderAuthorizationError):
                _run_training_pipeline(self.root / "output.db",
                                       authorize=lambda: require_builder_context(context, action="build_delta"))
            mutate.assert_not_called()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.builder = DataBuilder(Path(self.temp_dir.name))
        self.root = self.builder.state / "versions" / "a1"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _refresh(self, *args, **kwargs):
        if not kwargs.get("apply"):
            return build_ontology_refresh(*args, **kwargs)
        with self.builder.exclusive("build_delta", "a1") as context:
            return build_ontology_refresh(*args, builder_context=context, **kwargs)

    def test_apply_rejects_missing_expired_or_wrong_scope_context_before_io(self):
        missing = self.root / "missing-source.db"
        for context in (None, {"action": "build_delta"}, object()):
            with self.assertRaisesRegex(RefreshBuilderError, "builder_authorization_required"):
                build_ontology_refresh(missing, apply=True, builder_context=context)
        with self.builder.exclusive("build_delta", "a1") as context:
            for output in (self.root.parent / "a2" / "ncs.db", Path(self.temp_dir.name) / "escaped.db"):
                with self.assertRaisesRegex(RefreshBuilderError, "builder_authorization_required"):
                    build_ontology_refresh(missing, apply=True, prepared_output=output, builder_context=context)
            with self.assertRaisesRegex(RefreshBuilderError, "builder_authorization_required"):
                build_ontology_refresh(missing, apply=True)
        with self.assertRaisesRegex(RefreshBuilderError, "builder_authorization_required"):
            build_ontology_refresh(missing, apply=True, builder_context=context)
        with self.builder.exclusive("refresh_api", "a1") as context:
            with self.assertRaisesRegex(RefreshBuilderError, "builder_authorization_required"):
                build_ontology_refresh(missing, apply=True, builder_context=context)

    def test_direct_apply_cli_fails_before_creating_output_or_report(self):
        destination = self.root / "cli-output"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/refresh_ncs_ontology.py"),
             str(self.root / "missing.db"), "--apply", "--output", str(destination / "ncs.db"),
             "--report", str(destination / "report.json")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "builder_authorization_required")
        self.assertFalse(destination.exists())

    def test_redirected_version_directory_is_rejected(self):
        target = Path(self.temp_dir.name) / "outside"
        target.mkdir()
        redirected = self.root.parent / "a2"
        try:
            redirected.symlink_to(target, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                self.skipTest("Directory symlinks are unavailable")
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(redirected), str(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(junction.returncode, 0, junction.stderr)
        try:
            # The shared Builder lease now rejects a redirected version
            # directory before a lower-level ontology context can be issued.
            with self.assertRaises(BuilderError):
                with self.builder.exclusive("build_delta", "a2"):
                    self.fail("redirected Builder version acquired a live lease")
            self.assertFalse((target / "ncs.db").exists())
        finally:
            if redirected.is_symlink():
                redirected.unlink()
            else:
                redirected.rmdir()

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
        report = self._refresh(
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

        report = self._refresh(
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

        report = self._refresh(
            candidate,
            state_dir=self.root / "state",
            prepared_output=output,
            apply=True,
        )

        self.assertEqual(report["status"], "completed")
        self.assertTrue(output.is_file())
        self.assertEqual(_sha256(candidate), before)
        self.assertTrue(report["safety"]["raw_ksa_preserved"])
        self.assertEqual(
            report["safety"]["trusted_status_identity_digest_before"],
            report["safety"]["trusted_status_identity_digest_after"],
        )
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

        report = self._refresh(
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
        self.assertIsNone(report["next_publisher_command"])
        self.assertEqual(report["next_builder_action"], "package")
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

        report = self._refresh(
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

        report = self._refresh(
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

        report = self._refresh(
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

    def test_training_pipeline_rejects_trusted_status_row_swap_with_same_count(self) -> None:
        candidate = self._db("candidate.db")
        with closing(connect(candidate)) as conn:
            conn.executemany(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name,current_query,target_query,review_status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                [
                    ("first", "current", "target", "human_reviewed", "now", "now"),
                    ("second", "current", "target", "candidate", "now", "now"),
                ],
            )
            conn.commit()
            before_counts = _trusted_counts(conn)
            before_identity = _trusted_status_identity_digest(conn)

        def swap_rows(conn, *, reset: bool):
            self.assertFalse(reset)
            conn.execute(
                "UPDATE training_transition_gold_scenarios SET review_status='candidate' "
                "WHERE scenario_name='first'"
            )
            conn.execute(
                "UPDATE training_transition_gold_scenarios SET review_status='human_reviewed' "
                "WHERE scenario_name='second'"
            )
            return {}

        with patch.object(
            ontology_refresh_builder, "build_training_course_ontology_links", swap_rows
        ):
            with self.assertRaisesRegex(
                RefreshBuilderError, "trusted-state invariants"
            ):
                _run_training_pipeline(candidate)

        with closing(connect(candidate, read_only=True)) as conn:
            self.assertEqual(before_counts, _trusted_counts(conn))
            self.assertEqual(before_identity, _trusted_status_identity_digest(conn))

    def test_update_or_delete_is_blocked_as_full_rebuild(self) -> None:
        baseline = self._db("baseline.db")
        candidate = self.root / "candidate.db"
        shutil.copy2(baseline, candidate)
        with closing(sqlite3.connect(candidate)) as conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='changed' WHERE ksa_no='1'")
            conn.commit()
        output = self.root / "prepared.db"

        report = self._refresh(
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

        report = self._refresh(
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

        report = self._refresh(
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

        report = self._refresh(
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
            report = self._refresh(
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
