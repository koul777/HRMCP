from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import institutional_chatbot_readiness_report as readiness


EXAMPLES_DIR = ROOT / "docs" / "examples"
EVIDENCE_PATH = EXAMPLES_DIR / "institutional_chatbot_integration_evidence.example.json"
CONTRACT_PATH = EXAMPLES_DIR / "institutional_chatbot_operational_contract.md"

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "credential assignment": re.compile(
        r"(?im)^\s*(?:api[_-]?key|service[_-]?key|password|secret|access[_-]?token)"
        r"\s*[:=]\s*[\"']?(?!$|<|redacted\b|unset\b|none\b)[^\s#\"']{8,}"
    ),
}

TRUSTED_STATUS_ASSIGNMENT = re.compile(
    r'(?i)["\']?(?:review_status|status)["\']?\s*[:=]\s*'
    r'["\']?(?:human_reviewed|accepted|reviewed)["\']?'
)


def _walk_values(value: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            values.append(key)
            values.extend(_walk_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_walk_values(item))
    else:
        values.append(value)
    return values


class InstitutionalChatbotExamplesTests(unittest.TestCase):
    def test_evidence_template_has_every_required_control_and_no_claims(self) -> None:
        payload = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
        readiness.validate_evidence({"institution_integration": payload})

        required_ids = {item["id"] for item in readiness.INTEGRATION_REQUIREMENTS}
        self.assertEqual(set(payload["controls"]), required_ids)
        self.assertTrue(payload["template"])
        self.assertEqual(payload["verification_status"], "unverified")
        self.assertTrue(payload["report_only"])
        self.assertFalse(payload["status_update_allowed"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])

        for control_id, control in payload["controls"].items():
            with self.subTest(control_id=control_id):
                self.assertEqual(control["control_id"], control_id)
                self.assertFalse(control["implemented"])
                self.assertFalse(control["tested"])
                self.assertEqual(control["verification_status"], "unverified")
                self.assertEqual(control["owner"], "")
                self.assertEqual(control["evidence_refs"], [])
                for reference in control["repository_support_refs"]:
                    self.assertTrue((ROOT / reference).is_file(), reference)

    def test_operational_contract_covers_all_control_ids_and_boundaries(self) -> None:
        text = CONTRACT_PATH.read_text(encoding="utf-8")
        required_ids = {item["id"] for item in readiness.INTEGRATION_REQUIREMENTS}

        for control_id in required_ids:
            with self.subTest(control_id=control_id):
                self.assertIn(f"`{control_id}`", text)

        for marker in (
            "model adapter",
            "private NCS MCP",
            "read-only prepared SQLite database",
            "operator path",
            "route fingerprint",
            "retention/deletion",
            "restore drill",
            "rollback drill",
            "incident exercise",
            "repository_support_refs",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_examples_contain_no_embedded_secrets(self) -> None:
        paths = sorted(EXAMPLES_DIR.glob("institutional_chatbot_*"))
        self.assertTrue(paths)

        for path in paths:
            text = path.read_text(encoding="utf-8")
            for label, pattern in SECRET_PATTERNS.items():
                with self.subTest(path=path.name, secret_pattern=label):
                    self.assertIsNone(pattern.search(text))

    def test_examples_do_not_encode_automatic_human_review_statuses(self) -> None:
        payload = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
        values = {value for value in _walk_values(payload) if isinstance(value, str)}
        self.assertTrue({"human_reviewed", "accepted", "reviewed"}.isdisjoint(values))

        for path in sorted(EXAMPLES_DIR.glob("institutional_chatbot_*")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIsNone(TRUSTED_STATUS_ASSIGNMENT.search(text))


if __name__ == "__main__":
    unittest.main()
