from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from scripts import verify_remote_mcp_transport as verifier


class RemoteMcpTransportVerifierTests(unittest.TestCase):
    def test_protocol_version_is_configurable_and_used_for_post_requests(self) -> None:
        selected_protocol = "2025-06-18"
        observed_protocols: list[str | None] = []
        lock = threading.Lock()

        def fake_request(
            _url: str,
            *,
            method: str,
            body: bytes | None = None,
            accept: str = "application/json, text/event-stream",
            protocol_version: str | None = None,
            timeout: float = 35.0,
        ) -> dict[str, object]:
            del accept, timeout
            with lock:
                observed_protocols.append(protocol_version)
            if method == "GET":
                return {"status": 405, "allow": "POST", "duration_seconds": 0.01}
            if method == "PUT":
                return {"status": 405, "duration_seconds": 0.01}
            if body == b"{not-json":
                return {"status": 400, "duration_seconds": 0.01}

            payload = json.loads(body or b"{}")
            rpc_method = payload.get("method")
            if protocol_version == "2099-01-01":
                return {"status": 400, "duration_seconds": 0.01}
            if rpc_method == "initialize":
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {
                        "result": {"protocolVersion": selected_protocol},
                    },
                }
            if rpc_method == "notifications/initialized":
                return {"status": 202, "duration_seconds": 0.01}
            if rpc_method == "tools/list":
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {
                        "result": {
                            "tools": [
                                {"name": "ncs_search"},
                                {"name": "ncs_analysis"},
                                {"name": "ncs_discover_tools"},
                            ]
                        }
                    },
                }
            if rpc_method == "tools/call":
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {"result": {"isError": False}},
                }
            raise AssertionError((method, payload, protocol_version))

        with patch.object(verifier, "_request", side_effect=fake_request):
            report = verifier.verify(
                "https://example.test/api/mcp",
                concurrency=3,
                protocol_version=selected_protocol,
            )

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["protocol_version"], selected_protocol)
        self.assertGreaterEqual(observed_protocols.count(selected_protocol), 5)
        self.assertIn("2099-01-01", observed_protocols)


if __name__ == "__main__":
    unittest.main()
