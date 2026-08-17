from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scripts.audit_query_route_contracts import (  # noqa: E402
    RouteAuditCase,
    audit_routes,
    default_cases,
    write_markdown,
)


class QueryRouteContractAuditTests(unittest.TestCase):
    def test_default_cases_pass_without_review_writes(self) -> None:
        payload = audit_routes(default_cases())

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], "ncs_query_route_contract_audit_v1")
        self.assertFalse(payload["status_update_allowed"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])
        self.assertEqual(payload["pass_count"], payload["case_count"])
        self.assertTrue(payload["rows"])

        operator_row = next(row for row in payload["rows"] if row["name"] == "operator_review")
        self.assertFalse(operator_row["actual"]["execution_policy"]["meta_executable"])
        self.assertTrue(
            operator_row["actual"]["execution_policy"]["operator_review_requires_operator_surface"]
        )

    def test_mismatched_expected_tool_fails_closed(self) -> None:
        payload = audit_routes(
            [
                RouteAuditCase(
                    name="bad_expectation",
                    query="인력채용 훈련과정 추천",
                    expected_scenario="task_training",
                    expected_tool="ncs_search",
                    require_save_forced_false=False,
                )
            ]
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failure_count"], 1)
        self.assertTrue(
            any("expected_tool:ncs_search" in issue for issue in payload["rows"][0]["issues"])
        )

    def test_markdown_writer_includes_case_table(self) -> None:
        payload = audit_routes(default_cases()[:1])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit.md"
            write_markdown(path, payload)
            text = path.read_text(encoding="utf-8")

        self.assertIn("ncs_query_route_contract_audit_v1", text)
        self.assertIn("PASS", text)
        self.assertIn("education_system_design", text)


if __name__ == "__main__":
    unittest.main()
