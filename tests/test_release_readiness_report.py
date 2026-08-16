from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_readiness_report import build_release_readiness, main


class ReleaseReadinessReportTests(unittest.TestCase):
    def test_build_release_readiness_separates_hygiene_from_release_blockers(self) -> None:
        quality_report = {
            "status": "warn",
            "summary": {"fail_count": 0, "warn_count": 1},
            "gates": [
                {
                    "name": "review_debt:human_reviewed_concepts",
                    "status": "warn",
                    "message": "human_reviewed_concepts is still zero.",
                    "value": 0,
                    "threshold": "> 0",
                },
                {
                    "name": "qualification:collection_coverage",
                    "status": "warn",
                    "message": "Qualification coverage is low.",
                    "value": 0.5,
                    "threshold": "warn < 0.90",
                },
            ],
        }
        contract = {"surface": {"active_tool_count": 10, "operator_tool_count": 0}}

        report = build_release_readiness(quality_report, contract)

        self.assertTrue(report["engineering_hygiene_ok"])
        self.assertFalse(report["release_ready"])
        self.assertEqual(
            {blocker["name"] for blocker in report["blockers"]},
            {
                "review_debt:human_reviewed_concepts",
                "qualification:collection_coverage",
                "trusted_transition_scenarios",
            },
        )

    def test_main_writes_same_json_shape_that_it_prints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            contract_path = tmp_path / "contract.json"
            out_path = tmp_path / "readiness.json"
            markdown_path = tmp_path / "readiness.md"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": [
                            {
                                "name": "transition_eval:trusted_scenarios",
                                "status": "pass",
                                "value": 10,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(
                    {"surface": {"active_tool_count": 10, "operator_tool_count": 0}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--out",
                        str(out_path),
                        "--markdown-out",
                        str(markdown_path),
                    ]
                )

            printed = json.loads(stdout.getvalue())
            written = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(written, printed)
        self.assertEqual(written["markdown_path"], str(markdown_path))
        self.assertTrue(written["release_ready"])


if __name__ == "__main__":
    unittest.main()
