from __future__ import annotations

import unittest

from scripts.verify_ncs_scope_hazards import (
    _path_key,
    _scope_key,
    _stratified_sample,
)


class ScopeHazardExecutionHelpersTests(unittest.TestCase):
    def test_path_and_scope_keys_trim_missing_levels(self) -> None:
        row = {"path": {"major_code": "02", "middle_code": "02", "small_code": "02", "sub_code": "01"}}
        self.assertEqual(_path_key(row), "02/02/02/01")
        self.assertEqual(_scope_key({"major_code": "02", "middle_code": "02"}), "02/02")

    def test_stratified_sample_keeps_each_hazard_kind(self) -> None:
        rows = [
            {"scenario_id": "p1", "hazard_kind": "prefix"},
            {"scenario_id": "p2", "hazard_kind": "prefix"},
            {"scenario_id": "i1", "hazard_kind": "internal_compound"},
            {"scenario_id": "e1", "hazard_kind": "exact_off_path"},
        ]
        selected = _stratified_sample(rows, 3)
        self.assertEqual({item["hazard_kind"] for item in selected}, {"prefix", "internal_compound", "exact_off_path"})
        self.assertEqual(len(selected), 3)


if __name__ == "__main__":
    unittest.main()
