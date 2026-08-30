from __future__ import annotations

import csv
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_ncs_search_relevance_review_pack as review_pack  # noqa: E402


class NcsSearchRelevanceReviewPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "schema": "ncs_search_eval_candidate_pack_v1",
            "candidates": [
                {
                    "case_id": "NCS-EVAL-001",
                    "query": "채용 면접",
                    "domain_group": "hr_core",
                    "intent_candidate": "task_training",
                    "scope_candidate": "all",
                    "preferred_result_type_candidates": ["unit", "ksa"],
                    "tags": ["hr_core"],
                    "challenge_reason": "candidate only",
                    "evaluation_status": "candidate_eval",
                    "gold_label_present": False,
                    "human_decision_present": False,
                }
            ],
        }
        self.raw_results = [
            {
                "type": "unit",
                "id": "U-1",
                "text": "인력채용",
                "api_definition": "조직에 적합한 인재를 확보하기 위한 능력",
                "path": {"major_code": "02", "major": "경영"},
                "match_mode": "token_or",
                "matched_tokens": ["채용"],
                "match_fields": ["unit_name", "definition"],
            },
            {
                "type": "ksa",
                "id": 7,
                "text": "면접 방식에 대한 지식" + (" 근거" * 200),
                "ksa_type": "지식",
                "path": {
                    "unit_code": "U-2",
                    "unit_name": "취업상담",
                    "element_id": 3,
                    "element_name": "면접 지원하기",
                },
                "match_mode": "phrase",
                "matched_tokens": ["면접"],
                "match_fields": ["ksa_text"],
            },
        ]

    def _search(self, query: str, **kwargs):
        self.assertEqual(query, "채용 면접")
        self.assertEqual(kwargs, {"scope": "all", "limit": 10, "offset": 0})
        return {
            "normalized_query": query,
            "query_tokens": ["채용", "면접"],
            "match_mode": "mixed",
            "match_mode_by_type": {"unit": "token_or", "ksa": "phrase"},
            "counts_by_type": {"unit": 1, "ksa": 1},
            "results": self.raw_results,
        }

    def _build(self):
        observations = review_pack.collect_search_observations(
            self.source["candidates"],
            search_fn=self._search,
            limit=10,
            stability_runs=2,
        )
        return review_pack.build_review_pack(
            self.source,
            observations,
            unit_scopes={
                "U-1": {"unit_name": "인력채용", "major_code": "02", "major_name": "경영"},
                "U-2": {"unit_name": "취업상담", "major_code": "07", "major_name": "사회복지"},
            },
            limit=10,
            generated_at="2026-08-30T00:00:00+00:00",
        )

    def test_candidate_packet_has_no_gold_approval_or_write_claim(self) -> None:
        pack = self._build()

        self.assertEqual(pack["schema"], review_pack.SCHEMA)
        self.assertTrue(pack["candidate_eval"])
        self.assertFalse(pack["gold"])
        self.assertFalse(pack["approval_claim"])
        self.assertFalse(pack["db_writes"])
        self.assertEqual(pack["review_contract"]["status"], "human_review_required")
        self.assertFalse(pack["review_contract"]["automatic_relevance_labels"])
        self.assertEqual(pack["summary"]["stable_rank_order_case_count"], 1)

    def test_ranked_results_use_stable_ids_bounded_evidence_and_scope(self) -> None:
        pack = self._build()
        first, second = pack["cases"][0]["results"]

        self.assertEqual(first["stable_result_id"], "unit:U-1")
        self.assertEqual(second["stable_result_id"], "ksa:7")
        self.assertEqual([first["rank"], second["rank"]], [1, 2])
        self.assertEqual(first["match"]["tier"], 2)
        self.assertEqual(second["match"]["tier"], 0)
        self.assertEqual(second["major_scope"]["major_code"], "07")
        self.assertEqual(second["unit_scope"]["unit_code"], "U-2")
        self.assertLessEqual(len(second["snippet"]), 320)
        self.assertTrue(second["snippet"].endswith("…"))
        self.assertGreater(len(self.raw_results[1]["text"]), 320)

    def test_decision_csv_starts_with_every_human_field_blank(self) -> None:
        rows = list(csv.DictReader(io.StringIO(review_pack.decision_csv_text(self._build()))))

        self.assertEqual(len(rows), 2)
        for row in rows:
            for field in review_pack.DECISION_FIELDS:
                self.assertEqual(row[field], "", (field, row))
        self.assertEqual(rows[0]["stable_result_id"], "unit:U-1")

    def test_source_with_gold_or_human_decision_is_rejected(self) -> None:
        bad = {**self.source, "candidates": [{**self.source["candidates"][0], "gold_label_present": True}]}
        with self.assertRaisesRegex(ValueError, "gold"):
            review_pack._validate_source(bad)

        bad = {**self.source, "candidates": [{**self.source["candidates"][0], "human_decision_present": True}]}
        with self.assertRaisesRegex(ValueError, "human decision"):
            review_pack._validate_source(bad)


if __name__ == "__main__":
    unittest.main()
