from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import package_install_smoke as package_smoke


def _write_preview(root: Path) -> None:
    package_dir = root / "src" / "ncs_mcp"
    package_dir.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'setuptools.build_meta'\n\n"
        "[project]\nname = 'ncs-mcp'\nversion = '0.1.0'\n\n"
        "[project.scripts]\nncs-mcp = 'ncs_mcp.server:main'\n"
        "ncs-institutional-chat = 'ncs_mcp.institutional_chat:main'\n\n"
        "[tool.setuptools.packages.find]\nwhere = ['src']\n",
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text(
        "__version__ = '0.1.0'\n",
        encoding="utf-8",
    )
    (package_dir / "server.py").write_text(
        "import argparse\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser(prog='ncs-mcp')\n"
        "    parser.add_argument('--transport', default='stdio')\n"
        "    parser.parse_args()\n",
        encoding="utf-8",
    )
    (package_dir / "institutional_chat.py").write_text(
        "import argparse\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser(prog='ncs-institutional-chat')\n"
        "    parser.add_argument('--auth-mode', default='local')\n"
        "    parser.add_argument('--allow-remote-bind', action='store_true')\n"
        "    parser.parse_args()\n",
        encoding="utf-8",
    )


def _write_repository_root(root: Path) -> Path:
    repository_root = root / "repository"
    reports_dir = repository_root / "reports"
    processed_dir = repository_root / "data" / "processed"
    reports_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    (reports_dir / "existing-report.json").write_text(
        '{"status": "existing"}\n',
        encoding="utf-8",
    )
    (processed_dir / "existing.db").write_text(
        "existing-db-sentinel\n",
        encoding="utf-8",
    )
    return repository_root


def _command_result(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, object]:
    return {
        "argv": args,
        "command": " ".join(args),
        "returncode": returncode,
        "timed_out": False,
        "duration_seconds": 0.01,
        "stdout_tail": stdout,
        "stdout_truncated": False,
        "stderr_tail": stderr,
        "stderr_truncated": False,
    }


def _write_fake_entrypoints(target: Path) -> None:
    for name in package_smoke.REQUIRED_CONSOLE_SCRIPTS:
        entrypoint = package_smoke._entrypoint_candidates(target, name)[0]
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("generated entrypoint\n", encoding="utf-8")


def _help_result(args: list[str]) -> dict[str, object]:
    if "ncs-institutional-chat" in Path(args[0]).name:
        return _command_result(
            args,
            stdout=(
                "usage: ncs-institutional-chat [-h] [--auth-mode AUTH_MODE] "
                "[--allow-remote-bind]\n"
            ),
        )
    return _command_result(
        args,
        stdout="usage: ncs-mcp [-h] [--transport TRANSPORT]\n",
    )


class PackageInstallSmokeEnvironmentTests(unittest.TestCase):
    def test_safe_environment_removes_secrets_network_and_active_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = {
                "EXAMPLE_PRIVATE_TOKEN": "do-not-propagate",
                "https_proxy": "http://credential@example.invalid",
                "NCS_DB_PATH": "active.db",
                "NCS_REPORTS_DIR": "active-reports",
                "PYTHONPATH": "active-source",
                "SAFE_VALUE": "preserved",
            }
            with patch.dict(os.environ, original, clear=True):
                env, safety = package_smoke.build_safe_environment(
                    target_dir=root / "target",
                    temporary_db_path=root / "sentinel" / "ncs.db",
                    temporary_reports_dir=root / "reports",
                )

        self.assertNotIn("EXAMPLE_PRIVATE_TOKEN", env)
        self.assertNotIn("https_proxy", env)
        self.assertEqual(env["SAFE_VALUE"], "preserved")
        self.assertEqual(env["NCS_MCP_READ_ONLY"], "1")
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertIn("EXAMPLE_PRIVATE_TOKEN", safety["secret_like_names_removed"])
        self.assertIn(
            "HTTPS_PROXY",
            [name.upper() for name in safety["network_names_removed"]],
        )
        self.assertIn("NCS_DB_PATH", safety["runtime_path_names_removed"])
        self.assertNotIn("do-not-propagate", json.dumps(safety))


class PackageInstallSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows console layout only")
    def test_entrypoint_discovery_includes_real_windows_scripts_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            expected = target / "Scripts" / "ncs-institutional-chat.exe"
            expected.parent.mkdir(parents=True)
            expected.write_text("generated entrypoint\n", encoding="utf-8")

            found = next(
                (
                    path
                    for path in package_smoke._entrypoint_candidates(
                        target,
                        "ncs-institutional-chat",
                    )
                    if path.is_file()
                ),
                None,
            )

        self.assertEqual(found, expected)

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_preflight_rejects_windows_directory_junction(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            outside = root / "outside"
            _write_preview(preview)
            outside.mkdir()
            (outside / "private.txt").write_text("outside\n", encoding="utf-8")
            junction = preview / "linked-outside"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("directory junction creation is unavailable")

            preflight = package_smoke._preflight(preview)

        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["junction_paths"], ["linked-outside"])
        self.assertEqual(preflight["unsafe_link_count"], 1)
        self.assertEqual(preflight["file_count"], 0)
        self.assertFalse(preflight["within_size_limits"])

    def test_preflight_requires_both_public_console_scripts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_preview(root)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                pyproject.read_text(encoding="utf-8").replace(
                    "ncs-institutional-chat = 'ncs_mcp.institutional_chat:main'\n",
                    "",
                ),
                encoding="utf-8",
            )

            preflight = package_smoke._preflight(root)

        self.assertFalse(preflight["ok"])
        self.assertEqual(
            preflight["missing_required_console_scripts"],
            ["ncs-institutional-chat"],
        )

    def test_preflight_rejects_repository_root_before_copying_generated_trees(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_preview(root)
            (root / ".git").mkdir()
            (root / "reports").mkdir()
            (root / "data").mkdir()

            preflight = package_smoke._preflight(root)

        self.assertFalse(preflight["ok"])
        self.assertEqual(
            preflight["blocked_top_level_paths"],
            [".git", "data", "reports"],
        )
        self.assertEqual(preflight["file_count"], 0)
        self.assertFalse(preflight["within_size_limits"])

    def test_runs_offline_install_import_and_generated_entrypoint_help(self) -> None:
        temporary_root: Path | None = None

        def fake_run(
            args: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
            tail_chars: int,
        ) -> dict[str, object]:
            nonlocal temporary_root
            self.assertNotIn("EXAMPLE_PRIVATE_TOKEN", env)
            self.assertEqual(env["PIP_NO_INDEX"], "1")
            if "pip" in args:
                target = Path(args[args.index("--target") + 1])
                temporary_root = target.parent
                build_source = Path(args[-1])
                (build_source / "temporary-build-marker.txt").write_text(
                    "allowed temporary source-copy change\n",
                    encoding="utf-8",
                )
                package_dir = target / "ncs_mcp"
                package_dir.mkdir(parents=True)
                (package_dir / "__init__.py").write_text("", encoding="utf-8")
                _write_fake_entrypoints(target)
                return _command_result(args, stdout="Successfully installed ncs-mcp\n")
            if len(args) > 1 and args[1] == "-c":
                target = Path(args[-1]).resolve()
                probe = {
                    "module": "ncs_mcp",
                    "version": "0.1.0",
                    "package_file": str(target / "ncs_mcp" / "__init__.py"),
                    "from_target": True,
                }
                return _command_result(args, stdout=json.dumps(probe) + "\n")
            return _help_result(args)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            _write_preview(preview)
            with patch.dict(
                os.environ,
                {"EXAMPLE_PRIVATE_TOKEN": "do-not-propagate"},
                clear=False,
            ), patch.object(package_smoke, "run_command", side_effect=fake_run):
                report = package_smoke.run_package_install_smoke(
                    preview,
                    repository_root=repository_root,
                )

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["active_db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["external_network_allowed"])
        self.assertTrue(report["temporary_workspace_cleaned_up"])
        self.assertEqual(report["summary"]["executed_command_count"], 4)
        self.assertTrue(report["checks"]["import_from_temporary_target"])
        self.assertTrue(report["checks"]["entrypoint_help_ok"])
        self.assertTrue(report["checks"]["institutional_chat_entrypoint_help_ok"])
        self.assertTrue(report["checks"]["protected_paths_unchanged"])
        self.assertFalse(report["active_environment_modified"])
        self.assertFalse(report["source_preview_modified"])
        self.assertFalse(
            report["protected_paths"]["temporary_source_preview_copy"]["protected"]
        )
        self.assertTrue(
            report["protected_paths"]["temporary_source_preview_copy"][
                "changes_allowed"
            ]
        )
        self.assertIsNotNone(temporary_root)
        self.assertFalse(temporary_root.exists())

        install_argv = report["steps"]["install"]["argv"]
        self.assertIn("--no-deps", install_argv)
        self.assertIn("--no-index", install_argv)
        self.assertIn("--no-build-isolation", install_argv)
        self.assertIn("--target", install_argv)

    def test_external_protected_path_changes_fail_the_smoke(self) -> None:
        def mutate_external_paths(
            args: list[str],
            **_: object,
        ) -> dict[str, object]:
            if "pip" in args:
                (preview / "pyproject.toml").write_text(
                    (preview / "pyproject.toml").read_text(encoding="utf-8")
                    + "# modified outside temporary workspace\n",
                    encoding="utf-8",
                )
                (repository_root / "reports" / "created-report.json").write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                (repository_root / "data" / "processed" / "existing.db").unlink()

                target = Path(args[args.index("--target") + 1])
                package_dir = target / "ncs_mcp"
                package_dir.mkdir(parents=True)
                (package_dir / "__init__.py").write_text("", encoding="utf-8")
                _write_fake_entrypoints(target)
                return _command_result(args, stdout="Successfully installed ncs-mcp\n")
            if len(args) > 1 and args[1] == "-c":
                target = Path(args[-1]).resolve()
                return _command_result(
                    args,
                    stdout=json.dumps(
                        {
                            "module": "ncs_mcp",
                            "version": "0.1.0",
                            "package_file": str(
                                target / "ncs_mcp" / "__init__.py"
                            ),
                            "from_target": True,
                        }
                    )
                    + "\n",
                )
            return _help_result(args)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            _write_preview(preview)
            with patch.object(
                package_smoke,
                "run_command",
                side_effect=mutate_external_paths,
            ):
                report = package_smoke.run_package_install_smoke(
                    preview,
                    repository_root=repository_root,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["checks"]["install_ok"])
        self.assertTrue(report["checks"]["entrypoint_help_ok"])
        self.assertTrue(report["checks"]["institutional_chat_entrypoint_help_ok"])
        self.assertFalse(report["checks"]["protected_paths_unchanged"])
        self.assertTrue(report["active_environment_modified"])
        self.assertTrue(report["source_preview_modified"])
        self.assertTrue(report["active_db_writes"])
        self.assertTrue(report["db_writes"])
        self.assertEqual(report["db_write_scope"], "active_data_processed_modified")
        self.assertTrue(report["temporary_workspace_cleaned_up"])

        protected = report["protected_paths"]["paths"]
        self.assertGreater(protected["source_preview_original"]["modified_count"], 0)
        self.assertGreater(protected["repository_reports"]["created_count"], 0)
        self.assertGreater(
            protected["repository_data_processed"]["deleted_count"],
            0,
        )
        self.assertIn(
            "ProtectedPathModified",
            {item["type"] for item in report["errors"]},
        )

    def test_install_failure_skips_import_and_cleans_workspace(self) -> None:
        temporary_root: Path | None = None

        def fail_install(
            args: list[str],
            **_: object,
        ) -> dict[str, object]:
            nonlocal temporary_root
            temporary_root = Path(args[args.index("--target") + 1]).parent
            return _command_result(args, returncode=1, stderr="local build failed\n")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            _write_preview(preview)
            with patch.object(package_smoke, "run_command", side_effect=fail_install) as run_mock:
                report = package_smoke.run_package_install_smoke(
                    preview,
                    repository_root=repository_root,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(report["steps"]["import"]["status"], "skipped")
        self.assertEqual(report["steps"]["entrypoint_help"]["status"], "skipped")
        self.assertEqual(
            report["steps"]["institutional_chat_entrypoint_help"]["status"],
            "skipped",
        )
        self.assertTrue(report["temporary_workspace_cleaned_up"])
        self.assertIsNotNone(temporary_root)
        self.assertFalse(temporary_root.exists())

    def test_real_smoke_uses_no_index_and_needs_no_network(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            _write_preview(preview)
            report = package_smoke.run_package_install_smoke(
                preview,
                timeout_seconds=60,
                repository_root=repository_root,
            )

        self.assertTrue(report["ok"], report["steps"])
        self.assertTrue(report["offline_install"])
        self.assertTrue(report["pip_no_deps"])
        self.assertTrue(report["pip_no_index"])
        self.assertTrue(report["checks"]["install_ok"])
        self.assertTrue(report["checks"]["import_from_temporary_target"])
        self.assertTrue(report["checks"]["entrypoint_help_ok"])
        self.assertTrue(report["checks"]["institutional_chat_entrypoint_help_ok"])
        self.assertTrue(report["checks"]["temporary_db_absent"])
        self.assertTrue(report["checks"]["cleanup_ok"])

    def test_temporary_db_write_is_detected_without_touching_active_db(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            active_db = root / "active.db"
            _write_preview(preview)
            active_db.write_text("active-db-sentinel\n", encoding="utf-8")

            def write_temporary_db(
                args: list[str],
                *,
                env: dict[str, str],
                **_: object,
            ) -> dict[str, object]:
                isolated_db = Path(env["NCS_DB_PATH"])
                self.assertNotEqual(isolated_db.resolve(), active_db.resolve())
                isolated_db.parent.mkdir(parents=True)
                isolated_db.write_text("temporary-db-write\n", encoding="utf-8")
                return _command_result(args, returncode=1)

            with patch.dict(
                os.environ,
                {"NCS_DB_PATH": str(active_db)},
                clear=False,
            ), patch.object(
                package_smoke,
                "run_command",
                side_effect=write_temporary_db,
            ):
                report = package_smoke.run_package_install_smoke(
                    preview,
                    repository_root=repository_root,
                )

            self.assertEqual(
                active_db.read_text(encoding="utf-8"),
                "active-db-sentinel\n",
            )

        self.assertFalse(report["ok"])
        self.assertFalse(report["active_db_writes"])
        self.assertTrue(report["temporary_db_writes"])
        self.assertTrue(report["db_writes"])
        self.assertEqual(report["db_write_scope"], "ephemeral_sentinel_only")
        self.assertFalse(report["checks"]["temporary_db_absent"])
        self.assertTrue(report["checks"]["cleanup_ok"])

    def test_invalid_preview_returns_structured_report_without_subprocess(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            preview.mkdir()
            with patch.object(package_smoke, "run_command") as run_mock:
                report = package_smoke.run_package_install_smoke(
                    preview,
                    repository_root=repository_root,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "invalid_source_preview")
        self.assertFalse(report["preflight"]["pyproject_exists"])
        self.assertEqual(report["summary"]["executed_command_count"], 0)
        self.assertTrue(report["report_only"])
        run_mock.assert_not_called()

    def test_rejects_symlinked_preview_content(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "preview"
            repository_root = _write_repository_root(root)
            _write_preview(preview)
            target = Path(tmp) / "outside.txt"
            target.write_text("outside\n", encoding="utf-8")
            link = preview / "linked.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")

            report = package_smoke.run_package_install_smoke(
                preview,
                repository_root=repository_root,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "invalid_source_preview")
        self.assertEqual(report["preflight"]["symlink_count"], 1)

    def test_run_command_is_bounded_and_does_not_report_environment_values(self) -> None:
        with TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["EXAMPLE_PRIVATE_TOKEN"] = "must-not-appear"
            result = package_smoke.run_command(
                [sys.executable, "-c", "print('ok')"],
                cwd=Path(tmp),
                env=env,
                timeout_seconds=10,
                tail_chars=100,
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout_tail"].strip(), "ok")
        self.assertNotIn("must-not-appear", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
