from __future__ import annotations

import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_remote_ncs_performance.py"
SPEC = importlib.util.spec_from_file_location("audit_remote_ncs_performance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Response:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers = _Headers({"Content-Type": "application/json"})
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LatencySummaryTests(unittest.TestCase):
    def test_latency_summary_has_required_statistics(self):
        result = MODULE.latency_summary([100, 200, 300, 400, 500])
        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["p50_ms"], 300.0)
        self.assertEqual(result["p95_ms"], 480.0)
        self.assertEqual(result["max_ms"], 500.0)
        self.assertTrue(math.isclose(result["coefficient_of_variation"], 0.471405))

    def test_latency_summary_empty_is_explicit(self):
        result = MODULE.latency_summary([])
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["p95_ms"])
        self.assertIsNone(result["coefficient_of_variation"])


class SearchContractTests(unittest.TestCase):
    def test_parses_counts_offsets_and_missing_match_metadata(self):
        text = (
            "## NCS search\n"
            "20\uac74 \uc911 5\uac74 \ud45c\uc2dc\n"
            "- \uc720\ud615\ubcc4 \ubc18\ud658: unit 5\uac74, element 5\uac74, criteria 5\uac74, ksa 5\uac74\n"
            "- current `offset=0`\n- next `offset=20`"
        )
        payload = {"result": {"content": [{"type": "text", "text": text}]}}
        result = MODULE.parse_search_contract(payload)
        self.assertTrue(result["found"])
        self.assertFalse(result["zero_hit"])
        self.assertEqual(result["result_count"], 20)
        self.assertEqual(result["counts_by_type"]["ksa"], 5)
        self.assertEqual(result["current_offset"], 0)
        self.assertEqual(result["next_offset"], 20)
        self.assertFalse(result["match_metadata"]["present"])

    def test_preview_fingerprint_ignores_audit_timestamp(self):
        body = (
            "20\uac74 \uc911 5\uac74 \ud45c\uc2dc\n"
            "| \ub2a5\ub825\ub2e8\uc704\uba85 | \ucf54\ub4dc |\n| --- | --- |\n"
            "| \uc778\ub825\ucc44\uc6a9 | 0202020103_23v4 |\n"
            "audit.generated_at: `{timestamp}`"
        )
        one = {"result": {"content": [{"type": "text", "text": body.format(timestamp="one")}]} }
        two = {"result": {"content": [{"type": "text", "text": body.format(timestamp="two")}]} }
        first = MODULE.parse_search_contract(one)
        second = MODULE.parse_search_contract(two)
        self.assertEqual(first["response_sha256"], second["response_sha256"])
        self.assertEqual(first["preview_result_sha256"], second["preview_result_sha256"])

    def test_structured_match_metadata_is_whitelisted(self):
        payload = {
            "result": {
                "content": [{"type": "text", "text": "20\uac74 \uc911 5\uac74 \ud45c\uc2dc"}],
                "structuredContent": {
                    "match_tier": "or",
                    "matched_tokens": ["data"],
                    "private_note": "do-not-persist",
                },
            }
        }
        result = MODULE.parse_search_contract(payload)
        fields = result["match_metadata"]["structured_fields"]
        self.assertEqual(fields, {"match_tier": "or", "matched_tokens": ["data"]})
        self.assertNotIn("private_note", json.dumps(result))


class RedactionTests(unittest.TestCase):
    def test_bootstrap_extractor_keeps_only_numeric_boolean_metrics(self):
        payload = {
            "runtime": {
                "bootstrap": {
                    "archive_hash_ms": 12.5,
                    "snapshot_path": "C:/secret/db.sqlite",
                    "token": "secret-token",
                    "extract_ok": True,
                }
            },
            "authorization": "Bearer secret",
        }
        metrics = MODULE.extract_bootstrap_metrics(payload, "health")
        serialized = json.dumps(metrics)
        self.assertIn("archive_hash_ms", serialized)
        self.assertIn("extract_ok", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("C:/secret", serialized)
        self.assertTrue(all(item["measurement_scope"] == "process_level" for item in metrics))

    def test_public_observation_persists_hash_not_body(self):
        observation = MODULE.HttpObservation(
            status=200,
            elapsed_ms=1.25,
            content_type="application/json",
            body=b'{"secret":"never-write-this"}',
        )
        public = MODULE._public_observation(observation)
        serialized = json.dumps(public)
        self.assertNotIn("never-write-this", serialized)
        self.assertEqual(len(public["response_body_sha256"]), 64)


class HttpClientMockTests(unittest.TestCase):
    @patch.object(MODULE.urllib.request, "urlopen")
    def test_transport_ascii_escapes_korean_json(self, urlopen: MagicMock):
        urlopen.return_value = _Response(200, {"jsonrpc": "2.0", "id": 1, "result": {}})
        transport = MODULE.UrlLibTransport(5.0, 0, 0.0, sleeper=lambda _value: None)
        result = transport(
            "https://example.test/api/mcp",
            "POST",
            MODULE._search_payload(1, "\ucc44\uc6a9", 0),
        )
        self.assertEqual(result.status, 200)
        request = urlopen.call_args.args[0]
        self.assertIn(b"\\ucc44\\uc6a9", request.data)
        self.assertNotIn("\ucc44\uc6a9".encode("utf-8"), request.data)

    def test_measure_scenario_separates_first_from_five_warm_samples(self):
        elapsed = iter([900, 100, 110, 120, 130, 140])

        def fake_request(_url, _method, _payload):
            return MODULE.HttpObservation(200, next(elapsed), "application/json", b"{}")

        result, first, warm = MODULE.measure_scenario(
            fake_request,
            "https://example.test/api/health",
            "GET",
            lambda _index: None,
            200,
            5,
            0.1,
            lambda _value: None,
        )
        self.assertEqual(first.elapsed_ms, 900)
        self.assertEqual(len(warm), 5)
        self.assertEqual(result["warm_latency"]["p50_ms"], 120.0)
        self.assertFalse(result["first_observed_is_cold_claim"])


class SchemaTests(unittest.TestCase):
    def test_base_url_requires_explicit_value_or_environment(self):
        with patch.dict(MODULE.os.environ, {}, clear=False):
            with self.assertRaises(SystemExit):
                MODULE.resolve_base_url(None)

    def test_base_url_can_come_from_environment(self):
        with patch.dict(
            MODULE.os.environ,
            {MODULE.BASE_URL_ENV_KEY: "https://example.test"},
            clear=False,
        ):
            self.assertEqual(
                MODULE.resolve_base_url(None), "https://example.test"
            )

    def test_readiness_contract_extracts_fast_path_fields(self):
        payload = {
            "status": "ready",
            "runtime": {
                "database": {
                    "ready": True,
                    "readiness_count_source": "verified_snapshot_metadata",
                }
            },
            "bootstrap": {
                "schema": "ncs_vercel_bootstrap_metrics_v2",
                "status": "ready",
                "source": "local_snapshot",
                "elapsed_ms": 4406.69,
                "process_level_metrics": True,
                "request_level_metrics": False,
                "local_snapshot": {"readiness_fast_path_configured": True},
            },
        }
        contract = MODULE._endpoint_contract(payload, "ready")
        self.assertEqual(contract["readiness_count_source"], "verified_snapshot_metadata")
        self.assertTrue(contract["bootstrap"]["readiness_fast_path_configured"])
        self.assertEqual(contract["bootstrap"]["elapsed_ms"], 4406.69)

    def test_baseline_comparison_reports_improvement_and_no_rollback(self):
        baseline = {
            "schema": "ncs_remote_performance_audit_v1",
            "generated_at": "before",
            "summary": {"search_zero_hit_count": 0},
            "endpoints": {"ready": {"warm_latency": {"p50_ms": 1000}}},
            "searches": [{"query": "q", "warm_latency": {"p50_ms": 500}}],
        }
        report = {
            "summary": {"contract_ok": True, "search_zero_hit_count": 0},
            "deployment_evidence": {"git_commit": "abcdef012345"},
            "endpoints": {
                "ready": {
                    "warm_latency": {"p50_ms": 200},
                    "response_contract": {
                        "readiness_count_source": "verified_snapshot_metadata",
                        "bootstrap": {
                            "readiness_fast_path_configured": True,
                            "elapsed_ms": 4000,
                        },
                    },
                }
            },
            "mcp": {"tools_list": {"response_contract": {"tool_count": 7}}},
            "pagination": {"contract_ok": True},
            "searches": [{"query": "q", "warm_latency": {"p50_ms": 250}}],
        }
        MODULE.add_baseline_comparison(report, baseline, "abcdef0", 7)
        self.assertEqual(
            report["baseline_comparison"]["ready_p50_ms"]["improvement_percent"],
            80.0,
        )
        self.assertEqual(report["release_assessment"]["severity"], "none")
        self.assertFalse(report["release_assessment"]["rollback_triggered"])

    def test_commit_mismatch_is_critical_rollback_trigger(self):
        baseline = {
            "schema": "v1",
            "summary": {"search_zero_hit_count": 0},
            "endpoints": {"ready": {"warm_latency": {"p50_ms": 100}}},
            "searches": [],
        }
        report = {
            "summary": {"contract_ok": True, "search_zero_hit_count": 0},
            "deployment_evidence": {"git_commit": "wrong"},
            "endpoints": {
                "ready": {
                    "warm_latency": {"p50_ms": 100},
                    "response_contract": {
                        "readiness_count_source": "verified_snapshot_metadata",
                        "bootstrap": {"readiness_fast_path_configured": True},
                    },
                }
            },
            "mcp": {"tools_list": {"response_contract": {"tool_count": 7}}},
            "pagination": {"contract_ok": True},
            "searches": [],
        }
        MODULE.add_baseline_comparison(report, baseline, "expected", 7)
        self.assertEqual(report["release_assessment"]["severity"], "critical")
        self.assertTrue(report["release_assessment"]["rollback_triggered"])

    def test_additional_baseline_comparison_is_kept_separate(self):
        baseline = {
            "schema": "v1",
            "generated_at": "original",
            "summary": {"search_zero_hit_count": 0},
            "endpoints": {"ready": {"warm_latency": {"p50_ms": 1000}}},
            "searches": [],
        }
        report = {
            "summary": {"contract_ok": True, "search_zero_hit_count": 0},
            "deployment_evidence": {"git_commit": "abcdef012345"},
            "endpoints": {
                "ready": {
                    "warm_latency": {"p50_ms": 250},
                    "response_contract": {
                        "readiness_count_source": "verified_snapshot_metadata",
                        "bootstrap": {"readiness_fast_path_configured": True},
                    },
                }
            },
            "mcp": {"tools_list": {"response_contract": {"tool_count": 7}}},
            "pagination": {"contract_ok": True},
            "searches": [],
        }
        MODULE.add_additional_baseline_comparison(
            report, "original", baseline, "abcdef0", 7
        )
        item = report["additional_baseline_comparisons"]["original"]
        self.assertEqual(item["comparison"]["ready_p50_ms"]["improvement_percent"], 75.0)
        self.assertEqual(item["assessment"]["severity"], "none")
        self.assertNotIn("baseline_comparison", report)

    def test_markdown_renders_required_sections_without_raw_body(self):
        scenario = {
            "expected_status": 200,
            "first_observed_request": {"elapsed_ms": 10},
            "warm_latency": {
                "sample_count": 5,
                "p50_ms": 5,
                "p95_ms": 7,
                "max_ms": 8,
                "coefficient_of_variation": 0.1,
            },
            "contract_status_ok": True,
        }
        report = {
            "schema": "ncs_remote_performance_audit_v1",
            "generated_at": "2026-08-30T00:00:00+00:00",
            "target": {"base_url": "https://example.test"},
            "summary": {"contract_ok": True, "search_zero_hit_count": 0, "core_search_count": 1},
            "deployment_evidence": {"server_version": "0.1+git.abcdef0", "git_commit": "abcdef0"},
            "endpoints": {},
            "mcp": {"initialize": scenario, "tools_list": scenario},
            "searches": [],
            "pagination": {
                "query": "q",
                "page_zero": {"current_offset": 0, "next_offset": 20},
                "page_twenty": {"response_contract": {"current_offset": 20}},
                "page_fingerprints_distinct": True,
                "contract_ok": True,
            },
            "response_bootstrap_metrics": [],
            "limitations": [],
        }
        markdown = MODULE.render_markdown(report)
        self.assertIn("## Endpoint latency", markdown)
        self.assertIn("## Search latency and coverage", markdown)
        self.assertIn("Cold claim: `false`", markdown)
        self.assertNotIn("raw_body", markdown)


if __name__ == "__main__":
    unittest.main()
