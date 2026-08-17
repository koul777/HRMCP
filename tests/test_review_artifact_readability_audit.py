from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_review_artifact_readability import (  # noqa: E402
    audit_paths,
    build_empty_scan_payload,
    collect_review_artifact_paths,
    main as readability_main,
    write_markdown,
)


class ReviewArtifactReadabilityAuditTests(unittest.TestCase):
    def test_safe_korean_artifact_passes_without_approval_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            json_path = tmp_path / "safe.json"
            jsonl_path = tmp_path / "safe.jsonl"
            markdown_path = tmp_path / "safe.md"
            csv_path = tmp_path / "safe.csv"
            html_path = tmp_path / "safe.html"
            json_path.write_text(
                json.dumps(
                    {
                        "title": "NCS 훈련 추천 MCP 사용자 가이드",
                        "decision": "",
                        "review_note": "사람 검토 전 표시 확인용",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            jsonl_path.write_text(json.dumps({"title": "직무 전환"}, ensure_ascii=False) + "\n", encoding="utf-8")
            markdown_path.write_text("# 사람 검토 전 표시 확인용\n", encoding="utf-8")
            csv_path.write_text("title,decision\n사람 검토,\n", encoding="utf-8-sig")
            html_path.write_text("<html><body>경력개발</body></html>\n", encoding="utf-8")

            payload = audit_paths([json_path, jsonl_path, markdown_path, csv_path, html_path])

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["artifact_count"], 5)
        self.assertIn("utf-8-sig", {artifact["encoding"] for artifact in payload["artifacts"]})
        self.assertTrue(payload["report_only"])
        self.assertFalse(payload["status_update_allowed"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])
        self.assertTrue(payload["human_decision_required"])

    def test_detects_korean_mojibake_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.md"
            path.write_text(
                "# NCS ?덈젴 異붿쿇\n"
                "吏곷Т ?꾪솚 援먯쑁??異붿쿇?섎뒗 ?쒕쾭??\n",
                encoding="utf-8",
            )

            payload = audit_paths([path])

        codes = {finding["code"] for finding in payload["findings"]}
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "review_required")
        self.assertIn("mojibake_marker_detected", codes)
        self.assertIn("question_mark_noise_detected", codes)
        self.assertEqual(payload["findings"][0]["rule_code"], payload["findings"][0]["code"])
        self.assertIn("recommended_action", payload["findings"][0])
        self.assertEqual(payload["artifacts"][0]["sample_lines"][0]["line_number"], 1)

    def test_detects_mojibake_marker_without_question_density(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.md"
            path.write_text("review field: 援먯육 artifact\n", encoding="utf-8")

            payload = audit_paths([path])

        codes = {finding["rule_code"] for finding in payload["findings"]}
        self.assertFalse(payload["ok"])
        self.assertIn("mojibake_marker_detected", codes)
        self.assertNotIn("question_mark_noise_detected", codes)

    def test_benign_cjk_text_does_not_trigger_mojibake_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.md"
            path.write_text("course evidence: 獄門 疫學 筌蹄\n", encoding="utf-8")

            payload = audit_paths([path])

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["finding_count"], 0)

    def test_directory_artifact_is_structured_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review_dir"
            path.mkdir()

            payload = audit_paths([path])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["findings"][0]["rule_code"], "artifact_path_is_directory")

    def test_invalid_utf8_bytes_are_decode_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.json"
            path.write_bytes(b'{"title": "\xff\xfe"}')

            payload = audit_paths([path])

        codes = {finding["rule_code"] for finding in payload["findings"]}
        self.assertFalse(payload["ok"])
        self.assertIn("invalid_utf8", codes)
        self.assertFalse(payload["artifacts"][0]["readable"])

    def test_utf16_bom_artifact_is_non_utf8_contract_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.json"
            path.write_bytes('{"title": "사람 검토"}'.encode("utf-16"))

            payload = audit_paths([path])

        codes = {finding["rule_code"] for finding in payload["findings"]}
        self.assertFalse(payload["ok"])
        self.assertIn("non_utf8_bom_detected", codes)
        self.assertEqual(payload["artifacts"][0]["encoding"], "utf-16")
        self.assertFalse(payload["artifacts"][0]["readable"])
        self.assertIn("UTF-8", payload["findings"][0]["recommended_action"])

    def test_surfaces_display_noise_triage_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "review_bucket": "encoding_display_triage",
                        "flags": ["possible_encoding_or_display_noise"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            payload = audit_paths([path])

        codes = {finding["rule_code"] for finding in payload["findings"]}
        self.assertFalse(payload["ok"])
        self.assertIn("display_noise_triage_metadata_present", codes)

    def test_collects_default_review_artifact_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "reports"
            nested = reports_dir / "nested"
            nested.mkdir(parents=True)
            expected = [
                reports_dir / "human_review_packet.json",
                nested / "review_seedpack.jsonl",
                nested / "operator_decision_sheet.csv",
                nested / "operator_decision_sheet.html",
            ]
            ignored = nested / "ordinary.txt"
            self_audit = nested / "review_artifact_readability_old.json"
            for path in expected + [ignored, self_audit]:
                path.write_text("사람 검토 전 표시 확인용\n", encoding="utf-8")

            paths = collect_review_artifact_paths(reports_dir)

        self.assertEqual({path.name for path in paths}, {path.name for path in expected})

    def test_collect_limit_applies_after_all_patterns_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()
            for name in [
                "a_review.json",
                "b_review.jsonl",
                "c_review.md",
                "d_review.csv",
                "e_review.html",
            ]:
                (reports_dir / name).write_text("사람 검토 전 표시 확인용\n", encoding="utf-8")

            limited = collect_review_artifact_paths(reports_dir, limit=3)
            unlimited = collect_review_artifact_paths(reports_dir, limit=0)

        self.assertEqual(len(limited), 3)
        self.assertEqual(len(unlimited), 5)
        self.assertEqual([path.name for path in limited], ["a_review.json", "b_review.jsonl", "c_review.md"])

    def test_empty_scan_payload_is_review_required_without_write_claims(self) -> None:
        payload = build_empty_scan_payload(reports_dir=Path("missing_reports"), patterns=["*review*.json"])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["findings"][0]["rule_code"], "no_review_artifacts_found")
        self.assertTrue(payload["report_only"])
        self.assertFalse(payload["status_update_allowed"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["approval_claim"])

    def test_missing_artifact_is_review_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.json"

            payload = audit_paths([path])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["findings"][0]["code"], "missing_artifact")
        self.assertFalse(payload["status_update_allowed"])

    def test_markdown_writer_preserves_safety_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "safe.md"
            source.write_text("NCS ?덈젴 異붿쿇\n", encoding="utf-8")
            markdown = Path(tmpdir) / "audit.md"

            payload = audit_paths([source])
            write_markdown(markdown, payload)
            text = markdown.read_text(encoding="utf-8")

        self.assertIn("review_artifact_readability_audit_v1", text)
        self.assertIn("report_only: true", text)
        self.assertIn("status_update_allowed: false", text)
        self.assertIn("approval_claim: false", text)
        self.assertIn("human_decision_required: true", text)
        self.assertIn("action:", text)

    def test_cli_exits_zero_by_default_and_nonzero_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.md"
            path.write_text("NCS ?덈젴 異붿쿇\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["audit_review_artifact_readability.py", str(path)]):
                with contextlib.redirect_stdout(stdout):
                    default_result = readability_main()
            with patch.object(
                sys,
                "argv",
                ["audit_review_artifact_readability.py", "--strict", str(path)],
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    strict_result = readability_main()

        self.assertEqual(default_result, 0)
        self.assertEqual(strict_result, 1)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_cli_auto_discovers_reports_dir_when_no_paths_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()
            artifact = reports_dir / "human_review_packet.md"
            artifact.write_text("사람 검토 전 표시 확인용\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(
                sys,
                "argv",
                [
                    "audit_review_artifact_readability.py",
                    "--reports-dir",
                    str(reports_dir),
                ],
            ):
                with contextlib.redirect_stdout(stdout):
                    result = readability_main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["scan"]["auto_discovered"])
        self.assertIn("*review_artifact_readability*.json", payload["scan"]["exclude_patterns"])
        self.assertFalse(payload["scan"]["limit_reached"])
        self.assertEqual(payload["artifact_count"], 1)

    def test_cli_creates_nested_markdown_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()
            artifact = reports_dir / "human_review_packet.md"
            artifact.write_text("사람 검토 전 표시 확인용\n", encoding="utf-8")
            markdown = Path(tmpdir) / "nested" / "audit.md"
            stdout = io.StringIO()

            with patch.object(
                sys,
                "argv",
                [
                    "audit_review_artifact_readability.py",
                    "--reports-dir",
                    str(reports_dir),
                    "--markdown-out",
                    str(markdown),
                ],
            ):
                with contextlib.redirect_stdout(stdout):
                    result = readability_main()

            self.assertEqual(result, 0)
            self.assertTrue(markdown.exists())


if __name__ == "__main__":
    unittest.main()
