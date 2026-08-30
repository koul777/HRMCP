from __future__ import annotations

from pathlib import Path
import unittest

from scripts import build_ncs_search_eval_pack as eval_pack


class SearchEvaluationCandidatePackTests(unittest.TestCase):
    def test_catalog_has_required_50_case_coverage(self) -> None:
        summary = eval_pack.summarize_catalog(eval_pack.CANDIDATES)

        self.assertEqual(summary["candidate_count"], 50)
        self.assertEqual(summary["unique_query_count"], 50)
        self.assertTrue(all(summary["minimum_coverage_checks"].values()))
        self.assertGreaterEqual(summary["tag_counts"]["hr_core"], 15)
        self.assertGreaterEqual(summary["tag_counts"]["direct_ksa"], 10)

    def test_every_case_is_unlabeled_and_human_gated(self) -> None:
        case_ids = set()
        for case in eval_pack.CANDIDATES:
            case_ids.add(case["case_id"])
            self.assertEqual(case["evaluation_status"], "candidate_eval")
            self.assertFalse(case["gold_label_present"])
            self.assertTrue(case["requires_human_review"])
            self.assertFalse(case["human_decision_present"])
            self.assertIsNone(case["human_label_template"]["relevance_grade_0_to_3"])
            self.assertIsNone(case["human_label_template"]["off_scope"])
            self.assertEqual(case["human_label_template"]["acceptable_result_ids"], [])
            self.assertTrue(case["preferred_result_type_candidates"])
            self.assertTrue(case["challenge_reason"])
        self.assertEqual(len(case_ids), 50)
        off_scope_cases = [
            case
            for case in eval_pack.CANDIDATES
            if "off_scope_candidate" in case["tags"]
        ]
        self.assertGreaterEqual(len(off_scope_cases), 5)

    def test_measurement_is_observation_not_relevance_judgment(self) -> None:
        selected = list(eval_pack.CANDIDATES[:3])

        def fake_search(query: str, scope: str, limit: int) -> dict:
            if query == selected[1]["query"]:
                return {"results": [], "counts_by_type": {}}
            result_type = "ksa" if scope == "ksa" else "unit"
            return {
                "results": [
                    {
                        "type": result_type,
                        "id": f"{query}-1",
                        "unit_name": f"{query} 후보",
                    }
                ][:limit],
                "counts_by_type": {result_type: 1},
            }

        report = eval_pack.build_report(
            db_path=Path("candidate-test.db"),
            runs=2,
            limit=5,
            measure=True,
            search_fn=fake_search,
            candidates=selected,
        )

        aggregate = report["current_search_observation"]["aggregate"]
        self.assertEqual(aggregate["measured_case_count"], 3)
        self.assertEqual(aggregate["zero_hit_count"], 1)
        self.assertFalse(report["evaluation_contract"]["gold_dataset"])
        self.assertFalse(report["evaluation_contract"]["automatic_relevance_labels"])
        self.assertFalse(report["evaluation_contract"]["approval_claim"])
        self.assertTrue(
            all(
                not record["relevance_judgment_present"]
                for record in report["current_search_observation"]["records"]
            )
        )

    def test_skip_measure_keeps_candidate_pack_reproducible(self) -> None:
        report = eval_pack.build_report(
            db_path=Path("missing-is-allowed-when-not-measuring.db"),
            runs=1,
            limit=20,
            measure=False,
        )

        self.assertEqual(report["schema"], "ncs_search_eval_candidate_pack_v1")
        self.assertEqual(report["catalog_summary"]["candidate_count"], 50)
        self.assertFalse(report["current_search_observation"]["executed"])
        self.assertFalse(report["safety"]["database_writes"])
        self.assertFalse(report["safety"]["raw_ksa_mutation"])

    def test_markdown_states_candidate_and_not_gold(self) -> None:
        report = eval_pack.build_report(
            db_path=Path("candidate-test.db"),
            runs=1,
            limit=20,
            measure=False,
        )
        markdown = eval_pack.render_markdown(report)

        self.assertIn("Status: `candidate_eval`", markdown)
        self.assertIn("Gold dataset: `false`", markdown)
        self.assertIn("Human review required: `true`", markdown)
        self.assertIn("NCS-EVAL-050", markdown)


if __name__ == "__main__":
    unittest.main()
