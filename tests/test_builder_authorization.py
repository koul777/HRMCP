import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.builder_authorization import (
    BUILDER_OWNER,
    OPERATION_SCHEMA,
    BuilderAuthorizationError,
    _bind_operation_version,
    current_builder_context,
    require_builder_context,
)
from ncs_mcp.data_builder import BuilderError, DataBuilder


class BuilderAuthorizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.builder = DataBuilder(self.root)
        self.lock = self.builder.state / "operation.lock"

    def write_lock(self, payload):
        self.lock.write_text(json.dumps(payload), encoding="utf-8")

    def test_live_context_matches_full_structured_lock_and_scope(self):
        with self.builder.exclusive("prepare_gold", "abc") as context:
            payload = json.loads(self.lock.read_text(encoding="utf-8"))
            self.assertEqual(payload, context.lineage())
            self.assertEqual(payload["schema"], OPERATION_SCHEMA)
            self.assertEqual(payload["owner"], BUILDER_OWNER)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertNotIn(context._nonce, self.lock.read_text(encoding="utf-8"))
            self.assertNotIn(context._nonce, repr(context))
            self.assertIs(current_builder_context(), context)
            self.assertIs(
                require_builder_context(
                    context, action=("prepare_gold", "unused"), root=self.root,
                    state_dir=self.builder.state, version="abc",
                    version_dir=self.builder.state / "versions/abc",
                ), context,
            )
        self.assertFalse(self.lock.exists())
        self.assertIsNone(current_builder_context())
        with self.assertRaises(BuilderAuthorizationError):
            require_builder_context(context, action="prepare_gold")

    def test_flags_dicts_cloned_capabilities_and_ambient_context_are_rejected(self):
        with self.builder.exclusive("refresh_api") as context:
            with patch.dict(os.environ, {"NCS_BUILDER_AUTHORIZED": "true"}):
                for invalid in (
                    None, True, context.lineage(), copy.copy(context),
                    copy.deepcopy(context), replace(context),
                ):
                    with self.subTest(value=type(invalid).__name__):
                        with self.assertRaises(BuilderAuthorizationError):
                            require_builder_context(invalid, action="refresh_api")

    def test_wrong_action_root_state_version_and_version_folder_rejected(self):
        with self.builder.exclusive("prepare_gold", "abc") as context:
            for scope in (
                {"action": "deploy"}, {"root": self.root / "other"},
                {"state_dir": self.root / "other"}, {"version": "def"},
                {"version_dir": self.builder.state / "versions/def"},
                {"version_dir": self.builder.state / "versions/abc/child"},
            ):
                with self.subTest(scope=scope):
                    kwargs = {"action": "prepare_gold", **scope}
                    with self.assertRaises(BuilderAuthorizationError):
                        require_builder_context(context, **kwargs)

    def test_every_lock_identity_field_is_checked(self):
        with self.builder.exclusive("prepare_gold", "abc") as context:
            original = context.lineage()
            for key in original:
                with self.subTest(field=key):
                    self.write_lock({**original, key: "tampered"})
                    with self.assertRaises(BuilderAuthorizationError):
                        require_builder_context(context, action="prepare_gold")
                    self.write_lock(original)

    def test_invalid_or_missing_lock_revokes_validation(self):
        with self.builder.exclusive("refresh_api") as context:
            for content in ("1234", "[]", "{}", "invalid", ""):
                self.lock.write_text(content, encoding="utf-8")
                with self.assertRaises(BuilderAuthorizationError):
                    require_builder_context(context, action="refresh_api")
            self.lock.unlink()
            with self.assertRaises(BuilderAuthorizationError):
                require_builder_context(context, action="refresh_api")

    def test_nonce_and_process_identity_cannot_be_substituted(self):
        with self.builder.exclusive("refresh_api") as context:
            original_nonce = context._nonce
            object.__setattr__(context, "_nonce", "wrong")
            with self.assertRaises(BuilderAuthorizationError):
                require_builder_context(context, action="refresh_api")
            object.__setattr__(context, "_nonce", original_nonce)
            with patch("ncs_mcp.builder_authorization.os.getpid", return_value=-1):
                with self.assertRaises(BuilderAuthorizationError):
                    require_builder_context(context, action="refresh_api")

    def test_nested_and_concurrent_operations_cannot_issue_second_lease(self):
        second = DataBuilder(self.root)
        failures = []

        def attempt():
            try:
                with second.exclusive("deploy"):
                    failures.append("unexpected lease")
            except BuilderError:
                failures.append("blocked")

        with self.builder.exclusive("refresh_api") as context:
            with self.assertRaises(BuilderError):
                with self.builder.exclusive("copy_current"):
                    pass
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, ["blocked"])
            self.assertEqual(json.loads(self.lock.read_text()), context.lineage())

    def test_legacy_or_unowned_lock_blocks_issuance_and_is_preserved(self):
        self.lock.write_text("1234", encoding="utf-8")
        with self.assertRaises(BuilderError):
            with self.builder.exclusive("copy_current"):
                pass
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "1234")

    def test_exception_and_keyboard_interrupt_release_lease(self):
        for exception in (ValueError("fixture"), KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with self.builder.exclusive("copy_current") as context:
                        raise exception
                self.assertFalse(self.lock.exists())
                self.assertIsNone(current_builder_context())
                with self.assertRaises(BuilderAuthorizationError):
                    require_builder_context(context, action="copy_current")

    def test_replacement_lock_is_not_removed(self):
        with self.builder.exclusive("copy_current") as context:
            replacement = {**context.lineage(), "operation_id": "other-operation"}
            self.write_lock(replacement)
        self.assertEqual(json.loads(self.lock.read_text()), replacement)

    def test_recreated_lock_cannot_reactivate_expired_capability(self):
        with self.builder.exclusive("prepare_gold", "abc") as context:
            recorded_lock = context.lineage()
        self.write_lock(recorded_lock)
        with self.assertRaises(BuilderAuthorizationError):
            require_builder_context(context, action="prepare_gold", version="abc")
        with self.assertRaises(BuilderError):
            with self.builder.exclusive("prepare_gold", "abc"):
                pass
        self.assertEqual(json.loads(self.lock.read_text()), recorded_lock)

    def test_new_build_binds_version_and_persists_lineage_without_approval(self):
        with self.builder.exclusive("build_delta") as context:
            folder, report = self.builder._new("excel-delta", context)
            self.assertEqual(context.version, report["version"])
            self.assertEqual(report["operation_lineage"], context.lineage())
            self.assertFalse(report["human_approval_claim"])
            self.assertEqual(json.loads((folder / "build.json").read_text()), report)
            require_builder_context(
                context, action="build_delta", version=report["version"], version_dir=folder
            )
            _bind_operation_version(context, report["version"])
            with self.assertRaises(BuilderAuthorizationError):
                _bind_operation_version(context, "def")

    def test_version_binding_checks_lock_before_modifying_it(self):
        with self.builder.exclusive("build_delta") as context:
            replacement = {**context.lineage(), "operation_id": "other"}
            self.write_lock(replacement)
            with self.assertRaises(BuilderAuthorizationError):
                _bind_operation_version(context, "abc")
            self.assertIsNone(context.version)
            self.assertEqual(json.loads(self.lock.read_text()), replacement)

    def test_identical_payload_replacement_inode_revokes_context(self):
        with self.builder.exclusive("build_delta") as context:
            replacement = self.lock.with_name("replacement.lock")
            replacement.write_text(json.dumps(context.lineage()), encoding="utf-8")
            os.replace(replacement, self.lock)
            with self.assertRaises(BuilderAuthorizationError):
                require_builder_context(context, action="build_delta")
        self.assertTrue(self.lock.exists())

    def test_handoff_race_never_deletes_replacement(self):
        original_rename = os.rename
        replacement = {"operation_id": "replacement", "nonce_sha256": "replacement"}
        with self.builder.exclusive("build_delta"):
            def swap_before_rename(source, destination):
                other = self.lock.with_name("replacement.lock")
                other.write_text(json.dumps(replacement), encoding="utf-8")
                os.replace(other, self.lock)
                original_rename(source, destination)
            with patch("ncs_mcp.builder_authorization.os.rename", side_effect=swap_before_rename):
                # Trigger cleanup while the replacement arrives after the read.
                from ncs_mcp.builder_authorization import _release_owned_lock, _lock_identities
                context = current_builder_context()
                _release_owned_lock(self.lock, context.lineage(), _lock_identities[context.state_dir])
        self.assertEqual(json.loads(self.lock.read_text()), replacement)
        self.assertTrue(list(self.builder.state.glob("operation-handoff-*.lock")))

    def test_binding_handoff_cannot_overwrite_new_lock(self):
        original_link = os.link
        replacement = {"operation_id": "new-owner"}
        with self.builder.exclusive("build_delta") as context:
            def racing_link(source, destination, *args, **kwargs):
                self.write_lock(replacement)
                return original_link(source, destination, *args, **kwargs)
            with patch("ncs_mcp.builder_authorization.os.link", side_effect=racing_link):
                with self.assertRaises(BuilderAuthorizationError):
                    _bind_operation_version(context, "abc")
            self.assertIsNone(context.version)
        self.assertEqual(json.loads(self.lock.read_text()), replacement)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_windows_junction_state_and_versions_fail_closed(self):
        for relative in (".state", ".state/ncs-data-builder", ".state/ncs-data-builder/versions"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "root"
                outside = Path(tmp) / "outside"
                root.mkdir()
                outside.mkdir()
                junction = root / relative
                junction.parent.mkdir(parents=True, exist_ok=True)
                command = (
                    "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''")
                    + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True,
                               capture_output=True)
                try:
                    with self.assertRaises(BuilderAuthorizationError):
                        DataBuilder(root)
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    # rmdir removes this exact junction, never its target contents.
                    os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_versions_junction_introduced_after_init_blocks_operation(self):
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.builder.state / "versions"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{junction}' -Target '{outside}' | Out-Null"],
                       check=True, capture_output=True)
        try:
            with self.assertRaises(BuilderAuthorizationError):
                self.builder._version_dir("abc")
            with self.assertRaises(BuilderError):
                with self.builder.exclusive("build_delta"):
                    self.fail("junction must prevent issuance")
            self.assertFalse(self.lock.exists())
        finally:
            os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
