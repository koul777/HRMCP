from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "measure_vercel_fresh_instances.py"
)
SPEC = importlib.util.spec_from_file_location("measure_vercel_fresh_instances", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VercelFreshInstanceMeasurementTests(unittest.TestCase):
    def test_default_deploy_root_prefers_the_linked_repo_root(self) -> None:
        args = MODULE.parse_args(
            ["--out", "unused.json", "--markdown-out", "unused.md"]
        )
        self.assertEqual(
            Path(args.deploy_root).resolve(), MODULE.DEFAULT_DEPLOY_ROOT.resolve()
        )
        self.assertTrue((MODULE.DEFAULT_DEPLOY_ROOT / ".vercel" / "project.json").is_file())
        self.assertTrue((MODULE.DEFAULT_DEPLOY_ROOT / "api" / "ncs_ontology_compact.zip").is_file())

    def test_redaction_is_allowlist_based(self) -> None:
        payload = {
            "status": "ready",
            "name": "ncs-mcp",
            "token": "must-not-survive",
            "bootstrap": {
                "schema": "ncs_vercel_bootstrap_metrics_v2",
                "status": "ready",
                "source": "local_snapshot",
                "ready": True,
                "elapsed_ms": 4321.5,
                "stages_ms": {"extract_stream_write_sha256": 3000.0},
                "required_tables": ["secretly-large-list"],
                "minimum_rows": {"ksa_items": 1},
                "local_snapshot": {
                    "sqlite_bytes": 425758720,
                    "runtime_path": "/tmp/ncs.db",
                    "sqlite_sha256": "do-not-store",
                },
            },
            "runtime": {
                "database": {
                    "ready": True,
                    "public_tools_ready": True,
                    "path": "/tmp/ncs.db",
                    "readiness_count_source": "verified_snapshot_metadata",
                },
                "process": {"rss_bytes": 123456},
                "authorization": "must-not-survive",
            },
        }
        redacted = MODULE.redact_ready_payload(payload)
        rendered = repr(redacted)
        self.assertNotIn("must-not-survive", rendered)
        self.assertNotIn("/tmp/ncs.db", rendered)
        self.assertNotIn("do-not-store", rendered)
        self.assertNotIn("required_tables", rendered)
        self.assertEqual(redacted["bootstrap"]["elapsed_ms"], 4321.5)
        self.assertEqual(
            redacted["bootstrap"]["stages_ms"]["extract_stream_write_sha256"],
            3000.0,
        )
        self.assertEqual(
            redacted["runtime"]["rss_metrics"][0]["metric_path"],
            "runtime.process.rss_bytes",
        )

    def test_latency_summary_has_p50_p95_and_cv(self) -> None:
        summary = MODULE.latency_summary([100.0, 200.0, 300.0])
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["p50_ms"], 200.0)
        self.assertEqual(summary["p95_ms"], 290.0)
        self.assertAlmostEqual(summary["coefficient_of_variation"], 0.408248)

    def test_inspect_payload_is_reduced_to_deployment_evidence(self) -> None:
        safe = MODULE.sanitize_inspect_payload(
            {
                "id": "dpl_example",
                "readyState": "READY",
                "target": None,
                "createdAt": 123,
                "env": {"SECRET": "must-not-survive"},
            }
        )
        self.assertEqual(
            safe,
            {
                "deployment_id": "dpl_example",
                "ready_state": "READY",
                "created_at": 123,
            },
        )

    def test_report_schema_and_cold_claim_are_bounded(self) -> None:
        deployment = {
            "sequence": 1,
            "deployment": {"deployment_id": "dpl_example", "ready_state": "READY"},
            "first_request": {
                "status": 200,
                "elapsed_ms": 5000.0,
                "safe_payload": {
                    "bootstrap": {
                        "elapsed_ms": 4500.0,
                        "stages_ms": {"extract": 3000.0, "validate": 500.0},
                    }
                },
            },
            "warm_ready": {
                "contract_status_ok": True,
                "samples": [{"status": 200, "elapsed_ms": 250.0}],
            },
            "health": {"contract_status_ok": True},
            "mcp_get": {"contract_status_ok": True},
            "first_vs_warm_ready_p50_delta_ms": 4750.0,
        }
        report = MODULE.build_report(
            deployments=[deployment],
            source_commit="d05274d",
            deploy_root="deploy/vercel_mcp_app",
            bundle_bytes=126189734,
            delay_seconds=0.5,
            timeout_seconds=35.0,
        )
        self.assertEqual(report["schema"], MODULE.SCHEMA)
        self.assertEqual(
            report["measurement_contract"]["cold_claim"],
            "fresh_deployment_first_request",
        )
        self.assertFalse(
            report["measurement_contract"]["cold_claim_is_platform_cold_proof"]
        )
        self.assertTrue(report["summary"]["contract_ok"])
        self.assertEqual(
            report["summary"]["bootstrap"]["dominant_stage"]["name"], "extract"
        )
        self.assertEqual(report["summary"]["observed_max_duration_margin_ms"], 25000.0)

    def test_deployment_count_is_capped(self) -> None:
        with self.assertRaises(SystemExit):
            MODULE.main(
                [
                    "--deployments",
                    "4",
                    "--out",
                    "unused.json",
                    "--markdown-out",
                    "unused.md",
                ]
            )


if __name__ == "__main__":
    unittest.main()
