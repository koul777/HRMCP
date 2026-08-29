from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MIRRORS = (
    "knowledge_graph.py",
    "training_recommendation.py",
    "compact_postings.py",
    "server.py",
    "vercel_snapshot.py",
)


def _runtime_import_closure() -> tuple[str, ...]:
    """Resolve local API/NCS imports that must be copied into deploy/."""

    roots = {
        "api/index.py": REPOSITORY_ROOT / "api" / "index.py",
        "api/mcp.py": REPOSITORY_ROOT / "api" / "mcp.py",
        "api/health.py": REPOSITORY_ROOT / "api" / "health.py",
        "api/ready.py": REPOSITORY_ROOT / "api" / "ready.py",
    }
    pending = list(roots.values())
    discovered = set(roots)

    def add_module(module_name: str) -> None:
        if module_name == "ncs_mcp":
            candidate = REPOSITORY_ROOT / "src" / "ncs_mcp" / "__init__.py"
            relative = "src/ncs_mcp/__init__.py"
        elif module_name == "api":
            candidate = REPOSITORY_ROOT / "api" / "__init__.py"
            relative = "api/__init__.py"
        elif module_name.startswith("ncs_mcp."):
            candidate = REPOSITORY_ROOT / "src" / Path(*module_name.split("."))
            candidate = candidate.with_suffix(".py")
            relative = candidate.relative_to(REPOSITORY_ROOT).as_posix()
        elif module_name.startswith("api."):
            candidate = REPOSITORY_ROOT / Path(*module_name.split("."))
            candidate = candidate.with_suffix(".py")
            relative = candidate.relative_to(REPOSITORY_ROOT).as_posix()
        else:
            return
        if candidate.is_file() and relative not in discovered:
            discovered.add(relative)
            pending.append(candidate)

    while pending:
        path = pending.pop()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module or ""
                if module.startswith(("api", "ncs_mcp")):
                    add_module(module)
                    for alias in node.names:
                        if alias.name != "*":
                            add_module(f"{module}.{alias.name}")

    return tuple(sorted(discovered | {f"src/ncs_mcp/{name}" for name in RUNTIME_MIRRORS}))


class DeployRuntimeParityTests(unittest.TestCase):
    def test_compact_runtime_files_match_the_canonical_sources(self) -> None:
        for relative in _runtime_import_closure():
            with self.subTest(filename=relative):
                source = REPOSITORY_ROOT / relative
                mirror = REPOSITORY_ROOT / "deploy" / "vercel_mcp_app" / relative
                self.assertTrue(source.is_file(), f"missing canonical source: {source}")
                self.assertTrue(mirror.is_file(), f"missing deploy mirror: {mirror}")
                self.assertEqual(
                    hashlib.sha256(source.read_bytes()).hexdigest(),
                    hashlib.sha256(mirror.read_bytes()).hexdigest(),
                    f"deploy runtime mirror is stale: {relative}",
                )


if __name__ == "__main__":
    unittest.main()
