from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_learning_module_download_assessment import assess_download
from ncs_learning_module_pdf_download import (
    _execution_scope_error,
    _headers_to_dict,
    _is_pdf,
    _select_unique_rows,
)
from ncs_learning_module_verify_download import verify_download


class LearningModuleDownloadScriptTests(unittest.TestCase):
    def test_headers_to_dict_keeps_only_safe_download_metadata(self) -> None:
        headers = _headers_to_dict(
            "\n".join(
                [
                    "HTTP/1.1 200 OK",
                    "Content-Type: application/pdf",
                    "Content-Length: 123",
                    "Set-Cookie: JSESSIONID=secret; path=/",
                    "X-CSRF-Token: secret",
                    "Content-Disposition: attachment; filename=LM0202020101_%EC%9D%B8%EC%82%AC.pdf;",
                ]
            )
        )

        self.assertEqual(headers["status_line"], "HTTP/1.1 200 OK")
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertEqual(headers["content-length"], "123")
        self.assertEqual(headers["decoded_filename"], "LM0202020101_인사.pdf")
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn("x-csrf-token", headers)

    def test_select_unique_rows_filters_scope_before_limit(self) -> None:
        rows = [
            {"ncs_cl_cd": "0101010101_01v1", "download_key": "outside"},
            {"ncs_cl_cd": "0202020101_13v1", "download_key": "a"},
            {"ncs_cl_cd": "0202020102_13v1", "download_key": "a"},
            {"ncs_cl_cd": "0202030101_13v1", "download_key": "b"},
        ]

        selected = _select_unique_rows(rows, "020202", 2)

        self.assertEqual([row["download_key"] for row in selected], ["a"])

    def test_non_dry_run_requires_bounded_scope_or_explicit_full_mirror(self) -> None:
        self.assertIsNotNone(
            _execution_scope_error(
                dry_run=False,
                scope_prefix=None,
                limit=None,
                allow_full_mirror=False,
            )
        )
        self.assertIsNone(
            _execution_scope_error(
                dry_run=True,
                scope_prefix=None,
                limit=None,
                allow_full_mirror=False,
            )
        )
        self.assertIsNone(
            _execution_scope_error(
                dry_run=False,
                scope_prefix="02",
                limit=None,
                allow_full_mirror=False,
            )
        )
        self.assertIsNone(
            _execution_scope_error(
                dry_run=False,
                scope_prefix=None,
                limit=None,
                allow_full_mirror=True,
            )
        )

    def test_is_pdf_rejects_non_pdf_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "ok.pdf"
            html_path = root / "error.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            html_path.write_text("<html>error</html>", encoding="utf-8")

            self.assertTrue(_is_pdf(pdf_path))
            self.assertFalse(_is_pdf(html_path))

    def test_verify_download_reports_pdf_magic_and_header_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "0202020101_training_a.pdf"
            bad_path = root / "0202020102_training_b.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            bad_path.write_text("<html>error</html>", encoding="utf-8")
            (root / "0202020101_training_a.headers.json").write_text("{}", encoding="utf-8")
            (root / "0202020102_training_b.headers.tmp").write_text("raw", encoding="utf-8")

            report = verify_download(out_dir=root, scope_prefix="020202")

        self.assertEqual(report["pdf_count"], 2)
        self.assertEqual(report["headers_json_count"], 1)
        self.assertEqual(report["headers_tmp_count"], 1)
        self.assertEqual(report["bad_pdf_magic_count"], 1)
        self.assertFalse(report["ok"])

    def test_assessment_uses_exact_counts_when_index_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index_jsonl = root / "index.jsonl"
            rows = [
                {"page_index": 0, "ncs_cl_cd": "0202020101_13v1", "download_key": "a"},
                {"page_index": 0, "ncs_cl_cd": "0202020102_13v1", "download_key": "a"},
                {"page_index": 1, "ncs_cl_cd": "0301010101_13v1", "download_key": "b"},
            ]
            index_jsonl.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            probe_json = root / "probe.json"
            probe_json.write_text(
                json.dumps({"sample_download": {"pdf_probe": {"bytes": 10}}}),
                encoding="utf-8",
            )
            summary_json = root / "summary.json"
            summary_json.write_text(json.dumps({"max_page_index_observed": 1}), encoding="utf-8")

            report = assess_download(
                index_jsonl=index_jsonl,
                probe_json=probe_json,
                summary_json=summary_json,
            )

        self.assertTrue(report["observed"]["observed_complete"])
        self.assertEqual(report["estimated_full_scope"]["estimated_raw_rows"], 3)
        self.assertEqual(report["estimated_full_scope"]["estimated_unique_files"], 2)


if __name__ == "__main__":
    unittest.main()
