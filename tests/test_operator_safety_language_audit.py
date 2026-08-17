from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_operator_safety_language import audit_paths, write_markdown  # noqa: E402


class OperatorSafetyLanguageAuditTests(unittest.TestCase):
    def test_detects_guarded_api_command_when_api_calls_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "queue.md"
            path.write_text(
                "api_call_allowed_now: false\n"
                "command: python scripts\\ncs_harness.py retry-qualification-errors\n",
                encoding="utf-8",
            )

            payload = audit_paths([path])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["finding_count"], 1)
        self.assertEqual(payload["findings"][0]["code"], "guarded_api_command_presented")
        self.assertEqual(payload["findings"][0]["severity"], "high")
        self.assertFalse(payload["status_update_allowed"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])

    def test_detects_definition_review_action_wording(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review_pack.csv"
            path.write_text(
                "concept_id,recommended_review_action\n"
                "1,write_manual_definition_from_task_evidence\n",
                encoding="utf-8",
            )

            payload = audit_paths([path])

        codes = {finding["code"] for finding in payload["findings"]}
        self.assertIn("definition_review_action_promotional_wording", codes)

    def test_safe_wording_passes_and_markdown_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "safe.md"
            source.write_text(
                "audit-backed trusted rows: 0\n"
                "pending reconfirmation rows: 34\n"
                "review_assist_only_no_db_write\n",
                encoding="utf-8",
            )
            markdown = Path(tmpdir) / "audit.md"

            payload = audit_paths([source])
            write_markdown(markdown, payload)
            text = markdown.read_text(encoding="utf-8")

        self.assertTrue(payload["ok"])
        self.assertIn("operator_safety_language_audit_v1", text)
        self.assertIn("No risky", text)


if __name__ == "__main__":
    unittest.main()
