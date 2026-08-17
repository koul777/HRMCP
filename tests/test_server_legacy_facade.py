from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Any
import unittest
from unittest.mock import patch

from ncs_mcp import server_legacy_facade


@contextmanager
def fake_open_db():
    yield object()


class ServerLegacyFacadeTests(unittest.TestCase):
    def test_compare_raw_refined_payload_uses_quality_lookup(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY,
                criteria_text_raw TEXT,
                criteria_text_refined TEXT,
                review_status TEXT
            );
            INSERT INTO performance_criteria VALUES (
                7, 'raw text', 'refined text', 'candidate'
            );
            """
        )

        @contextmanager
        def open_compare_db():
            yield conn

        def quality_lookup(quality_conn: Any, target_type: str, target_id: str | int) -> list[dict[str, Any]]:
            self.assertIs(quality_conn, conn)
            self.assertEqual(target_type, "criteria")
            self.assertEqual(target_id, "7")
            return [{"issue_type": "test_issue"}]

        try:
            payload = server_legacy_facade.compare_raw_refined_payload(
                open_compare_db,
                quality_lookup,
                target_type="criteria",
                target_id="7",
            )
        finally:
            conn.close()

        self.assertEqual(payload["comparison"]["raw_text"], "raw text")
        self.assertEqual(payload["comparison"]["refined_text"], "refined text")
        self.assertEqual(payload["quality_issues"], [{"issue_type": "test_issue"}])

    def test_get_api_join_status_payload_filters_and_preserves_shape(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_name TEXT,
                middle_name TEXT,
                small_name TEXT,
                sub_name TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                unit_level_raw TEXT,
                api_unit_name TEXT,
                api_unit_level TEXT,
                api_definition TEXT,
                api_match_status TEXT,
                classification_id INTEGER
            );
            """
        )
        conn.execute("INSERT INTO classifications VALUES (1, 'Business', 'HR', 'Org', 'Planning')")
        conn.execute(
            """
            INSERT INTO competency_units VALUES (
                '0202020101_23v3',
                'HR planning',
                '5',
                'HR planning',
                '5',
                'HR planning definition',
                'matched',
                1
            )
            """
        )

        @contextmanager
        def open_join_db():
            yield conn

        try:
            payload = server_legacy_facade.get_api_join_status_payload(
                open_join_db,
                classification_filter="HR",
                limit=5,
            )
        finally:
            conn.close()

        self.assertEqual(len(payload["api_join_status"]), 1)
        row = payload["api_join_status"][0]
        self.assertEqual(row["unit_code"], "0202020101_23v3")
        self.assertEqual(row["api_match_status"], "matched")
        self.assertEqual(row["sub_name"], "Planning")

    def test_search_learning_modules_payload_wraps_read_only_results(self) -> None:
        modules = [{"learn_module_seq": "LM-1", "learn_module_name": "HR planning"}]
        with patch.object(
            server_legacy_facade,
            "recommendation_search_learning_modules",
            return_value=modules,
        ) as search:
            payload = server_legacy_facade.search_learning_modules_payload(
                fake_open_db,
                query="HR",
                major_code="02",
                limit=3,
            )

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["query"], "HR")
        self.assertEqual(search.call_args.kwargs["major_code"], "02")
        self.assertEqual(search.call_args.kwargs["limit"], 3)
        self.assertEqual(payload["modules"], modules)
        self.assertEqual(payload["audit"]["data_sources"], ["ncs_learning_modules"])
        self.assertEqual(payload["audit"]["returned"], 1)

    def test_get_learning_module_payload_preserves_found_and_missing_shapes(self) -> None:
        found_result: dict[str, Any] = {
            "module": {
                "learn_module_seq": "LM-1",
                "source_payload": {"serviceKey": "secret"},
            },
            "unit_links": [
                {
                    "unit_code": "0202020101_23v3",
                    "raw_payload": {"debug": True},
                }
            ],
        }
        missing_result: dict[str, Any] = {
            "error": "learning_module_not_found",
            "learn_module_seq": "missing",
        }
        with patch.object(
            server_legacy_facade,
            "recommendation_get_learning_module",
            side_effect=[found_result, missing_result],
        ):
            found = server_legacy_facade.get_learning_module_payload(
                fake_open_db,
                learn_module_seq="LM-1",
            )
            missing = server_legacy_facade.get_learning_module_payload(
                fake_open_db,
                learn_module_seq="missing",
            )

        self.assertTrue(found["ok"])
        self.assertEqual(found["module"]["learn_module_seq"], "LM-1")
        found_json = json.dumps(found, ensure_ascii=False)
        self.assertNotIn("source_payload", found_json)
        self.assertNotIn("raw_payload", found_json)
        self.assertNotIn("secret", found_json)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "learning_module_not_found")

    def test_search_sqf_document_chunks_payload_hides_asset_path(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE sqf_document_sources (
                document_id INTEGER PRIMARY KEY,
                title TEXT,
                ontology_role TEXT
            );
            CREATE TABLE sqf_document_assets (
                asset_id INTEGER PRIMARY KEY,
                document_id INTEGER,
                asset_name TEXT,
                asset_path TEXT
            );
            CREATE TABLE sqf_document_chunks (
                chunk_id INTEGER PRIMARY KEY,
                asset_id INTEGER,
                chunk_index INTEGER,
                page_start INTEGER,
                page_end INTEGER,
                char_count INTEGER,
                keywords_json TEXT,
                ontology_tags_json TEXT,
                text TEXT
            );
            INSERT INTO sqf_document_sources VALUES (1, 'SQF report', 'reference');
            INSERT INTO sqf_document_assets VALUES (10, 1, 'asset.pdf', 'C:/secret/raw/asset.pdf');
            INSERT INTO sqf_document_chunks VALUES (
                100, 10, 1, 2, 3, 40, '[]', '["tag"]', 'skill evidence chunk'
            );
            """
        )

        @contextmanager
        def open_sqf_db():
            yield conn

        try:
            payload, audit = server_legacy_facade.search_sqf_document_chunks_payload(
                open_sqf_db,
                query="skill",
                limit=3,
            )
        finally:
            conn.close()

        self.assertEqual(audit["data_sources"], ["sqf_document_chunks", "sqf_document_assets", "sqf_document_sources"])
        self.assertEqual(len(payload["chunks"]), 1)
        chunk = payload["chunks"][0]
        self.assertEqual(chunk["asset_filename"], "asset.pdf")
        self.assertNotIn("asset_path", chunk)
        self.assertNotIn("C:/secret", json.dumps(payload, ensure_ascii=False))

    def test_get_learning_path_for_sqf_job_payload_delegates_to_recommendation_layer(self) -> None:
        result = {"learning_path": [{"stage": 1, "label": "foundation"}]}
        with patch.object(
            server_legacy_facade,
            "recommendation_get_learning_path",
            return_value=result,
        ) as get_path:
            payload = server_legacy_facade.get_learning_path_for_sqf_job_payload(
                fake_open_db,
                query="HR",
                major_code="02",
                target_source_key="SQF-1",
                target_level="5",
                current_concepts=["planning"],
                limit=2,
            )

        get_path.assert_called_once()
        self.assertEqual(get_path.call_args.kwargs["query"], "HR")
        self.assertEqual(get_path.call_args.kwargs["major_code"], "02")
        self.assertEqual(get_path.call_args.kwargs["target_source_key"], "SQF-1")
        self.assertEqual(get_path.call_args.kwargs["target_level"], "5")
        self.assertEqual(get_path.call_args.kwargs["current_concepts"], ["planning"])
        self.assertEqual(get_path.call_args.kwargs["limit"], 2)
        self.assertEqual(payload, result)

    def test_recommend_learning_modules_by_ncs_payload_forces_save_false(self) -> None:
        result = {
            "ok": True,
            "recommendations": [{"learn_module_seq": "LM-1"}],
        }
        with patch.object(
            server_legacy_facade,
            "ncs_reference_recommend_learning_modules",
            return_value=result,
        ) as recommend:
            payload = server_legacy_facade.recommend_learning_modules_by_ncs_payload(
                fake_open_db,
                query="HR",
                major_code="02",
                limit=2,
                save=True,
            )

        recommend.assert_called_once()
        self.assertEqual(recommend.call_args.kwargs["query"], "HR")
        self.assertEqual(recommend.call_args.kwargs["major_code"], "02")
        self.assertEqual(recommend.call_args.kwargs["limit"], 2)
        self.assertFalse(recommend.call_args.kwargs["save"])
        self.assertTrue(payload["audit"]["save_requested_ignored"])
        self.assertEqual(
            payload["audit"]["save_policy"],
            "legacy_mcp_wrapper_forces_save_false",
        )

    def test_search_ncs_reference_chunks_payload_preserves_shape(self) -> None:
        chunks = [
            {
                "chunk_id": 1,
                "document_id": 2,
                "match_summary": "HR planning",
            }
        ]
        with patch.object(
            server_legacy_facade,
            "ncs_reference_search_chunks",
            return_value=chunks,
        ) as search:
            payload, audit = server_legacy_facade.search_ncs_reference_chunks_payload(
                fake_open_db,
                query="HR",
                document_id=2,
                limit=4,
            )

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["query"], "HR")
        self.assertEqual(search.call_args.kwargs["document_id"], 2)
        self.assertEqual(search.call_args.kwargs["limit"], 4)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["chunks"], chunks)
        self.assertEqual(audit["data_sources"], ["ncs_reference_chunks"])
        self.assertEqual(audit["returned"], 1)
        self.assertIn("generated_at", audit)

    def test_explain_education_recommendation_payload_delegates_to_recommendation_layer(self) -> None:
        result = {"recommendation": {"rank": 1}, "evidence": []}
        with patch.object(
            server_legacy_facade,
            "recommendation_explain_education",
            return_value=result,
        ) as explain:
            payload = server_legacy_facade.explain_education_recommendation_payload(
                fake_open_db,
                recommendation_item_id=7,
                recommendation_run_id=3,
                rank=1,
            )

        explain.assert_called_once()
        self.assertEqual(explain.call_args.kwargs["recommendation_item_id"], 7)
        self.assertEqual(explain.call_args.kwargs["recommendation_run_id"], 3)
        self.assertEqual(explain.call_args.kwargs["rank"], 1)
        self.assertEqual(payload, result)


if __name__ == "__main__":
    unittest.main()
