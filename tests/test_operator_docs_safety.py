from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


PUBLIC_OPERATOR_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "HARNESS_ENGINEERING.md",
    ROOT / "docs" / "MCP_EXPERIMENT_GUIDE.md",
    ROOT / "docs" / "MCP_RELEASE_CHECKLIST.md",
]


def _harness_command_lines(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    commands: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("python scripts\\ncs_harness.py "):
            commands.append((line_number, stripped))
    return commands


class OperatorDocsSafetyTests(unittest.TestCase):
    def test_qualification_retry_examples_use_rate_limit_guard(self) -> None:
        failures: list[str] = []
        for path in PUBLIC_OPERATOR_DOCS:
            for line_number, command in _harness_command_lines(path):
                if " retry-qualification-errors " not in f" {command} ":
                    continue
                missing = [
                    token
                    for token in [
                        "--stop-after-rate-limit-errors",
                        "--max-retries 1",
                        "--num-of-rows 50",
                        "--max-pages 1",
                    ]
                    if token not in command
                ]
                if missing:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{line_number} missing {', '.join(missing)}"
                    )

        self.assertEqual(failures, [])

    def test_all_unit_qualification_collection_examples_are_batched_and_guarded(self) -> None:
        failures: list[str] = []
        for path in PUBLIC_OPERATOR_DOCS:
            for line_number, command in _harness_command_lines(path):
                if " collect-qualification-items " not in f" {command} ":
                    continue
                if "--all-units" not in command:
                    continue
                missing = [
                    token
                    for token in [
                        "--limit-units",
                        "--stop-after-rate-limit-errors",
                        "--max-retries 1",
                        "--num-of-rows 50",
                        "--max-pages 1",
                    ]
                    if token not in command
                ]
                if missing:
                    failures.append(
                        f"{path.relative_to(ROOT)}:{line_number} missing {', '.join(missing)}"
                    )

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
