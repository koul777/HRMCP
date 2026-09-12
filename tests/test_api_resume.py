import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.api_refresh_builder import RefreshCallables, refresh_ncs_api_evidence
from ncs_mcp.data_builder import DataBuilder


class ApiResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.builder = DataBuilder(Path(self.temp.name))
        self.root = self.builder.state / "versions" / "a1"
        self.root.mkdir(parents=True)
        self.db = self.root / "ncs.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("CREATE TABLE classifications(major_code TEXT); INSERT INTO classifications VALUES('01'),('02'); CREATE TABLE ksa_items(ksa_id INTEGER,ksa_text_raw TEXT); INSERT INTO ksa_items VALUES(1,'raw'); CREATE TABLE received(major TEXT PRIMARY KEY);")
        self.options = dict(apply=True, sources=["job-base"], credentials={"job-base": "secret"}, output_path=self.root / "working.db", checkpoint_dir=self.root / "api-checkpoint")

    def tearDown(self):
        self.temp.cleanup()

    def _refresh(self, *args, **kwargs):
        action = "resume" if kwargs.get("resume") else "refresh_api"
        with self.builder.exclusive(action, "a1") as context:
            return refresh_ncs_api_evidence(*args, builder_context=context, **kwargs)

    def collector(self, fail=False):
        def collect(db, key, **kwargs):
            major = kwargs["major_code"]
            if fail and major == "02":
                raise OSError("fake error")
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO received VALUES(?)", (major,))
            return {"ok": True, "pages_processed": 1, "error_count": 0}
        return collect

    def test_resume_skips_committed_major_and_does_not_recopy(self):
        first = self._refresh(self.db, **self.options, callables=RefreshCallables(collect_job_base=self.collector(fail=True)))
        self.assertEqual(first["outcome"], "failed_no_reconcile")
        calls = []
        def resumed(*args, **kwargs):
            calls.append(kwargs["major_code"])
            return self.collector()(*args, **kwargs)
        with patch("ncs_mcp.api_refresh_builder._prepare_working_copy", side_effect=AssertionError("must not copy")):
            final = self._refresh(self.db, **self.options, resume=True, callables=RefreshCallables(collect_job_base=resumed))
        self.assertEqual(final["outcome"], "succeeded_append_only", final)
        self.assertEqual(calls, ["02"])
        self.assertNotIn("secret", (self.root / "api-checkpoint/api_checkpoint.json").read_text())

    def test_source_change_refuses_resume(self):
        self._refresh(self.db, **self.options, callables=RefreshCallables(collect_job_base=self.collector(fail=True)))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='changed'")
        result = self._refresh(self.db, **self.options, resume=True)
        self.assertEqual(result["preflight_errors"], ["checkpoint_identity_mismatch"])

    def test_working_raw_invariant_change_refuses_resume(self):
        self._refresh(self.db, **self.options, callables=RefreshCallables(collect_job_base=self.collector(fail=True)))
        with closing(sqlite3.connect(self.options["output_path"])) as conn, conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='changed'")
        result = self._refresh(self.db, **self.options, resume=True)
        self.assertEqual(result["preflight_errors"], ["checkpoint_invariant_mismatch"])

    def test_source_parameter_change_refuses_resume(self):
        self._refresh(self.db, **self.options, callables=RefreshCallables(collect_job_base=self.collector(fail=True)))
        options = {**self.options, "sources": ["training-courses"], "credentials": {"training-courses": "secret"}}
        result = self._refresh(self.db, **options, resume=True)
        self.assertEqual(result["preflight_errors"], ["checkpoint_identity_mismatch"])


if __name__ == "__main__":
    unittest.main()
