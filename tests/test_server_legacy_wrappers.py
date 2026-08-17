from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.server_legacy_wrappers import (
    build_legacy_operation_handlers,
    build_read_only_legacy_handlers,
    resolve_review_packet_artifact,
    trusted_review_provenance_blockers,
)


@contextmanager
def fake_open_db():
    yield object()


def build_handlers():
    def quality_for(_conn: Any, _target_type: str, _target_id: str | int) -> list[dict[str, Any]]:
        return []

    def tool_response(
        payload: dict[str, Any],
        *,
        data: Any | None = None,
        audit: dict[str, Any] | None = None,
        ok: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True if ok is None else ok,
            "payload": payload,
            "data": data,
            "audit": audit,
        }

    def error_response(code: str, **fields: Any) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, **fields}}

    return build_read_only_legacy_handlers(
        open_db=fake_open_db,
        quality_for=quality_for,
        tool_response=tool_response,
        error_response=error_response,
        now_utc=lambda: "2026-06-23T00:00:00Z",
        db_path_getter=lambda: "data/processed/ncs.db",
    )


def build_operation_handlers():
    def tool_response(
        payload: dict[str, Any],
        *,
        data: Any | None = None,
        audit: dict[str, Any] | None = None,
        ok: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True if ok is None else ok,
            "payload": payload,
            "data": data,
            "audit": audit,
        }

    def error_response(code: str, **fields: Any) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, **fields}}

    return build_legacy_operation_handlers(
        open_db=fake_open_db,
        tool_response=tool_response,
        error_response=error_response,
        now_utc=lambda: "2026-06-23T00:00:00Z",
        db_path_getter=lambda: "data/processed/ncs.db",
    )


class ServerLegacyWrappersTests(unittest.TestCase):
    def test_legacy_review_packet_artifact_requires_repo_local_reports_file(self) -> None:
        reports_root = ROOT / "reports"
        reports_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=reports_root) as repo_packet_tmp:
            repo_packet = Path(repo_packet_tmp) / "legacy_review_packet.md"
            repo_packet.write_text("# legacy review packet\n", encoding="utf-8")
            repo_hash = "sha256:" + hashlib.sha256(repo_packet.read_bytes()).hexdigest()

            self.assertEqual(resolve_review_packet_artifact(str(repo_packet)), repo_packet)
            self.assertEqual(
                trusted_review_provenance_blockers(
                    review_status="human_reviewed",
                    reviewer_id="tester",
                    source_decision_packet=str(repo_packet),
                    source_artifact_hash=repo_hash,
                    rationale="human decision rationale",
                ),
                [],
            )

        with tempfile.TemporaryDirectory() as tmp:
            off_repo_packet = Path(tmp) / "reports" / "legacy_review_packet.md"
            off_repo_packet.parent.mkdir(parents=True, exist_ok=True)
            off_repo_packet.write_text("# off repo packet\n", encoding="utf-8")
            off_repo_hash = "sha256:" + hashlib.sha256(off_repo_packet.read_bytes()).hexdigest()

            self.assertIsNone(resolve_review_packet_artifact(str(off_repo_packet)))
            blockers = trusted_review_provenance_blockers(
                review_status="human_reviewed",
                reviewer_id="tester",
                source_decision_packet=str(off_repo_packet),
                source_artifact_hash=off_repo_hash,
                rationale="human decision rationale",
            )

        self.assertIn(
            "trusted_status_requires_packet_backed_source_decision_packet",
            blockers,
        )

    def test_server_module_has_no_legacy_function_definitions(self) -> None:
        from ncs_mcp import server

        source = inspect.getsource(server)
        self.assertNotIn("def _legacy_", source)

    def test_server_compatibility_aliases_resolve_to_read_only_handlers(self) -> None:
        from ncs_mcp import server

        alias_names = [
            "compare_raw_refined",
            "get_api_join_status",
            "get_sqf_duties",
            "search_sqf_jobs",
            "get_sqf_job_level",
            "analyze_gap",
            "recommend_next_ncs_units",
            "explain_mapping",
            "search_learning_modules",
            "get_learning_module",
            "get_learning_path_for_sqf_job",
            "explain_education_recommendation",
            "get_sqf_ontology_summary",
            "search_sqf_document_chunks",
            "search_sqf_precision_matches",
            "get_sqf_ontology_job_level",
        ]

        for name in alias_names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(server, name),
                    getattr(server.READ_ONLY_LEGACY_HANDLERS, name),
                )

    def test_search_sqf_document_chunks_adds_generated_at_to_audit(self) -> None:
        handlers = build_handlers()
        with patch(
            "ncs_mcp.server_legacy_wrappers.legacy_search_sqf_document_chunks_payload",
            return_value=(
                {"chunks": [{"chunk_id": 1}]},
                {"data_sources": ["sqf_document_chunks"], "returned": 1},
            ),
        ) as search:
            result = handlers.search_sqf_document_chunks("HR", ontology_tag="sqf", limit=3)

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["query"], "HR")
        self.assertEqual(search.call_args.kwargs["ontology_tag"], "sqf")
        self.assertEqual(search.call_args.kwargs["limit"], 3)
        self.assertEqual(result["payload"]["chunks"], [{"chunk_id": 1}])
        self.assertEqual(result["audit"]["generated_at"], "2026-06-23T00:00:00Z")

    def test_get_sqf_ontology_job_level_uses_error_response_for_missing_payload(self) -> None:
        handlers = build_handlers()
        with patch(
            "ncs_mcp.server_legacy_wrappers.legacy_get_sqf_ontology_job_level_payload",
            return_value=None,
        ):
            result = handlers.get_sqf_ontology_job_level("missing-source-key")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "sqf_job_level_not_found")
        self.assertEqual(result["error"]["source_key"], "missing-source-key")

    def test_get_sqf_ontology_summary_reads_configured_db_path(self) -> None:
        handlers = build_handlers()
        with patch(
            "ncs_mcp.server_legacy_wrappers.legacy_get_sqf_ontology_summary_payload",
            return_value={"summary": {"job_levels": 7}},
        ) as summary:
            result = handlers.get_sqf_ontology_summary()

        summary.assert_called_once_with("data/processed/ncs.db")
        self.assertEqual(result["payload"]["summary"]["job_levels"], 7)

    def test_map_sqf_to_ncs_returns_not_found_for_missing_duty(self) -> None:
        handlers = build_operation_handlers()
        with patch("ncs_mcp.server_legacy_wrappers.get_sqf_duty", return_value=None):
            result = handlers.map_sqf_to_ncs("missing-source-key")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "sqf_target_not_found")
        self.assertEqual(result["error"]["source_key"], "missing-source-key")

    def test_collect_job_base_competencies_requires_service_key(self) -> None:
        handlers = build_operation_handlers()
        fake_settings = type("Settings", (), {"job_base_service_key": None, "db_path": "db"})()
        with patch("ncs_mcp.server_legacy_wrappers.load_settings", return_value=fake_settings):
            result = handlers.collect_job_base_competencies()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "job_base_service_key_missing")


if __name__ == "__main__":
    unittest.main()
