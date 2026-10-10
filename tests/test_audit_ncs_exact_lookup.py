from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unicodedata
import unittest


PATH = Path(__file__).resolve().parents[1] / "scripts/audit_ncs_exact_lookup.py"
SPEC = importlib.util.spec_from_file_location("audit_ncs_exact_lookup", PATH)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ExactLookupAuditTests(unittest.TestCase):
    def test_output_cannot_overwrite_database_or_hardlink_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "source.db"
            db.write_bytes(b"source")
            alias = Path(folder) / "alias.json"
            os.link(db, alias)
            for path in (db, alias):
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    audit.validate_output_path(path, db)
            self.assertEqual(db.read_bytes(), b"source")
            with self.assertRaisesRegex(ValueError, ".json"):
                audit.validate_output_path(Path(folder) / "report.db", db)

    def test_duplicate_names_accept_all_source_codes_across_majors(self):
        cases = audit.build_cases([("A", "shared", "01"), ("B", "shared", "24")])
        self.assertEqual(cases, [{"query": "shared", "expected": ["A", "B"], "majors": ["01", "24"]}])
        results = audit.evaluate(cases, lambda *a, **kw: {"results": [{"id": "B", "text": "shared"}]})
        self.assertEqual(audit.aggregate(results)["overall"]["hit_at_1"], 1)
        self.assertEqual(set(audit.aggregate(results)["by_major"]), {"01", "24"})

    def test_sample_is_order_independent_and_covers_every_major(self):
        rows = [(f"{major}_{i}", f"name {major} {i}", major) for major in ("01", "12", "24") for i in range(20)]
        forward = audit.build_cases(rows, per_major_limit=4)
        backward = audit.build_cases(reversed(rows), per_major_limit=4)
        self.assertEqual(forward, backward)
        self.assertEqual(len(forward), 12)
        self.assertEqual({m for c in forward for m in c["majors"]}, {"01", "12", "24"})

    def test_unicode_variant_preserves_authoritative_expected_code(self):
        cases = audit.build_cases([("CODE", "장비 진단", "15")], variant="nfd")
        self.assertNotEqual(cases[0]["query"], "장비 진단")
        self.assertEqual(unicodedata.normalize("NFC", cases[0]["query"]), "장비 진단")
        self.assertEqual(cases[0]["expected"], ["CODE"])
        codes = audit.build_cases([("CODE", "name", "15")], kind="code")
        self.assertEqual(codes[0]["query"], "CODE")

    def test_missing_and_lower_rank_are_not_top_one_successes(self):
        rows = [{"rank": None, "majors": ["01"]}, {"rank": 3, "majors": ["01"]}]
        metrics = audit.aggregate(rows)["overall"]
        self.assertEqual(metrics["hit_at_1"], 0)
        self.assertEqual(metrics["hit_at_3"], 0.5)
        self.assertAlmostEqual(metrics["mrr"], 1 / 6)

    def test_spelling_variant_preserves_source_labels_and_is_order_independent(self):
        rows = [("A", "인력채용", "02"), ("B", "재무제표 작성", "03"), ("C", "인력채용", "24")]
        cases = audit.build_cases(rows, variant="single_typo")
        self.assertEqual(cases, audit.build_cases(reversed(rows), variant="single_typo"))
        by_source = {case["source_query"]: case for case in cases}
        self.assertEqual(by_source["인력채용"]["query"], "인력용채")
        self.assertEqual(by_source["인력채용"]["expected"], ["A", "C"])
        self.assertEqual(by_source["인력채용"]["majors"], ["02", "24"])
        self.assertEqual(by_source["재무제표 작성"]["expected"], ["B"])
        self.assertNotEqual(by_source["재무제표 작성"]["query"], "재무제표 작성")

    def test_spelling_audit_does_not_perturb_codes_or_short_names(self):
        with self.assertRaisesRegex(ValueError, "never source codes"):
            audit.build_cases([("CODE", "인력채용", "02")], kind="code", variant="single_typo")
        self.assertEqual(audit.build_cases([("SHORT", "문서 작성", "02")], variant="single_typo"), [])

    def test_spelling_sample_does_not_depend_on_search_accepting_a_suggestion(self):
        rows = [(f"{major}_{i}", f"문서관리 {major} {i}", major) for major in ("01", "12", "24") for i in range(5)]
        cases = audit.build_cases(rows, variant="single_typo", per_major_limit=2)
        self.assertEqual(len(cases), 6)
        self.assertEqual({m for case in cases for m in case["majors"]}, {"01", "12", "24"})

    def test_public_scope_error_is_a_miss_not_a_success_or_a_crash(self):
        cases = audit.build_cases([("A", "name", "01")])
        results = audit.evaluate(cases, lambda *a, **kw: {
            "ok": False, "error": {"code": "route_context_required"},
        })
        self.assertIsNone(results[0]["rank"])
        self.assertEqual(results[0]["error_code"], "route_context_required")
        self.assertEqual(audit.aggregate(results)["overall"]["hit_at_3"], 0)

    def test_prompt_frames_preserve_all_expected_source_identifiers(self):
        cases = audit.build_cases([("A", "장비 진단", "01"), ("B", "장비 진단", "24")])
        wrapped = audit.with_prompt_templates(cases, ("prefix", "evidence"))
        self.assertEqual(len(wrapped), 2)
        self.assertEqual(cases[0]["query"], "장비 진단")
        self.assertEqual(wrapped[0]["query"], "NCS 기준으로 다음 직무를 찾아줘: 장비 진단")
        for case in wrapped:
            self.assertEqual(case["source_query"], "장비 진단")
            self.assertEqual(case["expected"], ["A", "B"])
            self.assertEqual(case["majors"], ["01", "24"])


if __name__ == "__main__":
    unittest.main()
