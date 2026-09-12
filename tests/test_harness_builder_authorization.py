from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ncs_harness as harness
from ncs_mcp.builder_authorization import (
    BuilderAuthorizationError,
    _exclusive_operation,
    current_builder_context,
    qualification_operator_lease,
    require_builder_context,
)


class HarnessBuilderAuthorizationTests(unittest.TestCase):
    def parse(self, *argv):
        with patch.object(sys, "argv", ["ncs_harness.py", *argv]):
            return harness.parse_args()

    def assert_blocked(self, args):
        out = io.StringIO()
        with (
            patch.object(harness, "parse_args", return_value=args),
            patch.object(harness, "load_settings") as settings,
            patch.object(harness, "connect") as connection,
            patch.object(harness, "initialize_database") as initialize,
            patch.object(harness, "_dispatch_harness_command") as dispatch,
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as exc,
        ):
            harness.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "builder_authorization_required")
        settings.assert_not_called()
        connection.assert_not_called()
        initialize.assert_not_called()
        dispatch.assert_not_called()

    def test_inventory_covers_every_dispatch_and_parser_command(self):
        tree = ast.parse((ROOT / "scripts/ncs_harness.py").read_text(encoding="utf-8-sig"))
        dispatch = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_dispatch_harness_command")
        commands = {
            node.comparators[0].value
            for node in ast.walk(dispatch)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "args.command"
        }
        parsers = {
            node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser" and node.args
        }
        self.assertEqual(set(harness.HARNESS_COMMAND_POLICIES), commands)
        self.assertEqual(commands, parsers)

    def test_all_builder_required_commands_block_before_dispatch(self):
        for command, policy in harness.HARNESS_COMMAND_POLICIES.items():
            if policy == "builder_required":
                with self.subTest(command=command):
                    self.assert_blocked(argparse.Namespace(command=command))
        self.assert_blocked(argparse.Namespace(command="future-unclassified-command"))

    def test_conditional_mutations_block_before_dispatch(self):
        for command, policy in harness.HARNESS_COMMAND_POLICIES.items():
            if policy not in {"apply", "save", "non_dry_run"}:
                continue
            flag = "dry_run" if policy == "non_dry_run" else policy
            value = False if policy == "non_dry_run" else True
            with self.subTest(command=command):
                self.assert_blocked(argparse.Namespace(command=command, **{flag: value}))
                safe = argparse.Namespace(command=command, **{flag: not value})
                self.assertFalse(harness.harness_requires_builder(safe))

    def test_every_pipeline_mutation_stage_blocks(self):
        for stage in harness.HARNESS_PIPELINE_MUTATION_FLAGS:
            with self.subTest(stage=stage):
                self.assert_blocked(self.parse("pipeline", "--" + stage.replace("_", "-")))
        for flag in ("--smoke", "--lint", "--validate-ontology", "--export-ontology-jsonld"):
            self.assertFalse(harness.harness_requires_builder(self.parse("pipeline", flag)))

    def test_legacy_dry_run_does_not_bypass_schema_mutation(self):
        for argv in (("mvp-bootstrap", "--dry-run"), ("refine", "stats"),
                     ("refine", "export-jsonl"), ("refine", "import-jsonl", "--dry-run"),
                     ("refine", "apply", "--dry-run"), ("build-sqf-sqlite-model", "--summary")):
            self.assert_blocked(self.parse(*argv))

    def test_environment_boolean_does_not_authorize(self):
        with patch.dict(os.environ, {"NCS_BUILDER_AUTHORIZED": "true", "BUILDER_AUTHORIZED": "1"}):
            self.assert_blocked(self.parse("collect-training-courses", "--all-majors"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("collect-training-courses", "--all-majors", "--builder-authorized")

    def test_report_and_plan_commands_still_dispatch(self):
        for argv in (("plan-elements",), ("qualification-retry-hygiene",),
                     ("qualification-coverage-plan",), ("build-duplicate-concept-relations", "--dry-run"),
                     ("prepare-ontology-review-queue", "--dry-run")):
            args = self.parse(*argv)
            with patch.object(harness, "parse_args", return_value=args), patch.object(harness, "_dispatch_harness_command") as dispatch:
                harness.main()
            dispatch.assert_called_once_with(args)

    def test_report_does_not_initialize_or_create_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "missing.db"
            for command in ("qualification-retry-hygiene", "plan-elements"):
                args = self.parse(command)
                with (
                    self.subTest(command=command),
                    patch.object(harness, "parse_args", return_value=args),
                    patch.object(harness, "load_settings", return_value=argparse.Namespace(db_path=database)),
                    patch.object(harness, "initialize_database") as initialize,
                    self.assertRaises(FileNotFoundError),
                ):
                    harness.main()
                initialize.assert_not_called()
                self.assertFalse(database.exists())


class QualificationOperatorLeaseTests(unittest.TestCase):
    gate = {"status": "allowed", "qualification_retry_allowed_now": True,
            "api_call_allowed_now": True, "safety_violations": []}

    def args(self, command="collect-qualification-items", **overrides):
        values = dict(command=command, ncs006_checkpoint_path=None, limit_units=10,
                      num_of_rows=50, max_pages=1, page_no=1, timeout=30,
                      max_retries=1, stop_after_rate_limit_errors=2,
                      request_delay=3.0, retry_backoff_seconds=120.0,
                      refresh=False, include_not_due=False, unit_code=[],
                      all_units=True, major_code=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_unsafe_bounds_block_before_lock_settings_or_dispatch(self):
        bad = {"limit_units": [None, 0, 101], "num_of_rows": [0, 51],
               "max_pages": [None, 0, 2], "page_no": [0, 2], "timeout": [0, 121],
               "max_retries": [-1, 2], "stop_after_rate_limit_errors": [0, 4],
               "request_delay": [0, 1, float("nan"), float("inf")],
               "retry_backoff_seconds": [0, 29], "refresh": [True], "include_not_due": [True]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for key, values in bad.items():
                for value in values:
                    with (
                        self.subTest(key=key, value=value),
                        patch.object(harness, "ROOT", root),
                        patch.object(harness, "parse_args", return_value=self.args(**{key: value})),
                        patch.object(harness, "build_ncs006_guarded_api_gate", return_value=self.gate),
                        patch.object(harness, "load_settings") as settings,
                        patch.object(harness, "_dispatch_harness_command") as dispatch,
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        harness.main()
                    settings.assert_not_called()
                    dispatch.assert_not_called()
                    self.assertFalse((root / ".state").exists())

    def test_blocked_gate_never_acquires_lease(self):
        for gate in ({}, {**self.gate, "safety_violations": ["cooldown"]},
                     {**self.gate, "qualification_retry_allowed_now": False}):
            with (
                tempfile.TemporaryDirectory() as tmp,
                patch.object(harness, "ROOT", Path(tmp)),
                patch.object(harness, "parse_args", return_value=self.args()),
                patch.object(harness, "build_ncs006_guarded_api_gate", return_value=gate),
                patch.object(harness, "_dispatch_harness_command") as dispatch,
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit),
            ):
                harness.main()
            dispatch.assert_not_called()

    def test_lease_has_no_builder_authority_and_blocks_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".state/ncs-data-builder"
            with qualification_operator_lease(root=root, command="collect-qualification-items") as lineage:
                self.assertEqual(lineage["owner"], "qualification_operator")
                self.assertNotIn("version", lineage)
                self.assertIsNone(current_builder_context())
                with self.assertRaises(BuilderAuthorizationError):
                    require_builder_context(lineage, action="build_delta")
                with self.assertRaises(BuilderAuthorizationError):
                    with _exclusive_operation(root=root, state_dir=state, action="build_delta", version=None):
                        self.fail("concurrent Builder operation entered")
            self.assertFalse((state / "operation.lock").exists())
            self.assertFalse((state / "versions").exists())

    def test_builder_and_legacy_locks_block_operator_without_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".state/ncs-data-builder"
            state.mkdir(parents=True)
            with _exclusive_operation(root=root, state_dir=state, action="build_delta", version=None):
                with self.assertRaises(BuilderAuthorizationError):
                    with qualification_operator_lease(root=root, command="retry-qualification-errors"):
                        self.fail("concurrent operator entered")
            lock = state / "operation.lock"
            lock.write_text("1234", encoding="utf-8")
            with self.assertRaises(BuilderAuthorizationError):
                with qualification_operator_lease(root=root, command="retry-qualification-errors"):
                    self.fail("legacy lock bypassed")
            self.assertEqual(lock.read_text(), "1234")

    def test_dispatch_runs_with_lease_and_exception_releases_it(self):
        for command in ("collect-qualification-items", "retry-qualification-errors"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def fail_inside(args):
                    payload = json.loads((root / ".state/ncs-data-builder/operation.lock").read_text())
                    self.assertEqual(payload, args._qualification_operation_lineage)
                    self.assertEqual(payload["action"], command)
                    raise RuntimeError("mock collector failed")
                with (
                    patch.object(harness, "ROOT", root),
                    patch.object(harness, "parse_args", return_value=self.args(command)),
                    patch.object(harness, "build_ncs006_guarded_api_gate", return_value=self.gate),
                    patch.object(harness, "_dispatch_harness_command", side_effect=fail_inside),
                    self.assertRaisesRegex(RuntimeError, "mock collector failed"),
                ):
                    harness.main()
                self.assertFalse((root / ".state/ncs-data-builder/operation.lock").exists())

    def test_live_builder_lock_blocks_cli_before_settings_or_collector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".state/ncs-data-builder"
            state.mkdir(parents=True)
            with _exclusive_operation(root=root, state_dir=state, action="refresh_api", version=None):
                with (
                    patch.object(harness, "ROOT", root),
                    patch.object(harness, "parse_args", return_value=self.args()),
                    patch.object(harness, "build_ncs006_guarded_api_gate", return_value=self.gate),
                    patch.object(harness, "load_settings") as settings,
                    patch.object(harness, "_dispatch_harness_command") as dispatch,
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit),
                ):
                    harness.main()
                settings.assert_not_called()
                dispatch.assert_not_called()

    def test_ncs006_collection_hold_does_not_override_qualification_specific_gate(self):
        with patch.object(harness, "build_ncs006_guarded_api_gate", return_value={
            **self.gate, "api_call_allowed_now": False,
        }):
            result = harness._qualification_operator_preflight(self.args())
        self.assertTrue(result["qualification_retry_allowed_now"])
        self.assertFalse(result["api_call_allowed_now"])

    def test_lease_does_not_delete_a_replacement_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with qualification_operator_lease(root=root, command="collect-qualification-items"):
                lock = root / ".state/ncs-data-builder/operation.lock"
                lock.write_text('{"operation_id":"replacement"}', encoding="utf-8")
            self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
