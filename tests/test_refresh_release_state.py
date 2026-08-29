from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.ontology_refresh_builder import (
    RefreshBuilderError,
    build_ontology_refresh,
    resolve_managed_baseline,
)
from ncs_mcp.refresh_release_state import promote_refresh_baseline
from scripts import promote_ncs_refresh_baseline as promotion_cli


class RefreshReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self._source_db("source.db")
        self.publisher = self.root / "publisher.db"
        self.refresh = build_ontology_refresh(
            self.source,
            state_dir=self.root / "builder-state",
            prepared_output=self.publisher,
            apply=True,
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
            "publication": {"attempted": True},
            "policy": {
                "stage_verified_before_publish": True,
                "source_hash_rechecked_after_build": True,
            },
        }

    @staticmethod
    def _verification_report() -> dict:
        return {
            "schema": "ncs_remote_mcp_transport_verification_v1",
            "ok": True,
            "failures": [],
            "checks": {"initialize": {"status": 200}},
        }

    def _promote(self) -> dict:
        return promote_refresh_baseline(
            refresh_report_path=self.refresh_path,
            publish_report_path=self.publish_path,
            remote_verification_path=self.verify_path,
            state_dir=self.state,
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
        self.assertFalse(report["safety"]["automatic_deletion"])

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

        report = build_ontology_refresh(
            self.source,
            state_dir=self.state,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
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

        report = build_ontology_refresh(
            self.source,
            state_dir=legacy_state,
            apply=True,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )
        self.assertEqual(report["selected_strategy"], "no_rebuild")
        self.assertEqual(report["publisher_source"]["path"], str(legacy.resolve()))

    def test_promotion_cli_writes_machine_readable_report(self) -> None:
        out = self.root / "promotion-report.json"
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

        self.assertEqual(return_code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["schema"],
            "ncs_ontology_refresh_baseline_promotion_report_v1",
        )


if __name__ == "__main__":
    unittest.main()
