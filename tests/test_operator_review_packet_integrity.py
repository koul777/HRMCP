from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_operator_review_packet_integrity import (  # noqa: E402
    build_integrity_audit,
    sha256_file,
    write_markdown,
)


class OperatorReviewPacketIntegrityAuditTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _json_with_md(
        self,
        path: Path,
        *,
        schema: str,
        source_paths: dict[str, str] | None = None,
        source_hashes: dict[str, str | None] | None = None,
        extra: dict | None = None,
    ) -> None:
        payload = {
            "schema": schema,
            "generated_at": "2026-07-12T03:00:00+00:00",
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
            "forbidden_automatic_statuses": [
                "human_reviewed",
                "accepted",
                "reviewed",
            ],
        }
        if source_paths is not None:
            payload["source_paths"] = source_paths
            payload["source_hashes"] = source_hashes or {}
        if extra:
            payload.update(extra)
        self._write_json(path, payload)
        path.with_suffix(".md").write_text(
            f"# {schema}\n\n- generated_at: `{payload['generated_at']}`\n",
            encoding="utf-8",
        )

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        packet = reports / "packet.json"
        self._write_json(packet, {"schema": "operator_source_packet"})
        packet_hash = sha256_file(packet)
        concept_csv = reports / "concept.csv"
        blocker_csv = reports / "blocker.csv"
        decision_csv = reports / "decision.csv"
        for path in (concept_csv, blocker_csv, decision_csv):
            self._write_csv(
                path,
                [
                    "id",
                    "decision",
                    "reviewer_id",
                    "reviewed_at",
                    "rationale",
                    "status_update_allowed",
                    "db_writes",
                    "approval_claim",
                ],
                [
                    {
                        "id": "1",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    }
                ],
            )

        qualification_csv = reports / "qualification.csv"
        self._write_csv(
            qualification_csv,
            ["wave", "batch_count", "requires_operator_start"],
            [{"wave": "pilot", "batch_count": "3", "requires_operator_start": "True"}],
        )
        crosswalk_csv = reports / "transition_provenance_operator_crosswalk_20260712_10h.csv"
        self._write_csv(
            crosswalk_csv,
            [
                "scenario_id",
                "decision_sheet_order",
                "operator_source_decision_packet_ref",
                "operator_source_artifact_hash",
                "operator_decision_fields_blank",
                "operator_guard_fields_false",
            ],
            [
                {
                    "scenario_id": "3",
                    "decision_sheet_order": "24",
                    "operator_source_decision_packet_ref": "reports/packet.json#order:24",
                    "operator_source_artifact_hash": packet_hash,
                    "operator_decision_fields_blank": "true",
                    "operator_guard_fields_false": "true",
                }
            ],
        )

        support = reports / "support.json"
        self._write_json(support, {"ok": True})
        queue_json = reports / "queue.json"
        queue_audit_json = reports / "queue_audit.json"
        crosswalk_json = reports / "transition_provenance_operator_crosswalk_20260712_10h.json"
        crosswalk_audit_json = reports / "transition_provenance_operator_crosswalk_audit_20260712_10h.json"
        decision_json = reports / "decision.json"
        decision_audit_json = reports / "decision_audit.json"
        qualification_json = reports / "qualification.json"
        transition_gap_json = reports / "transition_gap.json"
        next_actions_json = reports / "next_actions.json"

        self._json_with_md(
            queue_json,
            schema="aihr_blocker_reduction_operator_sprint_queue_v1",
            source_paths={"crosswalk": "reports/transition_provenance_operator_crosswalk_20260712_10h.csv"},
            source_hashes={"crosswalk": sha256_file(crosswalk_csv)},
            extra={
                "queue": [
                    {
                        "sprint_id": "S1-transition-provenance-crosswalk",
                        "open_first": "reports/transition_provenance_operator_crosswalk_20260712_10h.csv",
                    }
                ]
            },
        )
        self._json_with_md(
            queue_audit_json,
            schema="aihr_blocker_reduction_operator_sprint_queue_audit_v1",
        )
        self._json_with_md(
            crosswalk_json,
            schema="transition_provenance_operator_crosswalk_v1",
            source_paths={"support": "reports/support.json"},
            source_hashes={"support": sha256_file(support)},
        )
        self._json_with_md(
            crosswalk_audit_json,
            schema="transition_provenance_operator_crosswalk_audit_v1",
            extra={"warning_count": 1},
        )
        self._json_with_md(
            decision_json,
            schema="aihr_provenance_reconfirmation_decision_sheet_v1",
        )
        self._json_with_md(
            decision_audit_json,
            schema="aihr_provenance_reconfirmation_decision_audit_v1",
        )
        self._json_with_md(
            qualification_json,
            schema="qualification_guarded_batch_operator_decision_v1",
            source_paths={"support": "reports/support.json"},
            source_hashes={"support": sha256_file(support)},
            extra={
                "execution_authorized": False,
                "automatic_queue_execution_allowed": False,
            },
        )
        self._json_with_md(
            transition_gap_json,
            schema="transition_trusted_scenario_provenance_gap_v1",
        )
        self._json_with_md(
            next_actions_json,
            schema="aihr_operator_next_actions_v3",
            source_paths={
                "blocker_reduction_sprint_queue": "reports/queue.json",
                "blocker_reduction_sprint_queue_audit": "reports/queue_audit.json",
                "transition_provenance_crosswalk": "reports/transition_provenance_operator_crosswalk_20260712_10h.json",
                "transition_provenance_crosswalk_csv": "reports/transition_provenance_operator_crosswalk_20260712_10h.csv",
                "transition_provenance_crosswalk_audit": "reports/transition_provenance_operator_crosswalk_audit_20260712_10h.json",
            },
            source_hashes={
                "blocker_reduction_sprint_queue": sha256_file(queue_json),
                "blocker_reduction_sprint_queue_audit": sha256_file(queue_audit_json),
                "transition_provenance_crosswalk": sha256_file(crosswalk_json),
                "transition_provenance_crosswalk_csv": sha256_file(crosswalk_csv),
                "transition_provenance_crosswalk_audit": sha256_file(crosswalk_audit_json),
            },
            extra={
                "actions": [
                    {
                        "id": "transition_eval:trusted_scenarios",
                        "open_first": "reports/transition_provenance_operator_crosswalk_20260712_10h.csv",
                        "artifacts_to_open": [
                            "reports/transition_provenance_operator_crosswalk_20260712_10h.csv"
                        ],
                    }
                ]
            },
        )

        return {
            "concept_csv": concept_csv,
            "blocker_csv": blocker_csv,
            "decision_csv": decision_csv,
            "crosswalk_csv": crosswalk_csv,
            "qualification_csv": qualification_csv,
            "decision_json": decision_json,
            "decision_audit_json": decision_audit_json,
            "qualification_json": qualification_json,
            "transition_gap_json": transition_gap_json,
            "crosswalk_json": crosswalk_json,
            "crosswalk_audit_json": crosswalk_audit_json,
            "queue_json": queue_json,
            "queue_audit_json": queue_audit_json,
            "next_actions_json": next_actions_json,
            "packet": packet,
        }

    def _build(self, root: Path, paths: dict[str, Path]) -> dict:
        return build_integrity_audit(
            concept_seedpack_csv=paths["concept_csv"],
            blocker_ranked_seedpack_csv=paths["blocker_csv"],
            provenance_decision_sheet_csv=paths["decision_csv"],
            transition_crosswalk_csv=paths["crosswalk_csv"],
            qualification_decision_csv=paths["qualification_csv"],
            provenance_decision_sheet_json=paths["decision_json"],
            provenance_decision_audit_json=paths["decision_audit_json"],
            qualification_decision_json=paths["qualification_json"],
            transition_gap_json=paths["transition_gap_json"],
            transition_crosswalk_json=paths["crosswalk_json"],
            transition_crosswalk_audit_json=paths["crosswalk_audit_json"],
            blocker_sprint_queue_json=paths["queue_json"],
            blocker_sprint_queue_audit_json=paths["queue_audit_json"],
            operator_next_actions_json=paths["next_actions_json"],
            generated_at="2026-07-12T03:10:00+00:00",
            root=root,
        )

    def test_integrity_audit_passes_for_blank_decision_surfaces_and_crosswalk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = self._build(root, paths)

        self.assertTrue(report["ok"])
        self.assertEqual(report["issue_count"], 0)
        self.assertEqual(report["warning_count"], 1)
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertTrue(report["csv_reference_surfaces"][0]["operator_decision_fields_blank_ok"])
        self.assertFalse(report["next_actions_checks"]["missing_source_keys"])
        self.assertIn(
            "transition_provenance_operator_crosswalk",
            report["sprint_queue_checks"]["s1_open_first"],
        )

    def test_integrity_audit_flags_decision_contamination_and_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            with paths["decision_csv"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["decision"] = "approve"
            self._write_csv(paths["decision_csv"], list(rows[0].keys()), rows)
            next_payload = json.loads(paths["next_actions_json"].read_text(encoding="utf-8"))
            next_payload["source_hashes"]["transition_provenance_crosswalk_csv"] = (
                "sha256:" + "0" * 64
            )
            next_payload["actions"][0]["open_first"] = "reports/missing.csv"
            self._write_json(paths["next_actions_json"], next_payload)
            report = self._build(root, paths)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("csv_decision_fields_not_blank", codes)
        self.assertIn("json_source_hash_stale", codes)
        self.assertIn("next_actions_open_first_missing", codes)

    def test_integrity_audit_flags_crosswalk_row_packet_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            with paths["crosswalk_csv"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["operator_source_artifact_hash"] = "sha256:" + "0" * 64
            self._write_csv(paths["crosswalk_csv"], list(rows[0].keys()), rows)
            for json_key in ("queue_json", "next_actions_json"):
                payload = json.loads(paths[json_key].read_text(encoding="utf-8"))
                source_hashes = payload.get("source_hashes")
                if isinstance(source_hashes, dict):
                    for key, value in list(source_hashes.items()):
                        if str(value or ""):
                            path_value = payload["source_paths"][key]
                            source_path = root / Path(path_value)
                            source_hashes[key] = sha256_file(source_path)
                self._write_json(paths[json_key], payload)
            report = self._build(root, paths)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("crosswalk_operator_source_artifact_hash_mismatch", codes)
        self.assertNotIn("json_source_hash_stale", codes)

    def test_integrity_audit_flags_missing_json_source_hashes_and_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            next_payload = json.loads(paths["next_actions_json"].read_text(encoding="utf-8"))
            next_payload.pop("source_hashes")
            self._write_json(paths["next_actions_json"], next_payload)
            qualification_payload = json.loads(paths["qualification_json"].read_text(encoding="utf-8"))
            for field in ("status_update_allowed", "db_writes", "approval_claim"):
                qualification_payload.pop(field, None)
            self._write_json(paths["qualification_json"], qualification_payload)
            report = self._build(root, paths)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("json_source_hashes_missing", codes)
        self.assertIn("json_required_guard_fields_missing", codes)

    def test_integrity_audit_flags_missing_csv_guard_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            self._write_csv(
                paths["decision_csv"],
                ["id", "decision", "reviewer_id", "reviewed_at", "rationale"],
                [{"id": "1"}],
            )
            report = self._build(root, paths)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("csv_required_guard_columns_missing", codes)

    def test_markdown_writer_preserves_contract_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = self._build(root, paths)
            markdown = root / "integrity.md"
            write_markdown(markdown, report)
            text = markdown.read_text(encoding="utf-8")

        self.assertIn("operator_review_packet_integrity_audit_v2", text)
        self.assertIn("status_update_allowed: `False`", text)
        self.assertIn("No operator packet integrity issues found.", text)


if __name__ == "__main__":
    unittest.main()
