from __future__ import annotations

import unittest

from ncs_mcp.error_codes import error_metadata


class NcsSearchErrorCodeTests(unittest.TestCase):
    def test_scope_resolution_error_is_known_validation_error(self) -> None:
        metadata = error_metadata("route_context_required")

        self.assertTrue(metadata["known"])
        self.assertEqual(metadata["category"], "validation")
        self.assertFalse(metadata["retryable"])

    def test_scope_containment_error_is_known_policy_error(self) -> None:
        metadata = error_metadata("search_scope_containment_violation")

        self.assertTrue(metadata["known"])
        self.assertEqual(metadata["category"], "policy")
        self.assertEqual(metadata["severity"], "error")
        self.assertFalse(metadata["retryable"])

    def test_clarification_error_is_known_validation_error(self) -> None:
        metadata = error_metadata("needs_clarification")

        self.assertTrue(metadata["known"])
        self.assertEqual(metadata["category"], "validation")
        self.assertEqual(metadata["severity"], "warn")
        self.assertFalse(metadata["retryable"])


if __name__ == "__main__":
    unittest.main()
