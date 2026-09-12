from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ncs_mcp.refresh_release_state as release_state
from ncs_mcp.data_builder import DataBuilder
from ncs_mcp.db import connect, initialize_database
from ncs_mcp.ontology_refresh_builder import (
    RefreshBuilderError,
    build_ontology_refresh,
    resolve_managed_baseline,
)
from ncs_mcp.refresh_release_state import (
    RefreshReleaseStateError,
    promote_refresh_baseline,
    write_promotion_report,
)
from scripts import promote_ncs_refresh_baseline as promotion_cli


class RefreshReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.builder = DataBuilder(self.root)
        self.source = self._source_db("source.db")
        refresh_version = "aabbcc01"
        self.publisher = (
            self.builder.state / "versions" / refresh_version / "ncs.db"
        )
        with self.builder.exclusive(
            "build_delta", refresh_version
        ) as builder_context:
            self.refresh = build_ontology_refresh(
                self.source,
                state_dir=self.root / "builder-state",
                prepared_output=self.publisher,
                apply=True,
                builder_context=builder_context,
            )
        self.assertTrue(self.refresh["ok"])
        self.refresh_path = self._write("refresh.json", self.refresh)
        self.publish_path = self._write("publish.json", self._publish_report())
        self.verify_path = self._write("verify.json", self._verification_report())
        self.state = self.root / "managed-state"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _source_db(self, name: str) -> Path:
        path = self.root / name
        with closing(connect(path)) as conn:
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code,major_name,middle_code,middle_name,
                    small_code,small_name,sub_code,sub_name
                ) VALUES ('01','major','01','middle','01','small','01','sub')
                """
            )
            classification_id = int(
                conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code,base_unit_code,unit_version,unit_name_raw,
                    unit_level_raw,classification_id,created_at,updated_at
                ) VALUES ('U1','U1','v1','unit','3',?,'now','now')
                """,
                (classification_id,),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code,element_no,element_code_raw,element_name_raw,element_level_raw
                ) VALUES ('U1','1','E1','element','3')
                """
            )
            element_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO performance_criteria(element_id,criteria_no,criteria_text_raw) "
                "VALUES (?,'1','criterion')",
                (element_id,),
            )
            conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id,ksa_type_code,ksa_type_name,ksa_no,ksa_text_raw
                ) VALUES (?,'K','knowledge','1','source knowledge')
                """,
                (element_id,),
            )
            conn.commit()
        return path

    def _write(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _publish_report(self) -> dict:
        return {
            "schema": "ncs_vercel_snapshot_publish_report_v1",
            "ok": True,
            "dry_run": False,
            "source": dict(self.refresh["publisher_source"]),
            "targets": {
                "archive": str(self.root / "deploy" / "api" / "snapshot.zip"),
                "manifest": str(
                    self.root / "deploy" / "api" / "snapshot.manifest.json"
                ),
            },
            "publication": {"attempted": True},
            "policy": {
                "stage_verified_before_publish": True,
                "source_hash_rechecked_after_build": True,
            },
        }

    @staticmethod
    def _verification_report(
        url: str = "https://production.example/api/mcp",
    ) -> dict:
        return {
            "schema": "ncs_remote_mcp_transport_verification_v1",
            "ok": True,
            "url": url,
            "failures": [],
            "checks": {"initialize": {"status": 200}},
        }

    def _promote(self) -> dict:
        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            return promote_refresh_baseline(
                refresh_report_path=self.refresh_path,
                publish_report_path=self.publish_path,
                remote_verification_path=self.verify_path,
                state_dir=self.state,
                builder_context=builder_context,
            )

    def test_failed_publish_never_promotes(self) -> None:
        publish = self._publish_report()
        publish["ok"] = False
        self.publish_path = self._write("publish-failed.json", publish)

        report = self._promote()

        self.assertFalse(report["ok"])
        self.assertIn(
            "publish_not_successful_non_dry",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse((self.state / "current.json").exists())
        self.assertFalse((self.state / "baselines").exists())

    def test_failed_remote_verification_never_promotes(self) -> None:
        verification = self._verification_report()
        verification["ok"] = False
        verification["failures"] = ["tools_list"]
        self.verify_path = self._write("verify-failed.json", verification)

        report = self._promote()

        self.assertFalse(report["ok"])
        self.assertIn(
            "remote_verification_failed",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_failed_staged_verification_never_promotes(self) -> None:
        staged = self._verification_report("https://staged.example/api/mcp")
        staged["ok"] = False
        staged["failures"] = ["tools_call_ontology"]
        staged_path = self._write("staged-failed.json", staged)

        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            report = promote_refresh_baseline(
                refresh_report_path=self.refresh_path,
                publish_report_path=self.publish_path,
                staged_verification_path=staged_path,
                remote_verification_path=self.verify_path,
                state_dir=self.state,
                builder_context=builder_context,
            )

        self.assertFalse(report["ok"])
        self.assertIn(
            "staged_verification_failed",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_publish_source_hash_mismatch_blocks(self) -> None:
        publish = self._publish_report()
        publish["source"]["sha256"] = "sha256:" + ("0" * 64)
        self.publish_path = self._write("publish-mismatch.json", publish)

        report = self._promote()

        self.assertFalse(report["ok"])
        self.assertIn(
            "publish_source_identity_mismatch",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_publisher_sqlite_sidecar_blocks_exact_promotion(self) -> None:
        sidecar = self.publisher.with_name(self.publisher.name + "-wal")
        sidecar.write_bytes(b"pending")

        report = self._promote()

        self.assertFalse(report["ok"])
        self.assertIn(
            "publisher_source_has_sqlite_sidecars",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_success_promotes_immutable_baseline_and_resolves_pointer(self) -> None:
        report = self._promote()

        self.assertTrue(report["ok"])
        baseline = Path(report["promoted_baseline"]["path"])
        self.assertTrue(baseline.is_file())
        self.assertEqual(
            report["promoted_baseline"]["sha256"],
            self.refresh["publisher_source"]["sha256"],
        )
        self.assertEqual(resolve_managed_baseline(self.state), baseline)
        pointer = json.loads((self.state / "current.json").read_text(encoding="utf-8"))
        self.assertFalse(Path(pointer["baseline"]["path"]).is_absolute())
        lineage = baseline.with_suffix(baseline.suffix + ".refresh.json")
        self.assertTrue(lineage.is_file())
        lineage_payload = json.loads(lineage.read_text(encoding="utf-8"))
        self.assertEqual(
            lineage_payload["builder_operation"]["action"],
            "promote_refresh_baseline",
        )
        self.assertEqual(
            report["builder_operation"]["operation_id"],
            lineage_payload["builder_operation"]["operation_id"],
        )
        self.assertFalse(report["safety"]["automatic_deletion"])

    def test_success_records_staged_and_production_verification_lineage(self) -> None:
        staged_path = self._write(
            "staged.json",
            self._verification_report("https://staged.example/api/mcp"),
        )

        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            report = promote_refresh_baseline(
                refresh_report_path=self.refresh_path,
                publish_report_path=self.publish_path,
                staged_verification_path=staged_path,
                remote_verification_path=self.verify_path,
                state_dir=self.state,
                builder_context=builder_context,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(
            report["verification_targets"],
            {
                "staged": "https://staged.example/api/mcp",
                "production": "https://production.example/api/mcp",
            },
        )
        lineage = json.loads(
            Path(report["lineage"]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            lineage["verification_targets"]["staged"]["report"]["sha256"],
            report["inputs"]["staged_verification"]["sha256"],
        )
        self.assertEqual(
            lineage["verification_targets"]["production"]["report"]["sha256"],
            report["inputs"]["remote_verification"]["sha256"],
        )

    def test_pointer_hash_tamper_is_rejected(self) -> None:
        report = self._promote()
        self.assertTrue(report["ok"])
        pointer_path = self.state / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["baseline"]["sha256"] = "sha256:" + ("0" * 64)
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

        with self.assertRaisesRegex(RefreshBuilderError, "pointer hash"):
            resolve_managed_baseline(self.state)

    def test_promoted_pointer_drives_no_rebuild_publisher_selection(self) -> None:
        promotion = self._promote()
        self.assertTrue(promotion["ok"])

        version = "aabbcc02"
        prepared_output = self.builder.state / "versions" / version / "ncs.db"
        with self.builder.exclusive("build_delta", version) as builder_context:
            report = build_ontology_refresh(
                self.source,
                state_dir=self.state,
                prepared_output=prepared_output,
                apply=True,
                full_rebuild_change_ratio_threshold=1.0,
                per_table_change_ratio_threshold=1.0,
                builder_context=builder_context,
            )

        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(
            report["publisher_source"]["path"],
            promotion["promoted_baseline"]["path"],
        )
        self.assertNotEqual(
            report["publisher_source"]["path"], report["source"]["path"]
        )

    def test_legacy_baseline_db_resolution_remains_supported(self) -> None:
        legacy_state = self.root / "legacy-state"
        legacy_state.mkdir()
        legacy = legacy_state / "baseline.db"
        legacy.write_bytes(self.publisher.read_bytes())

        self.assertEqual(resolve_managed_baseline(legacy_state), legacy)

        version = "aabbcc03"
        prepared_output = self.builder.state / "versions" / version / "ncs.db"
        with self.builder.exclusive("build_delta", version) as builder_context:
            report = build_ontology_refresh(
                self.source,
                state_dir=legacy_state,
                prepared_output=prepared_output,
                apply=True,
                full_rebuild_change_ratio_threshold=1.0,
                per_table_change_ratio_threshold=1.0,
                builder_context=builder_context,
            )
        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertEqual(report["publisher_source"]["path"], str(legacy.resolve()))

    def test_promotion_cli_is_retired_with_machine_readable_auth_error(self) -> None:
        out = self.root / "promotion-report.json"
        staged = self._write(
            "staged-cli.json",
            self._verification_report("https://staged.example/api/mcp"),
        )
        with redirect_stdout(io.StringIO()):
            return_code = promotion_cli.main(
                [
                    "--refresh-report",
                    str(self.refresh_path),
                    "--publish-report",
                    str(self.publish_path),
                    "--staged-verification",
                    str(staged),
                    "--remote-verification",
                    str(self.verify_path),
                    "--state-dir",
                    str(self.state),
                    "--out",
                    str(out),
                ]
            )

        self.assertEqual(return_code, 2)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["schema"],
            "ncs_ontology_refresh_baseline_promotion_report_v1",
        )
        self.assertEqual(
            payload["blockers"][0]["code"], "builder_authorization_required"
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_valid_promotion_rejects_missing_and_forged_builder_context(self) -> None:
        for forged in (None, False, True, {}, {"action": "promote_refresh_baseline"}):
            with self.subTest(forged=forged):
                report = promote_refresh_baseline(
                    refresh_report_path=self.refresh_path,
                    publish_report_path=self.publish_path,
                    remote_verification_path=self.verify_path,
                    state_dir=self.state,
                    builder_context=forged,  # type: ignore[arg-type]
                )
                self.assertFalse(report["ok"])
                self.assertEqual(
                    report["blockers"][0]["code"],
                    "builder_authorization_required",
                )
                self.assertFalse((self.state / "current.json").exists())
                self.assertFalse((self.state / "baselines").exists())

    def test_invalid_evidence_remains_report_only_without_builder_context(self) -> None:
        publish = self._publish_report()
        publish["ok"] = False
        publish_path = self._write("publish-invalid-report-only.json", publish)

        report = promote_refresh_baseline(
            refresh_report_path=self.refresh_path,
            publish_report_path=publish_path,
            remote_verification_path=self.verify_path,
            state_dir=self.state,
        )

        self.assertFalse(report["ok"])
        self.assertIn(
            "publish_not_successful_non_dry",
            {item["code"] for item in report["blockers"]},
        )
        self.assertNotIn(
            "builder_authorization_required",
            {item["code"] for item in report["blockers"]},
        )
        self.assertFalse(self.state.exists())

    def test_promotion_context_binds_target_and_evidence_to_builder_root(self) -> None:
        unrelated_builder = DataBuilder(self.root / "unrelated")
        with unrelated_builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            report = promote_refresh_baseline(
                refresh_report_path=self.refresh_path,
                publish_report_path=self.publish_path,
                remote_verification_path=self.verify_path,
                state_dir=self.state,
                builder_context=builder_context,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["blockers"][0]["code"], "builder_authorization_required"
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_promotion_rejects_mismatched_publisher_builder_lineage(self) -> None:
        publish = self._publish_report()
        publish["lineage"] = {
            "builder_operation": {
                "schema": "ncs_data_builder_operation_v1",
                "owner": "windows_ncs_data_builder",
                "action": "publish_snapshot",
                "root": str((self.root / "other-root").resolve()),
            }
        }
        publish_path = self._write("publish-wrong-builder-root.json", publish)

        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            report = promote_refresh_baseline(
                refresh_report_path=self.refresh_path,
                publish_report_path=publish_path,
                remote_verification_path=self.verify_path,
                state_dir=self.state,
                builder_context=builder_context,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["blockers"][0]["code"], "builder_authorization_required"
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_promotion_lease_is_rechecked_before_lineage_and_pointer_writes(self) -> None:
        real_copy = release_state._copy_exact_immutable

        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            def copy_then_expire(
                source: Path,
                target: Path,
                expected: dict,
                *,
                mutation_guard,
            ) -> dict:
                artifact = real_copy(
                    source,
                    target,
                    expected,
                    mutation_guard=mutation_guard,
                )
                lock = self.builder.state / "operation.lock"
                payload = json.loads(lock.read_text(encoding="utf-8"))
                payload["operation_id"] = "expired-before-pointer"
                lock.write_text(json.dumps(payload), encoding="utf-8")
                return artifact

            with patch(
                "ncs_mcp.refresh_release_state._copy_exact_immutable",
                side_effect=copy_then_expire,
            ):
                report = promote_refresh_baseline(
                    refresh_report_path=self.refresh_path,
                    publish_report_path=self.publish_path,
                    remote_verification_path=self.verify_path,
                    state_dir=self.state,
                    builder_context=builder_context,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["blockers"][0]["code"], "builder_authorization_required"
        )
        self.assertFalse((self.state / "current.json").exists())

    def test_promotion_report_rejects_evidence_publisher_and_sidecar_paths(self) -> None:
        report = promote_refresh_baseline(
            refresh_report_path=self.refresh_path,
            publish_report_path=self.publish_path,
            remote_verification_path=self.verify_path,
            state_dir=self.state,
        )
        self.assertEqual(
            report["blockers"][0]["code"], "builder_authorization_required"
        )

        refresh_before = self.refresh_path.read_bytes()
        with self.assertRaisesRegex(
            RefreshReleaseStateError, "protected artifact"
        ):
            write_promotion_report(self.refresh_path, report)
        self.assertEqual(self.refresh_path.read_bytes(), refresh_before)

        publisher_before = self.publisher.read_bytes()
        with self.assertRaisesRegex(
            RefreshReleaseStateError, "protected artifact"
        ):
            write_promotion_report(self.publisher, report)
        self.assertEqual(self.publisher.read_bytes(), publisher_before)

        sidecar = self.publisher.with_name(self.publisher.name + "-wal")
        with self.assertRaisesRegex(
            RefreshReleaseStateError, "protected artifact"
        ):
            write_promotion_report(sidecar, report)
        self.assertFalse(sidecar.exists())

        archive = Path(self._publish_report()["targets"]["archive"])
        with self.assertRaisesRegex(
            RefreshReleaseStateError, "protected artifact"
        ):
            write_promotion_report(archive, report)
        self.assertFalse(archive.exists())

        baseline_report = self.state / "baselines" / "report.json"
        with self.assertRaisesRegex(
            RefreshReleaseStateError, "protected directory"
        ):
            write_promotion_report(baseline_report, report)
        self.assertFalse(self.state.exists())

    def test_authorization_blocked_cli_does_not_write_managed_pointer(self) -> None:
        out = self.state / "current.json"
        with redirect_stdout(io.StringIO()):
            return_code = promotion_cli.main(
                [
                    "--refresh-report",
                    str(self.refresh_path),
                    "--publish-report",
                    str(self.publish_path),
                    "--remote-verification",
                    str(self.verify_path),
                    "--state-dir",
                    str(self.state),
                    "--out",
                    str(out),
                ]
            )

        self.assertEqual(return_code, 2)
        self.assertFalse(out.exists())
        self.assertFalse(self.state.exists())

    def test_managed_baselines_reparse_escape_is_blocked(self) -> None:
        self.state.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as external_dir:
            external = Path(external_dir)
            baselines = self.state / "baselines"
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(baselines), str(external)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if created.returncode != 0:
                    self.skipTest("directory junction unavailable")
            else:
                os.symlink(external, baselines, target_is_directory=True)

            with self.builder.exclusive(
                "promote_refresh_baseline"
            ) as builder_context:
                report = promote_refresh_baseline(
                    refresh_report_path=self.refresh_path,
                    publish_report_path=self.publish_path,
                    remote_verification_path=self.verify_path,
                    state_dir=self.state,
                    builder_context=builder_context,
                )

            self.assertFalse(report["ok"])
            self.assertIn(
                "baselines_dir_invalid",
                {item["code"] for item in report["blockers"]},
            )
            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((self.state / "current.json").exists())

    def test_expired_promotion_lease_after_copy_performs_no_baseline_link(self) -> None:
        real_copyfile = release_state.shutil.copyfile

        with self.builder.exclusive(
            "promote_refresh_baseline"
        ) as builder_context:
            def copy_then_expire(source: object, target: object) -> object:
                result = real_copyfile(source, target)
                lock = self.builder.state / "operation.lock"
                payload = json.loads(lock.read_text(encoding="utf-8"))
                payload["operation_id"] = "expired-before-baseline-link"
                lock.write_text(json.dumps(payload), encoding="utf-8")
                return result

            with patch.object(
                release_state.shutil,
                "copyfile",
                side_effect=copy_then_expire,
            ):
                report = promote_refresh_baseline(
                    refresh_report_path=self.refresh_path,
                    publish_report_path=self.publish_path,
                    remote_verification_path=self.verify_path,
                    state_dir=self.state,
                    builder_context=builder_context,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["blockers"][0]["code"], "builder_authorization_required"
        )
        baselines = self.state / "baselines"
        self.assertFalse(any(path.suffix == ".db" for path in baselines.iterdir()))
        self.assertFalse(any(".incoming." in path.name for path in baselines.iterdir()))
        self.assertFalse((self.state / "current.json").exists())


if __name__ == "__main__":
    unittest.main()
