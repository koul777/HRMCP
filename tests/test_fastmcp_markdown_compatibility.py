from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tomllib
import unittest
from importlib import metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_CONSTRAINT = "mcp>=1.26,<=1.29.1"
MCP_LOCK_SPECIFIER = ">=1.26,<=1.29.1"


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


class FastMcpMarkdownCompatibilityTests(unittest.TestCase):
    def test_runtime_dependency_range_is_consistent_and_bounded(self) -> None:
        pyprojects = (
            ROOT / "pyproject.toml",
            ROOT / "deploy" / "vercel_mcp_app" / "pyproject.toml",
        )
        requirements = (
            ROOT / "requirements.txt",
            ROOT / "deploy" / "vercel_mcp_app" / "requirements.txt",
        )
        locks = (
            ROOT / "uv.lock",
            ROOT / "deploy" / "vercel_mcp_app" / "uv.lock",
        )

        for path in pyprojects:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            dependencies = payload["project"]["dependencies"]
            self.assertIn(MCP_CONSTRAINT, dependencies, path)

        for path in requirements:
            lines = {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            self.assertIn(MCP_CONSTRAINT, lines, path)

        for path in locks:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            packages = payload["package"]
            project = next(item for item in packages if item["name"] == "ncs-mcp")
            requirements_metadata = project["metadata"]["requires-dist"]
            mcp_requirement = next(
                item for item in requirements_metadata if item["name"] == "mcp"
            )
            self.assertEqual(mcp_requirement["specifier"], MCP_LOCK_SPECIFIER, path)
            locked_mcp = next(item for item in packages if item["name"] == "mcp")
            self.assertGreaterEqual(_version_tuple(locked_mcp["version"]), (1, 26))
            self.assertLessEqual(_version_tuple(locked_mcp["version"]), (1, 29, 1))

    def test_installed_private_converter_contract_matches_supported_range(self) -> None:
        from mcp.server.fastmcp.utilities import func_metadata

        installed_version = metadata.version("mcp")
        self.assertGreaterEqual(_version_tuple(installed_version), (1, 26))
        self.assertLessEqual(_version_tuple(installed_version), (1, 29, 1))
        converter = getattr(func_metadata, "_convert_to_content", None)
        self.assertTrue(callable(converter))
        signature = inspect.signature(converter)
        self.assertEqual(list(signature.parameters), ["result"])

    def test_markdown_converter_patch_is_idempotent_across_module_reload(self) -> None:
        script = """
import importlib
from mcp.server.fastmcp.utilities import func_metadata
import ncs_mcp.server as server

first = func_metadata._convert_to_content
first_original = getattr(first, "_ncs_mcp_original_converter", None)
assert callable(first_original)

reloaded = importlib.reload(server)
second = func_metadata._convert_to_content
second_original = getattr(second, "_ncs_mcp_original_converter", None)
assert callable(second_original)
assert second_original is first_original
assert second_original is not first

payload = reloaded.RenderedToolPayload({"ok": True}, markdown="## compact")
content = second(payload)
assert len(content) == 1
assert content[0].type == "text"
assert content[0].text == "## compact"

reloaded_again = importlib.reload(reloaded)
third = func_metadata._convert_to_content
assert getattr(third, "_ncs_mcp_original_converter", None) is first_original
payload_again = reloaded_again.RenderedToolPayload(
    {"ok": True}, markdown="## compact again"
)
assert third(payload_again)[0].text == "## compact again"
"""
        source_roots = (
            ROOT / "src",
            ROOT / "deploy" / "vercel_mcp_app" / "src",
        )
        for source_root in source_roots:
            with self.subTest(source_root=source_root):
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(source_root)
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"stdout={completed.stdout}\nstderr={completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
