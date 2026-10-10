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

    def test_search_request_frames_keep_subject_and_all_source_evidence(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_elements VALUES (9001, '데이터분석', 'U_EXACT')")
            conn.execute("INSERT INTO performance_criteria VALUES (9001, '데이터분석', NULL, 9001)")
            conn.execute("INSERT INTO ksa_items VALUES (9001, 'knowledge', '데이터분석', NULL, 9001)")
            conn.commit()
        templates = (
            "NCS 기준으로 다음 직무를 찾아줘: {subject}",
            "우리 회사에서 수행하는 업무 중 {subject}에 대한 수행준거를 찾아줘",
            "{subject}에 필요한 지식과 기술을 알려줘",
            "{subject}의 능력단위요소와 수행준거를 알려 주세요.",
            "국가직무능력표준에 따라 {subject}에 대해 설명해주세요",
            "다음 과업을 검색해 주세요: {subject}",
            "{subject} 관련 KSA를 보여줘",
        )
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            for scope in ("unit", "element", "criteria", "ksa", "all"):
                expected = server.search_ncs("데이터분석", scope=scope, limit=5)
                self.assertTrue(expected["results"])
                for template in templates:
                    query = template.format(subject="데이터분석")
                    with self.subTest(normalized=normalized, scope=scope, query=query):
                        actual = server.search_ncs(query, scope=scope, limit=5)
                        self.assertEqual(actual["query"], query)
                        self.assertEqual(actual["normalized_query"], "데이터분석")
                        self.assertEqual(actual["results"], expected["results"])
                        self.assertEqual(actual["query_tokens"], expected["query_tokens"])
                        public = server.ncs_search(query, scope=scope, limit=5)
                        self.assertTrue(public["ok"], public.get("error"))
                        if scope == "ksa":
                            public_plain = server.ncs_search(
                                "데이터분석", scope="ksa", limit=5
                            )
                            self.assertEqual(public["results"], public_plain["results"])
                            self.assertTrue(public["results"])
                            self.assertEqual(
                                {row["type"] for row in public["results"]}, {"ksa"}
                            )
                            self.assertTrue(
                                all(
                                    (row.get("path") or {}).get("unit_code")
                                    for row in public["results"]
                                )
                            )
                        else:
                            self.assertEqual(public["results"], expected["results"])

    def test_prompt_subject_preserves_exact_codes_and_long_official_names(self) -> None:
        long_name = "장비 진단 측정 결과 보고서 작성"
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('U_LONG_PROMPT', ?, '', '4', 1)",
                         (long_name,))
            conn.commit()
        for subject, expected_code in (("U_EXACT", "U_EXACT"), (long_name, "U_LONG_PROMPT")):
            query = f"NCS 기준으로 다음 직무를 찾아줘: {subject}"
            with self.subTest(subject=subject):
                result = server.ncs_search(query, scope="unit", limit=3)
                self.assertTrue(result["ok"])
                self.assertEqual(result["results"][0]["id"], expected_code)
                self.assertEqual(result["normalized_query"], subject)

    def test_prompt_subject_preserves_qualifiers_filters_and_pagination(self) -> None:
        subject = "인사 데이터분석"
        query = f"NCS 기준으로 다음 직무를 찾아줘: {subject}"
        for scope_filter in ({"major_code": "02"}, {"major_code": "15"}):
            for offset in (0, 1, 10):
                params = dict(scope="unit", limit=3, offset=offset,
                              classification_filter=scope_filter)
                with self.subTest(scope_filter=scope_filter, offset=offset):
                    expected = server.search_ncs(subject, **params)
                    actual = server.search_ncs(query, **params)
                    for key in ("results", "next_offset", "has_more_by_type",
                                "classification_filter", "classification_scope_invariant"):
                        self.assertEqual(actual.get(key), expected.get(key))

    def test_request_normalization_does_not_strip_domain_words_or_incomplete_commands(self) -> None:
        unchanged = (
            "데이터분석 지식 기술 태도", "지식재산 기술이전 관리",
            "금형 설계에 필요한 재료 선정", "우리 회사 인력 운영계획 수립",
            "NCS 기준으로", "직무를 찾아줘", "수행준거를 알려줘",
            "채용을 제외하고 인사기획", "회계 및 용접 비교",
        )
        for query in unchanged:
            with self.subTest(query=query):
                self.assertEqual(search_core._normalize_ncs_search_query(query)[0], query)
        subject = "금형 설계에 필요한 재료 선정"
        self.assertEqual(
            search_core._normalize_ncs_search_query(f"{subject}에 대한 수행준거를 알려줘")[0],
            subject,
        )

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

    def test_full_official_phrase_keeps_discriminating_tail_with_bounded_fallback(self) -> None:
        names = ("현장 장비 부품 진단", "현장 장비 부품 진단 결과 분석",
                 "현장 장비 부품 진단 결과 기록")
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, '', '4', 1)",
                [(f"LONG_{index}", name) for index, name in enumerate(names)],
            )
            conn.commit()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            for index in (1, 2):
                with self.subTest(normalized=normalized, name=names[index]):
                    result = server.search_ncs(names[index], scope="unit", limit=3)
                    self.assertEqual(result["normalized_query"], names[index])
                    self.assertEqual(len(result["query_tokens"]), 4)
                    self.assertEqual(result["match_mode"], "phrase")
                    self.assertEqual(result["results"][0]["id"], f"LONG_{index}")

    def test_exact_official_name_precedes_intent_rewrite_with_scope_and_unicode(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('LITERAL', 'ＳＥＮＳＯＲ diagnostics', '', '4', 2)")
            conn.execute("INSERT INTO competency_units VALUES ('HINT', 'control tuning', '', '4', 1)")
            conn.commit()
        with patch.dict(search_core._NCS_SEARCH_QUERY_INTENT_EQUIVALENTS,
                        {"sensor": ("control tuning",)}, clear=True):
            for normalized in (False, True):
                if normalized:
                    with self._open_db() as conn:
                        self._add_normalized_columns(conn)
                        conn.commit()
                with self.subTest(normalized=normalized):
                    # Legacy boundary LIKE does not handle fullwidth storage;
                    # verify the same Unicode-aware guard independently there.
                    with self._open_db() as conn:
                        search_core._register_ncs_search_udfs(conn)
                        self.assertTrue(search_core._ncs_search_has_exact_unit_name(
                            conn, "sensor diagnostics", {}, normalized=normalized))
                    if normalized:
                        for limit in (1, 3, 10):
                            exact = server.search_ncs("sensor diagnostics", scope="unit", limit=limit)
                            self.assertEqual(exact["results"][0]["id"], "LITERAL")
                            self.assertEqual(exact["match_mode"], "phrase")
                            self.assertEqual(exact["query_intent_expansions"], [])
                    # An exact name in another major must not disable valid
                    # intent retrieval inside the explicit classification.
                    scoped = server.search_ncs("sensor diagnostics", scope="unit", limit=3,
                                               classification_filter={"major_code": "02"})
                    self.assertEqual(scoped["results"][0]["id"], "HINT")
                    self.assertEqual(scoped["match_mode"], "intent_alias")

    def test_korean_official_name_does_not_collapse_into_general_intent(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('LITERAL_CLUB', '학습동아리 운영', '', '4', 1)")
            conn.commit()
        for limit in (1, 3, 10):
            exact = server.search_ncs("학습동아리 운영", scope="unit", limit=limit)
            self.assertEqual(exact["results"][0]["id"], "LITERAL_CLUB")
            self.assertEqual(exact["match_mode"], "phrase")
            self.assertEqual(exact["query_intent_expansions"], [])

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

    def test_post_query_scope_guard_fails_closed_on_leaked_candidate(self) -> None:
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (77, "77", "Foreign", "01", "Foreign", "01", "Foreign", "01", "Foreign", "7"),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                (
                    ("U_SCOPE_LOCAL", "scope", "", "4", 1),
                    ("U_SCOPE_FOREIGN", "scope", "", "4", 77),
                ),
            )
            conn.commit()

        # Simulate a broken/custom executor that forgot to apply the hard
        # classification predicate. The post-query guard must still contain it.
        with patch.object(
            search_core,
            "_apply_ncs_classification_filter_to_tiers",
            side_effect=lambda tiers, *_args, **_kwargs: tiers,
        ):
            result = server.search_ncs(
                "scope",
                scope="unit",
                limit=10,
                classification_filter={"major_code": "02"},
            )

        self.assertEqual(result["results"], [])
        self.assertEqual(result["returned"], 0)
        self.assertEqual(result["next_offset"], None)
        invariant = result["classification_scope_invariant"]
        self.assertFalse(invariant["ok"])
        self.assertEqual(invariant["status"], "failed_closed")
        self.assertEqual(
            invariant["violation"],
            "classification_filter_post_query_mismatch",
        )
        self.assertGreaterEqual(invariant["checked_rows"], 2)
        self.assertTrue(invariant["mismatches"])

    def test_scoped_results_strip_private_scope_values_and_keep_full_path(self) -> None:
        result = server.search_ncs(
            "data workflow analysis",
            scope="unit",
            limit=5,
            classification_filter={"major_code": "02"},
        )

        self.assertTrue(result["results"])
        self.assertNotIn("_classification_codes", result["results"][0])
        self.assertEqual(
            set(result["results"][0]["path"]),
            {
                "major_code", "major", "middle_code", "middle",
                "small_code", "small", "sub_code", "sub",
                "duty_order",
            },
        )
        self.assertNotIn("classification_scope_invariant", result)

    def test_normalized_name_filter_matches_unicode_punctuation_and_spacing(self) -> None:
        with self._open_db() as conn:
            conn.execute(
                "UPDATE classifications SET major_name = ?, middle_name = ? "
                "WHERE classification_id = 1",
                ("Class_Name", "Straße"),
            )
            self._add_normalized_columns(conn)
            conn.commit()

        result = server.search_ncs(
            "data workflow analysis",
            scope="unit",
            limit=5,
            classification_filter={
                "major_name": "  class-name  ",
                "middle_name": "  strasse  ",
            },
        )

        self.assertEqual([row["id"] for row in result["results"]], ["U_ASCII"])
        self.assertNotIn("classification_scope_invariant", result)

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
            ["Z_CONTEXT_ADMIN"],
        )
        self.assertTrue(executed["search_context"]["hard_filter_applied"])
        self.assertEqual(
            executed["search_context"]["selected_candidate"]["sub_code"],
            "01",
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
            ("U_FUND", "자금관리", "법인카드와 유가증권을 관리한다", "4", 1),
            ("U_OUTSOURCE", "용역관리", "시설관리 용역 계약을 관리한다", "4", 4),
            ("U_COST", "원가관리", "손익분기점과 CVP 분석을 수행한다", "4", 1),
            ("U_OFFICE_AUTO", "사무자동화 프로그램 활용", "프레젠테이션 자료를 제작한다", "4", 1),
            ("U_TEACH", "교수활동 수행", "교안 작성과 교수활동을 수행한다", "4", 1),
            ("U_CURRICULUM", "교육과정 개발", "평가 도구와 교육과정을 개발한다", "4", 1),
            ("U_CURRICULUM_DESIGN", "교육과정 설계", "교육과정 설계 계획을 수립한다", "4", 1),
            ("U_EDU_PLAN", "교육운영기획", "교육 제도와 평가지표를 운용한다", "4", 1),
            ("U_EDU_RESOURCE", "교육자원관리", "학습관리시스템 등 교육 인프라를 관리한다", "4", 1),
            ("U_LEARN_ORG", "학습조직구축", "조직 내 학습조직을 구축한다", "4", 1),
            ("U_BARGAIN", "단체교섭준비", "교섭 위원과 교섭안을 준비한다", "4", 1),
            ("U_DOC_ADMIN", "총무문서관리", "문서 보관과 폐기를 관리한다", "4", 1),
            ("U_EDU_SYSTEM", "교육체계 수립", "교육 수요와 교육체계를 수립한다", "4", 1),
            ("U_EDU_EVAL", "교육성과 평가", "교육 참여율과 만족도를 집계한다", "4", 1),
            ("U_EVENT", "행사지원관리", "연간 행사를 지원한다", "4", 1),
            ("U_JOB", "직무관리", "직무 등급과 직무 평가를 관리한다", "4", 1),
            ("U_MOVE", "인력이동관리", "배치전환 소요 인원을 파악한다", "4", 1),
            ("U_BARGAIN_RUN", "단체교섭", "협약 체결과 교섭을 진행한다", "4", 1),
            ("U_AGREE", "단체협약이행", "취업규칙 변경을 관리한다", "4", 1),
            ("U_DOCS", "자료 관리", "사내 자료 보안을 관리한다", "4", 1),
            ("U_PAY", "급여지급", "4대보험과 급여를 지급한다", "4", 1),
            ("U_WITHHOLD", "원천징수", "연말정산과 원천징수를 수행한다", "4", 1),
            ("U_HR_PLAN", "인사기획", "인건비 예산을 포함한 인사기획을 수행한다", "4", 1),
            ("U_LABOR_CONFLICT", "노사갈등 해결", "노동관계법 준수와 분쟁을 예방한다", "4", 1),
            ("U_ADMIN_SUPPORT", "업무지원", "법인 인감 날인을 지원한다", "4", 1),
            ("U_OFFICE_ADMIN", "사무행정 업무 관리", "부서 일정과 경비 정산을 지원한다", "4", 1),
            ("U_COMBINE", "사업결합회계", "연결재무제표를 작성한다", "4", 1),
            ("U_NPO", "비영리회계", "비영리법인 회계 보고서를 작성한다", "4", 1),
            ("U_GUARD_CUST", "경비고객관계관리", "경비원 고객 응대와 불만을 처리한다", "4", 1),
            ("U_AUTO_ACCIDENT", "차량사고 현장조사", "자동차 사고 현장을 조사하고 보고한다", "4", 1),
            ("U_MAKEUP", "베이스 메이크업", "피부 표현과 얼굴 윤곽 메이크업을 수행한다", "4", 1),
            ("U_USE_NOISE", "사용승인 관리", "공구 사용을 승인한다", "4", 6),
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

    def test_joined_official_compound_phrase_is_bounded(self) -> None:
        self.assertEqual(
            search_core._ncs_search_joined_compound_phrase(
                "급여 지급", ["급여", "지급"]
            ),
            "급여지급",
        )
        rejected = (
            ("급여지급", ["급여지급"]),
            ("급여 pay", ["급여", "pay"]),
            ("인사 및 평가", ["인사", "평가"]),
            ("하나 둘 셋 넷 다섯", ["하나", "둘", "셋", "넷"]),
            (
                "가나다라마바사아자차카타파하가나다라마바사아자차카타 파하",
                ["가나다라마바사아자차카타파하가나다라마바사아자차카타", "파하"],
            ),
        )
        for phrase, tokens in rejected:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    search_core._ncs_search_joined_compound_phrase(phrase, tokens),
                    "",
                )

    def test_joined_compound_subphrases_are_unit_only_and_particle_aware(self) -> None:
        query_tokens = [
            "\uacbd\uc601",
            "\uc815\ubcf4",
            "\ub300\uc2dc\ubcf4\ub4dc",
            "\uc2dc\uac01\ud654",
        ]
        expansions = search_core._ncs_search_joined_compound_subphrases(query_tokens)

        self.assertIn("\uacbd\uc601\uc815\ubcf4", expansions["\uacbd\uc601"])
        self.assertIn("\uacbd\uc601\uc815\ubcf4", expansions["\uc815\ubcf4"])
        self.assertIn("\uc815\ubcf4\ub300\uc2dc\ubcf4\ub4dc", expansions["\uc815\ubcf4"])

        particle_tokens = ["\ucd9c\uc785", "\ud1b5\uc81c\uc640", "\ubcf4\uc548"]
        particle_expansions = search_core._ncs_search_joined_compound_subphrases(
            particle_tokens
        )
        self.assertIn("\ucd9c\uc785\ud1b5\uc81c", particle_expansions["\ucd9c\uc785"])

    def test_joined_subphrase_recovers_official_name_but_rejects_internal_compound(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', 1)",
                (
                    (
                        "C_SUBPHRASE",
                        "\uacbd\uc601\uc815\ubcf4\uc2dc\uac01\ud654",
                        "\uc815\ubcf4\ub97c \ud65c\uc6a9\ud55c\ub2e4",
                    ),
                    ("C_INTERNAL", "\uc218\ucd9c\uc785\uacc4\uc57d", ""),
                    ("C_BOUNDARY", "\ucd9c\uc785\ud1b5\uc81c", ""),
                ),
            )
            conn.commit()

        recovered = server.search_ncs(
            "\uacbd\uc601 \uc815\ubcf4 \ub300\uc2dc\ubcf4\ub4dc \uc2dc\uac01\ud654",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "02"},
        )
        recovered_ids = [row["id"] for row in recovered["results"]]
        self.assertIn("C_SUBPHRASE", recovered_ids)
        self.assertEqual("C_SUBPHRASE", recovered_ids[0])
        recovered_row = next(row for row in recovered["results"] if row["id"] == "C_SUBPHRASE")
        self.assertIn("\uacbd\uc601\uc815\ubcf4", [
            item["matched_as"] for item in recovered_row["matched_expansions"]
        ])
        self.assertTrue(
            any(
                item["matched_as"] == "\uacbd\uc601\uc815\ubcf4"
                and item["match_fields"] == ["unit_name"]
                for item in recovered_row["matched_expansions"]
            )
        )

        internal = server.search_ncs(
            "\ucd9c\uc785 \uacc4\uc57d \uc791\uc131",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "02"},
        )
        self.assertNotIn("C_INTERNAL", [row["id"] for row in internal["results"]])

        boundary = server.search_ncs(
            "\ucd9c\uc785 \ud1b5\uc81c \ubcf4\uc548",
            scope="unit",
            limit=10,
            classification_filter={"major_code": "02"},
        )
        self.assertIn("C_BOUNDARY", [row["id"] for row in boundary["results"]])

    def _seed_joined_scope_controls(self) -> None:
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO classifications VALUES "
                "(97, '97', '시험', '97', '시험', '97', '시험', '97', '시험', '1')"
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', 97)",
                (
                    ("R3_TARGET", "경영정보시각화", "자료를 도표로 만든다"),
                    ("R3_DEFINITION", "재무회계", "경영정보 정보시각화 사례를 다룬다"),
                    ("R3_W1", "경영 분석", ""),
                    ("R3_W2", "정보 보안", ""),
                    ("R3_W3", "시각화 도구", ""),
                    ("R3_ALIAS", "보고서 작성", ""),
                ),
            )
            conn.execute(
                "INSERT INTO ncs_query_aliases VALUES "
                "('R3_ALIAS', '경영정보시각화', '경영정보시각화')"
            )
            conn.execute("INSERT INTO competency_elements VALUES (970, '경영정보 정보시각화', 'R3_TARGET')")
            conn.execute("INSERT INTO performance_criteria VALUES (970, '경영정보 정보시각화', NULL, 970)")
            conn.execute("INSERT INTO ksa_items VALUES (970, 'knowledge', '경영정보 정보시각화', NULL, 970)")
            conn.commit()

    def test_joined_scope_definition_cannot_promote_and_tier_or_hide_name(self) -> None:
        self._seed_joined_scope_controls()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            params = dict(scope="unit", limit=20, classification_filter={"major_code": "97"})
            with self.subTest(normalized=normalized):
                actual = server.search_ncs("정보 시각화 경영", **params)
                with patch.object(search_core, "_ncs_search_joined_compound_subphrases", return_value={}):
                    control = server.search_ncs("정보 시각화 경영", **params)
                self.assertEqual(control["match_mode"], "token_or")
                self.assertIn("R3_DEFINITION", [row["id"] for row in control["results"]])
                self.assertEqual(actual["match_mode"], "token_or")
                self.assertIn("R3_TARGET", [row["id"] for row in actual["results"]])
                decoy = next(row for row in actual["results"] if row["id"] == "R3_DEFINITION")
                self.assertEqual(decoy["matched_expansions"], [])
                self.assertNotIn("시각화", decoy["matched_tokens"])

    def test_joined_scope_name_alias_recovery_metadata_and_pagination(self) -> None:
        self._seed_joined_scope_controls()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            with self.subTest(normalized=normalized):
                params = dict(scope="unit", classification_filter={"major_code": "97"})
                query = "경영 정보 대시보드 시각화"
                actual = server.search_ncs(query, limit=20, **params)
                self.assertEqual(actual["match_mode"], "token_or")
                for code, field in (("R3_TARGET", "unit_name"), ("R3_ALIAS", "alias")):
                    row = next(row for row in actual["results"] if row["id"] == code)
                    self.assertTrue(any(
                        exp["matched_as"] == "경영정보" and exp["match_fields"] == [field]
                        for exp in row["matched_expansions"]
                    ))
                for row in actual["results"]:
                    for exp in row["matched_expansions"]:
                        self.assertTrue(set(exp["match_fields"]) <= {"unit_name", "alias"})
                        self.assertIn(exp["matched_as"], actual["query_expansions"][exp["token"]])
                for offset in (0, 1):
                    page = server.search_ncs(query, limit=2, offset=offset, **params)
                    self.assertEqual(page["results"], actual["results"][offset:offset + 2])

    def test_joined_scope_classification_and_code_cannot_supply_compound_evidence(self) -> None:
        self._seed_joined_scope_controls()
        with self._open_db() as conn:
            for index, field in enumerate(("major_name", "middle_name", "small_name", "sub_name"), 980):
                conn.execute(
                    "INSERT INTO classifications VALUES (?, '97', '시험', '97', '시험', '97', '시험', '97', '시험', '1')",
                    (index,),
                )
                conn.execute(f"UPDATE classifications SET {field} = ? WHERE classification_id = ?",
                             ("경영정보 정보시각화", index))
                conn.execute("INSERT INTO competency_units VALUES (?, '분류대조', '', '4', ?)",
                             (f"R3_CLASS_{field}", index))
            conn.execute("INSERT INTO competency_units VALUES ('경영정보 정보시각화', '코드대조', '', '4', 97)")
            conn.commit()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            with self.subTest(normalized=normalized):
                actual = server.search_ncs("정보 시각화 경영", scope="unit", limit=30,
                                           classification_filter={"major_code": "97"})
                self.assertEqual(actual["match_mode"], "token_or")
                self.assertIn("R3_TARGET", [row["id"] for row in actual["results"]])
                for row in actual["results"]:
                    if row["id"].startswith("R3_CLASS_") or row["id"] == "경영정보 정보시각화":
                        self.assertEqual(row["matched_expansions"], [])
                        self.assertNotIn("시각화", row["matched_tokens"])

    def test_joined_scope_requires_hard_filter_and_does_not_change_leaves(self) -> None:
        self._seed_joined_scope_controls()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            for scope in ("unit", "element", "criteria", "ksa"):
                for filter_value in (None, {"unknown_scope": "97"}, {"major_code": "97"}):
                    if scope == "unit" and filter_value == {"major_code": "97"}:
                        continue
                    with self.subTest(normalized=normalized, scope=scope, filter=filter_value):
                        params = dict(scope=scope, limit=20, classification_filter=filter_value)
                        actual = server.search_ncs("정보 시각화 경영", **params)
                        with patch.object(search_core, "_ncs_search_joined_compound_subphrases", return_value={}):
                            control = server.search_ncs("정보 시각화 경영", **params)
                        self.assertEqual(actual, control)

    def test_joined_scope_overlap_keeps_general_expansion_provenance(self) -> None:
        self._seed_joined_scope_controls()
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            with self.subTest(normalized=normalized), patch.object(
                server, "_validated_ncs_search_token_expansions",
                return_value={"시각화": ["정보시각화"]},
            ):
                result = server.search_ncs("정보 시각화 경영", scope="unit", limit=20,
                                           classification_filter={"major_code": "97"})
                self.assertEqual(result["match_mode"], "expanded_token_and")
                decoy = next(row for row in result["results"] if row["id"] == "R3_DEFINITION")
                self.assertIn({"token": "시각화", "matched_as": "정보시각화", "match_fields": ["definition"]},
                              decoy["matched_expansions"])
                self.assertIn("시각화", decoy["matched_tokens"])

    def test_scoped_expanded_and_reports_only_base_expansions_with_leaf_or(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES "
                         "('S1_AND', '인사 채용 지원자 인사채용관리', '', '4', 1)")
            conn.execute("INSERT INTO competency_elements VALUES (9800, '인사 검토', 'S1_AND')")
            conn.commit()
        query = "인사 채용관리 지원자"
        expected = {"채용관리": ["채용", "인력채용"]}
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            for scope in ("unit", "all"):
                with self.subTest(normalized=normalized, scope=scope):
                    result = server.search_ncs(query, scope=scope, limit=20,
                                              classification_filter={"major_code": "02"})
                    self.assertEqual(result["match_mode_by_type"]["unit"], "expanded_token_and")
                    if scope == "all":
                        self.assertEqual(result["match_mode"], "mixed")
                        self.assertEqual(result["match_mode_by_type"]["element"], "token_or")
                    self.assertEqual(result["query_expansions"], expected)
                    row = next(row for row in result["results"] if row["id"] == "S1_AND")
                    self.assertEqual(row["matched_expansions"], [{
                        "token": "채용관리", "matched_as": "채용", "match_fields": ["unit_name"],
                    }])

    def test_scoped_unit_or_and_leaf_expanded_and_union_only_active_maps(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES "
                         "('S1_OR', '경영성과평가', '', '4', 1)")
            conn.execute("INSERT INTO competency_elements VALUES (9801, '경영 인사평가 지원자', 'S1_OR')")
            conn.commit()
        query = "경영 성과평가 지원자"
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            with self.subTest(normalized=normalized):
                result = server.search_ncs(query, scope="all", limit=30,
                                          classification_filter={"major_code": "02"})
                self.assertEqual(result["match_mode"], "mixed")
                self.assertEqual(result["match_mode_by_type"]["unit"], "token_or")
                self.assertEqual(result["match_mode_by_type"]["element"], "expanded_token_and")
                # Unit search resolves the absent compound to 평가, while
                # leaf search retains 성과평가 and its general equivalent.
                self.assertEqual(result["query_expansions"], {
                    "경영": ["경영평가"], "평가": ["경영평가", "평가지원자"],
                    "지원자": ["평가지원자"], "성과평가": ["인사평가"],
                })
                for row in result["results"]:
                    if row["type"] != "unit":
                        self.assertTrue(all(exp["matched_as"] == "인사평가"
                                            for exp in row["matched_expansions"]))

    def test_compound_metadata_preserves_cross_token_general_provenance(self) -> None:
        tokens = ["경영", "정보", "시각화"]
        for mode in ("expanded_token_and", "token_or"):
            for definition in ("경영정보", "경영 정보 경영정보"):
                with self.subTest(mode=mode, definition=definition):
                    row = {"type": "unit", "_search_fields": {
                        "unit_name": "경영정보", "definition": definition,
                    }}
                    search_core._ncs_search_match_metadata(
                        row, query_tokens=tokens, phrase=" ".join(tokens), match_mode=mode,
                        token_expansions={"시각화": ["경영정보"]},
                        compound_subphrase_expansions={"경영": ["경영정보"], "정보": ["경영정보"]},
                    )
                    by_token = {entry["token"]: entry["match_fields"]
                                for entry in row["matched_expansions"]}
                    expected = {"시각화": ["unit_name", "definition"]}
                    if mode == "token_or":
                        expected.update({"경영": ["unit_name"], "정보": ["unit_name"]})
                    self.assertEqual(by_token, expected)

    def test_joined_official_compound_recovers_space_variant_without_extra_sql(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, '', '4', 1)",
                (
                    ("C_SPACED", "급여 지급 안내"),
                    ("C_JOINED", "급여지급"),
                    ("C_JOINED_PREFIX", "급여지급검토"),
                ),
            )
            conn.commit()

        query = "급여 지급"
        # Warm the per-process unit lexicon so both runs issue the same SQL.
        server.search_ncs(query, scope="unit", limit=10)
        self.sql_statements.clear()
        with patch.object(
            search_core, "_ncs_search_joined_compound_phrase", return_value=""
        ):
            baseline = server.search_ncs(query, scope="unit", limit=10)
        baseline_statement_count = len(self.sql_statements)

        self.sql_statements.clear()
        result = server.search_ncs(query, scope="unit", limit=10)
        self.assertEqual(len(self.sql_statements), baseline_statement_count)
        self.assertEqual(result["results"][:baseline["returned"]], baseline["results"])
        # U_PAY is the shared-fixture official 급여지급 unit used by intent
        # seeds; it must stay ahead of the longer prefix-only synthetic row.
        self.assertEqual(
            [row["id"] for row in result["results"]],
            ["C_SPACED", "C_JOINED", "U_PAY", "C_JOINED_PREFIX"],
        )
        joined = result["results"][1]
        self.assertEqual(joined["match_mode"], "phrase")
        self.assertEqual(joined["matched_tokens"], ["급여", "지급"])
        self.assertEqual(joined["match_fields"], ["unit_name"])
        self.assertEqual(
            joined["matched_expansions"],
            [
                {
                    "query": query,
                    "matched_as": "급여지급",
                    "match_fields": ["unit_name"],
                }
            ],
        )
        self.assertEqual(result["query_expansions"], {})
        self.assertEqual(
            server.search_ncs(
                query,
                scope="unit",
                classification_filter={"major_code": "99"},
            )["returned"],
            0,
        )

    def test_ksa_scope_is_public_and_returns_only_ksa(self) -> None:
        result = server.ncs_search("채용", scope="ksa", limit=5)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scope"], "ksa")
        self.assertTrue(result["results"])
        self.assertEqual({row["type"] for row in result["results"]}, {"ksa"})

    def test_ksa_scope_prefers_ksa_from_identified_units(self) -> None:
        result = server.ncs_search("채용 운영", scope="ksa", limit=5)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["match_mode"], "unit_anchored")
        self.assertEqual({row["type"] for row in result["results"]}, {"ksa"})
        self.assertTrue(result["results"])
        self.assertTrue(
            all(row.get("path", {}).get("unit_code") == "U_HIRE_1" for row in result["results"])
        )
        self.assertIn("U_HIRE_1", result.get("anchor_units") or [])

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

    def test_or_fallback_reports_effective_expansions_without_changing_pages(self) -> None:
        query = "인사 채용관리 미등록단어"
        for normalized in (False, True):
            if normalized:
                with self._open_db() as conn:
                    self._add_normalized_columns(conn)
                    conn.commit()
            with self.subTest(normalized=normalized):
                whole = server.search_ncs(query, scope="unit", limit=20)
                self.assertEqual(whole["match_mode"], "token_or")
                self.assertEqual(whole["query_expansions"]["채용관리"], ["채용", "인력채용"])
                self.assertTrue(any(row["matched_expansions"] for row in whole["results"]))
                pages = [server.search_ncs(query, scope="unit", limit=1, offset=i)
                         for i in range(min(3, whole["returned"]))]
                self.assertEqual([p["results"][0] for p in pages], whole["results"][:len(pages)])
                self.assertTrue(all(p["query_expansions"] == whole["query_expansions"] for p in pages))
                exact = server.search_ncs("인력채용", scope="unit", limit=3)
                self.assertEqual(exact["query_expansions"], {})

    def test_leaf_expansion_metadata_excludes_disabled_job_scope_reductions(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO ncs_query_aliases VALUES (?, ?, ?)",
                         ("U_HIRE_1", "인사", "인사"))
            conn.execute("INSERT INTO competency_elements VALUES (?, ?, ?)",
                         (9010, "인사평가", "U_HIRE_1"))
            conn.commit()
        result = server.search_ncs("인사업무 성과평가 미등록단어", scope="element", limit=3)
        self.assertEqual(result["match_mode"], "token_or")
        self.assertEqual(result["query_expansions"], {"성과평가": ["인사평가"]})
        self.assertEqual(result["results"][0]["matched_expansions"][0]["matched_as"], "인사평가")

    def test_job_scope_compound_expansion_does_not_match_leaf_homograph(self) -> None:
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO ncs_query_aliases VALUES (?, ?, ?)",
                ("U_HR_MANAGEMENT", "인사", "인사"),
            )
            conn.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', 2)",
                ("U_GREETING", "환영 환송", "고객을 맞이하고 배웅한다"),
            )
            conn.execute(
                "INSERT INTO competency_elements VALUES (?, ?, ?)",
                (300, "인사하기", "U_GREETING"),
            )
            conn.execute(
                "INSERT INTO performance_criteria VALUES (?, ?, NULL, ?)",
                (300, "인사하기 절차를 적용할 수 있다", 300),
            )
            conn.execute(
                "INSERT INTO ksa_items VALUES (?, 'attitude', ?, NULL, ?)",
                (300, "인사하기 태도", 300),
            )
            conn.commit()

        query = "인사업무"
        self.sql_statements.clear()
        with patch.object(
            search_core,
            "_ncs_search_leaf_token_expansions",
            side_effect=lambda _tokens, expansions: expansions,
        ):
            unsafe = server.search_ncs(query, scope="all", limit=20)
        unsafe_statement_count = len(self.sql_statements)
        for item_type in ("element", "criteria", "ksa"):
            self.assertIn(
                300,
                [row["id"] for row in unsafe["results"] if row["type"] == item_type],
            )

        self.sql_statements.clear()
        result = server.search_ncs(query, scope="all", limit=20)
        self.assertLessEqual(len(self.sql_statements), unsafe_statement_count)
        self.assertIn(
            "U_HR_MANAGEMENT",
            [row["id"] for row in result["results"] if row["type"] == "unit"],
        )
        self.assertNotIn(
            "U_GREETING",
            [row.get("path", {}).get("unit_code") for row in result["results"]],
        )
        self.assertEqual(result["query_expansions"][query], ["인사"])
        hr_unit = next(
            row for row in result["results"] if row["id"] == "U_HR_MANAGEMENT"
        )
        self.assertEqual(hr_unit["matched_tokens"], [query])
        self.assertEqual(hr_unit["matched_expansions"][0]["matched_as"], "인사")
        for item_type in ("element", "criteria", "ksa"):
            self.assertEqual(server.search_ncs(query, scope=item_type)["returned"], 0)

        expected_classification = {
            "major_code": "99",
            "major": "기타",
            "middle_code": "99",
            "middle": "기타",
            "small_code": "99",
            "small": "기타",
            "sub_code": "99",
            "sub": "데이터분석 직무",
        }
        for item_type in ("element", "criteria", "ksa"):
            direct = server.search_ncs("인사하기", scope=item_type, limit=5)
            self.assertEqual(direct["results"][0]["id"], 300)
            self.assertEqual(direct["results"][0]["match_mode"], "phrase")
            self.assertEqual(direct["results"][0]["matched_expansions"], [])
            for field, value in expected_classification.items():
                self.assertEqual(direct["results"][0]["path"][field], value)

        legacy_ids = [(row["type"], row["id"]) for row in result["results"]]
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        normalized = server.search_ncs(query, scope="all", limit=20)
        self.assertEqual(
            [(row["type"], row["id"]) for row in normalized["results"]],
            legacy_ids,
        )

    def test_all_low_information_suffix_expansions_are_unit_scope_only(self) -> None:
        suffix_cases = (
            ("관리", "나래", "온새"),
            ("운영", "보람", "가온"),
            ("업무", "다온", "누리"),
            ("직무", "해솔", "아람"),
            ("실무", "마루", "라온"),
        )
        with self._open_db() as conn:
            for index, (suffix, expansion_base, direct_base) in enumerate(
                suffix_cases, start=1
            ):
                classification_id = 100 + index
                unit_name_code = f"SCOPE_UNIT_{index}"
                classification_code = f"SCOPE_CLASS_{index}"
                leaf_code = f"SCOPE_LEAF_{index}"
                direct_code = f"SCOPE_DIRECT_{index}"
                conn.execute(
                    "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        classification_id,
                        f"8{index}",
                        "합성범위",
                        f"8{index}",
                        "합성중분류",
                        f"8{index}",
                        "합성소분류",
                        f"8{index}",
                        f"{expansion_base}분야",
                        str(index),
                    ),
                )
                conn.executemany(
                    "INSERT INTO competency_units VALUES (?, ?, '', '4', ?)",
                    (
                        (unit_name_code, f"{expansion_base}기획", 2),
                        (classification_code, "별도수행", classification_id),
                        (leaf_code, "무관활동", 2),
                        (direct_code, "직접활동", 2),
                    ),
                )
                conn.execute(
                    "INSERT INTO ncs_query_aliases VALUES (?, ?, ?)",
                    (unit_name_code, expansion_base, expansion_base),
                )
                leaf_id = 400 + index
                direct_id = 500 + index
                conn.executemany(
                    "INSERT INTO competency_elements VALUES (?, ?, ?)",
                    (
                        (leaf_id, f"{expansion_base}하기", leaf_code),
                        (direct_id, f"{direct_base}{suffix} 수행하기", direct_code),
                    ),
                )
                conn.executemany(
                    "INSERT INTO performance_criteria VALUES (?, ?, NULL, ?)",
                    (
                        (leaf_id, f"{expansion_base}하기 기준", leaf_id),
                        (direct_id, f"{direct_base}{suffix} 기준", direct_id),
                    ),
                )
                conn.executemany(
                    "INSERT INTO ksa_items VALUES (?, 'knowledge', ?, NULL, ?)",
                    (
                        (leaf_id, f"{expansion_base}하기 지식", leaf_id),
                        (direct_id, f"{direct_base}{suffix} 지식", direct_id),
                    ),
                )
            conn.execute(
                "INSERT INTO competency_elements VALUES (?, ?, ?)",
                (600, "인사평가하기", "U_HR_MANAGEMENT"),
            )
            conn.execute(
                "INSERT INTO performance_criteria VALUES (?, ?, NULL, ?)",
                (600, "인사평가하기 기준", 600),
            )
            conn.execute(
                "INSERT INTO ksa_items VALUES (?, 'skill', ?, NULL, ?)",
                (600, "인사평가하기 기술", 600),
            )
            conn.commit()

        def assert_contract() -> None:
            for index, (suffix, expansion_base, direct_base) in enumerate(
                suffix_cases, start=1
            ):
                query = f"{expansion_base}{suffix}"
                leaf_id = 400 + index
                for item_type in ("element", "criteria", "ksa"):
                    with self.subTest(
                        suffix=suffix,
                        scope=item_type,
                    ):
                        with patch.object(
                            search_core,
                            "_ncs_search_leaf_token_expansions",
                            side_effect=lambda _tokens, expansions: expansions,
                        ):
                            unsafe = server.search_ncs(
                                query, scope=item_type, limit=20
                            )
                        self.assertIn(
                            leaf_id, [row["id"] for row in unsafe["results"]]
                        )
                        safe = server.search_ncs(query, scope=item_type, limit=20)
                        self.assertNotIn(
                            leaf_id, [row["id"] for row in safe["results"]]
                        )

                units = server.search_ncs(query, scope="unit", limit=20)
                unit_by_id = {row["id"]: row for row in units["results"]}
                self.assertIn(f"SCOPE_UNIT_{index}", unit_by_id)
                self.assertIn(f"SCOPE_CLASS_{index}", unit_by_id)
                self.assertIn(
                    "classification",
                    unit_by_id[f"SCOPE_CLASS_{index}"]["match_fields"],
                )

                direct_query = f"{direct_base}{suffix}"
                direct_id = 500 + index
                for item_type in ("element", "criteria", "ksa"):
                    direct = server.search_ncs(
                        direct_query, scope=item_type, limit=20
                    )
                    self.assertEqual(direct["results"][0]["id"], direct_id)
                    self.assertEqual(direct["results"][0]["match_mode"], "phrase")
                    self.assertEqual(direct["results"][0]["matched_expansions"], [])

            for item_type in ("element", "criteria", "ksa"):
                reviewed = server.search_ncs(
                    "성과평가", scope=item_type, limit=20
                )
                reviewed_row = next(
                    row for row in reviewed["results"] if row["id"] == 600
                )
                self.assertEqual(
                    reviewed_row["match_mode"], "expanded_token_and"
                )
                self.assertEqual(
                    reviewed_row["matched_expansions"][0]["matched_as"],
                    "인사평가",
                )

        assert_contract()
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        assert_contract()

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
            "법인카드 사용 내역": ("U_FUND", "자금관리"),
            "세금계산서 발행": ("U_VAT", "부가가치세 신고"),
            "임직원 보안 점검": ("U_SECURITY", "총무보안관리"),
            "시설관리 용역 계약": ("U_OUTSOURCE", "용역관리"),
            "프레젠테이션 자료 제작": ("U_OFFICE_AUTO", "사무자동화 프로그램 활용"),
            "발표 자료 요청": ("U_OFFICE_AUTO", "사무자동화 프로그램 활용"),
            "손익분기점 분석": ("U_COST", "원가관리"),
            "사내강사 준비": ("U_TEACH", "교수활동 수행"),
            "평가문항 개발": ("U_CURRICULUM", "교육과정 개발"),
            "교육 성과 지표 점검": ("U_EDU_PLAN", "교육운영기획"),
            "LMS 운영 점검": ("U_EDU_RESOURCE", "교육자원관리"),
            "학습조직 활성화": ("U_LEARN_ORG", "학습조직구축"),
            "학습 동아리 운영": ("U_LEARN_ORG", "학습조직구축"),
            "4대보험 취득": ("U_PAY", "급여지급"),
            "연말정산 안내": ("U_WITHHOLD", "원천징수"),
            "인건비 예산 수립": ("U_HR_PLAN", "인사기획"),
            "노동관계법 교육": ("U_LABOR_CONFLICT", "노사갈등 해결"),
            "교섭안 마련": ("U_BARGAIN", "단체교섭준비"),
            "법인 인감 관리": ("U_ADMIN_SUPPORT", "업무지원"),
            "부서 일정 관리": ("U_OFFICE_ADMIN", "사무행정 업무 관리"),
            "연결재무제표 검토": ("U_COMBINE", "사업결합회계"),
            "비영리법인 결산": ("U_NPO", "비영리회계"),
            "비영리 회계 결산": ("U_NPO", "비영리회계"),
            "교육 프로그램 콘텐츠 제작": ("U_CURRICULUM", "교육과정 개발"),
            "근태 관리": ("U_PAY", "급여지급"),
            "인력 수급 계획": ("U_HR_PLAN", "인사기획"),
            "교육 수요 조사": ("U_EDU_SYSTEM", "교육체계 수립"),
            "문서 보관 폐기": ("U_DOC_ADMIN", "총무문서관리"),
            "출장 증명서 발급": ("U_ADMIN_SUPPORT", "업무지원"),
            "임금피크제 검토": ("U_WAGE", "임금관리"),
            "교육 참여율 집계": ("U_EDU_EVAL", "교육성과 평가"),
            "강사 섭외": ("U_EDU_RESOURCE", "교육자원관리"),
            "사무실 이전": ("U_ADMIN_SUPPORT", "업무지원"),
            "연간 행사 일정": ("U_EVENT", "행사지원관리"),
            "자금 수지 계획": ("U_FUND", "자금관리"),
            "인사전략 수립": ("U_HR_PLAN", "인사기획"),
            "직무 등급 산정": ("U_JOB", "직무관리"),
            "배치전환 인원": ("U_MOVE", "인력이동관리"),
            "협약 체결": ("U_BARGAIN_RUN", "단체교섭"),
            "취업규칙 변경": ("U_AGREE", "단체협약이행"),
            "자료 보안 관리": ("U_DOCS", "자료 관리"),
            "경비원 고객 응대": ("U_GUARD_CUST", "경비고객관계관리"),
            "자동차 사고 현장 조사": ("U_AUTO_ACCIDENT", "차량사고 현장조사"),
            "교육과정 설계 계획": ("U_CURRICULUM_DESIGN", "교육과정 설계"),
            "얼굴 윤곽 메이크업": ("U_MAKEUP", "베이스 메이크업"),
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

    def test_generic_usage_token_does_not_bury_definition_evidence(self) -> None:
        result = server.search_ncs("법인카드 사용 내역 관리", scope="unit", limit=5)
        ids = [row["id"] for row in result["results"]]
        self.assertIn("U_FUND", ids[:3])
        self.assertNotEqual(ids[0], "U_USE_NOISE")

    def test_soft_scope_coverage_prior_prefers_multi_token_evidence(self) -> None:
        from ncs_mcp.search.core import _rerank_ncs_unit_soft_scope_and_diversity

        weak = {
            "id": "U_WEAK",
            "type": "unit",
            "_match_tier": 3,
            "_classification_codes": {
                "major_code": "11",
                "middle_code": "01",
                "small_code": "01",
                "sub_code": "01",
            },
            "_search_fields": {
                "unit_name": "경비계획",
                "alias": "",
                "classification": "시설",
                "definition": "경비 업무",
            },
        }
        strong = {
            "id": "U_STRONG",
            "type": "unit",
            "_match_tier": 3,
            "_classification_codes": {
                "major_code": "02",
                "middle_code": "01",
                "small_code": "01",
                "sub_code": "01",
            },
            "_search_fields": {
                "unit_name": "용역관리",
                "alias": "",
                "classification": "총무",
                "definition": "시설관리 용역 계약을 관리한다",
            },
        }
        reranked = _rerank_ncs_unit_soft_scope_and_diversity(
            [weak, strong],
            ["청소", "경비", "용역", "계약"],
            {},
            {"청소": 0.5, "경비": 0.5, "용역": 0.9, "계약": 0.8},
            classification_filter=None,
        )
        self.assertEqual(reranked[0]["id"], "U_STRONG")
        unchanged = _rerank_ncs_unit_soft_scope_and_diversity(
            [weak, strong],
            ["청소", "경비", "용역", "계약"],
            {},
            {},
            classification_filter={"major_code": "11"},
        )
        self.assertEqual([item["id"] for item in unchanged], ["U_WEAK", "U_STRONG"])

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
        self.assertEqual(
            search_core._ncs_search_intent_expansions("병원 직원 급여와 4대보험"),
            [],
        )
        self.assertEqual(
            search_core._ncs_search_intent_expansions("4대보험 취득"),
            ["급여지급"],
        )
        self.assertEqual(
            search_core._ncs_search_intent_expansions("조업 인력 수급 계획"),
            [],
        )
        self.assertEqual(
            search_core._ncs_search_intent_expansions("사회복지 교육 수요 조사"),
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

    def test_unit_evidence_ranking_is_stable_across_limits_and_pages(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, '', '4', 1)",
                [(f"WINDOW_{i:02}", f"sensor candidate {i:02}") for i in range(61)],
            )
            conn.execute("INSERT INTO competency_elements VALUES (500, 'sensor testing', 'WINDOW_49')")
            conn.execute("INSERT INTO performance_criteria VALUES (500, 'sensor diagnostics', NULL, 500)")
            conn.commit()
        full = server.search_ncs("sensor diagnostics", scope="unit", limit=100)
        self.assertEqual(full["results"][0]["id"], "WINDOW_49")
        expected = [row["id"] for row in full["results"]]
        for limit in (1, 3, 5, 20, 50):
            for offset in (0, 3, 49, 50, 60):
                with self.subTest(limit=limit, offset=offset):
                    page = server.search_ncs("sensor diagnostics", scope="unit", limit=limit, offset=offset)
                    self.assertEqual([row["id"] for row in page["results"]], expected[offset:offset + limit])
                    self.assertEqual(page["next_offset"], offset + limit if offset + limit < len(expected) else None)

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

        for relative in ("search/__init__.py", "search/core.py", "search/normalization.py", "search/typo.py"):
            with self.subTest(relative=relative):
                local_search = ROOT / "src" / "ncs_mcp" / relative
                vercel_search = (
                    ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / relative
                )
                self.assertEqual(local_search.read_bytes(), vercel_search.read_bytes())

    def test_morphology_particles_are_bounded_and_do_not_rewrite_query(self) -> None:
        variants = search_core._ncs_search_morphology_expansions(
            ["장비를", "센서는", "설비의", "공정으로", "전원을"]
        )
        self.assertEqual(variants, {
            "장비를": ["장비"], "센서는": ["센서"],
            "설비의": ["설비"], "공정으로": ["공정"],
        })
        for token in ("사과", "관리", "장비을", "공정를", "API를", "UNIT_01", "%"):
            with self.subTest(token=token):
                self.assertEqual(search_core._ncs_search_morphology_expansions([token]), {})
        self.assertEqual(search_core._ncs_search_morphology_expansions(["기술로"]), {"기술로": ["기술"]})

    def test_morphology_fills_empty_results_with_trace_and_storage_parity(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('M_FILL', '센서 장비', '', '4', 1)")
            conn.execute("INSERT INTO competency_elements VALUES (150, '센서 장비', 'M_FILL')")
            conn.execute("INSERT INTO performance_criteria VALUES (150, '센서 장비', NULL, 150)")
            conn.execute("INSERT INTO ksa_items VALUES (150, 'knowledge', '센서 장비', NULL, 150)")
            conn.commit()
        query = "센서를 장비로"
        with patch.object(search_core, "_ncs_search_morphology_expansions", return_value={}):
            baseline = server.search_ncs(query, scope="all", limit=10)
        self.assertEqual(baseline["returned"], 0)
        result = server.search_ncs(query, scope="all", limit=10)
        self.assertEqual(result["returned"], 4)
        self.assertEqual(result["normalized_query"], query)
        self.assertEqual(result["query_tokens"], ["센서를", "장비로"])
        self.assertEqual(result["match_mode"], "morphology_fill")
        for row in result["results"]:
            self.assertEqual(row["matched_tokens"], ["센서를", "장비로"])
            self.assertEqual([item["matched_as"] for item in row["matched_expansions"]], ["센서", "장비"])
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        self.assertEqual(server.search_ncs(query, scope="all", limit=10), result)
        self.assertEqual(server.search_ncs(query, scope="unit", classification_filter={"major_code": "99"})["returned"], 0)

    def test_morphology_fill_preserves_original_or_prefix_across_scopes_and_pages(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('M_OR', '센서를 검토', '', '4', 1)")
            conn.execute("INSERT INTO competency_elements VALUES (150, '센서를 검토', 'M_OR')")
            conn.execute("INSERT INTO performance_criteria VALUES (150, '센서를 검토', NULL, 150)")
            conn.execute("INSERT INTO ksa_items VALUES (150, 'knowledge', '센서를 검토', NULL, 150)")
            for index in range(8):
                conn.execute("INSERT INTO competency_units VALUES (?, '센서 장비', '', '4', 1)", (f"M_FILL_{index}",))
            conn.commit()
        for scope in ("unit", "all"):
            query = "센서를 장비로"
            with patch.object(search_core, "_ncs_search_morphology_expansions", return_value={}):
                baseline = server.search_ncs(query, scope=scope, limit=30)
            actual = server.search_ncs(query, scope=scope, limit=30)
            self.assertEqual(actual["results"][:baseline["returned"]], baseline["results"])
            self.assertEqual(actual["returned"], baseline["returned"] + 8)
            self.assertEqual(actual["match_mode"], "mixed")
            pages = []
            offset = 0
            while offset is not None:
                page = server.search_ncs(query, scope=scope, limit=2, offset=offset)
                pages.extend(page["results"])
                offset = page["next_offset"]
            self.assertEqual(pages, actual["results"])
            self.assertEqual(len({(row["type"], row["id"]) for row in pages}), len(pages))

    def test_morphology_never_runs_after_strong_tier_or_full_or_page(self) -> None:
        with self._open_db() as conn:
            conn.executemany("INSERT INTO competency_units VALUES (?, ?, '', '4', 1)", (
                ("M_STRONG", "센서를 장비로",), ("M_FILL", "센서 장비",),
            ))
            conn.commit()
        for query, mode in (("센서를 장비로", "phrase"), ("장비로 센서를", "token_and")):
            self.sql_statements.clear()
            actual = server.search_ncs(query, scope="unit", limit=10)
            self.assertEqual(actual["match_mode"], mode)
            self.assertFalse(any("4 AS match_tier" in sql for sql in self.sql_statements))
            with patch.object(search_core, "_ncs_search_morphology_expansions", return_value={}):
                self.assertEqual(server.search_ncs(query, scope="unit", limit=10), actual)
        with self._open_db() as conn:
            conn.execute("UPDATE competency_units SET unit_name_raw = '센서를 검토' WHERE unit_code = 'M_STRONG'")
            conn.execute("INSERT INTO competency_units VALUES ('M_OR_2', '센서를 검사', '', '4', 1)")
            conn.commit()
        self.sql_statements.clear()
        actual = server.search_ncs("센서를 장비로", scope="unit", limit=1)
        self.assertEqual(actual["match_mode"], "token_or")
        self.assertFalse(any("4 AS match_tier" in sql for sql in self.sql_statements))

    def test_morphology_requires_all_terms_and_rejects_generic_only_stems(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('M_GENERIC', '관리 운영', '', '4', 1)")
            conn.execute("INSERT INTO competency_units VALUES ('M_PARTIAL', '센서', '', '4', 1)")
            conn.commit()
        for query in ("관리의 운영을", "센서를 장비로"):
            with self.subTest(query=query):
                with patch.object(search_core, "_ncs_search_morphology_expansions", return_value={}):
                    baseline = server.search_ncs(query, scope="unit")
                self.assertEqual(server.search_ncs(query, scope="unit"), baseline)

    def test_server_reexports_search_package_entrypoint(self) -> None:
        from ncs_mcp.search import search_ncs as package_search_ncs

        self.assertIs(server.search_ncs, package_search_ncs)

    def test_morphology_compound_uses_direct_name_and_preserves_full_definition_hit(self) -> None:
        with self._open_db() as conn:
            conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?, '4', 1)", (
                ("M_COMPOUND_NAME", "광학교정", "장치 검사"),
                ("M_FULL_DEFINITION", "기타수행", "광학교정업무"),
                ("M_BASE_DEFINITION", "기록보관", "광학교정"),
                ("M_EMBEDDED_NAME", "초광학교정", "장치 검사"),
            ))
            conn.commit()
        query = "광학교정업무를"
        with patch.object(search_core, "_ncs_search_morphology_compound_bases", return_value=[]):
            before = server.search_ncs(query, scope="unit", limit=10)
        self.assertEqual([row["id"] for row in before["results"]], ["M_FULL_DEFINITION"])
        result = server.search_ncs(query, scope="unit", limit=10)
        self.assertEqual([row["id"] for row in result["results"]], ["M_COMPOUND_NAME", "M_FULL_DEFINITION"])
        self.assertEqual(result["results"][1], before["results"][0])
        self.assertEqual(result["results"][0]["matched_expansions"], [{
            "token": query, "matched_as": "광학교정", "match_fields": ["unit_name"],
        }])
        self.assertEqual(result["query_tokens"], [query])
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        self.assertEqual(server.search_ncs(query, scope="unit", limit=10), result)
        self.assertEqual(server.search_ncs(query, scope="unit", classification_filter={"major_code": "99"})["returned"], 0)

    def test_morphology_compound_uses_only_existing_alias_on_its_own_unit(self) -> None:
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('M_FOREIGN_ALIAS', '대상선발', '인력채용', '4', 1)")
            conn.execute("UPDATE competency_units SET api_definition = '인력채용 절차' WHERE unit_code = 'U_HIRE_1'")
            conn.commit()
        query = "인력채용관리를"
        result = server.search_ncs(query, scope="unit", limit=10)
        self.assertEqual([row["id"] for row in result["results"]], ["U_HIRE_1"])
        self.assertEqual(result["results"][0]["matched_expansions"], [{
            "token": query, "matched_as": "인력채용", "match_fields": ["alias"],
        }])
        with self._open_db() as conn:
            self._add_normalized_columns(conn)
            conn.commit()
        self.assertEqual(server.search_ncs(query, scope="unit", limit=10), result)

    def test_morphology_compound_preserves_all_lexical_prefixes_and_sql_count(self) -> None:
        with self._open_db() as conn:
            conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?, '4', 1)", (
                ("M_COMPOUND_NAME", "광학교정", "장비"),
                ("M_ORIGINAL", "광학교정업무를 검토", "검토"),
            ))
            conn.execute("INSERT INTO competency_elements VALUES (200, '광학교정업무를 검토', 'M_ORIGINAL')")
            conn.execute("INSERT INTO performance_criteria VALUES (200, '광학교정업무를 검토', NULL, 200)")
            conn.commit()
        for query in ("광학교정업무를", "검토 광학교정업무를", "광학교정업무를 장비로"):
            for scope in ("unit", "all"):
                with self.subTest(query=query, scope=scope):
                    # Warm the per-process unit lexicon so both runs issue
                    # the same SQL.
                    server.search_ncs(query, scope=scope, limit=20)
                    self.sql_statements.clear()
                    with patch.object(search_core, "_ncs_search_morphology_compound_bases", return_value=[]):
                        before = server.search_ncs(query, scope=scope, limit=20)
                    statement_count = len(self.sql_statements)
                    self.sql_statements.clear()
                    after = server.search_ncs(query, scope=scope, limit=20)
                    self.assertEqual(len(self.sql_statements), statement_count)
                    original = [row for row in before["results"] if row["match_mode"] != "morphology_fill"]
                    self.assertEqual(after["results"][:len(original)], original)
                    if before["match_mode"] in ("phrase", "token_and"):
                        self.assertEqual(after, before)

    def test_morphology_compound_rejects_generic_roots_and_keeps_all_query_terms(self) -> None:
        self.assertEqual(search_core._ncs_search_morphology_compound_bases("관리업무"), [])
        self.assertEqual(search_core._ncs_search_morphology_compound_bases("광학교정관리소"), [])
        self.assertEqual(search_core._ncs_search_morphology_compound_bases("광학교정업무"), ["광학교정"])
        with self._open_db() as conn:
            conn.execute("INSERT INTO competency_units VALUES ('M_COMPOUND_NAME', '광학교정', '장비 검사', '4', 1)")
            conn.commit()
        self.assertEqual(server.search_ncs("광학교정업무를 희귀재료를", scope="unit")["returned"], 0)
        self.assertEqual(server.search_ncs("광학교정업무를", scope="element")["returned"], 0)


class NcsSearchHybridRecallTests(NcsSearchRecallTests):
    """Run the same ranking/Unicode/evidence contract against v2 storage."""

    def test_prefix_indexes_preserve_payload_pages_and_manifest_fallback(self) -> None:
        from ncs_mcp.search.prefix_index import PREFIX_FTS_REQUIRED_MANIFEST, prefix_fts_document

        texts = ("alpha beta", "ab short term", "alpha remedy", "xxalphabeta",
                 "ＡＬＰＨＡ Straße", "cafe\u0301 école", "가나 출입", "인사 채용",
                 "alpha hr", "C++ r&d", "alpha_beta", "alpha—beta")
        with self._open_db() as conn:
            for index, value in enumerate(texts, 100):
                conn.execute("INSERT INTO ksa_items VALUES (?, 'knowledge', ?, ?, 5)",
                             (index, value if index % 2 else "원문 근거", value))
                conn.execute("INSERT INTO performance_criteria VALUES (?, ?, ?, 5)",
                             (index, value if index % 2 else "원문 과업", value))
            self._add_normalized_columns(conn)
            conn.commit()
        cases = [
            dict(query=query, scope=scope, classification_filter=scope_filter, limit=3, offset=offset)
            for query in ("alpha", "alpha hr", "ab", "a", "ALPHA", "café", "strasse",
                          "가나", "채용", "C++", "alpha%beta", "alpha_beta", "없는검색")
            for scope in ("all", "criteria", "ksa")
            for scope_filter in (None, {"major_code": "02"}, {"major_code": "15"})
            for offset in (0, 3)
        ]
        baseline = [server.search_ncs(**case) for case in cases]
        with self._open_db() as conn:
            for table, query in (
                ("ksa_prefix_fts", "SELECT ksa_id, COALESCE(ksa_text_raw_search_override,ksa_text_raw,''), COALESCE(ksa_text_refined_search_override,ksa_text_refined,'') FROM ksa_items"),
                ("criteria_prefix_fts", "SELECT criteria_id, criteria_text_raw_search_norm, criteria_text_refined_search_norm FROM performance_criteria"),
            ):
                conn.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(search_prefixes, content='', detail='none', columnsize=0, tokenize='ascii')")
                rows = conn.execute(query).fetchall()
                conn.executemany(f"INSERT INTO {table}(rowid,search_prefixes) VALUES (?,?)",
                                 ((row[0], prefix_fts_document(row[1], row[2])) for row in rows))
            conn.executemany("INSERT INTO serving_snapshot_manifest VALUES (?,?)", PREFIX_FTS_REQUIRED_MANIFEST.items())
            self.assertTrue(search_core._compact_lexical_prefix_available(conn, "v2"))
            self.assertFalse(search_core._compact_lexical_prefix_available(conn, True))
            conn.commit()
        self.sql_statements.clear()
        self.assertEqual(baseline, [server.search_ncs(**case) for case in cases])
        self.assertTrue(any("ksa_prefix_fts MATCH" in sql for sql in self.sql_statements))
        self.assertTrue(any("criteria_prefix_fts MATCH" in sql for sql in self.sql_statements))
        for key, value in PREFIX_FTS_REQUIRED_MANIFEST.items():
            with self._open_db() as conn:
                conn.execute("UPDATE serving_snapshot_manifest SET manifest_value='unsupported' WHERE manifest_key=?", (key,))
                self.assertFalse(search_core._compact_lexical_prefix_available(conn, "v2"))
                conn.commit()
            self.sql_statements.clear()
            self.assertEqual(baseline[0], server.search_ncs(**cases[0]))
            self.assertFalse(any("prefix_fts MATCH" in sql for sql in self.sql_statements))
            with self._open_db() as conn:
                conn.execute("UPDATE serving_snapshot_manifest SET manifest_value=? WHERE manifest_key=?", (value, key))
                conn.commit()
        with self._open_db() as conn:
            conn.execute("DROP TABLE criteria_prefix_fts")
            self.assertFalse(search_core._compact_lexical_prefix_available(conn, "v2"))
            conn.commit()
        self.assertEqual(baseline[0], server.search_ncs(**cases[0]))

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

    def test_v2_ksa_trigram_index_preserves_full_search_payload(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO ksa_items VALUES (?, 'knowledge', ?, ?, 5)",
                (
                    (100, "alpha beta", None),
                    (101, "delta epsilon", "alpha remedy"),
                    (102, "\uff21\uff2c\uff30\uff28\uff21", None),
                    (103, "ab short term", None),
                    (104, "채용교육 과정 운영", None),
                ),
            )
            self._add_normalized_columns(conn)
            conn.commit()

        cases = [
            (query, scope, scope_filter)
            for query in (
                "alpha", "beta", "alpha beta", "delta", "ab", "ALPHA",
                "alpha remedy", "alp%ha", "alpha_beta", "채용교육",
            )
            for scope in ("all", "ksa")
            for scope_filter in (None, {"major_code": "02"})
        ]
        baseline = [
            server.search_ncs(query, scope=scope, limit=15, classification_filter=scope_filter)
            for query, scope, scope_filter in cases
        ]
        with self._open_db() as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE ksa_search_fts USING fts5("
                "search_text, content='', detail='none', columnsize=0, tokenize='trigram')"
            )
            conn.execute(
                "INSERT INTO ksa_search_fts(rowid, search_text) "
                "SELECT ksa_id, COALESCE(ksa_text_raw_search_override, ksa_text_raw, '') "
                "|| ' ' || COALESCE(ksa_text_refined_search_override, ksa_text_refined, '') "
                "FROM ksa_items"
            )
            conn.execute(
                "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                ("ksa_search_fts_schema", "ncs_ksa_search_fts_v1"),
            )
            conn.commit()
        self.sql_statements.clear()
        candidate = [
            server.search_ncs(query, scope=scope, limit=15, classification_filter=scope_filter)
            for query, scope, scope_filter in cases
        ]
        self.assertEqual(candidate, baseline)
        self.assertTrue(any("ksa_search_fts MATCH" in sql for sql in self.sql_statements))
        self.sql_statements.clear()
        server.search_ncs("ab", scope="ksa", limit=15)
        self.assertFalse(any("ksa_search_fts MATCH" in sql for sql in self.sql_statements))
        with self._open_db() as conn:
            conn.execute("DELETE FROM serving_snapshot_manifest WHERE manifest_key = 'ksa_search_fts_schema'")
            conn.commit()
        self.sql_statements.clear()
        self.assertEqual(server.search_ncs("alpha", scope="ksa", limit=15), baseline[2])
        self.assertFalse(any("ksa_search_fts MATCH" in sql for sql in self.sql_statements))

    def test_v2_joined_compound_uses_normalized_name_only_with_guards(self) -> None:
        with self._open_db() as conn:
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, '4', ?)",
                (
                    ("V2_LITERAL", "급여 지급 안내", "", 1),
                    ("V2_JOINED", "급여지급", "", 1),
                    ("V2_PREFIX", "급여지급검토", "", 1),
                    ("V2_INTERNAL", "초급여지급", "", 1),
                    ("V2_DEFINITION", "기타수행", "급여지급", 1),
                    ("V2_OTHER_MAJOR", "급여지급외부", "", 2),
                ),
            )
            self._add_normalized_columns(conn)
            conn.commit()

        query = "급여 지급"
        classification_filter = {"major_code": "02"}
        # The shared typo/name lexicon has a one-time corpus read. Compare
        # the joined-compound paths after that cache is warm in both calls.
        with self._open_db() as conn:
            search_core._ncs_search_unit_lexicon(conn)
        self.sql_statements.clear()
        with patch.object(
            search_core, "_ncs_search_joined_compound_phrase", return_value=""
        ):
            baseline = server.search_ncs(
                query,
                scope="unit",
                limit=10,
                classification_filter=classification_filter,
            )
        baseline_statement_count = len(self.sql_statements)

        self.sql_statements.clear()
        result = server.search_ncs(
            query,
            scope="unit",
            limit=10,
            classification_filter=classification_filter,
        )
        self.assertEqual(len(self.sql_statements), baseline_statement_count)
        self.assertTrue(
            any("unit_name_search_norm" in sql for sql in self.sql_statements)
        )
        self.assertTrue(
            any("ncs_search_match_normalized" in sql for sql in self.sql_statements)
        )
        self.assertEqual(result["results"][:baseline["returned"]], baseline["results"])
        # Shared-fixture U_PAY also owns the official 급여지급 name and ranks
        # with the joined recovery hits under major_code=02.
        self.assertEqual(
            [row["id"] for row in result["results"]],
            ["V2_LITERAL", "U_PAY", "V2_JOINED", "V2_PREFIX"],
        )
        self.assertNotIn(
            "V2_INTERNAL", [row["id"] for row in result["results"]]
        )
        self.assertNotIn(
            "V2_DEFINITION", [row["id"] for row in result["results"]]
        )
        joined = result["results"][2]
        self.assertEqual(joined["matched_tokens"], ["급여", "지급"])
        self.assertEqual(joined["match_fields"], ["unit_name"])
        self.assertEqual(joined["matched_expansions"][0]["matched_as"], "급여지급")
        other_major = server.search_ncs(
            query,
            scope="unit",
            limit=10,
            classification_filter={"major_code": "99"},
        )
        self.assertEqual(
            [row["id"] for row in other_major["results"]],
            ["V2_OTHER_MAJOR"],
        )

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
