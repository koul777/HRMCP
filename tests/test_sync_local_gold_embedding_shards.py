from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ncs_mcp.data_builder import BuilderError  # noqa: E402
from scripts import sync_local_gold_embedding_shards as command  # noqa: E402


class LocalGoldEmbeddingShardSyncCommandTests(unittest.TestCase):
    def test_default_is_dry_run_and_does_not_read_container_credentials(self):
        engine = Mock()
        engine.sync_gold_embedding_shards.return_value = {
            "status": "validated_dry_run",
            "neo4j_writes": False,
        }
        output = StringIO()
        with patch.object(command, "DataBuilder", return_value=engine), patch.object(
            command, "inspect_local_gold_container"
        ) as inspect_container, redirect_stdout(output):
            code = command.main(["--version", "20260909_122536_062bfe50"])

        self.assertEqual(code, 0)
        inspect_container.assert_not_called()
        self.assertFalse(
            engine.sync_gold_embedding_shards.call_args.kwargs["apply"]
        )
        self.assertFalse(json.loads(output.getvalue())["neo4j_writes"])

    def test_apply_uses_memory_only_overlay_and_explicit_settings(self):
        engine = Mock()
        engine.sync_gold_embedding_shards.return_value = {
            "status": "applied_full_resumable",
            "neo4j_writes": True,
        }
        container = object()
        overlay = {
            "NCS_MCP_GOLD_ENABLED": "true",
            "NCS_MCP_GOLD_URI": "neo4j://127.0.0.1:7687",
            "NCS_MCP_GOLD_USERNAME": "neo4j",
            "NCS_MCP_GOLD_PASSWORD": "secret-not-for-output",
            "NCS_MCP_GOLD_DATABASE": "neo4j",
            "NCS_MCP_GOLD_CONNECT_TIMEOUT_SECONDS": "5",
            "NCS_MCP_GOLD_QUERY_TIMEOUT_SECONDS": "30",
        }
        output = StringIO()
        with patch.object(command, "DataBuilder", return_value=engine), patch.object(
            command, "inspect_local_gold_container", return_value=container
        ), patch.object(
            command,
            "build_local_gold_child_environment",
            return_value=overlay,
        ) as build_overlay, redirect_stdout(output):
            code = command.main(
                ["--version", "20260909_122536_062bfe50", "--apply"]
            )

        self.assertEqual(code, 0)
        build_overlay.assert_called_once_with(container, base_environment={})
        kwargs = engine.sync_gold_embedding_shards.call_args.kwargs
        self.assertTrue(kwargs["apply"])
        self.assertEqual(kwargs["settings"].password, "secret-not-for-output")
        self.assertNotIn("secret-not-for-output", output.getvalue())

    def test_builder_error_is_value_free(self):
        engine = Mock()
        engine.sync_gold_embedding_shards.side_effect = BuilderError(
            "backend accidentally included secret-not-for-output"
        )
        errors = StringIO()
        with patch.object(command, "DataBuilder", return_value=engine), redirect_stderr(
            errors
        ):
            code = command.main(["--version", "20260909_122536_062bfe50"])

        self.assertEqual(code, 1)
        self.assertNotIn("secret-not-for-output", errors.getvalue())
        self.assertEqual(
            json.loads(errors.getvalue())["error_code"],
            "builder_embedding_shard_sync_failed",
        )


if __name__ == "__main__":
    unittest.main()
