from __future__ import annotations

import asyncio
from contextlib import contextmanager
from itertools import product
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import types


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.db import connect, initialize_database  # noqa: E402
from ncs_mcp.training_recommendation import (  # noqa: E402
    PUBLIC_TRAINING_COURSE_FIELDS,
    _course_payload,
)


NOW = "2026-08-29T00:00:00+00:00"
RANK_QUERY = "데이터분석"
ONTOLOGY_QUERY = "분석 역량"
EXACT_UNIT = "TST_EXACT"
PREFIX_UNIT = "TST_PREFIX"
PARTIAL_UNIT = "TST_PARTIAL"
CLASSIFICATION_UNIT = "TST_CLASS"
DEFINITION_UNIT = "TST_DEFINITION"
DUTY_DEFINITION = "데이터 기반 인사 직무의 범위와 책임을 정의한다."
PRIVATE_SENTINEL = "PRIVATE_SOURCE_AND_EVIDENCE_MUST_NOT_LEAK"
MAX_PUBLIC_PAYLOAD_CHARS = 8_000
MAX_MARKDOWN_TEXT_CHARS = {
    "ncs_search": 1_300,
    "ncs_unit_detail": 3_000,
    "ncs_training": 1_200,
    "ncs_analysis": 1_300,
}
SOURCE_FOOTER = "출처: 한국산업인력공단 NCS (공공데이터포털). 표준 원문: ncs.go.kr"


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _insert_classification(
    conn: sqlite3.Connection,
    *,
    major_code: str,
    major_name: str,
    sub_code: str,
    sub_name: str,
    duty_definition: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name,
            duty_def_api, duty_order
        ) VALUES (?, ?, '01', '중분류', '01', '소분류', ?, ?, ?, '1')
        """,
        (major_code, major_name, sub_code, sub_name, duty_definition),
    )
    return int(cursor.lastrowid)


def _insert_unit(
    conn: sqlite3.Connection,
    *,
    unit_code: str,
    unit_name: str,
    classification_id: int,
    definition: str,
    element_count: int = 1,
    evidence_rows_per_element: int = 0,
) -> list[int]:
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_definition,
            api_match_status, created_at, updated_at
        ) VALUES (?, ?, 'v1', ?, '5', ?, ?, 'matched', ?, ?)
        """,
        (unit_code, unit_code, unit_name, classification_id, definition, NOW, NOW),
    )
    element_ids: list[int] = []
    for element_index in range(1, element_count + 1):
        cursor = conn.execute(
            """
            INSERT INTO competency_elements(
                unit_code, element_no, element_code_raw, element_name_raw,
                element_level_raw, api_element_name, api_element_level,
                api_match_status
            ) VALUES (?, ?, ?, ?, '5', ?, '5', 'matched')
            """,
            (
                unit_code,
                str(element_index),
                f"{unit_code}-{element_index}",
                f"능력단위요소 {element_index}",
                f"능력단위요소 {element_index}",
            ),
        )
        element_id = int(cursor.lastrowid)
        element_ids.append(element_id)
        for evidence_index in range(1, evidence_rows_per_element + 1):
            evidence_suffix = "구체적 업무 상황과 판단 근거를 설명한다. " * 3
            conn.execute(
                """
                INSERT INTO performance_criteria(
                    element_id, criteria_no, criteria_text_raw
                ) VALUES (?, ?, ?)
                """,
                (
                    element_id,
                    str(evidence_index),
                    f"수행준거 {element_index}.{evidence_index}: {evidence_suffix}",
                ),
            )
            conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?, '02', 'skill', ?, ?)
                """,
                (
                    element_id,
                    str(evidence_index),
                    f"직무 기술 {element_index}.{evidence_index}: {evidence_suffix}",
                ),
            )
    return element_ids


def _insert_concept(
    conn: sqlite3.Connection,
    *,
    name: str,
    normalized_key: str,
    definition: str = "",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type, definition,
            definition_status, relation_status, review_status,
            created_at, updated_at
        ) VALUES (?, ?, 'skill', ?, 'candidate', 'linked', 'candidate', ?, ?)
        """,
        (name, normalized_key, definition, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _seed_public_contract_fixture(conn: sqlite3.Connection) -> dict[str, object]:
    base_classification = _insert_classification(
        conn,
        major_code="01",
        major_name="경영 지원",
        sub_code="01",
        sub_name="인사 관리",
        duty_definition=DUTY_DEFINITION,
    )
    classification_match = _insert_classification(
        conn,
        major_code="02",
        major_name=f"{RANK_QUERY} 분야",
        sub_code="02",
        sub_name="분류 일치",
        duty_definition="분류명으로 일치하는 직무이다.",
    )
    definition_match = _insert_classification(
        conn,
        major_code="03",
        major_name="전략 지원",
        sub_code="03",
        sub_name="정의 일치",
        duty_definition="정의문으로 일치하는 직무이다.",
    )

    unit_specs = [
        (EXACT_UNIT, RANK_QUERY, base_classification, "정확명 일치 단위"),
        (PREFIX_UNIT, f"{RANK_QUERY} 기획", base_classification, "접두 일치 단위"),
        (PARTIAL_UNIT, f"인사 {RANK_QUERY} 실무", base_classification, "부분 일치 단위"),
        (CLASSIFICATION_UNIT, "조직 역량 설계", classification_match, "분류명 일치 단위"),
        (
            DEFINITION_UNIT,
            "전략 지원",
            definition_match,
            f"업무에서 {RANK_QUERY}을 활용한다.",
        ),
    ]
    unit_codes: list[str] = []
    element_ids_by_unit: dict[str, list[int]] = {}
    for unit_code, unit_name, classification_id, definition in unit_specs:
        element_ids_by_unit[unit_code] = _insert_unit(
            conn,
            unit_code=unit_code,
            unit_name=unit_name,
            classification_id=classification_id,
            definition=definition,
            element_count=10 if unit_code == EXACT_UNIT else 1,
            evidence_rows_per_element=8 if unit_code == EXACT_UNIT else 0,
        )
        unit_codes.append(unit_code)

    for index in range(7):
        unit_code = f"TST_LINK_{index:02d}"
        unit_codes.append(unit_code)
        element_ids_by_unit[unit_code] = _insert_unit(
            conn,
            unit_code=unit_code,
            unit_name=f"연결 검증 단위 {index}",
            classification_id=base_classification,
            definition="링크 페이로드 검증용 단위이다.",
        )

    concept_ids = [
        _insert_concept(
            conn,
            name=f"연결 개념 {index}",
            normalized_key=f"linkconcept{index}",
        )
        for index in range(12)
    ]

    exact_concept_id = _insert_concept(
        conn,
        name=ONTOLOGY_QUERY,
        normalized_key="analysiscompetencyexact",
    )
    _insert_concept(
        conn,
        name=f"{ONTOLOGY_QUERY} 심화",
        normalized_key="analysiscompetencyprefix",
    )
    _insert_concept(
        conn,
        name=f"인사 {ONTOLOGY_QUERY} 활용",
        normalized_key="analysiscompetencypartial",
    )
    alias_concept_id = _insert_concept(
        conn,
        name="통계 해석 역량",
        normalized_key="statisticsinterpretation",
    )
    _insert_concept(
        conn,
        name="정의문 전용 역량",
        normalized_key="definitiononlycompetency",
        definition=f"이 역량은 {ONTOLOGY_QUERY}을 지원한다.",
    )
    conn.execute(
        """
        INSERT INTO ontology_concept_aliases(
            concept_id, alias_text, normalized_alias_key, alias_source, created_at
        ) VALUES (?, ?, 'analysiscompetencyalias', 'test', ?)
        """,
        (alias_concept_id, ONTOLOGY_QUERY, NOW),
    )

    course_ids: list[int] = []
    course_units = (EXACT_UNIT, PREFIX_UNIT, PARTIAL_UNIT)
    for index, unit_code in enumerate(course_units, start=1):
        cursor = conn.execute(
            """
            INSERT INTO ncs_training_courses(
                ncs_cl_cd, compe_unit_name, compe_unit_level,
                ncs_lclas_cd, ncs_lclas_cdnm,
                ncs_mclas_cd, ncs_mclas_cdnm,
                ncs_sclas_cd, ncs_sclas_cdnm,
                ncs_subd_cd, ncs_subd_cdnm,
                train_goal, train_time, fac_name, meth_name,
                source_payload, api_fetched_at
            ) VALUES (?, ?, '5', '01', '경영 지원', '01', '중분류',
                      '01', '소분류', '01', '인사 관리', ?, '8',
                      '실습실', '실습', ?, ?)
            """,
            (
                unit_code,
                f"훈련과정 {index}",
                f"직무 수행 역량 {index}을 개발한다.",
                PRIVATE_SENTINEL,
                NOW,
            ),
        )
        course_ids.append(int(cursor.lastrowid))

    link_course_id = course_ids[0]
    for index, (unit_code, concept_id) in enumerate(zip(unit_codes, concept_ids)):
        element_id = element_ids_by_unit[unit_code][0]
        conn.execute(
            """
            INSERT INTO ncs_training_course_unit_links(
                training_course_id, unit_code, link_method,
                confidence_score, review_status, created_at, updated_at
            ) VALUES (?, ?, 'test_unit_link', 0.9, 'auto_linked', ?, ?)
            """,
            (link_course_id, unit_code, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO ncs_training_course_concept_links(
                training_course_id, unit_code, concept_id, link_method,
                confidence_score, evidence_text, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'test_concept_link', 0.8, ?, 'auto_linked', ?, ?)
            """,
            (link_course_id, unit_code, concept_id, PRIVATE_SENTINEL, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO ncs_training_course_element_links(
                training_course_id, unit_code, element_id, link_method,
                confidence_score, evidence_text, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'test_element_link', 0.8, ?, 'auto_linked', ?, ?)
            """,
            (link_course_id, unit_code, element_id, PRIVATE_SENTINEL, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO training_goal_concept_links(
                training_course_id, unit_code, element_id, concept_id, link_method,
                confidence_score, evidence_text, review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'test_goal_link', 0.8, ?, 'auto_linked', ?, ?)
            """,
            (
                link_course_id,
                unit_code,
                element_id,
                concept_id,
                PRIVATE_SENTINEL,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO training_delivery_relations(
                training_course_id, relation_type, relation_value, normalized_value,
                numeric_value, evidence_text, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, 'method', ?, ?, ?, ?, 0.9, 'auto_linked', ?, ?)
            """,
            (
                link_course_id,
                f"전달 방식 {index}",
                f"delivery-{index}",
                float(index),
                PRIVATE_SENTINEL,
                NOW,
                NOW,
            ),
        )

    conn.execute(
        """
        INSERT INTO ncs_qualification_items(
            jm_cd, jm_nm, exam_insti_nm, source_payload, api_fetched_at
        ) VALUES ('Q-001', '데이터분석 자격', '검정기관', ?, ?)
        """,
        (PRIVATE_SENTINEL, NOW),
    )
    conn.execute(
        """
        INSERT INTO ncs_unit_qualification_links(
            unit_code, jm_cd, organ_std_ver_cd, compe_unit_name,
            ablt_unit_typ_cd, ablt_unit_typ_nm, min_edu_trng_tm,
            link_method, confidence_score, source_payload, api_fetched_at,
            review_status, created_at, updated_at
        ) VALUES (?, 'Q-001', 'v1', ?, 'MAND', '필수', 8,
                  'test', 1.0, ?, ?, 'auto_linked', ?, ?)
        """,
        (EXACT_UNIT, RANK_QUERY, PRIVATE_SENTINEL, NOW, NOW, NOW),
    )

    job_base_cursor = conn.execute(
        """
        INSERT INTO ncs_job_base_competencies(
            competency_name, normalized_key, created_at, updated_at
        ) VALUES ('문제해결능력', 'problem-solving', ?, ?)
        """,
        (NOW, NOW),
    )
    job_base_competency_id = int(job_base_cursor.lastrowid)
    factor_cursor = conn.execute(
        """
        INSERT INTO ncs_job_base_factors(
            job_base_competency_id, factor_name, normalized_key,
            created_at, updated_at
        ) VALUES (?, '사고력', 'thinking', ?, ?)
        """,
        (job_base_competency_id, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO ncs_unit_job_base_links(
            unit_code, job_base_competency_id, job_base_factor_id,
            ncs_lclas_cd, ncs_lclas_cdnm, compe_unit_name,
            link_method, confidence_score, source_payload, api_fetched_at,
            review_status, created_at, updated_at
        ) VALUES (?, ?, ?, '01', '경영 지원', ?, 'test', 1.0, ?, ?,
                  'auto_linked', ?, ?)
        """,
        (
            EXACT_UNIT,
            job_base_competency_id,
            int(factor_cursor.lastrowid),
            RANK_QUERY,
            PRIVATE_SENTINEL,
            NOW,
            NOW,
            NOW,
        ),
    )

    for source_row, (job_name, competency_name, unit_code) in enumerate(
        (
            ("인사기획", "인력 데이터 분석", EXACT_UNIT),
            ("생산관리", "공정 운영", PREFIX_UNIT),
        ),
        start=1,
    ):
        conn.execute(
            """
            INSERT INTO ncs_career_paths(
                source_file, source_row_number,
                major_code_raw, middle_code_raw, small_code_raw,
                job_code_raw, job_name, competency_code_raw,
                competency_level_raw, competency_name,
                position_level_raw, position_name,
                major_code, middle_code, small_code, sub_code,
                matched_unit_code, classification_match_method,
                unit_match_method, confidence_score, review_status,
                created_at, updated_at
            ) VALUES ('test.csv', ?, '01', '01', '01', ?, ?, ?,
                      '5', ?, '5', ?, '01', '01', '01', '01', ?,
                      'test', 'test', 1.0, 'candidate', ?, ?)
            """,
            (
                source_row,
                f"JOB-{source_row}",
                job_name,
                f"COMP-{source_row}",
                competency_name,
                f"직위 {source_row}",
                unit_code,
                NOW,
                NOW,
            ),
        )

    conn.commit()
    return {
        "course_ids": course_ids,
        "link_course_id": link_course_id,
        "exact_concept_id": exact_concept_id,
    }


class PublicMcpPayloadContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "ncs.db"
        conn = connect(self.db_path)
        try:
            initialize_database(conn)
            self.seed = _seed_public_contract_fixture(conn)
        finally:
            conn.close()
        self.open_db_patch = patch.object(server, "open_db", new=self._open_fixture_db)
        self.open_db_patch.start()

    def tearDown(self) -> None:
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _open_fixture_db(self):
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _call_tool_wire(self, name: str, arguments: dict[str, object]) -> tuple[dict[str, object], str]:
        async def invoke() -> dict[str, object]:
            handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
            request = types.CallToolRequest(
                params=types.CallToolRequestParams(name=name, arguments=arguments)
            )
            response = await handler(request)
            root = getattr(response, "root", response)
            return root.model_dump(mode="json", exclude_none=True, by_alias=True)

        wire = asyncio.run(invoke())
        text = "".join(
            str(item.get("text", ""))
            for item in wire.get("content", [])
            if isinstance(item, dict)
        )
        return wire, text

    def test_tool_response_keeps_dict_envelope_and_attaches_wire_markdown(self) -> None:
        generated_at = "2026-08-30T00:00:00+00:00"
        response = server.tool_response(
            {
                "ok": False,
                "error": {"code": "NOT_FOUND", "message": "missing"},
                "content": [
                    {
                        "type": "text",
                        "text": "[NOT_FOUND] missing\nLLM은 추측 또는 생성을 하지 마세요.",
                    }
                ],
            },
            data={},
            audit={"generated_at": generated_at},
            ok=False,
        )

        self.assertIsInstance(response, dict)
        self.assertFalse(response["ok"])
        self.assertEqual(response["audit"]["generated_at"], generated_at)
        markdown = server._payload_markdown(response)
        self.assertIsInstance(markdown, str)
        self.assertIn("[NOT_FOUND] missing", markdown)
        self.assertIn("LLM은 추측 또는 생성을 하지 마세요.", markdown)
        self.assertIn(f"audit.generated_at: `{generated_at}`", markdown)
        self.assertTrue(markdown.endswith(SOURCE_FOOTER), markdown)

    def test_ncs_search_ranking_and_duty_definition_boundary(self) -> None:
        response = server.ncs_search(query=RANK_QUERY, scope="unit", limit=5)

        self.assertTrue(response["ok"], response)
        self.assertEqual(
            [item["id"] for item in response["results"]],
            [
                EXACT_UNIT,
                PREFIX_UNIT,
                PARTIAL_UNIT,
                CLASSIFICATION_UNIT,
                DEFINITION_UNIT,
            ],
        )
        for item in response["results"]:
            self.assertNotIn("duty_definition", item["path"])

        detail = server.ncs_unit_detail(unit_code=EXACT_UNIT, include=["elements"])
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(detail["unit"]["classification"]["duty_definition"], DUTY_DEFINITION)

    def test_public_search_training_and_dense_detail_stay_under_budget(self) -> None:
        payloads = {
            "ncs_search(limit=5)": server.ncs_search(
                query=RANK_QUERY,
                scope="unit",
                limit=5,
            ),
            "ncs_training(limit=3)": server.ncs_training(limit=3),
            "ncs_unit_detail(default)": server.ncs_unit_detail(unit_code=EXACT_UNIT),
        }
        for label, payload in payloads.items():
            self.assertLessEqual(
                _json_size(payload),
                MAX_PUBLIC_PAYLOAD_CHARS,
                f"{label} returned {_json_size(payload)} chars",
            )

    def test_unit_detail_meaningful_include_combinations_stay_under_budget(self) -> None:
        structural_variants = (
            ("elements",),
            ("elements", "criteria"),
            ("elements", "ksa"),
            ("elements", "criteria", "ksa"),
        )
        optional_variants = (
            (),
            ("training",),
            ("qualification",),
            ("training", "qualification"),
        )
        for structural, optional in product(structural_variants, optional_variants):
            include = [*structural, *optional]
            with self.subTest(include=include):
                payload = server.ncs_unit_detail(unit_code=EXACT_UNIT, include=include)
                self.assertTrue(payload["ok"], payload)
                self.assertLessEqual(
                    _json_size(payload),
                    MAX_PUBLIC_PAYLOAD_CHARS,
                    f"include={include} returned {_json_size(payload)} chars",
                )

    def test_course_payload_caps_all_five_links_and_exposes_only_whitelists(self) -> None:
        conn = connect(self.db_path)
        try:
            course = conn.execute(
                "SELECT * FROM ncs_training_courses WHERE training_course_id = ?",
                (self.seed["link_course_id"],),
            ).fetchone()
            default_payload = _course_payload(conn, course)
            custom_payload = _course_payload(conn, course, link_limit=3)
        finally:
            conn.close()

        link_whitelists = {
            "unit_links": {
                "unit_code",
                "link_method",
                "confidence_score",
                "review_status",
            },
            "concept_links": {
                "unit_code",
                "concept_id",
                "concept_name",
                "concept_type",
                "link_method",
                "confidence_score",
                "review_status",
            },
            "element_links": {
                "unit_code",
                "element_id",
                "element_name",
                "link_method",
                "confidence_score",
                "review_status",
            },
            "goal_concept_links": {
                "unit_code",
                "element_id",
                "concept_id",
                "concept_name",
                "concept_type",
                "link_method",
                "confidence_score",
                "review_status",
            },
            "delivery_relations": {
                "relation_type",
                "relation_value",
                "normalized_value",
                "numeric_value",
                "confidence_score",
                "review_status",
            },
        }
        self.assertEqual(
            set(default_payload["training_course"]),
            set(PUBLIC_TRAINING_COURSE_FIELDS),
        )
        for link_name, expected_fields in link_whitelists.items():
            self.assertEqual(len(default_payload[link_name]), 10, link_name)
            self.assertEqual(len(custom_payload[link_name]), 3, link_name)
            self.assertEqual(set(custom_payload[link_name][0]), expected_fields, link_name)
            self.assertEqual(
                custom_payload["link_meta"][link_name],
                {"total_count": 12, "returned_count": 3, "truncated": True},
            )

        serialized = json.dumps(custom_payload, ensure_ascii=False)
        self.assertNotIn(PRIVATE_SENTINEL, serialized)
        forbidden_fields = {
            "source_payload",
            "evidence_text",
            "link_id",
            "relation_id",
            "api_fetched_at",
            "created_at",
            "updated_at",
        }

        def assert_no_forbidden_fields(value: object) -> None:
            if isinstance(value, dict):
                self.assertFalse(forbidden_fields.intersection(value), value)
                for nested in value.values():
                    assert_no_forbidden_fields(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_no_forbidden_fields(nested)

        assert_no_forbidden_fields(custom_payload)

    def test_five_compact_tools_have_no_output_schema_or_structured_content(self) -> None:
        compact_tool_names = {
            "ncs_search",
            "ncs_unit_detail",
            "ncs_training",
            "ncs_analysis",
            "recommend_training_for_task",
        }

        async def inspect_tools_and_call_training() -> tuple[dict[str, object], dict[str, object]]:
            tools = {
                tool.name: tool
                for tool in await server.mcp.list_tools()
                if tool.name in compact_tool_names
            }
            handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
            request = types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="ncs_training",
                    arguments={"limit": 3},
                )
            )
            response = await handler(request)
            root = getattr(response, "root", response)
            wire = root.model_dump(mode="json", exclude_none=True, by_alias=True)
            return tools, wire

        tools, wire = asyncio.run(inspect_tools_and_call_training())
        self.assertEqual(set(tools), compact_tool_names)
        for name, tool in tools.items():
            self.assertIsNone(tool.outputSchema, name)
        self.assertNotIn("structuredContent", wire)
        self.assertFalse(wire.get("isError", False), wire)
        content_text = "".join(
            str(item.get("text", ""))
            for item in wire.get("content", [])
            if isinstance(item, dict)
        )
        self.assertLessEqual(len(content_text), 2_000, len(content_text))
        self.assertIn(SOURCE_FOOTER, content_text)

    def test_ncs_search_markdown_stays_under_budget_and_preserves_ids(self) -> None:
        search_wire, search_text = self._call_tool_wire(
            "ncs_search",
            {"query": RANK_QUERY, "scope": "unit", "limit": 5},
        )
        self.assertNotIn("structuredContent", search_wire)
        self.assertLessEqual(len(search_text), MAX_MARKDOWN_TEXT_CHARS["ncs_search"])
        self.assertIn("| 능력단위명 | 수준 | 분류경로 | 능력단위코드 |", search_text)
        self.assertIn("audit.generated_at:", search_text)
        self.assertTrue(search_text.endswith(SOURCE_FOOTER), search_text)
        self.assertRegex(search_text, re.escape(EXACT_UNIT))


    def test_qualification_without_collection_status_remains_usable(self) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("DROP TABLE ncs_qualification_collection_status")
            conn.commit()
        finally:
            conn.close()

        response = server.ncs_analysis(
            mode="qualification",
            query=RANK_QUERY,
            unit_code=EXACT_UNIT,
            limit=3,
        )
        self.assertTrue(response["ok"], response)
        self.assertEqual(len(response["qualification_links"]), 1)
        self.assertFalse(response["summary"]["collection_status_available"])
        self.assertEqual(
            response["summary"]["missing_optional_tables"],
            ["ncs_qualification_collection_status"],
        )

    def test_job_base_job_query_falls_back_to_ranked_unit(self) -> None:
        response = server.ncs_analysis(
            mode="job_base",
            query=RANK_QUERY,
            limit=3,
        )

        self.assertTrue(response["ok"], response)
        self.assertEqual(len(response["job_base_links"]), 1)
        self.assertEqual(response["job_base_links"][0]["unit_code"], EXACT_UNIT)
        self.assertEqual(
            response["query_resolution"],
            {
                "input_query": RANK_QUERY,
                "resolved_unit_code": EXACT_UNIT,
                "method": "ncs_unit_name_fallback",
            },
        )
        self.assertEqual(
            set(response["job_base_links"][0]),
            set(server.PUBLIC_JOB_BASE_LINK_FIELDS),
        )
        self.assertEqual(
            response["summary"],
            {
                "returned_count": 1,
                "total_count": 1,
                "truncated": False,
                "requested_limit": 3,
                "applied_limit": 3,
            },
        )
        self.assertLessEqual(_json_size(response), 2_000)

    def test_job_base_payload_caps_links_and_reports_truncation(self) -> None:
        raw_rows = [
            {
                "unit_code": EXACT_UNIT,
                "unit_name": RANK_QUERY,
                "job_base_competency_id": index,
                "job_base_factor_id": index,
                "competency_name": f"공통역량 {index}",
                "factor_name": f"하위요소 {index}",
                "major_code": "01",
                "major_name": "경영",
                "link_method": "unit_code_exact",
                "confidence_score": 1.0,
                "review_status": "auto_linked",
                "source_payload": PRIVATE_SENTINEL,
            }
            for index in range(1, 5)
        ]
        with (
            patch.object(server, "job_base_search_links", return_value=raw_rows),
            patch.object(server, "job_base_count_links", return_value=9),
        ):
            response = server.search_job_base_competencies(
                unit_code=EXACT_UNIT,
                limit=20,
            )

        self.assertEqual(len(response["job_base_links"]), 3)
        self.assertEqual(response["summary"]["total_count"], 9)
        self.assertTrue(response["summary"]["truncated"])
        self.assertEqual(response["summary"]["applied_limit"], 3)
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(response, ensure_ascii=False))
        self.assertLessEqual(_json_size(response), 2_000)

    def test_career_query_is_applied_and_ontology_exact_name_ranks_first(self) -> None:
        career = server.ncs_analysis(mode="career_path", query="인사기획", limit=10)
        self.assertTrue(career["ok"], career)
        self.assertEqual(
            [row["job_name"] for row in career["career_paths"]],
            ["인사기획"],
        )

        ontology = server.ncs_analysis(mode="ontology", query=ONTOLOGY_QUERY, limit=10)
        self.assertTrue(ontology["ok"], ontology)
        self.assertEqual(ontology["concepts"][0]["concept_name"], ONTOLOGY_QUERY)
        self.assertEqual(ontology["concepts"][0]["concept_id"], self.seed["exact_concept_id"])

    def test_public_guard_hides_raw_exception_and_mcp_instructions_exist(self) -> None:
        secret_exception = "RAW_DATABASE_DETAIL_MUST_NOT_LEAK"
        with patch.object(server, "search_ncs", side_effect=RuntimeError(secret_exception)):
            response = server.ncs_search(query="예외 유도", scope="unit", limit=1)

        serialized = json.dumps(response, ensure_ascii=False)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "tool_execution_failed")
        self.assertNotIn(secret_exception, serialized)
        self.assertNotIn("Traceback", serialized)
        self.assertEqual(
            response["error"]["message"],
            "The tool failed while reading its configured NCS data.",
        )

        instructions = server.mcp.instructions
        self.assertIsInstance(instructions, str)
        self.assertGreater(len(instructions.strip()), 100)
        self.assertIn("HRMCP", instructions)
        self.assertIn("ncs_search", instructions)


if __name__ == "__main__":
    unittest.main()
