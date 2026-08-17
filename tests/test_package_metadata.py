from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTests(unittest.TestCase):
    def test_runtime_dependencies_have_major_version_bounds(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        for requirement in (
            '"mcp>=1.26,<2"',
            '"openpyxl>=3.1,<4"',
            '"requests>=2.32,<3"',
            '"python-dotenv>=1,<2"',
            '"Pillow>=10,<13"',
            '"PyMuPDF>=1.24,<2"',
            '"pypdf>=5,<7"',
            '"pytesseract>=0.3.10,<1"',
            '"olefile>=0.47,<1"',
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, pyproject)

        self.assertIn('readme = "README.md"', pyproject)

    def test_requirements_cover_every_runtime_dependency(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        for package in (
            "mcp",
            "openpyxl",
            "requests",
            "python-dotenv",
            "Pillow",
            "PyMuPDF",
            "pypdf",
            "pytesseract",
            "olefile",
        ):
            with self.subTest(package=package):
                self.assertIn(package, requirements)


if __name__ == "__main__":
    unittest.main()
