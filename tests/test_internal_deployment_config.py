from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL_FENCE_LANGUAGES = {
    "",
    "bash",
    "bat",
    "cmd",
    "console",
    "powershell",
    "pwsh",
    "sh",
    "shell",
}


def _markdown_shell_commands(path: Path) -> list[str]:
    """Return logical commands only from executable-looking Markdown fences."""

    blocks: list[list[str]] = []
    current: list[str] | None = None
    fence_character = ""
    fence_length = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if current is None:
            match = re.match(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)", line)
            if not match:
                continue
            marker, language = match.groups()
            if language.casefold() not in SHELL_FENCE_LANGUAGES:
                current = []
            else:
                current = []
                blocks.append(current)
            fence_character = marker[0]
            fence_length = len(marker)
            continue

        if re.match(rf"^\s*{re.escape(fence_character)}{{{fence_length},}}\s*$", line):
            current = None
            fence_character = ""
            fence_length = 0
            continue
        if blocks and current is blocks[-1]:
            current.append(line)

    commands: list[str] = []
    for block in blocks:
        pending = ""
        for raw_line in block:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("PS> ", "> ")):
                line = line.split(" ", 1)[1]
            continued = line.endswith(("`", "\\", "^"))
            if continued:
                line = line[:-1].rstrip()
            pending = f"{pending} {line}".strip()
            if not continued:
                commands.append(pending)
                pending = ""
        if pending:
            commands.append(pending)
    return commands


def _workflow_execution_surfaces(text: str) -> list[str]:
    """Extract YAML run/uses values without matching comments or descriptions."""

    lines = text.splitlines()
    surfaces: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(\s*)(?:-\s+)?(run|uses):\s*(.*)$", line)
        if not match:
            index += 1
            continue
        indent, _key, value = match.groups()
        if value not in {"|", ">", "|-", ">-"}:
            surfaces.append(value.strip())
            index += 1
            continue
        block: list[str] = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= len(
                indent
            ):
                break
            block.append(candidate.strip())
            index += 1
        surfaces.append("\n".join(block))
    return surfaces


def _contains_json_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_json_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_json_key(item, key) for item in value)
    return False


class InternalDeploymentConfigTests(unittest.TestCase):
    def test_institutional_chat_compose_uses_secret_files_and_private_port(
        self,
    ) -> None:
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

        env_text = (ROOT / "deploy" / "institutional-chat.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("NCS_CHAT_GATEWAY_SECRET_FILE=", env_text)
        self.assertIn("NCS_CHAT_AUDIT_HASH_SALT_FILE=", env_text)
        self.assertNotIn("NCS_CHAT_GATEWAY_SECRET=", env_text)
        self.assertNotIn("NCS_CHAT_AUDIT_HASH_SALT=", env_text)

    def test_compose_keeps_service_private_and_read_only(self) -> None:
        text = (ROOT / "deploy" / "compose.internal.yml").read_text(encoding="utf-8")

        required_markers = (
            "read_only: true",
            "- ALL",
            "- no-new-privileges:true",
            'NCS_MCP_READ_ONLY: "1"',
            'NCS_MCP_ENABLE_OPERATOR_TOOLS: "0"',
            'NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS: "2"',
            'NCS_MCP_ALLOW_REMOTE_BIND: "1"',
            "target: /data/ncs.db",
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
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

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

    def test_builder_is_the_only_data_refresh_and_deployment_authority(self) -> None:
        authority_docs = {
            "README.md": (
                "run_ncs_builder.bat",
                "production lifecycle의 유일한 운영 진입점",
                "자격/NCS006",
            ),
            "ARCHITECTURE.md": (
                "Production Lifecycle Authority",
                "single production lifecycle authority",
                "operator-guarded qualification / NCS006 exception",
            ),
            "docs/README_VERCEL_HTTPS.md": (
                "run_ncs_builder.bat",
                "유일한 운영 진입점",
                "자격/NCS006",
            ),
            "docs/VERCEL_SNAPSHOT_BUILDER.md": (
                "Builder-only refresh and Vercel release shape",
                "Windows Data Builder is the single owner",
                "run_ncs_builder.bat",
                "GitHub Actions remains available for CI tests only",
                "not a second data refresh or deployment authority",
            ),
        }
        for name, markers in authority_docs.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            for marker in markers:
                with self.subTest(name=name, marker=marker):
                    self.assertIn(marker, text)

        data_builder = (ROOT / "src" / "ncs_mcp" / "data_builder.py").read_text(
            encoding="utf-8"
        )
        for marker in ("def refresh_api", "def package", "def deploy"):
            with self.subTest(marker=marker):
                self.assertIn(marker, data_builder)

    def test_workflows_are_ci_only_without_schedule_or_release_commands(self) -> None:
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(workflows)
        forbidden_job_name = re.compile(
            r"(?:^|[-_\s])(deploy|deployment|publish|promotion|refresh|release)(?:$|[-_\s])",
            re.IGNORECASE,
        )
        forbidden_execution = (
            re.compile(
                r"\bvercel(?:\.cmd)?\s+(?:build|deploy|promote|rollback)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:publish_vercel_snapshot|promote_ncs_refresh_baseline|"
                r"refresh_ncs_(?:api_evidence|ontology))\.py\b",
                re.IGNORECASE,
            ),
            re.compile(r"\brun_ncs_builder\.py\b", re.IGNORECASE),
            re.compile(
                r"\bncs_harness\.py\s+(?:collect-|retry-qualification-errors|"
                r"preprocess-ncs-ontology)",
                re.IGNORECASE,
            ),
            re.compile(r"\b(?:vercel-action|vercel/actions)\b", re.IGNORECASE),
        )

        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.name, check="schedule"):
                self.assertIsNone(
                    re.search(r"^\s*schedule\s*:\s*(?:#.*)?$", text, re.MULTILINE)
                )

            in_jobs = False
            for line in text.splitlines():
                if re.match(r"^jobs:\s*(?:#.*)?$", line):
                    in_jobs = True
                    continue
                if in_jobs and line and not line[0].isspace():
                    in_jobs = False
                if not in_jobs:
                    continue
                job_match = re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
                name_match = re.match(r"^    name:\s*[\"']?(.+?)[\"']?\s*$", line)
                label = job_match or name_match
                if label:
                    value = label.group(1).strip().strip("\"'")
                    with self.subTest(workflow=workflow.name, job=value):
                        self.assertIsNone(forbidden_job_name.search(value))

            for surface in _workflow_execution_surfaces(text):
                for pattern in forbidden_execution:
                    with self.subTest(
                        workflow=workflow.name,
                        execution_surface=surface[:120],
                        pattern=pattern.pattern,
                    ):
                        self.assertIsNone(pattern.search(surface))

    def test_all_source_vercel_configs_disable_git_deploy_and_crons(self) -> None:
        ignored_parts = {
            ".git",
            ".runner-host",
            ".venv",
            ".vercel",
            "build",
            "node_modules",
            "reports",
            "tmp",
        }
        configs = sorted(
            path
            for path in ROOT.rglob("vercel.json")
            if not ignored_parts.intersection(path.relative_to(ROOT).parts)
        )
        self.assertTrue(
            {"vercel.json", "deploy/vercel_mcp_app/vercel.json"}.issubset(
                {path.relative_to(ROOT).as_posix() for path in configs}
            )
        )
        for config in configs:
            payload = json.loads(config.read_text(encoding="utf-8"))
            with self.subTest(config=config, check="crons"):
                self.assertFalse(_contains_json_key(payload, "crons"))
            with self.subTest(config=config, check="git deployment"):
                self.assertIs(payload.get("git", {}).get("deploymentEnabled"), False)

    def test_docs_have_no_executable_production_or_mutation_bypass(self) -> None:
        ignored_parts = {
            ".git",
            ".runner-host",
            ".venv",
            "build",
            "data",
            "node_modules",
            "promo",
            "reports",
            "tmp",
        }
        docs = sorted(
            path
            for path in ROOT.rglob("*.md")
            if not ignored_parts.intersection(path.relative_to(ROOT).parts)
        )
        direct_release = re.compile(
            r"\bvercel(?:\.cmd)?\s+(?:build|deploy|promote|rollback)\b",
            re.IGNORECASE,
        )
        mutating_bypass = (
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]run_ncs_builder\.py\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]"
                r"refresh_ncs_(?:api_evidence|ontology)\.py\b.*\s--apply\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]"
                r"(?:publish_vercel_snapshot|promote_ncs_refresh_baseline)\.py\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]build_vercel_snapshot\.py\b"
                r"(?!.*\s--dry-run\b)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]"
                r"package_vercel_compact_snapshot\.py\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpython(?:\.exe)?\s+scripts[\\/]"
                r"export_interview_serving_db\.py\b.*"
                r"--profile\s+vercel-ontology-compact\b",
                re.IGNORECASE,
            ),
        )

        for doc in docs:
            for command in _markdown_shell_commands(doc):
                relative = doc.relative_to(ROOT).as_posix()
                with self.subTest(doc=relative, command=command[:160]):
                    self.assertIsNone(direct_release.search(command))
                    for pattern in mutating_bypass:
                        self.assertIsNone(pattern.search(command))

    def test_deployment_guards_distinguish_execution_from_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            doc = Path(directory) / "guard.md"
            doc.write_text(
                "Internal Builder command: `vercel deploy --prebuilt --prod`.\n\n"
                "```text\nvercel deploy --prebuilt --prod\n```\n\n"
                "```powershell\nvercel deploy --prod\n```\n",
                encoding="utf-8",
            )
            self.assertEqual(_markdown_shell_commands(doc), ["vercel deploy --prod"])

        workflow = """name: example
description: vercel deploy --prod is forbidden
jobs:
  test:
    steps:
      - run: vercel deploy --prod
"""
        self.assertEqual(
            _workflow_execution_surfaces(workflow), ["vercel deploy --prod"]
        )
        self.assertRegex(
            _workflow_execution_surfaces(workflow)[0],
            r"\bvercel\s+deploy\b.*--prod\b",
        )
        self.assertTrue(_contains_json_key({"nested": {"crons": []}}, "crons"))

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
                    text.replace(
                        'set "NCS_MCP_READ_ONLY=1"', "set NCS_MCP_READ_ONLY=1"
                    ),
                )

        http_text = (ROOT / "run_ncs_mcp_http.cmd").read_text(encoding="utf-8")
        self.assertIn("NCS_MCP_ALLOW_REMOTE_BIND", http_text)
        self.assertIn("--allow-remote-bind", http_text)

        chat_text = (ROOT / "run_ncs_institutional_chat.cmd").read_text(
            encoding="utf-8"
        )
        self.assertIn("NCS_MCP_READ_ONLY=1", chat_text)
        self.assertIn("NCS_MCP_ENABLE_OPERATOR_TOOLS=0", chat_text)
        self.assertIn("NCS_CHAT_HOST=127.0.0.1", chat_text)
        self.assertIn("NCS_CHAT_ALLOW_REMOTE_BIND", chat_text)
        self.assertIn("ncs_mcp.institutional_chat", chat_text)

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
