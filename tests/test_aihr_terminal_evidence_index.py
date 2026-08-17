from __future__ import annotations

import contextlib
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

from scripts.build_aihr_terminal_evidence_index import (  # noqa: E402
    build_index,
    main as evidence_index_main,
    sha256_file,
)


class AihrTerminalEvidenceIndexTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_text(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _safe_payload(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "ok": True,
            "status": "pass",
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

    def _fixture(self, root: Path) -> list[tuple[str, Path]]:
        reports = root / "reports"
        next_actions = self._write_json(
            reports / "aihr_operator_next_actions_fixture.json",
            self._safe_payload(schema="aihr_operator_next_actions_fixture_v1"),
        )
        handoff = self._write_json(
            reports / "overnight_10h_operator_handoff_fixture.json",
            self._safe_payload(schema="overnight_10h_operator_handoff_v3"),
        )
        legacy_source = self._write_json(
            reports / "source_fixture.json",
            self._safe_payload(schema="source_fixture_v1"),
        )
        entrypoint = self._write_json(
            reports / "aihr_operator_entrypoint_manifest_fixture.json",
            self._safe_payload(
                schema="aihr_operator_entrypoint_manifest_v1",
                terminal_evidence_only=True,
                include_in_release_refresh_dag=False,
                source_hash_checks={
                    "operator_next_actions": {
                        "path": "reports/aihr_operator_next_actions_fixture.json",
                        "expected_sha256": sha256_file(next_actions),
                        "hash_matches": True,
                    }
                },
                next_actions_source_hash_checks={
                    "handoff": {
                        "path": "reports/overnight_10h_operator_handoff_fixture.json",
                        "expected_sha256": sha256_file(handoff),
                        "hash_matches": True,
                    }
                },
            ),
        )
        post_handoff = self._write_json(
            reports / "aihr_post_handoff_validation_fixture.json",
            self._safe_payload(
                schema="aihr_post_handoff_validation_v1",
                terminal_evidence_only=True,
                include_in_release_refresh_dag=False,
                include_in_operator_handoff=False,
                source_hash_checks={
                    "operator_handoff": {
                        "path": "reports/overnight_10h_operator_handoff_fixture.json",
                        "expected_sha256": sha256_file(handoff),
                        "hash_matches": True,
                    }
                },
            ),
        )
        readability = self._write_json(
            reports / "review_artifact_readability_fixture.json",
            self._safe_payload(
                schema="review_artifact_readability_audit_v1",
                finding_count=0,
                artifact_count=2,
            ),
        )
        legacy_audit_payload = self._safe_payload(
            schema="legacy_audit_fixture_v1",
            source_hash_checks={
                "source": {
                    "path": "reports/source_fixture.json",
                    "expected_sha256": sha256_file(legacy_source),
                    "matches": True,
                }
            },
        )
        legacy_audit_payload.pop("human_decision_required")
        legacy_audit = self._write_json(
            reports / "legacy_audit_fixture.json",
            legacy_audit_payload,
        )
        markdown = self._write_text(
            reports / "aihr_operator_entrypoint_manifest_fixture.md",
            "# Manifest\n\n- approval_claim: `False`\n- db_writes: `False`\n",
        )
        return [
            ("operator_entrypoint_manifest", entrypoint),
            ("post_handoff_validation", post_handoff),
            ("operator_handoff", handoff),
            ("operator_entrypoint_readability", readability),
            ("legacy_audit", legacy_audit),
            ("operator_entrypoint_manifest_markdown", markdown),
        ]

    def test_index_passes_for_safe_terminal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            report = build_index(
                artifacts=artifacts,
                root=root,
                generated_at="2026-07-12T15:00:00+00:00",
            )

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["terminal_evidence_only"])
        self.assertFalse(report["include_in_release_refresh_dag"])
        self.assertFalse(report["include_in_operator_handoff"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["acceptance_claim"])
        self.assertEqual(6, report["summary"]["artifact_count"])
        self.assertEqual(6, report["summary"]["artifact_ok_count"])
        self.assertEqual(0, report["summary"]["source_hash_mismatch_count"])
        self.assertEqual(0, report["summary"]["issue_count"])
        self.assertEqual(
            "reports/aihr_operator_entrypoint_manifest_fixture.md",
            report["operator_start"]["open_first"],
        )
        self.assertTrue(
            report["cycle_policy"]["must_not_be_source_for"],
        )

    def test_embedded_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            entrypoint = artifacts[0][1]
            payload = json.loads(entrypoint.read_text(encoding="utf-8"))
            payload["source_hash_checks"]["operator_next_actions"]["hash_matches"] = False
            self._write_json(entrypoint, payload)
            report = build_index(artifacts=artifacts, root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("embedded_source_hash_mismatch", codes)

    def test_embedded_hash_true_fails_when_source_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            self._write_json(
                root / "reports" / "aihr_operator_next_actions_fixture.json",
                self._safe_payload(schema="changed_next_actions_fixture_v1"),
            )
            report = build_index(artifacts=artifacts, root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("embedded_source_hash_mismatch", codes)
        mismatch = report["artifacts"][0]["embedded_hash_mismatches"][0]
        self.assertEqual("current_source_hash_mismatch", mismatch["reason"])
        self.assertEqual("operator_next_actions", mismatch["key"])

    def test_embedded_cycle_safe_release_hash_allows_raw_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            release = self._write_json(
                root / "reports" / "aihr_release_readiness_fixture.json",
                {
                    "schema": "aihr_release_readiness_v1",
                    "sha256_scope": "cycle_safe_release_readiness",
                    "cycle_safe_content_sha256": "sha256:" + "a" * 64,
                    "generated_at": "2026-07-12T00:00:00+00:00",
                },
            )
            entrypoint = artifacts[0][1]
            payload = json.loads(entrypoint.read_text(encoding="utf-8"))
            payload["next_actions_source_hash_checks"]["release_readiness"] = {
                "path": release.relative_to(root).as_posix(),
                "expected_sha256": "sha256:" + "a" * 64,
                "sha256_scope": "cycle_safe_release_readiness",
                "hash_matches": True,
            }
            self._write_json(entrypoint, payload)

            release_payload = json.loads(release.read_text(encoding="utf-8"))
            release_payload["generated_at"] = "2026-07-13T00:00:00+00:00"
            self._write_json(release, release_payload)
            report = build_index(artifacts=artifacts, root=root)

        self.assertTrue(report["ok"])
        self.assertEqual(0, report["summary"]["source_hash_mismatch_count"])

    def test_unsafe_json_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            handoff = artifacts[2][1]
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["approval_claim"] = True
            self._write_json(handoff, payload)
            report = build_index(artifacts=artifacts, root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_json_contract", codes)

    def test_unsafe_markdown_flag_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            markdown = artifacts[-1][1]
            self._write_text(markdown, "# Manifest\n\n- approval_claim: `True`\n")
            report = build_index(artifacts=artifacts, root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_markdown_flag", codes)

    def test_nested_summary_issue_count_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            handoff = artifacts[2][1]
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["summary"] = {"issue_count": 2}
            self._write_json(handoff, payload)
            report = build_index(artifacts=artifacts, root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("json_reports_findings", codes)
        self.assertEqual(
            [{"counter": "summary.issue_count", "value": 2}],
            report["artifacts"][2]["reported_issue_counters"],
        )

    def test_json_warning_reports_include_source_warning_code_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            readability = artifacts[3][1]
            payload = json.loads(readability.read_text(encoding="utf-8"))
            payload["summary"] = {"warning_count": 2}
            payload["warnings"] = [
                {
                    "code": "outer_warning",
                    "source_warnings": [
                        {"code": "nested_warning"},
                        {"code": "nested_warning"},
                    ],
                },
                {"code": "second_warning"},
            ]
            self._write_json(readability, payload)
            report = build_index(artifacts=artifacts, root=root)

        expected_counts = [
            {"code": "outer_warning", "count": 1},
            {"code": "nested_warning", "count": 2},
            {"code": "second_warning", "count": 1},
        ]
        self.assertTrue(report["ok"])
        self.assertEqual(expected_counts, report["artifacts"][3]["source_warning_code_counts"])
        warning = next(item for item in report["warnings"] if item["label"] == "operator_entrypoint_readability")
        self.assertEqual(expected_counts, warning["source_warning_code_counts"])

    def test_post_decision_matrix_extra_artifact_requires_terminal_cycle_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            matrix = self._write_json(
                root / "reports" / "aihr_post_decision_validation_matrix_fixture.json",
                self._safe_payload(
                    schema="aihr_post_decision_validation_matrix_v1",
                    terminal_evidence_only=True,
                    include_in_release_refresh_dag=False,
                    include_in_operator_handoff=False,
                    summary={"issue_count": 0, "warning_count": 1},
                ),
            )
            report = build_index(
                artifacts=artifacts + [("post_decision_validation_matrix", matrix)],
                root=root,
            )

        labels = {item["label"] for item in report["artifacts"]}
        warning_codes = {warning["code"] for warning in report["warnings"]}
        self.assertTrue(report["ok"])
        self.assertIn("post_decision_validation_matrix", labels)
        self.assertIn("json_reports_warnings", warning_codes)

    def test_post_decision_matrix_without_terminal_cycle_flags_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            matrix = self._write_json(
                root / "reports" / "aihr_post_decision_validation_matrix_fixture.json",
                self._safe_payload(schema="aihr_post_decision_validation_matrix_v1"),
            )
            report = build_index(
                artifacts=artifacts + [("post_decision_validation_matrix", matrix)],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_terminal_cycle_contract", codes)

    def test_unlabeled_post_decision_matrix_uses_schema_terminal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            matrix = self._write_json(
                root / "reports" / "aihr_post_decision_validation_matrix_fixture.json",
                self._safe_payload(schema="aihr_post_decision_validation_matrix_v1"),
            )
            report = build_index(
                artifacts=artifacts + [(matrix.stem, matrix)],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        matrix_record = report["artifacts"][-1]
        self.assertFalse(report["ok"])
        self.assertEqual(
            "post_decision_validation_matrix",
            matrix_record["effective_terminal_label"],
        )
        self.assertIn("unsafe_terminal_cycle_contract", codes)

    def test_require_post_decision_gate_fails_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            report = build_index(
                artifacts=artifacts,
                root=root,
                require_post_decision_gate=True,
            )

        self.assertFalse(report["ok"])
        self.assertIn("post_decision_gate_missing", {issue["code"] for issue in report["issues"]})
        self.assertFalse(report["post_decision_gate"]["included"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = self._fixture(root)
            out = root / "reports" / "terminal_index.json"
            md = root / "reports" / "terminal_index.md"
            argv = [
                "--root",
                str(root),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            for label, path in artifacts:
                argv.extend(["--artifact", f"{label}={path.relative_to(root).as_posix()}"])
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = evidence_index_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("reports/terminal_index.md", payload["operator_start"]["validate_first"])
        self.assertIn("AI-HR Terminal Evidence Index", markdown)
        self.assertIn("approval_claim: `False`", markdown)

    def test_stamp_and_extra_artifact_include_default_and_extra_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            safe = self._safe_payload()
            for label, template in [
                ("operator_entrypoint_manifest", "aihr_operator_entrypoint_manifest_demo.json"),
                ("post_handoff_validation", "aihr_post_handoff_validation_demo.json"),
                ("operator_handoff", "overnight_10h_operator_handoff_demo.json"),
                ("operator_next_actions", "aihr_operator_next_actions_demo.json"),
                ("blocker_sprint_queue", "aihr_blocker_reduction_operator_sprint_queue_demo.json"),
                ("release_refresh_dag", "aihr_release_operator_refresh_dag_demo.json"),
                ("release_refresh_dag_audit", "aihr_release_operator_refresh_dag_audit_demo.json"),
                ("acceptance_closure", "aihr_agent_queue_acceptance_closure_demo.json"),
                ("operator_json_powershell_compatibility", "operator_json_powershell_compatibility_audit_demo.json"),
                ("operator_primary_packet_readability", "review_artifact_readability_operator_primary_packet_surface_demo.json"),
                ("operator_entrypoint_readability", "review_artifact_readability_operator_entrypoint_manifest_demo.json"),
                ("post_handoff_readability", "review_artifact_readability_post_handoff_validation_demo.json"),
                ("operator_packet_integrity", "operator_review_packet_integrity_audit_demo.json"),
                ("operator_report_lineage", "operator_report_lineage_sync_audit_demo.json"),
                ("transition_crosswalk_audit", "transition_provenance_operator_crosswalk_audit_demo.json"),
            ]:
                payload = dict(safe)
                if label in {"operator_entrypoint_manifest", "post_handoff_validation"}:
                    payload["terminal_evidence_only"] = True
                    payload["include_in_release_refresh_dag"] = False
                    payload["include_in_operator_handoff"] = False
                self._write_json(reports / template, payload)
            for markdown_name in [
                "aihr_operator_entrypoint_manifest_demo.md",
                "aihr_post_handoff_validation_demo.md",
                "overnight_10h_operator_handoff_demo.md",
            ]:
                self._write_text(reports / markdown_name, "# safe\n\n- approval_claim: `False`\n")
            extra = self._write_json(reports / "extra.json", safe)

            report = build_index(
                artifacts=[("extra", extra.relative_to(root).as_posix())],
                stamp="demo",
                root=root,
            )

        labels = {item["label"] for item in report["artifacts"]}
        self.assertTrue(report["ok"])
        self.assertIn("operator_entrypoint_manifest", labels)
        self.assertIn("extra", labels)
        self.assertEqual(19, report["summary"]["artifact_count"])
        self.assertIn("post_decision_gate_not_in_default", {w["code"] for w in report["warnings"]})


if __name__ == "__main__":
    unittest.main()
