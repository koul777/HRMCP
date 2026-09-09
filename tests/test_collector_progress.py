"""Collector progress counts only pages whose persistence succeeded."""

from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ncs_mcp import job_base_api, training_course_api


class CollectorProgressTests(unittest.TestCase):
    def run_collector(self, kind, payloads, **kwargs):
        module = training_course_api if kind == "training" else job_base_api
        fetch_name = "fetch_training_course_page" if kind == "training" else "fetch_job_base_page"
        upsert_name = "upsert_training_courses" if kind == "training" else "upsert_job_base_rows"
        collector = module.collect_training_courses if kind == "training" else module.collect_job_base_competencies
        upsert_result = 1 if kind == "training" else {"rows_processed": 1, "links_upserted": 1, "missing_local_units": 0}
        events = []
        with ExitStack() as stack:
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = [0]
            stack.enter_context(patch.object(module, "connect", return_value=conn))
            stack.enter_context(patch.object(module, "initialize_database"))
            stack.enter_context(patch.object(module, fetch_name, side_effect=payloads))
            stack.enter_context(patch.object(module, upsert_name, return_value=upsert_result))
            if kind == "job":
                stack.enter_context(patch.object(module, "job_base_summary", return_value={}))
            result = collector(Path("unused.db"), "secret-must-not-be-in-progress", major_code="20", progress_callback=events.append, **kwargs)
        self.assertNotIn("secret-must-not-be-in-progress", str(events))
        self.assertTrue(all("20" in event["stage"] and event["unit"] == "페이지" for event in events))
        return result, events

    @staticmethod
    def payload(total=3, code="000"):
        return {"code": code, "message": "", "total_count": total, "total_page": total, "rows": [{}], "request": {}}

    def test_success_pages_follow_reported_total(self):
        for kind in ("training", "job"):
            with self.subTest(kind=kind):
                result, events = self.run_collector(kind, [self.payload() for _ in range(3)])
                self.assertEqual(result["pages_processed"], 3)
                self.assertEqual([(e["completed"], e["total"]) for e in events], [(0, None), (1, 3), (2, 3), (3, 3)])

    def test_failure_does_not_increment_or_finish(self):
        for kind in ("training", "job"):
            with self.subTest(kind=kind):
                result, events = self.run_collector(kind, [self.payload(), self.payload(code="500")])
                self.assertEqual(result["pages_processed"], 1)
                self.assertEqual([(e["completed"], e["total"]) for e in events], [(0, None), (1, 3)])

    def test_requested_page_range_is_denominator(self):
        for kind in ("training", "job"):
            with self.subTest(kind=kind):
                _, events = self.run_collector(kind, [self.payload(total=10), self.payload(total=10)], page_no=5, max_pages=2)
                self.assertEqual([(e["completed"], e["total"]) for e in events], [(0, None), (1, 2), (2, 2)])

    def test_unknown_api_total_stays_unknown(self):
        for kind in ("training", "job"):
            with self.subTest(kind=kind):
                _, events = self.run_collector(kind, [self.payload(total=0)])
                self.assertEqual(events[-1]["total"], None)


if __name__ == "__main__":
    unittest.main()
