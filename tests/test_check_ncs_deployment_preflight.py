from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_ncs_deployment_preflight.py"
SPEC = importlib.util.spec_from_file_location("check_ncs_deployment_preflight", SCRIPT)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class DeploymentPreflightTests(unittest.TestCase):
    def test_literal_string_set_reads_only_named_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "registry.py"
            path.write_text('USER_MCP_TOOLS = {"alpha", "beta"}\nSECRET = "do-not-read"\n', encoding="utf-8")
            self.assertEqual(preflight._literal_string_set(path, "USER_MCP_TOOLS"), {"alpha", "beta"})

    def test_env_example_parser_returns_names_not_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env.example"
            path.write_text("PUBLIC_NAME=visible\nSECRET_NAME=super-secret-value\n# IGNORED=x\n", encoding="utf-8")
            names = preflight._env_names_from_example(path)
            self.assertEqual(names, {"PUBLIC_NAME", "SECRET_NAME"})
            self.assertNotIn("super-secret-value", names)

    def test_snapshot_inspection_enforces_manifest_member_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deploy_root = Path(temp_dir)
            api = deploy_root / "api"
            api.mkdir()
            member = b"SQLite format 3\x00" + b"x" * 128
            manifest = {
                "schema": "ncs_ontology_compact_manifest_v1",
                "archive_member": "ncs_ontology_compact.db",
                "sqlite_bytes": len(member),
            }
            (api / "ncs_ontology_compact.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with zipfile.ZipFile(api / "ncs_ontology_compact.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("ncs_ontology_compact.db", member)
            details, check = preflight._inspect_snapshot(deploy_root)
            self.assertEqual(check["status"], "pass")
            self.assertEqual(details["sqlite_bytes"], len(member))

    def test_snapshot_inspection_blocks_hard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deploy_root = Path(temp_dir)
            api = deploy_root / "api"
            api.mkdir()
            manifest = {
                "schema": "ncs_ontology_compact_manifest_v1",
                "archive_member": "ncs_ontology_compact.db",
                "sqlite_bytes": preflight.HARD_SNAPSHOT_BYTES,
            }
            (api / "ncs_ontology_compact.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with zipfile.ZipFile(api / "ncs_ontology_compact.zip", "w") as bundle:
                info = zipfile.ZipInfo("ncs_ontology_compact.db")
                bundle.writestr(info, b"x")
            _details, check = preflight._inspect_snapshot(deploy_root)
            self.assertEqual(check["status"], "block")

    def test_release_path_classifier_separates_unrelated_changes(self) -> None:
        self.assertTrue(preflight._is_release_related("src/ncs_mcp/server.py"))
        self.assertTrue(preflight._is_release_related("reports/ncs_search_after_p1_20260830.json"))
        self.assertFalse(preflight._is_release_related("README.md"))

    def test_owned_git_status_places_untracked_option_before_pathspec_separator(self) -> None:
        _tracked_command, owned_command = preflight._git_status_commands()
        separator = owned_command.index("--")
        self.assertLess(owned_command.index("--untracked-files=all"), separator)
        self.assertEqual(owned_command[separator + 1 :], list(preflight.OWNED_PATHS))
        self.assertNotIn("--untracked-files=no", owned_command)

    def test_cli_failure_classification_never_returns_stderr(self) -> None:
        secret_bearing_stderr = "Error: invalid token top-secret-value; please login"
        self.assertEqual(
            preflight._classify_cli_failure(secret_bearing_stderr),
            "credential_missing_or_invalid",
        )
        self.assertNotIn("top-secret-value", preflight._classify_cli_failure(secret_bearing_stderr))

    def test_cli_inspection_distinguishes_link_session_and_token_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deploy = root / "deploy"
            (deploy / ".vercel").mkdir(parents=True)
            (deploy / ".vercel" / "project.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(preflight.shutil, "which", return_value="vercel-or-gh"), mock.patch.object(
                preflight,
                "_run_cli_probe",
                side_effect=[(0, "none"), (1, "credential_missing_or_invalid")],
            ), mock.patch.dict(preflight.os.environ, {"VERCEL_TOKEN": "configured-secret"}, clear=True):
                checks, paths = preflight._inspect_cli(root, deploy)
            self.assertTrue(paths["project_linked"])
            self.assertFalse(paths["vercel_cli_authenticated"])
            self.assertTrue(paths["vercel_token_configured"])
            self.assertEqual(paths["recommended_path"], "linked_project_token_env")
            self.assertNotIn("configured-secret", json.dumps({"checks": checks, "paths": paths}))


if __name__ == "__main__":
    unittest.main()
