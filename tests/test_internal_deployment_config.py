from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InternalDeploymentConfigTests(unittest.TestCase):
    def test_institutional_chat_compose_uses_secret_files_and_private_port(self) -> None:
        text = (ROOT / "deploy" / "compose.institutional-chat.yml").read_text(
            encoding="utf-8"
        )
        for marker in (
            "ncs_mcp.institutional_chat",
            "--allow-remote-bind",
            "--auth-mode",
            "gateway",
            "read_only: true",
            "- ALL",
            "- no-new-privileges:true",
            'NCS_MCP_READ_ONLY: "1"',
            'NCS_MCP_ENABLE_OPERATOR_TOOLS: "0"',
            "NCS_CHAT_GATEWAY_SECRET_FILE: /run/secrets/chat_gateway_secret",
            "NCS_CHAT_AUDIT_HASH_SALT_FILE: /run/secrets/chat_audit_hash_salt",
            "target: /data/ncs.db",
            "read_only: true",
            "target: /audit",
            '"127.0.0.1:${NCS_CHAT_HOST_PORT:-8780}:8780"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        self.assertNotIn("NCS_CHAT_GATEWAY_SECRET:", text)
        self.assertNotIn("NCS_CHAT_AUDIT_HASH_SALT:", text)

        env_text = (
            ROOT / "deploy" / "institutional-chat.env.example"
        ).read_text(encoding="utf-8")
        self.assertIn("NCS_CHAT_GATEWAY_SECRET_FILE=", env_text)
        self.assertIn("NCS_CHAT_AUDIT_HASH_SALT_FILE=", env_text)
        self.assertNotIn("NCS_CHAT_GATEWAY_SECRET=", env_text)
        self.assertNotIn("NCS_CHAT_AUDIT_HASH_SALT=", env_text)

    def test_compose_keeps_service_private_and_read_only(self) -> None:
        text = (ROOT / "deploy" / "compose.internal.yml").read_text(encoding="utf-8")

        required_markers = (
            'read_only: true',
            '- ALL',
            '- no-new-privileges:true',
            'NCS_MCP_READ_ONLY: "1"',
            'NCS_MCP_ENABLE_OPERATOR_TOOLS: "0"',
            'NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS: "2"',
            'NCS_MCP_ALLOW_REMOTE_BIND: "1"',
            'target: /data/ncs.db',
            '"127.0.0.1:${NCS_MCP_HOST_PORT:-8766}:8766"',
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        self.assertGreaterEqual(text.count("read_only: true"), 2)
        self.assertNotIn('"${NCS_MCP_HOST_PORT:-8766}:8766"', text)

    def test_docker_image_defaults_to_read_only_serving(self) -> None:
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ENV NCS_MCP_READ_ONLY=1", text)
        self.assertIn("ENV NCS_MCP_ENABLE_OPERATOR_TOOLS=0", text)
        self.assertIn("ENV NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2", text)
        self.assertIn("ENV NCS_MCP_HOST=127.0.0.1", text)
        self.assertIn("ENV NCS_MCP_ALLOW_REMOTE_BIND=0", text)
        self.assertIn("COPY pyproject.toml requirements.txt README.md ./", text)
        self.assertIn("USER app", text)
        self.assertIn("/ready", text)
        self.assertIn("python -m pip check", text)
        self.assertIn("/audit", text)
        self.assertIn('if [ \\"${NCS_MCP_ALLOW_REMOTE_BIND}\\" = \\"1\\" ]', text)
        self.assertNotIn('port "${NCS_MCP_PORT}" --allow-remote-bind', text)

    def test_docker_ci_uses_hardened_read_only_service_runtime(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        for marker in (
            "--read-only --cap-drop ALL",
            "--security-opt no-new-privileges --tmpfs /tmp",
            "127.0.0.1:8777:8777",
            "docker-smoke/ncs.db:/data/ncs.db:ro",
            "NCS_MCP_READ_ONLY=1",
            "NCS_MCP_ENABLE_OPERATOR_TOOLS=0",
            "NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2",
            "NCS_MCP_HOST=0.0.0.0",
            "NCS_MCP_ALLOW_REMOTE_BIND=1",
            "python -m pip check",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_vercel_snapshot_release_workflow_uses_single_db_builder_flow(self) -> None:
        text = (
            ROOT / ".github" / "workflows" / "vercel-snapshot-release.yml"
        ).read_text(encoding="utf-8")

        for marker in (
            "runs-on: [self-hosted, windows]",
            "NCS_SOURCE_DB_URL",
            "NCS_SOURCE_DB_ALLOWED_HOSTS",
            "scripts\\refresh_ncs_api_evidence.py",
            "scripts\\refresh_ncs_ontology.py",
            "scripts\\publish_vercel_snapshot.py",
            "--skip-domain",
            "vercel promote",
            "functions\\python.func",
            "scripts\\verify_remote_mcp_transport.py",
            "scripts\\promote_ncs_refresh_baseline.py",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        self.assertLess(
            text.index("scripts\\refresh_ncs_ontology.py"),
            text.index("scripts\\publish_vercel_snapshot.py"),
        )
        self.assertLess(
            text.index("Verify the exact staged MCP deployment"),
            text.index("Promote verified canonical baseline state"),
        )
        self.assertIn("production baseline was intentionally left unchanged", text)
        self.assertNotIn("github.event.inputs.source_db_url", text)
        self.assertIn("Remove generated release working copies\n        if: always()", text)

    def test_mcp_client_examples_use_hrmcp_name(self) -> None:
        stdio = json.loads((ROOT / "mcp" / "ncs-mcp.json").read_text(encoding="utf-8"))
        http = json.loads(
            (ROOT / "mcp" / "ncs-mcp-http.json").read_text(encoding="utf-8")
        )

        self.assertEqual(set(stdio["mcpServers"]), {"hrmcp"})
        self.assertEqual(set(http["mcpServers"]), {"hrmcp"})
        self.assertEqual(
            http["mcpServers"]["hrmcp"]["url"],
            "http://127.0.0.1:8766/mcp",
        )

    def test_windows_launchers_default_to_read_only_serving(self) -> None:
        for name in (
            "run_mcp_server.bat",
            "run_ncs_mcp_http.cmd",
            "run_ncs_mcp_stdio.cmd",
        ):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn(
                    'if "%NCS_MCP_READ_ONLY%"=="" set NCS_MCP_READ_ONLY=1',
                    text.replace('set "NCS_MCP_READ_ONLY=1"', "set NCS_MCP_READ_ONLY=1"),
                )

        http_text = (ROOT / "run_ncs_mcp_http.cmd").read_text(encoding="utf-8")
        self.assertIn("NCS_MCP_ALLOW_REMOTE_BIND", http_text)
        self.assertIn("--allow-remote-bind", http_text)

        chat_text = (ROOT / "run_ncs_institutional_chat.cmd").read_text(
            encoding="utf-8"
        )
        self.assertIn('NCS_MCP_READ_ONLY=1', chat_text)
        self.assertIn('NCS_MCP_ENABLE_OPERATOR_TOOLS=0', chat_text)
        self.assertIn('NCS_CHAT_HOST=127.0.0.1', chat_text)
        self.assertIn('NCS_CHAT_ALLOW_REMOTE_BIND', chat_text)
        self.assertIn('ncs_mcp.institutional_chat', chat_text)

    def test_docs_do_not_recommend_unsafe_container_serving(self) -> None:
        for name in (
            "docs/AIHR_DEPLOYMENT_RUNBOOK.md",
            "docs/MCP_EXPERIMENT_GUIDE.md",
        ):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn("-p 8766:8766", text)
                self.assertNotIn("data\\processed:/data", text)
                self.assertIn("compose.internal.yml", text)

        chat_guide = (ROOT / "docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("compose.institutional-chat.yml", chat_guide)
        self.assertIn("NCS_CHAT_GATEWAY_SECRET_FILE", chat_guide)
        self.assertIn("NCS_CHAT_AUDIT_HASH_SALT_FILE", chat_guide)


if __name__ == "__main__":
    unittest.main()
