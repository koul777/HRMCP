from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_dashboard import (
    HTML,
    get_api_orphans,
    get_classifications,
    get_item_detail,
    get_items,
    get_issues,
    get_progress,
    get_status,
    get_unit_detail,
    get_units,
    get_workbench,
)


class DashboardTests(unittest.TestCase):
    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_status_shape(self) -> None:
        status = get_status(ROOT / "data" / "processed" / "ncs.db")
        self.assertIn("counts", status)
        self.assertIn("element_progress", status)
        self.assertGreaterEqual(status["counts"]["competency_units"], 1)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_issues_shape(self) -> None:
        result = get_issues(ROOT / "data" / "processed" / "ncs.db", {"limit": ["5"]})
        self.assertIn("issues", result)
        self.assertIsInstance(result["issues"], list)

    def test_dashboard_html_has_lookup_and_large_editor(self) -> None:
        self.assertIn("NCS MCP 전처리 워크벤치", HTML)
        self.assertIn("온톨로지 준비 전처리 단계", HTML)
        self.assertIn("/api/preprocess", HTML)
        self.assertIn("min-height:210px", HTML)
        self.assertIn("min-width:420px", HTML)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_lookup_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        classifications = get_classifications(db_path, params)
        self.assertIn("classifications", classifications)
        self.assertGreaterEqual(len(classifications["classifications"]), 1)

        units = get_units(db_path, params)
        self.assertIn("units", units)
        self.assertGreaterEqual(len(units["units"]), 1)
        self.assertLessEqual(units["units"][0]["element_matched"], units["units"][0]["element_count"])

        detail = get_unit_detail(db_path, {"unit_code": [units["units"][0]["unit_code"]]})
        self.assertIn("unit", detail)
        self.assertIn("elements", detail)

        api_orphans = get_api_orphans(db_path, {"limit": ["5"]})
        self.assertIn("api_orphans", api_orphans)
        self.assertIsInstance(api_orphans["api_orphans"], list)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_workbench_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        progress = get_progress(db_path, params)
        self.assertIn("phases", progress)
        self.assertGreaterEqual(len(progress["phases"]), 1)

        workbench = get_workbench(db_path, params)
        self.assertIn("cards", workbench)
        self.assertGreaterEqual(len(workbench["cards"]), 1)

        items = get_items(db_path, {**params, "kind": ["element"], "state": ["api_matched"]})
        self.assertIn("items", items)
        self.assertGreaterEqual(len(items["items"]), 1)

        detail = get_item_detail(
            db_path,
            {"kind": ["element"], "id": [str(items["items"][0]["id"])]},
        )
        self.assertIn("item", detail)
        self.assertEqual(detail["item"]["kind"], "element")


if __name__ == "__main__":
    unittest.main()
