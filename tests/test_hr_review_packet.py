import csv
import tempfile
import unittest
from pathlib import Path

from scripts import build_hr_review_packet


class HrReviewPacketContractTests(unittest.TestCase):
    def test_review_only_contract_helpers_require_all_guardrails(self) -> None:
        payload = {}
        build_hr_review_packet.apply_review_only_contract(payload)

        build_hr_review_packet.require_review_only_contract(payload, "payload")
        self.assertFalse(payload["approval_claim"])
        self.assertFalse(payload["db_writes"])
        self.assertFalse(payload["active_scoring_source"])
        self.assertFalse(payload["status_update_allowed"])
        self.assertTrue(payload["review_only"])
        self.assertTrue(payload["non_scoring"])

    def test_review_only_contract_rejects_missing_or_scoring_payload(self) -> None:
        with self.assertRaises(ValueError):
            build_hr_review_packet.require_review_only_contract(
                {"approval_claim": False, "db_writes": False},
                "payload",
            )
        with self.assertRaises(ValueError):
            build_hr_review_packet.require_review_only_rows(
                [
                    {
                        "approval_claim": False,
                        "db_writes": False,
                        "active_scoring_source": True,
                        "status_update_allowed": False,
                        "review_only": True,
                        "non_scoring": True,
                    }
                ],
                "rows",
            )

    def test_human_review_template_is_non_scoring_and_no_write(self) -> None:
        template = build_hr_review_packet.human_review_template()

        self.assertEqual(template["decision_status"], "pending_human_review")
        self.assertFalse(template["db_writes_allowed"])
        build_hr_review_packet.require_review_only_contract(
            template,
            "human_review_template",
        )

    def test_artifact_paths_can_be_date_parameterized(self) -> None:
        paths = build_hr_review_packet.artifact_paths_for_date(
            "20260620",
            reports_dir=Path("tmp_reports"),
        )

        self.assertTrue(str(paths["main_json"]).endswith("tmp_reports\\aihr_hr_job_movement_learning_path_review_20260620.json"))
        self.assertTrue(str(paths["cards_json"]).endswith("tmp_reports\\aihr_hr_learning_path_ocr_context_cards_20260620.json"))
        self.assertTrue(str(paths["packet_html"]).endswith("tmp_reports\\aihr_hr_transferability_human_review_packet_20260620.html"))

    def test_configure_artifact_paths_accepts_explicit_outputs(self) -> None:
        original = (
            build_hr_review_packet.DB_PATH,
            build_hr_review_packet.MAIN_JSON,
            build_hr_review_packet.MAIN_MD,
            build_hr_review_packet.MANIFEST_JSON,
            build_hr_review_packet.CARDS_JSON,
            build_hr_review_packet.CARDS_MD,
            build_hr_review_packet.PACKET_JSON,
            build_hr_review_packet.PACKET_MD,
            build_hr_review_packet.PACKET_CSV,
            build_hr_review_packet.PACKET_HTML,
            build_hr_review_packet.MATRIX_CSV,
        )
        try:
            build_hr_review_packet.configure_artifact_paths(
                date_stamp="20260620",
                reports_dir=Path("tmp_reports"),
                db_path=Path("tmp.db"),
                main_json=Path("custom_main.json"),
                cards_json=Path("custom_cards.json"),
                packet_html=Path("custom_packet.html"),
            )

            self.assertTrue(str(build_hr_review_packet.DB_PATH).endswith("tmp.db"))
            self.assertTrue(str(build_hr_review_packet.MAIN_JSON).endswith("custom_main.json"))
            self.assertTrue(str(build_hr_review_packet.CARDS_JSON).endswith("custom_cards.json"))
            self.assertTrue(str(build_hr_review_packet.PACKET_HTML).endswith("custom_packet.html"))
            self.assertTrue(str(build_hr_review_packet.PACKET_JSON).endswith("tmp_reports\\aihr_hr_transferability_human_review_packet_20260620.json"))
        finally:
            (
                build_hr_review_packet.DB_PATH,
                build_hr_review_packet.MAIN_JSON,
                build_hr_review_packet.MAIN_MD,
                build_hr_review_packet.MANIFEST_JSON,
                build_hr_review_packet.CARDS_JSON,
                build_hr_review_packet.CARDS_MD,
                build_hr_review_packet.PACKET_JSON,
                build_hr_review_packet.PACKET_MD,
                build_hr_review_packet.PACKET_CSV,
                build_hr_review_packet.PACKET_HTML,
                build_hr_review_packet.MATRIX_CSV,
            ) = original

    def test_review_packet_csv_outputs_escape_formula_like_cells(self) -> None:
        original = (
            build_hr_review_packet.PACKET_CSV,
            build_hr_review_packet.MATRIX_CSV,
        )
        pair = {
            "source_unit_code": "s1",
            "source_unit_name": "=source",
            "source_level": 3,
            "target_unit_code": "t1",
            "target_unit_name": " @target",
            "target_level": 4,
            "movement_type": "horizontal",
            "level_delta": 1,
            "exact_ksa_overlap_ratio": 0.5,
            "shared_ksa_concept_count": 1,
            "target_ksa_concept_count": 2,
            "target_only_ksa_concept_count": 1,
            "task_similarity_max": 0.7,
            "task_similarity_link_count": 1,
            "report_movement_component": "task_ksa_chain",
            "report_grounded_transferability_ratio": 0.6,
            "review_priority": "high",
            "shared_ksa_concepts": [{"concept_name": "+SUM(1,1)"}],
            "target_only_gap_concepts": [{"concept_name": " @bad"}],
            "source_learning_module_context": {"snippets": {"module_goal": {"page": "-1"}}},
            "target_learning_module_context": {"snippets": {"module_goal": {"page": "@2"}}},
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                build_hr_review_packet.PACKET_CSV = tmp_path / "packet.csv"
                build_hr_review_packet.MATRIX_CSV = tmp_path / "matrix.csv"

                build_hr_review_packet.write_matrix_csv([pair])
                build_hr_review_packet.write_packet_csv([pair])

                with build_hr_review_packet.MATRIX_CSV.open(encoding="utf-8-sig", newline="") as handle:
                    matrix_rows = list(csv.DictReader(handle))
                with build_hr_review_packet.PACKET_CSV.open(encoding="utf-8-sig", newline="") as handle:
                    packet_rows = list(csv.DictReader(handle))

            self.assertEqual(matrix_rows[0]["source_unit_name"], "'=source")
            self.assertEqual(matrix_rows[0]["target_unit_name"], "' @target")
            self.assertEqual(packet_rows[0]["source_unit_name"], "'=source")
            self.assertEqual(packet_rows[0]["target_unit_name"], "' @target")
            self.assertEqual(packet_rows[0]["shared_ksa_concepts"], "'+SUM(1,1)")
            self.assertEqual(packet_rows[0]["target_gap_concepts"], "' @bad")
            self.assertEqual(packet_rows[0]["source_module_goal_page"], "'-1")
            self.assertEqual(packet_rows[0]["target_module_goal_page"], "'@2")
        finally:
            (
                build_hr_review_packet.PACKET_CSV,
                build_hr_review_packet.MATRIX_CSV,
            ) = original


if __name__ == "__main__":
    unittest.main()
