from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPT = ROOT / "scripts" / "trace_query_applicability.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

from trace_query_applicability import compare_traces


class TraceQueryApplicabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "query # evidence.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE items(value TEXT)")
            conn.execute("INSERT INTO items VALUES ('한글')")
            conn.commit()
        self.source = self.root / "src"
        package = self.source / "ncs_mcp"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "db.py").write_text(
            "import sqlite3\n"
            "def connect(path, *, read_only):\n"
            "    assert read_only\n"
            "    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)\n"
            "    conn.execute('PRAGMA query_only = ON')\n"
            "    return conn\n",
            encoding="utf-8",
        )
        (package / "server.py").write_text(
            "open_db = None\n"
            "calls = {}\n"
            "def query_db(query, write=False):\n"
            "    calls[query] = calls.get(query, 0) + 1\n"
            "    with open_db() as conn:\n"
            "        if calls[query] == 1:\n"
            "            conn.execute(\"SELECT 'warmup only'\")\n"
            "        if write:\n"
            "            conn.execute(\"INSERT INTO items VALUES ('forbidden')\")\n"
            "        conn.execute('SELECT value FROM items WHERE value = ?', (query,)).fetchall()\n"
            "        conn.execute('SELECT ? AS call_number', (calls[query],)).fetchone()\n"
            "    return {}\n"
            "search_ncs = query_db\n"
            "resolve_ncs_query_scope = query_db\n",
            encoding="utf-8",
        )
        (package / "training_recommendation.py").write_text(
            "def recommend_training_transition(conn, *, query, save):\n"
            "    assert save is False\n"
            "    conn.execute('SELECT value FROM items WHERE value = ?', (query,)).fetchall()\n"
            "    return {}\n",
            encoding="utf-8",
        )
        self.cases = self.root / "cases.json"
        self.out = self.root / "trace.json"

    def _run(self, *args, success=True):
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        else:
            self.assertNotEqual(completed.returncode, 0)
        return completed

    def _trace(self, cases, *, success=True, source=None):
        self.cases.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        return self._run(
            "--source", source or self.source, "--db", self.db,
            "--cases", self.cases, "--out", self.out, success=success,
        )

    def test_trace_uses_selected_source_workloads_warmup_and_ordered_utf8_hash(self) -> None:
        cases = [
            {"id": "search", "workload": "search_edge", "params": {"query": "한글"}},
            {"id": "scope", "workload": "scope_edge", "params": {"query": "O'Brien"}},
            {"id": "transition", "workload": "transition_edge", "params": {"query": "한글", "save": True}},
        ]
        before = self.db.read_bytes()
        self._trace(cases)
        report = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertTrue(report["sql_identity_only"])
        self.assertFalse(report["python_line_coverage_checked"])
        self.assertIn("does not check Python line coverage", report["limitations"])
        self.assertEqual(report["source"], str(self.source.resolve()))
        self.assertEqual(report["warmups_per_case"], 1)
        self.assertEqual(report["traced_calls_per_case"], 1)
        for case, record in zip(cases, report["records"]):
            expected = []
            with closing(sqlite3.connect(self.db)) as conn:
                conn.set_trace_callback(expected.append)
                conn.execute("SELECT value FROM items WHERE value = ?", (case["params"]["query"],)).fetchall()
                if case["workload"] != "transition_edge":
                    conn.execute("SELECT ? AS call_number", (2,)).fetchone()
            self.assertEqual(record["statement_count"], len(expected))
            self.assertEqual(record["sha256"], hashlib.sha256("\n".join(expected).encode("utf-8")).hexdigest())
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(self.source.rglob("*.pyc")), [])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted([self.db.name, "src", "cases.json", "trace.json"]))

    def test_trace_rejects_database_writes(self) -> None:
        before = self.db.read_bytes()
        result = self._trace([
            {"id": "write", "workload": "scope", "params": {"query": "x", "write": True}},
        ], success=False)
        self.assertIn("readonly", result.stderr)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.out.exists())

    def test_real_scope_workload_on_temporary_database(self) -> None:
        from ncs_mcp.db import connect, initialize_database

        conn = connect(self.db)
        try:
            initialize_database(conn)
        finally:
            conn.close()
        before = self.db.read_bytes()
        self._trace([
            {"id": "scope", "workload": "scope", "params": {"query": "unmatched fixture query"}},
        ], source=ROOT / "src")
        first = self.out.read_bytes()
        self._run("--source", ROOT / "src", "--db", self.db, "--cases", self.cases, "--out", self.out)
        self.assertEqual(first, self.out.read_bytes())
        report = json.loads(first)
        self.assertGreater(report["records"][0]["statement_count"], 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_compare_matches_by_id_and_reports_sql_changes_and_counts(self) -> None:
        self._trace([
            {"id": name, "workload": "scope", "params": {"query": name}}
            for name in ("identical", "changed_sql", "changed_count")
        ])
        a = json.loads(self.out.read_text(encoding="utf-8"))
        b = deepcopy(a)
        b["records"][1]["sha256"] = "0" * 64
        b["records"][2]["statement_count"] += 1
        b["records"].reverse()
        other = self.root / "other.json"
        other.write_text(json.dumps(b), encoding="utf-8")
        comparison = self.root / "comparison.json"
        self._run("--compare", self.out, other, "--out", comparison)
        report = json.loads(comparison.read_text(encoding="utf-8"))
        self.assertEqual([record["sql_identical"] for record in report["records"]], [True, False, False])
        self.assertEqual(report["records"][2]["statement_counts"], {"a": 2, "b": 3})
        self.assertEqual(report["sql_identical_count"], 1)
        self.assertEqual(report["sql_different_count"], 2)
        self.assertTrue(report["sql_identity_only"])
        self.assertFalse(report["python_line_coverage_checked"])

    def test_compare_rejects_missing_duplicate_or_mismatched_cases(self) -> None:
        a = {"schema": "ncs_query_sql_trace_v1", "records": [
            {"id": "one", "workload": "scope", "params": {}, "statement_count": 1, "sha256": "0" * 64},
        ]}
        variants = []
        b = deepcopy(a)
        b["records"][0]["id"] = "other"
        variants.append(b)
        b = deepcopy(a)
        b["records"].append(b["records"][0])
        variants.append(b)
        for key, value in (("params", {"query": "different"}), ("workload", "search"), ("sha256", "")):
            b = deepcopy(a)
            b["records"][0][key] = value
            variants.append(b)
        for b in variants:
            with self.subTest(report=b), self.assertRaises(ValueError):
                compare_traces(a, b)

    def test_cli_rejects_incomplete_or_mixed_modes_and_input_overwrite(self) -> None:
        before = self.db.read_bytes()
        for args in (
            ("--source", self.source, "--out", self.out),
            ("--compare", "a.json", "b.json", "--db", self.db, "--out", self.out),
            ("--source", self.source, "--db", self.db, "--cases", self.cases, "--out", self.db),
        ):
            with self.subTest(args=args):
                self._run(*args, success=False)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
