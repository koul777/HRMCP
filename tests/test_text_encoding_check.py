from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_text_encoding  # noqa: E402


class TextEncodingCheckTests(unittest.TestCase):
    def test_current_repository_text_encoding_check_passes(self) -> None:
        self.assertEqual(check_text_encoding.main(), 0)

    def test_check_text_file_detects_mojibake_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.md"
            path.write_text("NCS ?덈젴 異붿쿇\n", encoding="utf-8")

            issues = check_text_encoding.check_text_file(path, ["NCS"])

        self.assertEqual(len(issues), 1)
        self.assertIn("contains mojibake marker", issues[0])

    def test_check_contract_requires_current_korean_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "contract.json"
            path.write_text(
                json.dumps(
                    {
                        "surface": {"active_tool_count": 7},
                        "tools": [
                            {"name": "ncs_training", "aliases": ["training"]},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            issues = check_text_encoding.check_contract(path)

        self.assertTrue(any("ncs_training aliases do not include '훈련'" in issue for issue in issues))

    def test_check_contract_requires_public_active_tool_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "contract.json"
            path.write_text(
                json.dumps(
                    {
                        "surface": {"active_tool_count": 11},
                        "tools": [
                            {"name": "ncs_training", "aliases": ["훈련", "교육"]},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            issues = check_text_encoding.check_contract(path)

        self.assertTrue(any("active_tool_count is not 7" in issue for issue in issues))

    def test_main_returns_nonzero_for_missing_expected_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "docs").mkdir()
            (root / "mcp").mkdir()
            (root / "README.md").write_text("NCS MCP\n", encoding="utf-8")
            (root / "docs" / "NCS_MCP_USER_GUIDE_KO.md").write_text(
                "NCS 훈련 추천 MCP 사용자 가이드\n경력개발\n직무 전환\n",
                encoding="utf-8",
            )
            (root / "docs" / "MCP_RELEASE_CHECKLIST.md").write_text(
                "API keys\nDocker\n/ready\n",
                encoding="utf-8",
            )
            (root / "mcp" / "ncs-tool-contract.json").write_text(
                json.dumps(
                    {
                        "surface": {"active_tool_count": 11},
                        "tools": [
                            {"name": "ncs_training", "aliases": ["훈련"]},
                            {"name": "recommend_training_transition", "aliases": ["직무 전환"]},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(check_text_encoding, "ROOT", root):
                result = check_text_encoding.main()

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
