from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from ncs_mcp.data_builder import BuilderError, DataBuilder

from ncs_mcp.api_refresh_builder import (
    RefreshCallables,
    exclusive_refresh_lock,
    write_refresh_evidence,
    raw_ksa_sha256,
    refresh_ncs_api_evidence,
    trusted_review_status_identity_digest,
    trusted_review_status_counts,
)


class ApiRefreshBuilderTests(unittest.TestCase):
    def test_report_writer_rejects_database_sidecars_and_hardlinks(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(self.db_path) + suffix)
            if suffix:
                target.write_bytes(b"protected-sidecar")
            original = target.read_bytes()
            alias = self.db_path.parent / f"alias{suffix}.json"
            os.link(target, alias)
            for destination in (target, alias):
                with self.assertRaisesRegex(ValueError, "report_path_conflicts"):
                    write_refresh_evidence({"outcome": "blocked_preflight"}, destination,
                                           protected_databases=(self.db_path,))
                self.assertEqual(target.read_bytes(), original)
            alias.unlink()

    def test_plan_cli_cannot_overwrite_source_even_on_preflight_failure(self):
        original = self.db_path.read_bytes()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/refresh_ncs_api_evidence.py"),
             "--db", str(self.db_path), "--out", str(self.db_path)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("report_path_conflicts", result.stdout)
        self.assertEqual(original, self.db_path.read_bytes())

    def test_report_parent_junction_cannot_alias_source(self):
        alias = self.db_path.parent / "report-alias"
        try:
            alias.symlink_to(self.db_path.parent, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                self.skipTest("Directory aliases unavailable")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.db_path.parent)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        try:
            original = self.db_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "report_path_conflicts"):
                write_refresh_evidence({}, alias / self.db_path.name, protected_databases=(self.db_path,))
            self.assertEqual(original, self.db_path.read_bytes())
        finally:
            if alias.is_symlink():
                alias.unlink()
            else:
                alias.rmdir()

    def test_refresh_lock_preserves_replacement_and_edited_owner_record(self):
        for replacement in (True, False):
            with self.subTest(replacement=replacement):
                with exclusive_refresh_lock(self.db_path) as lock:
                    metadata = json.loads(lock.read_text(encoding="utf-8"))
                    self.assertEqual(len(metadata["owner_token"]), 64)
                    if replacement:
                        lock.unlink()
                    lock.write_text('{"owner_token": "another-owner"}', encoding="utf-8")
                self.assertEqual(json.loads(lock.read_text())["owner_token"], "another-owner")
                lock.unlink()
        with exclusive_refresh_lock(self.db_path) as lock:
            pass
        self.assertFalse(lock.exists())
        self.assertEqual(list(self.db_path.parent.glob(".ncs.db.api-refresh.lock.*")), [])

    def test_refresh_lock_identity_rejects_replacement_with_copied_token(self):
        with exclusive_refresh_lock(self.db_path) as lock:
            copied = lock.read_bytes()
            replacement = self.db_path.parent / "replacement.lock"
            replacement.write_bytes(copied)
            replacement.replace(lock)
        self.assertEqual(lock.read_bytes(), copied)
        lock.unlink()

    def test_refresh_lock_is_complete_at_atomic_publication(self):
        link = os.link
        seen = []
        def publish(source, destination):
            seen.append(json.loads(Path(source).read_text(encoding="utf-8")))
            self.assertFalse(Path(destination).exists())
            link(source, destination)
        with patch("ncs_mcp.api_refresh_builder.os.link", side_effect=publish):
            with exclusive_refresh_lock(self.db_path):
                pass
        self.assertEqual(seen[0]["schema"], "ncs_api_refresh_lock_v1")
        self.assertEqual(len(seen[0]["owner_token"]), 64)

    def test_revocation_in_backup_progress_does_not_publish_working_copy(self):
        output = self.version_dir / "prepared.db"
        def progress(event):
            if isinstance(event, dict) and event.get("stage") == "작업 DB 복사":
                (self.builder.state / "operation.lock").unlink(missing_ok=True)
        training = Mock()
        report = self._refresh(self.db_path, apply=True, credentials=self.credentials,
                               output_path=output, progress=progress,
                               callables=RefreshCallables(collect_training=training))
        training.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(report["failure_reason"], "builder_authorization_required")

    def test_lock_revoked_during_source_checks_prevents_backup(self):
        lock = self.builder.state / "operation.lock"
        def revoke(event):
            lock.unlink(missing_ok=True)
        with patch("ncs_mcp.api_refresh_builder._prepare_working_copy") as backup:
            report = self._refresh(self.db_path, apply=True, credentials=self.credentials,
                                   output_path=self.version_dir / "prepared.db", progress=revoke)
        backup.assert_not_called()
        self.assertEqual(report["failure_reason"], "builder_authorization_required")

    def test_lock_replaced_after_collector_stops_next_collector_and_links(self):
        lock = self.builder.state / "operation.lock"
        calls = []
        def collect(*args, **kwargs):
            calls.append(kwargs["major_code"])
            lock.write_text('{"owner": "replacement"}', encoding="utf-8")
            return self._ok_training(*args, **kwargs)
        job_base, links = Mock(), Mock()
        report = self._refresh(self.db_path, apply=True, credentials=self.credentials,
                               output_path=self.version_dir / "prepared.db",
                               callables=RefreshCallables(collect_training=collect,
                                                         collect_job_base=job_base,
                                                         build_training_links=links))
        self.assertEqual(len(calls), 1)
        job_base.assert_not_called()
        links.assert_not_called()
        self.assertEqual(report["failure_reason"], "builder_authorization_required")
        self.assertNotIn("prepared_output", report)
        self.assertTrue(Path(report["failed_output"]).exists())
        self.assertEqual(json.loads(lock.read_text())["owner"], "replacement")

    def test_lock_revoked_during_final_checks_prevents_completion(self):
        def progress(event):
            if event == "최종 원문·검토 상태 비교":
                (self.builder.state / "operation.lock").unlink()
        report = self._refresh(self.db_path, apply=True, credentials=self.credentials,
                               output_path=self.version_dir / "prepared.db", progress=progress,
                               callables=RefreshCallables(collect_training=self._ok_training,
                                                         collect_job_base=self._ok_job_base,
                                                         build_training_links=lambda *a, **k: {}))
        self.assertEqual(report["failure_reason"], "builder_authorization_required")
        self.assertNotIn("prepared_output", report)

    def test_optional_dbstat_schema_does_not_block_refresh_or_trusted_checks(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES ('table','page_statistics','page_statistics',0,'CREATE VIRTUAL TABLE page_statistics USING dbstat')")
        conn.commit()
        conn.close()
        counts = trusted_review_status_counts(self.db_path)
        self.assertEqual(counts['review_fixture.review_status.human_reviewed'], 1)
        from ncs_mcp.ontology_refresh_builder import _trusted_counts
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(_trusted_counts(connection)['review_fixture'], 1)
        result = self._refresh(
            self.db_path, apply=True, credentials=self.credentials,
            output_path=self.version_dir / 'prepared.db',
            callables=RefreshCallables(collect_training=self._ok_training,
                                      collect_job_base=self._ok_job_base,
                                      build_training_links=lambda *args, **kwargs: {}))
        self.assertEqual(result['outcome'], 'succeeded_append_only', result)

    def test_local_error_reports_safe_reason_and_stage(self) -> None:
        with patch('ncs_mcp.api_refresh_builder.trusted_review_status_counts',
                   side_effect=sqlite3.OperationalError('no such module: dbstat secret-key')):
            result = self._refresh(self.db_path, apply=True, credentials=self.credentials,
                                              output_path=self.version_dir / 'prepared.db')
        self.assertEqual(result['failed_phase'], 'source_invariant_check')
        self.assertEqual(result['failure_reason'], 'sqlite_dbstat_module_unavailable')
        self.assertNotIn('secret-key', str(result))

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "ncs.db"
        self.builder = DataBuilder(Path(self.tempdir.name))
        self.version_dir = self.builder.state / "versions" / "a1"
        self.version_dir.mkdir(parents=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE classifications (major_code TEXT);
            INSERT INTO classifications(major_code) VALUES ('02'), ('01'), ('02');
            CREATE TABLE ksa_items (ksa_id INTEGER PRIMARY KEY, ksa_text_raw TEXT);
            INSERT INTO ksa_items(ksa_id, ksa_text_raw) VALUES (1, 'raw KSA'), (2, 'other raw KSA');
            CREATE TABLE review_fixture (
                review_id INTEGER PRIMARY KEY,
                review_status TEXT,
                source_review_status TEXT
            );
            INSERT INTO review_fixture(review_id, review_status, source_review_status)
            VALUES (1, 'human_reviewed', 'accepted'), (2, 'candidate', 'reviewed');
            """
        )
        conn.commit()
        conn.close()
        self.credentials = {"training-courses": "present", "job-base": "present"}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _refresh(self, *args, **kwargs):
        if not kwargs.get("apply"):
            return refresh_ncs_api_evidence(*args, **kwargs)
        action = "resume" if kwargs.get("resume") else "refresh_api"
        with self.builder.exclusive(action, "a1") as context:
            return refresh_ncs_api_evidence(*args, builder_context=context, **kwargs)

    def test_apply_requires_explicit_live_context_before_settings_or_io(self):
        output = self.version_dir / "prepared.db"
        with patch("ncs_mcp.api_refresh_builder._credentials_from_settings") as settings:
            for context in (None, {"action": "refresh_api"}, object()):
                report = refresh_ncs_api_evidence(
                    self.db_path, apply=True, output_path=output, builder_context=context
                )
                self.assertEqual(report["preflight_errors"], ["builder_authorization_required"])
            with self.builder.exclusive("refresh_api", "a1") as context:
                report = refresh_ncs_api_evidence(self.db_path, apply=True, output_path=output)
                self.assertEqual(report["preflight_errors"], ["builder_authorization_required"])
            report = refresh_ncs_api_evidence(
                self.db_path, apply=True, output_path=output, builder_context=context
            )
            self.assertEqual(report["preflight_errors"], ["builder_authorization_required"])
            settings.assert_not_called()
        self.assertFalse(output.exists())

    def test_apply_rejects_wrong_action_version_and_checkpoint(self):
        with self.builder.exclusive("build_delta", "a1") as context:
            report = refresh_ncs_api_evidence(self.db_path, apply=True, builder_context=context)
            self.assertEqual(report["preflight_errors"], ["builder_authorization_required"])
        with self.builder.exclusive("refresh_api", "a1") as context:
            for options in (
                {"output_path": Path(self.tempdir.name) / "escaped.db"},
                {"output_path": self.version_dir.parent / "a2" / "ncs.db"},
                {"checkpoint_dir": Path(self.tempdir.name) / "checkpoint"},
                {"state_dir": Path(self.tempdir.name)},
                {"resume": True},
            ):
                report = refresh_ncs_api_evidence(self.db_path, apply=True, builder_context=context, **options)
                self.assertEqual(report["preflight_errors"], ["builder_authorization_required"])

    def test_direct_apply_cli_fails_before_creating_output_or_report(self):
        destination = Path(self.tempdir.name) / "cli-output"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/refresh_ncs_api_evidence.py"),
             "--apply", "--db", str(self.db_path), "--output", str(destination / "ncs.db"),
             "--out", str(destination / "report.json")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "builder_authorization_required")
        self.assertFalse(destination.exists())

    def test_redirected_version_directory_is_rejected(self):
        target = Path(self.tempdir.name) / "outside"
        target.mkdir()
        redirected = self.version_dir.parent / "a2"
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
            # directory before a lower-level refresh context can be issued.
            with self.assertRaises(BuilderError):
                with self.builder.exclusive("refresh_api", "a2"):
                    self.fail("redirected Builder version acquired a live lease")
            self.assertFalse((target / "ncs.db").exists())
        finally:
            if redirected.is_symlink():
                redirected.unlink()
            else:
                redirected.rmdir()

    def _ok_training(self, _db: Path, _key: str, **kwargs: object) -> dict[str, object]:
        return {
            "major_code": kwargs["major_code"],
            "pages_processed": 1,
            "rows_upserted": 2,
            "reported_total_count": 2,
            "reported_total_page": 1,
        }

    def _ok_job_base(self, _db: Path, _key: str, **kwargs: object) -> dict[str, object]:
        return {
            "major_code": kwargs["major_code"],
            "ok": True,
            "error_count": 0,
            "pages_processed": 1,
            "rows_processed": 2,
            "links_upserted": 2,
            "missing_local_units": 0,
        }

    def test_preflight_refuses_prohibited_source_and_missing_credentials(self) -> None:
        prohibited = self._refresh(
            self.db_path,
            sources=["qualification"],
            credentials={},
        )
        self.assertEqual(prohibited["outcome"], "blocked_preflight")
        self.assertIn(
            "unsupported_or_prohibited_sources:qualification",
            prohibited["preflight_errors"],
        )

        missing = self._refresh(
            self.db_path,
            sources=["job-base"],
            credentials={},
        )
        self.assertEqual(missing["outcome"], "blocked_preflight")
        self.assertIn("missing_credentials:job-base", missing["preflight_errors"])

    def test_plan_only_discovers_all_majors_without_writes(self) -> None:
        before = self.db_path.read_bytes()
        callbacks = RefreshCallables(
            collect_training=lambda *_args, **_kwargs: self.fail(
                "plan must not collect"
            ),
            collect_job_base=lambda *_args, **_kwargs: self.fail(
                "plan must not collect"
            ),
            build_training_links=lambda *_args, **_kwargs: self.fail(
                "plan must not link"
            ),
        )
        report = self._refresh(
            self.db_path,
            sources=["training-courses"],
            credentials=self.credentials,
            callables=callbacks,
        )
        self.assertEqual(report["outcome"], "plan_only")
        self.assertEqual(report["major_codes"], ["01", "02"])
        self.assertFalse(report["writes_performed"])
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_apply_calls_every_discovered_major_with_full_scope_only(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []
        prepared = self.version_dir / "ncs.db"
        source_before = self.db_path.read_bytes()

        def training(db: Path, key: str, **kwargs: object) -> dict[str, object]:
            calls.append(("training", key, kwargs))
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE IF NOT EXISTS refresh_marker (major_code TEXT)")
            conn.execute(
                "INSERT INTO refresh_marker(major_code) VALUES (?)",
                (kwargs["major_code"],),
            )
            conn.commit()
            conn.close()
            return self._ok_training(db, key, **kwargs)

        def job_base(db: Path, key: str, **kwargs: object) -> dict[str, object]:
            calls.append(("job-base", key, kwargs))
            return self._ok_job_base(db, key, **kwargs)

        link_calls: list[bool] = []

        def links(_conn: sqlite3.Connection, *, reset: bool) -> dict[str, object]:
            link_calls.append(reset)
            return {"ok": True}

        report = self._refresh(
            self.db_path,
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(training, job_base, links),
        )
        self.assertEqual(report["outcome"], "succeeded_append_only")
        self.assertEqual(
            [(name, kwargs["major_code"]) for name, _, kwargs in calls],
            [
                ("training", "01"),
                ("training", "02"),
                ("job-base", "01"),
                ("job-base", "02"),
            ],
        )
        for _, _, kwargs in calls:
            self.assertIsNone(kwargs["module_name"])
            self.assertEqual(kwargs["page_no"], 1)
            self.assertEqual(kwargs["num_of_rows"], 500)
            self.assertIsNone(kwargs["max_pages"])
        self.assertEqual(link_calls, [False])
        self.assertEqual(self.db_path.read_bytes(), source_before)
        self.assertEqual(Path(report["prepared_output"]), prepared)
        self.assertTrue(prepared.is_file())
        self.assertNotEqual(prepared.read_bytes(), source_before)
        prepared_conn = sqlite3.connect(prepared)
        self.assertEqual(
            prepared_conn.execute("SELECT COUNT(*) FROM refresh_marker").fetchone()[0],
            2,
        )
        prepared_conn.close()

    def test_unprovable_training_completion_never_builds_links_or_publishes(
        self,
    ) -> None:
        prepared = self.version_dir / "ncs.db"

        def incomplete(_db: Path, _key: str, **kwargs: object) -> dict[str, object]:
            return {
                "major_code": kwargs["major_code"],
                "pages_processed": 0,
                "reported_total_page": 0,
            }

        callbacks = RefreshCallables(
            incomplete,
            self._ok_job_base,
            lambda *_args, **_kwargs: self.fail(
                "links require proven training completion"
            ),
        )
        report = self._refresh(
            self.db_path,
            sources=["training-courses"],
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=callbacks,
        )
        self.assertEqual(report["outcome"], "inconclusive_no_publish")
        self.assertFalse(report["publish_performed"])
        self.assertFalse(report["training_link_build"]["performed"])
        self.assertFalse(prepared.exists())

    def test_raw_ksa_and_trusted_review_invariants_are_verified_on_the_copy(
        self,
    ) -> None:
        before_hash = raw_ksa_sha256(self.db_path)
        before_reviews = trusted_review_status_counts(self.db_path)
        source_before = self.db_path.read_bytes()
        prepared = self.version_dir / "ncs.db"

        def corrupt_raw(db: Path, _key: str, **kwargs: object) -> dict[str, object]:
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE ksa_items SET ksa_text_raw = 'changed' WHERE ksa_id = 1"
            )
            conn.commit()
            conn.close()
            return self._ok_training(db, "unused", **kwargs)

        report = self._refresh(
            self.db_path,
            sources=["training-courses"],
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(
                corrupt_raw, self._ok_job_base, lambda *_args, **_kwargs: {"ok": True}
            ),
        )
        self.assertEqual(report["outcome"], "failed_no_reconcile")
        self.assertFalse(report["working_copy_invariants_unchanged"])
        self.assertEqual(
            report["working_copy_invariants_before"]["raw_ksa_sha256"], before_hash
        )
        self.assertEqual(
            report["working_copy_invariants_before"]["trusted_review_status_counts"],
            before_reviews,
        )
        self.assertEqual(self.db_path.read_bytes(), source_before)
        self.assertFalse(prepared.exists())

    def test_trusted_status_row_swap_with_unchanged_counts_fails_closed(self) -> None:
        prepared = self.version_dir / "ncs.db"
        before_counts = trusted_review_status_counts(self.db_path)
        before_identity = trusted_review_status_identity_digest(self.db_path)

        def swap_trusted_status_rows(
            db: Path, _key: str, **kwargs: object
        ) -> dict[str, object]:
            if kwargs["major_code"] == "01":
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute(
                        "UPDATE review_fixture SET review_status='candidate' "
                        "WHERE review_id=1"
                    )
                    conn.execute(
                        "UPDATE review_fixture SET review_status='human_reviewed' "
                        "WHERE review_id=2"
                    )
                    conn.commit()
            return self._ok_training(db, "unused", **kwargs)

        report = self._refresh(
            self.db_path,
            sources=["training-courses"],
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(
                swap_trusted_status_rows,
                self._ok_job_base,
                lambda *_args, **_kwargs: self.fail("identity failure must not link"),
            ),
        )

        after_counts = report["working_copy_invariants_after"][
            "trusted_review_status_counts"
        ]
        after_identity = report["working_copy_invariants_after"][
            "trusted_review_status_identity_digest"
        ]
        self.assertEqual(report["outcome"], "failed_no_reconcile")
        self.assertEqual(before_counts, after_counts)
        self.assertNotEqual(before_identity, after_identity)
        self.assertFalse(report["working_copy_invariants_unchanged"])
        self.assertFalse(prepared.exists())

    def test_first_major_write_then_failure_never_mutates_source(self) -> None:
        prepared = self.version_dir / "ncs.db"
        source_before = self.db_path.read_bytes()

        def first_writes_second_fails(
            db: Path, _key: str, **kwargs: object
        ) -> dict[str, object]:
            if kwargs["major_code"] == "01":
                conn = sqlite3.connect(db)
                conn.execute("CREATE TABLE partial_refresh_marker (marker TEXT)")
                conn.execute(
                    "INSERT INTO partial_refresh_marker(marker) VALUES ('first-major')"
                )
                conn.commit()
                conn.close()
                return self._ok_training(db, "unused", **kwargs)
            raise RuntimeError("second major failure")

        report = self._refresh(
            self.db_path,
            sources=["training-courses"],
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(
                first_writes_second_fails,
                self._ok_job_base,
                lambda *_args, **_kwargs: self.fail("partial refresh must not link"),
            ),
        )
        self.assertEqual(report["outcome"], "failed_no_reconcile")
        self.assertEqual(self.db_path.read_bytes(), source_before)
        self.assertTrue(report["source_invariants_after"]["unchanged"])
        self.assertFalse(prepared.exists())

    def test_prepared_copy_includes_committed_uncheckpointed_wal_rows(self) -> None:
        prepared = self.version_dir / "ncs.db"
        source_conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                source_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            source_conn.execute("CREATE TABLE wal_probe (value TEXT)")
            source_conn.execute(
                "INSERT INTO wal_probe(value) VALUES ('committed-in-wal')"
            )
            source_conn.commit()
            source_before = self.db_path.read_bytes()

            report = self._refresh(
                self.db_path,
                sources=["job-base"],
                apply=True,
                output_path=prepared,
                credentials=self.credentials,
                callables=RefreshCallables(
                    self._ok_training,
                    self._ok_job_base,
                    lambda *_args, **_kwargs: self.fail(
                        "job-base-only refresh must not link training"
                    ),
                ),
            )
            self.assertEqual(report["outcome"], "succeeded_append_only")
            self.assertEqual(self.db_path.read_bytes(), source_before)
            prepared_conn = sqlite3.connect(prepared)
            try:
                self.assertEqual(
                    prepared_conn.execute("SELECT value FROM wal_probe").fetchone()[0],
                    "committed-in-wal",
                )
            finally:
                prepared_conn.close()
        finally:
            source_conn.close()

    def test_existing_lock_blocks_apply_without_collection(self) -> None:
        lock_path = self.db_path.with_name(f"{self.db_path.name}.api-refresh.lock")
        lock_path.write_text("occupied", encoding="utf-8")
        try:
            report = self._refresh(
                self.db_path,
                sources=["job-base"],
                apply=True,
                credentials=self.credentials,
                callables=RefreshCallables(
                    self._ok_training,
                    lambda *_args, **_kwargs: self.fail("lock must stop collection"),
                    lambda *_args, **_kwargs: self.fail("lock must stop linking"),
                ),
            )
        finally:
            lock_path.unlink()
        self.assertEqual(report["outcome"], "blocked_preflight")
        self.assertIn("refresh_lock_already_exists", report["preflight_errors"])

    def test_read_only_environment_blocks_apply(self) -> None:
        with patch.dict(os.environ, {"NCS_MCP_READ_ONLY": "true"}, clear=False):
            report = self._refresh(
                self.db_path,
                sources=["training-courses"],
                apply=True,
                credentials=self.credentials,
            )
        self.assertEqual(report["outcome"], "blocked_preflight")
        self.assertIn(
            "read_only_environment_refuses_refresh", report["preflight_errors"]
        )


if __name__ == "__main__":
    unittest.main()
