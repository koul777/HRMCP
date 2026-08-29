from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LicenseAndSourceNoticeTests(unittest.TestCase):
    def test_repository_declares_mit_without_relicensing_external_data(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 HRMCP contributors", license_text)
        self.assertEqual(pyproject["project"]["license"], {"file": "LICENSE"})
        self.assertIn("does not relicense source data", notice)
        self.assertIn("DATA_SOURCE_NOTICE.md", notice)
        self.assertIn("scripts/vendor/3d-force-graph-LICENSE.txt", notice)

    def test_docs_distinguish_git_source_from_vercel_snapshot(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        source_notice = (ROOT / "DATA_SOURCE_NOTICE.md").read_text(encoding="utf-8")
        release_checklist = (ROOT / "docs/MCP_RELEASE_CHECKLIST.md").read_text(
            encoding="utf-8"
        )

        for text in (readme, source_notice):
            with self.subTest(document="README" if text is readme else "source notice"):
                self.assertIn("MIT License", text)
                self.assertIn("NOTICE", text)
                self.assertIn("Git", text)
                self.assertIn("snapshot", text)
                self.assertIn("materialize", text)

        self.assertNotIn("코드 라이선스는 권한 있는 소유자가 아직 선택", source_notice)
        self.assertNotIn("현재 저장소 코드의 라이선스는 권한 있는", readme)
        self.assertNotIn("no root license declaration", release_checklist)
        self.assertIn("do not describe the repository MIT license as relicensing", release_checklist)

    def test_direct_download_collectors_carry_data_use_warnings(self) -> None:
        learning_module_scripts = sorted((ROOT / "scripts").glob("ncs_learning_module_*.py"))
        self.assertGreaterEqual(len(learning_module_scripts), 5)

        for path in learning_module_scripts:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("# DATA USE WARNING:", text)
                self.assertIn("legacy/reference-only", text)
                self.assertIn("serving-snapshot inclusion", text)
                self.assertIn("third-party rights", text)

        sqf_text = (ROOT / "src/ncs_mcp/collect_sqf_library.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("# DATA USE WARNING:", sqf_text)
        self.assertIn("directly from ncs.go.kr", sqf_text)
        self.assertIn("legacy/reference-only", sqf_text)
        self.assertIn("post/file-level KOGL/public-use terms", sqf_text)

    def test_notice_lists_active_api_records_without_global_rights_claim(self) -> None:
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

        for record_id in ("15128213", "15086447", "15074404", "15086440"):
            with self.subTest(record_id=record_id):
                self.assertIn(record_id, notice)

        self.assertIn('"이용허락범위 제한 없음"', notice)
        self.assertIn("for those API records", notice)
        self.assertIn("This list does not", notice)
        self.assertIn("every file or web page used by the project has unrestricted terms", notice)


if __name__ == "__main__":
    unittest.main()
