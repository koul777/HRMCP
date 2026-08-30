from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from starlette.testclient import TestClient

from ncs_mcp.smoke_data import create_ready_smoke_db


class VercelBootstrapMetricsTests(unittest.TestCase):
    def test_direct_health_and_ready_import_is_bootstrap_side_effect_free(self) -> None:
        modules_to_clear = (
            "api.bootstrap_state",
            "api.bootstrap_runtime",
            "api.health",
            "api.ready",
            "api.mcp",
            "ncs_mcp.server",
        )
        for name in modules_to_clear:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()

        importlib.import_module("api.health")
        importlib.import_module("api.ready")

        self.assertNotIn("api.mcp", sys.modules)
        self.assertNotIn("ncs_mcp.server", sys.modules)
        state = importlib.import_module("api.bootstrap_state")
        self.assertEqual(state.get_bootstrap_metrics()["status"], "not_initialized")
        self.assertIsNone(state.get_bootstrap_metrics()["ready"])

    def test_bootstrap_state_is_immutable_and_thread_safe(self) -> None:
        state = importlib.import_module("api.bootstrap_state")
        original = {
            "schema": "ncs_vercel_bootstrap_metrics_v2",
            "ready": True,
            "required_tables": ["competency_units"],
            "minimum_rows": {"competency_units": 1},
        }
        recorded = state.record_bootstrap_metrics(original)
        original["required_tables"].append("ksa_items")
        recorded["minimum_rows"]["competency_units"] = 999
        snapshot = state.get_bootstrap_metrics()
        self.assertEqual(snapshot["required_tables"], ["competency_units"])
        self.assertEqual(snapshot["minimum_rows"], {"competency_units": 1})

        start = threading.Barrier(8)

        def record_and_read(worker: int) -> dict[str, object]:
            start.wait()
            state.record_bootstrap_metrics(
                {
                    "schema": "ncs_vercel_bootstrap_metrics_v2",
                    "ready": bool(worker % 2),
                    "worker": {"id": worker, "values": [worker]},
                }
            )
            result = state.get_bootstrap_metrics()
            result["worker"]["values"].append("external-mutation")
            return result

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(record_and_read, range(8)))

        self.assertEqual(len(results), 8)
        final = state.get_bootstrap_metrics()
        self.assertEqual(final["worker"]["values"], [final["worker"]["id"]])

    def test_health_and_ready_include_bootstrap_metrics(self) -> None:
        previous_db = os.environ.get("NCS_DB_PATH")
        previous_read_only = os.environ.get("NCS_MCP_READ_ONLY")
        previous_allow_override = os.environ.get("NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE")
        modules_to_clear = (
            "api.index",
            "api.ready",
            "api.health",
            "api.mcp",
            "api.bootstrap_runtime",
            "api.bootstrap_state",
            "ncs_mcp.server",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            os.environ["NCS_DB_PATH"] = str(db_path)
            os.environ["NCS_MCP_READ_ONLY"] = "1"
            os.environ["NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE"] = "1"
            try:
                for name in modules_to_clear:
                    sys.modules.pop(name, None)
                importlib.invalidate_caches()
                app = importlib.import_module("api.index").app
                client = TestClient(app)
                health = client.get("/api/health")
                ready = client.get("/api/ready")
                self.assertNotIn("api.mcp", sys.modules)
                self.assertNotIn("ncs_mcp.server", sys.modules)
            finally:
                for name in modules_to_clear:
                    sys.modules.pop(name, None)
                if previous_db is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous_db
                if previous_read_only is None:
                    os.environ.pop("NCS_MCP_READ_ONLY", None)
                else:
                    os.environ["NCS_MCP_READ_ONLY"] = previous_read_only
                if previous_allow_override is None:
                    os.environ.pop("NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE", None)
                else:
                    os.environ["NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE"] = previous_allow_override

        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        for payload in (health.json(), ready.json()):
            self.assertEqual(
                payload["bootstrap"]["schema"], "ncs_vercel_bootstrap_metrics_v2"
            )
            self.assertTrue(payload["bootstrap"]["ready"])
            self.assertEqual(payload["bootstrap"]["source"], "explicit_path_override")
            self.assertGreaterEqual(payload["bootstrap"]["elapsed_ms"], 0.0)
            self.assertIn("explicit_path_override_validate", payload["bootstrap"]["stages_ms"])

    def test_root_and_deploy_api_mirrors_are_identical(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "bootstrap_runtime.py",
            "bootstrap_state.py",
            "health.py",
            "mcp.py",
            "ready.py",
        ):
            self.assertEqual(
                (root / "api" / relative).read_bytes(),
                (root / "deploy" / "vercel_mcp_app" / "api" / relative).read_bytes(),
                relative,
            )
