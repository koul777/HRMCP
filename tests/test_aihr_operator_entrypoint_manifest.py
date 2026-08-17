from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.build_aihr_operator_entrypoint_manifest import (  # noqa: E402
    bad_claims_for_markdown,
    build_manifest,
    main as entrypoint_main,
)


class AihrOperatorEntrypointManifestTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _safe_top(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "acceptance_claim": False,
            "human_decision_required": True,
        }
        payload.update(extra)
        return payload

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        crosswalk = self._write_csv(
            reports / "transition_provenance_operator_crosswalk_fixture.csv",
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
                    "operator_source_artifact_hash": "sha256:" + "1" * 64,
                    "operator_decision_fields_blank": "true",
                    "operator_guard_fields_false": "true",
                }
            ],
        )
        decision = self._write_csv(
            reports / "human_review_provenance_reconfirmation_decision_sheet_fixture.csv",
            [
                "order",
                "decision",
                "rationale",
                "reviewer_id",
                "reviewed_at",
                "source_decision_packet",
                "evidence_refs_json",
                "status_update_allowed",
                "db_writes",
                "approval_claim",
            ],
            [
                {
                    "order": "24",
                    "decision": "",
                    "rationale": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "source_decision_packet": "",
                    "evidence_refs_json": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                }
            ],
        )
        concept = self._write_csv(
            reports / "aihr_ontology_definition_review_seedpack_fixture.csv",
            [
                "sequence",
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
                    "sequence": "1",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                }
            ],
        )
        qualification = self._write_csv(
            reports / "qualification_guarded_batch_operator_decision_fixture.csv",
            ["wave", "batch_count", "requires_operator_start", "purpose"],
            [
                {
                    "wave": "pilot",
                    "batch_count": "1",
                    "requires_operator_start": "true",
                    "purpose": "operator timing",
                }
            ],
        )
        for path in (crosswalk, decision, concept, qualification):
            path.with_suffix(".md").write_text("# artifact\n", encoding="utf-8")
        next_actions = self._write_json(
            reports / "aihr_operator_next_actions_fixture.json",
            self._safe_top(
                schema="aihr_operator_next_actions_v3",
                actions=[
                    {
                        "id": "transition_eval:trusted_scenarios",
                        "blocker": "transition_eval:trusted_scenarios",
                        "open_first": crosswalk.relative_to(root).as_posix(),
                        "artifacts_to_open": [
                            crosswalk.relative_to(root).as_posix(),
                            decision.relative_to(root).as_posix(),
                        ],
                        "human_decision_required": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                    }
                ],
            ),
        )
        sprint_queue = self._write_json(
            reports / "aihr_blocker_reduction_operator_sprint_queue_fixture.json",
            self._safe_top(
                schema="aihr_blocker_reduction_operator_sprint_queue_v1",
                queue=[
                    {
                        "rank": 1,
                        "sprint_id": "S1-transition",
                        "blocker": "transition_eval:trusted_scenarios",
                        "open_first": crosswalk.relative_to(root).as_posix(),
                        "artifacts_to_open": [
                            crosswalk.relative_to(root).as_posix(),
                            decision.relative_to(root).as_posix(),
                        ],
                        "required_human_fields": [
                            "decision",
                            "rationale",
                            "reviewer_id",
                            "reviewed_at",
                            "source_decision_packet",
                            "evidence_refs_json",
                        ],
                    },
                    {
                        "rank": 2,
                        "sprint_id": "S2-concepts",
                        "blocker": "review_debt:human_reviewed_concepts",
                        "open_first": concept.relative_to(root).as_posix(),
                        "artifacts_to_open": [concept.relative_to(root).as_posix()],
                        "required_human_fields": [
                            "decision",
                            "reviewer_id",
                            "reviewed_at",
                            "rationale",
                        ],
                    },
                    {
                        "rank": 3,
                        "sprint_id": "S3-qualification",
                        "blocker": "qualification:collection_coverage",
                        "open_first": qualification.relative_to(root).as_posix(),
                        "artifacts_to_open": [qualification.relative_to(root).as_posix()],
                        "required_human_fields": [
                            "operator timing approval",
                            "batch count",
                        ],
                    },
                ],
            ),
        )
        return {
            "next_actions": next_actions,
            "sprint_queue": sprint_queue,
            "crosswalk": crosswalk,
            "decision": decision,
            "concept": concept,
            "qualification": qualification,
        }

    def test_manifest_maps_crosswalk_to_decision_sheet_and_preserves_safety_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
                generated_at="2026-07-12T14:20:00+00:00",
            )

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertTrue(report["terminal_evidence_only"])
        self.assertEqual(4, report["summary"]["entry_count"])
        self.assertEqual(2, report["summary"]["csv_decision_surface_count"])
        self.assertTrue(
            report["cycle_avoidance_contract"]["source_hashes_exclude_context_only_keys"]
        )
        self.assertTrue(
            all(check["hash_matches"] for check in report["source_hash_checks"].values())
        )
        self.assertTrue(
            all(
                check["lineage_validation"] is False
                for check in report["source_hash_checks"].values()
            )
        )
        self.assertFalse(report["source_hash_check_scope"]["lineage_validation"])
        transition_entry = next(
            item for item in report["entries"] if item["id"] == "S1-transition"
        )
        self.assertEqual("crosswalk_map", transition_entry["kind"])
        self.assertTrue(transition_entry["human_decision_required"])
        self.assertFalse(transition_entry["status_update_allowed"])
        self.assertFalse(transition_entry["db_writes"])
        self.assertFalse(transition_entry["api_calls"])
        self.assertFalse(transition_entry["approval_claim"])
        self.assertEqual(
            "reports/human_review_provenance_reconfirmation_decision_sheet_fixture.csv",
            transition_entry["decision_surface"]["path"],
        )
        self.assertEqual(1, transition_entry["row_count_actual"])
        self.assertTrue(transition_entry["markdown_claim_scan_ok"])
        qualification_entry = next(
            item for item in report["entries"] if item["id"] == "S3-qualification"
        )
        self.assertEqual("guarded_api_timing_surface", qualification_entry["kind"])
        self.assertEqual("not_applicable", qualification_entry["decision_surface"]["status"])

    def test_prefilled_decision_field_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            self._write_csv(
                paths["concept"],
                [
                    "sequence",
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
                        "sequence": "1",
                        "decision": "accept_concept",
                        "reviewer_id": "",
                        "reviewed_at": "",
                        "rationale": "",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    }
                ],
            )
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("entry_decision_fields_not_blank", codes)

    def test_cycle_safe_release_hash_ignores_raw_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            release = self._write_json(
                root / "reports" / "aihr_release_readiness_fixture.json",
                {
                    "schema": "aihr_release_readiness_v1",
                    "sha256_scope": "cycle_safe_release_readiness",
                    "cycle_safe_content_sha256": "sha256:" + "a" * 64,
                    "generated_at": "2026-07-12T00:00:00+00:00",
                },
            )
            payload = json.loads(paths["next_actions"].read_text(encoding="utf-8"))
            payload["source_paths"] = {
                "release_readiness": release.relative_to(root).as_posix()
            }
            payload["source_hashes"] = {
                "release_readiness": "sha256:" + "a" * 64
            }
            payload["source_hash_scopes"] = {
                "release_readiness": "cycle_safe_release_readiness"
            }
            self._write_json(paths["next_actions"], payload)

            release_payload = json.loads(release.read_text(encoding="utf-8"))
            release_payload["generated_at"] = "2026-07-13T00:00:00+00:00"
            self._write_json(release, release_payload)
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
            )

        check = report["next_actions_source_hash_checks"]["release_readiness"]
        self.assertTrue(report["ok"])
        self.assertTrue(check["hash_matches"])
        self.assertEqual("cycle_safe_release_readiness", check["sha256_scope"])

    def test_unsafe_source_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["next_actions"].read_text(encoding="utf-8"))
            payload["db_writes"] = True
            self._write_json(paths["next_actions"], payload)
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_source_contract", codes)

    def test_entry_guard_flag_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["sprint_queue"].read_text(encoding="utf-8"))
            payload["queue"][0]["db_writes"] = True
            self._write_json(paths["sprint_queue"], payload)
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("entry_guard_flag_not_false", codes)
        issue = next(item for item in report["issues"] if item["code"] == "entry_guard_flag_not_false")
        self.assertEqual("db_writes", issue["field"])

    def test_acceptance_claim_source_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["next_actions"].read_text(encoding="utf-8"))
            payload["acceptance_claim"] = True
            self._write_json(paths["next_actions"], payload)
            report = build_manifest(
                next_actions_path=paths["next_actions"],
                sprint_queue_path=paths["sprint_queue"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_source_contract", codes)

    def test_markdown_claim_scanner_flags_unsafe_claims_and_ignores_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "reports" / "unsafe.md"
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_text(
                "\n".join(
                    [
                        "Decision options: accepted, rejected, defer",
                        "- approval_claim: `False`",
                        "- human_reviewed: true",
                        "This item was accepted.",
                        "Set status to accepted when required by operator.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            findings = bad_claims_for_markdown(markdown, root=root)

        self.assertEqual(3, len(findings))
        self.assertTrue(any("human_reviewed: true" in item for item in findings))
        self.assertTrue(any("was accepted" in item for item in findings))
        self.assertTrue(any("Set status to accepted" in item for item in findings))
        self.assertFalse(any("Decision options" in item for item in findings))

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "entrypoint_manifest.json"
            md = root / "reports" / "entrypoint_manifest.md"
            argv = [
                "--root",
                str(root),
                "--next-actions",
                str(paths["next_actions"].relative_to(root)),
                "--sprint-queue",
                str(paths["sprint_queue"].relative_to(root)),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = entrypoint_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Operator Entrypoint Manifest", markdown)
        self.assertIn("approval_claim: `False`", markdown)


if __name__ == "__main__":
    unittest.main()
