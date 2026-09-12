from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.search import core as search_core  # noqa: E402
from ncs_mcp.search.normalization import (  # noqa: E402
    SEARCH_NORMALIZATION_FIELDS,
    SEARCH_NORMALIZATION_REQUIRED_MANIFEST,
    SEARCH_NORMALIZATION_V2_FIELDS,
    SEARCH_NORMALIZATION_V2_OVERRIDES,
    SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST,
    normalize_search_text,
)


class NcsSearchRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "search.db"
        self.sql_statements: list[str] = []
        conn = self._connect()
        try:
            self._create_schema(conn)
            self._seed(conn)
            conn.commit()
        finally:
            conn.close()
        self.open_db_patch = patch.object(server, "open_db", new=self._open_db)
        self.open_db_patch.start()

    def tearDown(self) -> None:
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    def _add_normalized_columns(self, conn: sqlite3.Connection) -> None:
        for table, fields in SEARCH_NORMALIZATION_FIELDS.items():
            for raw, derived in fields.items():
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {derived} TEXT")
                expression = (
                    "COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, '')"
                    if table == "ncs_query_aliases" else raw
                )
                rows = conn.execute(f"SELECT rowid, {expression} FROM {table}").fetchall()
                conn.executemany(
                    f"UPDATE {table} SET {derived} = ? WHERE rowid = ?",
                    ((normalize_search_text(row[1]), row[0]) for row in rows),
                )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS serving_snapshot_manifest ("
            "manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO serving_snapshot_manifest "
            "(manifest_key, manifest_value) VALUES (?, ?)",
            SEARCH_NORMALIZATION_REQUIRED_MANIFEST.items(),
        )

    def test_shared_normalization_handles_unicode_and_preserves_raw_values(self) -> None:
        cases = (
            (None, ""), (0, "0"),
            (" ＡＬＰＨＡ\u3000Straße ", "alpha strasse"),
            ("cafe\u0301—e\u0301cole", "café école"),
            ("\u1100\u1161\u1102\u1161·출입", "가나 출입"),
            ("alpha_beta", "alpha beta"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_search_text(raw), expected)

    def test_normalized_search_recovers_unicode_in_all_text_scopes(self) -> None:
        cases = (
            ("ＡＬＰＨＡ ＢＥＴＡ", "alpha beta"),
            ("Straße Straße", "strasse"),
            ("cafe\u0301 e\u0301cole", "café école"),
            ("\u1100\u1161\u1102\u1161 \u1103\u1161\u1105\u1161", "가나 다라"),
            ("delta—epsilon", "delta epsilon"),
            ("theta_iota", "theta iota"),
        )
        with self._open_db() as conn:
            for index, (raw, _) in enumerate(cases, 100):
                code = f"N{index}"
                conn.execute("INSERT INTO competency_units VALUES (?, ?, '', '4', 1)", (code, raw))
                conn.execute("INSERT INTO competency_elements VALUES (?, ?, ?)", (index, raw, code))
                criteria_raw = raw if index % 2 == 0 else "original criteria evidence"
                ksa_raw = raw if index % 2 else "original KSA evidence"
                conn.execute("INSERT INTO performance_criteria VALUES (?, ?, ?, ?)", (index, criteria_raw, raw, index))
                conn.execute("INSERT INTO ksa_items VALUES (?, 'knowledge', ?, ?, ?)", (index, ksa_raw, raw, index))
            self._add_normalized_columns(conn)
            conn.commit()
        self.sql_statements.clear()
        for index, (raw, query) in enumerate(cases, 100):
            for scope in ("unit", "element", "criteria", "ksa"):
                with self.subTest(raw=raw, scope=scope):
                    result = server.search_ncs(query, scope=scope, limit=5)
                    self.assertEqual(result["match_mode"], "phrase")
                    self.assertEqual(result["results"][0]["id"], f"N{index}" if scope == "unit" else index)
                    expected_raw = (
                        "original criteria evidence" if scope == "criteria" and index % 2
                        else "original KSA evidence" if scope == "ksa" and not index % 2
                        else raw
                    )
                    self.assertEqual(result["results"][0]["text"], expected_raw)
                    self.assertTrue(result["results"][0]["match_fields"])
        self.assertTrue(any("ncs_search_match_normalized" in sql for sql in self.sql_statements))

    def test_normalized_exact_unit_name_outranks_definition_only_match(self) -> None:
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', 1)",
                ("U_NORMALIZED_EXACT", "ＡＬＰＨＡ", "unrelated definition"),
            )
            conn.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', 1)",
                ("U_NORMALIZED_DEFINITION", "short", "alpha"),
            )
            self._add_normalized_columns(conn)
            conn.commit()

        result = server.search_ncs("alpha", scope="unit", limit=5)

        self.assertEqual(result["results"][0]["id"], "U_NORMALIZED_EXACT")
        self.assertEqual(result["results"][0]["text"], "ＡＬＰＨＡ")

    def test_normalized_task_ksa_prefilter_and_boundary(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO performance_criteria VALUES (90, ?, NULL, 5)", ("ＡＬＰＨＡ Straße",))
            conn.execute("INSERT INTO ksa_items VALUES (90, 'knowledge', ?, NULL, 6)", ("xＡＬＰＨＡ Straße",))
            self._add_normalized_columns(conn)
            scores = search_core._ncs_search_unit_task_ksa_scores(
                conn, ["U_TASK_SIGNAL", "U_NAME_ONLY"], ["alpha", "strasse"]
            )
        self.assertGreater(scores["U_TASK_SIGNAL"], 0)
        self.assertEqual(scores["U_NAME_ONLY"], 0)

    def test_legacy_task_ksa_evidence_keeps_unicode_recall_in_all_four_fields(self) -> None:
        cases = (
            ("ＡＬＰＨＡ ＢＥＴＡ", ["alpha", "beta"]),
            ("Straße Schloss", ["strasse", "schloss"]),
            ("cafe\u0301 e\u0301cole", ["café", "école"]),
            ("\u1100\u1161\u1102\u1161 \u1103\u1161\u1105\u1161", ["가나", "다라"]),
            ("delta—epsilon", ["delta", "epsilon"]),
        )
        with self._open_db() as conn:
            for table, fields in (
                ("performance_criteria", ("criteria_text_raw", "criteria_text_refined")),
                ("ksa_items", ("ksa_text_raw", "ksa_text_refined")),
            ):
                for field in fields:
                    for raw, tokens in cases:
                        with self.subTest(table=table, field=field, raw=raw):
                            conn.execute("SAVEPOINT unicode_case")
                            conn.execute(
                                f"INSERT INTO {table} (element_id, {field}) VALUES (5, ?)",
                                (raw,),
                            )
                            scores = search_core._ncs_search_unit_task_ksa_scores(
                                conn, ["U_TASK_SIGNAL"], tokens
                            )
                            self.assertGreater(scores["U_TASK_SIGNAL"], 0)
                            conn.execute("ROLLBACK TO unicode_case")
                            conn.execute("RELEASE unicode_case")

    def test_normalized_alias_and_classification_fields_keep_raw_metadata(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO ncs_query_aliases VALUES ('U_EXACT', ?, '')", ("ＡＬＩＡＳ Straße",))
            conn.execute("UPDATE classifications SET major_name = ? WHERE classification_id = 1", ("ＣＬＡＳＳ Straße",))
            self._add_normalized_columns(conn)
            conn.commit()
        alias = server.search_ncs("alias strasse", scope="unit", limit=5)
        self.assertEqual(alias["results"][0]["id"], "U_EXACT")
        self.assertEqual(alias["results"][0]["match_fields"], ["alias"])
        scoped = server.search_ncs(
            "데이터분석", scope="unit", limit=5,
            classification_filter={"major_name": "class strasse", "major_code": "02"},
        )
        self.assertEqual(scoped["results"][0]["id"], "U_EXACT")
        self.assertEqual(scoped["results"][0]["path"]["major"], "ＣＬＡＳＳ Straße")

    def test_partial_normalized_schema_uses_entire_legacy_path(self) -> None:
        before = server.search_ncs("데이터분석", scope="all", limit=15)
        with self._open_db() as conn:
            conn.execute("ALTER TABLE competency_units ADD COLUMN unit_name_search_norm TEXT")
            conn.execute("UPDATE competency_units SET unit_name_search_norm = 'wrong derived value'")
            conn.commit()
            self.assertFalse(search_core._has_normalized_search_columns(conn))
        self.sql_statements.clear()
        after = server.search_ncs("데이터분석", scope="all", limit=15)
        self.assertEqual(before, after)
        self.assertFalse(any("ncs_search_match_normalized" in sql for sql in self.sql_statements))

    def test_unattested_normalized_schema_uses_legacy_path(self) -> None:
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.execute(
                "DELETE FROM serving_snapshot_manifest "
                "WHERE manifest_key = 'search_normalization_source'"
            )
            conn.commit()
            self.assertFalse(search_core._has_normalized_search_columns(conn))
        self.sql_statements.clear()
        result = server.search_ncs("데이터분석", scope="all", limit=15)
        self.assertTrue(result["results"])
        self.assertFalse(any("ncs_search_match_normalized" in sql for sql in self.sql_statements))

    def test_normalized_snapshot_preserves_legacy_codes_and_order(self) -> None:
        queries = ("데이터분석", "U_EXACT", "0202010220_25v3", "project rare roadmap")
        before = [server.search_ncs(query, scope="all", limit=15) for query in queries]
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        after = [server.search_ncs(query, scope="all", limit=15) for query in queries]
        self.assertEqual(before, after)

    def test_boundary_match_rejects_internal_compound_but_keeps_prefix(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            search_core._register_ncs_search_udfs(conn)
            rows = conn.execute(
                """
                SELECT
                    ncs_search_match('alphabet soup', 'beta'),
                    ncs_search_match('beta testing', 'beta'),
                    ncs_search_match('alpha beta', 'beta'),
                    ncs_search_match('alpha-beta', 'beta'),
                    ncs_search_match('수출입계약', '출입'),
                    ncs_search_match('출입 통제와 보안 점검', '출입')
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(rows), (0, 1, 1, 1, 0, 1))

    def test_classification_filter_is_applied_to_structure_search(self) -> None:
        unfiltered = server.search_ncs(
            "data workflow analysis",
            scope="unit",
            limit=5,
        )
        filtered_out = server.search_ncs(
            "data workflow analysis",
            scope="unit",
            limit=5,
            classification_filter={"major_code": "99"},
        )

        self.assertEqual(unfiltered["results"][0]["id"], "U_ASCII")
        self.assertEqual(filtered_out["results"], [])
        self.assertEqual(filtered_out["classification_filter"], {"major_code": "99"})
        self.assertTrue(filtered_out["classification_filter_applied"])

    def _seed_context_shadow_candidates(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        70,
                        "91",
                        "Corporate Services",
                        "01",
                        "Administration",
                        "01",
                        "Operations",
                        "01",
                        "Administration Support",
                        "1",
                    ),
                    (
                        71,
                        "92",
                        "Creative Services",
                        "01",
                        "Events",
                        "01",
                        "Production",
                        "01",
                        "Event Production",
                        "1",
                    ),
                ),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                (
                    ("A_CONTEXT_OTHER", "shared planning", "event delivery", "4", 71),
                    ("Z_CONTEXT_ADMIN", "shared planning", "office support", "4", 70),
                ),
            )
            conn.commit()

    def test_context_shadow_preserves_public_order_and_redacts_raw_text(self) -> None:
        self._seed_context_shadow_candidates()
        baseline = server.search_ncs("shared planning", scope="unit", limit=10)
        blank_context = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            context_text=" \t ",
            job_scope="   ",
        )
        contextual = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            context_text="administration support setting",
            job_scope="Administration Support",
        )

        baseline_ids = [item["id"] for item in baseline["results"]]
        self.assertEqual(blank_context, baseline)
        self.assertEqual(
            [item["id"] for item in contextual["results"]],
            baseline_ids,
        )
        self.assertEqual(baseline_ids[:2], ["A_CONTEXT_OTHER", "Z_CONTEXT_ADMIN"])
        context = contextual["search_context"]
        self.assertEqual(context["schema"], "ncs_search_context_v1")
        self.assertEqual(context["status"], "resolved")
        self.assertEqual(context["selected_candidate"]["major_code"], "91")
        self.assertFalse(context["prior_applied"])
        self.assertTrue(context["shadow_ranking_computed"])
        rows = {item["id"]: item for item in contextual["results"]}
        self.assertEqual(rows["Z_CONTEXT_ADMIN"]["shadow_rank"], 1)
        self.assertEqual(rows["A_CONTEXT_OTHER"]["shadow_rank"], 2)
        self.assertEqual(rows["Z_CONTEXT_ADMIN"]["context_affinity"], 1.0)
        self.assertNotIn("_classification_codes", rows["Z_CONTEXT_ADMIN"])
        self.assertNotIn(
            "context_token:",
            json.dumps(context, ensure_ascii=False),
        )
        serialized = json.dumps(contextual, ensure_ascii=False)
        self.assertNotIn("administration support setting", serialized.casefold())

    def test_context_conflict_keeps_exact_hard_filter_path(self) -> None:
        self._seed_context_shadow_candidates()
        baseline = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "92"},
        )
        contextual = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "92"},
            job_scope="Administration Support",
        )

        self.assertEqual(
            [item["id"] for item in contextual["results"]],
            [item["id"] for item in baseline["results"]],
        )
        self.assertEqual([item["id"] for item in contextual["results"]], ["A_CONTEXT_OTHER"])
        self.assertTrue(contextual["classification_filter_applied"])
        self.assertEqual(contextual["search_context"]["status"], "conflict")
        self.assertFalse(contextual["search_context"]["prior_applied"])
        self.assertIn(
            "context_conflicts_with_hard_filter",
            contextual["search_context"]["warnings"],
        )

    def test_exact_job_scope_is_not_demoted_by_broader_context_match(self) -> None:
        self._seed_context_shadow_candidates()
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    72,
                    "93",
                    "Shared Services",
                    "01",
                    "Administration Support Division",
                    "01",
                    "Adjacent Operations",
                    "01",
                    "Other Function",
                    "1",
                ),
            )
            conn.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                ("Y_CONTEXT_BROAD", "purple comet", "", "4", 72),
            )
            conn.commit()

        contextual = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            context_text="purple comet",
            job_scope="Administration Support",
        )

        context = contextual["search_context"]
        self.assertEqual(context["status"], "resolved")
        self.assertEqual(context["selected_candidate"]["major_code"], "91")
        self.assertEqual(
            context["selected_candidate"]["match_basis"],
            ["job_scope_exact_sub_name"],
        )

    def test_unresolved_context_is_advisory_and_does_not_reorder(self) -> None:
        self._seed_context_shadow_candidates()
        baseline = server.search_ncs("shared planning", scope="unit", limit=10)
        contextual = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            job_scope="Unknown Synthetic Function",
            classification_filter={"unsupported_key": "ignored"},
        )

        self.assertEqual(
            [item["id"] for item in contextual["results"]],
            [item["id"] for item in baseline["results"]],
        )
        context = contextual["search_context"]
        self.assertEqual(context["status"], "unresolved")
        self.assertTrue(context["needs_context"])
        self.assertFalse(context["prior_applied"])
        self.assertIn(
            "ignored_classification_filter_key:unsupported_key",
            context["warnings"],
        )

    def test_context_resolver_exposes_not_provided_filtered_and_ambiguous_states(self) -> None:
        self._seed_context_shadow_candidates()
        with self._open_db() as conn:
            not_provided = search_core.resolve_ncs_search_context(conn)
            filtered = search_core.resolve_ncs_search_context(
                conn,
                classification_filter={"major_code": "91"},
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        73,
                        "94",
                        "First Domain",
                        "01",
                        "First Group",
                        "01",
                        "First Area",
                        "01",
                        "Duplicated Function",
                        "1",
                    ),
                    (
                        74,
                        "95",
                        "Second Domain",
                        "01",
                        "Second Group",
                        "01",
                        "Second Area",
                        "01",
                        "Duplicated Function",
                        "1",
                    ),
                ),
            )
            ambiguous = search_core.resolve_ncs_search_context(
                conn,
                job_scope="Duplicated Function",
            )

        self.assertEqual(not_provided["status"], "not_provided")
        self.assertEqual(filtered["status"], "filtered")
        self.assertTrue(filtered["hard_filter_applied"])
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertIsNone(ambiguous["selected_candidate"])
        self.assertGreaterEqual(ambiguous["alternative_count"], 2)
        self.assertFalse(ambiguous["prior_applied"])

    def test_context_route_v2_binds_meta_execution_and_rejects_tampering(self) -> None:
        self._seed_context_shadow_candidates()
        route_query = "shared planning NCS search"
        discovery = server.ncs_discover_tools(
            route_query,
            context_text="administration support setting",
            job_scope="Administration Support",
        )
        route = discovery["query_route"]
        self.assertEqual(route["tool"], "ncs_search")
        self.assertEqual(
            route["route_contract"]["fingerprint_version"],
            "route-fingerprint-v2",
        )
        self.assertNotIn(
            "administration support setting",
            json.dumps(discovery, ensure_ascii=False).casefold(),
        )
        params = {
            "query": "shared planning",
            "scope": "unit",
            "context_text": "administration support setting",
            "job_scope": "Administration Support",
            "_route_query": route_query,
            "_route_fingerprint": route["route_fingerprint"],
        }
        executed = server.ncs_execute_tool("ncs_search", params)
        self.assertTrue(executed["ok"])
        self.assertTrue(
            executed["meta_execution"]["search_context_binding_verified"]
        )
        self.assertEqual(
            executed["meta_execution"]["search_context_binding_schema"],
            "ncs_search_context_binding_v1",
        )
        self.assertEqual(
            executed["meta_execution"]["search_context_hash"],
            route["route_contract"]["search_context_hash"],
        )
        self.assertEqual(
            [item["id"] for item in executed["results"][:2]],
            ["A_CONTEXT_OTHER", "Z_CONTEXT_ADMIN"],
        )
        missing = dict(params)
        missing.pop("_route_fingerprint")
        missing_result = server.ncs_execute_tool("ncs_search", missing)
        self.assertEqual(
            missing_result["error"]["code"],
            "context_route_binding_required",
        )
        changed = dict(params)
        changed["job_scope"] = "Event Production"
        changed_result = server.ncs_execute_tool("ncs_search", changed)
        self.assertEqual(
            changed_result["error"]["code"],
            "route_fingerprint_mismatch",
        )

    def test_context_meta_execution_fails_closed_on_post_validation_db_change(self) -> None:
        self._seed_context_shadow_candidates()
        route_query = "shared planning NCS search"
        raw_context = "administration support setting"
        discovery = server.ncs_discover_tools(
            route_query,
            context_text=raw_context,
            job_scope="Administration Support",
        )
        route = discovery["query_route"]
        original_handler = server.NCS_EXECUTABLE_TOOL_HANDLERS["ncs_search"]

        def mutate_then_search(**kwargs):
            with self._open_db() as conn:
                conn.execute(
                    """
                    UPDATE classifications
                    SET major_name = 'Changed Domain',
                        middle_name = 'Changed Group',
                        small_name = 'Changed Area',
                        sub_name = 'Changed Function'
                    WHERE classification_id = 70
                    """
                )
                conn.commit()
            return original_handler(**kwargs)

        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": mutate_then_search},
        ):
            result = server.ncs_execute_tool(
                "ncs_search",
                {
                    "query": "shared planning",
                    "scope": "unit",
                    "context_text": raw_context,
                    "job_scope": "Administration Support",
                    "_route_query": route_query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["code"],
            "context_route_resolution_mismatch",
        )
        self.assertEqual(result["error"]["expected_search_context_status"], "resolved")
        self.assertEqual(result["error"]["actual_search_context_status"], "unresolved")
        self.assertNotEqual(
            result["error"]["expected_search_context_hash"],
            result["error"]["actual_search_context_hash"],
        )
        self.assertFalse(
            result["meta_execution"]["search_context_binding_verified"]
        )
        self.assertNotIn(raw_context, json.dumps(result, ensure_ascii=False))

    def test_no_context_meta_execution_keeps_v1_without_final_binding_fields(self) -> None:
        self._seed_context_shadow_candidates()
        route_query = "shared planning NCS search"
        route = server.ncs_discover_tools(route_query)["query_route"]
        result = server.ncs_execute_tool(
            "ncs_search",
            {
                "query": "shared planning",
                "scope": "unit",
                "_route_query": route_query,
                "_route_fingerprint": route["route_fingerprint"],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            route["route_contract"]["fingerprint_version"],
            "route-fingerprint-v1",
        )
        self.assertNotIn(
            "search_context_binding_verified",
            result["meta_execution"],
        )
        self.assertEqual(
            [item["id"] for item in result["results"][:2]],
            ["A_CONTEXT_OTHER", "Z_CONTEXT_ADMIN"],
        )

    def test_ignored_filter_keys_do_not_change_context_route_fingerprint(self) -> None:
        self._seed_context_shadow_candidates()
        route_query = "shared planning NCS search"
        clean = server.ncs_discover_tools(
            route_query,
            context_text="administration support setting",
            job_scope="Administration Support",
        )["query_route"]
        ignored = server.ncs_discover_tools(
            route_query,
            classification_filter={"unsupported_key": "ignored"},
            context_text="administration support setting",
            job_scope="Administration Support",
        )["query_route"]

        self.assertEqual(clean["route_fingerprint"], ignored["route_fingerprint"])
        self.assertIn(
            "ignored_classification_filter_key:unsupported_key",
            ignored["search_context"]["warnings"],
        )

    def test_ignored_filter_warning_preview_is_bounded_under_key_flood(self) -> None:
        self._seed_context_shadow_candidates()
        route_query = "shared planning NCS search"
        ignored_filter = {
            f"unknown_{index:04d}_{'x' * 256}": "ignored"
            for index in range(600)
        }
        baseline = server.search_ncs("shared planning", scope="unit", limit=10)
        attacked = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter=ignored_filter,
        )
        baseline_route = server.route_ncs_query(route_query)
        attacked_route = server.route_ncs_query(
            route_query,
            classification_filter=ignored_filter,
        )

        self.assertEqual(
            [item["id"] for item in attacked["results"]],
            [item["id"] for item in baseline["results"]],
        )
        self.assertFalse(attacked["classification_filter_applied"])
        self.assertEqual(
            attacked_route["route_fingerprint"],
            baseline_route["route_fingerprint"],
        )
        warnings = attacked["search_context"]["warnings"]
        preview = warnings[:-1]
        self.assertEqual(len(preview), 8)
        self.assertEqual(
            warnings[-1],
            "ignored_classification_filter_keys_omitted:592",
        )
        self.assertEqual(preview, sorted(preview))
        prefix = "ignored_classification_filter_key:"
        self.assertTrue(all(item.startswith(prefix) for item in preview))
        self.assertTrue(all(len(item.removeprefix(prefix)) <= 64 for item in preview))
        self.assertLessEqual(len(json.dumps(attacked, ensure_ascii=False)), 8_000)

        hard_only = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "91"},
        )
        hard_and_ignored = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "91", **ignored_filter},
        )
        self.assertEqual(
            [item["id"] for item in hard_and_ignored["results"]],
            [item["id"] for item in hard_only["results"]],
        )
        self.assertTrue(hard_and_ignored["classification_filter_applied"])
        self.assertEqual(
            server.route_ncs_query(
                route_query,
                classification_filter={"major_code": "91", **ignored_filter},
            )["route_fingerprint"],
            server.route_ncs_query(
                route_query,
                classification_filter={"major_code": "91"},
            )["route_fingerprint"],
        )

    def test_small_ignored_filter_warning_list_remains_exact_and_sorted(self) -> None:
        self._seed_context_shadow_candidates()
        result = server.search_ncs(
            "shared planning",
            scope="unit",
            limit=10,
            classification_filter={"zeta_unknown": 1, "alpha_unknown": 2},
        )

        self.assertEqual(
            result["search_context"]["warnings"],
            [
                "ignored_classification_filter_key:alpha_unknown",
                "ignored_classification_filter_key:zeta_unknown",
            ],
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _open_db(self):
        conn = self._connect()
        conn.set_trace_callback(self.sql_statements.append)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT, major_name TEXT,
                middle_code TEXT, middle_name TEXT,
                small_code TEXT, small_name TEXT,
                sub_code TEXT, sub_name TEXT,
                duty_order TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                api_definition TEXT,
                unit_level_raw TEXT,
                classification_id INTEGER
            );
            CREATE TABLE ncs_query_aliases (
                unit_code TEXT,
                alias_text TEXT,
                normalized_query TEXT
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY,
                element_name_raw TEXT,
                unit_code TEXT
            );
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY,
                criteria_text_raw TEXT,
                criteria_text_refined TEXT,
                element_id INTEGER
            );
            CREATE TABLE ksa_items (
                ksa_id INTEGER PRIMARY KEY,
                ksa_type_name TEXT,
                ksa_text_raw TEXT,
                ksa_text_refined TEXT,
                element_id INTEGER
            );
            """
        )

    @staticmethod
    def _seed(conn: sqlite3.Connection) -> None:
        conn.executemany(
            """
            INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (1, "02", "경영", "02", "인사", "02", "인사관리", "01", "인사조직", "1"),
                (2, "99", "기타", "99", "기타", "99", "기타", "99", "데이터분석 직무", "2"),
                (3, "12", "이용·숙박·여행·오락·스포츠", "04", "스포츠", "04", "스포츠산업", "03", "스포츠구단", "3"),
                (4, "02", "경영·회계·사무", "02", "총무·인사", "01", "총무", "02", "시설총무", "4"),
                (5, "07", "사회복지·종교", "01", "사회복지", "02", "사회복지서비스", "05", "자원봉사관리", "5"),
                (6, "15", "기계", "01", "금속가공", "01", "표면가공", "01", "금속표면가공", "6"),
            ),
        )
        units = (
            ("U_EXACT", "데이터분석", "직무 정의", "5", 1),
            ("U_PREFIX", "데이터분석 실무", "직무 정의", "5", 1),
            ("U_PARTIAL", "인사 데이터분석", "직무 정의", "5", 1),
            ("U_CLASS", "직무분류 검색", "직무 정의", "5", 2),
            ("U_DEFINITION", "정의 검색", "데이터분석 업무를 수행한다", "5", 1),
            ("U_HIRE_1", "채용 운영", "신입사원 선발", "4", 1),
            ("U_HIRE_2", "급여 운영", "급여 업무", "4", 1),
            ("U_ASSET", "자산관리", "자산 취득과 처분을 관리한다", "4", 1),
            ("U_QUALITY", "품질관리", "품질 기준을 관리한다", "4", 1),
            ("U_PERFORMANCE", "성과관리", "성과 목표를 관리한다", "4", 1),
            ("U_HR_MANAGEMENT", "인사관리", "인사 운영을 관리한다", "4", 1),
            ("U_LABOR", "노무관리", "노무 업무를 관리한다", "4", 1),
            ("U_ASCII", "data workflow analysis", "data operations", "4", 1),
            ("U_WAGE", "임금관리", "임금 정책과 보상 기준을 관리한다", "5", 1),
            ("U_SUPPLIES", "비품관리", "사무 비품을 구매하고 관리한다", "4", 1),
            ("U_SECURITY", "총무보안관리", "사옥 출입과 보안을 관리한다", "4", 1),
            ("U_VAT", "부가가치세 신고", "부가가치세 신고 업무를 수행한다", "4", 1),
            ("U_PLAYER", "선수연봉계약", "프로야구 선수의 연봉 협상을 수행한다", "4", 3),
            # Same base code 02020102 20 shared by two 세분류: NCS lets a 세분류
            # borrow a unit developed elsewhere. The borrowed copy keeps the
            # older version tag, so a plain unit_code sort puts it first.
            ("0202010220_19v2", "공용시설관리", "공용 시설을 관리한다", "4", 5),
            ("0202010220_25v3", "공용시설관리", "공용 시설을 관리한다", "4", 4),
            # 처리 spreads across the corpus while 퇴직정산 names one unit, so a
            # lone 퇴직정산 hit has to outrank a lone 처리 hit even though the
            # heat-treatment names are shorter.
            ("U_HEAT_1", "심냉처리", "금속을 냉각한다", "3", 6),
            ("U_HEAT_2", "퀜칭열처리", "금속을 열처리한다", "3", 6),
            ("U_HEAT_3", "진공열처리", "진공에서 열처리한다", "3", 6),
            ("U_SEVERANCE", "퇴직정산지원", "퇴직 정산 업무를 지원한다", "4", 1),
            # The expected unit has concrete task terms in its definition,
            # while the competing name shares more query words. Definition
            # weighting should make the evidence-rich unit win.
            ("U_DEFINITION_RICH", "핵심인재관리", "인재를 선발하고 육성하는 기준을 운영한다", "5", 1),
            ("U_NAME_HEAVY", "인재 선발 전략", "인재 관련 교육을 지원한다", "5", 1),
            ("U_TASK_SIGNAL", "project planning", "", "5", 1),
            ("U_NAME_ONLY", "project management", "", "5", 1),
        )
        conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)", units)
        conn.execute(
            "INSERT INTO ncs_query_aliases VALUES ('U_HIRE_1', 'recruiting', 'recruiting')"
        )
        conn.execute(
            "INSERT INTO ncs_query_aliases VALUES ('U_HIRE_1', '채용', '인력채용')"
        )
        elements = (
            (1, "채용 운영", "U_HIRE_1"),
            (2, "급여 운영", "U_HIRE_2"),
            (3, "분석 방법", "U_EXACT"),
            (4, "data workflow analysis", "U_ASCII"),
            (5, "project planning", "U_TASK_SIGNAL"),
            (6, "project management", "U_NAME_ONLY"),
        )
        conn.executemany("INSERT INTO competency_elements VALUES (?, ?, ?)", elements)
        criteria = (
            (1, "신입사원 면접 절차와 채용 기준을 적용한다", None, 1),
            (2, "채용 운영 계획을 검토한다", None, 1),
            (3, "급여 운영 계획을 검토한다", None, 2),
            (4, "data workflow analysis", None, 4),
            (5, "project planning uses a rare deliverable roadmap", None, 5),
            (6, "common project administration", None, 6),
        )
        conn.executemany("INSERT INTO performance_criteria VALUES (?, ?, ?, ?)", criteria)
        ksa = (
            (1, "knowledge", "채용 운영 절차 지식", None, 1),
            (2, "skill", "급여 운영 도구 활용 기술", None, 2),
            (3, "skill", "데이터 품질 점검 기술", None, 3),
            (4, "skill", "data workflow analysis", None, 4),
            (5, "skill", "rare deliverable roadmap", None, 5),
            (6, "skill", "common project administration", None, 6),
        )
        conn.executemany("INSERT INTO ksa_items VALUES (?, ?, ?, ?, ?)", ksa)

    def test_exact_prefix_phrase_unit_ranking_is_preserved(self) -> None:
        result = server.search_ncs("데이터분석", scope="unit", limit=5)

        self.assertEqual(
            [row["id"] for row in result["results"]],
            ["U_EXACT", "U_PREFIX", "U_PARTIAL", "U_CLASS", "U_DEFINITION"],
        )
        self.assertEqual(result["match_mode"], "phrase")

    def test_ksa_scope_is_public_and_returns_only_ksa(self) -> None:
        result = server.ncs_search("채용", scope="ksa", limit=5)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scope"], "ksa")
        self.assertTrue(result["results"])
        self.assertEqual({row["type"] for row in result["results"]}, {"ksa"})

    def test_multiword_query_uses_and_then_or_fallback(self) -> None:
        token_and = server.search_ncs("신입사원 채용 면접", scope="all", limit=10)
        token_or = server.search_ncs("데이터 분석가", scope="all", limit=10)

        self.assertEqual(token_and["match_mode_by_type"]["criteria"], "token_and")
        self.assertIn(1, [row["id"] for row in token_and["results"] if row["type"] == "criteria"])
        self.assertIn(token_or["match_mode"], {"token_or", "mixed"})
        self.assertGreater(token_or["returned"], 0)

    def test_alias_validated_compound_expansion_preserves_and_semantics(self) -> None:
        result = server.search_ncs("인사 채용관리", scope="unit", limit=5)

        self.assertEqual(result["match_mode"], "expanded_token_and")
        self.assertEqual(result["results"][0]["id"], "U_HIRE_1")
        self.assertEqual(
            result["query_expansions"]["채용관리"],
            ["채용", "인력채용"],
        )
        self.assertEqual(result["results"][0]["matched_tokens"], ["인사", "채용관리"])
        self.assertEqual(
            result["results"][0]["matched_expansions"][0]["matched_as"],
            "채용",
        )

    def test_exact_management_terms_outrank_compound_expansion(self) -> None:
        expectations = {
            "자산관리": "U_ASSET",
            "품질관리": "U_QUALITY",
            "성과관리": "U_PERFORMANCE",
            "인사관리": "U_HR_MANAGEMENT",
            "노무관리": "U_LABOR",
        }

        for query, expected_id in expectations.items():
            with self.subTest(query=query):
                result = server.search_ncs(query, scope="unit", limit=5)
                self.assertEqual(result["match_mode"], "phrase")
                self.assertEqual(result["results"][0]["id"], expected_id)
                self.assertEqual(result["query_expansions"], {})

    def test_high_specificity_practitioner_aliases_retrieve_official_units(self) -> None:
        expectations = {
            "연봉 협상 기준": ("U_WAGE", "임금관리"),
            "사무용품 구매 요청": ("U_SUPPLIES", "비품관리"),
            "사옥 보안 점검": ("U_SECURITY", "총무보안관리"),
            "부가세 신고 준비": ("U_VAT", "부가가치세 신고"),
        }

        for query, (expected_id, expected_expansion) in expectations.items():
            with self.subTest(query=query):
                result = server.search_ncs(query, scope="unit", limit=3)
                self.assertEqual(result["match_mode"], "intent_alias")
                self.assertEqual(result["results"][0]["id"], expected_id)
                self.assertIn(expected_expansion, result["query_intent_expansions"])
                self.assertEqual(
                    result["results"][0]["matched_expansions"][0]["matched_as"],
                    expected_expansion,
                )

    def test_intent_aliases_do_not_override_explicit_cross_domain_qualifiers(self) -> None:
        from ncs_mcp.search import core as search_core

        sports = server.search_ncs("프로야구 선수 연봉 협상", scope="unit", limit=3)

        self.assertEqual(sports["results"][0]["id"], "U_PLAYER")
        self.assertEqual(sports["query_intent_expansions"], [])
        self.assertEqual(
            search_core._ncs_search_intent_expansions("자동차 제조 원가 계산"),
            [],
        )
        self.assertEqual(
            search_core._ncs_search_intent_expansions("설비보수 외주 용역"),
            [],
        )

    def test_rare_token_outranks_common_token_in_fallback(self) -> None:
        result = server.search_ncs("퇴직정산 처리", scope="unit", limit=5)

        self.assertEqual(result["results"][0]["id"], "U_SEVERANCE")

    def test_definition_evidence_can_beat_name_only_candidate(self) -> None:
        result = server.search_ncs("인재 선발 육성", scope="unit", limit=5)

        self.assertEqual(result["match_mode"], "token_and")
        self.assertEqual(result["results"][0]["id"], "U_DEFINITION_RICH")

    def test_task_ksa_evidence_reranks_or_fallback_candidates(self) -> None:
        result = server.search_ncs("project rare roadmap", scope="unit", limit=5)

        self.assertEqual(result["match_mode"], "token_or")
        self.assertEqual(result["results"][0]["id"], "U_TASK_SIGNAL")
        task_ksa_queries = [
            sql for sql in self.sql_statements if "evidence_text" in sql
        ]
        self.assertTrue(task_ksa_queries)
        self.assertFalse(any(" LIKE " in sql for sql in task_ksa_queries))

    def test_task_ksa_prefilter_keeps_boundary_semantics(self) -> None:
        from ncs_mcp.search import core as search_core

        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO performance_criteria VALUES (?, ?, ?, ?)",
                (7, "xrare roadmap", None, 6),
            )
            scores = search_core._ncs_search_unit_task_ksa_scores(
                conn,
                ["U_TASK_SIGNAL", "U_NAME_ONLY"],
                ["rare", "roadmap"],
            )

        self.assertGreater(scores["U_TASK_SIGNAL"], 0.0)
        self.assertEqual(scores["U_NAME_ONLY"], 0.0)

    def test_token_idf_weights_scan_the_corpus_once(self) -> None:
        from ncs_mcp.search import core as search_core

        with self._open_db() as conn:
            self.sql_statements.clear()
            search_core._ncs_search_token_idf_weights(
                conn, ["퇴직정산", "처리", "관리", "퇴직정산"]
            )

        # The compact serving profile has no unit_name_raw index, so every LIKE
        # reads the whole table. One statement keeps that at one scan.
        self.assertEqual(len(self.sql_statements), 1)

    def test_token_idf_weights_fall_with_document_frequency(self) -> None:
        from ncs_mcp.search import core as search_core

        with self._open_db() as conn:
            weights = search_core._ncs_search_token_idf_weights(
                conn, ["퇴직정산", "처리"]
            )

        self.assertLess(weights["처리"], weights["퇴직정산"])
        self.assertGreaterEqual(weights["처리"], search_core._NCS_SEARCH_IDF_FLOOR)
        self.assertLessEqual(weights["퇴직정산"], 1.0)

    def test_token_idf_weights_use_explicit_classification_scope(self) -> None:
        from ncs_mcp.search import core as search_core

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_code TEXT, major_name TEXT,
                    middle_code TEXT, middle_name TEXT,
                    small_code TEXT, small_name TEXT,
                    sub_code TEXT, sub_name TEXT,
                    duty_order TEXT
                );
                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY,
                    unit_name_raw TEXT,
                    api_definition TEXT,
                    unit_level_raw TEXT,
                    classification_id INTEGER
                );
                """
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (1, "02", "경영", "", "", "", "", "", "", "1"),
                    (2, "15", "기계", "", "", "", "", "", "", "2"),
                ),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                (
                    ("HR_VEHICLE", "차량운영", "", "4", 1),
                    ("HR_ADMIN", "업무관리", "", "4", 1),
                    ("MECH_VEHICLE", "차량제조", "", "4", 2),
                    ("MECH_ADMIN", "업무관리", "", "4", 2),
                ),
            )
            search_core._register_ncs_search_udfs(conn)
            global_weights = search_core._ncs_search_token_idf_weights(
                conn,
                ["차량"],
            )
            scoped_weights = search_core._ncs_search_token_idf_weights(
                conn,
                ["차량"],
                {"major_code": "02"},
            )
        finally:
            conn.close()

        self.assertGreater(scoped_weights["차량"], global_weights["차량"])
        self.assertLessEqual(scoped_weights["차량"], 1.0)

    def test_shared_unit_ranks_home_classification_above_borrowed_copy(self) -> None:
        result = server.search_ncs("공용시설관리", scope="unit", limit=5)

        ids = [row["id"] for row in result["results"]]
        self.assertEqual(ids, ["0202010220_25v3", "0202010220_19v2"])

    def test_borrowing_classification_context_still_surfaces_borrowed_copy(self) -> None:
        result = server.search_ncs("자원봉사관리 공용시설관리", scope="unit", limit=5)

        ids = [row["id"] for row in result["results"]]
        self.assertEqual(ids[0], "0202010220_19v2")

    def test_phrase_hit_skips_lower_tier_sql_for_each_type(self) -> None:
        result = server.search_ncs("data workflow", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "phrase")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [sql for sql in self.sql_statements if f"FROM {table}" in sql]
            self.assertEqual(len(statements), 1, table)

    def test_and_fallback_runs_only_after_empty_phrase_tier(self) -> None:
        result = server.search_ncs("analysis data", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "token_and")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [sql for sql in self.sql_statements if f"FROM {table}" in sql]
            self.assertEqual(len(statements), 2, table)

    def test_or_fallback_runs_only_after_empty_phrase_and_and_tiers(self) -> None:
        result = server.search_ncs("missing data", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "token_or")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [
                sql
                for sql in self.sql_statements
                if f"FROM {table}" in sql and "evidence_text" not in sql
            ]
            self.assertEqual(len(statements), 3, table)

    def test_all_scope_keeps_each_types_best_available_match_tier(self) -> None:
        result = server.search_ncs("신입사원 면접", scope="all", limit=10)

        modes_by_type = {
            row["type"]: row["match_mode"] for row in result["results"]
        }
        self.assertEqual(result["match_mode"], "mixed")
        self.assertEqual(modes_by_type["criteria"], "phrase")
        self.assertEqual(modes_by_type["unit"], "token_or")
        self.assertEqual(result["match_mode_by_type"]["criteria"], "phrase")
        self.assertEqual(result["match_mode_by_type"]["unit"], "token_or")

    def test_all_scope_balances_types_and_pages_without_overlap(self) -> None:
        first = server.search_ncs("운영", scope="all", limit=4, offset=0)
        second = server.search_ncs("운영", scope="all", limit=4, offset=4)

        self.assertEqual(
            [row["type"] for row in first["results"]],
            ["unit", "element", "criteria", "ksa"],
        )
        self.assertEqual(first["counts_by_type"], {"unit": 1, "element": 1, "criteria": 1, "ksa": 1})
        self.assertEqual(first["next_offset"], 4)
        first_ids = {(row["type"], row["id"]) for row in first["results"]}
        second_ids = {(row["type"], row["id"]) for row in second["results"]}
        self.assertTrue(second_ids)
        self.assertFalse(first_ids.intersection(second_ids))

    def test_match_metadata_and_next_offset_are_rendered(self) -> None:
        result = server.search_ncs("운영", scope="all", limit=4)

        for row in result["results"]:
            self.assertEqual(row["match_mode"], "phrase")
            self.assertEqual(row["matched_tokens"], ["운영"])
            self.assertTrue(row["match_fields"])
        self.assertIn("최대 5건 미리보기", result["markdown_summary"])
        self.assertIn("offset=4", result["markdown_summary"])
        rendered = server._render_ncs_search_markdown(result)
        self.assertIsInstance(rendered, str)
        self.assertIn("offset=4", rendered)

    def test_like_wildcards_do_not_expand_and_offset_is_optional_in_mcp_schema(self) -> None:
        wildcard = server.search_ncs("%", scope="all", limit=20)
        self.assertEqual(wildcard["returned"], 0)

        tools = asyncio.run(server.mcp.list_tools())
        search_tool = next(tool for tool in tools if tool.name == "ncs_search")
        properties = search_tool.inputSchema.get("properties", {})
        self.assertIn("offset", properties)
        self.assertNotIn("offset", search_tool.inputSchema.get("required", []))

    def test_hr_natural_language_queries_rank_expected_units(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                DELETE FROM ksa_items;
                DELETE FROM performance_criteria;
                DELETE FROM competency_elements;
                DELETE FROM ncs_query_aliases;
                DELETE FROM competency_units;
                DELETE FROM classifications;
                """
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        10,
                        "02",
                        "경영·회계·사무",
                        "0202",
                        "총무·인사",
                        "020202",
                        "인사·조직",
                        "02020201",
                        "인사",
                        "1",
                    ),
                    (
                        20,
                        "08",
                        "문화·예술·디자인·방송",
                        "0803",
                        "문화콘텐츠",
                        "080304",
                        "영상제작",
                        "08030402",
                        "촬영",
                        "2",
                    ),
                    (
                        30,
                        "03",
                        "금융·보험",
                        "0301",
                        "금융",
                        "030104",
                        "자산운용",
                        "03010404",
                        "투자운용",
                        "3",
                    ),
                ),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        "0202020105_23v3",
                        "인사평가",
                        "인사평가 계획을 수립하고 조직 성과 향상을 지원한다",
                        "5",
                        10,
                    ),
                    (
                        "0202020107_23v4",
                        "교육훈련운영",
                        "직원의 교육훈련 계획을 수립하고 운영한다",
                        "4",
                        10,
                    ),
                    (
                        "0202020103_23v4",
                        "인력채용",
                        "인사 부문의 채용 계획을 수립한다",
                        "4",
                        10,
                    ),
                    (
                        "0202020109_23v5",
                        "급여지급",
                        "직원의 급여를 계산하여 지급한다",
                        "4",
                        10,
                    ),
                    (
                        "0301040409_14v1",
                        "투자 성과평가",
                        "투자 성과평가를 수행한다",
                        "5",
                        30,
                    ),
                    (
                        "0803040207_13v1",
                        "촬영",
                        "직원 업무의 제도 설계와 계획 수립을 지원한다",
                        "3",
                        20,
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO ncs_query_aliases VALUES (?, ?, ?)",
                ("0202020103_23v4", "채용", "인력채용"),
            )
            conn.commit()
        finally:
            conn.close()

        performance = server.search_ncs("성과평가 제도 설계", scope="unit", limit=5)
        training = server.search_ncs("직원 교육훈련 계획 수립", scope="unit", limit=5)
        hiring = server.search_ncs("인사 채용관리", scope="unit", limit=5)
        exact_evaluation = server.search_ncs("인사평가", scope="unit", limit=5)
        payroll = server.search_ncs("급여 계산", scope="unit", limit=5)

        self.assertIn(
            "0202020105_23v3",
            [row["id"] for row in performance["results"][:3]],
        )
        self.assertIn(
            "0202020107_23v4",
            [row["id"] for row in training["results"][:3]],
        )
        self.assertEqual(hiring["results"][0]["id"], "0202020103_23v4")
        self.assertEqual(exact_evaluation["results"][0]["id"], "0202020105_23v3")
        self.assertEqual(payroll["results"][0]["id"], "0202020109_23v5")

    def test_local_and_vercel_server_mirrors_are_identical(self) -> None:
        local_server = ROOT / "src" / "ncs_mcp" / "server.py"
        vercel_server = ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / "server.py"

        self.assertEqual(local_server.read_bytes(), vercel_server.read_bytes())

        for relative in ("query_router.py", "tool_registry.py"):
            with self.subTest(relative=relative):
                local_module = ROOT / "src" / "ncs_mcp" / relative
                vercel_module = (
                    ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / relative
                )
                self.assertEqual(local_module.read_bytes(), vercel_module.read_bytes())

        for relative in ("search/__init__.py", "search/core.py", "search/normalization.py"):
            with self.subTest(relative=relative):
                local_search = ROOT / "src" / "ncs_mcp" / relative
                vercel_search = (
                    ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / relative
                )
                self.assertEqual(local_search.read_bytes(), vercel_search.read_bytes())

    def test_server_reexports_search_package_entrypoint(self) -> None:
        from ncs_mcp.search import search_ncs as package_search_ncs

        self.assertIs(server.search_ncs, package_search_ncs)


class NcsSearchHybridRecallTests(NcsSearchRecallTests):
    """Run the same ranking/Unicode/evidence contract against v2 storage."""

    def _add_normalized_columns(self, conn: sqlite3.Connection) -> None:
        for table, fields in SEARCH_NORMALIZATION_V2_FIELDS.items():
            for raw, derived in fields.items():
                sparse = raw in SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {})
                declaration = "TEXT" if sparse else "TEXT NOT NULL DEFAULT ''"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {derived} {declaration}")
                expression = (
                    "COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, '')"
                    if table == "ncs_query_aliases" else raw
                )
                rows = conn.execute(f"SELECT rowid, {expression} FROM {table}").fetchall()
                conn.executemany(
                    f"UPDATE {table} SET {derived} = ? WHERE rowid = ?",
                    (
                        (None if sparse and normalize_search_text(row[1]) == row[1]
                         else normalize_search_text(row[1]), row[0])
                        for row in rows
                    ),
                )
        conn.execute(
            "CREATE TABLE serving_snapshot_manifest ("
            "manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
            SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST.items(),
        )
        self.assertEqual(search_core._normalized_search_storage(conn), "v2")

    def test_sparse_identity_null_and_empty_overrides(self) -> None:
        with self._open_db() as conn:
            # Each sparse field exercises identity, missing raw, and a legitimate
            # empty normalized value from punctuation-only source evidence.
            conn.execute("INSERT INTO competency_elements VALUES (100, 'alpha beta', 'U_EXACT')")
            conn.execute("INSERT INTO competency_elements VALUES (101, '---', 'U_EXACT')")
            conn.execute("INSERT INTO competency_elements VALUES (102, NULL, 'U_EXACT')")
            conn.execute("INSERT INTO ksa_items VALUES (100, 'knowledge', 'alpha beta', 'gamma delta', 100)")
            conn.execute("INSERT INTO ksa_items VALUES (101, 'knowledge', '---', '!!!', 101)")
            conn.execute("INSERT INTO ksa_items VALUES (102, 'knowledge', NULL, NULL, 102)")
            self._add_normalized_columns(conn)
            for alias, table in (("ce", "competency_elements"), ("ki", "ksa_items")):
                for raw, override in SEARCH_NORMALIZATION_V2_OVERRIDES[table].items():
                    expression = search_core._ncs_search_column(f"{alias}.{raw}", "v2")
                    self.assertEqual(expression, f"COALESCE({alias}.{override}, {alias}.{raw}, '')")
                    rows = conn.execute(
                        f"SELECT {raw}, {override}, {expression} FROM {table} {alias} "
                        f"WHERE {alias}.rowid >= 100 ORDER BY {alias}.rowid"
                    ).fetchall()
                    self.assertIsNone(rows[0][1])
                    self.assertEqual(rows[0][0], rows[0][2])
                    self.assertEqual(tuple(rows[1])[1:], ("", ""))
                    # Builder materializes '' for NULL raw: normalization is
                    # not identity, so the sparse override must not be NULL.
                    self.assertEqual(tuple(rows[2]), (None, "", ""))
            conn.commit()
        for scope in ("element", "ksa"):
            result = server.search_ncs("alpha beta", scope=scope, limit=5)
            self.assertEqual(result["results"][0]["id"], 100)
            self.assertEqual(result["results"][0]["text"], "alpha beta")

    def test_v2_manifest_misattribution_uses_entire_legacy_path(self) -> None:
        baseline = server.search_ncs("데이터분석", scope="all", limit=15)
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.execute("UPDATE competency_units SET unit_name_search_norm = 'misleading' ")
            conn.commit()
            cases = [(key, None) for key in SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST]
            cases += [(key, "wrong") for key in SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST]
            cases.append(("search_normalization_schema", "ncs_search_normalization_v1"))
            for key, value in cases:
                with self.subTest(key=key, value=value):
                    conn.execute("DELETE FROM serving_snapshot_manifest WHERE manifest_key = ?", (key,))
                    if value is not None:
                        conn.execute("INSERT INTO serving_snapshot_manifest VALUES (?, ?)", (key, value))
                    conn.commit()
                    self.assertFalse(search_core._normalized_search_storage(conn))
                    self.sql_statements.clear()
                    self.assertEqual(server.search_ncs("데이터분석", scope="all", limit=15), baseline)
                    self.assertFalse(any("ncs_search_match_normalized" in sql for sql in self.sql_statements))
                    conn.execute(
                        "INSERT OR REPLACE INTO serving_snapshot_manifest VALUES (?, ?)",
                        (key, SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST[key]),
                    )
                    conn.commit()

    def test_v2_partial_schema_cannot_borrow_v1_columns(self) -> None:
        baseline = server.search_ncs("데이터분석", scope="all", limit=15)
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.execute("ALTER TABLE ksa_items DROP COLUMN ksa_text_raw_search_override")
            conn.execute("ALTER TABLE ksa_items ADD COLUMN ksa_text_raw_search_norm TEXT")
            conn.commit()
            self.assertFalse(search_core._normalized_search_storage(conn))
        self.sql_statements.clear()
        self.assertEqual(server.search_ncs("데이터분석", scope="all", limit=15), baseline)
        self.assertFalse(any("ncs_search_match_normalized" in sql for sql in self.sql_statements))

    def test_v2_rejects_dense_nullable_or_sparse_not_null_schema(self) -> None:
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            for table, field, declaration in (
                ("competency_units", "unit_name_search_norm", "TEXT"),
                ("ksa_items", "ksa_text_raw_search_override", "TEXT NOT NULL DEFAULT ''"),
                ("ksa_items", "ksa_text_refined_search_override", "BLOB"),
            ):
                with self.subTest(field=field):
                    conn.execute("SAVEPOINT malformed_schema")
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {field}")
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} {declaration}")
                    self.assertFalse(search_core._normalized_search_storage(conn))
                    conn.execute("ROLLBACK TO malformed_schema")
                    conn.execute("RELEASE malformed_schema")

    def test_v2_duplicate_attestation_fails_closed(self) -> None:
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.execute("ALTER TABLE serving_snapshot_manifest RENAME TO original_manifest")
            conn.execute("CREATE TABLE serving_snapshot_manifest AS SELECT * FROM original_manifest")
            conn.execute("INSERT INTO serving_snapshot_manifest SELECT * FROM original_manifest")
            self.assertFalse(search_core._normalized_search_storage(conn))

    def test_v2_preserves_v1_exact_payloads_across_scopes_and_filters(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('V2_UNICODE', 'ＡＬＰＨＡ Straße', '', '4', 1)")
            conn.execute("INSERT INTO competency_elements VALUES (100, 'ＡＬＰＨＡ Straße', 'V2_UNICODE')")
            conn.execute("INSERT INTO performance_criteria VALUES (100, 'ＡＬＰＨＡ Straße', NULL, 100)")
            conn.execute("INSERT INTO ksa_items VALUES (100, 'knowledge', 'ＡＬＰＨＡ Straße', NULL, 100)")
            NcsSearchRecallTests._add_normalized_columns(self, conn)
            self.assertEqual(search_core._normalized_search_storage(conn), "v1")
            conn.commit()
        queries = ("데이터분석", "U_EXACT", "0202010220_25v3", "project rare roadmap", "alpha strasse")

        def payloads():
            return [
                server.search_ncs(query, scope=scope, limit=15, classification_filter=scope_filter)
                for query in queries
                for scope in ("all", "unit", "element", "criteria", "ksa")
                for scope_filter in (None, {"major_code": "02"}, {"major_name": "경영"})
            ]

        before = payloads()
        with self._open_db() as conn:
            for table, fields in SEARCH_NORMALIZATION_FIELDS.items():
                for field in fields.values():
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {field}")
            conn.execute("DROP TABLE serving_snapshot_manifest")
            self._add_normalized_columns(conn)
            conn.commit()
        self.assertEqual(before, payloads())


if __name__ == "__main__":
    unittest.main()
