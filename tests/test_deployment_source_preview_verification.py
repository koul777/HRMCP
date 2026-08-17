from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import run_deployment_source_preview_smoke as runtime_smoke
from scripts import verify_deployment_source_preview as tree_verify


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _write_runtime_preview(root: Path) -> None:
    package = root / "src" / "ncs_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.1.0'\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'ncs-mcp'\nversion = '0.1.0'\n\n"
        "[project.scripts]\nncs-mcp = 'ncs_mcp.server:main'\n"
        "ncs-institutional-chat = 'ncs_mcp.institutional_chat:main'\n",
        encoding="utf-8",
    )


class DeploymentSourcePreviewTreeVerificationTests(unittest.TestCase):
    def test_rejects_generated_python_build_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "preview"
            build_file = root / "build" / "lib" / "ncs_mcp" / "server.py"
            build_file.parent.mkdir(parents=True)
            build_file.write_text("print('stale')\n", encoding="utf-8")
            export = {
                "ok": True,
                "output_dir": str(root),
                "copied_files": [
                    {
                        "path": "build/lib/ncs_mcp/server.py",
                        "bytes": build_file.stat().st_size,
                        "sha256": _sha256(build_file),
                    }
                ],
            }

            report = tree_verify.verify_preview_tree(export)

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["blocked_path_count"], 1)

    def test_verifies_exported_tree_hashes_and_python_syntax(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "preview"
            source = root / "src" / "app.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('ok')\n", encoding="utf-8")
            export = {
                "ok": True,
                "output_dir": str(root),
                "copied_files": [
                    {
                        "path": "src/app.py",
                        "bytes": source.stat().st_size,
                        "sha256": _sha256(source),
                    }
                ],
            }

            report = tree_verify.verify_preview_tree(export)

        self.assertTrue(report["ok"])
        self.assertEqual(report["file_count"], 1)
        self.assertEqual(report["hash_mismatch_count"], 0)
        self.assertEqual(report["summary"]["compile_error_count"], 0)

    def test_rejects_mutated_extra_and_syntax_invalid_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "preview"
            source = root / "src" / "app.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('ok')\n", encoding="utf-8")
            export = {
                "ok": True,
                "output_dir": str(root),
                "copied_files": [
                    {
                        "path": "src/app.py",
                        "bytes": source.stat().st_size,
                        "sha256": _sha256(source),
                    }
                ],
            }
            source.write_text("def broken(:\n", encoding="utf-8")
            extra = root / "extra.txt"
            extra.write_text("unexpected\n", encoding="utf-8")

            report = tree_verify.verify_preview_tree(export)

        self.assertFalse(report["ok"])
        self.assertEqual(report["hash_mismatch_count"], 1)
        self.assertEqual(report["extra_file_count"], 1)
        self.assertEqual(report["summary"]["compile_error_count"], 1)


class DeploymentSourcePreviewRuntimeSmokeTests(unittest.TestCase):
    def test_preview_smoke_contains_command_side_effects_in_temporary_copies(self) -> None:
        def mutate_command_copy(
            args: list[str],
            *,
            cwd: Path,
            **_: object,
        ) -> dict[str, object]:
            generated = cwd / "reports" / "generated-by-command.txt"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("temporary\n", encoding="utf-8")
            return {
                "command": " ".join(args),
                "returncode": 0,
                "timed_out": False,
            }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_runtime_preview(root)
            before = runtime_smoke._tree_fingerprint(root)
            with patch.object(
                runtime_smoke,
                "run_command",
                side_effect=mutate_command_copy,
            ):
                report = runtime_smoke.run_preview_smoke(root)

            after = runtime_smoke._tree_fingerprint(root)
            original_reports_created = (root / "reports").exists()

        self.assertTrue(report["ok"])
        self.assertTrue(report["source_preview_unchanged"])
        self.assertEqual(before, after)
        self.assertFalse(original_reports_created)

    def test_clean_environment_removes_secret_like_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = os.environ.get("EXAMPLE_PRIVATE_TOKEN")
            os.environ["EXAMPLE_PRIVATE_TOKEN"] = "must-not-propagate"
            try:
                env, removed = runtime_smoke._clean_environment(root, root / "smoke.db")
            finally:
                if previous is None:
                    os.environ.pop("EXAMPLE_PRIVATE_TOKEN", None)
                else:
                    os.environ["EXAMPLE_PRIVATE_TOKEN"] = previous

        self.assertNotIn("EXAMPLE_PRIVATE_TOKEN", env)
        self.assertIn("EXAMPLE_PRIVATE_TOKEN", removed)
        self.assertEqual(env["PYTHONPATH"], str((root / "src").resolve()))

    def test_run_command_captures_success_without_environment_values(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env["NCS_SERVICE_KEY"] = "must-not-appear"
            result = runtime_smoke.run_command(
                [sys.executable, "-c", "print('ok')"],
                cwd=root,
                env=env,
                timeout_seconds=10,
                tail_chars=100,
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout_tail"].strip(), "ok")
        self.assertNotIn("must-not-appear", str(result))

    def test_run_command_records_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            result = runtime_smoke.run_command(
                [sys.executable, "-c", "raise SystemExit(7)"],
                cwd=Path(tmp),
                env=os.environ.copy(),
                timeout_seconds=10,
                tail_chars=100,
            )

        self.assertEqual(result["returncode"], 7)
        self.assertFalse(result["timed_out"])

    def test_run_command_timeout_terminates_child_process_tree(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_path = root / "child.pid"
            script = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            result = runtime_smoke.run_command(
                [sys.executable, "-c", script],
                cwd=root,
                env=os.environ.copy(),
                timeout_seconds=1,
                tail_chars=100,
            )
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while _pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.1)

        self.assertEqual(result["returncode"], 124)
        self.assertTrue(result["timed_out"])
        self.assertFalse(_pid_exists(child_pid))

    def test_preview_smoke_runs_all_commands_after_failure(self) -> None:
        command_results = [
            {
                "command": f"command-{index}",
                "returncode": returncode,
                "timed_out": returncode == 124,
            }
            for index, returncode in enumerate((0, 1, 0, 124, 0, 0, 0, 0), start=1)
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_runtime_preview(root)
            original_fingerprint = runtime_smoke._tree_fingerprint(root)
            with patch.object(
                runtime_smoke,
                "run_command",
                side_effect=command_results,
            ) as run_mock:
                report = runtime_smoke.run_preview_smoke(root)

        self.assertFalse(report["ok"])
        self.assertEqual(run_mock.call_count, 8)
        self.assertEqual(report["command_count"], 8)
        self.assertEqual(report["failed_command_count"], 2)
        self.assertEqual(report["timed_out_command_count"], 1)
        self.assertTrue(report["source_preview_unchanged"])
        self.assertEqual(report["source_preview_before"], original_fingerprint)
        self.assertEqual(report["source_preview_after"], original_fingerprint)
        self.assertTrue(report["temporary_source_copies_cleaned_up"])
        self.assertEqual(
            [item["returncode"] for item in report["commands"]],
            [0, 1, 0, 124, 0, 0, 0, 0],
        )
        command_args = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(command_args[0][1:3], ["-m", "ncs_mcp.smoke_data"])
        self.assertEqual(command_args[1][-2:], ["scripts/ncs_harness.py", "lint"])
        self.assertEqual(command_args[2][1:4], ["-m", "unittest", "tests.test_query_router"])
        self.assertEqual(command_args[3][-2:], ["--timeout", "15"])
        self.assertIn("mcp_http_health_smoke.py", command_args[4][1])
        self.assertEqual(command_args[4][-2:], ["--timeout", "20"])
        self.assertIn("institutional_chat_smoke.py", command_args[5][1])
        self.assertEqual(command_args[5][-2:], ["--timeout-seconds", "60"])
        self.assertIn("package_install_smoke.py", command_args[6][1])
        self.assertEqual(command_args[6][2:4], ["--source-preview-dir", "."])
        self.assertEqual(command_args[7][-2:], ["scripts/ncs_harness.py", "smoke"])
        call_cwds = [call.kwargs["cwd"] for call in run_mock.call_args_list]
        self.assertTrue(all(path != root for path in call_cwds))
        self.assertEqual(call_cwds[0], call_cwds[5])
        self.assertNotEqual(call_cwds[5], call_cwds[6])

    def test_preview_smoke_rejects_blocked_env_before_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("NCS_SERVICE_KEY=unsafe\n", encoding="utf-8")
            with patch.object(runtime_smoke, "run_command") as run_mock:
                report = runtime_smoke.run_preview_smoke(root)

        self.assertFalse(report["ok"])
        self.assertFalse(report["preflight_scan_ok"])
        self.assertEqual(report["command_count"], 0)
        self.assertGreater(report["preflight_scan"]["blocked_name_finding_count"], 0)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
