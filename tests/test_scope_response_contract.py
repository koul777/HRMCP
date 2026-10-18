from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402


class ScopeResponseContractTests(unittest.TestCase):
    def call(self, payload):
        with patch.object(server, "open_db", return_value=nullcontext(object())), \
             patch.object(server, "training_resolve_ncs_query_scope", return_value=payload):
            return server.resolve_ncs_query_scope(payload.get("query", "query"))

    def test_completed_empty_lookup_is_not_a_tool_failure(self):
        for query in ("", "no matching source name"):
            with self.subTest(query=query):
                payload = {"ok": False, "query": query, "normalized_query": query.replace(" ", ""),
                           "candidates": [], "selected_candidate": None, "needs_clarification": False}
                result = self.call(payload)
                self.assertTrue(result["ok"])
                self.assertIsNone(result["error"])
                self.assertEqual(result["candidates"], [])
                self.assertIsNone(result["selected_candidate"])
                self.assertEqual(result["query"], query)
                self.assertEqual(result["data"]["candidates"], [])
                self.assertIn("ontology_concepts", result["audit"]["data_sources"])
                self.assertFalse(payload["ok"], "do not mutate the domain resolver's result")

    def test_explicit_failure_or_incomplete_payload_is_not_promoted(self):
        ordinary = {"ok": False, "query": "query", "normalized_query": "query", "candidates": []}
        invalid = [
            {**ordinary, "error": {"code": "database_failed"}},
            {**ordinary, "error": None},
            {**ordinary, "isError": True},
            {**ordinary, "timeout": True},
            {**ordinary, "timed_out": True},
            {key: value for key, value in ordinary.items() if key != "normalized_query"},
            {**ordinary, "candidates": [{"unit_code": "U1"}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                result = self.call(payload)
                self.assertFalse(result["ok"])
                self.assertIsNotNone(result["error"])
        self.assertEqual(self.call(invalid[0])["error"]["code"], "database_failed")

    def test_found_and_ambiguous_results_keep_their_selection_state(self):
        for ambiguous in (False, True):
            payload = {"ok": True, "query": "query", "normalized_query": "query",
                       "candidates": [{"unit_code": "U1"}], "needs_clarification": ambiguous,
                       "selected_candidate": None if ambiguous else {"unit_code": "U1"}}
            result = self.call(payload)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["candidates"], payload["candidates"])
            self.assertEqual(result["needs_clarification"], ambiguous)
            self.assertEqual(result["selected_candidate"], payload["selected_candidate"])


if __name__ == "__main__":
    unittest.main()
