from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import check_deployment_source_boundary as boundary
from scripts import build_deployment_source_manifest as manifest
from scripts import export_deployment_source_preview as export_preview
from scripts import summarize_preview_release_evidence as preview_summary


ROOT = boundary.ROOT


class DeploymentSourceBoundaryTests(unittest.TestCase):
    def test_tracked_path_reason_allows_templates_and_gitkeep(self) -> None:
        self.assertIsNone(boundary.tracked_path_reason(".env.example"))
        self.assertIsNone(boundary.tracked_path_reason("data/raw/.gitkeep"))
        self.assertIsNone(boundary.tracked_path_reason("data/processed/.gitkeep"))
        self.assertIsNone(boundary.tracked_path_reason("reports/.gitkeep"))

    def test_tracked_path_reason_blocks_generated_artifacts(self) -> None:
        cases = {
            ".env": "local secret configuration",
            "data/raw/ncs.xlsx": "raw source download",
            "data/processed/ncs.db": "generated SQLite or data artifact",
            "reports/aihr_release.json": "generated report or copied reference artifact",
            "exports/ncs.jsonld": "generated export artifact",
            "tmp/scratch.txt": "temporary working artifact",
            "src/ncs_mcp.egg-info/PKG-INFO": "generated Python package metadata",
            "ncs_mcp.egg-info/PKG-INFO": "generated Python package metadata",
            "build/lib/ncs_mcp/server.py": "generated Python build artifact",
            "dist/ncs_mcp-0.1.0.whl": "generated Python distribution artifact",
            ".codex/config.toml": "local Codex configuration",
            ".mcp.json": "local MCP configuration",
            "data/ocr/tessdata/kor.traineddata": "OCR model artifact",
            "docs/reference/ncs_hrd_guide_codex_readable.md": "converted HRD guide reference artifact",
            "docs/reference/ncs_hrd_guide_reference.index.json": "generated HRD guide reference artifact",
            "docs/reference/ncs_hrd_guide_reference.chunks.jsonl": "generated HRD guide reference artifact",
            ".venv/pyvenv.cfg": "virtual environment",
            "venv/pyvenv.cfg": "virtual environment",
            "__pycache__/x.pyc": "python bytecode cache",
            ".pytest_cache/v/cache": "test cache",
            ".ruff_cache/content": "tool cache",
            ".mypy_cache/meta.json": "tool cache",
            "logs/build.log": "log artifact",
            "src/ncs_mcp/__pycache__/server.cpython-311.pyc": "python bytecode cache",
        }
        for path, expected_reason in cases.items():
            with self.subTest(path=path):
                self.assertEqual(boundary.tracked_path_reason(path), expected_reason)

    def test_find_tracked_blockers_normalizes_windows_paths(self) -> None:
        blockers = boundary.find_tracked_blockers(
            [
            "README.md",
            r"data\\processed\\ncs.db",
            r"reports\\release.md",
            r".codex\\config.toml",
            ".env.example",
        ]
        )
        self.assertEqual(
            blockers,
            [
                {
                    "path": "data/processed/ncs.db",
                    "reason": "generated SQLite or data artifact",
                },
                {
                    "path": "reports/release.md",
                    "reason": "generated report or copied reference artifact",
                },
                {
                    "path": ".codex/config.toml",
                    "reason": "local Codex configuration",
                },
            ],
        )

    def test_stdio_launcher_respects_existing_db_path(self) -> None:
        launcher = (ROOT / "run_ncs_mcp_stdio.cmd").read_text(encoding="utf-8")
        self.assertIn('if "%NCS_DB_PATH%"=="" set "NCS_DB_PATH=', launcher)
        self.assertNotIn('\nset "NCS_DB_PATH=%ROOT%data\\processed\\ncs.db"', launcher)

    def test_env_example_does_not_advertise_unused_openai_secret(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("OPENAI_API_KEY", env_example)

    def test_manifest_source_summary_buckets_paths(self) -> None:
        self.assertEqual(
            manifest._source_summary(
                [
                    "README.md",
                    "src/ncs_mcp/server.py",
                    "scripts/ncs_harness.py",
                    "tests/test_ncs_mcp.py",
                    "docs/MCP_RELEASE_CHECKLIST.md",
                    "mcp/ncs-tool-contract.json",
                    ".github/workflows/ci.yml",
                    ".agents/README.md",
                    "run_ncs_mcp_stdio.cmd",
                ]
            ),
            {
                "root": 2,
                "src": 1,
                "scripts": 1,
                "tests": 1,
                "docs": 1,
                "mcp": 1,
                "github": 1,
                "agents": 1,
                "other": 0,
            },
        )

    def test_source_boundary_fails_when_lfs_history_check_errors(self) -> None:
        with (
            patch.object(boundary, "list_tracked_paths", return_value=(["README.md"], [])),
            patch.object(boundary, "check_ignore_expectations", return_value=[]),
            patch.object(boundary, "check_attribute_expectations", return_value=[]),
            patch.object(boundary, "list_lfs_history_paths", return_value=([], ["git lfs failed"])),
        ):
            report = boundary.build_report(check_lfs_history=True, fail_on_lfs_history=False)
        self.assertFalse(report["ok"])
        self.assertEqual(report["lfs_history_errors"], ["git lfs failed"])

    def test_source_boundary_can_fail_on_lfs_history_blockers(self) -> None:
        with (
            patch.object(boundary, "list_tracked_paths", return_value=(["README.md"], [])),
            patch.object(boundary, "check_ignore_expectations", return_value=[]),
            patch.object(boundary, "check_attribute_expectations", return_value=[]),
            patch.object(
                boundary,
                "list_lfs_history_paths",
                return_value=(["README.md", "data/processed/ncs.db"], []),
            ),
        ):
            report = boundary.build_report(check_lfs_history=True, fail_on_lfs_history=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["lfs_history_blocker_count"], 1)
        self.assertEqual(
            report["lfs_history_blockers"],
            [{"path": "data/processed/ncs.db", "reason": "generated SQLite or data artifact"}],
        )

    def test_manifest_fails_when_git_queries_error_or_untracked_blockers_exist(self) -> None:
        with patch.object(
            manifest,
            "_git_lines",
            side_effect=[
                (["README.md"], ["tracked query failed"]),
                (["src/ncs_mcp.egg-info/PKG-INFO"], []),
            ],
        ):
            report = manifest.build_manifest()
        self.assertFalse(report["ok_for_preview_commit"])
        self.assertEqual(report["summary"]["untracked_blocker_count"], 1)
        self.assertEqual(report["errors"], ["tracked query failed"])

    def test_preview_summary_requires_source_boundary_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            release_path.write_text(
                '{"release_decision":{"blocked_by":[]},"release_ready":false,'
                '"engineering_hygiene_ok":true,"blocker_count":0}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                '{"ok":true,"checks":[{"name":"static_artifacts","ok":true}],'
                '"review_chain_safety_summary":'
                '{"do_not_set_human_reviewed_accepted_reviewed_automatically":true}}',
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":'
                '{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=None,
            )
        self.assertFalse(report["ok"])
        self.assertIn("missing_source_boundary_evidence", report["preview_blockers"])

    def test_preview_summary_rejects_valid_export_without_source_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            export_path = root / "source_export.json"
            preview_dir = root / "tmp" / "preview"
            preview_dir.mkdir(parents=True)
            release_path.write_text(
                '{"release_decision":{"blocked_by":[]},"release_ready":false,'
                '"engineering_hygiene_ok":true,"blocker_count":0}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                '{"ok":true,"checks":[{"name":"static_artifacts","ok":true}],'
                '"review_chain_safety_summary":'
                '{"do_not_set_human_reviewed_accepted_reviewed_automatically":true}}',
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":'
                '{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            export_path.write_text(
                '{"ok":true,"generated_at":"2026-07-02T01:00:00+00:00",'
                f'"output_dir":"{preview_dir.as_posix()}",'
                '"summary":{"copied_file_count":10,"copied_blocker_count":0}}',
                encoding="utf-8",
            )

            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=None,
                source_preview_export_path=export_path,
            )

        self.assertFalse(report["ok"])
        self.assertFalse(report["contract_ok"])
        self.assertFalse(report["preview_evidence_complete"])
        self.assertTrue(report["source_preview_export_ok"])
        self.assertTrue(report["source_package_ok"])
        self.assertIn("missing_source_boundary_evidence", report["preview_blockers"])

    def test_preview_summary_accepts_clean_export_when_branch_boundary_is_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            source_boundary_path = root / "source_boundary.json"
            export_path = root / "source_export.json"
            release_path.write_text(
                '{"release_decision":{"blocked_by":[],"release_ready":false},'
                '"release_ready":false,"engineering_hygiene_ok":true,"blocker_count":0}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                '{"ok":true,"checks":[{"name":"static_artifacts","ok":true}],'
                '"review_chain_safety_summary":'
                '{"do_not_set_human_reviewed_accepted_reviewed_automatically":true}}',
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":'
                '{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            source_boundary_path.write_text('{"ok":false}', encoding="utf-8")
            preview_dir = root / "tmp" / "preview"
            preview_dir.mkdir(parents=True)
            export_path.write_text(
                '{"ok":true,"generated_at":"2026-07-02T01:00:00+00:00",'
                f'"output_dir":"{preview_dir.as_posix()}",'
                '"summary":{"copied_file_count":10,"copied_blocker_count":0,'
                '"included_untracked_path_count":0,"excluded_untracked_candidate_count":2}}',
                encoding="utf-8",
            )
            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=source_boundary_path,
                source_preview_export_path=export_path,
            )
        self.assertTrue(report["ok"])
        self.assertFalse(report["source_boundary_ok"])
        self.assertTrue(report["source_preview_export_ok"])
        self.assertTrue(report["source_package_ok"])
        self.assertIn("current_branch_source_boundary_not_clean", report["preview_warnings"])
        self.assertNotIn("source_boundary_branch_not_clean", report["preview_blockers"])

    def test_source_preview_export_defaults_to_tracked_only_under_tmp(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/ncs_mcp").mkdir(parents=True)
            (root / "reports").mkdir()
            (root / ".codex").mkdir()
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            (root / "src/ncs_mcp/server.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "reports/release.json").write_text("{}\n", encoding="utf-8")
            (root / ".codex/config.toml").write_text("model='x'\n", encoding="utf-8")
            output_dir = root / "tmp" / "preview"
            fake_manifest = {
                "ok_for_preview_commit": False,
                "errors": [],
                "summary": {
                    "tracked_source_count": 1,
                    "tracked_blocker_count": 2,
                    "untracked_source_candidate_count": 3,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md"],
                "untracked_source_candidates": [
                    "src/ncs_mcp/server.py",
                    "reports/release.json",
                    ".codex/config.toml",
                ],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(output_dir=output_dir)
            self.assertTrue(report["ok"])
            self.assertFalse(report["current_manifest"]["ok_for_preview_commit"])
            self.assertTrue((output_dir / "README.md").exists())
            self.assertFalse((output_dir / "src/ncs_mcp/server.py").exists())
            self.assertFalse((output_dir / "reports/release.json").exists())
            self.assertFalse((output_dir / ".codex/config.toml").exists())
            self.assertEqual(report["summary"]["copied_file_count"], 1)
            self.assertEqual(report["summary"]["copied_blocker_count"], 0)
            self.assertEqual(report["summary"]["included_untracked_path_count"], 0)
            self.assertEqual(report["summary"]["excluded_untracked_candidate_count"], 3)

    def test_source_preview_export_includes_only_explicit_untracked_candidates(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/ncs_mcp").mkdir(parents=True)
            (root / "reports").mkdir()
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            (root / "src/ncs_mcp/server.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "reports/release.json").write_text("{}\n", encoding="utf-8")
            output_dir = root / "tmp" / "preview"
            fake_manifest = {
                "ok_for_preview_commit": False,
                "errors": [],
                "summary": {
                    "tracked_source_count": 1,
                    "tracked_blocker_count": 1,
                    "untracked_source_candidate_count": 2,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md"],
                "untracked_source_candidates": [
                    "src/ncs_mcp/server.py",
                    "reports/release.json",
                ],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(
                    output_dir=output_dir,
                    include_untracked_paths=["src/ncs_mcp/server.py"],
                )
            self.assertTrue(report["ok"])
            self.assertTrue((output_dir / "README.md").exists())
            self.assertTrue((output_dir / "src/ncs_mcp/server.py").exists())
            self.assertFalse((output_dir / "reports/release.json").exists())
            self.assertEqual(report["summary"]["copied_file_count"], 2)
            self.assertEqual(report["summary"]["included_untracked_path_count"], 1)
            self.assertEqual(report["summary"]["excluded_untracked_candidate_count"], 1)

    def test_source_preview_export_rejects_unknown_untracked_include(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            output_dir = root / "tmp" / "preview"
            fake_manifest = {
                "ok_for_preview_commit": True,
                "errors": [],
                "summary": {
                    "tracked_source_count": 1,
                    "tracked_blocker_count": 0,
                    "untracked_source_candidate_count": 0,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md"],
                "untracked_source_candidates": [],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(
                    output_dir=output_dir,
                    include_untracked_paths=["local_notes.md"],
                )
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["copy_error_count"], 1)
            self.assertIn("not an untracked source candidate", report["copy_errors"][0]["reason"])

    def test_source_preview_export_rejects_output_outside_tmp(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            fake_manifest = {
                "ok_for_preview_commit": True,
                "errors": [],
                "summary": {
                    "tracked_source_count": 1,
                    "tracked_blocker_count": 0,
                    "untracked_source_candidate_count": 0,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md"],
                "untracked_source_candidates": [],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(output_dir=root / "preview")
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["copy_error_count"], 1)
            self.assertIn("under tmp", report["copy_errors"][0]["reason"])

    def test_source_preview_export_rejects_existing_output_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "tmp" / "preview"
            output_dir.mkdir(parents=True)
            fake_manifest = {
                "ok_for_preview_commit": True,
                "errors": [],
                "summary": {
                    "tracked_source_count": 1,
                    "tracked_blocker_count": 0,
                    "untracked_source_candidate_count": 0,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md"],
                "untracked_source_candidates": [],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(output_dir=output_dir)
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["copy_error_count"], 1)
            self.assertIn("already exists", report["copy_errors"][0]["reason"])

    def test_source_preview_export_skips_parent_traversal_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            output_dir = root / "tmp" / "preview"
            fake_manifest = {
                "ok_for_preview_commit": True,
                "errors": [],
                "summary": {
                    "tracked_source_count": 2,
                    "tracked_blocker_count": 0,
                    "untracked_source_candidate_count": 0,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md", "../secret.txt"],
                "untracked_source_candidates": [],
            }
            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
            ):
                report = export_preview.export_preview(output_dir=output_dir)
            self.assertTrue(report["ok"])
            self.assertTrue((output_dir / "README.md").exists())
            self.assertFalse((output_dir / ".." / "secret.txt").exists())
            self.assertIn(
                {"path": "../secret.txt", "reason": "path traversal is not allowed"},
                report["skipped_files"],
            )

    def test_source_preview_export_skips_symlinks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            (root / "link.py").write_text("print('link')\n", encoding="utf-8")
            output_dir = root / "tmp" / "preview"
            fake_manifest = {
                "ok_for_preview_commit": True,
                "errors": [],
                "summary": {
                    "tracked_source_count": 2,
                    "tracked_blocker_count": 0,
                    "untracked_source_candidate_count": 0,
                    "untracked_blocker_count": 0,
                },
                "tracked_source_paths": ["README.md", "link.py"],
                "untracked_source_candidates": [],
            }

            def fake_is_symlink(path: Path) -> bool:
                return path.name == "link.py"

            with (
                patch.object(export_preview, "ROOT", root),
                patch.object(export_preview, "build_manifest", return_value=fake_manifest),
                patch.object(Path, "is_symlink", fake_is_symlink),
            ):
                report = export_preview.export_preview(output_dir=output_dir)
            self.assertTrue(report["ok"])
            self.assertTrue((output_dir / "README.md").exists())
            self.assertFalse((output_dir / "link.py").exists())
            self.assertIn(
                {"path": "link.py", "reason": "symlinks are not copied"},
                report["skipped_files"],
            )


if __name__ == "__main__":
    unittest.main()
