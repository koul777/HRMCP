from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_harness import lint_repo, run_smoke_check


class HarnessTests(unittest.TestCase):
    def test_lint_repo_returns_result_shape(self) -> None:
        result = lint_repo(strict=False)
        self.assertIn("ok", result)
        self.assertIn("issues", result)
        self.assertIsInstance(result["issues"], list)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_smoke_check_uses_hr_classification_codes(self) -> None:
        result = run_smoke_check("02", "02", "02", "01")
        self.assertEqual(result["classification"]["major_code"], "02")
        self.assertGreaterEqual(result["unit_count"], 1)
        self.assertGreaterEqual(result["sample_elements"], 1)
        self.assertGreaterEqual(result["sample_criteria"], 1)
        self.assertGreaterEqual(result["sample_ksa"], 1)


if __name__ == "__main__":
    unittest.main()
