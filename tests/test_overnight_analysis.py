from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts import overnight_analysis


class OvernightAnalysisCsvTests(unittest.TestCase):
    def test_write_csv_escapes_formula_like_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "overnight.csv"
            overnight_analysis.write_csv(
                csv_path,
                [
                    {
                        "plain": "safe",
                        "direct_formula": "=cmd",
                        "space_formula": " +cmd",
                        "tab_formula": "\t-cmd",
                        "list_value": ["=cmd"],
                    }
                ],
            )
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["plain"], "safe")
        self.assertEqual(row["direct_formula"], "'=cmd")
        self.assertEqual(row["space_formula"], "' +cmd")
        self.assertEqual(row["tab_formula"], "'\t-cmd")
        self.assertEqual(row["list_value"], "[\"=cmd\"]")


if __name__ == "__main__":
    unittest.main()
