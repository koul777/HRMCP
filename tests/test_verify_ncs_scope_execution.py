from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.verify_ncs_scope_execution import run_gate, sample_scope_cases


def _rows():
    return [
        {
            "classification_id": 1,
            "major_code": "01",
            "major_name": "Major One",
            "middle_code": "0101",
            "middle_name": "Middle One",
            "small_code": "010101",
            "small_name": "Small One",
            "sub_code": "01010101",
            "sub_name": "Sub One",
        },
        {
            "classification_id": 2,
            "major_code": "01",
            "major_name": "Major One",
            "middle_code": "0102",
            "middle_name": "Middle Two",
            "small_code": "010201",
            "small_name": "Small Two",
            "sub_code": "01020101",
            "sub_name": "Sub Two",
        },
        # Same-branch duplicate row: it must not create ambiguity.
        {
            "classification_id": 3,
            "major_code": "01",
            "major_name": "Major One",
            "middle_code": "0101",
            "middle_name": "Middle One",
            "small_code": "010101",
            "small_name": "Small One",
            "sub_code": "01010101",
            "sub_name": "Sub One",
        },
        {
            "classification_id": 4,
            "major_code": "02",
            "major_name": "Major Two",
            "middle_code": "0201",
            "middle_name": "Middle Three",
            "small_code": "020101",
            "small_name": "Small Three",
            "sub_code": "02010101",
            "sub_name": "Sub Three",
        },
    ]


class ScopeExecutionGateTests(unittest.TestCase):
    def test_sampling_collapses_same_branch_and_flags_cross_branch_label(self):
        rows = _rows()
        rows[1]["small_name"] = "Shared Small"
        rows[3]["small_name"] = "Shared Small"
        sampled = sample_scope_cases(rows, expected_major_count=2)
        self.assertTrue(sampled["coverage_complete"])
        shared = [item for item in sampled["samples"] if item["label"] == "Shared Small"]
        self.assertEqual(len(shared), 1)
        self.assertTrue(shared[0]["ambiguous"])

    def test_gate_passes_unique_scopes_and_does_not_scope_bare_terms(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "synthetic.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_code TEXT, major_name TEXT,
                    middle_code TEXT, middle_name TEXT,
                    small_code TEXT, small_name TEXT,
                    sub_code TEXT, sub_name TEXT
                )"""
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(row.values()) for row in _rows()],
            )
            conn.commit()
            conn.close()

            def search_fn(*, query, scope, limit, job_scope=None):
                label = job_scope or query
                explicit_suffix = " \uc9c1\ubb34\uc5d0 \ud544\uc694\ud55c \uc5ed\ub7c9"
                if explicit_suffix in label:
                    label = label.split(explicit_suffix, 1)[0]
                selected = next((row for row in _rows() if label in row.values()), _rows()[0])
                path = {level + "_code": selected[level + "_code"] for level in ("major", "middle", "small", "sub")}
                path.update({level + "_name": selected[level + "_name"] for level in ("major", "middle", "small", "sub")})
                return {
                    "results": [{"type": "unit", "id": "u1", "path": path}],
                    "classification_filter_applied": bool(job_scope) or explicit_suffix in query,
                }

            report = run_gate(db_path, search_fn=search_fn, expected_major_count=2)
            self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))
            self.assertFalse(report["db_mutation"])
            self.assertFalse(report["holdout_inspected"])
            self.assertEqual(report["summary"]["execution_count"], 24)

    def test_gate_fails_on_out_of_scope_result(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "synthetic.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE classifications (classification_id INTEGER, major_code TEXT, major_name TEXT, middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT, sub_code TEXT, sub_name TEXT)")
            conn.execute("INSERT INTO classifications VALUES (1, '01', 'Major One', '0101', 'Middle One', '010101', 'Small One', '01010101', 'Sub One')")
            conn.commit(); conn.close()

            def leaking_search_fn(*, query, scope, limit, job_scope=None):
                return {"results": [{"type": "unit", "id": "leak", "path": {
                    "major_code": "99", "middle_code": "9901", "small_code": "990101", "sub_code": "99010101",
                    "major_name": "Other", "middle_name": "Other", "small_name": "Other", "sub_name": "Other",
                }}], "classification_filter_applied": bool(job_scope)}

            report = run_gate(db_path, search_fn=leaking_search_fn, expected_major_count=1)
            self.assertFalse(report["ok"])
            self.assertGreater(report["summary"]["unexpected_failure_count"], 0)
            self.assertLessEqual(len(report["unexpected_failures"]), 10)

    def test_gate_rejects_vacuous_empty_unscoped_explicit_results(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "synthetic.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE classifications (classification_id INTEGER, major_code TEXT, major_name TEXT, middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT, sub_code TEXT, sub_name TEXT)")
            conn.execute("INSERT INTO classifications VALUES (1, '01', 'Major One', '0101', 'Middle One', '010101', 'Small One', '01010101', 'Sub One')")
            conn.commit(); conn.close()

            def empty_unscoped_search(*, query, scope, limit, job_scope=None):
                return {"results": [], "classification_filter_applied": bool(job_scope)}

            report = run_gate(
                db_path,
                search_fn=empty_unscoped_search,
                expected_major_count=1,
            )

            self.assertFalse(report["ok"])
            explicit_failures = [
                item for item in report["execution"]
                if item["kind"] == "explicit_full_query" and not item["passed"]
            ]
            self.assertTrue(explicit_failures)
            self.assertEqual(
                explicit_failures[0]["failure_reason"],
                "explicit_scope_not_hard_bound",
            )

    def test_gate_rejects_bare_term_exceptions(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "synthetic.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE classifications (classification_id INTEGER, major_code TEXT, major_name TEXT, middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT, sub_code TEXT, sub_name TEXT)")
            conn.execute("INSERT INTO classifications VALUES (1, '01', 'Major One', '0101', 'Middle One', '010101', 'Small One', '01010101', 'Sub One')")
            conn.commit(); conn.close()

            def exception_on_bare(*, query, scope, limit, job_scope=None):
                if job_scope is None and " \uc9c1\ubb34\uc5d0 \ud544\uc694\ud55c \uc5ed\ub7c9" not in query:
                    raise RuntimeError("synthetic bare failure")
                path = {
                    "major_code": "01", "middle_code": "0101",
                    "small_code": "010101", "sub_code": "01010101",
                    "major_name": "Major One", "middle_name": "Middle One",
                    "small_name": "Small One", "sub_name": "Sub One",
                }
                return {
                    "results": [{"type": "unit", "id": "u1", "path": path}],
                    "classification_filter_applied": True,
                }

            report = run_gate(
                db_path,
                search_fn=exception_on_bare,
                expected_major_count=1,
            )

            self.assertFalse(report["ok"])
            bare_failures = [
                item for item in report["execution"]
                if item["kind"] == "bare_term" and not item["passed"]
            ]
            self.assertTrue(bare_failures)
            self.assertTrue(
                all(item["error_code"] == "exception" for item in bare_failures)
            )
            self.assertTrue(
                all(
                    item["failure_reason"] == "bare_term_execution_failure"
                    for item in bare_failures
                )
            )


if __name__ == "__main__":
    unittest.main()
