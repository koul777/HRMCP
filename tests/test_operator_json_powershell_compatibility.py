from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.audit_operator_json_powershell_compatibility import (  # noqa: E402
    build_audit,
    discover_json_artifacts_from_handoff,
    main as compatibility_main,
)


def _powershell_ok(path: Path, powershell_exe: str | None, timeout_seconds: int) -> dict[str, Any]:
    return {
        "available": True,
        "ok": True,
        "status": "pass",
        "powershell_executable": powershell_exe or "powershell",
        "exit_code": 0,
        "stdout_tail": "OK",
        "stderr_tail": "",
    }


def _powershell_failed(path: Path, powershell_exe: str | None, timeout_seconds: int) -> dict[str, Any]:
    return {
        "available": True,
        "ok": False,
        "status": "parse_failed",
        "powershell_executable": powershell_exe or "powershell",
        "exit_code": 1,
        "stdout_tail": "",
        "stderr_tail": "ConvertFrom-Json failed",
    }


class OperatorJsonPowerShellCompatibilityTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        release = self._write_json(
            reports / "aihr_release_readiness_fixture.json",
            {"schema": "release_readiness_v1", "ok": True},
        )
        queue_run = self._write_json(
            reports / "aihr_agent_queue_run_fixture.json",
            {"schema": "aihr_agent_queue_run_v1", "ok": True},
        )
        md = reports / "operator_note_fixture.md"
        md.write_text("# note\n", encoding="utf-8")
        handoff = self._write_json(
            reports / "overnight_10h_operator_handoff_fixture.json",
            {
                "schema": "overnight_10h_operator_handoff_v3",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "canonical_artifacts": [
                    {"path": release.relative_to(root).as_posix()},
                    {"path": queue_run.relative_to(root).as_posix()},
                    {"path": queue_run.relative_to(root).as_posix()},
                    {"path": md.relative_to(root).as_posix()},
                ],
            },
        )
        return {
            "handoff": handoff,
            "release": release,
            "queue_run": queue_run,
            "markdown": md,
        }

    def test_discovers_handoff_json_artifacts_and_happy_path_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            artifacts = discover_json_artifacts_from_handoff(paths["handoff"], root=root)
            report = build_audit(
                artifacts=artifacts,
                handoff_path=paths["handoff"],
                root=root,
                powershell_runner=_powershell_ok,
            )

        self.assertEqual(
            ["aihr_release_readiness_fixture.json", "aihr_agent_queue_run_fixture.json"],
            [path.name for path in artifacts],
        )
        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertEqual(2, report["artifact_count"])
        self.assertEqual(2, report["python_json_ok_count"])
        self.assertEqual(2, report["powershell_convertfrom_json_ok_count"])
        self.assertEqual(0, report["python_ok_powershell_failed_count"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertTrue(report["human_decision_required"])

    def test_python_valid_powershell_failure_is_separate_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_audit(
                artifacts=[paths["release"]],
                handoff_path=paths["handoff"],
                root=root,
                powershell_runner=_powershell_failed,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["python_json_ok_count"])
        self.assertEqual(0, report["powershell_convertfrom_json_ok_count"])
        self.assertEqual(1, report["python_ok_powershell_failed_count"])
        self.assertEqual("powershell_convertfrom_json_failed", report["findings"][0]["rule_code"])
        self.assertTrue(report["artifacts"][0]["python_json_ok"])

    def test_python_json_parse_failure_stops_before_powershell(self) -> None:
        calls: list[Path] = []

        def runner(path: Path, powershell_exe: str | None, timeout_seconds: int) -> dict[str, Any]:
            calls.append(path)
            return _powershell_ok(path, powershell_exe, timeout_seconds)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "reports" / "broken.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not-json", encoding="utf-8")
            report = build_audit(
                artifacts=[path],
                root=root,
                powershell_runner=runner,
            )

        self.assertFalse(report["ok"])
        self.assertEqual([], calls)
        self.assertEqual("python_json_parse_failed", report["findings"][0]["rule_code"])
        self.assertFalse(report["artifacts"][0]["python_json_ok"])

    def test_cli_writes_json_and_markdown_with_injected_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "operator_json_powershell_compatibility.json"
            md = root / "reports" / "operator_json_powershell_compatibility.md"
            argv = [
                "--root",
                str(root),
                "--operator-handoff",
                str(paths["handoff"].relative_to(root)),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = compatibility_main(argv, powershell_runner=_powershell_ok)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("Operator JSON PowerShell Compatibility Audit", markdown)
        self.assertIn("approval_claim: false", markdown)


if __name__ == "__main__":
    unittest.main()
