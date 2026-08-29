from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.api_refresh_builder import (
    RefreshCallables,
    raw_ksa_sha256,
    refresh_ncs_api_evidence,
    trusted_review_status_counts,
)


class ApiRefreshBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "ncs.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE classifications (major_code TEXT);
            INSERT INTO classifications(major_code) VALUES ('02'), ('01'), ('02');
            CREATE TABLE ksa_items (ksa_id INTEGER PRIMARY KEY, ksa_text_raw TEXT);
            INSERT INTO ksa_items(ksa_id, ksa_text_raw) VALUES (1, 'raw KSA'), (2, 'other raw KSA');
            CREATE TABLE review_fixture (review_status TEXT, source_review_status TEXT);
            INSERT INTO review_fixture(review_status, source_review_status)
            VALUES ('human_reviewed', 'accepted'), ('candidate', 'reviewed');
            """
        )
        conn.commit()
        conn.close()
        self.credentials = {"training-courses": "present", "job-base": "present"}

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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
        prohibited = refresh_ncs_api_evidence(
            self.db_path,
            sources=["qualification"],
            credentials={},
        )
        self.assertEqual(prohibited["outcome"], "blocked_preflight")
        self.assertIn("unsupported_or_prohibited_sources:qualification", prohibited["preflight_errors"])

        missing = refresh_ncs_api_evidence(
            self.db_path,
            sources=["job-base"],
            credentials={},
        )
        self.assertEqual(missing["outcome"], "blocked_preflight")
        self.assertIn("missing_credentials:job-base", missing["preflight_errors"])

    def test_plan_only_discovers_all_majors_without_writes(self) -> None:
        before = self.db_path.read_bytes()
        callbacks = RefreshCallables(
            collect_training=lambda *_args, **_kwargs: self.fail("plan must not collect"),
            collect_job_base=lambda *_args, **_kwargs: self.fail("plan must not collect"),
            build_training_links=lambda *_args, **_kwargs: self.fail("plan must not link"),
        )
        report = refresh_ncs_api_evidence(
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
        prepared = Path(self.tempdir.name) / "state" / "ncs.db"
        source_before = self.db_path.read_bytes()

        def training(db: Path, key: str, **kwargs: object) -> dict[str, object]:
            calls.append(("training", key, kwargs))
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE IF NOT EXISTS refresh_marker (major_code TEXT)")
            conn.execute("INSERT INTO refresh_marker(major_code) VALUES (?)", (kwargs["major_code"],))
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

        report = refresh_ncs_api_evidence(
            self.db_path,
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(training, job_base, links),
        )
        self.assertEqual(report["outcome"], "succeeded_append_only")
        self.assertEqual([(name, kwargs["major_code"]) for name, _, kwargs in calls], [
            ("training", "01"), ("training", "02"), ("job-base", "01"), ("job-base", "02"),
        ])
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
        self.assertEqual(prepared_conn.execute("SELECT COUNT(*) FROM refresh_marker").fetchone()[0], 2)
        prepared_conn.close()

    def test_unprovable_training_completion_never_builds_links_or_publishes(self) -> None:
        prepared = Path(self.tempdir.name) / "state" / "ncs.db"
        def incomplete(_db: Path, _key: str, **kwargs: object) -> dict[str, object]:
            return {"major_code": kwargs["major_code"], "pages_processed": 0, "reported_total_page": 0}

        callbacks = RefreshCallables(
            incomplete,
            self._ok_job_base,
            lambda *_args, **_kwargs: self.fail("links require proven training completion"),
        )
        report = refresh_ncs_api_evidence(
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

    def test_raw_ksa_and_trusted_review_invariants_are_verified_on_the_copy(self) -> None:
        before_hash = raw_ksa_sha256(self.db_path)
        before_reviews = trusted_review_status_counts(self.db_path)
        source_before = self.db_path.read_bytes()
        prepared = Path(self.tempdir.name) / "state" / "ncs.db"

        def corrupt_raw(db: Path, _key: str, **kwargs: object) -> dict[str, object]:
            conn = sqlite3.connect(db)
            conn.execute("UPDATE ksa_items SET ksa_text_raw = 'changed' WHERE ksa_id = 1")
            conn.commit()
            conn.close()
            return self._ok_training(db, "unused", **kwargs)

        report = refresh_ncs_api_evidence(
            self.db_path,
            sources=["training-courses"],
            apply=True,
            output_path=prepared,
            credentials=self.credentials,
            callables=RefreshCallables(corrupt_raw, self._ok_job_base, lambda *_args, **_kwargs: {"ok": True}),
        )
        self.assertEqual(report["outcome"], "failed_no_reconcile")
        self.assertFalse(report["working_copy_invariants_unchanged"])
        self.assertEqual(report["working_copy_invariants_before"]["raw_ksa_sha256"], before_hash)
        self.assertEqual(report["working_copy_invariants_before"]["trusted_review_status_counts"], before_reviews)
        self.assertEqual(self.db_path.read_bytes(), source_before)
        self.assertFalse(prepared.exists())

    def test_first_major_write_then_failure_never_mutates_source(self) -> None:
        prepared = Path(self.tempdir.name) / "state" / "ncs.db"
        source_before = self.db_path.read_bytes()

        def first_writes_second_fails(db: Path, _key: str, **kwargs: object) -> dict[str, object]:
            if kwargs["major_code"] == "01":
                conn = sqlite3.connect(db)
                conn.execute("CREATE TABLE partial_refresh_marker (marker TEXT)")
                conn.execute("INSERT INTO partial_refresh_marker(marker) VALUES ('first-major')")
                conn.commit()
                conn.close()
                return self._ok_training(db, "unused", **kwargs)
            raise RuntimeError("second major failure")

        report = refresh_ncs_api_evidence(
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
        prepared = Path(self.tempdir.name) / "state" / "ncs.db"
        source_conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(source_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            source_conn.execute("CREATE TABLE wal_probe (value TEXT)")
            source_conn.execute("INSERT INTO wal_probe(value) VALUES ('committed-in-wal')")
            source_conn.commit()
            source_before = self.db_path.read_bytes()

            report = refresh_ncs_api_evidence(
                self.db_path,
                sources=["job-base"],
                apply=True,
                output_path=prepared,
                credentials=self.credentials,
                callables=RefreshCallables(
                    self._ok_training,
                    self._ok_job_base,
                    lambda *_args, **_kwargs: self.fail("job-base-only refresh must not link training"),
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
            report = refresh_ncs_api_evidence(
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
            report = refresh_ncs_api_evidence(
                self.db_path,
                sources=["training-courses"],
                apply=True,
                credentials=self.credentials,
            )
        self.assertEqual(report["outcome"], "blocked_preflight")
        self.assertIn("read_only_environment_refuses_refresh", report["preflight_errors"])


if __name__ == "__main__":
    unittest.main()
