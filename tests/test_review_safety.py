from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.review_safety import (
    evidence_refs_json_is_nonempty_string_list,
    is_portable_reports_packet_ref,
    normalize_source_decision_packet_ref,
    resolve_repo_reports_artifact,
    review_packet_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_TEST_ROOT = ROOT / "reports" / "_test_review_packets"


class ReviewSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        REPORT_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self._cleanup_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self._cleanup_paths:
            if path.exists():
                shutil.rmtree(path)
        if REPORT_TEST_ROOT.exists() and not any(REPORT_TEST_ROOT.iterdir()):
            REPORT_TEST_ROOT.rmdir()

    def _repo_packet(self, suffix: str = ".md") -> Path:
        packet_dir = Path(tempfile.mkdtemp(dir=REPORT_TEST_ROOT))
        self._cleanup_paths.append(packet_dir)
        packet = packet_dir / f"review_packet{suffix}"
        packet.write_text("human decision packet", encoding="utf-8")
        return packet

    def test_resolves_only_project_reports_artifacts(self) -> None:
        packet = self._repo_packet()
        self.assertEqual(resolve_repo_reports_artifact(str(packet)), packet.resolve())
        self.assertEqual(
            resolve_repo_reports_artifact(packet.relative_to(ROOT).as_posix()),
            packet.resolve(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            off_repo_packet = Path(tmp) / "reports" / "review_packet.md"
            off_repo_packet.parent.mkdir()
            off_repo_packet.write_text("outside repo reports", encoding="utf-8")
            self.assertIsNone(resolve_repo_reports_artifact(str(off_repo_packet)))

    def test_rejects_unsupported_extensions(self) -> None:
        packet = self._repo_packet(".txt")
        self.assertIsNone(resolve_repo_reports_artifact(str(packet)))

    def test_normalizes_repo_local_absolute_packet_to_portable_ref(self) -> None:
        packet = self._repo_packet()
        expected = packet.relative_to(ROOT).as_posix() + "#row:1"
        self.assertEqual(
            normalize_source_decision_packet_ref(f"{packet}#row:1"),
            expected,
        )
        self.assertEqual(
            normalize_source_decision_packet_ref(expected),
            expected,
        )

    def test_portable_reports_packet_ref_contract(self) -> None:
        self.assertTrue(is_portable_reports_packet_ref(""))
        self.assertTrue(is_portable_reports_packet_ref("reports/review_packet.md#row:1"))
        self.assertTrue(is_portable_reports_packet_ref("reports\\review_packet.csv"))
        self.assertFalse(is_portable_reports_packet_ref("review_packet.md"))
        self.assertFalse(is_portable_reports_packet_ref("../reports/review_packet.md"))
        self.assertFalse(is_portable_reports_packet_ref("reports/../review_packet.md"))
        self.assertFalse(is_portable_reports_packet_ref(str(self._repo_packet())))

    def test_hash_and_evidence_refs_helpers(self) -> None:
        packet = self._repo_packet()
        self.assertRegex(review_packet_sha256(packet), r"^[0-9a-f]{64}$")
        self.assertTrue(evidence_refs_json_is_nonempty_string_list('["packet#row:1"]'))
        self.assertFalse(evidence_refs_json_is_nonempty_string_list("[]"))
        self.assertFalse(evidence_refs_json_is_nonempty_string_list("[1]"))
        self.assertFalse(evidence_refs_json_is_nonempty_string_list('[""]'))


if __name__ == "__main__":
    unittest.main()
