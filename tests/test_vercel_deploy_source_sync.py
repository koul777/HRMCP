from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"


class VercelDeploySourceSyncTests(unittest.TestCase):
    def test_shared_python_runtime_files_match_the_canonical_source_tree(self) -> None:
        canonical_package = ROOT / "src" / "ncs_mcp"
        deployed_package = DEPLOY_ROOT / "src" / "ncs_mcp"
        deployed_files = sorted(deployed_package.glob("*.py"))

        self.assertGreater(len(deployed_files), 0)
        for deployed_path in deployed_files:
            canonical_path = canonical_package / deployed_path.name
            with self.subTest(path=deployed_path.name):
                self.assertTrue(canonical_path.is_file())
                self.assertEqual(
                    canonical_path.read_bytes(),
                    deployed_path.read_bytes(),
                    f"Vercel runtime copy is stale: {deployed_path.name}",
                )

    def test_shared_vercel_entrypoints_match_the_canonical_api_tree(self) -> None:
        for name in ("health.py", "mcp.py"):
            canonical_path = ROOT / "api" / name
            deployed_path = DEPLOY_ROOT / "api" / name
            with self.subTest(path=name):
                self.assertEqual(
                    canonical_path.read_bytes(),
                    deployed_path.read_bytes(),
                    f"Vercel API entrypoint is stale: {name}",
                )


if __name__ == "__main__":
    unittest.main()
